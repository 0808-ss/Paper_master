"""Remote-only asset validation checks; no media pipeline or FFmpeg invocation."""
import csv
import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_quality import validate_assets
from media_experiment import load_network_jitter_times


class QualityAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.sender = self.root / 'media' / 'sender'
        self.receiver = self.root / 'media' / 'receiver'
        self.sender.mkdir(parents=True)
        self.receiver.mkdir(parents=True)
        (self.root / 'run_manifest.json').write_text(json.dumps({
            'video_mode': 'gst', 'status': 'completed', 'media_output_valid': True}))
        (self.sender / 'media_config.json').write_text(json.dumps({
            'codec': 'h264', 'bframes': 0, 'colorimetry': 'bt709',
            'width': 16, 'height': 16, 'fps': 30, 'frames': 3}))
        for path in (self.sender / 'source.yuv', self.receiver / 'playout.yuv'):
            path.write_bytes(bytes(16 * 16 * 3 // 2 * 3))
        self.source = [{'frame_id': i, 'pts_ns': i * 1_000_000_000 // 30} for i in range(3)]
        self.encoded = [dict(row, au_bytes=5, crc32=zlib.crc32(b'\0\0\1\x65\x55')) for row in self.source]
        (self.sender / 'encoded.h264').write_bytes(b'\0\0\1\x65\x55' * 3)
        self.played = [{'slot': i, 'pts_ns': i * 1_000_000_000 // 30,
                        'displayed_source_frame_id': 0, 'status': 'new' if i == 0 else 'repeat'} for i in range(3)]
        self.write(self.sender / 'source_frames.csv', self.source)
        self.write(self.sender / 'encoded_frames.csv', self.encoded)
        self.write(self.receiver / 'playout_frames.csv', self.played)
        self.write(self.receiver / 'receive_frames.csv', self.source)

    @staticmethod
    def write(path, rows):
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    def tearDown(self):
        self.directory.cleanup()

    def test_freeze_slots_remain_in_full_timeline(self):
        result = validate_assets(self.root)
        self.assertEqual([row['status'] for row in result[-2]], ['new', 'repeat', 'repeat'])

    def test_missing_reference_index_is_rejected(self):
        self.write(self.sender / 'source_frames.csv', self.source[:2])
        with self.assertRaisesRegex(ValueError, 'indices'):
            validate_assets(self.root)

    def test_pts_mismatch_is_rejected(self):
        self.encoded[1]['pts_ns'] += 1_000_000
        self.write(self.sender / 'encoded_frames.csv', self.encoded)
        with self.assertRaisesRegex(ValueError, 'PTS mismatch'):
            validate_assets(self.root)

    def test_future_frame_cannot_fill_earlier_slot(self):
        self.played[1]['displayed_source_frame_id'] = 2
        self.write(self.receiver / 'playout_frames.csv', self.played)
        with self.assertRaisesRegex(ValueError, 'alignment'):
            validate_assets(self.root)

    def test_archive_corruption_is_rejected(self):
        (self.sender / 'encoded.h264').write_bytes(b'bad!!' * 3)
        with self.assertRaisesRegex(ValueError, 'CRC mismatch'):
            validate_assets(self.root)

    def test_truncated_playback_raw_is_rejected(self):
        (self.receiver / 'playout.yuv').write_bytes(bytes(10))
        with self.assertRaisesRegex(ValueError, 'size/count mismatch'):
            validate_assets(self.root)

    def test_network_calendar_ignores_former_frame_sizes(self):
        path = self.root / 'network_calendar.csv'
        path.write_text('not_a_size,0.033333\n-5,0.033333\n0,0\n')
        self.assertEqual(load_network_jitter_times(path), [0.0, 0.033333, 0.066666])


if __name__ == '__main__':
    unittest.main()
