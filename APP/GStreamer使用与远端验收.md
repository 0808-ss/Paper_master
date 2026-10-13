# GStreamer 媒体模式：使用与远端验收

本版源代码已经加入，按照 `项目.md` 未在本地编译或运行。以下命令仅供远端 Linux / Mininet 执行。picoquic 子模块保持 `94f48cb746de101fbb88aee5decfe14b28b04496` / `1.1.44.2`，无需生成新的 bundle。

## 构建与检查

需要原 APP 的 picoquic / picotls / OpenSSL 静态库，以及 GStreamer 的 core、app、video 开发包；运行时需包含 videotestsrc、x264enc、h264parse 和 avdec_h264 插件。画质评估还需要 FFmpeg / ffprobe 和支持 libvmaf 的 FFmpeg 构建。

```bash
cd ~/Paper_master/APP
pkg-config --modversion gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0
gst-inspect-1.0 videotestsrc
gst-inspect-1.0 x264enc
gst-inspect-1.0 h264parse
gst-inspect-1.0 avdec_h264
make media
make media-protocol-check
python3 -m unittest discover -s tests -p 'test_media_quality.py'
```

`make` 仍只构建原模拟程序；`make media` 新增 `video_sender_gst`、`video_receiver_gst`。两套入口分开，避免改变既有模拟程序源码。GStreamer 和 picoquic 的上下文由不同线程管理，只有网络主线程调用 picoquic。

运行时会检查 x264enc 的 bitrate 属性是否可以在 PLAYING 状态更新。不支持或缺插件会明确失败，不会替换传输实现、重启 QUIC 或偷偷换编码器。

## 使用原网络 trace 运行

```bash
sudo env VIDEO_MODE=gst python3 demo_dubao.py
```

默认使用运动球测试图、H.264、1080p、30 fps、300 帧、初始目标 4000 kbit/s、GOP 30、无 B 帧、200 ms 播放缓冲。负载为编码器实际 AU 字节；没有固定帧大小填充。

保持 Travel、ground / sky 网格、两条路径、策略路由、HTB / netem、10 ms 网络更新时间和原抖动 RNG 参数。原视频 CSV 仅用第二列生成网络抖动切换日历，默认与原 `4k_30f_trace_0.8.csv` 相同。它不再决定媒体帧大小、媒体 fps 或媒体时长。

`VIDEO_MODE` 默认为 `synthetic`，可继续使用原实验；真实媒体模式显式选择 `gst`。

可配置项：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| MEDIA_WIDTH / MEDIA_HEIGHT | 1920 / 1080 | I420 必须为偶数；本版最大 3840×2160 |
| MEDIA_FPS | 30 | 1～120，固定帧率 |
| MEDIA_FRAMES | fps×10 | 1～18000，控制源时长 |
| MEDIA_BITRATE_KBPS | 4000 | 初始平均码率目标，100～100000 |
| MEDIA_GOP | GOP_SIZE_CFG 或 30 | 实际编码 GOP，1～600 |
| MEDIA_PLAYOUT_MS | 200 | 20～5000 ms |
| MEDIA_DECODE_MARGIN_MS | 20 | 小于播放缓冲；完整 AU 的解码准入余量 |
| MEDIA_PATTERN | ball | ball / zone-plate / smpte |
| MEDIA_PREVIEW | 0 | 0 为无窗口定时播放输出；1 为额外窗口预览，需要远端图形环境 |
| PREEMPT_CFG | 1 | 继续使用现有库 I/BP 准入抢占开关 |
| GOP_SIZE_CFG | 30 | 在媒体模式中是编码 GOP 默认来源；0 只把传输标记全部改为 BP，编码仍有 IDR |
| SCHEDULER_CFG | default | default / rr / minrtt |
| NETWORK_JITTER_TIMING_TRACE | VIDEO_TRACE 或原 4k CSV | 网络抖动日历；只使用间隔，不使用 size |
| RUN_TAG | 自动 | 每轮归档标签 |

`ball` 使用帧计数驱动动画；`zone-plate` 设置 kx2=20、ky2=20、kt=1，产生随时间变化的纹理；`smpte` 设置滚动速度 8。运动球内容容易压缩，实际码率可能远低于目标。对编码码率变化做压力验证时建议改用 zone-plate，再用真实内容复验。

例如：

```bash
sudo env VIDEO_MODE=gst MEDIA_PATTERN=zone-plate MEDIA_FRAMES=600 PREEMPT_CFG=0 SCHEDULER_CFG=rr python3 demo_dubao.py
```

1080p/30/10 秒的原始参考及播放输出各约 0.93 GB；离线编码参考解码还需约 0.93 GB。启动器检查两条原始序列所需磁盘余量。较高分辨率 / fps 必须结合远端 CPU、磁盘和队列日志确认时序有效。

## 运行时调整编码码率

启动日志会打印每轮唯一的 `/tmp/mpquic_....sock` 路径。在另一终端，将实际路径代入：

