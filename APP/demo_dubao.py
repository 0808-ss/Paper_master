#!/usr/bin/python3
import time
import os
import sys
import math
import shutil
import subprocess
import threading
import random
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.link import TCLink
from mininet.log import setLogLevel, info

# sudo mn -c
# sudo python3 demo_with_jitter.py

# ================= 配置参数 =================

# 随机种子，用于复现实验结果
RANDOM_SEED = 42
# 时延抖动概率 (0.0 ~ 1.0)
JITTER_PROBABILITY = 0.2
# 时延抖动增加量选项 (ms)
JITTER_DELAYS = [5, 10, 15]

# ================= I帧优先机制测试参数 =================
# 传给 video_sender 的传输层抢占配置 "enabled,L1,L2,L3,L4,burst"：
#   enabled: 运行期总开关（0 = 完全基线，机制不生效）
#   L1(包级抢占): I帧存在时整包只装I帧，BP帧完全让路
#   L2(流级选择): send_queue 头部为 I 帧的流优先
#   L3(路径级抢占): min-RTT 且 cwin 允许的路径优先给 I 帧
#   L4(cwin限幅突破): 每 RTT 最多 burst 个包突破拥塞窗口
#   burst: L4 每 RTT 突破配额（包数，0 = 关闭 L4）
# 可用环境变量 PREEMPT_CFG 覆盖（便于不修改代码跑消融实验）。
# 消融实验示例：
#   基线(机制关闭):  "0,1,1,1,1,0"
#   仅包级抢占:      "1,1,0,0,0,0"
#   全开(默认):      "1,1,1,1,1,4"
PREEMPT_CONFIG = "1,1,1,1,1,4"

# 实验归档标签：每次运行结束后把 qlog_sender/qlog_receiver/send.log/recv.log/
# link_metrics.csv 归档到 runs/<RUN_TAG>_<时间戳>/，避免消融对比时被下次运行覆盖。
# 可用环境变量 RUN_TAG 覆盖；不设置时自动按抢占配置命名。
RUN_TAG = ""

# 初始化随机种子
random.seed(RANDOM_SEED)

# ================= 数据读取与查询逻辑 =================

def load_video_trace(filepath):
    """读取视频文件以获取帧调度定时"""
    frame_times = []
    current_time = 0.0
    try:
        with open(filepath, 'r') as f:
            for line in f:
                if not line.strip(): continue
                parts = line.split(',')
                if len(parts) >= 2:
                    try:
                        current_time += float(parts[1])
                        frame_times.append(current_time)
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
        dist = math.sqrt((pt['x']-x)**2 + (pt['y']-y)**2 + (pt['z']-z)**2)
        if dist < min_dist:
            min_dist = dist
            best_thr = pt['thr']
            best_lat = pt['lat']

    best_thr = max(1.0, best_thr)
    return best_thr, best_lat

# ================= 平滑更新 TC 的辅助函数（终极通用版） =================

def safe_update_tc(intf, bw_mbps, delay_str):
    """
    终极通用TC更新函数，兼容所有Mininet版本
    不依赖任何硬编码的类ID，直接重建整个TC配置
    """
    node = intf.node
    ifname = intf.name
    
    # 第一步：完全删除接口上所有现有的TC配置
    node.cmd(f'tc qdisc del dev {ifname} root 2>/dev/null || true')
    
    # 第二步：从头创建我们自己的标准TC配置
    # 根qdisc: HTB
    node.cmd(f'tc qdisc add dev {ifname} root handle 1: htb default 1')
    # 主类: 限制带宽
    node.cmd(f'tc class add dev {ifname} parent 1: classid 1:1 htb rate {bw_mbps}mbit ceil {bw_mbps}mbit')
    # 附加netem: 添加延迟和抖动
    node.cmd(f'tc qdisc add dev {ifname} parent 1:1 handle 10: netem delay {delay_str}')

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

