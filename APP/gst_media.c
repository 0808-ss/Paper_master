#define _GNU_SOURCE
#include "gst_media.h"
#include <gst/gst.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/video/video.h>
#include <sys/eventfd.h>
#include <unistd.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <errno.h>
#include <string.h>
#include <time.h>

#define MQ_CAP 16
#define MQ_BYTES (32u * 1024u * 1024u)
typedef struct { media_frame_t frame; int reset; } input_item_t;
struct gst_media {
    media_config_t cfg;
    GstElement *pipeline, *encoder, *appsrc;
    GstBus *bus;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    pthread_t worker;
    int worker_started, source, stop, failed, eos, wake, input_eos;
    uint32_t epoch;
    uint64_t produced, encoded, first_rt;
    int have_origin;
    GstSegment source_segment;
    uint64_t *source_times, *source_pts;
    uint8_t *codec_prefix;
    size_t codec_prefix_len;
    media_frame_t aus[MQ_CAP];
    media_frame_t archives[MQ_CAP];
    media_raw_t raws[MQ_CAP];
    GstSample *refs[MQ_CAP];
    input_item_t inputs[MQ_CAP];
    size_t au_head, au_count, au_bytes, raw_head, raw_count;
    size_t ref_head, ref_count, in_head, in_count, in_bytes;
    size_t archive_head, archive_count, archive_bytes;
    uint64_t reference_written, archive_written;
    FILE *raw_file, *raw_index, *au_file, *au_index;
};

static uint64_t mono_us(void)
{ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000; }
static void wake_media(gst_media_t *m)
{ uint64_t one = 1; if (write(m->wake, &one, sizeof(one)) < 0 && errno != EAGAIN) { /* shutdown still observes failed */ } }
static void fail_media(gst_media_t *m, const char *why)
{
    pthread_mutex_lock(&m->lock); m->failed = 1;
    pthread_cond_broadcast(&m->cond); pthread_mutex_unlock(&m->lock);
    fprintf(stderr, "[MEDIA_ERROR] %s\n", why); wake_media(m);
}
static uint64_t sample_rt(GstSample *s)
{
    GstBuffer *b = gst_sample_get_buffer(s);
    const GstSegment *seg = gst_sample_get_segment(s);
    if (!GST_BUFFER_PTS_IS_VALID(b) || !seg || seg->format != GST_FORMAT_TIME) return GST_CLOCK_TIME_NONE;
    return gst_segment_to_running_time(seg, GST_FORMAT_TIME, GST_BUFFER_PTS(b));
}
static int source_id(gst_media_t *m, uint64_t rt, uint64_t *id, uint64_t *pts)
{
    if (!m->have_origin || rt == GST_CLOCK_TIME_NONE || rt < m->first_rt) return -1;
    *pts = rt - m->first_rt;
    if (*pts > (uint64_t)m->cfg.frames * GST_SECOND / m->cfg.fps + GST_SECOND) return -1;
    *id = (*pts * m->cfg.fps + GST_SECOND / 2) / GST_SECOND;
    if (*id >= m->cfg.frames || *id >= m->produced) return -1;
    uint64_t saved = m->source_pts[*id];
    if ((*pts > saved ? *pts - saved : saved - *pts) > 1000) return -1;
    return 0;
}

static GstPadProbeReturn source_probe(GstPad *pad, GstPadProbeInfo *info, gpointer opaque)
{
    (void)pad; gst_media_t *m = opaque;
    if (GST_PAD_PROBE_INFO_TYPE(info) & GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM) {
        GstEvent *event = GST_PAD_PROBE_INFO_EVENT(info);
        if (GST_EVENT_TYPE(event) == GST_EVENT_SEGMENT) {
            const GstSegment *seg; gst_event_parse_segment(event, &seg);
            pthread_mutex_lock(&m->lock); gst_segment_copy_into(seg, &m->source_segment); pthread_mutex_unlock(&m->lock);
        }
    }
    if (GST_PAD_PROBE_INFO_TYPE(info) & GST_PAD_PROBE_TYPE_BUFFER) {
        GstBuffer *b = GST_PAD_PROBE_INFO_BUFFER(info);
        pthread_mutex_lock(&m->lock);
        uint64_t rt = GST_BUFFER_PTS_IS_VALID(b) ?
            gst_segment_to_running_time(&m->source_segment, GST_FORMAT_TIME, GST_BUFFER_PTS(b)) : GST_CLOCK_TIME_NONE;
        if (rt == GST_CLOCK_TIME_NONE || m->produced >= m->cfg.frames) {
            pthread_mutex_unlock(&m->lock); fail_media(m, "invalid source timestamp/count"); return GST_PAD_PROBE_DROP;
        }
        if (!m->have_origin) { m->first_rt = rt; m->have_origin = 1; }
        uint64_t id = m->produced++;
        m->source_times[id] = mono_us(); m->source_pts[id] = rt - m->first_rt;
        pthread_mutex_unlock(&m->lock);
    }
    return GST_PAD_PROBE_OK;
}

