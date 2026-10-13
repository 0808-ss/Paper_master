# GStreamer 负载测试与诊断（2026-10-13）

## 当前传输模式

`demo_dubao.py` 默认仍为 `VIDEO_MODE=synthetic`：按视频 CSV 的尺寸和间隔生成模拟帧。`VIDEO_MODE=gst` 选择独立的 `video_sender_gst` / `video_receiver_gst`，发送负载来自实时 GStreamer H.264 编码器，CSV 仅提供网络抖动日历。

```mermaid
flowchart LR
  src[实时 I420 视频源] --> tee[源时间戳与参考分支]
  tee --> raw[source.yuv]
  tee --> enc[x264 / 无 B 帧 / h264parse]
  enc --> au[每 AU 一个单向 QUIC Stream]
  au --> net[NewReno / 多路径 default、rr、minrtt]
  net --> rx[FIN + 长度 + CRC 校验]
  rx --> order[排序 / 截止期 / WAIT_IDR]
  order --> dec[解码]
  dec --> play[固定播放槽 / 冻结或黑帧 / playout.yuv]
```

- h1 主动向 h2 推送；stream 0 为双向控制流，负责 SETTINGS、READY、共同起点、END、PLAYOUT_DONE。媒体流号 2、6、10…，一 AU 一流，保留可靠重传；完整接收后不一定赶上播放截止期。
- 默认地面网段 `10.0.1.0/24`，空中网段 `10.0.2.0/24`。路径探测和回调负责建立多路径；配置 `rr` 不等于已证明两条物理路径均发送媒体。
- I/BP 抢占打开时，库在 I 暂存队列非空的 drain 中会丢弃当时 BP 暂存队列，I 流获优先级 0。已经进入普通 QUIC 流队列的数据保持原有可靠传输。它不是所有旧 BP 流的即时取消。
- 当前库实际记录为 NewReno。发送日志旧 `PKT_SEND path=` 表示接口索引，不能当成 qlog 的路径 ID；新日志显式添加 `if_index`、`local`、`peer`。qlog 统计以 stream 归因为准，避免把 ACK、控制流或包内重复数据当成独立视频帧。
- 现有实现仅接实时测试图，尚未接 file / camera / RTSP，也没有自动码率适配。这里的“真实负载”指真实 H.264 编码字节经 QUIC 传输；真实场景内容的画质结论需要后续接入实际视频源。

## 对本轮远端输出的判断

远端 `be14420`，2560×1440 / 30 fps / 360 帧 / ball / 6000 kbit/s / preempt=0 / rr：功能通过，两端退出 0，生成、准入、完整接收均 360；媒体文件检查通过。

时序未通过：网络更新跳过 3 个抖动日历时点，最大更新滞后 36.266 ms；帧生成最大滞后 46.351 ms；阈值 31.667 ms。3 个跳过事件不代表丢失 3 个视频帧。完整接收也不证明按时播放。

现有 manifest 缺实际 AU 码率、媒体路径分布及播放新帧/冻结结果，因此不能证明这轮有 6 Mbit/s 编码负载或链路饱和。ball 易压缩，目标码率需要用实际 AU 字节验证。

## 本次修补及边界

