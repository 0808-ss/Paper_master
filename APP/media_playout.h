#ifndef MEDIA_PLAYOUT_H
#define MEDIA_PLAYOUT_H
#include "gst_media.h"
typedef struct media_playout media_playout_t;
media_playout_t *media_playout_open(const media_config_t *, gst_media_t *, uint64_t t0_us);
int media_playout_receive(media_playout_t *, media_frame_t *);
int media_playout_tick(media_playout_t *, uint64_t now_us);
int media_playout_finish(media_playout_t *);
int media_playout_close(media_playout_t *);
#endif