int media_write_i420_sample(void *opaque, void *file, uint32_t width, uint32_t height)
{
    GstSample *s = opaque; FILE *fp = file;
    GstVideoInfo vi; GstVideoFrame vf = {0}; gst_video_info_init(&vi);
    if (!gst_video_info_from_caps(&vi, gst_sample_get_caps(s)) ||
        GST_VIDEO_INFO_FORMAT(&vi) != GST_VIDEO_FORMAT_I420 ||
        GST_VIDEO_INFO_WIDTH(&vi) != (int)width || GST_VIDEO_INFO_HEIGHT(&vi) != (int)height ||
        !gst_video_frame_map(&vf, &vi, gst_sample_get_buffer(s), GST_MAP_READ)) return -1;
    int result = 0;
    for (int p = 0; p < 3; ++p) {
        uint32_t w = p ? width / 2 : width, h = p ? height / 2 : height;
        const uint8_t *base = GST_VIDEO_FRAME_PLANE_DATA(&vf, p);
        gint stride = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, p);
        for (uint32_t y = 0; y < h; ++y)
            if (fwrite(base + (ptrdiff_t)y * stride, 1, w, fp) != w) { result = -1; break; }
        if (result) break;
    }
    gst_video_frame_unmap(&vf); return result;
}

static GstFlowReturn reference_sample(GstAppSink *sink, gpointer opaque)
{
    gst_media_t *m = opaque; GstSample *s = gst_app_sink_pull_sample(sink);
    if (!s) return GST_FLOW_EOS;
    pthread_mutex_lock(&m->lock);
    if (m->stop || m->failed || m->ref_count == MQ_CAP) {
        int stopping = m->stop, full = m->ref_count == MQ_CAP;
        pthread_mutex_unlock(&m->lock); gst_sample_unref(s);
        if (full && !stopping) fail_media(m, "reference queue overflow; quality invalid");
        return stopping ? GST_FLOW_FLUSHING : GST_FLOW_ERROR;
    }
    m->refs[(m->ref_head + m->ref_count++) % MQ_CAP] = s;
    pthread_cond_signal(&m->cond); pthread_mutex_unlock(&m->lock); return GST_FLOW_OK;
}

