"""Remote-only TC regression checks; use fake nodes, never configure host TC."""
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from link_replay import TCUpdater, change_commands, run_batch


class FakeProcess:
    def __init__(self, code=0, timeout=False):
        self.returncode = code
        self.timeout = timeout
        self.killed = False
        self.input = None

    def communicate(self, text=None, timeout=None):
        self.input = text or self.input
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired('tc', timeout)
        return '', 'injected failure' if self.returncode else ''

    def kill(self):
        self.killed = True


class FakeNode:
    def __init__(self, name, code=0):
        self.name, self.code, self.processes = name, code, []

    def popen(self, args, **kwargs):
        if args != ['tc', '-batch', '-'] or not kwargs.get('text'):
            raise AssertionError('TC must use stdin batch without the interactive shell')
        proc = FakeProcess(self.code)
        self.processes.append(proc)
        return proc


class ReplayTests(unittest.TestCase):
    def test_delay_only_preserves_htb_class(self):
        commands = change_commands('h1-eth0', (6, 10), (6.05, 15))
        self.assertEqual(len(commands), 1)
        self.assertIn('qdisc change', commands[0])
        self.assertNotIn('replace', commands[0])

    def test_invalid_parameters_rejected(self):
        for name, value in [('bad name', (6, 10)), ('h1-eth0', (float('nan'), 10)),
                            ('h1-eth0', (0, 10))]:
            with self.assertRaises(ValueError):
                change_commands(name, (6, 10), value)

    def topology(self, code=0):
        h1, s1, s3 = FakeNode('h1', code), FakeNode('s1'), FakeNode('s3')
        links = [SimpleNamespace(intf1=SimpleNamespace(name='h1-eth0', node=h1),
                                 intf2=SimpleNamespace(name='s1-eth1', node=s1)),
                 SimpleNamespace(intf1=SimpleNamespace(name='h1-eth1', node=h1),
                                 intf2=SimpleNamespace(name='s3-eth1', node=s3))]
        return links, [h1, s1, s3]

    def test_two_paths_use_three_batches_and_four_delay_commands(self):
        links, nodes = self.topology()
        updater = TCUpdater(links)
        try:
            updated, commands, batches = updater.apply([(6, 10), (5, 20)], [(6, 15), (5, 25)])
        finally:
            updater.close()
        self.assertEqual((commands, batches), (4, 3))
        self.assertEqual(updated, [(6, 15), (5, 25)])
        self.assertEqual([len(node.processes) for node in nodes], [1, 1, 1])
        self.assertEqual(len(nodes[0].processes[0].input.splitlines()), 2)

    def test_threshold_accumulates_from_last_applied_property(self):
        links, _ = self.topology()
        updater = TCUpdater(links)
        try:
            previous, _, _ = updater.apply([(6, 10), (5, 20)], [(6.06, 11), (5, 20)])
            self.assertEqual(previous[0], (6, 11))
            updated, commands, _ = updater.apply(previous, [(6.12, 11), (5, 20)])
            self.assertEqual(updated[0], (6.12, 11))
            self.assertEqual(commands, 2)
        finally:
            updater.close()

    def test_failure_is_reported_and_input_state_is_not_mutated(self):
        links, nodes = self.topology(code=2)
        updater = TCUpdater(links)
        previous = [(6, 10), (5, 20)]
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                updater.apply(previous, [(6, 15), (5, 25)])
        finally:
            updater.close()
        self.assertEqual(previous, [(6, 10), (5, 20)])
        self.assertTrue(all(len(node.processes) == 1 for node in nodes))

    def test_timeout_kills_and_reaps_child(self):
        proc = FakeProcess(timeout=True)
        node = SimpleNamespace(name='h1', popen=lambda *args, **kwargs: proc)
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            run_batch(node, ['qdisc change'])
        self.assertTrue(proc.killed)


if __name__ == '__main__':
    unittest.main()
