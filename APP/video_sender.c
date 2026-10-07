#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <picoquic.h>
#include <picoquic_utils.h>
#include <picosocks.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <autoqlog.h>

#include "video_common.h"

#define VIDEO_ALPN "video-test"

/* 经验参数：端口未就绪时的 probe 重试周期 */
#define PROBE_RETRY_INTERVAL_US 200000

/*
 * 应用层包格式（放在 QUIC Stream payload 里）
 * 每个视频帧被切分为若干 app pkt，所有 app pkt 按顺序放入同一个 Stream。
 * 头部 20 字节（布局见 video_common.h）：
 *   [ frame_idx      (uint32) ]
 *   [ frame_class    (uint32) ]   // 0=BP帧, 1=I帧（I帧优先机制测试新增）
 *   [ pkt_idx        (uint32) ]
 *   [ total_pkts     (uint32) ]
 *   [ payload_size   (uint32) ]
 */
/* APP_PKT_HEADER_SIZE / APP_PKT_PAYLOAD_MAX 已统一到 video_common.h */

/*
 * UDP 自定义首部格式（放在每个 UDP 报文前）
 * 所有 UDP 报文统一加首部，包括握手、探测、ACK、关闭阶段。
 * 头部 16 字节：
 *   [ send_ts_us     (uint64) ]
 *   [ global_pkt_seq (uint32) ]
 *   [ reserved       (uint32) ]
 */
#define UDP_HDR_SIZE 16

/*
 * 排空阶段（drain）参数：
 * 所有帧写入 QUIC 发送队列后，并不代表数据已送达对端。若立即 picoquic_close()，
 * 连接进入 disconnecting 状态后只发送 ACK + CONNECTION_CLOSE，发送队列中尚未发出的
 * stream 数据会被直接丢弃（这就是 4K 负载下接收端只收到 150/349 帧的根因）。
 * 因此进入排空阶段：继续收发，直到 QUIC 层不再有待发送/待重传/待确认的数据，再关闭。
 *
 * 判定依据：picoquic_get_next_wake_delay() 返回下一次必须唤醒的时间间隔——
 *   还有待发送数据时返回 0/极小值；还有已发送未确认数据时返回重传定时点（短）；
 *   数据全部确认后返回空闲/idle 长间隔。连续多次观察到长间隔，即判定排空完成。
 */
#define DRAIN_IDLE_THRESHOLD_US 500000   /* 单次"空闲间隔"阈值：0.5s 内无任何发送/重传任务 */
#define DRAIN_STREAK_REQUIRED 3          /* 连续空闲次数（共需约 1.5s 稳定空闲） */
#define DRAIN_TIMEOUT_US 60000000        /* 排空超时：60s，防止对端失联时无限等待 */
#define DRAIN_SELECT_POLL_US 250000      /* select 轮询间隔上限：空闲时每 250ms 检查一次，
                                            避免按完整 idle-timeout(最长10s) 阻塞而拖慢判定 */

typedef struct {
    video_frame_t* frames;
    int frame_count;
    int current_frame_idx;
    uint64_t next_send_time;
    int is_disconnected;
    int is_connected;

    int is_multipath_established;

    /* probe 控制 */
    int smart_probe_attempted;
    uint64_t last_probe_time;

    /* 路径状态与参数打印相关 */
    uint64_t active_paths[16];
    int active_path_count;

    /* 本地地址列表（用于 probe） */
    struct sockaddr_storage local_addrs[8];
    int local_addr_count;
    int local_if_index[8];

    /* 服务器地址（主路径） */
    struct sockaddr_storage server_addr;

    /* 可靠获取本地 UDP 端口用（IPv4 socket） */
    SOCKET_TYPE probe_socket_v4;

    /* 当前正在发送的帧对应的 stream */
    uint64_t current_stream_id;

    /* 全局唯一包序号 */
    uint32_t global_pkt_seq;

    /* I帧优先机制测试：轨迹中最大帧尺寸（用于判定 I 帧：size == max 即 GOP 关键帧） */
    size_t max_frame_size;
} sender_ctx_t;

static int collect_local_ipv4_addrs(sender_ctx_t* ctx, uint16_t n_port)
{
    ctx->local_addr_count = 0;

    struct ifaddrs *ifaddr = NULL, *ifa = NULL;
    if (getifaddrs(&ifaddr) != 0) {
        perror("getifaddrs");
        return -1;
    }

    for (ifa = ifaddr; ifa != NULL; ifa = ifa->ifa_next) {
        if (ifa->ifa_addr == NULL) continue;
        if (ifa->ifa_addr->sa_family != AF_INET) continue;
        if ((ifa->ifa_flags & IFF_LOOPBACK) != 0) continue;
        if (ctx->local_addr_count >= (int)(sizeof(ctx->local_addrs) / sizeof(ctx->local_addrs[0]))) break;

        struct sockaddr_in sa = *(struct sockaddr_in*)ifa->ifa_addr;
        sa.sin_port = n_port;

        memcpy(&ctx->local_addrs[ctx->local_addr_count], &sa, sizeof(sa));
        ctx->local_if_index[ctx->local_addr_count] = (int)if_nametoindex(ifa->ifa_name);
        ctx->local_addr_count++;
    }

    freeifaddrs(ifaddr);
    return 0;
}

