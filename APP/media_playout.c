#define _GNU_SOURCE
#include "media_playout.h"
#include <gst/gst.h>
#include <gst/app/gstappsrc.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <inttypes.h>
#include <time.h>

#define PLAY_QUEUE 8
#define REORDER_BYTES (32u * 1024u * 1024u)
#define REORDER_FRAMES 64
#define RAW_CACHE_FRAMES 24
typedef struct {
    uint8_t *pixels;
    uint64_t slot, pts_ns, deadline, decision_us, source_id, decoded_us, source_us;
    int new_frame;
} play_item_t;
struct media_playout {
    media_config_t cfg;
    gst_media_t *decoder;
    uint64_t t0, next_decode, next_play, last_id, last_source_us, last_decoded_us;
    media_frame_t *frames;
    media_raw_t *raws;
    uint8_t *received;
    uint8_t *decode_state;
    uint64_t *source_times, *receive_times;
    uint8_t *last_pixels;
    size_t pixels_len, pending_bytes, pending_count, raw_count;
    int waiting_idr, have_last, failed, stop, worker_started, finished;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    pthread_t worker;
    play_item_t queue[PLAY_QUEUE];
    size_t head, count;
    uint64_t written;
    FILE *pixels, *timeline, *states;
    GstElement *preview, *preview_src;
};
static uint64_t mono_us(void)
{ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000; }
static void play_fail(media_playout_t *p, const char *why)
{
    pthread_mutex_lock(&p->lock); p->failed = 1; pthread_cond_broadcast(&p->cond); pthread_mutex_unlock(&p->lock);
    fprintf(stderr, "[MEDIA_ERROR] %s\n", why);
}
static uint64_t deadline(media_playout_t *p, uint64_t id)
{ return p->t0 + id * 1000000 / p->cfg.fps + (uint64_t)p->cfg.playout_ms * 1000; }

static void *play_writer(void *opaque)
{
    media_playout_t *p = opaque;
    for (;;) {
        pthread_mutex_lock(&p->lock);
        while (!p->count && !p->stop && !p->failed) pthread_cond_wait(&p->cond, &p->lock);
        if ((!p->count && p->stop) || p->failed) { pthread_mutex_unlock(&p->lock); break; }
        play_item_t item = p->queue[p->head]; memset(&p->queue[p->head], 0, sizeof(item));
        p->head = (p->head + 1) % PLAY_QUEUE; --p->count; pthread_mutex_unlock(&p->lock);
        const char *status = item.new_frame ? "new" : (item.source_id == UINT64_MAX ? "black" : "repeat");
        int bad = item.slot != p->written || fwrite(item.pixels, 1, p->pixels_len, p->pixels) != p->pixels_len;
        if (!bad && p->preview_src) {
            GstBuffer *b = gst_buffer_new_allocate(NULL, p->pixels_len, NULL);
            if (!b) bad = 1;
            else {
                gst_buffer_fill(b, 0, item.pixels, p->pixels_len);
                GST_BUFFER_PTS(b) = item.pts_ns; GST_BUFFER_DURATION(b) = GST_SECOND / p->cfg.fps;
                bad = gst_app_src_push_buffer(GST_APP_SRC(p->preview_src), b) != GST_FLOW_OK;
            }
        }
        uint64_t output_us = mono_us();
        if (!bad) bad = fprintf(p->timeline, "%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%s,%" PRIu64 ",%" PRIu64 "\n",
            item.slot, item.pts_ns, item.deadline, item.decision_us, item.source_id,
            item.source_us, item.decoded_us, status, item.decision_us > item.deadline ? item.decision_us - item.deadline : 0,
            output_us) < 0;
        free(item.pixels);
        if (bad) { play_fail(p, "playout write/preview failed"); break; }
        ++p->written;
    }
    if (fflush(p->pixels) || fflush(p->timeline)) play_fail(p, "playout flush failed");
    return NULL;
}

