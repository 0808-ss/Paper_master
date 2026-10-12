#define _GNU_SOURCE
#include "media_transport.h"
#include "media_protocol.h"
#include "gst_media.h"
#include "media_playout.h"
#include "media_control.h"
#include <picoquic.h>
#include <picoquic_internal.h>
#include <picoquic_utils.h>
#include <picosocks.h>
#include <autoqlog.h>
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <signal.h>
#include <unistd.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define UDP_EXPERIMENT_HEADER 16
#define WIRE_LIMIT (MEDIA_MAX_AU + MEDIA_HEADER_SIZE + ((MEDIA_MAX_AU + MEDIA_HEADER_SIZE + 1199) / 1200) * MEDIA_APP_HEADER)
#define RECEIVE_BUDGET (32u * 1024u * 1024u)
#define SEND_BUDGET (32u * 1024u * 1024u)
typedef struct rx_stream {
    struct rx_stream *next;
    uint64_t id;
    uint8_t *bytes;
    size_t len, cap;
} rx_stream_t;
typedef struct { uint64_t id; struct sockaddr_storage local, peer; } path_entry_t;
typedef struct {
    int sender, failed, connected, disconnected, settings_seen, ready_seen;
    int start_acked, source_started, source_finished, end_seen, done_sent, peer_done, closing;
    uint64_t t0, source_end_us, close_us, timeout_us, received_frames;
    media_config_t cfg;
    picoquic_cnx_t *cnx;
    picoquic_server_sockets_t sockets;
    gst_media_t *media;
    media_playout_t *playout;
    media_control_server_t *control;
    uint8_t control_buf[MEDIA_CONTROL_SIZE]; size_t control_len;
    rx_stream_t *streams; size_t rx_capacity, rx_count;
    struct sockaddr_storage server;
    uint64_t last_probe; int probe_finished;
    path_entry_t paths[16]; size_t path_count;
    int packet_had_media;
    uint32_t packet_seq;
    uint64_t au_window_start, au_window_bytes;
} transport_t;
static volatile sig_atomic_t interrupted;
static int save_config(const media_config_t *);
static void stop_handler(int signal_number) { (void)signal_number; interrupted = 1; }
static int fail_transport(transport_t *t, const char *why)
{ fprintf(stderr, "[MEDIA_ERROR] %s\n", why); t->failed = 1; return -1; }

static int send_control(transport_t *t, const media_control_record_t *r)
{
    uint8_t bytes[MEDIA_CONTROL_SIZE]; media_control_pack(r, bytes);
    if (picoquic_add_to_stream(t->cnx, 0, bytes, sizeof(bytes), 0)) return fail_transport(t, "control send failed");
    (void)picoquic_set_stream_priority(t->cnx, 0, 1);
    printf("[CONTROL_SEND] stream=0 type=%u bytes=%u\n", r->type, MEDIA_CONTROL_SIZE); return 0;
}
static void save_path(transport_t *t, uint64_t id)
{
    struct sockaddr_storage local = {0}, peer = {0};
    if (picoquic_get_path_addr(t->cnx, id, 1, &local) || picoquic_get_path_addr(t->cnx, id, 2, &peer)) return;
    size_t i; for (i = 0; i < t->path_count && t->paths[i].id != id; ++i) {}
    if (i == t->path_count) { if (i == 16) return; ++t->path_count; }
    t->paths[i].id = id; t->paths[i].local = local; t->paths[i].peer = peer;
    char a[128], b[128]; picoquic_addr_text((struct sockaddr *)&local, a, sizeof(a));
    picoquic_addr_text((struct sockaddr *)&peer, b, sizeof(b));
    printf("[PATH] path_id=%" PRIu64 " local=%s peer=%s\n", id, a, b);
}
static int tuple_equal(const struct sockaddr_storage *a, const struct sockaddr_storage *b)
{
    if (a->ss_family != b->ss_family) return 0;
    if (a->ss_family == AF_INET) {
        const struct sockaddr_in *x = (const void *)a, *y = (const void *)b;
        return x->sin_port == y->sin_port && x->sin_addr.s_addr == y->sin_addr.s_addr;
    }
    if (a->ss_family == AF_INET6) {
        const struct sockaddr_in6 *x = (const void *)a, *y = (const void *)b;
        return x->sin6_port == y->sin6_port && !memcmp(&x->sin6_addr, &y->sin6_addr, sizeof(x->sin6_addr));
    }
    return 0;
}
static void probe_paths(transport_t *t, uint64_t now)
{
    if (!t->sender || !t->connected || t->probe_finished || now < t->last_probe + 200000) return;
    t->last_probe = now;
    struct sockaddr *local = NULL; picoquic_get_local_addr(t->cnx, &local);
    uint16_t port = local && local->sa_family == AF_INET ? ((struct sockaddr_in *)local)->sin_port : 0;
    if (!port) {
        struct sockaddr_in bound = {0}; socklen_t n = sizeof(bound);
        if (!getsockname(t->sockets.s_socket[1], (struct sockaddr *)&bound, &n)) port = bound.sin_port;
    }
    if (!port) return;
    struct ifaddrs *list = NULL; if (getifaddrs(&list)) return;
    int attempted = 0;
    for (struct ifaddrs *a = list; a; a = a->ifa_next) {
        if (!a->ifa_addr || a->ifa_addr->sa_family != AF_INET || (a->ifa_flags & IFF_LOOPBACK)) continue;
        struct sockaddr_in src = *(struct sockaddr_in *)a->ifa_addr; src.sin_port = port;
        struct sockaddr_storage dst = t->server;
        if (src.sin_addr.s_addr == inet_addr("10.0.2.1")) ((struct sockaddr_in *)&dst)->sin_addr.s_addr = inet_addr("10.0.2.2");
        int index = (int)if_nametoindex(a->ifa_name);
        int ret = picoquic_probe_new_path_ex(t->cnx, (struct sockaddr *)&dst, (struct sockaddr *)&src, index, now, 0);
        char ip[INET_ADDRSTRLEN]; inet_ntop(AF_INET, &src.sin_addr, ip, sizeof(ip));
        printf("[PROBE] local=%s port=%u if_index=%d ret=%d\n", ip, ntohs(port), index, ret);
        attempted = 1;
    }
    freeifaddrs(list); if (attempted) t->probe_finished = 1;
}