```bash
sudo python3 media_bitrate.py /tmp/mpquic_实际运行编号.sock 2000
sudo python3 media_bitrate.py /tmp/mpquic_实际运行编号.sock 6000
```

接口使用本机 Unix socket，每个连接接收一条 JSON Lines 请求。实验以 sudo 运行时 socket 属于 root，权限 0600，所以控制命令也使用 sudo。

```json
{"op":"set_bitrate","command_id":7,"target_kbps":2500}
```

回复 `applied_to_element` 表示编码器属性已更新，不意味着下一帧精确达到目标。`send.log` 记录属性值、控制 ID、时间和 epoch；`encoded_frames.csv` 记录每个 AU 在回调观察到的 epoch。epoch 是观察标记，不是插件提供的逐帧生效证明。`[ENCODED_RATE]` 和离线报告按真实 AU 大小计算输出码率。

这里使用 x264 的 ABR 模式：插件枚举名是 `pass=cbr`，实际是平均码率控制，不给每帧补齐恒定字节数。参数更新保持 fps、尺寸、GOP、QUIC 连接及网络 trace；首版只提供外部控制入口，没有额外自动码率算法。

源 EOS 后控制接口关闭，不接受在已结束的视频上修改码率。

## qlog 时延图修补与同步（gst-delay-scatter-fix-v1）

gst 模式使用真实编码 AU；一 AU 一条单向 QUIC 流。小 AU 常由单个 QUIC 包承载，其包时延和帧传输时延坐标重合。旧图中的实心帧标记遮住了包点，不能据此认定漏计视频包。修补后使用空心帧轮廓与置顶实心包点，并保存三张图：

- `qlog_frame_packet_delay_scatter.png`：帧与包合并图。
- `qlog_packet_delay_scatter.png`：仅包点。
- `qlog_frame_delay_scatter.png`：仅帧点。

默认对数纵轴显示全部正延迟，显式 `--y-floor-ms` 才裁剪；0 或负时延保留 CSV，但无法绘在对数轴。Path 2 摘要合并 path 1/2 原始样本再算分位数。图/CSV 的帧传输延迟不含解码或播放缓冲。控制流 0 和纯 ACK 包仍排除；包中同时含视频与控制数据时，仅保留视频流归因。

本地 Windows PowerShell 发布已提交修补（以下命令由用户执行）：

```powershell
Set-Location D:\Paper_master
git push origin main
git push origin refs/tags/rollback-before-gst-delay-scatter-fix refs/tags/gst-delay-scatter-fix-v1
```

远端只需同步外层仓库；picoquic 不变，不需要新 bundle 或重编译媒体程序：

```bash
cd /home/duwenji/ljy/Paper_master
git pull --ff-only origin main
git fetch origin tag gst-delay-scatter-fix-v1
git diff --exit-code gst-delay-scatter-fix-v1 -- APP/qlog_delay_scatter.py APP/tests/test_qlog_delay_scatter.py
cd APP
python -m unittest discover -s tests -p 'test_qlog_delay_scatter.py'
```

重新分析当前 APP 目录中已有的这轮日志，无需重跑网络实验：

```bash
python qlog_delay_scatter.py --outdir delay_analysis_gst --outlier-rule cap:1500
```

对于已归档轮次，将 `实际归档标签` 替换为该轮目录名：

```bash
python qlog_delay_scatter.py --run-dir runs/实际归档标签 --outlier-rule cap:1500
```

此时 qlog、send.log、recv.log 与 manifest 从同一轮读取，输出写入该轮 `delay_analysis/`。每端有多份 qlog 时必须用 `--sender` / `--receiver` 明确选择同一连接的文件；不按文件名排序猜测。已知 gst 的归档缺 send.log 时明确失败。`timing_valid=false` 会打印提示，实际传输时延仍可观察，严格算法对比需另验收网络回放时序。

本地遵守《项目.md》只进行源码静态检查，未执行上述测试、重新画图或网络实验。远端应确认本轮仍为 156 个视频包、150 个完整帧、5 个 I 帧，合并图可见实心包点和空心帧轮廓，包独立图包含包散点，终端仅有一行 Path 2 摘要。默认设置不应再出现 p5 裁掉 14 个低延迟样本的提示。

## 归档与画质报告

每轮归档位置为 `APP/runs/<标签>/`。除了原 qlog、send.log、recv.log、link_metrics.csv、TC 状态和 manifest，新增：

```text
network_jitter_events.csv       预生成的网络抖动日历
media/sender/media_config.json  初始媒体配置
media/sender/source.yuv         实际编码前参考，BT.709 I420
media/sender/source_frames.csv  参考帧索引、PTS、采集时间
media/sender/encoded.h264       完整的发送端编码输出（含后来未被准入的 AU）
media/sender/encoded_frames.csv 每个 AU 的大小、IDR、CRC、时间、epoch
media/receiver/playout.yuv      完整播放时间轴，包含重复帧和黑帧
media/receiver/playout_frames.csv 播放决定、输出、源帧编号及时间
media/receiver/receive_frames.csv 关闭连接时的最终收帧 / 解码状态
```

