#!/usr/bin/python3
import time
import os
import sys
import math
import shutil
import subprocess
import threading
import random
import bisect
import re
import shlex
import signal
import json
import hashlib
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from media_experiment import prepare_media, archive_media, media_outputs_valid, load_network_jitter_times
from link_replay import TCUpdater

# sudo mn -c
# sudo python3 demo_dubao.py

# ================= 配置参数 =================

# 随机种子，用于复现实验结果
RANDOM_SEED = 42
# 时延抖动概率 (0.0 ~ 1.0)
JITTER_PROBABILITY = 0.2
# 时延抖动增加量选项 (ms)
JITTER_DELAYS = [5, 10, 15]

# ================= I帧优先机制测试参数 =================
# 传给 video_sender 的传输层抢占开关 "0/1"：
#   0 = 完全基线（机制不生效，等同于未修改的 picoquic 行为）
#   1 = 启用 I/BP 帧抢占（默认）
# 机制说明（对应 picoquic work@9e620450）：
#   - 应用按"一帧一流"把每帧数据经 picoquic_add_to_stream_with_frame_type 标记 I/BP
#   - 发包前先放行 I 帧队列（I 帧流置优先级 0），I 队列空才放行 BP 帧
#   - 跨帧、多路径抢占由未改动的原调度器完成
# 可用环境变量 PREEMPT_CFG 覆盖（便于不修改代码跑消融实验）。
# 消融实验示例：
#   基线(机制关): "0"
#   全开(默认):   "1"
PREEMPT_CONFIG = "1"

# GOP 间隔（I 帧关键帧间隔，单位：帧）：传给 video_sender 第 5 个参数。
#   帧索引为 GOP_SIZE 整数倍的帧（0, GOP_SIZE, 2*GOP_SIZE, ...）标记为 I 帧，
#   其余为 BP 帧；0 = 所有帧按 BP 发送（无 I 帧标记，可用于对照）。
#   修改 GOP_SIZE 时须保证轨迹中真实关键帧每 GOP_SIZE 帧出现一次（当前 3 条轨迹为 30）。
# 可用环境变量 GOP_SIZE_CFG 覆盖（便于不同 GOP 间隔的消融实验不修改代码）。
GOP_SIZE_CONFIG = "30"

# 多路径数据包调度策略（多路径选路）：传给 video_sender 第 6 个参数。
#   default = picoquic 原版选路（min-RTT ACK + 流亲和 + 最近未用，机制不生效）
#   rr      = Round Robin：按路径索引轮询选择可用路径
#   minrtt  = 最小 RTT：始终选择 rtt_min 最小的可用路径
#   arrival = 每 AU 完成时间调度：带宽、RTT/2、CWND、pacing 和已分配待发队列
# arrival 同时约束 AU/Stream 在每条路径上的成包预算；保留路径校验、ACK 与拥塞控制；
# 单路径连接下该配置无效。
# 可用环境变量 SCHEDULER_CFG 覆盖（便于不修改代码跑不同调度策略实验）。
SCHEDULER_CONFIG = "default"

# 实验归档标签：每次运行结束后把 qlog_sender/qlog_receiver/send.log/recv.log/
# link_metrics.csv 归档到 runs/<RUN_TAG>_<时间戳>/，避免消融对比时被下次运行覆盖。
# 可用环境变量 RUN_TAG 覆盖；不设置时自动按抢占配置与 GOP 间隔命名。
RUN_TAG = ""

# 每次实验使用独立 RNG，在启动网络负载前生成抖动序列。

# ================= 数据读取与查询逻辑 =================

def load_video_trace(filepath):
    """读取视频文件以获取帧调度定时"""
    frame_times = []
    current_time_us = 0
    try:
        with open(filepath, 'r') as f:
            for line in f:
                if not line.strip(): continue
                parts = line.split(',')
                if len(parts) >= 2:
                    try:
                        if int(parts[0]) <= 0:
                            raise ValueError("frame size must be positive")
                        wait_us = int(float(parts[1]) * 1000000)
                        if wait_us < 0:
                            raise ValueError("negative frame interval")
                        # C 在 ready 后的共同起点立即生成第一帧，然后等待前一帧间隔。
                        frame_times.append(current_time_us / 1000000.0)
                        current_time_us += wait_us
                    except ValueError:
                        continue
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
    return frame_times

def load_trace(filepath):
    """读取轨迹文件 (time x y z)"""
    trace = []
    with open(filepath, 'r') as f:
        lines = f.readlines()
        for i, line in enumerate(lines):
            if i == 0 or not line.strip(): continue # 跳过表头
            parts = line.split()
            if len(parts) >= 4:
                trace.append({
                    'time': float(parts[0]),
                    'x': float(parts[1]),
                    'y': float(parts[2]),
                    'z': float(parts[3])
                })
    return trace

