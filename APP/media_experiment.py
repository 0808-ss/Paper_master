"""Configuration/archiving helpers; no Mininet or media processes run on import."""
import csv
import math
import os
import shutil
import time


def load_network_jitter_times(filepath):
    """Read only interval seconds; the former load-size column is irrelevant."""
    times, elapsed_us = [], 0
    with open(filepath, encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            parts = line.split(',')
            try:
                seconds = float(parts[1])
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError('invalid interval')
                wait_us = int(seconds * 1_000_000)
            except (ValueError, IndexError) as exc:
                raise ValueError(f'Invalid network jitter interval at {filepath}:{line_number}') from exc
            times.append(elapsed_us / 1_000_000)
            elapsed_us += wait_us
    return times


def integer(name, default, minimum, maximum):
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def prepare_media(cwd, preempt, gop, scheduler):
    fps = integer("MEDIA_FPS", 30, 1, 120)
    cfg = {
        "width": integer("MEDIA_WIDTH", 1920, 16, 3840),
        "height": integer("MEDIA_HEIGHT", 1080, 16, 2160),
        "fps": fps,
        "frames": integer("MEDIA_FRAMES", fps * 10, 1, 18000),
        "bitrate_kbps": integer("MEDIA_BITRATE_KBPS", 4000, 100, 100000),
        "gop": integer("MEDIA_GOP", int(gop) or 30, 1, 600),
        "playout_ms": integer("MEDIA_PLAYOUT_MS", 200, 20, 5000),
        "decode_margin_ms": integer("MEDIA_DECODE_MARGIN_MS", 20, 0, 4999),
        "preempt": int(preempt), "all_bp": int(int(gop) == 0), "scheduler": scheduler,
        "pattern": os.environ.get("MEDIA_PATTERN", "ball"),
        "preview": integer("MEDIA_PREVIEW", 0, 0, 1),
        "i_priority": integer("MEDIA_I_PRIORITY", 0, 0, 1),
        "stream_window_bytes": integer("MEDIA_STREAM_WINDOW_BYTES", 1048576, 65535, 33554432),
        "connection_window_bytes": integer("MEDIA_CONNECTION_WINDOW_BYTES", 8388608, 65535, 33554432),
    }
    if cfg["width"] % 2 or cfg["height"] % 2 or cfg["decode_margin_ms"] >= cfg["playout_ms"]:
        raise ValueError("I420 requires even dimensions; decode margin must be below playout buffer")
    if cfg["preempt"] not in (0, 1) or scheduler not in ("default", "rr", "minrtt", "arrival"):
        raise ValueError("Invalid preemption/scheduler config")
    if cfg["pattern"] not in ("ball", "zone-plate", "smpte", "snow"):
        raise ValueError("MEDIA_PATTERN must be ball, zone-plate, smpte or snow")
    if cfg['connection_window_bytes'] < cfg['stream_window_bytes']:
        raise ValueError('Connection flow-control window must cover the stream window')
    run_id = f"{os.getpid()}_{time.time_ns()}"
    root = os.path.join(cwd, "media_work", run_id)
    os.makedirs(root)
    raw_size = cfg['width'] * cfg['height'] * 3 // 2 * cfg['frames']
    if shutil.disk_usage(root).free < 2 * raw_size + 128 * 1024 * 1024:
        raise ValueError('Insufficient free space for source and playout raw files')
    sender_dir, receiver_dir = os.path.join(root, "sender"), os.path.join(root, "receiver")
    control = f"/tmp/mpquic_{run_id}.sock"
    sender = [os.path.join(cwd, "video_sender_gst"), "10.0.1.2", "12345"]
    for flag, name in (("width", "width"), ("height", "height"), ("fps", "fps"), ("frames", "frames"),
                       ("bitrate", "bitrate_kbps"), ("gop", "gop"), ("preempt", "preempt"),
                       ("all-bp", "all_bp"), ("scheduler", "scheduler"), ("playout-ms", "playout_ms"),
                       ("decode-margin-ms", "decode_margin_ms"), ("pattern", "pattern"),
                       ("i-priority", "i_priority"), ("stream-window", "stream_window_bytes"),
                       ("connection-window", "connection_window_bytes")):
        sender.extend(["--" + flag, str(cfg[name])])
    sender.extend(["--output", sender_dir, "--control-socket", control])
    receiver = [os.path.join(cwd, "video_receiver_gst"), "12345", os.path.join(cwd, "cert.pem"),
                os.path.join(cwd, "key.pem"), "--output", receiver_dir, "--preview", str(cfg["preview"])]
    receiver.extend(['--stream-window', str(cfg['stream_window_bytes']),
                     '--connection-window', str(cfg['connection_window_bytes'])])
    return {"config": cfg, "root": root, "control_socket": control,
            "sender_command": sender, "receiver_command": receiver,
            "frame_times": [i / fps for i in range(cfg["frames"])]}


def archive_media(media, run_dir):
    if media:
        destination = os.path.join(run_dir, 'media')
        if os.path.exists(destination):
            raise FileExistsError(f'Refusing to overwrite media archive: {destination}')
        # Move the owned, unique run directory instead of duplicating multi-GB raw files.
        shutil.move(media['root'], destination)


def media_outputs_valid(media):
    cfg = media["config"]
    expected_size = cfg["width"] * cfg["height"] * 3 // 2 * cfg["frames"]
    errors = []
    for role, filename in (("sender", "source.yuv"), ("receiver", "playout.yuv")):
        path = os.path.join(media["root"], role, filename)
        if not os.path.isfile(path) or os.path.getsize(path) != expected_size:
            errors.append(f"Wrong/missing {role}/{filename}: expected {expected_size} bytes")
    for role, filename in (("sender", "source_frames.csv"), ("sender", "encoded_frames.csv"),
                           ("receiver", "playout_frames.csv"), ("receiver", "receive_frames.csv")):
        path = os.path.join(media["root"], role, filename)
        try:
            with open(path, newline="") as stream:
                rows = list(csv.DictReader(stream))
            field = "slot" if filename == "playout_frames.csv" else "frame_id"
            if [int(row[field]) for row in rows] != list(range(cfg["frames"])):
                errors.append(f"Missing/duplicate/reordered {role}/{filename} frame indices")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"Invalid {role}/{filename}: {exc}")
    return errors