static int control_record(transport_t *t, const media_control_record_t *r)
{
    uint64_t now = picoquic_current_time();
    printf("[CONTROL_RECV] stream=0 type=%u bytes=%u\n", r->type, MEDIA_CONTROL_SIZE);
    if (!t->sender && r->type == MEDIA_SETTINGS && !t->settings_seen) {
        t->cfg.width = r->value[0]; t->cfg.height = r->value[1]; t->cfg.fps = r->value[2]; t->cfg.frames = r->value[3];
        t->cfg.bitrate_kbps = r->value[4]; t->cfg.gop = r->value[5]; t->cfg.playout_ms = r->value[6]; t->cfg.decode_margin_ms = r->value[7];
        if (!media_config_valid(&t->cfg)) return fail_transport(t, "invalid media settings");
        struct statvfs disk;
        uint64_t raw_bytes = (uint64_t)t->cfg.width * t->cfg.height * 3 / 2 * t->cfg.frames;
        if (!statvfs(t->cfg.output_dir, &disk) && (uint64_t)disk.f_bavail * disk.f_frsize < raw_bytes + 64 * 1024 * 1024)
            return fail_transport(t, "insufficient receiver raw output disk space");
        if (save_config(&t->cfg)) return fail_transport(t, "receiver configuration write failed");
        t->media = gst_media_decoder_open(&t->cfg);
        if (!t->media || gst_media_start(t->media)) return fail_transport(t, "decoder startup failed");
        t->settings_seen = 1;
        media_control_record_t reply = {0}; reply.type = MEDIA_READY; return send_control(t, &reply);
    }
    if (t->sender && r->type == MEDIA_READY && !r->count && !t->ready_seen) {
        t->ready_seen = 1;
        uint64_t advance = 200000;
        for (int i = 0; i < t->cnx->nb_paths; ++i)
            if (t->cnx->path[i]->smoothed_rtt * 2 + 50000 > advance)
                advance = t->cnx->path[i]->smoothed_rtt * 2 + 50000;
        t->t0 = now + advance;
        media_control_record_t start = {0}; start.type = MEDIA_START; start.time_us = t->t0;
        printf("[VIDEO_START] monotonic_us=%" PRIu64 "\n", t->t0);
        return send_control(t, &start);
    }
    if (!t->sender && r->type == MEDIA_START && t->settings_seen && !t->t0) {
        if (r->time_us <= now || r->time_us > now + 30000000) return fail_transport(t, "START arrived late/outside startup bound");
        t->t0 = r->time_us;
        t->playout = media_playout_open(&t->cfg, t->media, t->t0);
        if (!t->playout) return fail_transport(t, "playout startup failed");
        media_control_record_t reply = {0}; reply.type = MEDIA_READY; reply.time_us = t->t0; reply.count = 1;
        return send_control(t, &reply);
    }
    if (t->sender && r->type == MEDIA_READY && r->count == 1 && t->t0 && !t->start_acked) {
        if (r->time_us != t->t0 || now >= t->t0) return fail_transport(t, "START acknowledgment late/mismatched");
        t->start_acked = 1; return 0;
    }
    if (!t->sender && r->type == MEDIA_END && t->t0 && !t->end_seen) {
        if (r->count != t->cfg.frames) return fail_transport(t, "END source frame count differs from settings");
        t->end_seen = 1; return 0;
    }
    if (t->sender && r->type == MEDIA_PLAYOUT_DONE && t->source_finished && r->count == t->cfg.frames && !t->peer_done) {
        t->peer_done = 1; return 0;
    }
    return fail_transport(t, r->type == MEDIA_ERROR ? "peer media failure" : "unexpected control state/message");
}
static void rx_remove(transport_t *t, rx_stream_t *s)
{
    rx_stream_t **p = &t->streams; while (*p && *p != s) p = &(*p)->next;
    if (*p) *p = s->next;
    t->rx_capacity -= s->cap; --t->rx_count;
    picoquic_set_app_stream_ctx(t->cnx, s->id, NULL); free(s->bytes); free(s);
}
static int transport_callback(picoquic_cnx_t *cnx, uint64_t sid, uint8_t *bytes, size_t length,
    picoquic_call_back_event_t event, void *opaque, void *stream_ctx)
{
    transport_t *t = opaque;
    if (t->cnx && t->cnx != cnx) return -1; /* This experiment accepts one session. */
    t->cnx = cnx;
    if (event == picoquic_callback_ready) {
        t->connected = 1; save_path(t, 0);
        printf("[CC] algorithm=%s\n", cnx->congestion_alg ? cnx->congestion_alg->congestion_algorithm_id : "unknown");
        if (t->sender) {
            int allowed = 0; picoquic_subscribe_new_path_allowed(cnx, &allowed);
            media_control_record_t r = {0}; r.type = MEDIA_SETTINGS;
            r.value[0] = t->cfg.width; r.value[1] = t->cfg.height; r.value[2] = t->cfg.fps; r.value[3] = t->cfg.frames;
            r.value[4] = t->cfg.bitrate_kbps; r.value[5] = t->cfg.gop; r.value[6] = t->cfg.playout_ms; r.value[7] = t->cfg.decode_margin_ms;
            return send_control(t, &r);
        }
    } else if (event == picoquic_callback_path_available || event == picoquic_callback_path_quality_changed) save_path(t, sid);
    else if (event == picoquic_callback_path_suspended || event == picoquic_callback_path_deleted)
        printf("[PATH_EVENT] path_id=%" PRIu64 " event=%d\n", sid, event);
    else if (event == picoquic_callback_next_path_allowed) probe_paths(t, picoquic_current_time());
    else if (event == picoquic_callback_close || event == picoquic_callback_application_close || event == picoquic_callback_stateless_reset) {
        /* Application-close is delivered in closing_received: keep the socket
           loop alive to send the close response and run QUIC draining. */
        t->disconnected = picoquic_get_cnx_state(cnx) == picoquic_state_disconnected;
        printf("[CONNECTION_CLOSE] local=%" PRIu64 " remote=%" PRIu64 " application=%" PRIu64 "\n",
            picoquic_get_local_error(cnx), picoquic_get_remote_error(cnx), picoquic_get_application_error(cnx));
        if (picoquic_get_local_error(cnx) || picoquic_get_remote_error(cnx) || picoquic_get_application_error(cnx) ||
            (t->sender ? !t->peer_done : !t->done_sent)) t->failed = 1;
    } else if (event == picoquic_callback_stream_data || event == picoquic_callback_stream_fin) {
        if (sid == 0) {
            size_t offset = 0;
            while (offset < length) {
                size_t n = MEDIA_CONTROL_SIZE - t->control_len;
                if (n > length - offset) n = length - offset;
                memcpy(t->control_buf + t->control_len, bytes + offset, n); t->control_len += n; offset += n;
                if (t->control_len == MEDIA_CONTROL_SIZE) {
                    media_control_record_t r; t->control_len = 0;
                    if (media_control_unpack(t->control_buf, &r) || control_record(t, &r)) return -1;
                }
            }
            if (event == picoquic_callback_stream_fin && t->control_len) return fail_transport(t, "partial control FIN");
            return 0;
        }
        if (t->sender || (sid & 3u) != 2u || !t->playout) return fail_transport(t, "unexpected media stream/direction/start state");
        t->packet_had_media |= length > 0;
        rx_stream_t *s = stream_ctx;
        if (!s) {
            if (t->rx_count >= 64) return fail_transport(t, "too many unfinished media streams");
            s = calloc(1, sizeof(*s)); if (!s) return fail_transport(t, "stream context allocation");
            s->id = sid; s->next = t->streams; t->streams = s; ++t->rx_count;
            if (picoquic_set_app_stream_ctx(cnx, sid, s)) return fail_transport(t, "stream context association");
        }
        if (length > WIRE_LIMIT - s->len) return fail_transport(t, "media stream exceeds size bound");
        size_t need = s->len + length;
        if (need > s->cap) {
            size_t cap = s->cap ? s->cap : 4096; while (cap < need) cap *= 2;
            if (cap > WIRE_LIMIT) cap = WIRE_LIMIT;
            if (t->rx_capacity + cap - s->cap > RECEIVE_BUDGET) return fail_transport(t, "stream receive memory limit");
            uint8_t *data = realloc(s->bytes, cap); if (!data) return fail_transport(t, "stream buffer allocation");
            t->rx_capacity += cap - s->cap; s->bytes = data; s->cap = cap;
        }
        if (length) memcpy(s->bytes + s->len, bytes, length); s->len += length;
        if (event == picoquic_callback_stream_fin) {
            media_frame_t f;
            if (media_frame_unpack(s->bytes, s->len, &f)) {
                printf("[FRAME_INCOMPLETE] stream=%" PRIu64 " size=%zu\n", sid, s->len);
                rx_remove(t, s); return fail_transport(t, "invalid AU/FIN integrity");
            }
            uint32_t packets = media_read_u32(s->bytes + 12);
            printf("[FRAME_RECV] frame=%" PRIu64 " class=%s stream=%" PRIu64 " total_pkts=%u recv_pkts=%u size=%zu recv_ts=%" PRIu64 "\n",
                f.frame_id, f.flags & MEDIA_FLAG_PRIORITY_I ? "I" : "BP", sid, packets, packets, s->len, picoquic_current_time());
            int result = media_playout_receive(t->playout, &f);
            media_frame_release(&f); rx_remove(t, s);
            if (result) return fail_transport(t, "playout receive/order/memory validation");
            ++t->received_frames;
        }
    } else if (event == picoquic_callback_stream_reset || event == picoquic_callback_stop_sending) {
        if (sid == 0) return fail_transport(t, "control stream reset");
        if (stream_ctx) rx_remove(t, stream_ctx);
        printf("[MEDIA_STREAM_RESET] stream=%" PRIu64 "\n", sid);
    }
    return 0;
}

