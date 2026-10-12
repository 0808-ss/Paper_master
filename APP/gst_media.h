#ifndef GST_MEDIA_H
#define GST_MEDIA_H
#include "media_protocol.h"
typedef struct gst_media gst_media_t;
typedef struct { uint64_t frame_id, pts_ns, ready_us; uint8_t *pixels; size_t length; } media_raw_t;
gst_media_t *gst_media_source_open(const media_config_t *);
gst_media_t *gst_media_decoder_open(const media_config_t *);
int gst_media_start(gst_media_t *);
int gst_media_wake_fd(gst_media_t *);
void gst_media_clear_wake(gst_media_t *);
int gst_media_pop_au(gst_media_t *, media_frame_t *);
int gst_media_pop_raw(gst_media_t *, media_raw_t *);
int gst_media_submit(gst_media_t *, media_frame_t *, int reset);
void gst_media_end_input(gst_media_t *);
int gst_media_status(gst_media_t *, uint64_t *produced, uint64_t *encoded);
int gst_media_finish_source(gst_media_t *);
int gst_media_set_bitrate(gst_media_t *, uint32_t, uint64_t);
int gst_media_close(gst_media_t *);
void media_raw_release(media_raw_t *);
int media_write_i420_sample(void *sample, void *file, uint32_t width, uint32_t height);
#endif
