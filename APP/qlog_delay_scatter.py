# -*- coding: utf-8 -*-
"""
qlog_delay_scatter.py — 依据 qlog 直接生成帧延迟 / 包延迟散点图

本脚本完全只依赖 qlog，不需要 send.log / recv.log：
  - qlog_sender/*.qlog     发送端日志（packet_sent 事件，含 stream frame 归因）
  - qlog_receiver/*.qlog   接收端日志（packet_received 事件）

口径说明：
  1. 帧 = 一个 QUIC stream。本实验中每帧视频数据使用独立 stream（id 步进 4），
     因此按 stream id 分组即得帧级数据。
  2. 包匹配键 = (path_id, packet_number)。picoquic 的包号在每条路径上独立编号，
     必须联合 path_id 才能唯一匹配发送端 packet_sent 与接收端 packet_received。
  3. 两端 qlog 的 reference_time 基准不同。自动对齐方式：
       offset = min_over_matched(recv_rel - send_rel) - min_owd_us
     取所有匹配包中 (接收相对时间 - 发送相对时间) 的最小值作为时钟偏移基线；
     若已知链路真实最小单向延迟，用 --min-owd-us 校正（默认 0）。
  4. 包延迟 = (接收相对时间 - 发送相对时间) - offset。
  5. 帧延迟 = 帧最后一个包到达时间 - 帧第一个包发送时间（同一基准下）。

路径标记（默认，可用 --path-labels 或 make_path_labels() 动态调整）：
  picoquic path 0            -> "Path 1"（蓝）
  picoquic path 1 + path 2   -> "Path 2"（红，合并展示）
  即：原始 path 0 标记为 Path 1；原始 path 1 与 path 2 合并标记为 Path 2。
  帧/包按展示标签着色与图例；CSV 的路径计数列按展示标签合并。

× 离群标记（可动态调整，公开 API）：
  make_outlier_rule(cap_ms=…, pct=…, per_path_ms=…, custom=fn, enabled=…)
  classify_outliers(packets, frames, rule, paths)
      -> (norm_packets, out_packets, norm_frames, out_frames, all_y)
  plot_combined(…, outlier_rule=rule, …)
  命令行等价参数：--outlier-rule cap:150 | pct:99 | path:0:50,1:100 | none
  详见各函数 docstring。

输出（单图）：
  qlog_frame_packet_delay_scatter.png    帧延迟 + 包延迟合并散点（单图）
                                         - 包=小圆点；帧=方块；I 帧=菱形（黑色描边）
                                         - 纵轴为对数刻度，下界默认自适应（取正延迟 p5
                                           分位，保证最多丢弃 5% 的数据点）
                                         - 极端离群值不拉伸纵轴，改在纵轴顶部用 × 标注
                                           （保留真实发送时刻与路径颜色；离群判定规则
                                           由 --outlier-rule / make_outlier_rule 指定）
                                         - 图例显示在主图右侧（与数据区分离，不遮挡）
  qlog_frame_delay.csv                  帧级明细（路径计数列按展示标签合并：
                                         path1_pkts=旧 path0、path2_pkts=旧 path1+2；
                                         main_path 为展示标签；用
                                         --path-labels 0:0,1:1,2:2 可回到原始编号列）
  qlog_packet_delay.csv                 包级明细（path_id 为 qlog 原始路径号，
                                         另附 path_label 展示标签列）

用法：
  python qlog_delay_scatter.py [--sender 发送端.qlog]
                               [--receiver 接收端.qlog]
                               [--outdir 输出目录]
                               [--min-owd-us 0]
                               [--paths 0,2]
                               [--iframe-pkts 30]
                               [--y-floor-ms 0]
                               [--y-cap-ms 100]
                               [--outlier-rule cap:100]
                               [--path-labels 0:1,1:2,2:2]
  不带 --sender/--receiver 时，自动在脚本所在目录查找
  qlog_sender/*.qlog 与 qlog_receiver/*.qlog。

  python qlog_delay_scatter.py --outlier-rule cap:150

"""

import argparse
import csv
import glob
import json
import os
import re
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D

