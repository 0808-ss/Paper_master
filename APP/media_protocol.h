#ifndef MEDIA_PROTOCOL_H
#define MEDIA_PROTOCOL_H
#include <stddef.h>
#include <stdint.h>

#define MEDIA_ALPN "video-gst-v1"
#define MEDIA_HEADER_SIZE 96u
#define MEDIA_APP_HEADER 20u
#define MEDIA_APP_PAYLOAD 1200u
#define MEDIA_MAX_AU (8u * 1024u * 1024u)
#define MEDIA_MAX_FRAMES 18000u
#define MEDIA_CONTROL_SIZE 64u
#define MEDIA_FLAG_IDR 1u
#define MEDIA_FLAG_PRIORITY_I 2u
#define MEDIA_CODEC_H264 1u

typedef struct {
    uint32_t width, height, fps, frames, bitrate_kbps, gop;
    uint32_t playout_ms, decode_margin_ms;
    int preempt, all_bp, preview;
    const char *pattern;
    const char *scheduler;
    const char *output_dir;
    const char *control_socket;
} media_config_t;

typedef struct {
    uint64_t frame_id, decode_order_id, pts_ns, dts_ns, duration_ns;
    uint64_t source_mono_us, encoded_ready_mono_us;
    uint32_t flags, bitrate_epoch, crc32;
    uint8_t *au;
    size_t au_len;
} media_frame_t;

enum media_control_type {
    MEDIA_SETTINGS = 1, MEDIA_READY, MEDIA_START, MEDIA_END,
    MEDIA_PLAYOUT_DONE, MEDIA_ERROR
};
typedef struct {
    uint32_t type;
    uint32_t value[8];
    uint64_t time_us, count;
} media_control_record_t;

uint32_t media_read_u32(const uint8_t *);
uint64_t media_read_u64(const uint8_t *);
void media_write_u32(uint8_t *, uint32_t);
void media_write_u64(uint8_t *, uint64_t);
uint32_t media_crc32(const uint8_t *, size_t);
int media_config_valid(const media_config_t *);
int media_au_is_idr(const uint8_t *, size_t);
int media_au_has_vcl(const uint8_t *, size_t);
int media_frame_pack(const media_frame_t *, uint8_t **, size_t *);
int media_frame_unpack(const uint8_t *, size_t, media_frame_t *);
void media_frame_release(media_frame_t *);
void media_control_pack(const media_control_record_t *, uint8_t[MEDIA_CONTROL_SIZE]);
int media_control_unpack(const uint8_t[MEDIA_CONTROL_SIZE], media_control_record_t *);
#endif
