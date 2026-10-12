# GStreamer 真实视频传输与画质评估设计与实现说明

日期：2026-10-12。用户已确认开始开发，第一阶段源码已加入；未编译、未运行 APP / Mininet / GStreamer。原传输分析以设计时工作区为准。最终实现采用独立 GStreamer 入口；具体文件、使用与远端验收见 `APP/GStreamer使用与远端验收.md`，本文件中的管线和接口示意不是逐行代码清单。

## 1. 已确认的需求与结论

用户明确：trace 保留指**网络条件保留**；应用负载使用 GStreamer 的真实编码输出，不要求匹配原视频 CSV 的帧大小或发送间隔。

第一阶段以 GStreamer 测试画面作为视频源，h1 主动推给 h2，h2 实时解码、播放并评估。后续可替换真实视频源。编码输出不能做固定大小填充，应提供外部信号调整编码目标码率的接口。

结论：**可以实现**。保留 picoquic 为唯一跨主机媒体传输层，GStreamer 负责视频生成、编码、解码与图像输出。可行性依据是应用层已经具有“一帧一流”的接入边界；不需要用其他 QUIC 实现或 RTP/UDP 替换 picoquic。

第一阶段保留 picoquic 子模块版本 `94f48cb746de101fbb88aee5decfe14b28b04496`，版本宏 `1.1.44.2`。多路径、选路、可靠重传和 I/BP 暂存抢占实现继续使用现有库。

## 2. 当前 APP 的实际传输模式

### 2.1 媒体与传输路径

```text
视频负载 CSV：size, timeToNextFrameSeconds
   ↓
video_sender：按间隔产生模拟帧；frame_idx % GOP_SIZE 判断 I/BP
   ↓
DAMS 应用队列：准入 / 过期过滤
   ↓
每帧拆成若干应用包：20 字节头 + 最多 1200 字节载荷
载荷由 memset(..., 0xAA, ...) 生成
   ↓
一帧 = 一条客户端单向 QUIC Stream，最后附 FIN
   ↓
picoquic I/BP 暂存 → Stream 调度 → 多路径选路 → 拥塞控制 / 重传
   ↓
发送端在 UDP 数据报前加 16 字节实验头：时间戳、包序号、保留字段
   ↓
Mininet ground / sky 双路径，HTB + netem 动态回放
   ↓
video_receiver 去掉实验头 → picoquic → 应用包重组、完整性校验、日志
```

这是真实 QUIC 网络传输，但当前媒体内容是模拟字节，不是可解码视频。接收端解析应用包后直接消费载荷，FIN 完成时释放重组缓冲；没有编码帧保存、解码和播放。

源码入口：

| 位置 | 已核实的行为 |
|---|---|
| `APP/video_common.h:42` | 加载模拟负载 CSV；没有图像、codec、PTS、DTS 信息 |
| `APP/video_sender.c:415` / `:452` | 构造一帧的流缓冲；载荷填 `0xAA` |
| `APP/video_sender.c:779` / `:803` | 申请客户端单向流，调用 `picoquic_add_to_stream_with_frame_type`，一帧一流 |
| `APP/video_sender.c:153` | 探测本机接口；第二路径对应 `10.0.2.1 → 10.0.2.2` |
| `APP/video_receiver.c:147` / `:244` | 流回调重组应用包，FIN + 包数 + 剩余字节检查完整性 |
| `picoquic/picoquic/sender.c:389` | I 暂存队列非空时放行 I，丢弃当时仍在 BP 暂存队列里的数据 |
| `picoquic/picoquic/paths.c:551` | 在现有路径管理之后选择 default / rr / minrtt |
| `APP/demo_dubao.py:232` | 网络回放以 VIDEO_START 的 Linux 单调时钟为共同起点 |

