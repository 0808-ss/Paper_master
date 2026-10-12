#!/usr/bin/env python3
"""Send an explicit bitrate update to the current run's local control endpoint."""
import argparse
import json
import socket
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket_path")
    parser.add_argument("target_kbps", type=int)
    parser.add_argument("--command-id", type=int, default=None)
    args = parser.parse_args()
    if not 100 <= args.target_kbps <= 100000:
        parser.error("target_kbps must be in [100, 100000]")
    command = args.command_id if args.command_id is not None else time.monotonic_ns()
    if not 0 <= command < 2**64:
        parser.error("command_id must be an unsigned 64-bit integer")
    payload = {"op": "set_bitrate", "command_id": command, "target_kbps": args.target_kbps}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(args.socket_path)
        client.sendall((json.dumps(payload) + "\n").encode("ascii"))
        response = b""
        while b"\n" not in response:
            chunk = client.recv(256)
            if not chunk or len(response) + len(chunk) > 1024:
                raise SystemExit("Incomplete/oversized control response")
            response += chunk
    result = json.loads(response)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("command_id") != command or result.get("status") != "applied_to_element":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