def load_grid_data(thr_filepath, lat_filepath):
    """分别从吞吐和延迟TSV文件中读取数据并合并 (基于 x, y, z)"""
    grid_dict = {}

    # 解析吞吐
    try:
        with open(thr_filepath, 'r') as f:
            for i, line in enumerate(f):
                if i == 0 or not line.strip(): continue
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        key = (float(parts[1]), float(parts[2]), float(parts[3]))
                        thr = float(parts[5]) if parts[5].strip().lower() != 'nan' else 0.0
                        grid_dict[key] = {'thr': thr, 'lat': 10.0}
                    except ValueError:
                        continue
    except Exception as e:
        print(f"Error reading {thr_filepath}: {e}")

    # 解析延迟
    try:
        with open(lat_filepath, 'r') as f:
            for i, line in enumerate(f):
                if i == 0 or not line.strip(): continue
                parts = line.split()
                if len(parts) >= 8:
                    try:
                        key = (float(parts[1]), float(parts[2]), float(parts[3]))
                        lat = float(parts[7]) if parts[7].strip().lower() != 'nan' else 10.0
                        if key in grid_dict:
                            grid_dict[key]['lat'] = lat
                        else:
                            grid_dict[key] = {'thr': 0.1, 'lat': lat}
                    except ValueError:
                        continue
    except Exception as e:
        print(f"Error reading {lat_filepath}: {e}")

    grid = []
    for k, v in grid_dict.items():
        grid.append({'x': k[0], 'y': k[1], 'z': k[2], 'thr': v['thr'], 'lat': v['lat']})

    return grid

def get_nearest_values(grid, x, y, z):
    """查找欧氏距离最近的网格点数据"""
    if not grid: return 50.0, 10.0

    min_dist = float('inf')
    best_thr, best_lat = 50.0, 10.0
    for pt in grid:
        dist = (pt['x']-x)**2 + (pt['y']-y)**2 + (pt['z']-z)**2
        if dist < min_dist:
            min_dist = dist
            best_thr = pt['thr']
            best_lat = pt['lat']

    best_thr = max(1.0, best_thr)
    return best_thr, best_lat

# ================= TC 初始化与原位更新 =================

def checked_tc(intf, command):
    output = intf.node.cmd(command + '; printf "\\n__TC_RC__%s\\n" "$?"')
    marker = re.search(r"__TC_RC__(\d+)\s*$", output)
    if not marker or int(marker.group(1)) != 0:
        raise RuntimeError(f"TC command failed on {intf.name}: {command}: {output}")


def safe_update_tc(intf, bw_mbps, delay_str, initialize=False):
    """启动负载前创建固定句柄；运行中只 change，保留排队报文。"""
    ifname = shlex.quote(intf.name)
    if initialize:
        checked_tc(intf, f'tc qdisc replace dev {ifname} root handle 1: htb default 1')
    action = 'add' if initialize else 'change'
    checked_tc(intf, f'tc class {action} dev {ifname} parent 1: classid 1:1 '
               f'htb rate {bw_mbps}mbit ceil {bw_mbps}mbit')
    checked_tc(intf, f'tc qdisc {action} dev {ifname} parent 1:1 handle 10: '
               f'netem limit 1000 delay {delay_str}')


def precompute_link_trace(trace, ground_grid, sky_grid):
    """最近点扫描只在负载启动前执行，不占用回放的更新时间预算。"""
    if not trace or not ground_grid or not sky_grid:
        raise ValueError("Travel/ground/sky input data must not be empty")
    if any(b['time'] <= a['time'] for a, b in zip(trace, trace[1:])):
        raise ValueError("Travel timestamps must be strictly increasing")
    prepared = []
    for pt in trace:
        g = get_nearest_values(ground_grid, pt['x'], pt['y'], pt['z'])
        s = get_nearest_values(sky_grid, pt['x'], pt['y'], pt['z'])
        if not all(math.isfinite(v) and v >= 0 for v in (*g, *s)):
            raise ValueError("non-finite or negative link parameter")
        prepared.append(dict(pt, ground_bw=g[0], ground_lat=g[1], sky_bw=s[0], sky_lat=s[1]))
    return prepared

# ================= Mininet 拓扑设计 =================

class MPQUICVideoTopo(Topo):
    def build(self):
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')

        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        self.addLink(h1, s1, bw=10, delay='10ms')
        self.addLink(s1, s2, bw=100, delay='1ms')
        self.addLink(s2, h2, bw=100, delay='1ms')

        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')
        self.addLink(h1, s3, bw=5, delay='5ms')
        self.addLink(s3, s4, bw=100, delay='1ms')
        self.addLink(s4, h2, bw=100, delay='1ms')

# ================= 动态链路更新线程（带抖动） =================