static size_t queued_bytes(picoquic_cnx_t *c)
{
    size_t n = 0;
    for (picoquic_frame_queue_node_t *q = c->frame_queue_i; q; q = q->next) n += q->length;
    for (picoquic_frame_queue_node_t *q = c->frame_queue_bp; q; q = q->next) n += q->length;
    for (picoquic_stream_head_t *s = c->first_output_stream; s; s = s->next_output_stream)
        for (picoquic_stream_queue_node_t *q = s->send_queue; q; q = q->next_stream_data) n += q->length - q->offset;
    return n;
}
static int queue_empty(picoquic_cnx_t *c)
{
    if (c->frame_queue_i || c->frame_queue_bp || picosplay_first(&c->queue_data_repeat_tree)) return 0;
    for (picoquic_stream_head_t *s = c->first_output_stream; s; s = s->next_output_stream)
        if (s->send_queue || s->is_active || (s->fin_requested && !s->fin_sent)) return 0;
    return picoquic_is_cnx_backlog_empty(c);
}
static int send_one_frame(transport_t *t, uint64_t now)
{
    media_frame_t f; if (!gst_media_pop_au(t->media, &f)) return 0;
    const char *cls = f.flags & MEDIA_FLAG_PRIORITY_I ? "I" : "BP";
    printf("[FRAME_GEN] frame=%" PRIu64 " size=%zu class=%s gen_ts=%" PRIu64 " pts_ns=%" PRIu64 " encoded_ready_us=%" PRIu64 " epoch=%u\n",
        f.frame_id, f.au_len, cls, f.source_mono_us, f.pts_ns, f.encoded_ready_mono_us, f.bitrate_epoch);
    t->au_window_bytes += f.au_len;
    if (!t->au_window_start) t->au_window_start = now;
    if (now - t->au_window_start >= 1000000) {
        printf("[ENCODED_RATE] window_us=%" PRIu64 " au_bytes=%" PRIu64 " kbps=%.3f\n",
            now - t->au_window_start, t->au_window_bytes, (double)t->au_window_bytes * 8000.0 / (now - t->au_window_start));
        t->au_window_start = now; t->au_window_bytes = 0;
    }
    /* Retain the existing 100ms admission deadline minus its 5ms adjustment.
       The existing DAMS size/(size+remaining) cancellation branch never fires
       for an unsubmitted whole AU; do not invent a new capacity estimator. */
    if (now >= f.encoded_ready_mono_us + 95000) {
        printf("[MEDIA_DROP] frame=%" PRIu64 " reason=admission_deadline\n", f.frame_id);
        media_frame_release(&f); return 0;
    }
    if (queued_bytes(t->cnx) + f.au_len > SEND_BUDGET) {
        media_frame_release(&f); return fail_transport(t, "QUIC application backlog memory limit");
    }
    uint8_t *wire = NULL; size_t len = 0;
    if (media_frame_pack(&f, &wire, &len)) { media_frame_release(&f); return fail_transport(t, "AU serialize failed"); }
    uint64_t sid = picoquic_get_next_local_stream_id(t->cnx, 1);
    int result = picoquic_add_to_stream_with_frame_type(t->cnx, sid, wire, len, 1,
        f.flags & MEDIA_FLAG_PRIORITY_I ? PICOQUIC_VIDEO_FRAME_I : PICOQUIC_VIDEO_FRAME_BP);
    if (!result)
        printf("[STREAM_MAP] stream=%" PRIu64 " frame=%" PRIu64 " size=%zu stream_len=%zu class=%s first_pkt_ts=%" PRIu64 "\n",
            sid, f.frame_id, f.au_len, len, cls, now);
    free(wire); media_frame_release(&f);
    return result ? fail_transport(t, "picoquic AU submission failed") : 0;
}