static uint16_t get_local_udp_port_v4(sender_ctx_t* ctx, picoquic_cnx_t* cnx)
{
    struct sockaddr* cnx_local = NULL;
    picoquic_get_local_addr(cnx, &cnx_local);
    if (cnx_local != NULL && cnx_local->sa_family == AF_INET) {
        uint16_t n_port = ((struct sockaddr_in*)cnx_local)->sin_port;
        if (n_port != 0) return n_port;
    }

    if (ctx->probe_socket_v4 != INVALID_SOCKET) {
        struct sockaddr_in sin = { 0 };
        socklen_t slen = sizeof(sin);
        if (getsockname(ctx->probe_socket_v4, (struct sockaddr*)&sin, &slen) == 0 &&
            sin.sin_family == AF_INET && sin.sin_port != 0) {
            return sin.sin_port;
        }
    }

    return 0;
}

static void sender_try_probe(sender_ctx_t* ctx, picoquic_cnx_t* cnx, uint64_t now)
{
    if (ctx->smart_probe_attempted) return;

    if (ctx->last_probe_time != 0 && now - ctx->last_probe_time < PROBE_RETRY_INTERVAL_US) {
        return;
    }
    ctx->last_probe_time = now;

    uint16_t n_port = get_local_udp_port_v4(ctx, cnx);
    if (n_port == 0) {
        printf("[Multipath] Local port still 0, will retry.\n");
        return;
    }

    if (collect_local_ipv4_addrs(ctx, n_port) != 0 || ctx->local_addr_count == 0) {
        printf("[Multipath] No non-loopback IPv4 addresses found.\n");
        return;
    }

    printf("[Multipath] Probing all local interfaces (port=%u)...\n", ntohs(n_port));

    for (int i = 0; i < ctx->local_addr_count; i++) {
        struct sockaddr_in* local = (struct sockaddr_in*)&ctx->local_addrs[i];
        struct sockaddr_storage peer = ctx->server_addr;

        if (peer.ss_family == AF_INET) {
            if (local->sin_addr.s_addr == inet_addr("10.0.2.1")) {
                ((struct sockaddr_in*)&peer)->sin_addr.s_addr = inet_addr("10.0.2.2");
            }
        }

        char ltxt[256], ptxt[256];
        picoquic_addr_text((struct sockaddr*)local, ltxt, sizeof(ltxt));
        picoquic_addr_text((struct sockaddr*)&peer, ptxt, sizeof(ptxt));

        int ret = picoquic_probe_new_path_ex(
            cnx,
            (struct sockaddr*)&peer,
            (struct sockaddr*)local,
            ctx->local_if_index[i],
            now,
            0);

        printf("[Multipath] Probe %s -> %s (if=%d) ret=%d\n",
            ltxt, ptxt, ctx->local_if_index[i], ret);
    }

    ctx->smart_probe_attempted = 1;
}

static int sender_callback(picoquic_cnx_t* cnx,
    uint64_t stream_id, uint8_t* bytes, size_t length,
    picoquic_call_back_event_t fin_or_event, void* callback_ctx, void* stream_ctx)
{
    sender_ctx_t* ctx = (sender_ctx_t*)callback_ctx;

    switch (fin_or_event) {
    case picoquic_callback_close:
    case picoquic_callback_application_close:
        {
            uint64_t le = picoquic_get_local_error(cnx);
            uint64_t re = picoquic_get_remote_error(cnx);
            uint64_t ae = picoquic_get_application_error(cnx);
            printf("Connection closed. progress=%d/%d, local=0x%" PRIx64 "(%s), remote=0x%" PRIx64 "(%s), app=0x%" PRIx64 "(%s)\n",
                ctx->current_frame_idx, ctx->frame_count,
                le, picoquic_error_name(le),
                re, picoquic_error_name(re),
                ae, picoquic_error_name(ae));
        }
        ctx->is_disconnected = 1;
        break;

    case picoquic_callback_ready:
        printf("Connection established. Starting video stream.\n");
        ctx->is_connected = 1;
        ctx->next_send_time = picoquic_current_time();

        if (ctx->active_path_count == 0) {
            ctx->active_paths[ctx->active_path_count++] = 0;
        }

        {
            int already_allowed = 0;
            (void)picoquic_subscribe_new_path_allowed(cnx, &already_allowed);
            if (!already_allowed) {
                printf("[Multipath] Waiting for next_path_allowed...\n");
            }
            sender_try_probe(ctx, cnx, picoquic_current_time());
        }
        break;

    case picoquic_callback_next_path_allowed:
        sender_try_probe(ctx, cnx, picoquic_current_time());
        break;

    case picoquic_callback_path_available:
        printf("Multipath Event: Path Available. Path ID: %lu\n", stream_id);
        ctx->is_multipath_established = 1;

        if (ctx->active_path_count < (int)(sizeof(ctx->active_paths) / sizeof(ctx->active_paths[0]))) {
            int exists = 0;
            for (int i = 0; i < ctx->active_path_count; i++) {
                if (ctx->active_paths[i] == stream_id) { exists = 1; break; }
            }
            if (!exists) {
                ctx->active_paths[ctx->active_path_count++] = stream_id;
            }
        }
        break;
    case picoquic_callback_path_suspended:
        printf("Multipath Event: Path Suspended. Path ID: %lu\n", stream_id);
        break;
    case picoquic_callback_path_deleted:
        printf("Multipath Event: Path Deleted. Path ID: %lu\n", stream_id);

        for (int i = 0; i < ctx->active_path_count; i++) {
            if (ctx->active_paths[i] == stream_id) {
                ctx->active_paths[i] = ctx->active_paths[ctx->active_path_count - 1];
                ctx->active_path_count--;
                break;
            }
        }
        break;
    case picoquic_callback_path_quality_changed:
        printf("Multipath Event: Path Quality Changed. Path ID: %lu\n", stream_id);
        break;

    default:
        break;
    }

    return 0;
}

