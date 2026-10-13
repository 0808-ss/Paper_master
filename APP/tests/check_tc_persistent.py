#!/usr/bin/env python3
"""Remote-only, read-only tc compatibility check; never changes a qdisc or class."""
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from link_replay import PersistentTC


def main():
    node = SimpleNamespace(name='local-read-only', popen=subprocess.Popen)
    worker = PersistentTC(node)
    pid, durations = worker.proc.pid, []
    try:
        for _ in range(10):
            begin = time.monotonic()
            worker.apply([])  # Only qdisc show dev lo.
            durations.append((time.monotonic() - begin) * 1000)
            if worker.proc.pid != pid or worker.proc.poll() is not None:
                raise RuntimeError('Persistent tc did not remain alive')
    finally:
        worker.close()
    print(f'Persistent TC read-only check passed: pid={pid}, queries=10, '
          f'max_query_ms={max(durations):.3f}, exit={worker.proc.returncode}')


if __name__ == '__main__':
    main()