static int wait_io(transport_t *t, int64_t delay)
{
    fd_set fds; FD_ZERO(&fds); int maxfd = -1;
    for (int i = 0; i < PICOQUIC_NB_SERVER_SOCKETS; ++i) {
        int fd = t->sockets.s_socket[i]; if (fd == INVALID_SOCKET) continue;
        if (fd >= FD_SETSIZE) return -1;
        FD_SET(fd, &fds); if (fd > maxfd) maxfd = fd;
    }
    int wake = gst_media_wake_fd(t->media);
    if (wake >= 0) { if (wake >= FD_SETSIZE) return -1; FD_SET(wake, &fds); if (wake > maxfd) maxfd = wake; }
    if (delay < 0) delay = 0;
    struct timeval tv = {(long)(delay / 1000000), (long)(delay % 1000000)};
    int result = select(maxfd + 1, &fds, NULL, NULL, &tv);
    if (result < 0 && errno != EINTR) return -1;
    if (wake >= 0 && result > 0 && FD_ISSET(wake, &fds)) gst_media_clear_wake(t->media);
    return 0;
}
static int network_tick(transport_t *t, picoquic_quic_t *quic, uint64_t now)
{
    uint8_t packet[1536]; struct sockaddr_storage peer, local;
    int index; unsigned char ecn;
    for (int batch = 0; batch < 32; ++batch) {
        int n = picoquic_select(t->sockets.s_socket, PICOQUIC_NB_SERVER_SOCKETS, &peer, &local, &index, &ecn,
            packet, sizeof(packet), 0, &now);
        if (n < 0) return fail_transport(t, "UDP receive failed");
        if (!n) break;
        uint64_t sent = 0; uint32_t seq = 0; const uint8_t *data = packet; int length = n;
        if (!t->sender) {
            if (n <= UDP_EXPERIMENT_HEADER) continue;
            memcpy(&sent, packet, 8); memcpy(&seq, packet + 8, 4);
            data += UDP_EXPERIMENT_HEADER; length -= UDP_EXPERIMENT_HEADER;
        }
        t->packet_had_media = 0;
        int ret = picoquic_incoming_packet(quic, data, (size_t)length, (struct sockaddr *)&peer,
            (struct sockaddr *)&local, index, ecn, now);
        if (!t->sender) {
            char label[32] = "UNKNOWN";
            for (size_t p = 0; p < t->path_count; ++p)
                if (tuple_equal(&local, &t->paths[p].local) && tuple_equal(&peer, &t->paths[p].peer))
                    snprintf(label, sizeof(label), "%" PRIu64, t->paths[p].id);
            printf("[PKT_RECV] seq=%u path=%s send_ts=%" PRIu64 " recv_ts=%" PRIu64 " size=%d useful=%d\n",
                seq, label, sent, now, length, t->packet_had_media);
        }
        /* Malformed datagrams can be ignored by QUIC; application callback errors set failed. */
        (void)ret; if (t->failed) return -1;
        if (t->disconnected) return 0;
    }
    for (int batch = 0; batch < 64; ++batch) {
        size_t n = 0; picoquic_connection_id_t cid; picoquic_cnx_t *last = NULL;
        size_t prefix = t->sender ? UDP_EXPERIMENT_HEADER : 0;
        int ret = picoquic_prepare_next_packet(quic, now, packet + prefix, sizeof(packet) - prefix,
            &n, &peer, &local, &index, &cid, &last);
        if (t->disconnected) return 0;
        if (ret) return fail_transport(t, "QUIC packet preparation failed");
        if (!n) break;
        if (prefix) {
            uint32_t seq = t->packet_seq++, zero = 0;
            memcpy(packet, &now, 8); memcpy(packet + 8, &seq, 4); memcpy(packet + 12, &zero, 4);
            printf("[PKT_SEND] seq=%u stream=UNKNOWN frame=-1 path=%d send_ts=%" PRIu64 " size=%zu useful=UNKNOWN\n", seq, index, now, n);
        }
        int sock_error = 0;
        if (picoquic_send_through_server_sockets(&t->sockets, (struct sockaddr *)&peer, (struct sockaddr *)&local,
            index, (const char *)packet, (int)(n + prefix), &sock_error) != (int)(n + prefix))
            return fail_transport(t, "UDP send failed");
    }
    return 0;
}

