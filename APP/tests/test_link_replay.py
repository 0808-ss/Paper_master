"""Remote-only TC regression checks; use fake nodes, never configure host TC."""
import subprocess
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from link_replay import PersistentTC, TCUpdater, change_commands, run_batch


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
        updater = TCUpdater(links, backend='batch')
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
        updater = TCUpdater(links, backend='batch')
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
        updater = TCUpdater(links, backend='batch')
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

    def test_persistent_backend_reuses_three_workers(self):
        links, _ = self.topology()
        created = []
        class Worker:
            def __init__(self, node):
                self.calls, self.closed = [], False
                created.append(self)
            def apply(self, commands):
                self.calls.append(commands)
            def close(self):
                self.closed = True
        updater = TCUpdater(links, worker_factory=Worker)
        try:
            previous, _, _ = updater.apply([(6, 10), (5, 20)], [(6, 15), (5, 25)])
            updater.apply(previous, [(6, 20), (5, 30)])
            self.assertEqual(updater.process_starts, 3)
            self.assertEqual([len(worker.calls) for worker in created], [2, 2, 2])
        finally:
            updater.close()
        self.assertTrue(all(worker.closed for worker in created))

    def test_worker_preparation_failure_closes_other_workers(self):
        links, _ = self.topology()
        created = []
        class Worker:
            def __init__(self, node):
                if node.name == 'h1':
                    raise RuntimeError('injected prepare failure')
                self.closed = False
                created.append(self)
            def close(self):
                self.closed = True
        with self.assertRaisesRegex(RuntimeError, 'prepare failure'):
            TCUpdater(links, worker_factory=Worker)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(worker.closed for worker in created))


class PipeProcess:
    """Fake tc over real Linux pipes; no subprocess, qdisc or network mutation."""
    def __init__(self, mode='success'):
        self.returncode, self.mode, self.requests, self.killed = None, mode, [], False
        out_r, self.out_w = os.pipe()
        err_r, self.err_w = os.pipe()
        self.stdout, self.stderr = os.fdopen(out_r, 'rb', buffering=0), os.fdopen(err_r, 'rb', buffering=0)
        self.stdin = SimpleNamespace(write=self.write, close=self.finish)
    def write(self, payload):
        self.requests.append(payload)
        if len(self.requests) == 1 or self.mode == 'success':
            # A complete JSON fence deliberately has no newline.
            os.write(self.out_w, b'[]')
        elif self.mode == 'error':
            os.write(self.err_w, b'RTNETLINK injected failure\n')
            self.returncode = 1
        elif self.mode == 'invalid':
            os.write(self.out_w, b'{}')
        return len(payload)
    def poll(self):
        return self.returncode
    def finish(self):
        if self.returncode is None:
            self.returncode = 0
    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fake tc', timeout)
        for field in ('out_w', 'err_w'):
            fd = getattr(self, field)
            if fd is not None:
                os.close(fd)
                setattr(self, field, None)
        return self.returncode
    def kill(self):
        self.killed = True
        self.returncode = -9


@unittest.skipUnless(sys.platform.startswith('linux'), 'Persistent tc pipe control targets remote Linux')
class PersistentPipeTests(unittest.TestCase):
    def worker(self, mode='success'):
        proc = PipeProcess(mode)
        def popen(args, **kwargs):
            self.assertEqual(args, ['stdbuf', '-o0', '-e0', 'tc', '-j', '-batch', '-'])
            self.assertNotIn('-force', args)
            return proc
        worker = PersistentTC(SimpleNamespace(name='h1', popen=popen), timeout=.03)
        return worker, proc

    def test_fence_waits_for_complete_json_without_newline_and_reuses_process(self):
        worker, proc = self.worker()
        try:
            worker.apply(['class change dev h1-eth0'])
            worker.apply(['qdisc change dev h1-eth0'])
            self.assertEqual(len(proc.requests), 3)  # one preparation query + two updates
            self.assertTrue(all(payload.endswith(b'qdisc show dev lo\n') for payload in proc.requests))
        finally:
            worker.close()
        self.assertFalse(proc.killed)

    def test_timeout_kills_worker_and_forbids_stale_reply_reuse(self):
        worker, proc = self.worker('timeout')
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            worker.apply(['class change'])
        self.assertTrue(proc.killed)
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            worker.apply([])
        worker.close()

    def test_command_failure_propagates_stderr_without_success_fence(self):
        worker, _ = self.worker('error')
        with self.assertRaisesRegex(RuntimeError, 'RTNETLINK injected failure'):
            worker.apply(['class change'])
        worker.close()

    def test_invalid_json_shape_fails_and_kills_worker(self):
        worker, proc = self.worker('invalid')
        with self.assertRaisesRegex(RuntimeError, 'invalid JSON'):
            worker.apply(['class change'])
        self.assertTrue(proc.killed)
        worker.close()


if __name__ == '__main__':
    unittest.main()
