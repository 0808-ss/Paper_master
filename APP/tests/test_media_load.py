"""Remote-only load audit regressions; tiny archived fixtures, no media processes."""
import csv
import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_load import acceptance_errors, build_report


class LoadTests(unittest.TestCase):
    @staticmethod
    def write(path, rows):
        with path.open('w', newline='') as fp:
            writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.run = Path(self.directory.name)
        self.sender, receiver = self.run / 'media/sender', self.run / 'media/receiver'
        self.sender.mkdir(parents=True)
        receiver.mkdir(parents=True)
        (self.run / 'run_manifest.json').write_text(json.dumps({
            'video_mode': 'gst', 'status': 'completed', 'media_output_valid': True, 'timing_valid': False}))
        (self.sender / 'media_config.json').write_text(json.dumps({
            'codec': 'h264', 'bframes': 0, 'colorimetry': 'bt709', 'width': 16, 'height': 16,
            'fps': 2, 'frames': 4, 'bitrate_kbps': 6000}))
        for path in (self.sender / 'source.yuv', receiver / 'playout.yuv'):
            path.write_bytes(bytes(16 * 16 * 3 // 2 * 4))
        blob = b'\0\0\1\x65\x55'
        (self.sender / 'encoded.h264').write_bytes(blob * 4)
        self.source = [{'frame_id': i, 'pts_ns': i * 500_000_000, 'source_mono_us': 1_040_000 + i * 500_000}
                       for i in range(4)]
        self.encoded = [dict(row, au_bytes=5, crc32=zlib.crc32(blob),
                             encoded_ready_mono_us=row['source_mono_us'] + 5000) for row in self.source]
        self.write(self.sender / 'source_frames.csv', self.source)
        self.write(self.sender / 'encoded_frames.csv', self.encoded)
        played = [{'slot': i, 'pts_ns': i * 500_000_000,
                   'displayed_source_frame_id': i if i < 2 else 1,
                   'status': 'new' if i < 2 else 'repeat', 'decision_us': 1_200_000 + i * 500_000,
                   'output_us': 1_201_000 + i * 500_000, 'deadline_us': 1_200_000 + i * 500_000,
                   'tick_lag_us': 0} for i in range(4)]
        self.write(receiver / 'playout_frames.csv', played)
        self.write(receiver / 'receive_frames.csv', [{'frame_id': i, 'transport': 'complete'} for i in range(4)])
        self.log = '[VIDEO_START] monotonic_us=1000000\n'
        for i in range(4):
            self.log += f'[STREAM_MAP] stream={i * 4 + 2} frame={i} size=5 stream_len=121 class=BP\n'
        self.log += '[PATH] path_id=0 local=10.0.1.1:5000 peer=10.0.1.2:12345\n'
        self.log += '[PATH] path_id=2 local=10.0.2.1:5000 peer=10.0.2.2:12345\n'
        (self.run / 'send.log').write_text(self.log)

    def tearDown(self):
        self.directory.cleanup()

    def qlog_csv(self, with_paths=True):
        (self.run / 'delay_analysis').mkdir()
        self.write(self.run / 'delay_analysis/qlog_packet_delay.csv', [
            {'path_id': 0, 'packet_number': 1, 'stream_ids': '0|2'},
            {'path_id': 2, 'packet_number': 1, 'stream_ids': '6'},
            {'path_id': 0, 'packet_number': 2, 'stream_ids': '0'}])
        (self.run / 'qlog_sender').mkdir()
        fields = ['relative_time', 'path_id', 'category', 'event', 'data'] if with_paths else [
            'relative_time', 'category', 'event', 'data']
        (self.run / 'qlog_sender/one.qlog').write_text(json.dumps({'traces': [{'event_fields': fields}]}))

    def test_target_rate_is_not_used_as_measured_load(self):
        report = build_report(self.run)
        self.assertAlmostEqual(report['encoded_load']['encoded_kbps'], .08)
        self.assertAlmostEqual(report['admission']['source_wall_kbps'], .08)
        self.assertTrue(acceptance_errors(report, min_encoded_kbps=4500))

    def test_stalled_encoder_uses_wall_time_in_rate_gate(self):
        for i, row in enumerate(self.source):
            row['source_mono_us'] += i * 500_000
            self.encoded[i]['source_mono_us'] = row['source_mono_us']
            self.encoded[i]['encoded_ready_mono_us'] = row['source_mono_us'] + 5000
        self.write(self.sender / 'source_frames.csv', self.source)
        self.write(self.sender / 'encoded_frames.csv', self.encoded)
        report = build_report(self.run)
        self.assertLess(report['encoded_load']['source_wall_kbps'], .08)
        self.assertTrue(acceptance_errors(report, min_encoded_kbps=.07))

    def test_timing_decomposition_does_not_clear_failure(self):
        report = build_report(self.run)
        self.assertEqual(report['source_timing']['startup_lag_ms'], 40)
        self.assertEqual(report['source_timing']['pacing_lag_after_first_frame_ms']['max'], 0)
        self.assertTrue(acceptance_errors(report, require_timing=True))

    def test_complete_frames_can_still_freeze(self):
        report = build_report(self.run)
        self.assertEqual(report['admission']['complete_au_frames'], 4)
        self.assertEqual(report['playout']['new_frame_ratio'], .5)
        self.assertEqual(report['playout']['freeze_seconds'], 1)

    def test_control_packets_do_not_prove_media_path_use(self):
        self.qlog_csv()
        report = build_report(self.run)
        self.assertEqual(report['media_path_evidence']['matched_media_packets'], {'ground': 1, 'sky': 1})
        self.assertFalse(acceptance_errors(report, require_multipath=True))

    def test_missing_path_ids_cannot_prove_physical_multipath(self):
        self.qlog_csv(with_paths=False)
        report = build_report(self.run)
        self.assertIsNone(report['media_path_evidence']['both_physical_paths_carry_media'])
        self.assertTrue(acceptance_errors(report, require_multipath=True))

    def test_partial_admission_fails_admitted_load_gate(self):
        lines = self.log.splitlines()
        (self.run / 'send.log').write_text('\n'.join(line for line in lines if 'frame=3 ' not in line))
        report = build_report(self.run)
        self.assertEqual(report['admission']['ratio'], .75)
        self.assertTrue(acceptance_errors(report, min_admitted_kbps=.07))


if __name__ == '__main__':
    unittest.main()
