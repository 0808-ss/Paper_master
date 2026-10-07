#include <picoquic.h>
#include <picoquic_utils.h>
#include <picosocks.h>

#include <arpa/inet.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include <autoqlog.h>

#include "video_common.h"   /* I帧优先机制测试：APP_PKT_HEADER_SIZE / APP_FRAME_CLASS_I 等
                               共享常量统一来自该头文件（接收端此前未包含，需补上） */

#define VIDEO_ALPN "video-test"

/*
 * 应用层包头部大小，与 sender 约定一致（布局见 video_common.h）。
 * [ frame_idx (4) | frame_class (4) | pkt_idx (4) | total_pkts (4) | payload_size (4) ]
 */
/* APP_PKT_HEADER_SIZE / APP_PKT_PAYLOAD_MAX 已统一到 video_common.h */

/*
 * UDP 自定义首部大小，与 sender 约定一致。
 * [ send_ts_us (8) | global_pkt_seq (4) | reserved (4) ]
 */
#define UDP_HDR_SIZE 16

typedef struct {
    /* 帧级信息 */
    uint64_t frame_idx;
    uint32_t frame_class;   /* I帧优先机制测试：0=BP, 1=I（取自 app pkt 头部） */
    int total_pkts;
    int pkts_received;
    size_t received_len;

    /* 流数据重组缓冲，因为 stream_data 回调可能只收到部分 app pkt */
    uint8_t* reassembly_buf;
    size_t reassembly_len;
    size_t reassembly_capacity;
} stream_ctx_t;

typedef struct {
    uint64_t unique_path_id;
    struct sockaddr_storage local;
    struct sockaddr_storage peer;
    uint64_t packet_count;
} receiver_path_entry_t;

typedef struct {
    /* 最近一次收到的 UDP 报文来源信息 */
    struct sockaddr_storage last_peer;
    struct sockaddr_storage last_local;
    int last_if_index;

    /* path_id -> (local, peer) 映射 */
    receiver_path_entry_t paths[8];
    int path_count;

    uint64_t pkts_unknown;

    int close_printed;
    uint64_t last_stat_print_time;

    picoquic_cnx_t* cnx_ref;

    /* 当前 UDP 报文是否触发了 stream_data，用于 PKT_RECV 的 useful 字段 */
    int current_pkt_had_stream_data;
} receiver_app_ctx_t;

static int sockaddr_ip_port_equal(const struct sockaddr_storage* a, const struct sockaddr_storage* b)
{
    if (a->ss_family != b->ss_family) return 0;

    if (a->ss_family == AF_INET) {
        const struct sockaddr_in* ia = (const struct sockaddr_in*)a;
        const struct sockaddr_in* ib = (const struct sockaddr_in*)b;
        return ia->sin_port == ib->sin_port && ia->sin_addr.s_addr == ib->sin_addr.s_addr;
    } else if (a->ss_family == AF_INET6) {
        const struct sockaddr_in6* ia = (const struct sockaddr_in6*)a;
        const struct sockaddr_in6* ib = (const struct sockaddr_in6*)b;
        return ia->sin6_port == ib->sin6_port && memcmp(&ia->sin6_addr, &ib->sin6_addr, sizeof(ia->sin6_addr)) == 0;
    }

    return 0;
}

static int receiver_find_path_index(receiver_app_ctx_t* app, uint64_t unique_path_id)
{
    for (int i = 0; i < app->path_count; i++) {
        if (app->paths[i].unique_path_id == unique_path_id) return i;
    }
    return -1;
}

static void receiver_store_path_tuple(receiver_app_ctx_t* app, picoquic_cnx_t* cnx, uint64_t unique_path_id)
{
    struct sockaddr_storage local = { 0 };
    struct sockaddr_storage peer = { 0 };

    if (picoquic_get_path_addr(cnx, unique_path_id, 1, &local) != 0) return;
    if (picoquic_get_path_addr(cnx, unique_path_id, 2, &peer) != 0) return;

    int idx = receiver_find_path_index(app, unique_path_id);
    if (idx < 0) {
        if (app->path_count >= (int)(sizeof(app->paths) / sizeof(app->paths[0]))) return;
        idx = app->path_count++;
        app->paths[idx].unique_path_id = unique_path_id;
    }

    app->paths[idx].local = local;
    app->paths[idx].peer = peer;
}

