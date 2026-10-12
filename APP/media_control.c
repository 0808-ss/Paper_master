#define _GNU_SOURCE
#include "media_control.h"
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <poll.h>
#include <unistd.h>
#include <pthread.h>
#include <errno.h>
#include <ctype.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

struct media_control_server {
    gst_media_t *media;
    int fd, stop, started;
    pthread_t thread;
    pthread_mutex_t lock;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
};
static void spaces(const char **p) { while (isspace((unsigned char)**p)) ++*p; }
static int token(const char **p, char *out, size_t cap)
{
    spaces(p); if (*(*p)++ != '"') return -1;
    size_t n = 0;
    while (**p && **p != '"') {
        if (**p == '\\' || (unsigned char)**p < 32 || n + 1 >= cap) return -1;
        out[n++] = *(*p)++;
    }
    if (**p != '"') return -1;
    ++*p; out[n] = 0; return 0;
}
static int number(const char **p, uint64_t *out)
{
    spaces(p); if (!isdigit((unsigned char)**p)) return -1;
    errno = 0; char *end; unsigned long long v = strtoull(*p, &end, 10);
    if (errno || end == *p) return -1;
    *p = end; *out = v; return 0;
}
static int parse_request(const char *p, uint64_t *id, uint32_t *kbps)
{
    unsigned seen = 0; spaces(&p); if (*p++ != '{') return -1;
    for (;;) {
        char key[32], op[32]; if (token(&p, key, sizeof(key))) return -1;
        spaces(&p); if (*p++ != ':') return -1;
        if (!strcmp(key, "op")) {
            if ((seen & 1) || token(&p, op, sizeof(op)) || strcmp(op, "set_bitrate")) return -1;
            seen |= 1;
        } else if (!strcmp(key, "command_id")) {
            if ((seen & 2) || number(&p, id)) return -1;
            seen |= 2;
        } else if (!strcmp(key, "target_kbps")) {
            uint64_t v; if ((seen & 4) || number(&p, &v) || v < 100 || v > 100000) return -1;
            *kbps = (uint32_t)v; seen |= 4;
        } else return -1;
        spaces(&p); if (*p == '}') { ++p; break; } if (*p++ != ',') return -1;
    }
    spaces(&p); return *p || seen != 7 ? -1 : 0;
}
static int stopping(media_control_server_t *s)
{ pthread_mutex_lock(&s->lock); int stop = s->stop; pthread_mutex_unlock(&s->lock); return stop; }
static void *control_worker(void *opaque)
{
    media_control_server_t *s = opaque;
    while (!stopping(s)) {
        struct pollfd listener = {s->fd, POLLIN, 0};
        if (poll(&listener, 1, 100) <= 0 || !(listener.revents & POLLIN)) continue;
        int client = accept4(s->fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (client < 0) continue;
        char request[256] = {0}; size_t n = 0; int complete = 0;
        for (int waits = 0; waits < 10 && !stopping(s) && n + 1 < sizeof(request); ++waits) {
            struct pollfd p = {client, POLLIN, 0}; if (poll(&p, 1, 100) <= 0) continue;
            ssize_t got = recv(client, request + n, sizeof(request) - n - 1, 0);
            if (got <= 0) break;
            n += (size_t)got;
            if (memchr(request, '\n', n)) { complete = 1; break; }
        }
        uint64_t id = 0; uint32_t kbps = 0;
        int ok = complete && !parse_request(request, &id, &kbps) && !stopping(s) &&
            !gst_media_set_bitrate(s->media, kbps, id);
        char response[256]; int len = snprintf(response, sizeof(response),
            "{\"command_id\":%" PRIu64 ",\"status\":\"%s\",\"requested_kbps\":%u,\"property_kbps\":%u}\n",
            id, ok ? "applied_to_element" : "rejected", kbps, ok ? kbps : 0);
        /* One bounded response; client verifies a complete newline, never waits indefinitely. */
        (void)send(client, response, (size_t)len, MSG_NOSIGNAL); close(client);
    }
    return NULL;
}
media_control_server_t *media_control_open(gst_media_t *m, const char *path)
{
    if (!path || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) return NULL;
    media_control_server_t *s = calloc(1, sizeof(*s)); if (!s) return NULL;
    s->media = m; s->fd = -1; pthread_mutex_init(&s->lock, NULL); strcpy(s->path, path);
    /* Never unlink an existing endpoint belonging to another run. */
    struct stat st; if (!lstat(path, &st) || errno != ENOENT) goto bad;
    s->fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (s->fd < 0) goto bad;
    struct sockaddr_un addr = {0}; addr.sun_family = AF_UNIX; strcpy(addr.sun_path, path);
    if (bind(s->fd, (struct sockaddr *)&addr, sizeof(addr))) goto bad;
    if (chmod(path, 0600) || listen(s->fd, 4) || pthread_create(&s->thread, NULL, control_worker, s)) {
        unlink(path); goto bad;
    }
    s->started = 1; printf("[BITRATE_CONTROL] socket=%s min_kbps=100 max_kbps=100000\n", path); return s;
bad:
    if (s->fd >= 0) close(s->fd);
    pthread_mutex_destroy(&s->lock); free(s); return NULL;
}
void media_control_close(media_control_server_t *s)
{
    if (!s) return;
    pthread_mutex_lock(&s->lock); s->stop = 1; pthread_mutex_unlock(&s->lock);
    if (s->started) pthread_join(s->thread, NULL);
    close(s->fd); unlink(s->path); pthread_mutex_destroy(&s->lock); free(s);
}