def dynamic_link_updater_with_jitter(net, prepared_trace, frame_times, stop_event,
                                      sender_log, errors, timing_state, granularity=0.01,
                                      log_file=None, jitter_file=None, tc_updater=None):
    """在与 Linux picoquic 相同的 CLOCK_MONOTONIC 时间轴回放预计算参数。"""
    rng = random.Random(RANDOM_SEED)
    jitter = []
    for _ in frame_times:
        g = rng.choice(JITTER_DELAYS) if rng.random() < JITTER_PROBABILITY else 0
        s = rng.choice(JITTER_DELAYS) if rng.random() < JITTER_PROBABILITY else 0
        jitter.append((g, s))
    h1 = net.get('h1')
    links = (net.linksBetween(h1, net.get('s1'))[0],
             net.linksBetween(h1, net.get('s3'))[0])
    trace_times = [pt['time'] for pt in prepared_trace]
    first = prepared_trace[0]
    previous = [(first['ground_bw'], max(0.1, first['ground_lat'])),
                (first['sky_bw'], max(0.1, first['sky_lat']))]
    deadline = time.monotonic() + 30.0
    start_time = None
    previous_frame = -1
    reader = None
    updater = tc_updater
    owns_updater = tc_updater is None
    try:
        if updater is None:
            updater = TCUpdater(links, backend=os.environ.get('TC_BACKEND', 'persistent'))
        timing_state['tc_backend'] = updater.backend
        timing_state['tc_process_starts'] = updater.process_starts
        if jitter_file:
            # Independent of live encoder frames/bitrate; preserve the old network calendar.
            with open(jitter_file, 'w') as jitter_fp:
                jitter_fp.write('event,time,ground_jitter_ms,sky_jitter_ms\n')
                for event, (scheduled, extras) in enumerate(zip(frame_times, jitter)):
                    jitter_fp.write(f'{event},{scheduled:.9f},{extras[0]},{extras[1]}\n')
        # 发送端 ready 后公布未来 200 ms 的共同起点。旧二进制缺少标记时显式失败。
        while not stop_event.is_set() and start_time is None:
            if reader is None and os.path.exists(sender_log):
                reader = open(sender_log, 'r')
            if reader is not None:
                for line in reader:
                    match = re.search(r"\[VIDEO_START\] monotonic_us=(\d+)", line)
                    if match:
                        start_time = int(match.group(1)) / 1000000.0
                        break
            if time.monotonic() >= deadline:
                raise RuntimeError("No VIDEO_START marker within 30s; rebuild sender or check handshake")
            if start_time is None:
                stop_event.wait(0.005)
        if start_time is None:
            return
        if stop_event.wait(max(0.0, start_time - time.monotonic())):
            return
        info(f"*** Link/video common origin: {start_time:.6f}s (monotonic) ***\n")
        scheduled = 0.0
        with open(log_file, 'w') as log_fp:
            log_fp.write("time,ground_bw,ground_lat,ground_jitter,sky_bw,sky_lat,sky_jitter,"
                         "applied_time,update_lag_ms,frames_skipped,scheduled_time,tc_apply_ms,tc_commands,tc_batches\n")
            while not stop_event.is_set():
                elapsed = time.monotonic() - start_time
                # 保留旧 Travel 查询的 ceiling 采样规则，不改变数据集建模。
                ti = min(bisect.bisect_left(trace_times, elapsed), len(prepared_trace) - 1)
                pt = prepared_trace[ti]
                fi = bisect.bisect_right(frame_times, elapsed) - 1
                g_extra, s_extra = jitter[fi] if fi >= 0 else (0, 0)
                skipped = max(0, fi - previous_frame - 1)
                previous_frame = fi
                timing_state['frames_skipped'] += skipped
                values = [(pt['ground_bw'], max(0.1, pt['ground_lat'] + g_extra)),
                          (pt['sky_bw'], max(0.1, pt['sky_lat'] + s_extra))]
                apply_begin = time.monotonic()
                previous, command_count, batch_count = updater.apply(previous, values)
                timing_state['tc_process_starts'] = updater.process_starts
                applied = time.monotonic() - start_time
                tc_apply_ms = (time.monotonic() - apply_begin) * 1000.0
                timing_state['max_tc_apply_ms'] = max(timing_state.get('max_tc_apply_ms', 0.0), tc_apply_ms)
                timing_state['tc_commands'] = timing_state.get('tc_commands', 0) + command_count
                timing_state['tc_batches'] = timing_state.get('tc_batches', 0) + batch_count
                lag_ms = max(0.0, applied - scheduled) * 1000.0
                timing_state['max_update_lag_ms'] = max(timing_state['max_update_lag_ms'], lag_ms)
                log_fp.write(f"{elapsed:.6f},{pt['ground_bw']:.6f},{pt['ground_lat']:.6f},{g_extra},"
                             f"{pt['sky_bw']:.6f},{pt['sky_lat']:.6f},{s_extra},"
                             f"{applied:.6f},{lag_ms:.3f},{skipped},{scheduled:.9f},"
                             f"{tc_apply_ms:.3f},{command_count},{batch_count}\n")
                log_fp.flush()
                # Wake at jitter boundaries as well as the original absolute 10ms grid.
                # Do not replay expired events in a burst or conceal missed events.
                scheduled = (int(applied / granularity) + 1) * granularity
                next_event = bisect.bisect_right(frame_times, applied)
                if next_event < len(frame_times):
                    scheduled = min(scheduled, frame_times[next_event])
                stop_event.wait(max(0.0, start_time + scheduled - time.monotonic()))
    except Exception as exc:
        errors.append(str(exc))
        info(f"*** Link updater failed: {exc} ***\n")
        stop_event.set()
    finally:
        if updater is not None and owns_updater:
            try:
                updater.close()
            except Exception as exc:
                errors.append(str(exc))
                stop_event.set()
        if reader is not None:
            reader.close()