def dynamic_link_updater_with_jitter(net, trace, ground_grid, sky_grid, frame_times, stop_event,
                                      granularity=0.01, jitter_prob=JITTER_PROBABILITY,
                                      jitter_delays=JITTER_DELAYS, log_file=None):
    """定期更新底层链路参数，并在帧调度时添加随机时延抖动"""
    info("*** Starting Dynamic Link Updater with Jitter (Frame-triggered) ***\n")
    info(f"*** Random Seed: {RANDOM_SEED}, Jitter Probability: {jitter_prob}\n")

    start_time = time.time()

    # 打开数据记录文件
    log_fp = None
    if log_file:
        log_fp = open(log_file, 'w')
        log_fp.write("time,ground_bw,ground_lat,ground_jitter,sky_bw,sky_lat,sky_jitter\n")

    h1 = net.get('h1')
    s1, s2 = net.get('s1'), net.get('s2')
    s3, s4 = net.get('s3'), net.get('s4')
    link1 = net.linksBetween(h1, s1)[0]
    link2 = net.linksBetween(h1, s3)[0]

    last_g_thr, last_g_lat = -1, -1
    last_s_thr, last_s_lat = -1, -1

    # 抖动状态：记录当前是否处于抖动状态及额外延迟
    g_jitter_extra = 0
    s_jitter_extra = 0

    while not stop_event.is_set():
        elapsed = time.time() - start_time

        # 检查是否触发帧调度
        frame_triggered = False
        frames_popped = 0
        while frame_times and elapsed >= frame_times[0]:
            frame_triggered = True
            frames_popped += 1
            frame_times.pop(0)

        # 查找当前轨迹点
        current_pt = trace[-1]
        for pt in trace:
            if pt['time'] >= elapsed:
                current_pt = pt
                break

        # 获取基础延迟值
        g_thr, g_lat_base = get_nearest_values(ground_grid, current_pt['x'], current_pt['y'], current_pt['z'])
        s_thr, s_lat_base = get_nearest_values(sky_grid, current_pt['x'], current_pt['y'], current_pt['z'])

        # === 时延抖动逻辑 ===
        # 在帧调度时计算是否发生抖动，如果发生则在当前时间段应用抖动
        if frame_triggered:
            # 严格按照弹出的帧数生成随机数，隔离线程调度的影响，保证每次实验随机序列一致
            for _ in range(frames_popped):
                if random.random() < jitter_prob:
                    g_jitter_extra = random.choice(jitter_delays)
                else:
                    g_jitter_extra = 0

                if random.random() < jitter_prob:
                    s_jitter_extra = random.choice(jitter_delays)
                else:
                    s_jitter_extra = 0
                    
            if g_jitter_extra > 0:
                info(f"[JITTER] Ground path delay +{g_jitter_extra}ms\n")
            if s_jitter_extra > 0:
                info(f"[JITTER] Sky path delay +{s_jitter_extra}ms\n")

        # 计算实际延迟值
        g_lat = g_lat_base + g_jitter_extra
        s_lat = s_lat_base + s_jitter_extra

        g_lat_str = f"{g_lat:.2f}ms" if g_lat > 0 else "0.1ms"
        s_lat_str = f"{s_lat:.2f}ms" if s_lat > 0 else "0.1ms"

        try:
            updated = False
            if abs(g_thr - last_g_thr) > 0.1 or abs(g_lat - last_g_lat) > 0.1:
                safe_update_tc(link1.intf1, g_thr, g_lat_str)
                safe_update_tc(link1.intf2, g_thr, g_lat_str)
                last_g_thr, last_g_lat = g_thr, g_lat
                updated = True

            if abs(s_thr - last_s_thr) > 0.1 or abs(s_lat - last_s_lat) > 0.1:
                safe_update_tc(link2.intf1, s_thr, s_lat_str)
                safe_update_tc(link2.intf2, s_thr, s_lat_str)
                last_s_thr, last_s_lat = s_thr, s_lat
                updated = True

            if updated:
                jitter_g_info = f" (base {g_lat_base:.2f}ms + jitter {g_jitter_extra}ms)" if g_jitter_extra > 0 else ""
                jitter_s_info = f" (base {s_lat_base:.2f}ms + jitter {s_jitter_extra}ms)" if s_jitter_extra > 0 else ""
                info(f"[{elapsed:.2f}s] Link Updated at Pos:({current_pt['x']:.1f}, {current_pt['y']:.1f}, {current_pt['z']:.1f})\n"
                     f"    -> Ground : BW={g_thr:.2f}Mbps, Latency={g_lat:.2f}ms{jitter_g_info}\n"
                     f"    -> Sky    : BW={s_thr:.2f}Mbps, Latency={s_lat:.2f}ms{jitter_s_info}\n")

            # 记录数据到CSV
            if log_fp:
                log_fp.write(f"{elapsed:.3f},{g_thr:.2f},{g_lat_base:.2f},{g_jitter_extra},"
                            f"{s_thr:.2f},{s_lat_base:.2f},{s_jitter_extra}\n")
                log_fp.flush()

        except Exception as e:
            info(f"Error updating link: {e}\n")

        if elapsed > trace[-1]['time'] + granularity:
            pass

        time.sleep(granularity)

    if log_fp:
        log_fp.close()

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
                          ("link_metrics.csv", "link_metrics.csv")):
        p = os.path.join(cwd, src)
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(run_dir, dst_name), dirs_exist_ok=True)
        elif os.path.exists(p):
            shutil.copy2(p, os.path.join(run_dir, dst_name))
    info(f"*** Run archived to {run_dir} ***\n")

