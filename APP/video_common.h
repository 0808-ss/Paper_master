#ifndef VIDEO_COMMON_H
#define VIDEO_COMMON_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ================= I帧优先机制测试：应用层包格式（sender/receiver 共享） =================
 * 头部 20 字节：
 *   [ frame_idx    (uint32) ] 帧序号
 *   [ frame_class  (uint32) ] 帧类：0=BP帧(P/B预测帧), 1=I帧(关键帧)
 *   [ pkt_idx      (uint32) ] 帧内包序号
 *   [ total_pkts   (uint32) ] 帧总包数
 *   [ payload_size (uint32) ] 本包 payload 字节数
 * 注意：frame_class 为应用层约定（0=BP,1=I），仅用于接收端日志观测；
 * 传输层帧标记为 picoquic.h 的 PICOQUIC_VIDEO_FRAME_I/BP（值序相反），
 * 由 sender 按帧尺寸判定后映射，二者不共用同一枚举。
 * 修改本布局时 sender/receiver 必须同步重编译。
 */
#define APP_PKT_HEADER_SIZE 20
#define APP_PKT_PAYLOAD_MAX 1200
#define APP_FRAME_CLASS_BP  0
#define APP_FRAME_CLASS_I   1

/* 视频帧结构 */
typedef struct {
    size_t size;
    uint64_t wait_time_us; // 微秒
} video_frame_t;

// 读取 CSV 文件
static inline int load_video_trace(const char* filename, video_frame_t** frames, int* count) {
    FILE* fp = fopen(filename, "r");
    if (!fp) {
        perror("Cannot open CSV file");
        return -1;
    }

    char line[1024];
    int capacity = 1000;
    *count = 0;
    *frames = (video_frame_t*)malloc(sizeof(video_frame_t) * capacity);

    while (fgets(line, sizeof(line), fp)) {
        if (line[0] == '#') continue; // 跳过注释
        
        size_t size;
        double input_time;
        if (sscanf(line, "%lu,%lf", &size, &input_time) == 2) {
            if (*count >= capacity) {
                capacity *= 2;
                *frames = (video_frame_t*)realloc(*frames, sizeof(video_frame_t) * capacity);
            }
            (*frames)[*count].size = size;
            (*frames)[*count].wait_time_us = (uint64_t)(input_time * 1000000.0);
            (*count)++;
        }
    }
    fclose(fp);
    return 0;
}

#endif