/* 视频块描述结构 */
typedef struct {
    int frame_idx;
    size_t size;
    size_t remaining_size;
    uint64_t arrival_time;
    uint64_t deadline;
    uint64_t effective_deadline;
    double priority;
    int canceled;
} video_chunk_t;

#define MAX_CHUNK_QUEUE_SIZE 1024
static video_chunk_t chunk_queue[MAX_CHUNK_QUEUE_SIZE];
static int chunk_queue_head = 0;
static int chunk_queue_tail = 0;
static int chunk_queue_count = 0;

static void enqueue_chunk(video_chunk_t chunk) {
    if (chunk_queue_count < MAX_CHUNK_QUEUE_SIZE) {
        chunk_queue[chunk_queue_tail] = chunk;
        chunk_queue_tail = (chunk_queue_tail + 1) % MAX_CHUNK_QUEUE_SIZE;
        chunk_queue_count++;
    } else {
        printf("[DAMS] Warning: Chunk queue is full!\n");
    }
}

static void preprocess_chunk_deadline(video_chunk_t* chunk) {
    double alpha = 1.0;
    uint64_t std_dev = 10000;
    uint64_t adjustment = (uint64_t)(alpha * (std_dev / 2));

    if (chunk->deadline > adjustment) {
        chunk->effective_deadline = chunk->deadline - adjustment;
    } else {
        chunk->effective_deadline = chunk->deadline;
    }
}

static int check_dams_scheduling_trigger(sender_ctx_t* ctx, uint64_t current_time, int new_frame_arrived) {
    static uint64_t last_schedule_time = 0;
    int periodic_trigger = 0;
    if (current_time - last_schedule_time >= 10000) {
        last_schedule_time = current_time;
        periodic_trigger = 1;
    }

    if (new_frame_arrived || periodic_trigger) {
        return 1;
    }
    return 0;
}

static int cmp_chunk_deadline(const void* a, const void* b) {
    video_chunk_t* ca = (video_chunk_t*)a;
    video_chunk_t* cb = (video_chunk_t*)b;
    if (ca->effective_deadline < cb->effective_deadline) return -1;
    if (ca->effective_deadline > cb->effective_deadline) return 1;
    return 0;
}

static uint64_t estimate_network_capacity(uint64_t interval_us) {
    return (uint64_t)(1.25 * interval_us);
}

static void execute_dams_scheduling(uint64_t current_time) {
    int valid_count = 0;
    video_chunk_t valid_chunks[MAX_CHUNK_QUEUE_SIZE];

    for (int i = 0; i < chunk_queue_count; i++) {
        int idx = (chunk_queue_head + i) % MAX_CHUNK_QUEUE_SIZE;
        video_chunk_t* c = &chunk_queue[idx];
        if (!c->canceled && c->remaining_size > 0 && c->effective_deadline > current_time) {
            valid_chunks[valid_count++] = *c;
        }
    }

    qsort(valid_chunks, valid_count, sizeof(video_chunk_t), cmp_chunk_deadline);

    uint64_t total_required = 0;
    for (int i = 0; i < valid_count; i++) {
        total_required += valid_chunks[i].remaining_size;
    }

    uint64_t capacity = estimate_network_capacity(50000);

    if (total_required > capacity) {
        for (int i = 0; i < valid_count; i++) {
            video_chunk_t* c = &valid_chunks[i];
            double c_val = c->priority;
            double s = (double)c->size;
            double r = (double)c->remaining_size;
            double d = c_val / (s + r + 0.001);
            if (d < 0.1) {
                c->canceled = 1;
            }
        }
        for (int i = 0; i < valid_count; i++) {
            if (valid_chunks[i].canceled) {
                for (int j = 0; j < chunk_queue_count; j++) {
                    int obj_idx = (chunk_queue_head + j) % MAX_CHUNK_QUEUE_SIZE;
                    if (chunk_queue[obj_idx].frame_idx == valid_chunks[i].frame_idx) {
                        chunk_queue[obj_idx].canceled = 1;
                    }
                }
            }
        }
    }

    chunk_queue_count = 0;
    chunk_queue_head = 0;
    chunk_queue_tail = 0;
    for (int i = 0; i < valid_count; i++) {
        if (!valid_chunks[i].canceled) {
            enqueue_chunk(valid_chunks[i]);
        }
    }
}

/*
 * 将一帧切分为若干应用包，写入 stream buffer。
 * frame_class: APP_FRAME_CLASS_I / APP_FRAME_CLASS_BP（写入 app pkt 头部，
 *              供接收端日志直接观测帧类，验证 I 帧优先机制是否生效）
 * 返回分配的 buffer（调用者负责释放），out_len 为 buffer 总长度。
 */
