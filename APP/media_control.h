#ifndef MEDIA_CONTROL_H
#define MEDIA_CONTROL_H
#include "gst_media.h"
typedef struct media_control_server media_control_server_t;
media_control_server_t *media_control_open(gst_media_t *, const char *path);
void media_control_close(media_control_server_t *);
#endif