1. TC 运行更新改为按节点分组 `tc -batch -`，用独立 `Node.popen` 并行执行；两条链路同时变化最多 3 个 batch，避免串行 8 次交互 shell 命令。仅带宽变化时改 HTB，仅时延变化时改 netem；固定句柄、阈值、队列上限和 Travel 的 ceiling 查询规则保留。每个子进程检查退出码、2 秒超时后 kill/wait；任一错误使实验失败，不把部分更新当作成功。
2. 回放同时在原 10 ms 网格和抖动事件边界唤醒；错过的日历事件仍计入失败。`link_metrics.csv` 追加 scheduled_time、tc_apply_ms、tc_commands、tc_batches；manifest 增加 TC 耗时及独立的网络/源时序标志。
3. 媒体日志改为 64 KiB 缓冲、循环中约每 100 ms 刷新，缓冲满时也会写出；共同起点立即刷新。发送端逐包刷新 QUIC 发包时间，实验头记录 UDP send 前的时间，成功发送后记录日志；刷新网络循环及等待前的过期时间值。日志和 qlog 都保留，实际运行仍有观测开销。
4. live 源在握手前进入 PAUSED 准备任务，至共同起点才 PLAYING；PAUSED 不产生媒体，不等待 live sink preroll。第一帧的渲染和编码初始化成本仍可能存在。新增 SOURCE_START 记录状态切换耗时，拆出 source_startup_lag_ms / source_pacing_lag_max_ms。**原始 frame_generation_lag_max_ms 和 timing_valid 的严格判定保留，不将起点重置到第一帧以抹去失败。**
5. 新增 `snow` 编码源作为压力场景，不做字节填充、不强制 CBR padding；它可能增加 CPU 压力，仍需实测码率。`ball` / `zone-plate` / `smpte` 原选项保留。
6. RUN_TAG 在启动前检查并预留独立目录，重复标签或路径穿越值直接失败，避免混合两轮 qlog/媒体文件。
7. `media_load.py` 在独立归档上验证原始索引、PTS 和编码 CRC，输出 `load_report.json`；无需 FFmpeg / VMAF。包含媒体时间和源实际经过时间的编码码率、逐秒编码/准入码率、未准入帧、完整 AU 数、软件播放新帧/黑帧/冻结/延迟，以及 UDP 地址流量。
8. 媒体路径证据来自同轮 `delay_analysis/qlog_packet_delay.csv` 的注册 AU 流，结合 `[PATH]` API 地址映射归并地面/空中物理路径；仅统计两端匹配的媒体包，含重传，未收到的发送包不计入该证据。四列 qlog 无 path_id 或地址映射不明确时不宣称物理分流。

批量更新不具有跨接口原子性；csv 的 applied_time 表示所有 batch 完成，单个接口实际更新发生在 begin 与完成之间。修补旨在减少开销，不保证普通 Linux 调度一定达到硬实时阈值。新采样和日志策略需使用修补后的共同版本重新跑基线，不能把旧版本直接作为严格算法对照。

