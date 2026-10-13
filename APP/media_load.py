#!/usr/bin/env python3
"""Audit actual encoded load, media path use and software playout without FFmpeg."""
import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path

from media_quality import distribution, validate_assets


def physical_path(address):
    if address.startswith('10.0.1.'):
        return 'ground'
    if address.startswith('10.0.2.'):
        return 'sky'
    return None


def non_new_runs(played):
    runs = []
    for index, row in enumerate(played):
        if row['status'] == 'new':
            continue
        shown = int(row['displayed_source_frame_id'])
        if (runs and runs[-1]['end_frame'] == index - 1 and runs[-1]['status'] == row['status']
                and runs[-1]['displayed_source_frame_id'] == shown):
            runs[-1]['end_frame'] = index
        else:
            runs.append({'start_frame': index, 'end_frame': index,
                         'status': row['status'], 'displayed_source_frame_id': shown})
    return runs


def deadline_evidence(index, cfg, encoded, played, received):
    arrival = int(received[index].get('received_us', 0))
    cutoff = int(played[index]['deadline_us']) - cfg.get('decode_margin_ms', 20) * 1000
    ready = int(encoded[index]['encoded_ready_mono_us'])
    return {'frame_id': index, 'idr': int(encoded[index].get('idr', 0)),
            'au_bytes': int(encoded[index]['au_bytes']), 'playout_status': played[index]['status'],
            'decode_state': received[index].get('decode'), 'decode_cutoff_us': cutoff,
            'received_us': arrival or None,
            'arrival_minus_decode_cutoff_ms': (arrival - cutoff) / 1000 if arrival else None,
            'encoded_to_received_ms': (arrival - ready) / 1000 if arrival else None}


def path_evidence(run, send_text, streams):
    """Matched media packets from qlog CSV; address mapping from the sender API."""
    mappings = {}
    for match in re.finditer(r'\[PATH\] path_id=(\d+) local=(\S+) peer=(\S+)', send_text):
        path = physical_path(match[2])
        if path:
            mappings.setdefault(match[1], set()).add(path)
    counts, unmapped = Counter(), Counter()
    seen = set()
    csv_path = run / 'delay_analysis' / 'qlog_packet_delay.csv'
    if not csv_path.is_file():
        return {'available': False, 'matched_media_packets': {}, 'unmapped_path_ids': {},
                'both_physical_paths_carry_media': None,
                'scope': 'Run qlog_delay_scatter.py --run-dir first; UDP totals include control packets'}
    qlogs = list((run / 'qlog_sender').glob('*.qlog'))
    if len(qlogs) != 1:
        raise ValueError('Path audit requires one sender qlog from this run')
    traces = json.loads(qlogs[0].read_text(encoding='utf-8')).get('traces', [])
    if len(traces) != 1:
        raise ValueError('Path audit requires a single qlog trace')
    fields = traces[0].get('event_fields')
    events = traces[0].get('events', [])
    has_paths = 'path_id' in fields if fields is not None else bool(events) and all(
        isinstance(event, list) and len(event) == 5 for event in events)
    if not has_paths:
        return {'available': False, 'matched_media_packets': {}, 'unmapped_path_ids': {},
                'both_physical_paths_carry_media': None,
                'scope': 'Sender qlog has no path_id; fallback packet-number space 0 cannot prove a physical path'}
    with csv_path.open(newline='', encoding='utf-8-sig') as fp:
        for row in csv.DictReader(fp):
            if not (set(map(int, filter(None, row['stream_ids'].split('|')))) & streams):
                continue
            key = row['path_id'], row['packet_number']
            if key in seen:
                raise ValueError('Duplicate packet in qlog packet CSV')
            seen.add(key)
            labels = mappings.get(row['path_id'], set())
            if len(labels) == 1:
                counts[next(iter(labels))] += 1
            else:
                unmapped[row['path_id']] += 1
    return {'available': True, 'matched_media_packets': dict(counts),
            'unmapped_path_ids': dict(unmapped),
            'both_physical_paths_carry_media': counts['ground'] > 0 and counts['sky'] > 0,
            'scope': 'Matched video-bearing QUIC packets; retransmissions included; lost sends excluded. '
                     'Physical paths require unambiguous PATH address records; qlog display labels are not used'}


