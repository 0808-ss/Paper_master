"""TC update helpers, independent of Mininet; remote-only runtime checks."""
import math
import re
import subprocess
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


class TCUpdater:
    """One checked batch per node, in parallel; keep fixed qdisc handles."""
    def __init__(self, links):
        self.links = links
        self.pool = ThreadPoolExecutor(max_workers=4)

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