static int receiver_lookup_path_id_by_addr(receiver_app_ctx_t* app,
    const struct sockaddr_storage* local,
    const struct sockaddr_storage* peer,
    uint64_t* out_path_id)
{
    for (int i = 0; i < app->path_count; i++) {
        if (sockaddr_ip_port_equal(&app->paths[i].local, local) &&
            sockaddr_ip_port_equal(&app->paths[i].peer, peer)) {
            *out_path_id = app->paths[i].unique_path_id;
            return 1;
        }
    }
    return 0;
}

/*
 * 解析并消费 reassembly_buf 中所有完整的应用包。
 * 每解析出一个完整应用包就打印 [APP_PKT_RECV]。
 */
static void receiver_parse_app_pkts(stream_ctx_t* s_ctx, uint64_t recv_ts)
{
    while (s_ctx->reassembly_len >= APP_PKT_HEADER_SIZE) {
        uint32_t frame_idx = 0, frame_class = 0, pkt_idx = 0, total_pkts = 0, payload_size = 0;
        memcpy(&frame_idx, s_ctx->reassembly_buf + 0, sizeof(uint32_t));
        memcpy(&frame_class, s_ctx->reassembly_buf + 4, sizeof(uint32_t));
        memcpy(&pkt_idx, s_ctx->reassembly_buf + 8, sizeof(uint32_t));
        memcpy(&total_pkts, s_ctx->reassembly_buf + 12, sizeof(uint32_t));
        memcpy(&payload_size, s_ctx->reassembly_buf + 16, sizeof(uint32_t));

        if (payload_size > APP_PKT_PAYLOAD_MAX || total_pkts == 0 || total_pkts > 100000) {
            printf("[APP_PKT_RECV] Warning: invalid app pkt header, payload_size=%u total=%u\n",
                payload_size, total_pkts);
            break;
        }

        if (s_ctx->reassembly_len < APP_PKT_HEADER_SIZE + payload_size) {
            break;
        }

        if (s_ctx->frame_idx == 0 && s_ctx->total_pkts == 0) {
            s_ctx->frame_idx = frame_idx;
            s_ctx->frame_class = frame_class;
            s_ctx->total_pkts = (int)total_pkts;
        }
        s_ctx->pkts_received++;

        printf("[APP_PKT_RECV] frame=%u class=%s pkt=%u total=%u recv_ts=%lu payload_size=%u\n",
            frame_idx, (frame_class == APP_FRAME_CLASS_I) ? "I" : "BP",
            pkt_idx, total_pkts, recv_ts, payload_size);

        size_t consumed = APP_PKT_HEADER_SIZE + payload_size;
        size_t remain = s_ctx->reassembly_len - consumed;
        if (remain > 0) {
            memmove(s_ctx->reassembly_buf, s_ctx->reassembly_buf + consumed, remain);
        }
        s_ctx->reassembly_len = remain;
    }
}