# ================= 绘制图表逻辑 =================

def plot_metrics(log_file, output_img):
    if not os.path.exists(log_file):
        return
    times = []
    g_bw, g_lat, s_bw, s_lat = [], [], [], []
    with open(log_file, 'r') as f:
        lines = f.readlines()
        for i, line in enumerate(lines):
            if i == 0: continue
            parts = line.strip().split(',')
            if len(parts) >= 7:
                times.append(float(parts[0]))
                g_bw.append(float(parts[1]))
                g_lat.append(float(parts[2]) + float(parts[3]))
                s_bw.append(float(parts[4]))
                s_lat.append(float(parts[5]) + float(parts[6]))
                
    if not times: return

    info(f"*** Plot saved to {output_img} ***\n")

# ================= 主控制流 =================

def cleanup_qlog(cwd):
    """清理上次运行残留的 qlog 目录，确保本次实验数据干净。

    发送端写 ./qlog_sender/*.qlog，接收端写 ./qlog_receiver/*.qlog，
    目录由 video_sender/video_receiver 启动时自动重建。
    """
    for d in ("qlog_sender", "qlog_receiver"):
        qlog_dir = os.path.join(cwd, d)
        if os.path.isdir(qlog_dir):
            shutil.rmtree(qlog_dir, ignore_errors=True)
            info(f"*** Cleaned residual qlog dir: {qlog_dir} ***\n")
        elif os.path.exists(qlog_dir):
            os.remove(qlog_dir)
            info(f"*** Removed residual qlog file: {qlog_dir} ***\n")

def archive_run(cwd, tag):
    """I帧优先机制测试：把本次运行的 qlog 与日志归档到 runs/<tag>/，
    保证消融对比的每一组实验数据不被下一次运行覆盖。"""
    if not tag:
        return
    run_dir = os.path.join(cwd, "runs", tag)
    os.makedirs(run_dir, exist_ok=True)
    for src, dst_name in (("qlog_sender", "qlog_sender"),
                          ("qlog_receiver", "qlog_receiver"),
                          ("send.log", "send.log"),
                          ("recv.log", "recv.log"),
                          ("link_metrics.csv", "link_metrics.csv"),
                          ("tc_state.log", "tc_state.log"),
                          ("network_jitter_events.csv", "network_jitter_events.csv"),
                          ("run_manifest.json", "run_manifest.json")):
        p = os.path.join(cwd, src)
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(run_dir, dst_name), dirs_exist_ok=True)
        elif os.path.exists(p):
            shutil.copy2(p, os.path.join(run_dir, dst_name))
    info(f"*** Run archived to {run_dir} ***\n")
    return run_dir