def build_report(run):
    manifest, cfg, sender, receiver, source, encoded, played, received = validate_assets(run)
    send_text = (run / 'send.log').read_text(encoding='utf-8', errors='replace')
    stream_matches = list(re.finditer(
        r'\[STREAM_MAP\] stream=(\d+) frame=(\d+) size=(\d+) stream_len=(\d+)', send_text))
    streams = {int(match[1]) for match in stream_matches}
    admitted = {int(match[2]): int(match[3]) for match in stream_matches}
    if len(streams) != len(stream_matches) or len(admitted) != len(stream_matches):
        raise ValueError('Duplicate stream/frame admission records')
    for frame_id, size in admitted.items():
        if not 0 <= frame_id < cfg['frames'] or size != int(encoded[frame_id]['au_bytes']):
            raise ValueError('Admission record does not match encoded archive')
    count, fps = cfg['frames'], cfg['fps']
    source_times = [int(row['source_mono_us']) for row in source]
    ready_times = [int(row['encoded_ready_mono_us']) for row in encoded]
    if any(int(row['source_mono_us']) != generated for row, generated in zip(encoded, source_times)):
        raise ValueError('Encoded/source generation timestamps disagree')
    if any(b <= a for a, b in zip(source_times, source_times[1:])):
        raise ValueError('Non-increasing source timestamps')
    if any(ready < generated for ready, generated in zip(ready_times, source_times)):
        raise ValueError('Encoded-ready timestamp precedes source timestamp')
    origin = re.search(r'\[VIDEO_START\] monotonic_us=(\d+)', send_text)
    if not origin:
        raise ValueError('Missing common VIDEO_START origin')
    origin_us = int(origin[1])
    duration = count / fps
    source_wall_seconds = (source_times[-1] - source_times[0]) / 1e6 + 1 / fps
    au_bytes = sum(int(row['au_bytes']) for row in encoded)
    admitted_bytes = sum(admitted.values())
    windows = []
    for first in range(0, count, fps):
        block = encoded[first:first + fps]
        seconds = len(block) / fps
        windows.append({'second': first // fps, 'frames': len(block),
                        'encoded_kbps': sum(int(row['au_bytes']) for row in block) * 8 / seconds / 1000,
                        'admitted_kbps': sum(admitted.get(i, 0) for i in range(first, first + len(block))) * 8 / seconds / 1000})
    encoded_wall_seconds = (ready_times[-1] - ready_times[0]) / 1e6 + 1 / fps
    if encoded_wall_seconds <= 0:
        raise ValueError('Invalid encoded-ready timeline')
    udp = {}
    window_begin, window_end = source_times[0], source_times[-1] + 1_000_000 / fps
    for match in re.finditer(r'\[PKT_SEND\].*?send_ts=(\d+) size=(\d+).*?local=(\S+) peer=(\S+)', send_text):
        label = physical_path(match[3]) or 'unknown'
        timestamp, size = int(match[1]), int(match[2]) + 16
        row = udp.setdefault(label, {'packets': 0, 'quic_plus_experiment_bytes': 0,
                                     'source_window_bytes': 0, 'drain_bytes': 0})
        row['packets'] += 1
        row['quic_plus_experiment_bytes'] += size
        if window_begin <= timestamp < window_end:
            row['source_window_bytes'] += size
        elif timestamp >= window_end:
            row['drain_bytes'] += size
    for row in udp.values():
        row['source_window_kbps'] = row['source_window_bytes'] * 8 / source_wall_seconds / 1000
    new_latencies, output_latencies, freezes = [], [], []
    consecutive = 0
    for index, row in enumerate(played):
        if row['status'] == 'new':
            new_latencies.append((int(row['decision_us']) - source_times[index]) / 1000)
            output_latencies.append((int(row['output_us']) - source_times[index]) / 1000)
        if row['status'] == 'repeat':
            consecutive += 1
        elif consecutive:
            freezes.append(consecutive / fps)
            consecutive = 0
    if consecutive:
        freezes.append(consecutive / fps)
    state_counts = Counter(row['status'] for row in played)
    frame_lags = [(value - origin_us) / 1000 - index * 1000 / fps
                  for index, value in enumerate(source_times)]
    affected_runs = non_new_runs(played)
    evidence_ids = {0} | {index for index, row in enumerate(encoded) if int(row.get('idr', 0))}
    evidence_ids |= {row['start_frame'] for row in affected_runs}
    evidence_ids |= {row['end_frame'] + 1 for row in affected_runs if row['end_frame'] + 1 < count}
    flow_records = [dict(zip(('local_stream_window', 'peer_stream_window', 'local_connection_window',
                              'peer_connection_window'), map(int, match.groups()))) for match in re.finditer(
        r'\[FLOW_CONTROL\] local_stream_window=(\d+) peer_stream_window=(\d+) local_connection_window=(\d+) peer_connection_window=(\d+)', send_text)]
    return {
        'run_dir': str(run), 'source_git_head': manifest.get('source_git_head'),
        'config': cfg, 'status': manifest['status'],
        'timing_valid': manifest.get('timing_valid'),
        'network_replay_timing_valid': manifest.get('network_replay_timing_valid'),
        'source_timing_valid': manifest.get('source_timing_valid'),
        'timing': manifest.get('timing'),
        'jitter_calendar': manifest.get('jitter_calendar'),
        'flow_control': {'negotiated_at_sender': flow_records,
                         'au_over_initial_peer_window_frames': [int(value) for value in re.findall(
                             r'\[AU_FLOW_WINDOW\] frame=(\d+)', send_text)],
                         'scope': 'Initial credit is not a cwnd or bandwidth estimate; large AUs may need additional MAX_STREAM_DATA credit'},
        'deadline_diagnostics': {'non_new_runs': affected_runs,
                                 'keyframes_and_recovery_boundaries': [deadline_evidence(index, cfg, encoded, played, received)
                                                                      for index in sorted(evidence_ids)],
                                 'scope': 'Positive arrival-minus-cutoff means late complete AU; decoded state alone does not imply timely playout'},
        'encoded_load': {'au_bytes': au_bytes, 'nominal_seconds': duration,
                         'source_wall_seconds': source_wall_seconds,
                         'encoded_kbps': au_bytes * 8 / duration / 1000,
                         'source_wall_kbps': au_bytes * 8 / source_wall_seconds / 1000,
                         'encoded_ready_wall_kbps': au_bytes * 8 / encoded_wall_seconds / 1000,
                         'initial_target_kbps': cfg['bitrate_kbps'], 'windows': windows,
                         'scope': 'Measured H.264 AU bytes; no synthetic byte padding. '
                                  'Nominal windows are media-time windows, not UDP throughput'},
        'admission': {'frames': len(admitted), 'ratio': len(admitted) / count,
                      'au_bytes': admitted_bytes, 'bytes_ratio': admitted_bytes / au_bytes,
                      'source_wall_kbps': admitted_bytes * 8 / source_wall_seconds / 1000,
                      'unadmitted_frames': sorted(set(range(count)) - set(admitted)),
                      'admission_deadline_drops': len(re.findall(r'\[MEDIA_DROP\].*reason=admission_deadline', send_text)),
                      'complete_au_frames': sum(row['transport'] == 'complete' for row in received)},
        'udp_send_totals': udp,
        'udp_scope': 'All successfully sent datagrams, including control and retransmissions; '
                     'size includes the 16-byte experiment header and excludes UDP/IP headers. '
                     'Old logs without address fields cannot provide these totals',
        'media_path_evidence': path_evidence(run, send_text, streams),
        'source_timing': {'startup_lag_ms': frame_lags[0],
                          'raw_generation_lag_ms': distribution(frame_lags),
                          'pacing_lag_after_first_frame_ms': distribution([value - frame_lags[0] for value in frame_lags]),
                          'encode_latency_ms': distribution([(ready - generated) / 1000 for ready, generated in zip(ready_times, source_times)]),
                          'scope': 'Startup/pacing decomposition is diagnostic; it never overrides timing_valid'},
        'playout': {'counts': dict(state_counts), 'new_frame_ratio': state_counts['new'] / count,
                    'black_seconds': state_counts['black'] / fps,
                    'freeze_seconds': sum(freezes), 'longest_freeze_seconds': max(freezes, default=0),
                    'freeze_events': len(freezes),
                    'tick_lag_ms': distribution([int(row['tick_lag_us']) / 1000 for row in played]),
                    'output_lag_ms': distribution([(int(row['output_us']) - int(row['deadline_us'])) / 1000 for row in played]),
                    'source_to_decision_ms': distribution(new_latencies),
                    'source_to_output_ms': distribution(output_latencies),
                    'scope': 'Software playout; completeness alone does not imply timely decoding or display'},
    }