static int receiver_callback(picoquic_cnx_t* cnx,
    uint64_t stream_id, uint8_t* bytes, size_t length,
    picoquic_call_back_event_t fin_or_event, void* callback_ctx, void* stream_ctx)
{
    receiver_app_ctx_t* app = (receiver_app_ctx_t*)callback_ctx;
    stream_ctx_t* s_ctx = (stream_ctx_t*)stream_ctx;

    switch (fin_or_event) {
    case picoquic_callback_ready:
        printf("Connection established.\n");
        app->cnx_ref = cnx;
        receiver_store_path_tuple(app, cnx, 0);
        break;

    case picoquic_callback_stream_data:
        if (s_ctx == NULL) {
            s_ctx = (stream_ctx_t*)malloc(sizeof(stream_ctx_t));
            if (!s_ctx) return -1;
            memset(s_ctx, 0, sizeof(stream_ctx_t));
            picoquic_set_app_stream_ctx(cnx, stream_id, s_ctx);
        }

        app->current_pkt_had_stream_data = 1;

        /* 将收到的字节追加到重组缓冲 */
        if (length > 0) {
            size_t need = s_ctx->reassembly_len + length;
            if (need > s_ctx->reassembly_capacity) {
                size_t new_cap = s_ctx->reassembly_capacity == 0 ? 4096 : s_ctx->reassembly_capacity * 2;
                while (new_cap < need) new_cap *= 2;
                uint8_t* new_buf = (uint8_t*)realloc(s_ctx->reassembly_buf, new_cap);
                if (!new_buf) return -1;
                s_ctx->reassembly_buf = new_buf;
                s_ctx->reassembly_capacity = new_cap;
            }
            memcpy(s_ctx->reassembly_buf + s_ctx->reassembly_len, bytes, length);
            s_ctx->reassembly_len += length;
            s_ctx->received_len += length;

            receiver_parse_app_pkts(s_ctx, picoquic_current_time());
        }
        break;

    case picoquic_callback_stream_fin:
        if (s_ctx) {
            uint64_t now = picoquic_current_time();
            /* 如果还有未解析完的数据，最后再尝试一次 */
            receiver_parse_app_pkts(s_ctx, now);

            printf("[FRAME_RECV] frame=%" PRIu64 " class=%s stream=%" PRIu64 " total_pkts=%d recv_pkts=%d size=%zu recv_ts=%lu\n",
                s_ctx->frame_idx, (s_ctx->frame_class == APP_FRAME_CLASS_I) ? "I" : "BP",
                stream_id, s_ctx->total_pkts, s_ctx->pkts_received,
                s_ctx->received_len, now);

            free(s_ctx->reassembly_buf);
            free(s_ctx);
            picoquic_set_app_stream_ctx(cnx, stream_id, NULL);
        } else {
            printf("[FRAME_RECV] frame=-1 stream=%" PRIu64 " size=0 recv_ts=%lu\n",
                stream_id, picoquic_current_time());
        }
        break;

    case picoquic_callback_stream_reset:
    case picoquic_callback_stop_sending:
        if (s_ctx) {
            free(s_ctx->reassembly_buf);
            free(s_ctx);
            picoquic_set_app_stream_ctx(cnx, stream_id, NULL);
        }
        break;

    case picoquic_callback_path_available: {
        uint64_t pid = stream_id;
        receiver_store_path_tuple(app, cnx, pid);

        char ltxt[128] = { 0 };
        char ptxt[128] = { 0 };
        struct sockaddr_storage local = { 0 }, peer = { 0 };
        (void)picoquic_get_path_addr(cnx, pid, 1, &local);
        (void)picoquic_get_path_addr(cnx, pid, 2, &peer);
        picoquic_addr_text((struct sockaddr*)&local, ltxt, sizeof(ltxt));
        picoquic_addr_text((struct sockaddr*)&peer, ptxt, sizeof(ptxt));

        printf("Multipath Event: Path Available. Path ID: %" PRIu64 " (Local=%s, Peer=%s)\n", pid, ltxt, ptxt);
        break;
    }
    case picoquic_callback_path_suspended:
        printf("Multipath Event: Path Suspended. Path ID: %" PRIu64 "\n", stream_id);
        break;
    case picoquic_callback_path_deleted:
        printf("Multipath Event: Path Deleted. Path ID: %" PRIu64 "\n", stream_id);
        break;
    case picoquic_callback_path_quality_changed:
        receiver_store_path_tuple(app, cnx, stream_id);
        printf("Multipath Event: Path Quality Changed. Path ID: %" PRIu64 "\n", stream_id);
        break;

    case picoquic_callback_close:
    case picoquic_callback_application_close:
        if (!app->close_printed) {
            app->cnx_ref = NULL;
            app->close_printed = 1;
            uint64_t le = picoquic_get_local_error(cnx);
            uint64_t re = picoquic_get_remote_error(cnx);
            uint64_t ae = picoquic_get_application_error(cnx);
            printf("Receiver: Connection closed. local=0x%" PRIx64 "(%s), remote=0x%" PRIx64 "(%s), app=0x%" PRIx64 "(%s)\n",
                le, picoquic_error_name(le),
                re, picoquic_error_name(re),
                ae, picoquic_error_name(ae));
        }
        break;

    default:
        break;
    }

    return 0;
}

