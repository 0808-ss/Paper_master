"""Remote-only configuration contract checks; no media or network processes."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_experiment import prepare_media


class MediaConfigTests(unittest.TestCase):
    def config(self, directory, extra=None):
        env = {'MEDIA_WIDTH': '16', 'MEDIA_HEIGHT': '16', 'MEDIA_FRAMES': '1'}
        env.update(extra or {})
        with patch.dict(os.environ, env, clear=True):
            return prepare_media(directory, '0', '30', 'rr')

    def test_both_roles_receive_initial_flow_control_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            media = self.config(directory)
        self.assertEqual(media['config']['stream_window_bytes'], 1048576)
        self.assertEqual(media['config']['connection_window_bytes'], 8388608)
        self.assertEqual(media['config']['i_priority'], 0)
        for role in ('sender_command', 'receiver_command'):
            command = media[role]
            self.assertEqual(command[command.index('--stream-window') + 1], '1048576')
            self.assertEqual(command[command.index('--connection-window') + 1], '8388608')

    def test_priority_does_not_enable_preemption_and_old_windows_remain_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            media = self.config(directory, {'MEDIA_I_PRIORITY': '1',
                'MEDIA_STREAM_WINDOW_BYTES': '65535', 'MEDIA_CONNECTION_WINDOW_BYTES': '1048576'})
        command = media['sender_command']
        self.assertEqual(command[command.index('--i-priority') + 1], '1')
        self.assertEqual(command[command.index('--preempt') + 1], '0')
        self.assertNotIn('--i-priority', media['receiver_command'])

    def test_invalid_connection_credit_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'cover the stream window'):
                self.config(directory, {'MEDIA_CONNECTION_WINDOW_BYTES': '65535'})
            self.assertFalse((Path(directory) / 'media_work').exists())

    def test_arrival_scheduler_reaches_sender_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'MEDIA_WIDTH': '16', 'MEDIA_HEIGHT': '16',
                                        'MEDIA_FRAMES': '1'}, clear=True):
                media = prepare_media(directory, '0', '30', 'arrival')
        self.assertEqual(media['config']['scheduler'], 'arrival')
        command = media['sender_command']
        self.assertEqual(command[command.index('--scheduler') + 1], 'arrival')
        self.assertNotIn('--scheduler', media['receiver_command'])


if __name__ == '__main__':
    unittest.main()
