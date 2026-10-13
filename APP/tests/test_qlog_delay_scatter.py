"""Remote-only regression checks; run after syncing, per 项目.md."""
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qlog_delay_scatter as qds


def stream(sid, offset=0, length=100, fin=True):
    return {'frame_type': 'stream', 'id': sid, 'offset': offset,
            'length': length, 'fin': fin}


def data(pn, *frames):
    return {'packet_type': '1RTT', 'header': {'packet_number': pn},
            'frames': list(frames)}


class DelayRegressionTests(unittest.TestCase):
    def test_four_and_five_column_packets(self):
        four = [[100, 'transport', 'packet_sent', data(1, stream(2))]]
        five = [[100, 0, 'transport', 'packet_sent', data(1, stream(2))]]
        self.assertEqual(qds.parse_1rtt_packets(four, 'packet_sent'),
                         qds.parse_1rtt_packets(five, 'packet_sent'))

    def test_declared_field_order_and_path_packet_number_spaces(self):
        fields = ['data', 'event', 'category', 'path_id', 'relative_time']
        events = [[data(7), 'packet_sent', 'transport', p, 100] for p in (0, 2)]
        packets = qds.parse_1rtt_packets(qds.normalize_events(events, fields), 'packet_sent')
        self.assertEqual(set(packets), {(0, 7), (2, 7)})

    def test_duplicate_receipt_keeps_first_arrival(self):
        packets = qds.parse_1rtt_packets(
            [[t, 0, 'transport', 'packet_received', data(1, stream(2))]
             for t in (130, 110)], 'packet_received')
        self.assertEqual(packets[(0, 1)][0], 110)

    def test_control_ack_and_mixed_packets(self):
        packets = {(0, 1): (100, data(1, stream(0))),
                   (0, 2): (110, data(2, {'frame_type': 'ack'})),
                   (0, 3): (120, data(3, stream(0), stream(2)))}
        filtered = qds.filter_media_packets(packets, {2})
        self.assertEqual(set(filtered), {(0, 3)})
        self.assertEqual(qds.extract_stream_frames(filtered[(0, 3)][1]), [(2, 0, 100, True)])
        self.assertEqual(len(packets[(0, 3)][1]['frames']), 2)

    def test_fin_before_lost_first_segment_retransmission(self):
        sent = {(0, 1): (100, data(1, stream(2, 0, 50, False))),
                (2, 1): (110, data(1, stream(2, 50, 50))),
                (0, 2): (200, data(2, stream(2, 0, 50, False)))}
        received = {(2, 1): (130, sent[(2, 1)][1]),
                    (0, 2): (220, sent[(0, 2)][1])}
        st = qds.stream_receipt_status(sent, received, 0, {2: {'expected_bytes': 100}})[2]
        self.assertEqual((st['status'], st['received_bytes']), ('complete', 100))
        self.assertEqual(st['complete_time'] - st['send_first'], 120)
        partial = qds.stream_receipt_status(sent, {(2, 1): received[(2, 1)]}, 0,
                                            {2: {'expected_bytes': 100}})[2]
        self.assertEqual(partial['status'], 'partial')

    def test_path_label_summary_pools_samples(self):
        samples = [{'path_id': p, 'delay_us': d} for p, d in ((0, 10), (1, 20), (2, 30))]
        self.assertEqual(qds.packet_delay_summary(samples, [0, 1, 2], qds.make_path_labels()),
                         {'Path 1': [10], 'Path 2': [20, 30]})

    def test_single_packet_au_markers_are_visible_without_coordinate_shift(self):
        packet = {'path_id': 0, 'delay_us': 10210, 'send_rel': 1000000}
        frame = {'main_path': 0, 'frame_delay_us': 10210,
                 'send_first_us': 1000000, 'is_iframe': False}
        real_close = qds.plt.close
        with patch.object(qds.plt, 'close'), patch('matplotlib.figure.Figure.savefig'):
            try:
                qds.plot_combined([packet], [frame], [0], 'unused.png')
                ax = qds.plt.gcf().axes[0]
                packet_marks, frame_marks = ax.collections
                self.assertEqual(packet_marks.get_offsets().tolist(), [[1.0, 10.21]])
                self.assertEqual(frame_marks.get_offsets().tolist(), [[1.0, 10.21]])
                self.assertEqual(len(frame_marks.get_facecolors()), 0)
                self.assertGreater(packet_marks.get_zorder(), frame_marks.get_zorder())
                self.assertLess(ax.get_ylim()[0], 10.21)
            finally:
                real_close('all')

    def test_run_dir_gst_exports_video_only_csv_and_three_plots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'send.log').write_text(
                '[MEDIA_MODE] sender=1 alpn=video-gst-v1\n'
                '[FRAME_GEN] frame=0 size=1 class=I gen_ts=100000\n'
                '[STREAM_MAP] stream=2 frame=0 size=1 stream_len=100 class=I first_pkt_ts=100100\n')
            (root / 'recv.log').write_text(
                '[FRAME_RECV] frame=0 class=I stream=2 total_pkts=1 recv_pkts=1 size=100 recv_ts=111000\n')
            for role, event, ref, when in (('sender', 'packet_sent', 100000, 100),
                                          ('receiver', 'packet_received', 90000, 20310)):
                target = root / ('qlog_' + role)
                target.mkdir()
                trace = {'common_fields': {'reference_time': str(ref)},
                         'configuration': {'time_units': 'us'},
                         'event_fields': ['relative_time', 'path_id', 'category', 'event', 'data'],
                         'events': [[when, 0, 'transport', event, data(1, stream(0))],
                                    [when, 0, 'transport', event, data(2, stream(2))]]}
                (target / 'test.qlog').write_text(json.dumps({'traces': [trace]}))
            with patch.object(sys, 'argv', ['qlog_delay_scatter.py', '--run-dir', str(root)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                qds.main()
            out = root / 'delay_analysis'
            with (out / 'qlog_frame_delay.csv').open(encoding='utf-8-sig', newline='') as fp:
                frames = list(csv.DictReader(fp))
            with (out / 'qlog_packet_delay.csv').open(encoding='utf-8-sig', newline='') as fp:
                packets = list(csv.DictReader(fp))
            self.assertEqual(len(frames), 1)
            self.assertEqual(len(packets), 1)
            self.assertEqual(packets[0]['stream_ids'], '2')
            self.assertEqual(int(frames[0]['frame_delay_us']), 10210)
            self.assertEqual(int(frames[0]['end_to_end_us']), 11000)
            self.assertEqual(frames[0]['app_complete'], 'True')
            for name in ('qlog_frame_packet_delay_scatter.png', 'qlog_packet_delay_scatter.png',
                         'qlog_frame_delay_scatter.png'):
                self.assertTrue((out / name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))


if __name__ == '__main__':
    unittest.main()