def acceptance_errors(report, min_encoded_kbps=0, require_timing=False, require_multipath=False,
                      min_admitted_kbps=0):
    errors = []
    # Use elapsed source wall time, so a stalled encoder cannot pass by nominal FPS alone.
    if report['encoded_load']['source_wall_kbps'] < min_encoded_kbps:
        errors.append(f'Actual encoded source-wall rate is below {min_encoded_kbps:g} kbit/s')
    if report['admission']['source_wall_kbps'] < min_admitted_kbps:
        errors.append(f'Actual admitted source-wall rate is below {min_admitted_kbps:g} kbit/s')
    if require_timing and report['timing_valid'] is not True:
        errors.append('Common-origin source/network timing did not pass')
    if require_multipath and report['media_path_evidence']['both_physical_paths_carry_media'] is not True:
        errors.append('Missing matched media evidence on both physical paths')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--min-encoded-kbps', type=float, default=0)
    parser.add_argument('--min-admitted-kbps', type=float, default=0)
    parser.add_argument('--require-timing', action='store_true')
    parser.add_argument('--require-multipath', action='store_true')
    args = parser.parse_args()
    if any(not math.isfinite(value) or value < 0 for value in
           (args.min_encoded_kbps, args.min_admitted_kbps)):
        parser.error('Minimum rate thresholds must be finite and nonnegative')
    run = args.run_dir.resolve()
    try:
        report = build_report(run)
        report['acceptance_errors'] = acceptance_errors(report, args.min_encoded_kbps,
                                                        args.require_timing, args.require_multipath,
                                                        args.min_admitted_kbps)
        # Refresh this small derived report; never alter original logs/media/manifest.
        output = run / 'load_report.json'
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        print(f'Report: {output}')
        print(f"Encoded nominal/source-wall rate: {report['encoded_load']['encoded_kbps']:.1f}/"
              f"{report['encoded_load']['source_wall_kbps']:.1f} kbit/s; "
              f"admitted={report['admission']['ratio']:.1%}; new playout={report['playout']['new_frame_ratio']:.1%}; "
              f"timing_valid={report['timing_valid']}")
        if report['acceptance_errors']:
            raise SystemExit('; '.join(report['acceptance_errors']))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise SystemExit(f'Load audit failed: {exc}') from exc


if __name__ == '__main__':
    main()