static GstFlowReturn encoded_sample(GstAppSink *sink, gpointer opaque)
{
    gst_media_t *m = opaque; GstSample *s = gst_app_sink_pull_sample(sink);
    if (!s) return GST_FLOW_EOS;
    GstBuffer *b = gst_sample_get_buffer(s); GstMapInfo map;
    if (!gst_buffer_map(b, &map, GST_MAP_READ)) {
        gst_sample_unref(s); fail_media(m, "encoded buffer mapping failed"); return GST_FLOW_ERROR;
    }
    if (!map.size || map.size + m->codec_prefix_len > MEDIA_MAX_AU) {
        gst_buffer_unmap(b, &map); gst_sample_unref(s); fail_media(m, "encoded AU/header size bound"); return GST_FLOW_ERROR;
    }
    if (!media_au_has_vcl(map.data, map.size)) {
        /* Header-only buffers are not pictures. Fold their exact bytes into
           the next AU instead of consuming a raw-frame ID or losing SPS/PPS. */
        uint8_t *prefix = realloc(m->codec_prefix, m->codec_prefix_len + map.size);
        if (!prefix) { gst_buffer_unmap(b, &map); gst_sample_unref(s); fail_media(m, "codec header allocation"); return GST_FLOW_ERROR; }
        memcpy(prefix + m->codec_prefix_len, map.data, map.size);
        m->codec_prefix = prefix; m->codec_prefix_len += map.size;
        gst_buffer_unmap(b, &map); gst_sample_unref(s); return GST_FLOW_OK;
    }
    gst_buffer_unmap(b, &map);
    media_frame_t f = {0}; uint64_t rt = sample_rt(s);
    pthread_mutex_lock(&m->lock);
    int valid = source_id(m, rt, &f.frame_id, &f.pts_ns) == 0 && f.frame_id == m->encoded;
    if (valid) { f.source_mono_us = m->source_times[f.frame_id]; f.bitrate_epoch = m->epoch; }
    pthread_mutex_unlock(&m->lock);
    if (!valid || !gst_buffer_map(b, &map, GST_MAP_READ)) { gst_sample_unref(s); fail_media(m, "encoder PTS/frame mapping failed"); return GST_FLOW_ERROR; }
    if (!map.size || map.size > MEDIA_MAX_AU) { gst_buffer_unmap(b, &map); gst_sample_unref(s); fail_media(m, "encoded AU exceeds limit"); return GST_FLOW_ERROR; }
    f.au_len = m->codec_prefix_len + map.size; f.au = malloc(f.au_len);
    if (f.au) {
        if (m->codec_prefix_len) memcpy(f.au, m->codec_prefix, m->codec_prefix_len);
        memcpy(f.au + m->codec_prefix_len, map.data, map.size);
    }
    free(m->codec_prefix); m->codec_prefix = NULL; m->codec_prefix_len = 0;
    gst_buffer_unmap(b, &map); gst_sample_unref(s);
    if (!f.au) { fail_media(m, "AU allocation failed"); return GST_FLOW_ERROR; }
    f.decode_order_id = f.frame_id; f.dts_ns = f.pts_ns;
    f.duration_ns = GST_SECOND / m->cfg.fps; f.encoded_ready_mono_us = mono_us();
    f.flags = media_au_is_idr(f.au, f.au_len) ? MEDIA_FLAG_IDR : 0;
    if ((f.flags & MEDIA_FLAG_IDR) && !m->cfg.all_bp) f.flags |= MEDIA_FLAG_PRIORITY_I;
    f.crc32 = media_crc32(f.au, f.au_len);
    media_frame_t archive = f;
    archive.au = malloc(f.au_len);
    if (!archive.au) { media_frame_release(&f); fail_media(m, "archive allocation failed"); return GST_FLOW_ERROR; }
    memcpy(archive.au, f.au, f.au_len);
    pthread_mutex_lock(&m->lock);
    if (m->au_count == MQ_CAP || m->au_bytes + f.au_len > MQ_BYTES ||
        m->archive_count == MQ_CAP || m->archive_bytes + f.au_len > MQ_BYTES || m->stop || m->failed) {
        int stopping = m->stop;
        pthread_mutex_unlock(&m->lock); media_frame_release(&f); media_frame_release(&archive);
        if (!stopping) fail_media(m, "encoded AU/archive queue overflow");
        return stopping ? GST_FLOW_FLUSHING : GST_FLOW_ERROR;
    }
    m->aus[(m->au_head + m->au_count++) % MQ_CAP] = f;
    m->au_bytes += f.au_len; ++m->encoded;
    m->archives[(m->archive_head + m->archive_count++) % MQ_CAP] = archive;
    m->archive_bytes += archive.au_len;
    pthread_cond_signal(&m->cond); pthread_mutex_unlock(&m->lock); wake_media(m); return GST_FLOW_OK;
}