注意：1200 字节是应用包载荷上限，不等于 QUIC 数据报大小；同一个 QUIC 数据报可能包含多个流的 STREAM 帧。发送端 `[PKT_SEND]` 中的 frame/stream 是近似归属，path 字段是接口索引；准确流归属和路径统计应使用 qlog 的 STREAM 记录、路径 ID 与地址映射。

### 2.2 网络 trace 与负载 trace 必须分开

网络条件由以下数据与配置构成：

- `APP/Travel.csv`：0～10 秒的 time/x/y/z 轨迹，原查询使用 ceiling 采样规则。
- `APP/p2p_data/summary_Throughput_ground.tsv`、`summary_Throughput_sky.tsv`：两条路径的吞吐网格。
- 对应 `summary_Latency_*.tsv`：时延网格。
- 双路径拓扑：h1-s1-s2-h2 与 h1-s3-s4-h2；两组地址及源地址策略路由。
- HTB / netem 固定句柄、队列 limit=1000、运行中 change、10 ms 更新时间网格及原更新阈值。
- 随机种子 42，抖动概率 0.2，附加时延选项 5/10/15 ms。

原视频 CSV 是**模拟应用负载**。真实媒体模式中不再用它的 size 生成负载，也不再用它的间隔调度编码帧。

有一个需要保留的耦合：原链路抖动在视频 CSV 的帧时间点切换。为了不因换成 30 fps 的新视频源而改变网络条件，真实媒体模式仍可读取原 CSV 的**第二列**，仅作为网络抖动的时间日历，名称改为 `NETWORK_JITTER_TIMING_TRACE`，默认沿用当前 `4k_30f_trace_0.8.csv`。第一列不参与真实负载。以后只有明确改变网络实验配置时才更换该日历。

网络回放绝不能用编码线程的实际出帧次数、码率变化或解码反馈驱动 RNG。每轮开始前预计算同样的抖动事件表，并归档其哈希、计划时间与实际 TC 应用时间。不同媒体负载下 TC 执行迟到仍可能不同，保留现有 timing_valid 诊断，不宣称实际网络调度完全相同。

### 2.3 现有算法的实际边界

1. I 抢占只丢弃尚在 picoquic BP 暂存队列的数据，不取消已放行流、不禁止其后续重传，也没有自动向接收端发送“该帧被丢弃”通知。不能把 PREEMPT=1 理解为任何时刻都能清空所有 P/B 帧。
2. `remaining_size=0` 代表整帧已经交给 picoquic，不代表网络已发完或已收到。应用期限目前不能中止这些已提交的数据。
3. 当前 DAMS 容量估计是固定的 `1.25 × interval_us`；不是从两条路径测量出的容量。当前优先级=size，在 remaining=size 时取消评分约为 0.5，一般不会触发 `<0.1` 的分支。此次接媒体不同时改造 DAMS 算法。
4. 当前应用准入期限为到达时间 +100 ms，预处理再减 5 ms。保留该准入机制；新增接收播放期限，两者单独命名和记录。
5. 当前工作区 `APP/video_sender.c:590–591` 的 BBR1 设置已被注释；固定版本库默认拥塞算法是 NewReno。不能把当前实验称为 BBR1 实验。此次保留用户已有改动，启动日志和 manifest 必须记录实际算法。
6. 单流内部可靠有序；不同流完成顺序不保证解码顺序。跨帧仍有 codec 依赖关系，不能把完成的流任意拼给解码器。

## 3. 建议的第一阶段配置