media_playout_t *media_playout_open(const media_config_t *cfg, gst_media_t *decoder, uint64_t t0)
{
    media_playout_t *p = calloc(1, sizeof(*p)); if (!p) return NULL;
    p->cfg = *cfg; p->decoder = decoder; p->t0 = t0; p->waiting_idr = 1;
    p->pixels_len = (size_t)cfg->width * cfg->height * 3 / 2;
    pthread_mutex_init(&p->lock, NULL); pthread_cond_init(&p->cond, NULL);
    p->frames = calloc(cfg->frames, sizeof(*p->frames)); p->raws = calloc(cfg->frames, sizeof(*p->raws));
    p->received = calloc(cfg->frames, 1); p->decode_state = calloc(cfg->frames, 1);
    p->source_times = calloc(cfg->frames, sizeof(uint64_t)); p->receive_times = calloc(cfg->frames, sizeof(uint64_t));
    p->last_pixels = malloc(p->pixels_len);
    if (!p->frames || !p->raws || !p->received || !p->decode_state || !p->source_times || !p->receive_times || !p->last_pixels) goto bad;
    /* Limited-range BT.709 I420 black: Y=16, U=V=128. */
    memset(p->last_pixels, 16, (size_t)cfg->width * cfg->height);
    memset(p->last_pixels + (size_t)cfg->width * cfg->height, 128, (size_t)cfg->width * cfg->height / 2);
    gchar *path = g_build_filename(cfg->output_dir, "playout.yuv", NULL); p->pixels = fopen(path, "wb"); g_free(path);
    path = g_build_filename(cfg->output_dir, "playout_frames.csv", NULL); p->timeline = fopen(path, "w"); g_free(path);
    path = g_build_filename(cfg->output_dir, "receive_frames.csv", NULL); p->states = fopen(path, "w"); g_free(path);
    if (!p->pixels || !p->timeline || !p->states) goto bad;
    fprintf(p->timeline, "slot,pts_ns,deadline_us,decision_us,displayed_source_frame_id,source_mono_us,decoded_us,status,tick_lag_us,output_us\n");
    fprintf(p->states, "frame_id,transport,decode,received_us\n");
    if (cfg->preview) {
        GError *e = NULL;
        p->preview = gst_parse_launch("appsrc name=preview is-live=true format=time block=true max-bytes=12582912 ! videoconvert ! autovideosink sync=false", &e);
        if (e) { fprintf(stderr, "[MEDIA_ERROR] preview: %s\n", e->message); g_error_free(e); goto bad; }
        if (!p->preview) goto bad;
        p->preview_src = gst_bin_get_by_name(GST_BIN(p->preview), "preview");
        GstCaps *caps = gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, "I420",
            "width", G_TYPE_INT, cfg->width, "height", G_TYPE_INT, cfg->height,
            "framerate", GST_TYPE_FRACTION, cfg->fps, 1, "colorimetry", G_TYPE_STRING, "bt709", NULL);
        gst_app_src_set_caps(GST_APP_SRC(p->preview_src), caps); gst_caps_unref(caps);
        if (gst_element_set_state(p->preview, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) goto bad;
    }
    if (pthread_create(&p->worker, NULL, play_writer, p)) goto bad;
    p->worker_started = 1; return p;
bad:
    media_playout_close(p); return NULL;
}

int media_playout_receive(media_playout_t *p, media_frame_t *f)
{
    uint64_t id = f->frame_id;
    uint64_t expected_pts = id * UINT64_C(1000000000) / p->cfg.fps;
    uint64_t diff = expected_pts > f->pts_ns ? expected_pts - f->pts_ns : f->pts_ns - expected_pts;
    if (id >= p->cfg.frames || p->received[id] || diff > 1000 || f->dts_ns != f->pts_ns ||
        f->source_mono_us > f->encoded_ready_mono_us) return -1;
    p->received[id] = 1;
    p->source_times[id] = f->source_mono_us; p->receive_times[id] = mono_us();
    printf("[MEDIA_RECEIVED] frame=%" PRIu64 " au_bytes=%zu pts_ns=%" PRIu64 " recv_us=%" PRIu64 "\n", id, f->au_len, f->pts_ns, mono_us());
    if (id < p->next_decode) { media_frame_release(f); return 0; }
    if (p->pending_count >= REORDER_FRAMES || p->pending_bytes + f->au_len > REORDER_BYTES) {
        play_fail(p, "encoded reorder queue overflow"); return -1;
    }
    p->pending_bytes += f->au_len; ++p->pending_count; p->frames[id] = *f; memset(f, 0, sizeof(*f)); return 0;
}