static GstFlowReturn decoded_sample(GstAppSink *sink, gpointer opaque)
{
    gst_media_t *m = opaque; GstSample *s = gst_app_sink_pull_sample(sink);
    if (!s) return GST_FLOW_EOS;
    uint64_t pts = GST_BUFFER_PTS(gst_sample_get_buffer(s));
    media_raw_t raw = {0};
    if (pts == GST_CLOCK_TIME_NONE || pts > (uint64_t)m->cfg.frames * GST_SECOND / m->cfg.fps + GST_SECOND) goto bad;
    raw.frame_id = (pts * m->cfg.fps + GST_SECOND / 2) / GST_SECOND;
    raw.pts_ns = pts;
    raw.length = (size_t)m->cfg.width * m->cfg.height * 3 / 2;
    raw.pixels = malloc(raw.length);
    GstVideoInfo vi; GstVideoFrame vf = {0}; gst_video_info_init(&vi);
    if (!raw.pixels || raw.frame_id >= m->cfg.frames ||
        !gst_video_info_from_caps(&vi, gst_sample_get_caps(s)) ||
        GST_VIDEO_INFO_FORMAT(&vi) != GST_VIDEO_FORMAT_I420 ||
        GST_VIDEO_INFO_WIDTH(&vi) != (int)m->cfg.width || GST_VIDEO_INFO_HEIGHT(&vi) != (int)m->cfg.height ||
        !gst_video_frame_map(&vf, &vi, gst_sample_get_buffer(s), GST_MAP_READ)) goto bad;
    size_t offset = 0;
    for (int p = 0; p < 3; ++p) {
        uint32_t w = p ? m->cfg.width / 2 : m->cfg.width, h = p ? m->cfg.height / 2 : m->cfg.height;
        const uint8_t *base = GST_VIDEO_FRAME_PLANE_DATA(&vf, p);
        gint stride = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, p);
        for (uint32_t y = 0; y < h; ++y) { memcpy(raw.pixels + offset, base + (ptrdiff_t)y * stride, w); offset += w; }
    }
    gst_video_frame_unmap(&vf); gst_sample_unref(s);
    pthread_mutex_lock(&m->lock);
    if (m->raw_count == MQ_CAP || m->stop || m->failed) {
        int stopping = m->stop;
        pthread_mutex_unlock(&m->lock); media_raw_release(&raw);
        if (!stopping) fail_media(m, "decoded raw queue overflow");
        return stopping ? GST_FLOW_FLUSHING : GST_FLOW_ERROR;
    }
    raw.ready_us = mono_us(); /* Pixels are copied and available to the application now. */
    m->raws[(m->raw_head + m->raw_count++) % MQ_CAP] = raw;
    pthread_mutex_unlock(&m->lock); wake_media(m); return GST_FLOW_OK;
bad:
    media_raw_release(&raw); gst_sample_unref(s); fail_media(m, "invalid decoded raw frame"); return GST_FLOW_ERROR;
}

static void sink_eos(GstAppSink *sink, gpointer opaque)
{
    (void)sink; gst_media_t *m = opaque;
    /* One branch can reach EOS before the raw reference branch. Only the
       pipeline bus EOS (all sinks) permits finalization. */
    wake_media(m);
}

static void *reference_worker(void *opaque)
{
    gst_media_t *m = opaque; uint64_t expected = 0;
    for (;;) {
        pthread_mutex_lock(&m->lock);
        while (!m->ref_count && !m->archive_count && !m->stop && !m->failed) pthread_cond_wait(&m->cond, &m->lock);
        if ((!m->ref_count && !m->archive_count && m->stop) || m->failed) { pthread_mutex_unlock(&m->lock); break; }
        if (m->archive_count) {
            media_frame_t f = m->archives[m->archive_head];
            memset(&m->archives[m->archive_head], 0, sizeof(f));
            m->archive_head = (m->archive_head + 1) % MQ_CAP; --m->archive_count; m->archive_bytes -= f.au_len;
            pthread_mutex_unlock(&m->lock);
            int bad = f.frame_id != m->archive_written || fwrite(f.au, 1, f.au_len, m->au_file) != f.au_len ||
                fprintf(m->au_index, "%" PRIu64 ",%" PRIu64 ",%zu,%d,%u,%" PRIu64 ",%" PRIu64 ",%u\n",
                    f.frame_id, f.pts_ns, f.au_len, !!(f.flags & MEDIA_FLAG_IDR), f.crc32,
                    f.source_mono_us, f.encoded_ready_mono_us, f.bitrate_epoch) < 0;
            media_frame_release(&f);
            if (bad) { fail_media(m, "encoded archive write/index failed"); break; }
            ++m->archive_written; continue;
        }
        GstSample *s = m->refs[m->ref_head]; m->refs[m->ref_head] = NULL;
        m->ref_head = (m->ref_head + 1) % MQ_CAP; --m->ref_count;
        uint64_t id = 0, pts = 0; int valid = source_id(m, sample_rt(s), &id, &pts) == 0 && id == expected;
        uint64_t source_time = valid ? m->source_times[id] : 0;
        pthread_mutex_unlock(&m->lock);
        if (!valid || media_write_i420_sample(s, m->raw_file, m->cfg.width, m->cfg.height) ||
            fprintf(m->raw_index, "%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n", id, pts, source_time) < 0) {
            gst_sample_unref(s); fail_media(m, "reference write/index failed"); break;
        }
        ++expected; ++m->reference_written; gst_sample_unref(s);
    }
    if (fflush(m->raw_file) || fflush(m->raw_index) || fflush(m->au_file) || fflush(m->au_index)) fail_media(m, "reference/archive flush failed");
    return NULL;
}