| 参数 | 建议初值 | 用途 |
|---|---|---|
| 输入 | `videotestsrc is-live=true pattern=ball` | 运动画面，先验证真实编码、网络传输和解码 |
| 视频 | 1920×1080，30/1 fps，I420，固定色彩参数 | 可重复参考和软件编码起点 |
| codec | H.264 / x264enc | 有成熟应用接口，方便后续码率控制 |
| 目标码率 | 初始 4000 kbit/s，允许运行时调整 | 是平均码率目标；每帧大小自然变化 |
| 编码延迟 | `tune=zerolatency`，`speed-preset=veryfast` | 尽量减少编码缓冲；远端 CPU 不足时调整 preset |
| GOP | 30 帧，关闭场景切换提前插入关键帧，关闭 open GOP | 约每秒一个独立恢复点 |
| B 帧 | 0 | 第一阶段避免解码 / 显示重排的额外复杂度 |
| 关键帧 | IDR，包含 SPS/PPS | 发生依赖缺失后可恢复解码 |
| 播放缓冲 | 200 ms，可配置 | 初始值，后续按测得 RTT / 抖动与 CPU 时间调整 |
| 视频时长 | 10 秒 / 300 帧 | 与当前 Travel 时长对应；网络耗尽后保留原末值延续行为 |
| 抢占 / 选路 | 原默认 1 / default，保留所有消融值 | 保留研究特性 |

`ball` 画面较容易压缩，真实输出码率可能低于 4 Mbps。后续另用滚动纹理测试图和真实运动视频验证码率调整及网络压力。测试图可验证管线和对齐，但不能据其 PSNR/VMAF 推断真实视频的主观质量收益。

这里的“可变码率”包含两层：即使目标暂时不变，编码帧大小与短时码率也不固定；外部指令可以改变平均码率目标。第一阶段不加填充、不强制每帧恒定大小、不把新码率写回旧负载 CSV。

## 4. GStreamer 与 picoquic 的接入架构

```text
h1：videotestsrc → raw tee → x264enc → h264parse → appsink
                    └→ 原始参考帧 / 时间索引
                                 ↓ 有界 AU 队列
                            picoquic 主事件循环
                            一 AU 一 QUIC Stream
                                 ↓
                       原 ground / sky 双路径
                                 ↓
h2：picoquic → 流重组 → 帧排序 / 依赖状态 → 有界解码输入队列
                                                   ↓
                                       appsrc → h264parse → avdec_h264
                                                   ↓
                                  I420 appsink → 定时播放控制器
                                                   ├→ 可选预览
                                                   └→ 实际播放帧 / 画质评估
```

AU 是 access unit：一组构成一幅编码图像的 NAL 单元。`h264parse` 后强制 `alignment=au, stream-format=byte-stream`。NAL 单元、GStreamer buffer 和应用包不能未经协商就当作完整视频帧。

发送编码分支的设计管线：

```text
videotestsrc is-live=true pattern=ball num-buffers=300
 ! video/x-raw,format=I420,width=1920,height=1080,framerate=30/1
 ! tee name=raw
 raw. ! queue [有界、溢出可观测]
      ! x264enc name=encoder bitrate=4000 pass=cbr
          tune=zerolatency speed-preset=veryfast
          key-int-max=30 bframes=0 byte-stream=true
          option-string="scenecut=0:open-gop=0"
      ! h264parse config-interval=-1
      ! video/x-h264,stream-format=byte-stream,alignment=au
      ! appsink name=encoded sync=false [有界]
 raw. ! queue [有界、溢出使评估失败]
      ! appsink name=reference sync=false [有界]
```

这是元素级设计示意，含说明占位符，不是可直接执行的 gst-launch 命令。`pass=cbr` 是插件枚举名称，x264 插件源码说明该方式实际采用 ABR；因此不意味着帧大小恒定。`h264parse config-interval=-1` 用于每个 IDR 携带 SPS/PPS。远端检查参数支持情况后生成最终管线。

接收解码分支：

```text
appsrc name=encoded_input is-live=true format=time do-timestamp=false block=true
 caps=video/x-h264,stream-format=byte-stream,alignment=au
 ! h264parse ! avdec_h264 ! videoconvert
 ! video/x-raw,format=I420
 ! appsink name=decoded sync=false [有界]
```

`block=true` 只允许在独立媒体 feeder 线程使用；QUIC 回调从不调用可能阻塞的 push-buffer，也不等待解码或磁盘。decode appsink 立即提取帧，播放控制器在指定时间选择像素帧，避免显示 sink 的默认丢帧行为暗中改变评估结果。