static int numeric(const char *s, uint32_t *out)
{
    if (!s || !*s || *s == '-') return -1;
    errno = 0; char *end; unsigned long v = strtoul(s, &end, 10);
    if (errno || *end || v > UINT32_MAX) return -1;
    *out = (uint32_t)v; return 0;
}
static int options(int argc, char **argv, int first, transport_t *t)
{
    for (int i = first; i < argc; ++i) {
        const char *key = argv[i]; if (i + 1 >= argc) return -1; const char *v = argv[++i];
        if (!strcmp(key, "--output")) t->cfg.output_dir = v;
        else if (!strcmp(key, "--control-socket") && t->sender) t->cfg.control_socket = v;
        else if (!strcmp(key, "--pattern") && t->sender) t->cfg.pattern = v;
        else if (!strcmp(key, "--scheduler") && t->sender) t->cfg.scheduler = v;
        else {
            uint32_t n; if (numeric(v, &n)) return -1;
            if (!strcmp(key, "--preview") && !t->sender && n <= 1) t->cfg.preview = (int)n;
            else if (!t->sender) return -1;
            else if (!strcmp(key, "--width")) t->cfg.width = n;
            else if (!strcmp(key, "--height")) t->cfg.height = n;
            else if (!strcmp(key, "--fps")) t->cfg.fps = n;
            else if (!strcmp(key, "--frames")) t->cfg.frames = n;
            else if (!strcmp(key, "--bitrate")) t->cfg.bitrate_kbps = n;
            else if (!strcmp(key, "--gop")) t->cfg.gop = n;
            else if (!strcmp(key, "--playout-ms")) t->cfg.playout_ms = n;
            else if (!strcmp(key, "--decode-margin-ms")) t->cfg.decode_margin_ms = n;
            else if (!strcmp(key, "--preempt") && n <= 1) t->cfg.preempt = (int)n;
            else if (!strcmp(key, "--all-bp") && n <= 1) t->cfg.all_bp = (int)n;
            else return -1;
        }
    }
    return !media_config_valid(&t->cfg) ||
        (strcmp(t->cfg.pattern, "ball") && strcmp(t->cfg.pattern, "zone-plate") && strcmp(t->cfg.pattern, "smpte")) ||
        (strcmp(t->cfg.scheduler, "default") && strcmp(t->cfg.scheduler, "rr") && strcmp(t->cfg.scheduler, "minrtt")) ? -1 : 0;
}
static int save_config(const media_config_t *c)
{
    char path[4096]; if (snprintf(path, sizeof(path), "%s/media_config.json", c->output_dir) >= (int)sizeof(path)) return -1;
    FILE *fp = fopen(path, "w"); if (!fp) return -1;
    int ret = fprintf(fp, "{\"width\":%u,\"height\":%u,\"fps\":%u,\"frames\":%u,\"bitrate_kbps\":%u,\"gop\":%u,"
        "\"playout_ms\":%u,\"decode_margin_ms\":%u,\"preempt\":%d,\"all_bp\":%d,\"codec\":\"h264\",\"pixel_format\":\"yuv420p\","
        "\"colorimetry\":\"bt709\",\"bframes\":0,\"pattern\":\"%s\",\"scheduler\":\"%s\",\"cc\":\"newreno\"}\n",
        c->width, c->height, c->fps, c->frames, c->bitrate_kbps, c->gop, c->playout_ms,
        c->decode_margin_ms, c->preempt, c->all_bp, c->pattern, c->scheduler);
    int close_result = fclose(fp); return ret < 0 || close_result ? -1 : 0;
}

