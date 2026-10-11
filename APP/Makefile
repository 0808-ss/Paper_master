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
PICOQUIC_HEADERS = ../picoquic/picoquic/picoquic.h \
                   ../picoquic/picoquic/picoquic_internal.h
PICOQUIC_LIBS = ../picoquic/libpicoquic-core.a ../picoquic/libpicoquic-log.a

all: $(TARGETS)

video_receiver: video_receiver.c video_common.h $(PICOQUIC_HEADERS) $(PICOQUIC_LIBS)
	$(CC) $(CFLAGS) $(INCLUDES) $< -o $@ $(LDFLAGS) $(LIBS)

video_sender: video_sender.c video_common.h $(PICOQUIC_HEADERS) $(PICOQUIC_LIBS)
	$(CC) $(CFLAGS) $(INCLUDES) $< -o $@ $(LDFLAGS) $(LIBS)

clean:
	rm -f $(TARGETS)