# ============================================================
# SCI 学术图表样式（与项目内 statics.py / trend.py 一致）
# ============================================================
plt.rcParams["font.family"] = "serif"
plt.rcParams["font.serif"] = ["Times New Roman", "DejaVu Serif"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["mathtext.fontset"] = "stix"
plt.rcParams["xtick.direction"] = "in"
plt.rcParams["ytick.direction"] = "in"
plt.rcParams["xtick.top"] = True
plt.rcParams["ytick.right"] = True
plt.rcParams["axes.linewidth"] = 1.2
plt.rcParams["figure.dpi"] = 300
plt.rcParams["savefig.dpi"] = 300
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.alpha"] = 0.4
plt.rcParams["grid.linestyle"] = "--"
plt.rcParams["grid.linewidth"] = 0.6
plt.rcParams["axes.labelsize"] = 20
plt.rcParams["axes.titlesize"] = 20
plt.rcParams["xtick.labelsize"] = 16
plt.rcParams["ytick.labelsize"] = 16
plt.rcParams["legend.fontsize"] = 14

# 帧 / 包的形状区分：包=小圆点，普通帧=方块，I 帧=菱形（黑色描边）
PKT_MARKER = "o"
FRAME_MARKER = "s"
IFRAME_MARKER = "D"


def fmt_ms(v, _pos=None):
    """对数纵轴刻度格式化：统一普通十进制（10/1/0.1），避免小值被显示成 0。"""
    if v >= 1e5 or v < 1e-4:
        return "%.0e" % v
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


# ============================================================
# 路径重标记 API（path0 -> Path 1；path1 + path2 -> Path 2）
# ============================================================

# 默认标记（用户最新要求）：
#   picoquic 原始 path 0            -> 展示 "Path 1"
#   picoquic 原始 path 1、path 2    -> 合并展示 "Path 2"
DEFAULT_PATH_LABELS = {0: "Path 1", 1: "Path 2", 2: "Path 2"}

# 展示标签 -> 颜色（按分组顺序分配）
LABEL_PALETTE = ["#4C72B0", "#C44E52", "#55A868", "#CCB974",
                 "#8172B3", "#64B5CD", "#DD8452", "#937860"]


def _norm_label(lab):
    """标签归一化：纯数字标签自动加 'Path ' 前缀（"1" -> "Path 1"）。"""
    lab = str(lab).strip()
    return "Path %s" % lab if lab.isdigit() else lab


def make_path_labels(spec=None):
    """构建 原始 path_id -> 展示标签 的映射（公开 API，可动态调整）。

    spec:
      - None              : 使用默认标记 {0:"Path 1", 1:"Path 2", 2:"Path 2"}
                            （path0 标记为 Path 1；path1+path2 合并标记为 Path 2）
      - dict              : {raw_path_id: 展示标签}，例如 {0:"A", 1:"B", 2:"B"}；
                            纯数字标签自动加 "Path " 前缀
      - 字符串 "0:1,1:2,2:2" : 逗号分隔 raw:label，含义同上（"0:0,1:1,2:2" 即原始编号）

    未出现在映射中的原始路径在渲染/CSV 阶段回退为 "Path <raw>"。
    """
    if spec is None:
        return dict(DEFAULT_PATH_LABELS)
    if isinstance(spec, dict):
        return {int(k): _norm_label(v) for k, v in spec.items()}
    if isinstance(spec, str):
        spec = spec.strip()
        if not spec:
            return dict(DEFAULT_PATH_LABELS)
        labels = {}
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            raw, _, lab = item.partition(":")
            raw = int(raw.strip())
            lab = lab.strip()
            if not lab:
                raise ValueError("--path-labels 条目缺少标签: %r" % item)
            labels[raw] = _norm_label(lab)
        return labels
    raise TypeError("path labels spec 必须是 dict 或字符串")


def label_token(label):
    """展示标签 -> CSV 列名 token："Path 1" -> "path1"。"""
    return re.sub(r"\s+", "", label).lower()


def ordered_labels(labels):
    """按分组内最小原始 path_id 排序去重，得到展示标签顺序（颜色按此分配）。"""
    order = {}
    for raw, lab in labels.items():
        if lab not in order or raw < order[lab]:
            order[lab] = raw
    return sorted(order, key=order.get)


def label_color(label, ordered):
    """按标签在 ordered 中的位置分配调色板颜色。"""
    return LABEL_PALETTE[ordered.index(label) % len(LABEL_PALETTE)]


def label_of(raw_path_id, labels):
    """原始 path_id -> 展示标签（未映射时回退 "Path <raw>"）。"""
    lab = labels.get(raw_path_id)
    return lab if lab is not None else "Path %d" % raw_path_id


# ============================================================
# × 离群标记 API（可动态调整判叉数据）
# ============================================================

def make_outlier_rule(cap_ms=None, pct=None, per_path_ms=None,
                      custom=None, enabled=True):
    """构建 × 离群判定规则（公开 API）。

    四种规则互斥，优先级：custom > per_path_ms > pct > cap_ms。
      cap_ms      : float，固定阈值(ms)；delay > cap_ms 判为离群（兼容 --y-cap-ms）
      pct         : float，0 < pct < 100；取全部正延迟的该百分位数为阈值
      per_path_ms : dict {raw_path_id: ms}；按路径分别指定阈值，未指定路径不判离群
      custom      : callable fn(delay_ms, raw_path_id, kind) -> bool，
                    kind ∈ {"packet", "frame"}；返回 True 判为离群
      enabled     : False 时任何数据都不标 ×（等效 --outlier-rule none）

    返回 rule 字典，可直接传给 classify_outliers() / plot_combined(outlier_rule=rule)。
    示例：
      rule = make_outlier_rule(cap_ms=150)
      rule = make_outlier_rule(pct=99.5)
      rule = make_outlier_rule(per_path_ms={0: 100, 1: 150, 2: 150})
      rule = make_outlier_rule(custom=lambda y, p, k: k == "frame" and y > 200)
    """
    if custom is not None:
        mode = "custom"
    elif per_path_ms:
        mode = "per_path"
    elif pct is not None:
        mode = "pct"
    else:
        mode = "cap"
        if cap_ms is None:
            cap_ms = 100.0
    return {"mode": mode, "enabled": bool(enabled),
            "cap_ms": cap_ms, "pct": pct, "per_path_ms": per_path_ms,
            "custom": custom}


def parse_outlier_rule(text):
    """命令行字符串 -> rule 字典（公开 API）。

    支持格式：
      "cap:150"           固定阈值 150ms
      "pct:99"            全部正延迟的 99 分位
      "path:0:50,1:100"   按路径阈值 {0: 50, 1: 100}
      "none"              不标 ×
      纯数字 "150"         等价 "cap:150"
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    low = text.lower()
    if low == "none":
        return make_outlier_rule(enabled=False)
    if low.startswith("cap:"):
        return make_outlier_rule(cap_ms=float(low[4:]))
    if low.startswith("pct:"):
        pct = float(low[4:])
        if not 0 < pct < 100:
            raise ValueError("pct 必须在 (0,100) 内: %r" % text)
        return make_outlier_rule(pct=pct)
    if low.startswith("path:"):
        per = {}
        for item in low[5:].split(","):
            parts = item.split(":")
            if len(parts) != 2:
                raise ValueError("path 规则格式应为 path:raw:ms,... : %r" % text)
            per[int(parts[0])] = float(parts[1])
        return make_outlier_rule(per_path_ms=per)
    try:
        return make_outlier_rule(cap_ms=float(text))
    except ValueError:
        raise ValueError("无法识别的 --outlier-rule: %r"
                         "（支持 cap:ms / pct:P / path:raw:ms,... / none）" % text)


def rule_desc(rule):
    """rule 的简短描述（用于图例与终端输出）。"""
    if not rule or not rule.get("enabled"):
        return "off"
    m = rule["mode"]
    if m == "cap":
        return "y > %.4g ms" % rule["cap_ms"]
    if m == "pct":
        return "y > P%.4g" % rule["pct"]
    if m == "per_path":
        return ",".join("p%d>%.4g" % (k, v)
                        for k, v in sorted(rule["per_path_ms"].items()))
    if m == "custom":
        return "custom rule"
    return "?"


def _quantile(sorted_vals, q):
    """排序列表的线性分位数（q ∈ [0,1]）。"""
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _effective_cap(rule, all_y):
    """rule 的参考阈值(ms)（用于离群分界线与顶部离群带）；custom 规则返回 None。"""
    if not rule or not rule.get("enabled"):
        return None
    m = rule["mode"]
    if m == "cap":
        return rule["cap_ms"]
    if m == "pct":
        return _quantile(sorted(all_y), rule["pct"] / 100.0)
    if m == "per_path":
        return max(rule["per_path_ms"].values())
    return None


def _is_outlier(rule, y_ms, path_id, kind):
    if not rule or not rule.get("enabled"):
        return False
    m = rule["mode"]
    if m == "cap":
        return y_ms > rule["cap_ms"]
    if m == "pct":
        return y_ms > rule["_cap_effective"]
    if m == "per_path":
        c = rule["per_path_ms"].get(path_id)
        return c is not None and y_ms > c
    if m == "custom":
        return bool(rule["custom"](y_ms, path_id, kind))
    return False


def classify_outliers(packets, frames, rule=None, paths=None):
    """公开 API：按 rule 把包/帧分为正常与离群（最终画 ×）两组。

    参数：
      packets : 包级 dict 列表（含 path_id / delay_us / send_rel 等字段）
      frames  : 帧级 dict 列表（含 main_path / frame_delay_us / send_first_us 等）
      rule    : make_outlier_rule() 的返回值；None 时默认 cap:100ms
      paths   : 参与判定的原始 path_id 列表；None 表示全部

    返回 (norm_packets, out_packets, norm_frames, out_frames, all_y)：
      out_packets / out_frames 即会被绘制成 × 的数据；
      all_y 为全部正延迟(ms)列表（含 pct 规则的分位数计算基准）。
    外部脚本可据此结合不同 rule 动态调整叉标记后调用 plot_combined 重画。
    """
    if rule is None:
        rule = make_outlier_rule()
    if paths is None:
        paths = sorted({pk["path_id"] for pk in packets} |
                       {fr["main_path"] for fr in frames})

    all_y = []
    for p in paths:
        all_y += [pk["delay_us"] / 1e3 for pk in packets
                  if pk["path_id"] == p and pk["delay_us"] is not None
                  and pk["delay_us"] > 0]
    for fr in frames:
        if fr["main_path"] in paths and fr["frame_delay_us"] > 0:
            all_y.append(fr["frame_delay_us"] / 1e3)

    # pct 规则：先算好阈值，再统一判定
    if rule.get("mode") == "pct":
        rule["_cap_effective"] = _quantile(sorted(all_y), rule["pct"] / 100.0)

    norm_p, out_p = [], []
    for pk in packets:
        if pk["path_id"] not in paths or pk["delay_us"] is None or pk["delay_us"] <= 0:
            continue
        (out_p if _is_outlier(rule, pk["delay_us"] / 1e3, pk["path_id"], "packet")
         else norm_p).append(pk)
    norm_f, out_f = [], []
    for fr in frames:
        if fr["main_path"] not in paths or fr["frame_delay_us"] <= 0:
            continue
        (out_f if _is_outlier(rule, fr["frame_delay_us"] / 1e3,
                              fr["main_path"], "frame")
         else norm_f).append(fr)
    return norm_p, out_p, norm_f, out_f, all_y


# ============================================================
# qlog 读取与解析
# ============================================================

def find_qlog(base_dir, subdir):
    """在 base_dir/subdir 下查找 *.qlog，返回排序后的路径列表。"""
    d = os.path.join(base_dir, subdir)
    if not os.path.isdir(d):
        return []
    return sorted(glob.glob(os.path.join(d, "*.qlog")))


def load_qlog(path):
    """读取 qlog 文件，返回 (reference_time, events)。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        raw = json.load(f)
    traces = raw.get("traces") or []
    if not traces:
        raise ValueError("qlog 无 traces: %s" % path)
    tr = traces[0]
    ref = int((tr.get("common_fields") or {}).get("reference_time", 0) or 0)
    return ref, tr.get("events") or []


def load_class_map(send_log):
    """I帧优先机制测试：从 send.log 解析发送端显式标记的帧类。

    video_sender 在每帧入流时打印 [STREAM_MAP] stream=X frame=Y class=I|BP，
    这里建立 stream_id -> is_iframe 映射，替代包数启发式判定
    （原启发式在 4k 轨迹下会把 P 帧误判为 I 帧）。
    找不到 send.log 时返回空映射（回退到 --iframe-pkts 启发式）。
    """
    cmap = {}
    if not send_log or not os.path.exists(send_log):
        return cmap
    with open(send_log, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = re.search(r"\[STREAM_MAP\] stream=(\d+).*class=(\S+)", line)
            if m:
                cmap[int(m.group(1))] = (m.group(2).upper() == "I")
    if cmap:
        print("class map from send.log: %d streams tagged" % len(cmap))
    return cmap


def parse_1rtt_packets(events, want_event):
    """
    取出所有 1RTT 的 packet_sent / packet_received 事件。

    返回 dict {(path_id, packet_number): (rel_time, data)}
    """
    out = {}
    for ev in events:
        if not isinstance(ev, list) or len(ev) < 5:
            continue
        path_id, category, event, data = ev[1], ev[2], ev[3], ev[4]
        if category != "transport" or event != want_event:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("packet_type") != "1RTT":
            continue
        pn = (data.get("header") or {}).get("packet_number")
        if pn is None:
            continue
        out[(int(path_id), int(pn))] = (int(ev[0]), data)
    return out


def extract_stream_frames(data):
    """从包 data 中提取 stream frame 列表 [(stream_id, offset, length, fin)]。"""
    frames = []
    for f in data.get("frames") or []:
        if not isinstance(f, dict):
            continue
        if f.get("frame_type") != "stream":
            continue
        frames.append((f.get("id"), f.get("offset", 0), f.get("length", 0),
                       f.get("fin", False)))
    return frames


# ============================================================
# 绘图
# ============================================================

def plot_combined(packets, frames, paths, out_path, y_floor_ms=0.0,
                  outlier_rule=None, y_cap_ms=None, path_labels=None):
    """
    单图：帧延迟 + 包延迟合并散点。
      - x=发送时刻(s)，y=延迟(ms)，纵轴为对数刻度（log10）
      - 包=小圆点（底层）；帧=方块（上层，黑色细边）；I 帧=菱形（黑色粗边）
      - 路径标记默认 path0->"Path 1"（蓝）、path1+path2->"Path 2"（红），
        可用 path_labels（dict 或 "0:1,1:2,2:2" 字符串）动态调整
      - 纵轴下界：y_floor_ms<=0 时自动取全部正延迟的 p5 分位作下界
      - 离群（×）判定：outlier_rule（make_outlier_rule() 返回值 / 字符串 / None）；
        兼容旧参数 y_cap_ms（outlier_rule 为 None 时生效）
      - 图例显示在主图右侧（bbox_to_anchor 移到坐标轴外），与数据区分离
    """
    labels = make_path_labels(path_labels)
    if outlier_rule is None:
        outlier_rule = (make_outlier_rule(cap_ms=y_cap_ms)
                        if y_cap_ms is not None else make_outlier_rule())
    elif isinstance(outlier_rule, str):
        outlier_rule = parse_outlier_rule(outlier_rule)
    ordered = ordered_labels(labels)

    fig, ax = plt.subplots(figsize=(13, 6.5))

    # ---- 离群判定（公开 API，可动态调整）----
    norm_p, out_p, norm_f, out_f, all_y = classify_outliers(
        packets, frames, outlier_rule, paths)

    if not all_y:
        print("note: no positive delay to plot")
        fig.savefig(out_path)
        plt.close(fig)
        return

    # ---- 离群分界：由 rule 决定（固定阈值 / 分位数 / 按路径 / 自定义）----
    cap = _effective_cap(outlier_rule, all_y)
    have_outliers = bool(out_p or out_f)
    # 有离群且存在数值阈值时：纵轴上界放宽到 cap 的 1.6 倍，× 画在顶部离群带
    top = cap * 1.6 if (have_outliers and cap is not None) else max(all_y) * 2.0
    outlier_y = top * 0.88

    # ---- 底层：正常包（小圆点，半透明；按展示标签着色）----
    for p in sorted({pk["path_id"] for pk in norm_p}):
        sel = [pk for pk in norm_p if pk["path_id"] == p]
        ax.scatter([pk["send_rel"] / 1e6 for pk in sel],
                   [pk["delay_us"] / 1e3 for pk in sel],
                   s=11, alpha=0.5, marker=PKT_MARKER,
                   color=label_color(label_of(p, labels), ordered),
                   linewidths=0)
    # ---- 离群包 → ×（保留真实发送时刻与路径颜色）----
    for pk in out_p:
        ax.scatter(pk["send_rel"] / 1e6, outlier_y,
                   s=85, alpha=0.95, marker="x",
                   color=label_color(label_of(pk["path_id"], labels), ordered),
                   linewidths=1.7, zorder=6)

    # ---- 上层：正常帧（方块；I 帧=菱形黑色描边）----
    for fr in norm_f:
        x = fr["send_first_us"] / 1e6
        y = fr["frame_delay_us"] / 1e3
        color = label_color(label_of(fr["main_path"], labels), ordered)
        if fr["is_iframe"]:
            ax.scatter(x, y, s=60, alpha=0.95, marker=IFRAME_MARKER,
                       color=color, edgecolors="#111111", linewidths=1.1, zorder=5)
        else:
            ax.scatter(x, y, s=34, alpha=0.9, marker=FRAME_MARKER,
                       color=color, edgecolors="#111111", linewidths=0.4, zorder=4)
    # ---- 离群帧 → × ----
    for fr in out_f:
        ax.scatter(fr["send_first_us"] / 1e6, outlier_y, s=95,
                   marker="x",
                   color=label_color(label_of(fr["main_path"], labels), ordered),
                   linewidths=1.9, zorder=7)

    # ---- 离群分界线（虚线，提示上方为压缩的离群带）----
    if have_outliers and cap is not None:
        ax.axhline(cap, color="#999999", linestyle="--", linewidth=0.8, alpha=0.6)

    # ---- 图例条目（绘制在主图右侧，与数据区分离）----
    shown = {label_of(p, labels) for p in paths}
    legend_groups = [(lab, label_color(lab, ordered)) for lab in ordered
                     if lab in shown]

    handles, labels_ = [], []
    for lab, color in legend_groups:
        handles.append(Line2D([0], [0], marker=PKT_MARKER, linestyle="none",
                              markersize=7, markerfacecolor=color,
                              markeredgecolor="none",
                              label="Packet \u00b7 %s" % lab))
    for lab, color in legend_groups:
        handles.append(Line2D([0], [0], marker=FRAME_MARKER, linestyle="none",
                              markersize=7, markerfacecolor=color,
                              markeredgecolor="#111111", markeredgewidth=0.6,
                              label="Frame \u00b7 %s" % lab))
    handles.append(Line2D([0], [0], marker=IFRAME_MARKER, linestyle="none",
                          markersize=8, markerfacecolor="#888888",
                          markeredgecolor="#111111", markeredgewidth=1.2,
                          label="I-frame"))
    if have_outliers:
        handles.append(Line2D([0], [0], marker="x", linestyle="none",
                              markersize=9, markeredgecolor="#333333",
                              markeredgewidth=1.8,
                              label="Outlier \u00d7 (%s)" % rule_desc(outlier_rule)))
    labels_ = [h.get_label() for h in handles]

    ax.set_xlabel("Send time (s)")
    ax.set_ylabel("Delay (ms)")
    ax.set_title("Frame & Packet Delay Scatter (qlog, by path)")

    # ---- 纵坐标改为对数刻度（避免离群值把主体压缩到看不清）----
    ax.set_yscale("log")
    # 主刻度：十进制定位 + 普通十进制标签（10 / 1 / 0.1 ...）
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10.0))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_ms))
    # 次刻度（2、3、5 倍）只画网格不标数字
    ax.yaxis.set_minor_locator(mticker.LogLocator(base=10.0, subs=(2, 3, 5),
                                                  numticks=20))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.grid(True, which="major", axis="y", alpha=0.4,
            linestyle="--", linewidth=0.6)
    ax.grid(True, which="minor", axis="y", alpha=0.18,
            linestyle=":", linewidth=0.5)

    # ---- 纵轴范围 ----
    # 下界：y_floor_ms<=0 时自适应取正延迟 p5（保证最多丢弃 5% 的点）；
    #      >0 时作为固定下界（丢弃比例由用户负责）
    if y_floor_ms and y_floor_ms > 0:
        lo = max(min(all_y) / 2.0, y_floor_ms)
        floor_desc = "fixed %.4g ms" % lo
    else:
        sy_all = sorted(all_y)
        lo = sy_all[min(int(len(sy_all) * 0.05), len(sy_all) - 1)]
        floor_desc = "auto p5 %.4g ms" % lo
    ax.set_ylim(lo, top)
    n_clip = sum(1 for v in all_y if v < lo)
    if n_clip:
        print("note: %d/%d (%.1f%%) point(s) below y-axis floor (%s) clipped"
              % (n_clip, len(all_y), 100.0 * n_clip / len(all_y), floor_desc))
    if have_outliers:
        print("note: %d/%d (%.1f%%) outlier(s) drawn as \u00d7 in top band"
              % (len(out_p) + len(out_f), len(all_y),
                 100.0 * (len(out_p) + len(out_f)) / len(all_y)))

    # ---- 图例：显示在主图右侧（bbox_to_anchor 移到坐标轴外，与数据区分离）----
    ax.legend(handles, labels_, loc="upper left", bbox_to_anchor=(1.01, 1.0),
              borderaxespad=0, frameon=False, ncol=1)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="依据 qlog 生成帧延迟/包延迟散点图")
    parser.add_argument("--sender", default=None, help="发送端 qlog 路径")
    parser.add_argument("--receiver", default=None, help="接收端 qlog 路径")
    parser.add_argument("--outdir", default=None, help="输出目录（默认：脚本所在目录）")
    parser.add_argument("--min-owd-us", type=int, default=0,
                        help="链路最小单向延迟(us)，用于时钟对齐校正，默认 0")
    parser.add_argument("--paths", default=None,
                        help="参与绘图的路径（qlog 原始 path_id，逗号分隔，如 0,2；"
                             "默认标记下 0=Path 1、1/2=Path 2）；默认全部")
    parser.add_argument("--iframe-pkts", type=int, default=30,
                        help="包数超过该值的帧判定为 I 帧（仅在 send.log 无显式帧类标记时作为回退），默认 30")
    parser.add_argument("--class-map", default=None,
                        help="send.log 路径，用于读取发送端显式帧类标记（stream->I/BP）；"
                             "缺省时自动在脚本目录查找 send.log")
    parser.add_argument("--y-floor-ms", type=float, default=0.0,
                        help="对数纵轴下界(ms)；0=自动取正延迟 p5 分位"
                             "（保证最多丢弃 5%% 的点），默认 0")
    parser.add_argument("--y-cap-ms", type=float, default=100.0,
                        help="[兼容旧参数] 极端离群分界(ms)，超过该值的点用 × 标注；"
                             "等价 --outlier-rule cap:<值>，默认 100")
    parser.add_argument("--outlier-rule", default=None,
                        help="× 离群标记规则（动态调整 API 的命令行入口）："
                             "cap:150（固定阈值ms）| pct:99（正延迟分位数）| "
                             "path:0:50,1:100（按原始路径阈值）| none（不标×）；"
                             "缺省时回退到 --y-cap-ms")
    parser.add_argument("--path-labels", default="0:1,1:2,2:2",
                        help="原始 path_id -> 展示标签映射（逗号分隔 raw:label）："
                             "默认 0:1,1:2,2:2 表示 path0->Path 1、path1+path2->Path 2；"
                             "0:0,1:1,2:2 为原始编号；label 为纯数字时自动加 'Path ' 前缀")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    sender_list = find_qlog(base_dir, "qlog_sender")
    recv_list = find_qlog(base_dir, "qlog_receiver")
    sender_path = args.sender or (sender_list[-1] if sender_list else None)
    receiver_path = args.receiver or (recv_list[-1] if recv_list else None)
    if not sender_path or not receiver_path:
        raise SystemExit("找不到 qlog；请用 --sender/--receiver 指定路径。")
    print("sender   qlog:", sender_path)
    print("receiver qlog:", receiver_path)

    sref, sev = load_qlog(sender_path)
    rref, rev = load_qlog(receiver_path)
    _ = (sref, rref)  # 对齐直接用两端相对时间差值，ref 之差在相减时抵消

    spkt = parse_1rtt_packets(sev, "packet_sent")
    rpkt = parse_1rtt_packets(rev, "packet_received")
    print("send 1RTT pkts:", len(spkt), " recv 1RTT pkts:", len(rpkt))

    # ---- 时钟对齐：offset = min(recv_rel - send_rel) - min_owd_us ----
    # I帧优先机制测试：优先使用 send.log 中发送端显式标记的帧类，
    # 缺失时回退到 --iframe-pkts 包数启发式。
    class_map = load_class_map(args.class_map or os.path.join(base_dir, "send.log"))

    raw_diffs = []
    for key, (srel, _) in spkt.items():
        if key in rpkt:
            raw_diffs.append(rpkt[key][0] - srel)
    if not raw_diffs:
        raise SystemExit("发送端与接收端没有匹配到任何 1RTT 包")
    offset = min(raw_diffs) - args.min_owd_us
    print("matched pkts:", len(raw_diffs),
          " raw min diff(us):", min(raw_diffs),
          " -> offset(us):", offset,
          " (assumed min OWD:", args.min_owd_us, "us)")

    # ---- 包级数据 ----
    packets = []
    send_only = 0
    for (path_id, pn), (srel, sdata) in spkt.items():
        streams = extract_stream_frames(sdata)
        if (path_id, pn) in rpkt:
            rrel, _ = rpkt[(path_id, pn)]
            packets.append({
                "path_id": path_id,
                "pn": pn,
                "send_rel": srel,
                "recv_abs": rrel,
                "delay_us": rrel - srel - offset,
                "stream_ids": [s[0] for s in streams],
            })
        else:
            send_only += 1
    print("matched packets:", len(packets), " send-only (lost/in-flight):", send_only)

    # ---- 帧级数据（一个 stream = 一帧）----
    by_stream = defaultdict(list)
    for pk in packets:
        for sid in pk["stream_ids"]:
            by_stream[sid].append(pk)

    # 预统计每个 stream 的总字节数（含未匹配包）
    stream_bytes = defaultdict(int)
    for (_path_id, _pn), (_srel, sdata) in spkt.items():
        for sid2, off, ln, _fin in extract_stream_frames(sdata):
            if sid2 + 1 > 0:
                stream_bytes[sid2] = max(stream_bytes[sid2], off + ln)

    frames = []
    for sid in sorted(by_stream.keys()):
        pks = by_stream[sid]
        send_first = min(p["send_rel"] for p in pks)
        send_last = max(p["send_rel"] for p in pks)
        recv_last = max(p["recv_abs"] for p in pks)
        path_counts = defaultdict(int)
        for p in pks:
            path_counts[p["path_id"]] += 1
        main_path = max(path_counts, key=path_counts.get)
        frames.append({
            "stream_id": sid,
            "send_first_us": send_first,
            "send_last_us": send_last,
            "recv_last_us": recv_last,
            "frame_delay_us": recv_last - send_first - offset,
            "npkts": len(pks),
            "nbytes": stream_bytes.get(sid, 0),
            "pkts_by_path": dict(path_counts),
            "main_path": main_path,
            "is_iframe": class_map.get(sid, len(pks) > args.iframe_pkts),
        })
    n_iframe = sum(1 for f in frames if f["is_iframe"])
    print("frames:", len(frames), " I-frames:", n_iframe)

    # ---- 路径过滤（qlog 原始 path_id）----
    paths = (sorted(int(x) for x in args.paths.split(",")) if args.paths
             else sorted({p["path_id"] for p in packets}))
    print("paths for plotting:", paths)

    # ---- 路径重标记（默认 path0->Path 1；path1+path2->Path 2）----
    labels = make_path_labels(args.path_labels)
    # 补齐数据中出现但未在映射中的原始路径（展示回退 "Path <raw>"）
    for r in sorted({pk["path_id"] for pk in packets} |
                    {fr["main_path"] for fr in frames}):
        if r not in labels:
            labels[r] = "Path %d" % r
    print("path labels:", ", ".join("%s<-raw%s" % (
        lab, ",".join(str(r) for r, l in labels.items() if l == lab))
        for lab in ordered_labels(labels)))

    # ---- × 离群规则（API 动态调整的命令行入口）----
    rule = parse_outlier_rule(args.outlier_rule)
    if rule is None:
        rule = make_outlier_rule(cap_ms=args.y_cap_ms)
    print("outlier rule:", rule_desc(rule))

    # ---- 输出 CSV ----
    outdir = args.outdir or base_dir
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    pkt_csv = os.path.join(outdir, "qlog_packet_delay.csv")
    with open(pkt_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["path_id", "path_label", "packet_number",
                                          "send_time_us", "recv_time_us", "delay_us",
                                          "stream_ids"])
        w.writeheader()
        for pk in packets:
            w.writerow({"path_id": pk["path_id"],
                        "path_label": label_of(pk["path_id"], labels),
                        "packet_number": pk["pn"],
                        "send_time_us": pk["send_rel"], "recv_time_us": pk["recv_abs"],
                        "delay_us": pk["delay_us"],
                        "stream_ids": "|".join(str(s) for s in pk["stream_ids"])})
    print("wrote:", pkt_csv)

    fr_csv = os.path.join(outdir, "qlog_frame_delay.csv")
    ordered = ordered_labels(labels)
    fr_fields = (["stream_id", "send_first_us", "send_last_us", "recv_last_us",
                  "frame_delay_us", "npkts", "nbytes"]
                 + ["%s_pkts" % label_token(lab) for lab in ordered]
                 + ["main_path", "is_iframe"])
    with open(fr_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fr_fields)
        w.writeheader()
        for fr in frames:
            row = {k: fr[k] for k in ("stream_id", "send_first_us", "send_last_us",
                                      "recv_last_us", "frame_delay_us", "npkts",
                                      "nbytes", "is_iframe")}
            for lab in ordered:
                raw_ids = [r for r, l in labels.items() if l == lab]
                row["%s_pkts" % label_token(lab)] = sum(
                    fr["pkts_by_path"].get(r, 0) for r in raw_ids)
            row["main_path"] = label_of(fr["main_path"], labels)
            w.writerow(row)
    print("wrote:", fr_csv)

    # ---- 绘图（单图：帧 + 包合并；图例显示在主图右侧）----
    fig_path = os.path.join(outdir, "qlog_frame_packet_delay_scatter.png")
    plot_combined(packets, frames, paths, fig_path,
                  y_floor_ms=args.y_floor_ms,
                  outlier_rule=rule, path_labels=labels)
    print("wrote:", fig_path)

    # ---- 延迟统计摘要（按展示标签输出）----
    for p in paths:
        vals = sorted(pk["delay_us"] for pk in packets
                      if pk["path_id"] == p and pk["delay_us"] is not None)
        if vals:
            n = len(vals)
            print("%s packet delay(ms): min=%.2f p50=%.2f p95=%.2f p99=%.2f max=%.2f" % (
                label_of(p, labels), vals[0] / 1e3, vals[n // 2] / 1e3,
                vals[int(n * 0.95)] / 1e3, vals[int(n * 0.99)] / 1e3, vals[-1] / 1e3))
    fvals = sorted(f["frame_delay_us"] for f in frames)
    if fvals:
        n = len(fvals)
        print("frame delay(ms): min=%.2f p50=%.2f p95=%.2f p99=%.2f max=%.2f" % (
            fvals[0] / 1e3, fvals[n // 2] / 1e3,
            fvals[int(n * 0.95)] / 1e3, fvals[int(n * 0.99)] / 1e3, fvals[-1] / 1e3))


if __name__ == "__main__":
    main()
