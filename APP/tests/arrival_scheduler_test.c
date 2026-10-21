/* Remote-only regression checks of real picoquic planning and STREAM framing.
 * No sockets, Mininet or media processes. Compile against the pinned core.
 */
#ifndef _WINDOWS
/* picoquic_utils.h uses POSIX thread types without including pthread.h. */
#include <pthread.h>
#endif
#include "picoquic_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NOW 1000000ull
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #x); result = 1; goto cleanup; } } while (0)

typedef struct {
    picoquic_quic_t* quic;
    picoquic_cnx_t* cnx;
    uint64_t time;
} fixture_t;

static int setup(fixture_t* f)
{
    struct sockaddr_in addr;
    memset(f, 0, sizeof(*f));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(4433);
    addr.sin_addr.s_addr = htonl(0x7f000001);
    f->time = NOW;
    f->quic = picoquic_create(1, NULL, NULL, NULL, "arrival-test", NULL, NULL,
        NULL, NULL, NULL, NOW, &f->time, NULL, NULL, 0);
    if (f->quic == NULL) return -1;
    picoquic_set_default_congestion_algorithm(f->quic, NULL);
    f->cnx = picoquic_create_cnx(f->quic, picoquic_null_connection_id,
        picoquic_null_connection_id, (struct sockaddr*)&addr, NOW, 0, "localhost", "arrival-test", 1);
    if (f->cnx == NULL) return -1;
    f->cnx->is_multipath_enabled = 1;
    if (picoquic_create_path(f->cnx, NOW, NULL, (struct sockaddr*)&addr, 0, 1) != 1) return -1;
    f->cnx->cnx_state = picoquic_state_ready;
    f->cnx->maxdata_remote = 10000000;
    f->cnx->max_stream_id_unidir_remote = 102;
    for (int i = 0; i < 2; i++) {
        picoquic_path_t* p = f->cnx->path[i];
        p->first_tuple->challenge_verified = 1;
        p->first_tuple->challenge_required = 0;
        p->cwin = 1000000;
        p->bytes_in_transit = 0;
        p->smoothed_rtt = p->rtt_min = 20000;
        p->bandwidth_estimate = 1000000;
        p->send_mtu = 1200;
        picoquic_pacing_init(&p->pacing, NOW);
    }
    picoquic_set_multipath_scheduler(f->cnx, picoquic_multipath_scheduler_arrival);
    return 0;
}

static picoquic_stream_head_t* au(fixture_t* f, uint64_t id, size_t size)
{
    uint8_t* data = (uint8_t*)calloc(size, 1);
    if (data == NULL) return NULL;
    int ret = picoquic_add_to_stream(f->cnx, id, data, size, 1);
    free(data);
    picoquic_stream_head_t* s = ret == 0 ? picoquic_find_stream(f->cnx, id) : NULL;
    if (s != NULL) s->maxdata_remote = 10000000;
    return s;
}

static picoquic_path_t* plan(fixture_t* f, uint64_t* wake)
{
    picoquic_path_t* path = NULL;
    picoquic_tuple_t* tuple = NULL;
    *wake = UINT64_MAX;
    picoquic_select_next_path_tuple(f->cnx, NOW, wake, &path, &tuple);
    return path;
}

static int split_and_queue(void)
{
    fixture_t f;
    int result = 0;
    uint64_t wake;
    CHECK(setup(&f) == 0);
    picoquic_stream_head_t* a = au(&f, 2, 20000);
    CHECK(a != NULL);
    f.cnx->path[0]->bandwidth_estimate = 3000000;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] + a->arrival_budget[1] == 20000);
    CHECK(a->arrival_budget[0] >= 13896 && a->arrival_budget[0] <= 16104);

    /* Existing assigned backlog belongs only to path 0, so the second AU
     * takes path 1. The shared connection credit is used only once. */
    f.cnx->path[0]->bandwidth_estimate = 1000000;
    a->affinity_path = f.cnx->path[0];
    picoquic_stream_head_t* b = au(&f, 6, 20000);
    CHECK(b != NULL);
    f.cnx->maxdata_remote = 30000;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] == 20000 && a->arrival_budget[1] == 0);
    CHECK(b->arrival_budget[0] == 0 && b->arrival_budget[1] == 10000);