### 4.1 线程和内存所有权

- QUIC 网络线程：唯一调用该 context 的 picoquic API 的线程，处理 socket / 重组 / 传输生命周期。
- GStreamer streaming / media control 线程：拉取或接收 sample、操作媒体状态和编码参数；不得触碰 picoquic context。
- 接收 feeder / raw collector / 播放控制器：通过有界队列交接完整 AU 和解码帧。
- 原始参考 / 评估写盘线程：保存帧、索引和日志；写盘压力可观测。

`GstSample` / `GstBuffer` 跨线程必须 retain/ref；解除 map 后不得继续使用其裸指针。将数据交给 picoquic 之前使用明确所有权的连续缓冲；当前 API 复制输入，成功提交后应用释放。接收端完整 AU 转移给媒体队列后才释放 stream_ctx。

所有队列同时限制帧数、字节数、驻留时间；另限制单帧 AU 大小、未完成流数和解码缓存。默认上限作为配置记录，不能仅依靠 appsrc 的 max-bytes 自动限制，因为 push 调用方式和应用回调是否遵守背压会影响效果。媒体 feeder 遵守 need-data/enough-data；超载标记 LOCAL_OVERLOAD，参考帧缺失则禁止输出有效画质报告。

## 5. 应用协议与代码接口设计

保留原每帧分应用包、一帧一单向流的结构；真实媒体模式使用独立 ALPN `video-gst-v1`，避免旧接收端将媒体协议当模拟负载。legacy 模式仍使用 `video-test`。

每个媒体流的应用包载荷拼接后为：

```text
[固定 96 字节媒体头][原始 H.264 AU 字节]
```

外层仍为 20 字节应用包头、每包最多 1200 字节载荷；媒体模式所有整数显式网络字节序，legacy 模式继续原格式。媒体头由序列化函数逐字段写入，不能直接 memcpy C struct，避免 padding 和端序差异。

媒体头字段草案：

| 字段 | 字节 |
|---|---:|
| magic / version / header_len | 4 / 2 / 2 |
| flags（IDR 等）/ codec_id | 4 / 4 |
| frame_id / decode_order_id | 8 / 8 |
| pts_ns / dts_ns / duration_ns | 8 / 8 / 8 |
| source_mono_us / encoded_ready_mono_us | 8 / 8 |
| au_length / au_crc32 / bitrate_epoch / reserved | 4 / 4 / 4 / 12 |
| 合计 | 96 |

元数据不计入“编码码率”，但计入应用和网络总开销。对 AU 大小 A：`N=ceil((96+A)/1200)`，流长度为 `96+A+20*N`；UDP 实验头与 QUIC / IP 开销另计。接收 FIN 必须验证应用包数、流长度、AU 长度、CRC 和元数据一致性，失败帧不喂解码器。

增加一条小型双向控制流，采用 length-prefixed 消息：SETTINGS / MEDIA_READY / START / END / PLAYOUT_DONE / ERROR。控制流不参与 I/BP 暂存，单独记录其字节和 stream_id；qlog 分析只把明确登记的媒体流计为视频帧。

SETTINGS 包含 codec、尺寸、fps、色彩参数、播放缓冲、session_id。START 包含共同 T0。END 包含最终 source_frame_count、最后 PTS、时长和已知本地丢弃清单或摘要。接收端据预期时间槽和最终计数识别从未收到任何字节的帧，不依赖“曾收到该流”才能判断缺帧。

接口草案（只定义职责，尚未实现）：