实现依据：[iproute2 的 tc batch 入口](https://github.com/iproute2/iproute2/blob/main/tc/tc.c)、[GStreamer live source 状态语义](https://gstreamer.freedesktop.org/documentation/additional/design/live-source.html)、[videotestsrc 的 snow 源](https://gstreamer.freedesktop.org/documentation/videotestsrc/)。

## 远端同步与基础检查（仅在远端执行）

本地不编译、不运行程序或测试。当前版本标签 `gst-load-fix-v2`，修改仅外层 APP 和记录文档；picoquic 保持 `94f48cb746de101fbb88aee5decfe14b28b04496`，无需新 bundle 或重编译库。因为本轮改了媒体 C 文件及配置头，必须重编译媒体二进制。

```bash
cd /home/duwenji/ljy/Paper_master
# 本地修补提交发布后，拉取对应版本；远端有改动时先自行保留。
git pull --ff-only origin main
git fetch origin tag gst-load-fix-v2
git rev-parse HEAD
git rev-parse 'gst-load-fix-v2^{commit}'
git submodule status
cd APP
make -B media
make media-protocol-check
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'
# 只查询本机 loopback 的 qdisc；验证持久 tc 的 JSON 完成响应，不修改网络。
sudo /usr/bin/python3 tests/check_tc_persistent.py
pkg-config --modversion gstreamer-1.0
ldd ./video_sender_gst
ldd ./video_receiver_gst
df -h .
```

新增 13 项远端回归用例：TC 的选择性 change、参数拒绝、batch 分组、阈值累计、失败传播、超时清理；负载的目标/实测区别、编码停顿、启动偏移、完整但冻结、媒体路径过滤、无路径 qlog、部分准入。这里写的是待执行用例，不是已经通过的测试结果。

v2 在这 13 项之上增加 11 项 Python 用例，以及 C 协议检查中的配置/优先级断言。新增用例覆盖持久进程复用、准备失败清理、JSON 响应、TC 错误、超时后禁止复用、实际迟到与恢复边界、两端窗口参数及优先级与丢弃独立性。Linux pipe 用例和只读兼容性检查仅在远端执行，本地不以 AST 检查替代运行通过证据。

仍按已有验收文档统一 GStreamer 工具、插件、编译库和运行库环境；不要混用 conda 工具与另一路系统插件。

## 第二轮反馈修补：持久 TC 与媒体流控窗口（v2）

v1 的 1080p / snow / 6000 kbit/s / 300 帧远端结果已确认：实际编码/准入均 6092.6 kbit/s，300 AU 完整收到；两路径媒体承载通过，地面 3966 包、空中 1751 包。源时序通过（启动偏移 31.194 ms、后续最大偏差 0.084 ms），但网络回放跳过 1 个时点、更新最大滞后 33.093 ms；4.800 s 对应 TC 更新 32.960 ms，属于视频有效区间内，不是排空期间误报。

首 AU 为 275440 字节，编码耗时 18.228 ms，编码完成到完整接收为 630.778 ms。其完整接收比解码准入截止期晚 500.200 ms，引发槽 0～29 黑屏，帧 30 IDR 恢复。帧 107、108、109 分别晚于准入截止期 69.499、52.017、21.693 ms；110～119 等待 IDR，槽 107～119 重复帧 106，帧 120 IDR 恢复。这些是完整但迟到的 AU，不是 CRC 损坏或永久缺帧。

库源码的默认单向流额度为 65535 字节，默认连接额度 1048576 字节，普通流优先级为 9（同级 FIFO）。首 AU 加封装为 280136 字节，超过默认单流额度；需要 MAX_STREAM_DATA 更新，流控受阻时后续较小流可以先完成。此参数不匹配是可验证的停顿因素，尚不能仅靠汇总断言 630.778 ms 全由流控造成；I/BP 同级并不等于流级 RR。

v2 改动：

- `TC_BACKEND=persistent` 为新默认。按节点在共同起点前创建 3 个常驻 `tc -j -batch -`，复用进程及 netlink socket；每组 change 后追加只读 `qdisc show dev lo`，等完整 JSON 查询结果作为前序命令完成的确认。不开 `-force`；change 失败使 tc 在确认查询前退出。遇到退出、EOF、超时或非法响应就终止该 worker，不复用可能迟到的响应。关闭时只结束自有进程，不清理任何 qdisc。`tc_process_starts` 正常为 3，`tc_batches` 仍表示更新批次，`tc_commands` 不包含确认查询。
- 显式 `TC_BACKEND=batch` 可对照 v1 的每次派生进程方式；不静默回退。需要 coreutils 的 stdbuf 和 tc 的 JSON/batch 支持；先执行只读兼容性检查。实现依据：[iproute2 v5.5 的顺序 batch 循环及逐命令刷新](https://github.com/iproute2/iproute2/blob/v5.5.0/tc/tc.c)、[完成查询的 JSON 输出](https://github.com/iproute2/iproute2/blob/v5.5.0/tc/tc_qdisc.c)。
- 媒体模式默认将两端接收单向流额度设为 1 MiB、连接额度设为 8 MiB，在连接创建/握手前配置。分别由 `MEDIA_STREAM_WINDOW_BYTES` / `MEDIA_CONNECTION_WINDOW_BYTES` 覆盖；范围 65535～32 MiB，连接额度不得小于单流额度。流控额度不是拥塞窗口，不绕过 NewReno/cwnd/pacing，也不预分配额度大小的缓存；既有接收/排序内存上限仍会使超限实验失败。大于单流初始额度的 AU 仍可能需要追加信用。
- `[FLOW_CONTROL]` 记录实际本地/对端协商额度；`[AU_FLOW_WINDOW]` 记录超过对端初始额度的 AU。判断传输参数以该记录为准，而不是只看请求值。接收端保存的是本地配置；sender 配置和 manifest 为发送策略依据。
- `MEDIA_I_PRIORITY=0` 为默认，保留原流级调度。显式设 1 后仅对标记为优先 I 的媒体流调用原库 priority=0 API，其他流不改；`PREEMPT_CFG=0` 时不会因此启用 BP 丢弃，`GOP_SIZE_CFG=0` 的 all-BP 仍不提升流。开启原 preempt 后原有 I 优先/BP 丢弃行为仍存在，独立开关不能关闭库机制自身的 I 优先。
- `load_report.json` 增加实际窗口、黑屏/重复区间、IDR 和恢复边界的接收减准入截止期、编码完成到完整接收时间。差值为正说明迟到。新诊断也可重算旧归档；旧版本没有窗口日志时不伪造协商值。

共享起点、严格时序阈值、200 ms 播放缓冲、20 ms 解码余量、可靠重传、WAIT_IDR、网络日历和 CC 保持原口径。新窗口与可选优先级属于实验配置变化：先固定新版本做对照，不直接把 v1/v2 差异归为某个拥塞算法收益。I 优先不能让已经错过截止期的 P 帧恢复，也不保证首帧一定按时完成。

## v2 最小复验与单因素对照

先重复同一轮 1080p / snow / 6000 kbit/s / 10 秒，保持 preempt=0、I priority=0，只验证常驻 TC 和新窗口。分析目录由 sudo 实验创建，因此分析也使用 sudo。

```bash
RUN_TAG="gst_v2_window_$(date +%Y%m%d_%H%M%S)_$$"
sudo env VIDEO_MODE=gst TC_BACKEND=persistent \
  MEDIA_WIDTH=1920 MEDIA_HEIGHT=1080 MEDIA_FPS=30 MEDIA_FRAMES=300 \
  MEDIA_BITRATE_KBPS=6000 MEDIA_GOP=30 MEDIA_PATTERN=snow MEDIA_PREVIEW=0 \
  MEDIA_PLAYOUT_MS=200 MEDIA_DECODE_MARGIN_MS=20 \
  MEDIA_I_PRIORITY=0 MEDIA_STREAM_WINDOW_BYTES=1048576 MEDIA_CONNECTION_WINDOW_BYTES=8388608 \
  PREEMPT_CFG=0 GOP_SIZE_CFG=30 SCHEDULER_CFG=rr \
  NETWORK_JITTER_TIMING_TRACE="$PWD/4k_30f_trace_0.8.csv" RUN_TAG="$RUN_TAG" \
  /usr/bin/python3 demo_dubao.py
sudo /usr/bin/python3 qlog_delay_scatter.py --run-dir "runs/$RUN_TAG" --clock-mode same-clock
sudo /usr/bin/python3 media_load.py "runs/$RUN_TAG" \
  --min-encoded-kbps 4500 --min-admitted-kbps 4500 --require-timing --require-multipath
/usr/bin/python3 -m json.tool "runs/$RUN_TAG/run_manifest.json"
/usr/bin/python3 -m json.tool "runs/$RUN_TAG/load_report.json"
```

在新版本上逐轮修改以下参数，其他配置、输入、播放缓冲均相同；每轮换 RUN_TAG，不同时运行多个 demo。

| 对照组 | TC_BACKEND | 单流/连接初始额度（字节） | MEDIA_I_PRIORITY | 目的 |
|---|---|---|---|---|
| A | batch | 65535 / 1048576 | 0 | 复现 v1 配置 |
| B | persistent | 65535 / 1048576 | 0 | 单独核对进程复用与 TC 时序 |
| C | persistent | 1048576 / 8388608 | 0 | 相对 B 核对流控窗口与关键帧时延 |
| D | persistent | 1048576 / 8388608 | 1 | 相对 C 核对 I 优先的额外影响 |

上述最小复验对应 C 组，方便先确认新默认能否达标；归因需要补 A/B/D 组并重复多轮。不要通过提高验收阈值或将共享起点移到第一帧来“通过”时序。v2 本地仅静态检查，常驻 tc 的远端兼容性、性能收益和首 IDR 改善仍待运行确认。

## 分阶段执行

所有命令沿用同一份 Travel / ground / sky / 网络抖动日历，固定种子 42。为每次执行设置唯一 RUN_TAG。不要并行运行多个 demo：顶层日志、qlog 目录和 Mininet 网络仍是共享的。

### 1. 轻负载时序基线

先用 720p / ball / 12 秒重新确认起点和回放。此阶段不要求达到目标码率。

```bash
RUN_TAG="gst_baseline_$(date +%Y%m%d_%H%M%S)_$$"
sudo env VIDEO_MODE=gst MEDIA_WIDTH=1280 MEDIA_HEIGHT=720 \
  MEDIA_FPS=30 MEDIA_FRAMES=360 MEDIA_BITRATE_KBPS=2000 MEDIA_GOP=30 \
  MEDIA_PATTERN=ball MEDIA_PREVIEW=0 PREEMPT_CFG=0 SCHEDULER_CFG=rr \
  RUN_TAG="$RUN_TAG" /usr/bin/python3 demo_dubao.py
/usr/bin/python3 qlog_delay_scatter.py --run-dir "runs/$RUN_TAG" --clock-mode same-clock
/usr/bin/python3 media_load.py "runs/$RUN_TAG" --require-timing --require-multipath
/usr/bin/python3 -m json.tool "runs/$RUN_TAG/load_report.json"
```

功能完成且严格时序达标后再做算法对比。若报告返回非零，报告仍已保存；先按下节定位原因，不仅提高阈值或播放缓冲掩盖失败。RR 要求两路径媒体证据；MinRTT 不要求均匀分流或两路径都承载大量媒体。

### 2. 真实编码压力（先 1080p，再复验原 1440p）

先跑 1080p / 30 fps / 10 秒 / snow / 6000 kbit/s，限制在当前网络抖动日历内。4500 kbit/s 是本次压力验收的可调下限，约为目标的 75%，不是编码器保证。分别要求实际编码和准入码率达标；未达标就不能将该轮称为所需负载下的传输实验。`udp_send_totals` 另报告源产生期间的实际发送码率及排空阶段字节，包含控制/重传，不能与纯 AU 码率混用。

```bash
RUN_TAG="gst_load_1080p_$(date +%Y%m%d_%H%M%S)_$$"
sudo env VIDEO_MODE=gst MEDIA_WIDTH=1920 MEDIA_HEIGHT=1080 \
  MEDIA_FPS=30 MEDIA_FRAMES=300 MEDIA_BITRATE_KBPS=6000 MEDIA_GOP=30 \
  MEDIA_PATTERN=snow MEDIA_PREVIEW=0 PREEMPT_CFG=0 SCHEDULER_CFG=rr \
  RUN_TAG="$RUN_TAG" /usr/bin/python3 demo_dubao.py
/usr/bin/python3 qlog_delay_scatter.py --run-dir "runs/$RUN_TAG" --clock-mode same-clock
/usr/bin/python3 media_load.py "runs/$RUN_TAG" \
  --min-encoded-kbps 4500 --min-admitted-kbps 4500 --require-timing --require-multipath
```

两份 1080p / 300 帧 I420 raw 总计约 1.74 GiB，持续写入合计约 178 MiB/s；质量评估还需约 0.87 GiB 编码参考 raw 和额外 CPU。源盘、接收盘实际是同机同目录文件系统，会相互竞争。1440p / 30 fps 时两份 raw 持续写入合计约 316 MiB/s；原 360 帧两份 raw 约 3.71 GiB。同步观察 `pidstat` / `iostat`（远端如已安装），用 SOURCE_START、source/encoded 时间线和 tc_apply_ms 区分 CPU、写盘与 TC 开销。

若 1080p 稳定，再将尺寸换为 2560×1440、帧数换为 360，使用新的标签；先 ball 复验原输出，再 snow 测试压力，其他网络配置相同。当前 `4k_30f_trace_0.8.csv` 只有 349 个时点：原 360 帧和更长实验末段会保持最后一个抖动事件，Travel 仍继续查询到最后一个点后保持末点。manifest 的 `jitter_calendar` 明确记录日历长度和保持末事件的时长。若测 30 秒以上，需显式提供覆盖全程的 `NETWORK_JITTER_TIMING_TRACE` 并对各组固定同一份输入、归档哈希；本次没有暗中循环或扩展原日历。

### 3. 码率阶梯与消融

先在 `zone-plate` 或 `snow` 上逐轮固定码率 2000、4000、6000、10000 kbit/s，确认实际 AU 和准入负载随码率改变。要测运行中调整，可按原验收文档使用本轮打印的 control socket 和 `media_bitrate.py`；不要假定命令 epoch 就是编码器精确生效边界。

时序和负载基线可复现后，再对相同参数跑 preempt=0/1 × default/rr/minrtt，并重复多次。开启抢占后允许完整 AU 数减少、冻结增多，这是要评估的结果；不以 100% 完整/播放作为算法优越的前提。丢帧、排空耗时、队列上限失败都需保留。每一轮单独分析，禁止复用 RUN_TAG。

### 4. 完整画质评估

```bash
# 此步骤要求所用 ffmpeg 含 psnr、ssim、libvmaf，且每轮尚未生成 quality/。
/usr/bin/python3 media_quality.py "runs/$RUN_TAG"
```

画质脚本会保存完整时间轴的 encoding、transport_playout、end_to_end 三组结果。先看 load_report 的实际码率/播放结果，再看画质；测试图结果只验证管线，不代表实际场景视觉质量。

## 时序仍失败时的定位

| 现象 | 核查证据 | 下一步 |
|---|---|---|
| max_tc_apply_ms 高、frames_skipped 增加 | link_metrics 的 batch 次数、耗时与 applied_time | 检查 CPU 调度和 tc 返回输出；必要时设计持久 netlink 回放器，重新验证同一日历 |
| SOURCE_START state_change_us 高 | 控制 READY/START、begin/returned 时间 | 检查源启动与线程创建；PAUSED 准备不保证首帧成本消失 |
| state_change 小，但 startup_lag_ms 大 | source_frames 首帧、FRAME_GEN、encoded_ready | 检查首帧渲染/编码初始化；不能仅将 t0 移到首帧来宣称修复 |
| 启动偏移小，source_pacing_lag 持续增加 | 每帧 source 时间与 PTS、编码时间差 | 检查实时编码 CPU、参考写盘队列和磁盘吞吐 |
| 编码达标但准入不足 | admission_deadline_drops、unadmitted_frames | 检查网络线程和本地排队；编码码率不能代替实际提交负载 |
| 完整 AU 多，new_frame_ratio 低 | receive_frames、playout 时间线、WAIT_IDR、冻结 | 区分迟到接收、等待关键帧和解码/输出压力 |
| rr 只有一条媒体路径 | PATH、PROBE、qlog_packet_delay 的 AU stream | 检查多路径协商/校验、cwnd/pacing 与地址映射；不用 qlog 展示标签猜物理路径 |

反馈至少包括构建/测试输出、该轮 run_manifest.json 与 load_report.json、send.log 中 SOURCE_START/PATH/PROBE、link_metrics.csv，以及画质报告（若执行）。本次本地仅静态审查；新版本的性能、时序和端到端结果待远端反馈。