启动器只在两个进程成功退出、raw 文件长度和逐帧索引正确时声明 `media_output_valid=true`。源码和二进制不匹配、缺少 VIDEO_START、缺 qlog、异常 FIN、参考支路掉帧或写盘失败会留下失败归档。

网络回放仍继续记录 `timing_valid`。相同输入的计划网络条件相同，但受 CPU / TC 执行影响，实际应用时间可能不同，比较六组实验时要同时检查该标志。

网络实验结束后再执行画质评估，避免 VMAF 与实验争抢 CPU：

```bash
python3 media_quality.py runs/实际归档标签
```

得到 `quality/quality_report.json`、`frame_quality.csv`、三组逐帧 PSNR / SSIM / VMAF 文件以及 FFmpeg 日志。评估目录要求尚不存在，防止覆盖已有报告；如果上一轮评估失败，保留或改名旧 quality 目录后重试。

三组比较分别是：编码前参考 vs 完整编码本地解码、完整编码本地解码 vs 实际播放、编码前参考 vs 实际播放。报告保留冻结和黑帧，不把丢帧后剩余帧压缩到一条更短的时间轴；非线性分数不作简单相减。

另外输出实际码率窗口、epoch 分段质量、按时新帧比例、完整 AU 比例、冻结时长和采集 / 编码 / 接收 / 播放延迟。时间是软件播放决定及写盘 / 预览提交时刻，不是屏幕物理扫描时间。`playout_timing_valid` 同时检查网络线程 tick 和输出线程的迟到是否超过一帧。

需要原 qlog 延迟 / 路径报告时：

```bash
python3 qlog_delay_scatter.py --sender runs/实际归档标签/qlog_sender/实际文件.qlog --receiver runs/实际归档标签/qlog_receiver/实际文件.qlog --outdir runs/实际归档标签/network_quality
```

媒体模式只统计 STREAM_MAP 明确注册的媒体流；新增的 stream 0 控制消息和纯 ACK 包不会被当作视频帧或视频数据包。UDP 发送日志不猜测某个包对应哪一帧；准确归属看 qlog，路径 ID 和接口编号分别记录。

## 播放与依赖策略

接收端先完成 AU 的长度 / 包序 / CRC 校验，再按解码序号提交。完整 AU 提前解码；到每个播放时间槽必须选择对应的按时解码帧，否则重复上次画面，尚无画面时输出黑帧。

缺失、过期或依赖断裂后采用 WAIT_IDR，后续预测帧不作为正常链继续提交，完整 IDR 到达后重置解码器恢复。首版关闭 B 帧，解码器使用单工作线程，减少解码重排 / 线程缓冲的额外延迟。

完整但迟到的 AU 仍计入最终 transport=complete，但不会回填以前的播放槽。未完整到达的帧记为 `unreceived_unknown_origin`；仅凭该状态不能区分网络中断、准入丢弃和原库的暂存 BP 丢弃，可结合 FRAME_GEN、MEDIA_DROP、STREAM_MAP 和 qlog 分析。

本版保留已提交 Stream 的可靠重传，不在播放过期时自动 reset 流。PREEMPT=1 的库行为仍仅针对当时留在 BP 暂存队列中的数据；不保证每轮都会发生 BP 丢弃。

## 远端验收顺序

1. 执行构建及两组基础测试。回传编译输出、GStreamer / x264 插件版本和 picoquic commit。
2. 先以 720p / 少量帧验证资源与媒体时间轴，再验证默认 1080p 配置。检查 SOURCE_FINISHED、PLAYOUT_FINISHED、MEDIA_EXIT、完整 raw 长度、CRC 和 qlog。
3. 检查两条路径校验完成；RR 有真实媒体承载，MinRTT 不要求平均分流。原 trace 下出现丢帧 / 冻结是实验结果，不以“全部帧按时”作为成功条件。
4. 用运动纹理源测试 4000→2000→6000 kbit/s 调整，检查属性值和实际 AU 码率窗口，确认路径 / 连接没有重建。
5. 对同一内容、网络日历和编码配置比较 preempt=0/1 × default/rr/minrtt 六组，检查实际时序、帧完整性、播放质量与资源压力。
6. 核查末尾帧、FIN 携带数据、断连 / 超时、参考写盘异常和 IDR 恢复。指标脚本的基础测试覆盖错位、缺帧、未来帧填旧槽、raw 截断及 CRC 损坏。

当前只实现测试视频源。后续 file / camera / RTSP 源可扩展 `gst_media_source_open` 的源构造部分，复用编码、控制、QUIC 传输和播放评估；VFR、B 帧、音频及运行时尺寸变化需要相应扩展时间轴 / 协议。