int media_playout_tick(media_playout_t *p, uint64_t now)
{
    pthread_mutex_lock(&p->lock); int failed = p->failed; pthread_mutex_unlock(&p->lock);
    if (failed) return -1;
    media_raw_t raw;
    while (gst_media_pop_raw(p->decoder, &raw)) {
        uint64_t id = raw.frame_id;
        if (id < p->cfg.frames) p->decode_state[id] = 3;
        if (id < p->next_play) { media_raw_release(&raw); continue; }
        if (id >= p->cfg.frames || p->raws[id].pixels || p->raw_count >= RAW_CACHE_FRAMES) {
            media_raw_release(&raw); play_fail(p, "decoded frame order/cache limit"); return -1;
        }
        p->raws[id] = raw; ++p->raw_count;
    }
    while (p->next_decode < p->cfg.frames) {
        uint64_t id = p->next_decode;
        uint64_t cutoff = deadline(p, id) - (uint64_t)p->cfg.decode_margin_ms * 1000;
        media_frame_t *f = &p->frames[id];
        if (!f->au) {
            if (now < cutoff) break;
            p->waiting_idr = 1; p->decode_state[id] = 2; ++p->next_decode; continue;
        }
        int idr = !!(f->flags & MEDIA_FLAG_IDR);
        if (now >= cutoff || (p->waiting_idr && !idr)) {
            p->decode_state[id] = now >= cutoff ? 5 : 4;
            p->pending_bytes -= f->au_len; --p->pending_count; media_frame_release(f);
            p->waiting_idr = 1; ++p->next_decode; continue;
        }
        size_t size = f->au_len;
        if (gst_media_submit(p->decoder, f, p->waiting_idr)) { play_fail(p, "decoder feeder overflow/failure"); return -1; }
        p->pending_bytes -= size; --p->pending_count;
        p->waiting_idr = 0; p->decode_state[id] = 1; ++p->next_decode;
    }
    if (p->next_decode == p->cfg.frames) gst_media_end_input(p->decoder);
    while (p->next_play < p->cfg.frames && now >= deadline(p, p->next_play)) {
        uint64_t id = p->next_play;
        media_raw_t *r = &p->raws[id];
        int new_frame = r->pixels && r->ready_us <= deadline(p, id);
        if (new_frame) {
            memcpy(p->last_pixels, r->pixels, p->pixels_len);
            p->last_id = id; p->last_source_us = p->source_times[id]; p->last_decoded_us = r->ready_us; p->have_last = 1;
        }
        if (r->pixels) { media_raw_release(r); --p->raw_count; }
        play_item_t item = {0}; item.pixels = malloc(p->pixels_len);
        if (!item.pixels) return -1;
        memcpy(item.pixels, p->last_pixels, p->pixels_len);
        item.slot = id; item.pts_ns = id * UINT64_C(1000000000) / p->cfg.fps;
        item.deadline = deadline(p, id); item.decision_us = now;
        item.source_id = p->have_last ? p->last_id : UINT64_MAX;
        item.source_us = p->last_source_us; item.decoded_us = p->last_decoded_us; item.new_frame = new_frame;
        pthread_mutex_lock(&p->lock);
        if (p->count == PLAY_QUEUE || p->failed) {
            pthread_mutex_unlock(&p->lock); free(item.pixels); play_fail(p, "playout I/O queue overflow/failure"); return -1;
        }
        p->queue[(p->head + p->count++) % PLAY_QUEUE] = item;
        pthread_cond_signal(&p->cond); pthread_mutex_unlock(&p->lock);
        ++p->next_play;
    }
    return p->next_play == p->cfg.frames ? 1 : 0;
}

int media_playout_finish(media_playout_t *p)
{
    if (p->finished) return p->failed ? -1 : 0;
    pthread_mutex_lock(&p->lock); p->stop = 1; pthread_cond_broadcast(&p->cond); pthread_mutex_unlock(&p->lock);
    if (p->worker_started) { pthread_join(p->worker, NULL); p->worker_started = 0; }
    if (p->written != p->cfg.frames) p->failed = 1;
    /* Transport results are finalized at connection shutdown; late complete AUs
       must not remain incorrectly classified as unreceived in this table. */
    p->finished = 1;
    printf("[PLAYOUT_FINISHED] frames=%" PRIu64 " valid=%d\n", p->written, !p->failed);
    return p->failed ? -1 : 0;
}
int media_playout_close(media_playout_t *p)
{
    if (!p) return 0;
    pthread_mutex_lock(&p->lock); p->stop = 1; pthread_cond_broadcast(&p->cond); pthread_mutex_unlock(&p->lock);
    if (p->preview) gst_element_set_state(p->preview, GST_STATE_NULL);
    if (p->worker_started) pthread_join(p->worker, NULL);
    if (p->states && p->received && p->decode_state && p->receive_times) {
        for (uint32_t i = 0; i < p->cfg.frames; ++i)
            if (fprintf(p->states, "%u,%s,%s,%" PRIu64 "\n", i,
                p->received[i] ? "complete" : "unreceived_unknown_origin",
                p->decode_state[i] == 3 ? "decoded" : (p->decode_state[i] == 1 ? "submitted" :
                (p->decode_state[i] == 4 ? "wait_idr" : (p->decode_state[i] == 5 ? "late_au" : "unreceived_deadline"))),
                p->receive_times[i]) < 0) p->failed = 1;
        if (fflush(p->states)) p->failed = 1;
    }
    for (uint32_t i = 0; i < p->cfg.frames; ++i) {
        if (p->frames) media_frame_release(&p->frames[i]);
        if (p->raws) media_raw_release(&p->raws[i]);
    }
    for (int i = 0; i < PLAY_QUEUE; ++i) free(p->queue[i].pixels);
    if (p->pixels && fclose(p->pixels)) p->failed = 1;
    if (p->timeline && fclose(p->timeline)) p->failed = 1;
    if (p->states && fclose(p->states)) p->failed = 1;
    if (p->preview_src) gst_object_unref(p->preview_src);
    if (p->preview) gst_object_unref(p->preview);
    free(p->frames); free(p->raws); free(p->received); free(p->decode_state); free(p->last_pixels);
    free(p->source_times); free(p->receive_times);
    int failed = p->failed;
    pthread_cond_destroy(&p->cond); pthread_mutex_destroy(&p->lock); free(p);
    return failed ? -1 : 0;
}