static uint8_t* build_frame_stream_buffer(int frame_idx, int frame_class, const video_frame_t* frame, size_t* out_len)
{
    if (frame->size == 0) {
        *out_len = 0;
        return NULL;
    }

    int npkts = (int)((frame->size + APP_PKT_PAYLOAD_MAX - 1) / APP_PKT_PAYLOAD_MAX);
    if (npkts < 1) npkts = 1;

    size_t total_size = 0;
    for (int i = 0; i < npkts; i++) {
        size_t payload_off = (size_t)i * APP_PKT_PAYLOAD_MAX;
        size_t payload_len = frame->size - payload_off;
        if (payload_len > APP_PKT_PAYLOAD_MAX) payload_len = APP_PKT_PAYLOAD_MAX;
        total_size += APP_PKT_HEADER_SIZE + payload_len;
    }

    uint8_t* buf = (uint8_t*)malloc(total_size);
    if (!buf) return NULL;

    size_t wp = 0;
    for (int i = 0; i < npkts; i++) {
        size_t payload_off = (size_t)i * APP_PKT_PAYLOAD_MAX;
        size_t payload_len = frame->size - payload_off;
        if (payload_len > APP_PKT_PAYLOAD_MAX) payload_len = APP_PKT_PAYLOAD_MAX;

        /* 应用包头部（20 字节，与 video_common.h 一致） */
        uint32_t fc = (uint32_t)frame_class;
        memcpy(buf + wp + 0, &frame_idx, sizeof(uint32_t));
        memcpy(buf + wp + 4, &fc, sizeof(uint32_t));
        memcpy(buf + wp + 8, &i, sizeof(uint32_t));
        memcpy(buf + wp + 12, &npkts, sizeof(uint32_t));
        memcpy(buf + wp + 16, &payload_len, sizeof(uint32_t));
        wp += APP_PKT_HEADER_SIZE;

        /* 填充 payload（原始数据用 0xAA 模拟） */
        memset(buf + wp, 0xAA, payload_len);
        wp += payload_len;
    }

    *out_len = total_size;
    return buf;
}

/*
 * I帧优先机制测试：解析抢占配置字符串 "enabled"
 *   - enabled: 0 = 完全基线（机制不生效，等同于未修改的 picoquic 行为）；
 *              1 = 启用 I/BP 帧抢占（默认）
 * 解析失败时使用默认值 enabled=1。
 */
static void parse_preempt_config(const char* cfg, int* enabled)
{
    *enabled = 1;
    if (cfg == NULL) return;

    int e = 0;
    if (sscanf(cfg, "%d", &e) == 1) {
        *enabled = e;
    } else {
        printf("[PREEMPT] Warning: cannot parse preempt_cfg '%s', using default (1=enabled)\n", cfg);
    }
}