cleanup:
    if (f.quic != NULL) picoquic_free(f.quic);
    return result;
}

static int windows_and_waiting(void)
{
    fixture_t f;
    int result = 0;
    uint64_t wake;
    CHECK(setup(&f) == 0);
    picoquic_stream_head_t* a = au(&f, 2, 10000);
    CHECK(a != NULL);
    f.cnx->path[0]->cwin = 1200;
    f.cnx->path[1]->cwin = 1200;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] + a->arrival_budget[1] == 10000);
    CHECK(a->arrival_budget[0] > 1200 && a->arrival_budget[1] > 1200);

    f.cnx->path[0]->cwin = 100000;
    f.cnx->path[0]->bytes_in_transit = 100000;
    f.cnx->path[1]->cwin = 100000;
    f.cnx->path[1]->bandwidth_estimate = 1000;
    f.cnx->path[1]->smoothed_rtt = 200000;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] == 10000 && a->arrival_budget[1] == 0);
    CHECK(picoquic_find_ready_stream_path(f.cnx, f.cnx->path[0], 0) == NULL);
    CHECK(picoquic_find_ready_stream_path(f.cnx, f.cnx->path[1], 0) == NULL);
    CHECK(wake > NOW && wake < UINT64_MAX);

    /* Both blocked: no new payload can escape through the fallback path. */
    f.cnx->path[1]->bytes_in_transit = 100000;
    plan(&f, &wake);
    CHECK(picoquic_find_ready_stream_path(f.cnx, f.cnx->path[0], 0) == NULL);
    CHECK(picoquic_find_ready_stream_path(f.cnx, f.cnx->path[1], 0) == NULL);
    CHECK(wake > NOW && wake < UINT64_MAX);

    /* Waiting for a slow full window loses to an immediately usable path. */
    f.cnx->path[0]->smoothed_rtt = 200000;
    f.cnx->path[1]->smoothed_rtt = 20000;
    f.cnx->path[1]->bytes_in_transit = 0;
    f.cnx->path[1]->bandwidth_estimate = 1000000;
    CHECK(plan(&f, &wake) == f.cnx->path[1]);
    CHECK(a->arrival_budget[0] == 0 && a->arrival_budget[1] == 10000);
cleanup:
    if (f.quic != NULL) picoquic_free(f.quic);
    return result;
}

static int framing_and_replan(void)
{
    fixture_t f;
    int result = 0, more = 0, pure_ack = 1, failed = 0, ret = 0;
    uint64_t wake;
    uint8_t buffer[2048];
    CHECK(setup(&f) == 0);
    picoquic_stream_head_t* a = au(&f, 2, 10000);
    CHECK(a != NULL);
    plan(&f, &wake);
    uint64_t budget = a->arrival_budget[0];
    picoquic_format_available_stream_frames(f.cnx, f.cnx->path[0], buffer, buffer + 1,
        UINT64_MAX, &more, &pure_ack, &failed, &ret);
    CHECK(ret == 0 && f.cnx->data_sent == 0 && a->arrival_budget[0] == budget);
    a->arrival_budget[0] = 100;
    picoquic_format_available_stream_frames(f.cnx, f.cnx->path[0], buffer, buffer + sizeof(buffer),
        UINT64_MAX, &more, &pure_ack, &failed, &ret);
    CHECK(ret == 0 && f.cnx->data_sent == 100 && a->arrival_budget[0] == 0);
    CHECK(a->sent_offset == 100);
    CHECK(picoquic_find_ready_stream_path(f.cnx, f.cnx->path[0], 0) == NULL);
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] + a->arrival_budget[1] == 9900);

    /* Lower RTT alone wins a small AU without forcing both paths to carry it. */
    f.cnx->path[1]->smoothed_rtt = 200000;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] == 9900 && a->arrival_budget[1] == 0);

    /* Path array reordering must not alias persistent path IDs. */
    picoquic_path_t* tmp = f.cnx->path[0];
    f.cnx->path[0] = f.cnx->path[1];
    f.cnx->path[1] = tmp;
    CHECK(picoquic_arrival_stream_budget(f.cnx, a, tmp) == 9900);
    CHECK(picoquic_arrival_stream_budget(f.cnx, a, f.cnx->path[0]) == 0);
    picoquic_set_multipath_scheduler(f.cnx, picoquic_multipath_scheduler_default);
    CHECK(!f.cnx->arrival_plan_active);
    CHECK(picoquic_arrival_stream_budget(f.cnx, a, tmp) == UINT64_MAX);