static void *decoder_worker(void *opaque)
{
    gst_media_t *m = opaque;
    for (;;) {
        pthread_mutex_lock(&m->lock);
        while (!m->in_count && !m->stop && !m->failed && !m->input_eos) pthread_cond_wait(&m->cond, &m->lock);
        if (m->stop || m->failed) { pthread_mutex_unlock(&m->lock); break; }
        if (!m->in_count && m->input_eos) {
            pthread_mutex_unlock(&m->lock);
            if (gst_app_src_end_of_stream(GST_APP_SRC(m->appsrc)) != GST_FLOW_OK) fail_media(m, "decoder EOS failed");
            break;
        }
        input_item_t item = m->inputs[m->in_head];
        memset(&m->inputs[m->in_head], 0, sizeof(item));
        m->in_head = (m->in_head + 1) % MQ_CAP; --m->in_count; m->in_bytes -= item.frame.au_len;
        pthread_mutex_unlock(&m->lock);
        if (item.reset) {
            /* A fresh decoder state cannot retain references from the lost GOP. */
            if (gst_element_set_state(m->pipeline, GST_STATE_READY) == GST_STATE_CHANGE_FAILURE ||
                gst_element_set_state(m->pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
                media_frame_release(&item.frame); fail_media(m, "decoder reset failed"); break;
            }
        }
        GstBuffer *b = gst_buffer_new_allocate(NULL, item.frame.au_len, NULL);
        if (!b) { media_frame_release(&item.frame); fail_media(m, "decoder input allocation failed"); break; }
        gst_buffer_fill(b, 0, item.frame.au, item.frame.au_len);
        GST_BUFFER_PTS(b) = item.frame.pts_ns; GST_BUFFER_DTS(b) = item.frame.dts_ns;
        GST_BUFFER_DURATION(b) = item.frame.duration_ns;
        if (item.reset) GST_BUFFER_FLAG_SET(b, GST_BUFFER_FLAG_DISCONT);
        if (!(item.frame.flags & MEDIA_FLAG_IDR)) GST_BUFFER_FLAG_SET(b, GST_BUFFER_FLAG_DELTA_UNIT);
        media_frame_release(&item.frame);
        if (gst_app_src_push_buffer(GST_APP_SRC(m->appsrc), b) != GST_FLOW_OK) {
            pthread_mutex_lock(&m->lock); int stopping = m->stop; pthread_mutex_unlock(&m->lock);
            if (!stopping) fail_media(m, "decoder push failed"); break;
        }
    }
    return NULL;
}

static gst_media_t *open_common(const media_config_t *cfg, int source)
{
    if (!media_config_valid(cfg)) return NULL;
    gst_init(NULL, NULL);
    gst_media_t *m = calloc(1, sizeof(*m)); if (!m) return NULL;
    m->cfg = *cfg; m->source = source; m->wake = -1;
    pthread_mutex_init(&m->lock, NULL); pthread_cond_init(&m->cond, NULL);
    gst_segment_init(&m->source_segment, GST_FORMAT_TIME);
    m->wake = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (m->wake < 0) goto bad;
    gchar *pipeline = NULL;
    if (source) {
        if (!cfg->pattern || (strcmp(cfg->pattern, "ball") && strcmp(cfg->pattern, "zone-plate") && strcmp(cfg->pattern, "smpte"))) goto bad;
        pipeline = g_strdup_printf(
            "videotestsrc name=source is-live=true pattern=%s %s num-buffers=%u ! "
            "video/x-raw,format=I420,width=%u,height=%u,framerate=%u/1,colorimetry=bt709 ! tee name=raw "
            "raw. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! "
            "x264enc name=encoder bitrate=%u pass=cbr tune=zerolatency speed-preset=veryfast "
            "key-int-max=%u bframes=0 byte-stream=true option-string=\"scenecut=0:open-gop=0\" ! "
            "h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=encoded sync=false max-buffers=8 drop=false "
            "raw. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! "
            "appsink name=reference sync=false max-buffers=8 drop=false",
            cfg->pattern, !strcmp(cfg->pattern, "zone-plate") ? "kx2=20 ky2=20 kt=1" :
                (!strcmp(cfg->pattern, "ball") ? "animation-mode=frames" : "horizontal-speed=8"),
            cfg->frames, cfg->width, cfg->height, cfg->fps, cfg->bitrate_kbps, cfg->gop);
    } else {
        pipeline = g_strdup("appsrc name=input is-live=true format=time do-timestamp=false block=true max-bytes=4194304 "
            "! h264parse ! avdec_h264 max-threads=1 output-corrupt=false ! videoconvert ! video/x-raw,format=I420,colorimetry=bt709 "
            "! appsink name=decoded sync=false max-buffers=8 drop=false");
    }
    GError *error = NULL; m->pipeline = gst_parse_launch(pipeline, &error); g_free(pipeline);
    if (error) { fprintf(stderr, "[MEDIA_ERROR] pipeline: %s\n", error->message); g_error_free(error); goto bad; }
    if (!m->pipeline) goto bad;
    m->bus = gst_element_get_bus(m->pipeline);
    if (source) {
        m->encoder = gst_bin_get_by_name(GST_BIN(m->pipeline), "encoder");
        if (!m->encoder) goto bad;
        GParamSpec *spec = g_object_class_find_property(G_OBJECT_GET_CLASS(m->encoder), "bitrate");
        if (!spec || !(spec->flags & GST_PARAM_MUTABLE_PLAYING)) {
            fprintf(stderr, "[MEDIA_ERROR] x264enc bitrate is not mutable in PLAYING\n"); goto bad;
        }
        m->source_times = calloc(cfg->frames, sizeof(uint64_t)); m->source_pts = calloc(cfg->frames, sizeof(uint64_t));
        if (!m->source_times || !m->source_pts) goto bad;
        gchar *p = g_build_filename(cfg->output_dir, "source.yuv", NULL); m->raw_file = fopen(p, "wb"); g_free(p);
        p = g_build_filename(cfg->output_dir, "source_frames.csv", NULL); m->raw_index = fopen(p, "w"); g_free(p);
        p = g_build_filename(cfg->output_dir, "encoded.h264", NULL); m->au_file = fopen(p, "wb"); g_free(p);
        p = g_build_filename(cfg->output_dir, "encoded_frames.csv", NULL); m->au_index = fopen(p, "w"); g_free(p);
        if (!m->raw_file || !m->raw_index || !m->au_file || !m->au_index) goto bad;
        fprintf(m->raw_index, "frame_id,pts_ns,source_mono_us\n");
        fprintf(m->au_index, "frame_id,pts_ns,au_bytes,idr,crc32,source_mono_us,encoded_ready_mono_us,bitrate_epoch\n");
        GstElement *tee = gst_bin_get_by_name(GST_BIN(m->pipeline), "raw");
        GstPad *pad = tee ? gst_element_get_static_pad(tee, "sink") : NULL;
        if (!pad) { if (tee) gst_object_unref(tee); goto bad; }
        gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER | GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM, source_probe, m, NULL);
        gst_object_unref(pad); gst_object_unref(tee);
        GstElement *encoded = gst_bin_get_by_name(GST_BIN(m->pipeline), "encoded");
        GstElement *ref = gst_bin_get_by_name(GST_BIN(m->pipeline), "reference");
        GstAppSinkCallbacks cb = {0}; cb.eos = sink_eos; cb.new_sample = encoded_sample;
        gst_app_sink_set_callbacks(GST_APP_SINK(encoded), &cb, m, NULL);
        memset(&cb, 0, sizeof(cb)); cb.new_sample = reference_sample;
        gst_app_sink_set_callbacks(GST_APP_SINK(ref), &cb, m, NULL);
        gst_object_unref(encoded); gst_object_unref(ref);
    } else {
        m->appsrc = gst_bin_get_by_name(GST_BIN(m->pipeline), "input");
        GstCaps *caps = gst_caps_new_simple("video/x-h264", "stream-format", G_TYPE_STRING, "byte-stream",
            "alignment", G_TYPE_STRING, "au", "width", G_TYPE_INT, cfg->width,
            "height", G_TYPE_INT, cfg->height, "framerate", GST_TYPE_FRACTION, cfg->fps, 1, NULL);
        gst_app_src_set_caps(GST_APP_SRC(m->appsrc), caps); gst_caps_unref(caps);
        GstElement *sink = gst_bin_get_by_name(GST_BIN(m->pipeline), "decoded");
        GstAppSinkCallbacks cb = {0}; cb.new_sample = decoded_sample;
        gst_app_sink_set_callbacks(GST_APP_SINK(sink), &cb, m, NULL); gst_object_unref(sink);
    }
    if (gst_element_set_state(m->pipeline, GST_STATE_READY) == GST_STATE_CHANGE_FAILURE) goto bad;
    gchar *version = gst_version_string();
    printf("[GSTREAMER] version=%s source=%d\n", version, source); g_free(version);
    return m;
bad:
    gst_media_close(m); return NULL;
}
gst_media_t *gst_media_source_open(const media_config_t *c) { return open_common(c, 1); }
gst_media_t *gst_media_decoder_open(const media_config_t *c) { return open_common(c, 0); }
int gst_media_start(gst_media_t *m)
{
    if (pthread_create(&m->worker, NULL, m->source ? reference_worker : decoder_worker, m)) return -1;
    m->worker_started = 1;
    return gst_element_set_state(m->pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE ? -1 : 0;
}
int gst_media_wake_fd(gst_media_t *m) { return m ? m->wake : -1; }
void gst_media_clear_wake(gst_media_t *m)
{ uint64_t n; while (read(m->wake, &n, sizeof(n)) == sizeof(n)) {} }
int gst_media_pop_au(gst_media_t *m, media_frame_t *f)
{
    pthread_mutex_lock(&m->lock);
    if (!m->au_count) { pthread_mutex_unlock(&m->lock); return 0; }
    *f = m->aus[m->au_head]; memset(&m->aus[m->au_head], 0, sizeof(*f));
    m->au_head = (m->au_head + 1) % MQ_CAP; --m->au_count; m->au_bytes -= f->au_len;
    pthread_mutex_unlock(&m->lock);
    return 1;
}
int gst_media_pop_raw(gst_media_t *m, media_raw_t *raw)
{
    pthread_mutex_lock(&m->lock);
    if (!m->raw_count) { pthread_mutex_unlock(&m->lock); return 0; }
    *raw = m->raws[m->raw_head]; memset(&m->raws[m->raw_head], 0, sizeof(*raw));
    m->raw_head = (m->raw_head + 1) % MQ_CAP; --m->raw_count;
    pthread_mutex_unlock(&m->lock); return 1;
}
int gst_media_submit(gst_media_t *m, media_frame_t *f, int reset)
{
    pthread_mutex_lock(&m->lock);
    if (m->in_count == MQ_CAP || m->in_bytes + f->au_len > MQ_BYTES || m->stop || m->failed || m->input_eos) {
        pthread_mutex_unlock(&m->lock); return -1;
    }
    input_item_t *item = &m->inputs[(m->in_head + m->in_count++) % MQ_CAP];
    item->frame = *f; item->reset = reset; m->in_bytes += f->au_len; memset(f, 0, sizeof(*f));
    pthread_cond_signal(&m->cond); pthread_mutex_unlock(&m->lock); return 0;
}
void gst_media_end_input(gst_media_t *m)
{
    pthread_mutex_lock(&m->lock); m->input_eos = 1;
    pthread_cond_signal(&m->cond); pthread_mutex_unlock(&m->lock);
}
int gst_media_status(gst_media_t *m, uint64_t *produced, uint64_t *encoded)
{
    GstMessage *msg;
    while ((msg = gst_bus_pop_filtered(m->bus, GST_MESSAGE_ERROR | GST_MESSAGE_EOS)) != NULL) {
        if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
            GError *e = NULL; gchar *debug = NULL; gst_message_parse_error(msg, &e, &debug);
            fail_media(m, e ? e->message : "GStreamer error"); g_clear_error(&e); g_free(debug);
        } else if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_EOS && m->source) {
            pthread_mutex_lock(&m->lock); m->eos = 1; pthread_mutex_unlock(&m->lock);
        }
        gst_message_unref(msg);
    }
    pthread_mutex_lock(&m->lock);
    if (produced) *produced = m->produced;
    if (encoded) *encoded = m->encoded;
    int status = m->failed ? -1 : (m->eos && !m->au_count ? 1 : 0);
    pthread_mutex_unlock(&m->lock); return status;
}
int gst_media_set_bitrate(gst_media_t *m, uint32_t kbps, uint64_t command)
{
    if (!m || !m->source || kbps < 100 || kbps > 100000) return -1;
    g_object_set(m->encoder, "bitrate", kbps, NULL);
    guint actual = 0; g_object_get(m->encoder, "bitrate", &actual, NULL);
    if (actual != kbps) return -1;
    pthread_mutex_lock(&m->lock); uint32_t epoch = ++m->epoch; uint64_t after = m->encoded;
    pthread_mutex_unlock(&m->lock);
    printf("[BITRATE_APPLIED] command_id=%" PRIu64 " target_kbps=%u property_kbps=%u epoch=%u apply_mono_us=%" PRIu64 " next_observed_au=%" PRIu64 "\n",
        command, kbps, actual, epoch, mono_us(), after); return 0;
}
int gst_media_finish_source(gst_media_t *m)
{
    if (!m || !m->source) return -1;
    gst_element_set_state(m->pipeline, GST_STATE_NULL);
    pthread_mutex_lock(&m->lock); m->stop = 1; pthread_cond_broadcast(&m->cond); pthread_mutex_unlock(&m->lock);
    if (m->worker_started) { pthread_join(m->worker, NULL); m->worker_started = 0; }
    return m->failed || m->reference_written != m->cfg.frames || m->archive_written != m->cfg.frames ||
        m->produced != m->cfg.frames || m->encoded != m->cfg.frames || m->codec_prefix_len ? -1 : 0;
}
void media_raw_release(media_raw_t *raw) { free(raw->pixels); memset(raw, 0, sizeof(*raw)); }
int gst_media_close(gst_media_t *m)
{
    if (!m) return 0;
    pthread_mutex_lock(&m->lock); m->stop = 1; pthread_cond_broadcast(&m->cond); pthread_mutex_unlock(&m->lock);
    /* NULL unblocks an appsrc push before joining its feeder. */
    if (m->pipeline) gst_element_set_state(m->pipeline, GST_STATE_NULL);
    if (m->worker_started) pthread_join(m->worker, NULL);
    for (int i = 0; i < MQ_CAP; ++i) {
        media_frame_release(&m->aus[i]); media_frame_release(&m->inputs[i].frame);
        media_frame_release(&m->archives[i]);
        media_raw_release(&m->raws[i]); if (m->refs[i]) gst_sample_unref(m->refs[i]);
    }
    if (m->raw_file && fclose(m->raw_file)) m->failed = 1;
    if (m->raw_index && fclose(m->raw_index)) m->failed = 1;
    if (m->au_file && fclose(m->au_file)) m->failed = 1;
    if (m->au_index && fclose(m->au_index)) m->failed = 1;
    if (m->bus) gst_object_unref(m->bus);
    if (m->encoder) gst_object_unref(m->encoder);
    if (m->appsrc) gst_object_unref(m->appsrc);
    if (m->pipeline) gst_object_unref(m->pipeline);
    if (m->wake >= 0) close(m->wake);
    free(m->source_times); free(m->source_pts);
    free(m->codec_prefix);
    int failed = m->failed;
    pthread_cond_destroy(&m->cond); pthread_mutex_destroy(&m->lock); free(m);
    return failed ? -1 : 0;
}