int main(int argc, char** argv)
{
    setbuf(stdout, NULL);

    if (argc < 4) {
        printf("Usage: %s <server_ip> <port> <trace_file> [preempt_enabled]\n", argv[0]);
        printf("  preempt_enabled (可选): 0 = 基线(机制关) / 1 = 启用 I/BP 帧抢占（默认）\n");
        return 1;
    }

    const char* server_ip = argv[1];
    int port = atoi(argv[2]);
    const char* trace_file = argv[3];

    uint64_t current_time = picoquic_current_time();

    picoquic_quic_t* quic = picoquic_create(
        1,
        NULL, NULL, NULL,
        VIDEO_ALPN,
        NULL, NULL,
        NULL, NULL,
        NULL,
        current_time,
        NULL,
        NULL,
        NULL, 0);

    if (!quic) {
        fprintf(stderr, "Could not create quic context\n");
        return 1;
    }

    picoquic_set_null_verifier(quic);
    picoquic_set_default_multipath_option(quic, 1);
    picoquic_enable_path_callbacks_default(quic, 1);

    extern picoquic_congestion_algorithm_t* picoquic_bbr1_algorithm;
    picoquic_set_default_congestion_algorithm(quic, picoquic_bbr1_algorithm);

    picoquic_set_default_tp_value(quic, picoquic_tp_active_connection_id_limit, 8);
    picoquic_set_default_address_discovery_mode(quic, 3);

    /* 启用 qlog：生成 per-connection 的 JSON 日志，包含每个 UDP 包的帧内容、路径、时间 */
    const char* qlog_dir = "./qlog_sender";
    (void)mkdir(qlog_dir, 0755);
    picoquic_set_qlog(quic, qlog_dir);
    picoquic_set_log_level(quic, 1);

    sender_ctx_t app_ctx;
    memset(&app_ctx, 0, sizeof(app_ctx));
    app_ctx.probe_socket_v4 = INVALID_SOCKET;
    app_ctx.current_stream_id = 0;
    app_ctx.global_pkt_seq = 0;

    if (load_video_trace(trace_file, &app_ctx.frames, &app_ctx.frame_count) != 0) {
        picoquic_free(quic);
        return 1;
    }

    /* I帧优先机制测试：统计轨迹中最大帧尺寸。
     * 判定规则：GOP 首帧（关键帧）是轨迹中尺寸最大的帧（本实验 3 条轨迹均如此），
     * 因此 frame->size == max_frame_size 即视为 I 帧，其余为 BP 帧。 */
    app_ctx.max_frame_size = 0;
    for (int i = 0; i < app_ctx.frame_count; i++) {
        if (app_ctx.frames[i].size > app_ctx.max_frame_size) {
            app_ctx.max_frame_size = app_ctx.frames[i].size;
        }
    }
    printf("[IFRAME] trace=%s frames=%d max_frame_size=%zu (size==max => I frame)\n",
        trace_file, app_ctx.frame_count, app_ctx.max_frame_size);

    struct sockaddr_storage addr;
    int is_name = 0;

    if (picoquic_get_server_address(server_ip, port, &addr, &is_name) != 0) {
        fprintf(stderr, "Invalid address\n");
        picoquic_free(quic);
        free(app_ctx.frames);
        return 1;
    }
    memcpy(&app_ctx.server_addr, &addr, sizeof(addr));

    picoquic_cnx_t* cnx = picoquic_create_cnx(
        quic,
        picoquic_null_connection_id,
        picoquic_null_connection_id,
        (struct sockaddr*)&addr,
        current_time,
        0,
        "localhost",
        VIDEO_ALPN,
        1);

    if (!cnx) {
        fprintf(stderr, "Could not create connection\n");
        picoquic_free(quic);
        free(app_ctx.frames);
        return 1;
    }

    picoquic_set_callback(cnx, sender_callback, &app_ctx);

    /* I帧优先机制测试：启用/关闭传输层 I/BP 帧抢占（运行期开关，支持消融）。
     * 配置为 0/1，通过第 4 个命令行参数传入；demo_dubao.py 通过 PREEMPT_CFG 环境变量透传。
     * 当前机制（picoquic work@9e620450）：应用按"一帧一流"经
     * picoquic_add_to_stream_with_frame_type 标记 I/BP；发包前先放行 I 帧队列
     * （I 帧流置优先级 0），I 队列空才放行 BP 帧；跨帧与多路径抢占由未改动的调度器完成。 */
    const char* preempt_cfg = (argc > 4) ? argv[4] : NULL;
    int pe = 1;
    parse_preempt_config(preempt_cfg, &pe);
    picoquic_set_frame_preemption(cnx, pe);
    printf("[PREEMPT] frame-preemption cfg='%s' -> enabled=%d\n",
        (preempt_cfg ? preempt_cfg : "(default)"), pe);

    if (picoquic_start_client_cnx(cnx) != 0) {
        fprintf(stderr, "Could not start client connection\n");
        picoquic_delete_cnx(cnx);
        picoquic_free(quic);
        free(app_ctx.frames);
        return 1;
    }

    picoquic_server_sockets_t sockets;
    if (picoquic_open_server_sockets(&sockets, 0) != 0) {
        fprintf(stderr, "Could not open client UDP sockets\n");
        picoquic_delete_cnx(cnx);
        picoquic_free(quic);
        free(app_ctx.frames);
        return 1;
    }

    app_ctx.probe_socket_v4 = sockets.s_socket[1];

    printf("Starting Sender Loop...\n");

    uint8_t* packet_buffer = malloc(1536);
    if (!packet_buffer) {
        fprintf(stderr, "malloc failed\n");
        picoquic_close_server_sockets(&sockets);
        picoquic_delete_cnx(cnx);
        picoquic_free(quic);
        free(app_ctx.frames);
        return 1;
    }

    while (!app_ctx.is_disconnected && app_ctx.current_frame_idx < app_ctx.frame_count) {

        current_time = picoquic_current_time();

        /* 定时打印队列状态 */
        static uint64_t last_queue_print_time = 0;
        if (app_ctx.is_connected && current_time > last_queue_print_time + 200000) {
            last_queue_print_time = current_time;
            if (chunk_queue_count > 0) {
                printf("\n==================================================================\n");
                printf("[Queue Status Validate] Current Time: %lu us, Queue Count = %d\n", current_time, chunk_queue_count);
                for (int i = 0; i < chunk_queue_count; i++) {
                    int idx = (chunk_queue_head + i) % MAX_CHUNK_QUEUE_SIZE;
                    video_chunk_t* c = &chunk_queue[idx];
                    printf("  -> [%d] frame_idx:%d, size:%zu, rem:%zu, D':%lu, canceled:%d\n",
                        i, c->frame_idx, c->size, c->remaining_size, c->effective_deadline, c->canceled);
                }
                printf("==================================================================\n\n");
            }
        }

        /* 多路径 probe */
        if (app_ctx.is_connected && picoquic_get_cnx_state(cnx) == picoquic_state_ready) {
            sender_try_probe(&app_ctx, cnx, current_time);
        }

        if (app_ctx.is_connected && picoquic_get_cnx_state(cnx) == picoquic_state_ready) {
            int new_frame_arrived = (current_time >= app_ctx.next_send_time && app_ctx.current_frame_idx < app_ctx.frame_count);

            if (new_frame_arrived) {
                video_frame_t* frame = &app_ctx.frames[app_ctx.current_frame_idx];

                video_chunk_t new_chunk;
                memset(&new_chunk, 0, sizeof(new_chunk));
                new_chunk.frame_idx = app_ctx.current_frame_idx;
                new_chunk.size = frame->size;
                new_chunk.remaining_size = frame->size;
                new_chunk.arrival_time = current_time;
                new_chunk.deadline = current_time + 100000;
                new_chunk.priority = (double)frame->size;
                new_chunk.canceled = 0;

                preprocess_chunk_deadline(&new_chunk);
                enqueue_chunk(new_chunk);

                const char* fc_name = (frame->size >= app_ctx.max_frame_size && app_ctx.max_frame_size > 0) ? "I" : "BP";
                printf("[FRAME_GEN] frame=%d size=%zu npkts=%d class=%s gen_ts=%lu\n",
                    new_chunk.frame_idx, frame->size,
                    (int)((frame->size + APP_PKT_PAYLOAD_MAX - 1) / APP_PKT_PAYLOAD_MAX),
                    fc_name, current_time);

                app_ctx.next_send_time += frame->wait_time_us;
                app_ctx.current_frame_idx++;
            }

            if (check_dams_scheduling_trigger(&app_ctx, current_time, new_frame_arrived)) {
                execute_dams_scheduling(current_time);
            }

            /* 清理过期/取消的队首 */
            while (chunk_queue_count > 0) {
                video_chunk_t* head_chunk = &chunk_queue[chunk_queue_head];
                if (head_chunk->canceled || head_chunk->effective_deadline <= current_time || head_chunk->remaining_size == 0) {
                    chunk_queue_head = (chunk_queue_head + 1) % MAX_CHUNK_QUEUE_SIZE;
                    chunk_queue_count--;
                } else {
                    break;
                }
            }

            /* 发送队头帧 */
            if (chunk_queue_count > 0) {
                video_chunk_t* sending_chunk = &chunk_queue[chunk_queue_head];

                uint64_t stream_id = picoquic_get_next_local_stream_id(cnx, 1);

                /* I帧优先机制测试：按帧尺寸判定帧类并传给传输层。
                 * size == 轨迹最大尺寸 => I 帧（关键帧），否则 BP 帧。
                 * 传输层标记用 picoquic.h 的 PICOQUIC_VIDEO_FRAME_I/BP（值序与应用层相反），
                 * 应用头 frame_class 用 APP_FRAME_CLASS_I/BP（仅接收端日志观测）。 */
                int frame_class = APP_FRAME_CLASS_BP;
                if (app_ctx.max_frame_size > 0 &&
                    app_ctx.frames[sending_chunk->frame_idx].size >= app_ctx.max_frame_size) {
                    frame_class = APP_FRAME_CLASS_I;
                }
                const char* fc_name = (frame_class == APP_FRAME_CLASS_I) ? "I" : "BP";

                size_t stream_len = 0;
                uint8_t* stream_buffer = build_frame_stream_buffer(sending_chunk->frame_idx, frame_class,
                    &app_ctx.frames[sending_chunk->frame_idx], &stream_len);
                if (!stream_buffer) {
                    printf("[DAMS] Error: Failed to allocate stream buffer for frame %d\n", sending_chunk->frame_idx);
                    break;
                }

                /* I帧优先机制测试：按帧类标记数据（一帧一流）。
                 * 机制关闭或标记非法时，该 API 行为与 picoquic_add_to_stream 完全一致。 */
                int ret_add = picoquic_add_to_stream_with_frame_type(cnx, stream_id, stream_buffer, stream_len, 1,
                    (frame_class == APP_FRAME_CLASS_I) ? PICOQUIC_VIDEO_FRAME_I : PICOQUIC_VIDEO_FRAME_BP);

                if (ret_add == 0) {
                    printf("[STREAM_MAP] stream=%" PRIu64 " frame=%d size=%zu stream_len=%zu class=%s first_pkt_ts=%lu\n",
                        stream_id, sending_chunk->frame_idx, sending_chunk->size, stream_len, fc_name, current_time);

                    app_ctx.current_stream_id = stream_id;
                    sending_chunk->remaining_size = 0;

                    chunk_queue_head = (chunk_queue_head + 1) % MAX_CHUNK_QUEUE_SIZE;
                    chunk_queue_count--;
                } else {
                    printf("[DAMS] Warning: Failed to add stream %" PRIu64 " to connection\n", stream_id);
                }

                free(stream_buffer);
            }
        }

        /* 计算唤醒时间 */
        int64_t delta_t = picoquic_get_next_wake_delay(quic, current_time, 10000000);
        if (app_ctx.next_send_time > current_time) {
            int64_t wait_frame = app_ctx.next_send_time - current_time;
            if (wait_frame < delta_t) delta_t = wait_frame;
        }

        struct sockaddr_storage peer_addr;
        struct sockaddr_storage local_addr;
        int if_index;
        unsigned char received_ecn;

        int ret = picoquic_select(
            sockets.s_socket, PICOQUIC_NB_SERVER_SOCKETS,
            &peer_addr, &local_addr, &if_index, &received_ecn,
            packet_buffer + UDP_HDR_SIZE, 1536 - UDP_HDR_SIZE, delta_t, &current_time);

        if (ret < 0) break;

        if (ret > 0) {
            (void)picoquic_incoming_packet(
                quic, packet_buffer + UDP_HDR_SIZE, ret,
                (struct sockaddr*)&peer_addr, (struct sockaddr*)&local_addr,
                if_index, received_ecn, current_time);
        }

        /* 准备并发送 UDP 报文，统一加首部 */
        while (1) {
            size_t send_length = 0;
            picoquic_connection_id_t logcid;
            picoquic_cnx_t* last_cnx = NULL;

            int ret_prep = picoquic_prepare_next_packet(
                quic, current_time,
                packet_buffer + UDP_HDR_SIZE, 1536 - UDP_HDR_SIZE,
                &send_length,
                &peer_addr, &local_addr, &if_index,
                &logcid, &last_cnx);

            if (ret_prep == 0 && send_length > 0) {
                uint32_t seq = app_ctx.global_pkt_seq++;
                uint64_t send_ts = current_time;

                memcpy(packet_buffer + 0, &send_ts, sizeof(uint64_t));
                memcpy(packet_buffer + 8, &seq, sizeof(uint32_t));
                uint32_t reserved = 0;
                memcpy(packet_buffer + 12, &reserved, sizeof(uint32_t));

                /* useful 判定：只要还在发视频帧阶段，就认为大概率携带数据 */
                int useful = (app_ctx.current_stream_id != 0 && app_ctx.current_frame_idx <= app_ctx.frame_count) ? 1 : 0;

                /* 推断当前包主要服务的帧：取 current_stream_id 对应的帧 */
                int pkt_frame = -1;
                /* 这里无法从 current_stream_id 反查 frame，用 current_frame_idx-1 近似 */
                if (app_ctx.current_frame_idx > 0) {
                    pkt_frame = app_ctx.current_frame_idx - 1;
                }

                printf("[PKT_SEND] seq=%u stream=%" PRIu64 " frame=%d path=%d send_ts=%lu size=%zu useful=%d\n",
                    seq, app_ctx.current_stream_id, pkt_frame, if_index, send_ts, send_length, useful);

                int sock_err = 0;
                (void)picoquic_send_through_server_sockets(
                    &sockets,
                    (struct sockaddr*)&peer_addr,
                    (struct sockaddr*)&local_addr,
                    if_index,
                    (const char*)packet_buffer,
                    (int)(send_length + UDP_HDR_SIZE),
                    &sock_err);
            } else {
                break;
            }
        }
    }

    /* ============ 排空阶段：等待 QUIC 发送队列真正排空再关闭 ============
     * 所有帧虽已写入 QUIC 发送队列（current_frame_idx == frame_count），但受拥塞窗口
     * /流控/链路带宽限制，大量数据可能仍在发送队列中未发出。此时若直接
     * picoquic_close()，disconnecting 状态只发 ACK+CLOSE，剩余数据全部丢失。
     * 因此继续收发循环，直到 QUIC 连续多轮没有任何发送/重传任务，再关闭。 */
    if (app_ctx.current_frame_idx >= app_ctx.frame_count && !app_ctx.is_disconnected) {
        printf("Video trace finished. Draining QUIC send queue...\n");

        int drain_streak = 0;
        uint64_t drain_start = current_time;
        uint64_t last_progress_print = 0;

        while (!app_ctx.is_disconnected && picoquic_get_cnx_state(cnx) != picoquic_state_disconnected) {

            current_time = picoquic_current_time();

            /* 进入本轮时 QUIC 的下一次唤醒间隔：小 => 有发送/重传任务；大 => 空闲 */
            int64_t delta_t = picoquic_get_next_wake_delay(quic, current_time, 10000000);

            /* select 等待时间封顶，空闲时每 DRAIN_SELECT_POLL_US 轮询一次判定 */
            int64_t select_timeout = delta_t;
            if (select_timeout > DRAIN_SELECT_POLL_US) select_timeout = DRAIN_SELECT_POLL_US;

            struct sockaddr_storage peer_addr;
            struct sockaddr_storage local_addr;
            int if_index;
            unsigned char received_ecn;

            int ret = picoquic_select(
                sockets.s_socket, PICOQUIC_NB_SERVER_SOCKETS,
                &peer_addr, &local_addr, &if_index, &received_ecn,
                packet_buffer + UDP_HDR_SIZE, 1536 - UDP_HDR_SIZE, select_timeout, &current_time);

            if (ret < 0) break;

            if (ret > 0) {
                (void)picoquic_incoming_packet(
                    quic, packet_buffer + UDP_HDR_SIZE, ret,
                    (struct sockaddr*)&peer_addr, (struct sockaddr*)&local_addr,
                    if_index, received_ecn, current_time);
            }

            /* 持续发送队列中剩余的数据（含丢失重传），统一加 UDP 首部 */
            int sent_any = 0;
            while (1) {
                size_t send_length = 0;
                picoquic_connection_id_t logcid;
                picoquic_cnx_t* last_cnx = NULL;

                int ret_prep = picoquic_prepare_next_packet(
                    quic, current_time,
                    packet_buffer + UDP_HDR_SIZE, 1536 - UDP_HDR_SIZE,
                    &send_length,
                    &peer_addr, &local_addr, &if_index,
                    &logcid, &last_cnx);

                if (ret_prep == 0 && send_length > 0) {
                    uint32_t seq = app_ctx.global_pkt_seq++;
                    uint64_t send_ts = current_time;

                    memcpy(packet_buffer + 0, &send_ts, sizeof(uint64_t));
                    memcpy(packet_buffer + 8, &seq, sizeof(uint32_t));
                    uint32_t reserved = 0;
                    memcpy(packet_buffer + 12, &reserved, sizeof(uint32_t));

                    printf("[PKT_SEND] seq=%u stream=%" PRIu64 " frame=-1 path=%d send_ts=%lu size=%zu useful=1\n",
                        seq, app_ctx.current_stream_id, if_index, send_ts, send_length);

                    int sock_err = 0;
                    (void)picoquic_send_through_server_sockets(
                        &sockets,
                        (struct sockaddr*)&peer_addr,
                        (struct sockaddr*)&local_addr,
                        if_index,
                        (const char*)packet_buffer,
                        (int)(send_length + UDP_HDR_SIZE),
                        &sock_err);

                    sent_any = 1;
                } else {
                    break;
                }
            }

            /* 排空完成判定：本轮无任何发送任务，且下一次唤醒间隔足够长，
             * 说明 QUIC 既无待发送数据、也无待重传/待确认数据（已全部被对端确认） */
            if (!sent_any && delta_t >= DRAIN_IDLE_THRESHOLD_US) {
                drain_streak++;
                if (drain_streak >= DRAIN_STREAK_REQUIRED) {
                    printf("[DRAIN] QUIC send queue drained after %lu us (frames %d/%d).\n",
                        current_time - drain_start, app_ctx.current_frame_idx, app_ctx.frame_count);
                    break;
                }
            } else {
                drain_streak = 0;
            }

            /* 排空进度打印（每 2s） */
            if (current_time > last_progress_print + 2000000) {
                last_progress_print = current_time;
                printf("[DRAIN] still draining, elapsed=%lu us, streak=%d, next_wake=%lld us\n",
                    current_time - drain_start, drain_streak, (long long)delta_t);
            }

            /* 超时保护：对端失联/链路中断等异常时强制关闭，避免无限等待 */
            if (current_time - drain_start > DRAIN_TIMEOUT_US) {
                printf("[DRAIN] Timeout after %lu us, closing anyway (progress=%d/%d).\n",
                    current_time - drain_start, app_ctx.current_frame_idx, app_ctx.frame_count);
                break;
            }
        }

        printf("Video trace finished. Closing connection.\n");
        picoquic_close(cnx, 0);
    } else {
        printf("Sender loop ended early. progress=%d/%d (disconnected=%d)\n",
            app_ctx.current_frame_idx, app_ctx.frame_count, app_ctx.is_disconnected);
    }

    /* 关闭阶段也要统一加 UDP 首部 */
    while (picoquic_get_cnx_state(cnx) != picoquic_state_disconnected) {
        current_time = picoquic_current_time();
        int64_t delta_t = picoquic_get_next_wake_delay(quic, current_time, 10000000);

        struct sockaddr_storage peer_addr;
        struct sockaddr_storage local_addr;
        int if_index;
        unsigned char received_ecn;

        int ret = picoquic_select(
            sockets.s_socket, PICOQUIC_NB_SERVER_SOCKETS,
            &peer_addr, &local_addr, &if_index, &received_ecn,
            packet_buffer + UDP_HDR_SIZE, 1536 - UDP_HDR_SIZE, delta_t, &current_time);

        if (ret > 0) {
            (void)picoquic_incoming_packet(
                quic, packet_buffer + UDP_HDR_SIZE, ret,
                (struct sockaddr*)&peer_addr, (struct sockaddr*)&local_addr,
                if_index, received_ecn, current_time);
        }

        while (1) {
            size_t send_length = 0;
            picoquic_connection_id_t logcid;
            picoquic_cnx_t* last_cnx = NULL;

            int ret_prep = picoquic_prepare_next_packet(
                quic, current_time,
                packet_buffer + UDP_HDR_SIZE, 1536 - UDP_HDR_SIZE,
                &send_length,
                &peer_addr, &local_addr, &if_index,
                &logcid, &last_cnx);

            if (ret_prep == 0 && send_length > 0) {
                uint32_t seq = app_ctx.global_pkt_seq++;
                uint64_t send_ts = current_time;

                memcpy(packet_buffer + 0, &send_ts, sizeof(uint64_t));
                memcpy(packet_buffer + 8, &seq, sizeof(uint32_t));
                uint32_t reserved = 0;
                memcpy(packet_buffer + 12, &reserved, sizeof(uint32_t));

                printf("[PKT_SEND] seq=%u stream=0 frame=-1 path=%d send_ts=%lu size=%zu useful=0\n",
                    seq, if_index, send_ts, send_length);

                int sock_err = 0;
                (void)picoquic_send_through_server_sockets(
                    &sockets,
                    (struct sockaddr*)&peer_addr,
                    (struct sockaddr*)&local_addr,
                    if_index,
                    (const char*)packet_buffer,
                    (int)(send_length + UDP_HDR_SIZE),
                    &sock_err);
            } else {
                break;
            }
        }
    }

    picoquic_close_server_sockets(&sockets);
    picoquic_free(quic);
    free(app_ctx.frames);
    free(packet_buffer);
    return 0;
}