```c
typedef struct {
    uint64_t frame_id, decode_order_id;
    uint64_t pts_ns, dts_ns, duration_ns;
    uint64_t source_mono_us, encoded_ready_mono_us;
    uint32_t bitrate_epoch;
    int is_idr;                /* 解析 NAL 验证，不用帧号取模替代 */
    uint8_t *au;
    size_t au_len;             /* 只包含编码输出 */
} media_frame_t;

int media_source_start(media_source_t *, const media_config_t *, uint64_t t0_us);
int media_source_try_pop(media_source_t *, media_frame_t *); /* 非阻塞 */
int media_source_set_bitrate(media_source_t *, uint32_t kbps, uint64_t command_id);
int media_frame_pack(const media_frame_t *, uint8_t **bytes, size_t *len);
int media_receiver_submit(media_receiver_t *, media_frame_t *); /* 非阻塞交接 */
int media_receiver_tick(media_receiver_t *, uint64_t now_us);
void media_frame_release(media_frame_t *);
```

发送事件循环概要：

```text
准备媒体源、控制接口和日志 → QUIC 建连 / 原多路径探测
SETTINGS / MEDIA_READY → START(T0=now+max(200ms,2×各路径RTT+50ms)) → 输出原 VIDEO_START 标记
到 T0 启动 live 源；采集帧带 source_frame_id / PTS，参考支路记录同帧
while 未关闭:
    处理 QUIC socket 与所有到期事件
    非阻塞取 AU；真实大小进入原应用准入队列
    按实际 IDR 映射 I/BP，整 AU 分包到一条单向流并提交 FIN
    用原 picoquic_prepare_next_packet / socket 函数发包
    保留发送方向 16 字节 UDP 实验头
    select 等待同时考虑 QUIC wake、媒体 eventfd、准入 tick、结束期限
源 EOS → 发送 END → 按原机制排空 → 等待 PLAYOUT_DONE 或明确超时 → 关闭
```

媒体队列使用 eventfd 或 pipe 唤醒事件循环，不能等原最多秒级的 QUIC select 自然返回。原 sender 的“frame_count 到末尾”结束条件替换为 source EOS；legacy 分支保留原 CSV 流程。

第一阶段保持准入算法，真实 AU 大小用于 size / remaining_size；记录从采集到编码输出、提交的各个时间点。GOP_SIZE_CFG 在 legacy 模式仍按取模标记；媒体模式设置实际 encoder GOP，并以 NAL 验证的 IDR 标记。若用 0 做“全部按 BP”消融，只覆盖传输标记，不关闭视频流本身的 IDR。编码 GOP 与传输标记配置分开归档。

## 6. 外部码率控制接口

首版采用本机 Unix domain socket 的 JSON Lines 控制入口；文件路径每轮独立，由启动器归档。外部程序发控制命令，GStreamer media control context 执行参数变更，不从信号处理函数直接调用 GStreamer。以后可接研究算法、前端或接收反馈。

请求示例：

```json
{"op":"set_bitrate","command_id":7,"target_kbps":2500}
```

反馈字段：command_id、accepted/rejected、requested_kbps、element_property_kbps、apply_mono_us、bitrate_epoch、first_output_after_apply_frame_id（可获得时）。记录“已设置编码器属性”和“测得实际输出码率”是两个不同事件，不承诺在下一帧精确达到新码率。

设计操作：

```c
/* 由媒体控制线程执行，不在 QUIC 回调或 POSIX signal handler 内执行 */
g_object_set(encoder, "bitrate", target_kbps, NULL);
```

官方当前 x264enc 源码将 bitrate 标为 GST_PARAM_MUTABLE_PLAYING，setter 会调用编码器重配置。远端具体版本未知：开发时预检查该属性标志、插件版本与依赖，远端验收确认 PLAYING 状态下有效；不支持时明确失败，不偷偷重建媒体 pipeline 或 QUIC 连接。

码率变更不改尺寸、fps、GOP 或网络 trace。第一阶段不自动把 picoquic 带宽估计转成编码码率；仅提供参数控制入口。后续自动适配需要定义观察窗口、上下限、变化速率及多路径容量估计，另行确认。

