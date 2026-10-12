CC = gcc
CFLAGS = -O2 -g -Wall

# 包含路径：picoquic 核心和依赖
INCLUDES = -I../picoquic/picoquic \
           -I../picoquic/_deps/picotls-src/include \
           -I../picoquic/loglib \
           -I.

# 库路径
LDFLAGS = -L../picoquic \
          -L../picoquic/_deps/picotls-build

# 链接库列表
# 用 --start-group / --end-group 让静态库循环扫描，
# 解决 libpicoquic-core.a 与 libpicoquic-log.a 之间的相互依赖
LIBS = -Wl,--start-group \
       -lpicoquic-core \
       -lpicoquic-log \
       -lpicotls-minicrypto \
       -lpicotls-openssl \
       -lpicotls-fusion \
       -lpicotls-core \
       -Wl,--end-group \
       -lssl \
       -lcrypto \
       -lpthread \
       -lm

TARGETS = video_receiver video_sender
MEDIA_TARGETS = video_receiver_gst video_sender_gst
MEDIA_SOURCES = media_protocol.c gst_media.c media_playout.c media_control.c media_transport.c
MEDIA_HEADERS = media_protocol.h gst_media.h media_playout.h media_control.h media_transport.h
GST_PACKAGES = gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0
PICOQUIC_HEADERS = ../picoquic/picoquic/picoquic.h \
                   ../picoquic/picoquic/picoquic_internal.h
PICOQUIC_LIBS = ../picoquic/libpicoquic-core.a ../picoquic/libpicoquic-log.a

all: $(TARGETS)

# Optional: legacy builds do not evaluate pkg-config or require GStreamer.
.PHONY: all media clean check-gstreamer media-protocol-check
media: $(MEDIA_TARGETS)

check-gstreamer:
	pkg-config --exists $(GST_PACKAGES)

$(MEDIA_TARGETS): %: %.c $(MEDIA_SOURCES) $(MEDIA_HEADERS) $(PICOQUIC_HEADERS) $(PICOQUIC_LIBS) | check-gstreamer
	$(CC) $(CFLAGS) -std=c11 $(INCLUDES) $< $(MEDIA_SOURCES) -o $@ $(LDFLAGS) $(LIBS) $$(pkg-config --cflags --libs $(GST_PACKAGES))

# Execute this only on the remote host, consistent with 项目.md.
media-protocol-check:
	$(CC) $(CFLAGS) -std=c11 -I. tests/media_protocol_test.c media_protocol.c -o media_protocol_test
	./media_protocol_test

video_receiver: video_receiver.c video_common.h $(PICOQUIC_HEADERS) $(PICOQUIC_LIBS)
	$(CC) $(CFLAGS) $(INCLUDES) $< -o $@ $(LDFLAGS) $(LIBS)

video_sender: video_sender.c video_common.h $(PICOQUIC_HEADERS) $(PICOQUIC_LIBS)
	$(CC) $(CFLAGS) $(INCLUDES) $< -o $@ $(LDFLAGS) $(LIBS)

clean:
	rm -f $(TARGETS) $(MEDIA_TARGETS) media_protocol_test