cleanup:
    if (f.quic != NULL) picoquic_free(f.quic);
    return result;
}

static int recovery_and_pacing(void)
{
    fixture_t f;
    picoquic_packet_t pending;
    int result = 0;
    uint64_t wake;
    memset(&pending, 0, sizeof(pending));
    CHECK(setup(&f) == 0);
    picoquic_stream_head_t* a = au(&f, 2, 20000);
    CHECK(a != NULL);
    /* A real STREAM frame in a pending packet, independently of send_queue. */
    const uint8_t frame[] = { 0x0a, 2, 1, 0 };
    memcpy(pending.bytes, frame, sizeof(frame));
    pending.length = sizeof(frame);
    pending.send_time = NOW - 1000;
    f.cnx->path[0]->pkt_ctx.pending_first = &pending;
    f.cnx->path[0]->pkt_ctx.pending_last = &pending;
    plan(&f, &wake);
    CHECK(a->arrival_tail_us == 9000);
    CHECK(a->arrival_budget[0] + a->arrival_budget[1] == 20000);
    f.cnx->path[0]->pkt_ctx.pending_first = NULL;
    f.cnx->path[0]->pkt_ctx.pending_last = NULL;

    f.cnx->queue_data_repeat_tree.root = &pending.queue_data_repeat_node;
    plan(&f, &wake);
    CHECK(f.cnx->arrival_plan_active);
    CHECK(picoquic_arrival_stream_budget(f.cnx, a, f.cnx->path[0]) == 0);
    CHECK(picoquic_arrival_stream_budget(f.cnx, a, f.cnx->path[1]) == 0);
    CHECK(picoquic_set_stream_priority(f.cnx, 2, 0) == 0);
    pending.data_repeat_priority = 9;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] + a->arrival_budget[1] == 20000);
    f.cnx->queue_data_repeat_tree.root = NULL;

    /* Same AU becomes eligible after recovery; a paced path cannot send now. */
    f.cnx->path[0]->pacing.bucket_nanosec = 0;
    f.cnx->path[0]->pacing.packet_time_nanosec = 1000000;
    plan(&f, &wake);
    CHECK(a->arrival_budget[0] + a->arrival_budget[1] == 20000);
    CHECK(wake > NOW && wake < UINT64_MAX);
    CHECK(plan(&f, &wake) == f.cnx->path[1]);
cleanup:
    if (f.cnx != NULL) {
        f.cnx->path[0]->pkt_ctx.pending_first = NULL;
        f.cnx->path[0]->pkt_ctx.pending_last = NULL;
        f.cnx->queue_data_repeat_tree.root = NULL;
    }
    if (f.quic != NULL) picoquic_free(f.quic);
    return result;
}

int main(void)
{
    int result = split_and_queue() | windows_and_waiting() | framing_and_replan() | recovery_and_pacing();
    if (result == 0) puts("arrival scheduler checks passed");
    return result;
}