每 1 秒同时统计 encoder AU bytes 的实际码率、应用总码率、线上码率。控制参数用 kbit/s，避免把 bytes/s、bit/s 与 picoquic 带宽字段单位混淆。无内容复杂度支撑时实际码率可能低于目标，不能把该现象误判为控制未生效。

## 7. 时间、排序、依赖和播放策略

### 7.1 共同时间轴

Mininet 两端运行于同一 Linux 内核，继续使用 CLOCK_MONOTONIC 和 VIDEO_START。GStreamer GstClock / segment running-time 和 picoquic 的 us 时钟做显式 ns/us 换算与起点映射。

x264 输出可能有 PTS 偏移，不可直接将 GstBuffer PTS 当单调时钟。将 encoded PTS/DTS 经 segment 转成 running-time，并与 raw 源 PTS / frame_id 建立映射。记录 pipeline start、首 raw / AU 时间，检查首帧起点与漂移；实际编码迟到计入媒体日志，不移动网络 T0 来掩盖迟到。

预览 / 播放时间槽：`play_time(frame) = T0 + normalized_source_pts + playout_buffer`。采集到播放时延用实际 source_mono_us；编码就绪到完整接收时延另列。跨机部署时当前单机时钟前提不成立，届时增加独立的同步方案。

### 7.2 解码和显示

接收端先按 decode_order_id 排序，而不是按 FIN 到达顺序。首版无 B 帧，通常显示顺序与解码顺序一致，但协议仍保留 DTS。

完整、连续 AU 尽早进入解码器，不等到显示时刻才开始解码。缺失帧等待到该槽的解码准入截止时间：`play_time - decode_margin`；初始 margin 建议 20 ms，可配置并按 CPU 测量修订。

首版采用明确、保守的依赖策略：若必需帧丢失、AU 无效、过期被跳过或本地队列溢出，则进入 WAIT_IDR；后续非 IDR 不作为正常连续参考链提交；遇到完整 IDR（含 SPS/PPS）重建 / 清理解码状态并恢复。较晚的完整 IDR 可以使排序跳过已确定缺失的旧 GOP，避免永久等不到流。

这会较解码器自带错误隐藏更保守，须在结果中注明策略。以后若要量化原生 decoder concealment，作为另一个固定配置，不与首版混合。

每个源显示槽在截止时刻必须输出一个画面：已经按时解码好的对应帧，或上一张已显示画面；尚无画面时输出黑帧。未来帧不得提前填旧槽。迟到帧不追赶显示，不回填已封闭槽。记录 displayed_source_frame_id，保留冻结对质量的影响。

默认仅在接收应用层拒绝迟到内容，继续保留原 QUIC 可靠传输；不自动 reset/STOP_SENDING 已提交流。需要显式中止过期传输时会改变网络行为，留作后续单独实验开关。

终态分字段记录：transport={完整/部分/未收到/无提交}，decode={正常/依赖缺失/解码失败/未解码}，playout={按时新帧/重复/黑帧/迟到}，local_reason={编码/队列/准入/资源}。不得仅凭未收到就断言是“网络丢包”或“库抢占丢弃”；原库不报告 BP 暂存丢弃原因，首版明确保留 unknown_origin。

### 7.3 结束与排空

源 EOS 不代表播放结束，也不代表所有独立流已完成。END 可先于媒体流到达，接收端仍继续到最终播放槽。sender 排空后等 PLAYOUT_DONE，启动器同时等两端媒体输出完成 / 错误 / 超时，再发清理信号并归档。不能沿用 sender 退出后立即 SIGINT receiver 的流程，否则可能丢失末尾播放、raw 文件和评估索引。

## 8. 画质评估设计

保存三种同尺寸、同像素格式、同色彩处理的参考 / 输出：

1. **R_src**：编码前的原始测试图帧。必须从实际 raw 支路抓取，以 frame_id / PTS 索引，不能另跑一遍测试源就当作同一参考。
2. **R_enc**：发送端完整编码 AU 的本地离线无损传输解码结果。用于观察编码自身损失，尤其是码率动态变化期间。
3. **R_play**：按接收端真实播放时间槽生成的像素序列，包含冻结重复与黑帧。