int main(int argc, char** argv)
{
    setbuf(stdout, NULL);

    if (argc < 2) {
        printf("Usage: %s <port> [cert_file] [key_file]\n", argv[0]);
        return 1;
    }

    int port = atoi(argv[1]);
    const char* cert_file = (argc > 2) ? argv[2] : NULL;
    const char* key_file = (argc > 3) ? argv[3] : NULL;

    receiver_app_ctx_t app_ctx;
    memset(&app_ctx, 0, sizeof(app_ctx));
    app_ctx.last_if_index = -1;

    picoquic_quic_t* quic = picoquic_create(
        8,
        cert_file, key_file, NULL,
        VIDEO_ALPN,
        receiver_callback, &app_ctx,
        NULL, NULL,
        NULL,
        picoquic_current_time(),
        NULL,
        NULL,
        NULL, 0);

    if (!quic) {
        fprintf(stderr, "Could not create quic context\n");
        return 1;
    }

    picoquic_set_default_multipath_option(quic, 1);
    picoquic_enable_path_callbacks_default(quic, 1);
    picoquic_set_default_tp_value(quic, picoquic_tp_active_connection_id_limit, 8);
    picoquic_set_default_address_discovery_mode(quic, 3);

    /* 启用 qlog */
    const char* qlog_dir = "./qlog_receiver";
    (void)mkdir(qlog_dir, 0755);
    picoquic_set_qlog(quic, qlog_dir);
    picoquic_set_log_level(quic, 1);

    picoquic_server_sockets_t sockets;

    if (picoquic_open_server_sockets(&sockets, port) != 0) {
        fprintf(stderr, "Could not open sockets\n");
        picoquic_free(quic);
        return 1;
    }

    printf("Receiver listening on port %d...\n", port);

    uint64_t current_time = picoquic_current_time();
    uint8_t* packet_buffer = malloc(1536);
    if (!packet_buffer) {
        fprintf(stderr, "malloc failed\n");
        picoquic_close_server_sockets(&sockets);
        picoquic_free(quic);
        return 1;
    }

    while (1) {
        int64_t delta_t = picoquic_get_next_wake_delay(quic, current_time, 10000000);

        struct sockaddr_storage peer_addr;
        struct sockaddr_storage local_addr;
        int if_index;
        unsigned char received_ecn;

        int ret = picoquic_select(
            sockets.s_socket, PICOQUIC_NB_SERVER_SOCKETS,
            &peer_addr, &local_addr, &if_index, &received_ecn,
            packet_buffer, 1536, delta_t, &current_time);

        if (ret < 0) break;

        if (ret > 0) {
            uint64_t send_ts = 0;
            uint32_t recv_seq = 0;
            uint8_t* quic_ptr = packet_buffer;
            int quic_len = ret;

            /* 统一剥离 16 字节 UDP 自定义首部 */
            if (ret > UDP_HDR_SIZE) {
                memcpy(&send_ts, packet_buffer + 0, sizeof(uint64_t));
                memcpy(&recv_seq, packet_buffer + 8, sizeof(uint32_t));
                quic_ptr = packet_buffer + UDP_HDR_SIZE;
                quic_len = ret - UDP_HDR_SIZE;
            } else {
                /* 长度异常，直接丢弃 */
                printf("[PKT_RECV] seq=UNKNOWN path=UNKNOWN send_ts=0 recv_ts=%lu size=%d useful=0 malformed=1\n",
                    current_time, ret);
                continue;
            }

            app_ctx.last_peer = peer_addr;
            app_ctx.last_local = local_addr;
            app_ctx.last_if_index = if_index;

            app_ctx.current_pkt_had_stream_data = 0;

            (void)picoquic_incoming_packet(
                quic, quic_ptr, quic_len,
                (struct sockaddr*)&peer_addr, (struct sockaddr*)&local_addr,
                if_index, received_ecn, current_time);

            uint64_t pid = 0;
            char path_str[32] = "UNKNOWN";
            int matched = receiver_lookup_path_id_by_addr(&app_ctx, &local_addr, &peer_addr, &pid);
            if (!matched) {
                app_ctx.pkts_unknown++;
            } else {
                int idx = receiver_find_path_index(&app_ctx, pid);
                if (idx >= 0) {
                    app_ctx.paths[idx].packet_count++;
                }
                snprintf(path_str, sizeof(path_str), "%" PRIu64, pid);
            }

            printf("[PKT_RECV] seq=%u path=%s send_ts=%lu recv_ts=%lu size=%d useful=%d\n",
                recv_seq, path_str, send_ts, current_time, quic_len, app_ctx.current_pkt_had_stream_data);

            if (current_time > app_ctx.last_stat_print_time + 100000) {
                app_ctx.last_stat_print_time = current_time;
            }
        }

        while (1) {
            size_t send_length = 0;
            picoquic_connection_id_t logcid;
            picoquic_cnx_t* last_cnx = NULL;

            int ret_prep = picoquic_prepare_next_packet(
                quic, current_time,
                packet_buffer, 1536,
                &send_length,
                &peer_addr, &local_addr, &if_index,
                &logcid, &last_cnx);

            if (ret_prep == 0 && send_length > 0) {
                int sock_err = 0;
                (void)picoquic_send_through_server_sockets(
                    &sockets,
                    (struct sockaddr*)&peer_addr,
                    (struct sockaddr*)&local_addr,
                    if_index,
                    (const char*)packet_buffer,
                    (int)send_length,
                    &sock_err);
            } else {
                break;
            }
        }
    }

    picoquic_close_server_sockets(&sockets);
    picoquic_free(quic);
    free(packet_buffer);
    return 0;
}