def run():

    # sudo mn -c
    # sudo python3 demo_with_jitter.py
    cwd = os.path.dirname(os.path.abspath(__file__))

    trace_file = os.path.join(cwd, "Travel.csv")
    ground_thr_path = os.path.join(cwd, "p2p_data/summary_Throughput_ground.tsv")
    ground_lat_path = os.path.join(cwd, "p2p_data/summary_Latency_ground.tsv")
    sky_thr_path = os.path.join(cwd, "p2p_data/summary_Throughput_sky.tsv")
    sky_lat_path = os.path.join(cwd, "p2p_data/summary_Latency_sky.tsv")

    cert_file = os.path.join(cwd, "cert.pem")
    key_file = os.path.join(cwd, "key.pem")
    receiver_bin = os.path.join(cwd, "video_receiver")
    sender_bin = os.path.join(cwd, "video_sender")

    if not os.path.exists(trace_file):
        sys.exit(f"Error: {trace_file} does not exist.")
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        sys.exit(f"Error: Certificates not found in {cwd}")
    if not os.path.exists(receiver_bin) or not os.path.exists(sender_bin):
        sys.exit("Error: video_sender/video_receiver not built.")

    # 每次启动前清理上次运行残留的 qlog，避免新旧日志混在一起
    cleanup_qlog(cwd)

    info("*** Loading Data Files ***\n")
    trace_data = load_trace(trace_file)
    ground_grid = load_grid_data(ground_thr_path, ground_lat_path)
    sky_grid = load_grid_data(sky_thr_path, sky_lat_path)

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

    info("*** Starting Receiver ***\n")
    h2.cmd(f'cd {cwd} && rm -f recv.log')
    h2.popen(f'{receiver_bin} 12345 {cert_file} {key_file} > recv.log 2>&1', shell=True, cwd=cwd)

    time.sleep(1.0)  # 增加启动延迟，确保接收端完全就绪

    stop_event = threading.Event()

    info("*** Starting Sender ***\n")
    h1.cmd(f'cd {cwd} && rm -f send.log')

    video_trace = os.path.join(cwd, "4k_30f_trace_0.8.csv")
    # video_trace = os.path.join(cwd, "output.csv")

    info("*** Loading Video Frame Trace ***\n")
    frame_times = load_video_trace(video_trace)

    # I帧优先机制测试：抢占配置透传给 video_sender（第 4 个参数），
    # 支持环境变量 PREEMPT_CFG 覆盖，便于消融实验不修改代码。
    preempt_cfg = os.environ.get("PREEMPT_CFG", PREEMPT_CONFIG)
    info(f"*** Preemption Config: {preempt_cfg} ***\n")
    h1.popen(f'{sender_bin} 10.0.1.2 12345 {video_trace} {preempt_cfg} > send.log 2>&1', shell=True, cwd=cwd)

    # 启动带抖动的链路更新线程
    log_csv = os.path.join(cwd, "link_metrics.csv")
    update_thread = threading.Thread(
        target=dynamic_link_updater_with_jitter,
        args=(net, trace_data, ground_grid, sky_grid, frame_times, stop_event, 0.01, JITTER_PROBABILITY, JITTER_DELAYS, log_csv)
    )
    update_thread.start()

    info("*** Tailing Logs (Press Ctrl+C to Stop) ***\n")
    try:
        subprocess.run(["tail", "-f", os.path.join(cwd, "send.log"), os.path.join(cwd, "recv.log")])
    except KeyboardInterrupt:
        pass
    finally:
        info("\n*** Stopping ***\n")
        stop_event.set()
        if update_thread.is_alive():
            update_thread.join()
        h1.cmd('killall -q video_sender || true')
        h2.cmd('killall -q video_receiver || true')
        net.stop()
        
        info("*** Generating Plot ***\n")
        plot_img = os.path.join(cwd, "link_metrics_plot.png")
        plot_metrics(log_csv, plot_img)

        # I帧优先机制测试：归档本次运行产物（消融对比用）
        run_tag = os.environ.get("RUN_TAG", None)
        if run_tag is None:
            run_tag = ("cfg_" + preempt_cfg.replace(",", "_")
                       + "_" + time.strftime("%Y%m%d_%H%M%S"))
        archive_run(cwd, run_tag)

if __name__ == '__main__':
    setLogLevel('info')
    run()