比较 R_src vs R_enc 反映编码损失；R_enc vs R_play 观察传输 / 播放影响；R_src vs R_play 报告最终质量。三组分开报告，不能将非线性 VMAF 分数直接相减解释成损失分解。

首版固定 CFR，以 frame_id / normalized PTS 验证一对一映射，再把三个序列写为等帧数、同 fps 的无损或 raw I420 工件。raw 帧按 GstVideoFrame 各 plane stride 逐行写出，避免把带 padding 的 GstBuffer 直接当连续 I420。VFR 视频源后续接入时要另定义共同显示时间网格。

离线使用 FFmpeg PSNR、SSIM、libvmaf；记录模型、版本、像素范围和全部对齐参数。不能直接对“删掉丢帧后的接收文件”与原视频算分。测试图上的指标用于验证，正式画质结论应使用真实内容。

主报告包含：

- PSNR / SSIM / VMAF 全时间轴均值及逐帧结果；动态码率前后分段结果。
- 按时显示新帧比例、完整 AU 比例、迟到比例、WAIT_IDR 时长、黑帧时长。
- 冻结累计时长、最长冻结、冻结事件次数；以源 frame_id 未更新判定，避免静态图像被误认成卡顿。
- source→encoded、encoded→received、source→displayed 延迟分布。
- 每路径承载、重传、有效媒体字节、协议开销；qlog 原传输延迟继续单独保留。
- 本地 CPU / 编码耗时 / 队列溢出 / 写盘迟到，使本地资源瓶颈与网络效果可区分。

1080p I420 每帧约 3.11 MB，30 fps 约 93 MB/s，10 秒约 0.93 GB / 序列；三条 raw 序列可能约 2.8 GB。默认先短时 raw 验证，也可采用固定无损压缩归档。远端检查磁盘余量和写盘吞吐；参考支路掉帧使画质评估 invalid，而非静默丢参考后继续报分。重型 VMAF 和参考解码在网络实验结束后运行，避免争抢实验 CPU。

## 9. 文件与改动范围

最终采用 `APP/video_sender_gst.c` / `video_receiver_gst.c` 与共享 `media_transport.c` 实现新的媒体入口，不修改原 sender / receiver 源码；通过 `make media` 和 `VIDEO_MODE=gst` 选择。下表保留原职责划分，源码最终名称与运行细节以使用说明为准。

| 文件 | 计划改动 |
|---|---|
| `APP/video_sender.c` | 新增媒体模式；真实 AU 替代填充；eventfd 唤醒；EOS / END 生命周期；保留 legacy、原 socket 和探路 |
| `APP/video_receiver.c` | 保留 FIN 处理与完整性检查；保存 AU；控制流、排序、播放完成通知；stream ctx 清理 |
| `APP/video_common.h` | 共享模式 / 配置声明；原 CSV 结构和格式用于 legacy |
| `APP/media_protocol.h` / `.c` | 显式序列化、长度 / 字节序 / CRC / 控制消息校验 |
| `APP/gst_media.h` / `.c` | 源工厂、编码 / 解码、raw 索引、线程队列、参数更新 |
| `APP/media_playout.h` / `.c` | 排序、WAIT_IDR、固定时间槽、冻结输出、完成通知 |
| `APP/media_control.h` / `.c` | Unix socket 控制、请求校验与状态反馈 |
| `APP/media_quality.py` | 对齐、FFmpeg 评估、逐帧与汇总报告 |
| `APP/demo_dubao.py` | 媒体配置、网络日历独立、两端完成协调、额外工件归档 |
| `APP/qlog_delay_scatter.py` | 明确筛媒体流 / 排除控制流；保留原日志字段匹配与延迟口径 |
| `APP/Makefile` | 用 pkg-config 增加 gstreamer-1.0 / gstreamer-app-1.0 / gstreamer-video-1.0；legacy 构建可不依赖 GStreamer |
| `项目.md` | 开发后按既有格式补回滚点、修改说明和验证范围 |