def run():

    # sudo mn -c
    # sudo python3 demo_with_jitter.py
    cwd = os.path.dirname(os.path.abspath(__file__))
    video_mode = os.environ.get('VIDEO_MODE', 'synthetic')
    if video_mode not in ('synthetic', 'gst'):
        raise ValueError('VIDEO_MODE must be synthetic or gst')
    media = None
    preempt_cfg = os.environ.get("PREEMPT_CFG", PREEMPT_CONFIG)
    gop_size_cfg = os.environ.get("GOP_SIZE_CFG", GOP_SIZE_CONFIG)
    scheduler_cfg = os.environ.get("SCHEDULER_CFG", SCHEDULER_CONFIG)
    tc_backend = os.environ.get('TC_BACKEND', 'persistent')
    if tc_backend not in ('persistent', 'batch'):
        raise ValueError('TC_BACKEND must be persistent or batch')
    run_tag = os.environ.get('RUN_TAG') or (
        f"{'gst_' if video_mode == 'gst' else ''}cfg_{preempt_cfg}_gop{gop_size_cfg}_{scheduler_cfg}_"
        + time.strftime('%Y%m%d_%H%M%S') + f"_{time.time_ns() % 1000000000:09d}")
    if run_tag in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9_.-]+', run_tag):
        raise ValueError('RUN_TAG must be a single directory name using letters, numbers, _, - or .')
    # Reserve before cleaning logs/launching traffic; an existing run is never merged.
    os.makedirs(os.path.join(cwd, 'runs', run_tag), exist_ok=False)

    trace_file = os.path.join(cwd, "Travel.csv")
    ground_thr_path = os.path.join(cwd, "p2p_data/summary_Throughput_ground.tsv")
    ground_lat_path = os.path.join(cwd, "p2p_data/summary_Latency_ground.tsv")
    sky_thr_path = os.path.join(cwd, "p2p_data/summary_Throughput_sky.tsv")
    sky_lat_path = os.path.join(cwd, "p2p_data/summary_Latency_sky.tsv")

    cert_file = os.path.join(cwd, "cert.pem")
    key_file = os.path.join(cwd, "key.pem")
    receiver_bin = os.path.join(cwd, "video_receiver_gst" if video_mode == 'gst' else "video_receiver")
    sender_bin = os.path.join(cwd, "video_sender_gst" if video_mode == 'gst' else "video_sender")

    if not os.path.exists(trace_file):
        sys.exit(f"Error: {trace_file} does not exist.")
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        sys.exit(f"Error: Certificates not found in {cwd}")
    if not os.path.exists(receiver_bin) or not os.path.exists(sender_bin):
        sys.exit(f"Error: binaries for VIDEO_MODE={video_mode} not built (make media for gst).")

    # 每次启动前清理上次运行残留的 qlog，避免新旧日志混在一起
    cleanup_qlog(cwd)

    info("*** Loading Data Files ***\n")
    trace_data = load_trace(trace_file)
    ground_grid = load_grid_data(ground_thr_path, ground_lat_path)
    sky_grid = load_grid_data(sky_thr_path, sky_lat_path)
    video_trace = os.path.abspath(os.environ.get("VIDEO_TRACE", os.path.join(cwd, "4k_30f_trace_0.8.csv")))
    if video_mode == 'gst':
        video_trace = os.path.abspath(os.environ.get('NETWORK_JITTER_TIMING_TRACE', video_trace))
    frame_times = load_network_jitter_times(video_trace) if video_mode == 'gst' else load_video_trace(video_trace)
    if not frame_times:
        raise ValueError("video trace must contain valid frames")
    info("*** Precomputing Travel link parameters before starting traffic ***\n")
    prepared_trace = precompute_link_trace(trace_data, ground_grid, sky_grid)
    if video_mode == 'gst':
        media = prepare_media(cwd, preempt_cfg, gop_size_cfg, scheduler_cfg)
        info(f"*** Media config: {media['config']} ***\n")
        info(f"*** Live bitrate control socket: {media['control_socket']} ***\n")

    topo = MPQUICVideoTopo()
    # 明确指定无控制器和自动设置MAC，避免任何干扰
    net = Mininet(topo=topo, link=TCLink, controller=None, autoSetMacs=True)
    net.start()

    h1, h2 = net.get('h1'), net.get('h2')
    s1, s2, s3, s4 = net.get('s1'), net.get('s2'), net.get('s3'), net.get('s4')

    # ========== 1. 交换机配置：绝对可靠的2端口交换机流表 ==========
    # 对于只有两个端口的交换机，直接指定端口转发规则，比NORMAL模式更可靠
    # 完全兼容所有OVS版本（2.13.x ~ 3.x）
    s1.cmd('ovs-ofctl del-flows s1')
    s1.cmd('ovs-ofctl add-flow s1 in_port=1,actions=output:2')
    s1.cmd('ovs-ofctl add-flow s1 in_port=2,actions=output:1')

    s2.cmd('ovs-ofctl del-flows s2')
    s2.cmd('ovs-ofctl add-flow s2 in_port=1,actions=output:2')
    s2.cmd('ovs-ofctl add-flow s2 in_port=2,actions=output:1')

    s3.cmd('ovs-ofctl del-flows s3')
    s3.cmd('ovs-ofctl add-flow s3 in_port=1,actions=output:2')
    s3.cmd('ovs-ofctl add-flow s3 in_port=2,actions=output:1')

    s4.cmd('ovs-ofctl del-flows s4')
    s4.cmd('ovs-ofctl add-flow s4 in_port=1,actions=output:2')
    s4.cmd('ovs-ofctl add-flow s4 in_port=2,actions=output:1')

    info("*** Configuring Interfaces & Routing ***\n")

    # ========== 2. 接口IP配置 ==========
    h1.cmd('ifconfig h1-eth0 10.0.1.1/24 up')
    h2.cmd('ifconfig h2-eth0 10.0.1.2/24 up')
    h1.cmd('ifconfig h1-eth1 10.0.2.1/24 up')
    h2.cmd('ifconfig h2-eth1 10.0.2.2/24 up')

    # ========== 3. 静态ARP配置（彻底解决ARP问题） ==========
    h1.setARP('10.0.1.2', h2.MAC(intf='h2-eth0'))
    h1.setARP('10.0.2.2', h2.MAC(intf='h2-eth1'))
    h2.setARP('10.0.1.1', h1.MAC(intf='h1-eth0'))
    h2.setARP('10.0.2.1', h1.MAC(intf='h1-eth1'))

    # ========== 4. 系统配置 ==========
    # 清除所有防火墙规则
    h1.cmd('iptables -F && iptables -X && iptables -t nat -F && iptables -t nat -X')
    h2.cmd('iptables -F && iptables -X && iptables -t nat -F && iptables -t nat -X')

    # 禁用反向路径过滤（多宿主主机必须配置）
    for h in [h1, h2]:
        h.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        h.cmd('sysctl -w net.ipv4.conf.default.rp_filter=0')
        h.cmd('sysctl -w net.ipv4.conf.lo.rp_filter=0')

    # ========== 5. 策略路由配置（绝对可靠版） ==========
    # 完全清除所有现有规则和路由表
    for h in [h1, h2]:
        h.cmd('ip rule flush')
        h.cmd('ip route flush table 1')
        h.cmd('ip route flush table 2')
        h.cmd('ip route flush table main')
        h.cmd('ip route flush cache')

    # h1配置
    h1.cmd('ip rule add from 10.0.1.1 table 1 priority 100')
    h1.cmd('ip rule add from 10.0.2.1 table 2 priority 200')
    h1.cmd('ip rule add from all lookup main priority 32767')

    h1.cmd('ip route add 10.0.1.0/24 dev h1-eth0 scope link table 1')
    h1.cmd('ip route add default via 10.0.1.2 dev h1-eth0 table 1')

    h1.cmd('ip route add 10.0.2.0/24 dev h1-eth1 scope link table 2')
    h1.cmd('ip route add default via 10.0.2.2 dev h1-eth1 table 2')

    h1.cmd('ip route add 10.0.1.0/24 dev h1-eth0 scope link')
    h1.cmd('ip route add 10.0.2.0/24 dev h1-eth1 scope link')

    # h2配置
    h2.cmd('ip rule add from 10.0.1.2 table 1 priority 100')
    h2.cmd('ip rule add from 10.0.2.2 table 2 priority 200')
    h2.cmd('ip rule add from all lookup main priority 32767')

    h2.cmd('ip route add 10.0.1.0/24 dev h2-eth0 scope link table 1')
    h2.cmd('ip route add default via 10.0.1.1 dev h2-eth0 table 1')

    h2.cmd('ip route add 10.0.2.0/24 dev h2-eth1 scope link table 2')
    h2.cmd('ip route add default via 10.0.2.1 dev h2-eth1 table 2')

    h2.cmd('ip route add 10.0.1.0/24 dev h2-eth0 scope link')
    h2.cmd('ip route add 10.0.2.0/24 dev h2-eth1 scope link')

    # 等待网络稳定
    time.sleep(0.5)

    # ========== 6. 连通性测试 ==========
    info("*** Connectivity Check ***\n")
    info("Testing Ground path (h1 -> h2 via 10.0.1.0/24):\n")
    h1.cmdPrint('ping -c 2 -W 1 10.0.1.2')
    info("Testing Sky path (h1 -> h2 via 10.0.2.0/24):\n")
    h1.cmdPrint('ping -c 2 -W 1 10.0.2.2')
    info("Testing Reverse path (h2 -> h1 via 10.0.1.0/24):\n")
    h2.cmdPrint('ping -c 2 -W 1 10.0.1.1')
    info("Testing Reverse path (h2 -> h1 via 10.0.2.0/24):\n")
    h2.cmdPrint('ping -c 2 -W 1 10.0.2.1')

    stop_event = threading.Event()
    errors = []
    timing_state = {'frames_skipped': 0, 'max_update_lag_ms': 0.0}
    log_csv = os.path.join(cwd, "link_metrics.csv")
    jitter_csv = os.path.join(cwd, 'network_jitter_events.csv')
    sender_proc = receiver_proc = tail_proc = update_thread = None
    tc_updater = None
    send_fp = recv_fp = None
    status = 'failed'
    first = prepared_trace[0]
    links = (net.linksBetween(h1, s1)[0], net.linksBetween(h1, s3)[0])
    try:
        # 初始化失败时也不得把上一轮的更新日志归档成本轮数据。
        with open(log_csv, 'w'):
            pass
        with open(jitter_csv, 'w'):
            pass
        send_fp = open(os.path.join(cwd, "send.log"), 'w')
        recv_fp = open(os.path.join(cwd, "recv.log"), 'w')
        # 只在握手/视频负载开始之前替换初始队列，之后不再 del/replace。
        for link, bw, lat in ((links[0], first['ground_bw'], first['ground_lat']),
                              (links[1], first['sky_bw'], first['sky_lat'])):
            for intf in (link.intf1, link.intf2):
                safe_update_tc(intf, bw, f"{max(0.1, lat):.2f}ms", initialize=True)
        # Prepare/verify persistent tc before either media process can publish START.
        tc_updater = TCUpdater(links, backend=tc_backend)
        info("*** Starting Receiver ***\n")
        receiver_command = media['receiver_command'] if media else [receiver_bin, '12345', cert_file, key_file]
        receiver_proc = h2.popen(receiver_command,
                                 stdout=recv_fp, stderr=subprocess.STDOUT, cwd=cwd)
        time.sleep(1.0)
        if receiver_proc.poll() is not None:
            raise RuntimeError("Receiver exited during startup")
        info(f"*** Starting Sender: preempt={preempt_cfg}, gop={gop_size_cfg}, "
             f"scheduler={scheduler_cfg}, trace={video_trace} ***\n")
        sender_command = media['sender_command'] if media else [sender_bin, '10.0.1.2', '12345', video_trace,
                                                               preempt_cfg, gop_size_cfg, scheduler_cfg]
        sender_proc = h1.popen(sender_command,
                              stdout=send_fp, stderr=subprocess.STDOUT, cwd=cwd)
        update_thread = threading.Thread(target=dynamic_link_updater_with_jitter,
            args=(net, prepared_trace, frame_times, stop_event,
                  os.path.join(cwd, 'send.log'), errors, timing_state),
            kwargs={'log_file': log_csv, 'jitter_file': jitter_csv, 'tc_updater': tc_updater})
        update_thread.start()
        tail_proc = subprocess.Popen(['tail', '-f', os.path.join(cwd, 'send.log'),
                                      os.path.join(cwd, 'recv.log')])
        media_duration = media['config']['frames'] / media['config']['fps'] if media else frame_times[-1]
        timeout_grace = 150.0 if media else 120.0
        timeout = time.monotonic() + media_duration + timeout_grace
        while sender_proc.poll() is None:
            if errors:
                raise RuntimeError(errors[-1])
            if receiver_proc.poll() is not None and (not media or receiver_proc.returncode != 0):
                raise RuntimeError("Receiver exited before sender completed")
            if time.monotonic() >= timeout:
                raise RuntimeError(f"Sender exceeded video duration + {timeout_grace:.0f}s")
            time.sleep(0.1)
        if sender_proc.returncode != 0:
            raise RuntimeError(f"Sender exited with status {sender_proc.returncode}")
        if errors:
            raise RuntimeError(errors[-1])
        if media:
            # Both pipelines must finalize their pixels/index files before teardown.
            receiver_proc.wait(timeout=30)
            if receiver_proc.returncode != 0:
                raise RuntimeError(f'Receiver exited with status {receiver_proc.returncode}')
            media_errors = media_outputs_valid(media)
            if media_errors:
                raise RuntimeError('; '.join(media_errors))
        status = 'completed'
    except KeyboardInterrupt:
        status = 'interrupted'
    except Exception as exc:
        if str(exc) not in errors:
            errors.append(str(exc))
        info(f"*** Experiment failed: {exc} ***\n")
    finally:
        stop_event.set()
        if update_thread is not None:
            update_thread.join()
        if tc_updater is not None:
            try:
                tc_updater.close()
            except Exception as exc:
                errors.append(str(exc))
        # 对本次 Popen 进程发信号并 wait，接收端处理信号后释放 QUIC/转换 qlog。
        for proc, graceful_signal in ((sender_proc, signal.SIGTERM),
                                      (receiver_proc, signal.SIGINT),
                                      (tail_proc, signal.SIGTERM)):
            if proc is not None:
                if proc.poll() is None:
                    proc.send_signal(graceful_signal)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    errors.append(f"Forced kill of process {proc.pid}")
        for fp in (send_fp, recv_fp):
            if fp is not None:
                fp.close()
        with open(os.path.join(cwd, 'tc_state.log'), 'w') as fp:
            for link in links:
                for intf in (link.intf1, link.intf2):
                    fp.write(f"[{intf.name}]\n")
                    fp.write(intf.node.cmd(f'tc -s qdisc show dev {shlex.quote(intf.name)}'))
                    fp.write(intf.node.cmd(f'tc -s class show dev {shlex.quote(intf.name)}'))
        net.stop()
        if errors:
            status = 'failed'
        elif receiver_proc is not None and receiver_proc.returncode != 0:
            status = 'failed'
            errors.append(f"Receiver exited with status {receiver_proc.returncode}")
        intervals = [b - a for a, b in zip(frame_times, frame_times[1:]) if b > a]
        timing_limit_ms = min(intervals) * 1000 if intervals else 10.0
        if media:
            timing_limit_ms = min(timing_limit_ms, 1000 / media['config']['fps'])
        manifest = {'status': status, 'errors': errors,
                    'video_mode': video_mode,
                    'media_config': None if not media else media['config'],
                    'media_control_socket': None if not media else media['control_socket'],
                    'network_jitter_timing_trace': video_trace,
                    'tc_backend': tc_backend,
                    'jitter_calendar': {'events': len(frame_times), 'last_event_time_s': frame_times[-1],
                        'media_nominal_end_s': None if not media else media['config']['frames'] / media['config']['fps'],
                        'hold_after_last_event_s': None if not media else max(0.0, media['config']['frames'] / media['config']['fps'] - frame_times[-1]),
                        'end_policy': 'hold last jitter event; do not loop or extend the input calendar'},
                    'sender_exit': None if sender_proc is None else sender_proc.returncode,
                    'receiver_exit': None if receiver_proc is None else receiver_proc.returncode,
                    'preempt': preempt_cfg, 'gop_size': gop_size_cfg, 'scheduler': scheduler_cfg,
                    'random_seed': RANDOM_SEED, 'jitter_probability': JITTER_PROBABILITY,
                    'jitter_delays_ms': JITTER_DELAYS, 'video_trace': video_trace,
                    'clock': 'CLOCK_MONOTONIC', 'timing': timing_state,
                    'timing_limit_ms': timing_limit_ms,
                    'timing_valid': status == 'completed' and not errors and timing_state['frames_skipped'] == 0
                        and timing_state['max_update_lag_ms'] <= timing_limit_ms,
                    'input_sha256': {}}
        for path in (trace_file, video_trace, ground_thr_path, ground_lat_path,
                     sky_thr_path, sky_lat_path, sender_bin, receiver_bin, jitter_csv):
            with open(path, 'rb') as fp:
                digest = hashlib.sha256()
                for chunk in iter(lambda: fp.read(1048576), b''):
                    digest.update(chunk)
            manifest['input_sha256'][os.path.basename(path)] = digest.hexdigest()
        git_head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=cwd,
                                  capture_output=True, text=True)
        manifest['source_git_head'] = git_head.stdout.strip() if git_head.returncode == 0 else None
        manifest['qlog_counts'] = {directory: len([name for name in os.listdir(os.path.join(cwd, directory))
                                                    if name.endswith('.qlog')])
                                   if os.path.isdir(os.path.join(cwd, directory)) else 0
                                   for directory in ('qlog_sender', 'qlog_receiver')}
        with open(os.path.join(cwd, 'send.log')) as fp:
            send_text = fp.read()
        with open(os.path.join(cwd, 'recv.log')) as fp:
            recv_text = fp.read()
        origin = re.search(r'\[VIDEO_START\] monotonic_us=(\d+)', send_text)
        frame_generated = re.findall(r'\[FRAME_GEN\] frame=(\d+).*gen_ts=(\d+)', send_text)
        generation_times = media['frame_times'] if media else frame_times
        if origin:
            timing_state['frame_generation_lag_max_ms'] = max(
                [max(0.0, (int(ts) - int(origin[1])) / 1000 - generation_times[int(frame)] * 1000)
                 for frame, ts in frame_generated if int(frame) < len(generation_times)] or [0.0])
            if timing_state['frame_generation_lag_max_ms'] > timing_limit_ms:
                manifest['timing_valid'] = False
            if frame_generated:
                first_frame, first_ts = min(frame_generated, key=lambda item: int(item[0]))
                first_lag_us = int(first_ts) - int(origin[1]) - int(generation_times[int(first_frame)] * 1_000_000)
                timing_state['source_startup_lag_ms'] = first_lag_us / 1000
                timing_state['source_pacing_lag_max_ms'] = max(
                    [max(0.0, (int(ts) - int(origin[1]) - first_lag_us) / 1000
                         - generation_times[int(frame)] * 1000)
                     for frame, ts in frame_generated if int(frame) < len(generation_times)] or [0.0])
        else:
            status = manifest['status'] = 'failed'
            manifest['timing_valid'] = False
            errors.append('Missing VIDEO_START; sender binary/source mismatch')
        expected = {int(sid): int(size) for sid, size in re.findall(
            r'\[STREAM_MAP\] stream=(\d+).*stream_len=(\d+)', send_text)}
        receipts = re.findall(r'\[FRAME_RECV\].*stream=(\d+) total_pkts=(\d+) '
                              r'recv_pkts=(\d+) size=(\d+)', recv_text)
        valid = {int(sid) for sid, total, count, size in receipts
                 if expected.get(int(sid)) == int(size) and int(total) > 0 and total == count}
        manifest['frames'] = {'generated': len(re.findall(r'\[FRAME_GEN\]', send_text)),
                              'admitted': len(expected), 'complete': len(valid),
                              'missing': len(set(expected) - valid),
                              'invalid_completion_records': len(receipts) - len(valid),
                              'incomplete_fin': len(re.findall(r'\[FRAME_INCOMPLETE\]', recv_text))}
        manifest['congestion_algorithms'] = sorted(set(re.findall(r'\[CC\] algorithm=(\S+)', send_text + recv_text)))
        manifest['gstreamer_versions'] = re.findall(r'\[GSTREAMER\] version=(.*) source=\d+', send_text + recv_text)
        if media:
            manifest['media_output_errors'] = media_outputs_valid(media)
            manifest['media_output_valid'] = not manifest['media_output_errors'] and status == 'completed'
            if manifest['media_output_errors']:
                status = manifest['status'] = 'failed'
                errors.extend(manifest['media_output_errors'])
        if manifest['frames']['invalid_completion_records'] or manifest['frames']['incomplete_fin']:
            status = manifest['status'] = 'failed'
            manifest['timing_valid'] = False
            errors.append('Receiver reported invalid/incomplete application frames')
        if status == 'completed' and not all(manifest['qlog_counts'].values()):
            status = manifest['status'] = 'failed'
            manifest['timing_valid'] = False
            errors.append('Missing sender/receiver qlog after process shutdown')
        if media:
            manifest['media_output_valid'] = manifest['media_output_valid'] and status == 'completed'
        # Keep timing flags consistent if final frame/qlog validation failed.
        manifest['timing_valid'] = manifest['timing_valid'] and status == 'completed'
        manifest['network_replay_timing_valid'] = status == 'completed' and not errors and timing_state['frames_skipped'] == 0 and timing_state['max_update_lag_ms'] <= timing_limit_ms
        manifest['source_timing_valid'] = bool(origin) and status == 'completed' and timing_state.get('frame_generation_lag_max_ms', float('inf')) <= timing_limit_ms
        with open(os.path.join(cwd, 'run_manifest.json'), 'w') as fp:
            json.dump(manifest, fp, indent=2)
        archive_dir = archive_run(cwd, run_tag)
        archive_media(media, archive_dir)
        info(f"*** Experiment {status}; timing_valid={manifest['timing_valid']} ***\n")
    if status != 'completed':
        raise SystemExit(1)

if __name__ == '__main__':
    setLogLevel('info')
    run()