int media_transport_main(int argc, char **argv, int sender)
{
    setbuf(stdout, NULL); signal(SIGINT, stop_handler); signal(SIGTERM, stop_handler);
    transport_t t = {0}; t.sender = sender;
    t.cfg = (media_config_t){1920, 1080, 30, 300, 4000, 30, 200, 20, 1, 0, 0,
        "ball", "default", sender ? "media_sender" : "media_receiver", "/tmp/picoquic-video.sock"};
    if (argc < (sender ? 3 : 4) || options(argc, argv, sender ? 3 : 4, &t)) {
        fprintf(stderr, sender ? "Usage: %s <server_ip> <port> [--width N --height N --fps N --frames N --bitrate kbps --gop N --preempt 0|1 --all-bp 0|1 --scheduler default|rr|minrtt --playout-ms N --decode-margin-ms N --pattern ball|zone-plate|smpte --output DIR --control-socket PATH]\n" :
            "Usage: %s <port> <cert.pem> <key.pem> [--output DIR --preview 0|1]\n", argv[0]); return 1;
    }
    uint32_t port; if (numeric(argv[sender ? 2 : 1], &port) || port < 1 || port > 65535) return 1;
    /* Output must be a fresh directory: never overwrite the previous experiment. */
    if (mkdir(t.cfg.output_dir, 0755)) { perror("Create fresh media output directory"); return 1; }
    struct statvfs disk;
    if (sender && !statvfs(t.cfg.output_dir, &disk)) {
        uint64_t need = (uint64_t)t.cfg.width * t.cfg.height * 3 / 2 * t.cfg.frames;
        if ((uint64_t)disk.f_bavail * disk.f_frsize < need + 64 * 1024 * 1024) {
            fail_transport(&t, "insufficient free space for raw frames"); return 1;
        }
    }
    picoquic_quic_t *quic = NULL; int sockets_open = 0; uint64_t now = picoquic_current_time();
    t.timeout_us = now + 30000000;
    if (sender) {
        if (save_config(&t.cfg)) { t.failed = 1; goto cleanup; }
        t.media = gst_media_source_open(&t.cfg); if (!t.media) { t.failed = 1; goto cleanup; }
        t.control = media_control_open(t.media, t.cfg.control_socket); if (!t.control) { fail_transport(&t, "local bitrate control startup"); goto cleanup; }
    }
    quic = picoquic_create(sender ? 1 : 8, sender ? NULL : argv[2], sender ? NULL : argv[3], NULL,
        MEDIA_ALPN, transport_callback, &t, NULL, NULL, NULL, now, NULL, NULL, NULL, 0);
    if (!quic) { t.failed = 1; goto cleanup; }
    picoquic_set_default_multipath_option(quic, 1); picoquic_enable_path_callbacks_default(quic, 1);
    picoquic_set_default_tp_value(quic, picoquic_tp_active_connection_id_limit, 8);
    picoquic_set_default_address_discovery_mode(quic, 3);
    const char *qlog = sender ? "./qlog_sender" : "./qlog_receiver";
    if (mkdir(qlog, 0755) && errno != EEXIST) { t.failed = 1; goto cleanup; }
    picoquic_set_qlog(quic, qlog); picoquic_set_log_level(quic, 1);
    if (picoquic_open_server_sockets(&t.sockets, sender ? 0 : (int)port)) { t.failed = 1; goto cleanup; }
    sockets_open = 1;
    if (sender) {
        int is_name = 0;
        if (picoquic_get_server_address(argv[1], (int)port, &t.server, &is_name)) { t.failed = 1; goto cleanup; }
        picoquic_set_null_verifier(quic); /* Same experimental certificate behavior as legacy sender. */
        t.cnx = picoquic_create_cnx(quic, picoquic_null_connection_id, picoquic_null_connection_id,
            (struct sockaddr *)&t.server, now, 0, "localhost", MEDIA_ALPN, 1);
        if (!t.cnx) { t.failed = 1; goto cleanup; }
        picoquic_set_callback(t.cnx, transport_callback, &t); picoquic_set_frame_preemption(t.cnx, t.cfg.preempt);
        picoquic_multipath_scheduler_enum scheduler = !strcmp(t.cfg.scheduler, "rr") ? picoquic_multipath_scheduler_rr :
            (!strcmp(t.cfg.scheduler, "minrtt") ? picoquic_multipath_scheduler_minrtt : picoquic_multipath_scheduler_default);
        picoquic_set_multipath_scheduler(t.cnx, scheduler);
        printf("[PREEMPT] enabled=%d\n[SCHED] multipath scheduler=%s\n[GOP] codec_gop=%u all_bp=%d\n", t.cfg.preempt, t.cfg.scheduler, t.cfg.gop, t.cfg.all_bp);
        if (picoquic_start_client_cnx(t.cnx)) { t.failed = 1; goto cleanup; }
    }
    printf("[MEDIA_MODE] sender=%d alpn=%s\n", sender, MEDIA_ALPN);
    while (!t.failed && !t.disconnected && !interrupted) {
        now = picoquic_current_time();
        if (t.t0) t.timeout_us = t.t0 + (uint64_t)t.cfg.frames * 1000000 / t.cfg.fps + 120000000;
        if (now > t.timeout_us || (t.closing && now > t.close_us + 10000000)) { fail_transport(&t, "session/close timeout"); break; }
        probe_paths(&t, now);
        if (sender && t.t0 && !t.source_started && now >= t.t0) {
            if (!t.start_acked || gst_media_start(t.media)) { fail_transport(&t, "media source start/ACK failed"); break; }
            t.source_started = 1;
        }
        if (sender && t.source_started && !t.source_finished && send_one_frame(&t, now)) break;
        if (t.media) {
            uint64_t produced, encoded; int status = gst_media_status(t.media, &produced, &encoded);
            if (status < 0) { fail_transport(&t, "media worker/pipeline failure"); break; }
            if (sender && status == 1 && !t.source_finished) {
                media_control_close(t.control); t.control = NULL;
                if (gst_media_finish_source(t.media)) { fail_transport(&t, "source reference/archive incomplete"); break; }
                t.source_finished = 1; t.source_end_us = now;
                media_control_record_t end = {0}; end.type = MEDIA_END; end.count = produced;
                if (send_control(&t, &end)) break;
                printf("[SOURCE_FINISHED] generated=%" PRIu64 " encoded=%" PRIu64 "\n", produced, encoded);
            }
        }
        if (!sender && t.playout && !t.done_sent) {
            int status = media_playout_tick(t.playout, now);
            if (status < 0) { fail_transport(&t, "playout failure"); break; }
            if (status == 1 && t.end_seen) {
                if (media_playout_finish(t.playout)) { fail_transport(&t, "playout output incomplete"); break; }
                media_control_record_t done = {0}; done.type = MEDIA_PLAYOUT_DONE; done.count = t.cfg.frames;
                if (send_control(&t, &done)) break;
                t.done_sent = 1;
            }
        }
        if (sender && t.source_finished && t.peer_done && !t.closing && queue_empty(t.cnx)) {
            printf("[DRAIN] QUIC send queue drained; peer playout completed\n");
            picoquic_close(t.cnx, 0); t.closing = 1; t.close_us = now;
        }
        if (network_tick(&t, quic, now)) break;
        if (!t.disconnected && t.cnx && picoquic_get_cnx_state(t.cnx) == picoquic_state_disconnected) t.disconnected = 1;
        int64_t delay = picoquic_get_next_wake_delay(quic, picoquic_current_time(), 10000);
        if (!sender && t.playout && delay > 2000) delay = 2000;
        if (sender && t.t0 && !t.source_started && now < t.t0 && (uint64_t)delay > t.t0 - now) delay = (int64_t)(t.t0 - now);
        if (wait_io(&t, delay)) { fail_transport(&t, "socket/media wait failed"); break; }
    }
    if (interrupted || !(sender ? t.peer_done : t.done_sent)) t.failed = 1;
cleanup:
    media_control_close(t.control);
    if (media_playout_close(t.playout)) t.failed = 1;
    if (gst_media_close(t.media)) t.failed = 1;
    while (t.streams) { rx_stream_t *s = t.streams; t.streams = s->next; free(s->bytes); free(s); }
    if (sockets_open) picoquic_close_server_sockets(&t.sockets);
    if (quic) picoquic_free(quic);
    printf("[MEDIA_EXIT] sender=%d status=%s received=%" PRIu64 "\n", sender, t.failed ? "failed" : "completed", t.received_frames);
    return t.failed ? 1 : 0;
}
