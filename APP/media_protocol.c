#include "media_protocol.h"
#include <limits.h>
#include <stdlib.h>
#include <string.h>

uint32_t media_read_u32(const uint8_t *p)
{ return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }
uint64_t media_read_u64(const uint8_t *p)
{ return ((uint64_t)media_read_u32(p) << 32) | media_read_u32(p + 4); }
void media_write_u32(uint8_t *p, uint32_t v)
{ p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v; }
void media_write_u64(uint8_t *p, uint64_t v)
{ media_write_u32(p, (uint32_t)(v >> 32)); media_write_u32(p + 4, (uint32_t)v); }

uint32_t media_crc32(const uint8_t *p, size_t n)
{
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < n; ++i) {
        crc ^= p[i];
        for (int j = 0; j < 8; ++j) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

int media_config_valid(const media_config_t *c)
{
    return c && c->width >= 16 && c->width <= 3840 && !(c->width & 1) &&
        c->height >= 16 && c->height <= 2160 && !(c->height & 1) &&
        c->fps >= 1 && c->fps <= 120 && c->frames > 0 && c->frames <= MEDIA_MAX_FRAMES &&
        c->bitrate_kbps >= 100 && c->bitrate_kbps <= 100000 && c->gop > 0 && c->gop <= 600 &&
        c->playout_ms >= 20 && c->playout_ms <= 5000 &&
        c->decode_margin_ms < c->playout_ms && (c->i_priority == 0 || c->i_priority == 1) &&
        c->stream_window_bytes >= 65535 && c->stream_window_bytes <= 32u * 1024u * 1024u &&
        c->connection_window_bytes >= c->stream_window_bytes && c->connection_window_bytes <= 32u * 1024u * 1024u;
}

int media_priority_override(const media_config_t *c, uint32_t flags)
{
    /* Leave other stream priorities and the existing preemption/drop mechanism alone. */
    return c && c->i_priority && !c->all_bp && (flags & MEDIA_FLAG_PRIORITY_I) ? 0 : -1;
}

static int contains_nal(const uint8_t *p, size_t n, int idr_only)
{
    for (size_t i = 0; i + 4 <= n; ++i) {
        size_t prefix = 0;
        if (p[i] == 0 && p[i + 1] == 0) {
            if (p[i + 2] == 1) prefix = 3;
            else if (i + 5 <= n && p[i + 2] == 0 && p[i + 3] == 1) prefix = 4;
        }
        if (prefix) {
            uint8_t type = p[i + prefix] & 31u;
            if (idr_only ? type == 5 : type >= 1 && type <= 5) return 1;
        }
    }
    return 0;
}
int media_au_is_idr(const uint8_t *p, size_t n) { return contains_nal(p, n, 1); }
int media_au_has_vcl(const uint8_t *p, size_t n) { return contains_nal(p, n, 0); }

static void pack_header(uint8_t *p, const media_frame_t *f)
{
    memset(p, 0, MEDIA_HEADER_SIZE);
    memcpy(p, "MGV1", 4); p[5] = 1; p[7] = MEDIA_HEADER_SIZE;
    media_write_u32(p + 8, f->flags); media_write_u32(p + 12, MEDIA_CODEC_H264);
    media_write_u64(p + 16, f->frame_id); media_write_u64(p + 24, f->decode_order_id);
    media_write_u64(p + 32, f->pts_ns); media_write_u64(p + 40, f->dts_ns);
    media_write_u64(p + 48, f->duration_ns); media_write_u64(p + 56, f->source_mono_us);
    media_write_u64(p + 64, f->encoded_ready_mono_us);
    media_write_u32(p + 72, (uint32_t)f->au_len);
    media_write_u32(p + 76, f->crc32); media_write_u32(p + 80, f->bitrate_epoch);
}

int media_frame_pack(const media_frame_t *f, uint8_t **out, size_t *length)
{
    if (!f || !f->au || !f->au_len || f->au_len > MEDIA_MAX_AU || f->frame_id > UINT32_MAX) return -1;
    size_t body_len = MEDIA_HEADER_SIZE + f->au_len;
    size_t packets = (body_len + MEDIA_APP_PAYLOAD - 1) / MEDIA_APP_PAYLOAD;
    uint8_t *body = malloc(body_len), *wire = malloc(body_len + packets * MEDIA_APP_HEADER);
    if (!body || !wire) { free(body); free(wire); return -1; }
    pack_header(body, f); memcpy(body + MEDIA_HEADER_SIZE, f->au, f->au_len);
    size_t wp = 0;
    for (size_t i = 0, offset = 0; offset < body_len; ++i) {
        size_t n = body_len - offset;
        if (n > MEDIA_APP_PAYLOAD) n = MEDIA_APP_PAYLOAD;
        media_write_u32(wire + wp, (uint32_t)f->frame_id);
        media_write_u32(wire + wp + 4, !!(f->flags & MEDIA_FLAG_PRIORITY_I));
        media_write_u32(wire + wp + 8, (uint32_t)i);
        media_write_u32(wire + wp + 12, (uint32_t)packets);
        media_write_u32(wire + wp + 16, (uint32_t)n);
        memcpy(wire + wp + MEDIA_APP_HEADER, body + offset, n);
        wp += MEDIA_APP_HEADER + n; offset += n;
    }
    free(body); *out = wire; *length = wp; return 0;
}

int media_frame_unpack(const uint8_t *wire, size_t length, media_frame_t *f)
{
    memset(f, 0, sizeof(*f));
    if (length < MEDIA_APP_HEADER + MEDIA_HEADER_SIZE ||
        length > MEDIA_MAX_AU + MEDIA_HEADER_SIZE +
            ((MEDIA_MAX_AU + MEDIA_HEADER_SIZE + 1199) / 1200) * MEDIA_APP_HEADER) return -1;
    uint32_t id = media_read_u32(wire), cls = media_read_u32(wire + 4);
    uint32_t count = media_read_u32(wire + 12);
    if (cls > 1 || !count || count > (MEDIA_MAX_AU + MEDIA_HEADER_SIZE + 1199) / 1200) return -1;
    uint8_t *body = malloc(length); if (!body) return -1;
    size_t rp = 0, bp = 0;
    for (uint32_t i = 0; i < count; ++i) {
        if (length - rp < MEDIA_APP_HEADER) goto invalid;
        uint32_t n = media_read_u32(wire + rp + 16);
        if (!n || n > MEDIA_APP_PAYLOAD || length - rp - MEDIA_APP_HEADER < n ||
            media_read_u32(wire + rp) != id || media_read_u32(wire + rp + 4) != cls ||
            media_read_u32(wire + rp + 8) != i || media_read_u32(wire + rp + 12) != count) goto invalid;
        memcpy(body + bp, wire + rp + MEDIA_APP_HEADER, n);
        bp += n; rp += MEDIA_APP_HEADER + n;
    }
    if (rp != length || bp < MEDIA_HEADER_SIZE || memcmp(body, "MGV1", 4) ||
        body[4] || body[5] != 1 || body[6] || body[7] != MEDIA_HEADER_SIZE ||
        media_read_u32(body + 12) != MEDIA_CODEC_H264) goto invalid;
    f->flags = media_read_u32(body + 8); f->frame_id = media_read_u64(body + 16);
    f->decode_order_id = media_read_u64(body + 24); f->pts_ns = media_read_u64(body + 32);
    f->dts_ns = media_read_u64(body + 40); f->duration_ns = media_read_u64(body + 48);
    f->source_mono_us = media_read_u64(body + 56); f->encoded_ready_mono_us = media_read_u64(body + 64);
    f->au_len = media_read_u32(body + 72); f->crc32 = media_read_u32(body + 76);
    f->bitrate_epoch = media_read_u32(body + 80);
    if (f->frame_id != id || f->decode_order_id != id || !f->au_len || f->au_len > MEDIA_MAX_AU ||
        f->au_len != bp - MEDIA_HEADER_SIZE || (f->flags & ~3u) ||
        !!(f->flags & MEDIA_FLAG_PRIORITY_I) != cls ||
        !media_au_has_vcl(body + MEDIA_HEADER_SIZE, f->au_len) ||
        !!(f->flags & MEDIA_FLAG_IDR) != media_au_is_idr(body + MEDIA_HEADER_SIZE, f->au_len) ||
        media_crc32(body + MEDIA_HEADER_SIZE, f->au_len) != f->crc32) goto invalid;
    f->au = malloc(f->au_len); if (!f->au) goto invalid;
    memcpy(f->au, body + MEDIA_HEADER_SIZE, f->au_len); free(body); return 0;
invalid:
    free(body); memset(f, 0, sizeof(*f)); return -1;
}

void media_frame_release(media_frame_t *f)
{ if (f) { free(f->au); memset(f, 0, sizeof(*f)); } }

/* Fixed-size records are length-prefixed so arbitrary QUIC callback boundaries are safe. */
void media_control_pack(const media_control_record_t *r, uint8_t p[MEDIA_CONTROL_SIZE])
{
    memset(p, 0, MEDIA_CONTROL_SIZE); media_write_u32(p, MEDIA_CONTROL_SIZE - 4);
    memcpy(p + 4, "MGC1", 4); media_write_u32(p + 8, r->type);
    for (int i = 0; i < 8; ++i) media_write_u32(p + 12 + 4 * i, r->value[i]);
    media_write_u64(p + 44, r->time_us); media_write_u64(p + 52, r->count);
}
int media_control_unpack(const uint8_t p[MEDIA_CONTROL_SIZE], media_control_record_t *r)
{
    if (media_read_u32(p) != MEDIA_CONTROL_SIZE - 4 || memcmp(p + 4, "MGC1", 4)) return -1;
    memset(r, 0, sizeof(*r)); r->type = media_read_u32(p + 8);
    if (r->type < MEDIA_SETTINGS || r->type > MEDIA_ERROR) return -1;
    for (int i = 0; i < 8; ++i) r->value[i] = media_read_u32(p + 12 + 4 * i);
    r->time_us = media_read_u64(p + 44); r->count = media_read_u64(p + 52); return 0;
}