第一阶段不修改 picoquic 子模块。新增源工厂接口为 test / file / camera / rtsp 预留；当前只实现 test，替换源不改 QUIC 协议与多路径层。需要原生拉流协商、音频、B 帧、实时分辨率变化时另设计扩展。

## 10. 开发顺序与验收（源码已加入，等待远端验收）

开发顺序：

1. 建立媒体协议与 test 源；真实 AU 一帧一流传输；h2 解码；控制流完成握手和 EOS。
2. 接收排序、IDR 恢复、时间槽输出、原始参考与逐帧索引。
3. 外部码率控制；报告请求时间、属性更新时间和实际 AU 码率变化。
4. 连接原 Mininet 回放和 qlog；完善生命周期、归档与离线画质评估。

远端验收内容：

- 理想网络下 AU CRC 全部一致，frame_id / PTS 一一对应，R_enc 与按时 R_play 像素一致；编码自身损失仍可存在。
- 同一网络输入、抖动日历、种子生成相同计划网络事件表；实际 TC 迟到有可追溯记录。
- 两条路径均校验成功，qlog 可识别路径；rr 的有效媒体承载可验证；minrtt 可集中一条路径，不能要求所有策略都均分。
- 在固定内容和网络下验证 preempt=0/1、default/rr/minrtt 六组，编码和播放配置固定；PREEMPT 不承诺实际发生暂存丢弃，需结合日志解释。
- 强制乱序完成、缺 P / 缺 IDR、FIN 带最后数据、末尾流迟到、编码 EOS、断连、写盘压力等场景，没有永久排序阻塞、引用泄漏或少尾帧。
- 运行中将目标码率按控制序列 4000→2000→6000 kbit/s 调整，QUIC 连接及已建路径保持；插件属性变化和实际输出窗口码率分别验证。内容容易压缩时增加纹理源复验。
- 画质脚本拒绝帧索引错位、参考缺帧、错误色彩 / 尺寸、缺少 VMAF 依赖；报告包含冻结槽，没有压缩丢帧后的时间轴。
- legacy 模式仍可按原 CSV 运行并使用原 qlog 分析口径。

遵守 `项目.md`：本地开发后仅做源码静态审查、Python AST / 差异检查，不本地编译运行实验，等待远端反馈。重大代码修改提交并创建说明标签，更新回滚记录。当前已有未提交 sender 改动和其他用户文件状态必须保留，不能混入或撤销。

## 11. 官方依据

- [GStreamer appsink](https://gstreamer.freedesktop.org/documentation/app/appsink.html)：应用取得 pipeline buffer / sample。
- [GStreamer appsrc](https://gstreamer.freedesktop.org/documentation/app/appsrc.html)：应用向 pipeline 送数据，push 的排队 / 阻塞语义。
- [GStreamer h264parse](https://gstreamer.freedesktop.org/documentation/videoparsersbad/h264parse.html)：AU 输出格式和 IDR SPS/PPS 插入。
- [GStreamer x264enc](https://gstreamer.freedesktop.org/documentation/x264/index.html)：编码参数与延迟。
- [GStreamer 官方 x264enc 源码](https://raw.githubusercontent.com/GStreamer/gstreamer/main/subprojects/gst-plugins-ugly/ext/x264/gstx264enc.c)：ABR 说明、bitrate 的 PLAYING 可变标志和重配置实现。此处是当前上游证据，远端版本仍需检查。
- [GStreamer videotestsrc](https://gstreamer.freedesktop.org/documentation/videotestsrc/index.html)：live 测试源和运动图案。
- [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html#libvmaf)：参考 / 失真输入、VMAF 依赖和帧同步；同文档的 PSNR / SSIM 条目提供指标实现。
