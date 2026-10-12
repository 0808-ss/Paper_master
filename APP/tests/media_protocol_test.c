/* Remote-only unit checks. No picoquic or GStreamer runtime required. */
#include "media_protocol.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void)
{
    assert(media_crc32((const uint8_t *)"123456789", 9) == 0xcbf43926u);
    uint8_t number[8]; media_write_u64(number, UINT64_C(0x0102030405060708));
    assert(!memcmp(number, "\x01\x02\x03\x04\x05\x06\x07\x08", 8));
    assert(media_read_u64(number) == UINT64_C(0x0102030405060708));
    uint8_t au[2500]; memset(au, 0x55, sizeof(au));
    memcpy(au, "\x00\x00\x00\x01\x65", 5);
    assert(media_au_has_vcl(au, sizeof(au)) && media_au_is_idr(au, sizeof(au)));
    assert(!media_au_has_vcl((const uint8_t *)"\x00\x00\x01\x67\x55", 5));
    media_frame_t input = {0}; input.frame_id = 7; input.decode_order_id = 7;
    input.pts_ns = input.dts_ns = 233333333; input.duration_ns = 33333333;
    input.source_mono_us = 100; input.encoded_ready_mono_us = 200;
    input.flags = MEDIA_FLAG_IDR | MEDIA_FLAG_PRIORITY_I;
    input.au = au; input.au_len = sizeof(au); input.crc32 = media_crc32(au, sizeof(au));
    uint8_t *wire; size_t length; assert(!media_frame_pack(&input, &wire, &length));
    assert(length == 96 + sizeof(au) + 3 * 20);
    media_frame_t decoded; assert(!media_frame_unpack(wire, length, &decoded));
    assert(decoded.frame_id == 7 && decoded.pts_ns == input.pts_ns && decoded.au_len == sizeof(au));
    assert(!memcmp(decoded.au, au, sizeof(au))); media_frame_release(&decoded);
    /* Corruption, truncation, packet index, length and flags are rejected. */
    wire[length - 1] ^= 1; assert(media_frame_unpack(wire, length, &decoded)); wire[length - 1] ^= 1;
    assert(media_frame_unpack(wire, length - 1, &decoded));
    media_write_u32(wire + 8, 1); assert(media_frame_unpack(wire, length, &decoded)); media_write_u32(wire + 8, 0);
    media_write_u32(wire + 16, 1201); assert(media_frame_unpack(wire, length, &decoded)); media_write_u32(wire + 16, 1200);
    wire[20 + 8] = 128; assert(media_frame_unpack(wire, length, &decoded)); wire[20 + 8] = 0;
    assert(!media_frame_unpack(wire, length, &decoded)); media_frame_release(&decoded); free(wire);
    media_control_record_t r = {0}, copy; uint8_t control[64];
    r.type = MEDIA_START; r.time_us = UINT64_C(123456789012); r.count = 300; r.value[0] = 1920;
    media_control_pack(&r, control); assert(!media_control_unpack(control, &copy));
    assert(copy.time_us == r.time_us && copy.count == 300 && copy.value[0] == 1920);
    control[0] = 1; assert(media_control_unpack(control, &copy));
    puts("media protocol checks passed"); return 0;
}
