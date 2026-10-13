"""TC update helpers, independent of Mininet; remote-only runtime checks."""
import json
import math
import os
import re
import select
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor


def change_commands(ifname, previous, current):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', ifname):
        raise ValueError(f'Invalid interface name: {ifname}')
    bw, lat = current
    if not all(math.isfinite(value) and value > 0 for value in current):
        raise ValueError('TC bandwidth and delay must be finite and positive')
    commands = []
    if abs(bw - previous[0]) > 0.1:
        commands.append(f'class change dev {ifname} parent 1: classid 1:1 '
                        f'htb rate {bw:.9f}mbit ceil {bw:.9f}mbit')
    if abs(lat - previous[1]) > 0.1:
        commands.append(f'qdisc change dev {ifname} parent 1:1 handle 10: '
                        f'netem limit 1000 delay {lat:.2f}ms')
    return commands


def run_batch(node, commands):
    # Popen does not share Mininet Node.cmd's interactive shell/read buffer.
    proc = node.popen(['tc', '-batch', '-'], stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate('\n'.join(commands) + '\n', timeout=2)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f'TC batch timed out on {node.name}') from exc
    if proc.returncode:
        raise RuntimeError(f'TC batch failed on {node.name} ({proc.returncode}): {out}{err}')


class PersistentTC:
    """Keep tc and its netlink socket alive; a read-only JSON query fences each batch."""
    def __init__(self, node, timeout=2):
        self.node, self.timeout = node, timeout
        self.closed = False
        self.errors = b''
        # No -force: a failed change terminates tc before the fence can succeed.
        self.proc = node.popen(['stdbuf', '-o0', '-e0', 'tc', '-j', '-batch', '-'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=0)
        try:
            os.set_blocking(self.proc.stdout.fileno(), False)
            os.set_blocking(self.proc.stderr.fileno(), False)
            self.apply([])  # Check JSON/batch compatibility before the video origin.
        except Exception:
            self._dispose(abort=True)
            raise

    def _read_errors(self):
        while True:
            try:
                chunk = os.read(self.proc.stderr.fileno(), 4096)
            except BlockingIOError:
                return
            if not chunk:
                return
            self.errors = (self.errors + chunk)[-65536:]

    def _fail(self, why):
        self._read_errors()
        detail = self.errors.decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'Persistent TC on {self.node.name}: {why}; {detail}')

    def _reply(self):
        deadline = time.monotonic() + self.timeout
        output = b''
        while True:
            if self.proc.poll() is not None:
                self._fail(f'exited with status {self.proc.returncode}')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail('completion fence timed out')
            readable, _, _ = select.select([self.proc.stdout, self.proc.stderr], [], [], remaining)
            if self.proc.stderr in readable:
                self._read_errors()
            if self.proc.stdout not in readable:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                self._fail('EOF before completion fence')
            output += chunk
            if len(output) > 65536:
                self._fail('oversized completion fence')
            try:
                reply = json.loads(output)
            except (ValueError, UnicodeDecodeError):
                continue  # JSON can arrive in several pipe reads, with or without a newline.
            if not isinstance(reply, list) or any(not isinstance(row, dict) for row in reply):
                self._fail('invalid JSON completion fence')
            return

    def apply(self, commands):
        if self.closed or self.proc.poll() is not None:
            raise RuntimeError(f'Persistent TC worker unavailable on {self.node.name}')
        # This query runs after all mutations and their netlink acknowledgements.
        # It reads loopback state only; it never changes or clears a traffic queue.
        payload = ('\n'.join([*commands, 'qdisc show dev lo']) + '\n').encode('ascii')
        if len(payload) > 4096:
            raise ValueError('TC batch exceeds bounded pipe-write size')
        try:
            if self.proc.stdin.write(payload) != len(payload):
                self._fail('short command write')
            self._reply()
        except Exception:
            # Never reuse a timed-out/failed worker: a later reply could be stale.
            self._dispose(abort=True)
            raise

    def _dispose(self, abort=False):
        if self.closed:
            return
        self.closed = True
        error = None
        try:
            if abort:
                if self.proc.poll() is None:
                    self.proc.kill()
            else:
                self.proc.stdin.close()  # EOF ends tc's batch loop without touching qdiscs.
            try:
                code = self.proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                error = f'TC worker shutdown timed out on {self.node.name}'
            else:
                if code and not abort:
                    self._read_errors()
                    error = f'TC worker exited with status {code} on {self.node.name}: {self.errors!r}'
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                stream.close()
        if error and not abort:
            raise RuntimeError(error)

    def close(self):
        self._dispose()


class TCUpdater:
    """One checked batch per node, in parallel; keep fixed qdisc handles."""
    def __init__(self, links, backend='persistent', worker_factory=None):
        if backend not in ('persistent', 'batch'):
            raise ValueError('TC_BACKEND must be persistent or batch')
        self.links = links
        self.backend, self.workers, self.process_starts = backend, {}, 0
        self.pool = ThreadPoolExecutor(max_workers=4)
        if backend == 'persistent':
            nodes = dict.fromkeys(intf.node for link in links for intf in (link.intf1, link.intf2))
            factory = worker_factory or PersistentTC
            pending = {node: self.pool.submit(factory, node) for node in nodes}
            errors = []
            for node, future in pending.items():
                try:
                    self.workers[node] = future.result()
                    self.process_starts += 1
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                try:
                    self.close()
                except Exception as exc:
                    errors.append(str(exc))
                raise RuntimeError('; '.join(errors))

    def apply(self, previous, current):
        grouped = {}
        updated = list(previous)
        for index, link in enumerate(self.links):
            bw, lat = current[index]
            # Retain each property's last applied value, including sub-threshold changes.
            updated[index] = (bw if abs(bw - previous[index][0]) > 0.1 else previous[index][0],
                              lat if abs(lat - previous[index][1]) > 0.1 else previous[index][1])
            for intf in (link.intf1, link.intf2):
                commands = change_commands(intf.name, previous[index], current[index])
                if commands:
                    grouped.setdefault(intf.node, []).extend(commands)
        if self.backend == 'persistent':
            pending = [self.pool.submit(self.workers[node].apply, commands) for node, commands in grouped.items()]
        else:
            self.process_starts += len(grouped)
            pending = [self.pool.submit(run_batch, node, commands) for node, commands in grouped.items()]
        errors = []
        for future in pending:
            try:
                future.result()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('; '.join(errors))
        return updated, sum(map(len, grouped.values())), len(grouped)

    def close(self):
        self.pool.shutdown(wait=True)
        errors = []
        for worker in self.workers.values():
            try:
                worker.close()
            except Exception as exc:
                errors.append(str(exc))
        self.workers.clear()
        if errors:
            raise RuntimeError('; '.join(errors))
