#!/usr/bin/env python3
"""Offline full-timeline quality/QoE report for a completed archived GST run."""
import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import statistics
import subprocess
import zlib
from pathlib import Path


def command(args, cwd, log_name):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=1800)
    (cwd / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise ValueError(f"Command failed ({log_name}): {result.stderr[-1500:]}")
    return result.stdout + result.stderr


def table(path, field, count):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if [int(row[field]) for row in rows] != list(range(count)):
        raise ValueError(f"Missing/duplicate/reordered indices in {path}")
    return rows


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def distribution(values):
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {"count": len(values), "mean": statistics.fmean(values),
            "p50": ordered[round((len(ordered) - 1) * .5)],
            "p95": ordered[round((len(ordered) - 1) * .95)], "max": ordered[-1]}


def inputs(config, path):
    return ["-f", "rawvideo", "-pixel_format", "yuv420p", "-video_size",
            f"{config['width']}x{config['height']}", "-framerate", str(config["fps"]), "-i", str(path)]


def measure(ffmpeg, config, distorted, reference, out, name, model):
    base = [ffmpeg, "-hide_banner", "-nostdin"] + inputs(config, distorted) + inputs(config, reference)
    psnr_output = command(base + ["-filter_complex", f"[0:v][1:v]psnr=stats_file={name}_psnr.log",
                                 "-f", "null", "-"], out, name + "_psnr_command.log")
    command(base + ["-filter_complex", f"[0:v][1:v]ssim=stats_file={name}_ssim.log",
                    "-f", "null", "-"], out, name + "_ssim_command.log")
    command(base + ["-filter_complex",
                    f"[0:v][1:v]libvmaf=model=version={model}:log_fmt=json:log_path={name}_vmaf.json:n_threads=2",
                    "-f", "null", "-"], out, name + "_vmaf_command.log")
    psnr_lines = (out / (name + "_psnr.log")).read_text().splitlines()
    ssim_lines = (out / (name + "_ssim.log")).read_text().splitlines()
    vmaf = json.loads((out / (name + "_vmaf.json")).read_text())
    if not (len(psnr_lines) == len(ssim_lines) == len(vmaf["frames"]) == config["frames"]):
        raise ValueError(f"Metric frame count mismatch: {name}")
    rows = []
    for index, (p_line, s_line, v_row) in enumerate(zip(psnr_lines, ssim_lines, vmaf["frames"])):
        psnr = float(re.search(r"psnr_avg:([^\s]+)", p_line)[1])
        mse = float(re.search(r"mse_avg:([^\s]+)", p_line)[1])
        ssim = float(re.search(r"All:([^\s]+)", s_line)[1])
        score = float(v_row["metrics"]["vmaf"])
        rows.append({"frame_id": index, "psnr": psnr if math.isfinite(psnr) else None,
                     "psnr_infinite": not math.isfinite(psnr), "mse": mse, "ssim": ssim, "vmaf": score})
    # FFmpeg's summary uses full internal MSE precision. Recomputing from the
    # rounded per-frame mse_avg fields can falsely turn tiny errors into zero.
    summary_scores = re.findall(r'PSNR[^\n]*?average:([^\s]+)', psnr_output)
    if not summary_scores:
        raise ValueError(f'Missing aggregate PSNR summary: {name}')
    overall_psnr = float(summary_scores[-1])
    return rows, {"frames": len(rows), "psnr_overall": overall_psnr if math.isfinite(overall_psnr) else None,
                  "psnr_infinite": not math.isfinite(overall_psnr), "ssim_mean": statistics.fmean(row["ssim"] for row in rows),
                  "vmaf_mean": statistics.fmean(row["vmaf"] for row in rows),
                  "vmaf_pooled": vmaf.get("pooled_metrics", {}).get("vmaf", {})}


def validate_assets(run):
    manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("video_mode") != "gst" or manifest.get("status") != "completed" or not manifest.get("media_output_valid"):
        raise ValueError("Require a completed GST run with valid media output")
    sender, receiver = run / "media" / "sender", run / "media" / "receiver"
    cfg = json.loads((sender / "media_config.json").read_text())
    if cfg.get("codec") != "h264" or cfg.get("bframes") != 0 or cfg.get("colorimetry") != "bt709":
        raise ValueError("Unsupported codec, reorder or color configuration")
    expected = cfg["width"] * cfg["height"] * 3 // 2 * cfg["frames"]
    for path in (sender / "source.yuv", receiver / "playout.yuv"):
        if path.stat().st_size != expected:
            raise ValueError(f"Raw frame size/count mismatch: {path}")
    source = table(sender / "source_frames.csv", "frame_id", cfg["frames"])
    encoded = table(sender / "encoded_frames.csv", "frame_id", cfg["frames"])
    played = table(receiver / "playout_frames.csv", "slot", cfg["frames"])
    received = table(receiver / "receive_frames.csv", "frame_id", cfg["frames"])
    last_shown = None
    for index in range(cfg["frames"]):
        pts = index * 1_000_000_000 // cfg["fps"]
        if any(abs(int(row["pts_ns"]) - pts) > 1000 for row in (source[index], encoded[index], played[index])):
            raise ValueError(f"Source/encoded/playout PTS mismatch at frame {index}")
        shown = int(played[index]["displayed_source_frame_id"])
        if played[index]["status"] not in ("new", "repeat", "black"):
            raise ValueError(f"Invalid playout state at frame {index}")
        if (played[index]["status"] == "new" and shown != index) or (played[index]["status"] == "repeat" and not 0 <= shown < index):
            raise ValueError(f"Wrong source-frame alignment at playout slot {index}")
        if played[index]['status'] == 'new':
            last_shown = shown
        elif played[index]['status'] == 'repeat' and shown != last_shown:
            raise ValueError(f'Frozen frame differs from last displayed frame at slot {index}')
        elif played[index]['status'] == 'black' and (last_shown is not None or shown != 2**64 - 1):
            raise ValueError(f'Invalid black-frame source mapping at slot {index}')
    with (sender / "encoded.h264").open("rb") as stream:
        for row in encoded:
            count = int(row["au_bytes"])
            if not 0 < count <= 8 * 1024 * 1024:
                raise ValueError("Invalid encoded AU size")
            blob = stream.read(count)
            if len(blob) != count or zlib.crc32(blob) != int(row["crc32"]):
                raise ValueError(f"Encoded archive CRC mismatch at frame {row['frame_id']}")
        if stream.read(1):
            raise ValueError("Unexpected trailing encoded archive bytes")
    return manifest, cfg, sender, receiver, source, encoded, played, received


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--model", choices=["vmaf_v0.6.1", "vmaf_v0.6.1neg"], default="vmaf_v0.6.1")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("Require FFmpeg and ffprobe on the evaluation host")
    try:
        manifest, cfg, sender, receiver, source, encoded, played, received = validate_assets(run)
        if shutil.disk_usage(run).free < (sender / 'source.yuv').stat().st_size + 64 * 1024 * 1024:
            raise ValueError('Insufficient free space for the decoded encoding reference')
        out = run / "quality"
        out.mkdir()  # Never overwrite previous reports.
        version = command([ffmpeg, "-version"], out, "ffmpeg_version.log")
        filters = command([ffmpeg, "-filters"], out, "ffmpeg_filters.log")
        if not all(re.search(rf"\s{re.escape(name)}\s", filters) for name in ("psnr", "ssim", "libvmaf")):
            raise ValueError("FFmpeg must include psnr, ssim and libvmaf")
        probe = command([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=width,height,pix_fmt,color_space,color_range", "-of", "json",
                         str(sender / "encoded.h264")], out, "encoded_probe.json")
        streams = json.loads(probe).get("streams", [])
        if len(streams) != 1 or streams[0].get("width") != cfg["width"] or streams[0].get("height") != cfg["height"]:
            raise ValueError("Encoded dimensions disagree with raw configuration")
        if streams[0].get("color_space") != "bt709" or streams[0].get("color_range") not in ("tv", "unknown", None):
            raise ValueError("Encoded color metadata disagrees with BT.709 limited-range reference")
        enc_raw = out / "encoded_reference.yuv"
        command([ffmpeg, "-hide_banner", "-nostdin", "-i", str(sender / "encoded.h264"),
                 "-vsync", "0", "-pix_fmt", "yuv420p", "-f", "rawvideo", str(enc_raw)], out, "reference_decode.log")
        if enc_raw.stat().st_size != (sender / "source.yuv").stat().st_size:
            raise ValueError("Decoded reference has missing/extra frames")
        metrics, summaries = {}, {}
        for name, distorted, reference in (
                ("encoding", enc_raw, sender / "source.yuv"),
                ("transport_playout", receiver / "playout.yuv", enc_raw),
                ("end_to_end", receiver / "playout.yuv", sender / "source.yuv")):
            metrics[name], summaries[name] = measure(ffmpeg, cfg, distorted, reference, out, name, args.model)
        freezes, current = [], 0
        for row in played:
            if row["status"] == "repeat":
                current += 1
            elif current:
                freezes.append(current / cfg["fps"]); current = 0
        if current:
            freezes.append(current / cfg["fps"])
        latencies, output_latencies, encode_latencies, receive_latencies = [], [], [], []
        for index, row in enumerate(played):
            encode_latencies.append((int(encoded[index]["encoded_ready_mono_us"]) - int(source[index]["source_mono_us"])) / 1000)
            if row["status"] == "new":
                latencies.append((int(row["decision_us"]) - int(source[index]["source_mono_us"])) / 1000)
                output_latencies.append((int(row['output_us']) - int(source[index]['source_mono_us'])) / 1000)
            if received[index]["transport"] == "complete" and int(received[index]["received_us"]) > 0:
                receive_latencies.append((int(received[index]["received_us"]) - int(encoded[index]["encoded_ready_mono_us"])) / 1000)
        rate_windows = []
        for second in range(math.ceil(cfg["frames"] / cfg["fps"])):
            block = encoded[second * cfg["fps"]:(second + 1) * cfg["fps"]]
            rate_windows.append({"second": second, "kbps": sum(int(row["au_bytes"]) for row in block) * 8 * cfg["fps"] / len(block) / 1000})
        epoch_summaries = []
        for epoch in sorted({int(row["bitrate_epoch"]) for row in encoded}):
            ids = [i for i, row in enumerate(encoded) if int(row["bitrate_epoch"]) == epoch]
            epoch_summaries.append({"epoch": epoch, "first_frame": min(ids), "last_frame": max(ids),
                                    "encoded_kbps": sum(int(encoded[i]["au_bytes"]) for i in ids) * 8 * cfg["fps"] / len(ids) / 1000,
                                    "end_to_end_vmaf": statistics.fmean(metrics["end_to_end"][i]["vmaf"] for i in ids)})
        report = {"config": cfg, "metrics": summaries, "vmaf_model": args.model,
                  "ffmpeg_version": version.splitlines()[0], "network_timing_valid": manifest.get("timing_valid"),
                  "test_pattern_quality_scope": "Pipeline validation; not representative real-content perceptual evidence",
                  "playout_timing_valid": all(int(row["tick_lag_us"]) <= 1_000_000 / cfg["fps"] and
                                               int(row["output_us"]) - int(row["deadline_us"]) <= 1_000_000 / cfg["fps"] for row in played),
                  "playout_clock_scope": "Software frame decision/output times; not physical screen scanout",
                  "qoe": {"new_frame_ratio": sum(row["status"] == "new" for row in played) / cfg["frames"],
                          "complete_au_ratio": sum(row["transport"] == "complete" for row in received) / cfg["frames"],
                          "black_seconds": sum(row["status"] == "black" for row in played) / cfg["fps"],
                          "freeze_seconds": sum(freezes), "longest_freeze_seconds": max(freezes, default=0),
                          "freeze_events": len(freezes), "source_to_playout_decision_ms": distribution(latencies),
                          "source_to_output_ms": distribution(output_latencies),
                          "late_complete_au_ratio": sum(row['transport'] == 'complete' and
                                                        int(row['received_us']) > int(played[i]['deadline_us'])
                                                        for i, row in enumerate(received)) / cfg['frames'],
                          "wait_idr_skipped_predictive_seconds": sum(row['decode'] == 'wait_idr' for row in received) / cfg['fps'],
                          "source_to_encoded_ms": distribution(encode_latencies),
                          "encoded_to_received_ms": distribution(receive_latencies)},
                  "encoded_rate_windows": rate_windows, "bitrate_epochs": epoch_summaries,
                  "bitrate_epoch_scope": "Observed command epoch at AU callback; not exact encoder reconfiguration boundary",
                  "input_sha256": {str(path.relative_to(run)): hash_file(path) for path in
                                   (sender / "source.yuv", sender / "encoded.h264", receiver / "playout.yuv")}}
        with (out / "frame_quality.csv").open("w", newline="", encoding="utf-8") as stream:
            fields = ["frame_id", "pts_ns", "bitrate_epoch", "au_bytes", "status", "displayed_source_frame_id"] + [f"{name}_{metric}" for name in metrics for metric in ("psnr", "psnr_infinite", "ssim", "vmaf")]
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
            for index, played_row in enumerate(played):
                row = {"frame_id": index, "status": played_row["status"], "displayed_source_frame_id": played_row["displayed_source_frame_id"]}
                row.update({key: encoded[index][key] for key in ('pts_ns', 'bitrate_epoch', 'au_bytes')})
                row.update({f"{name}_{metric}": frames[index][metric] for name, frames in metrics.items() for metric in ("psnr", "psnr_infinite", "ssim", "vmaf")})
                writer.writerow(row)
        (out / "quality_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(f"Report: {out / 'quality_report.json'}")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Quality evaluation failed: {exc}") from exc


if __name__ == "__main__":
    main()
