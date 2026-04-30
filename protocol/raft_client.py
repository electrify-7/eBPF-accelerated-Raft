#!/usr/bin/env python3
"""Sequential UDP client for the fixed-leader Raft benchmark."""

from __future__ import annotations

import argparse
import os
import random
import socket
import statistics
import sys
import time
from typing import List

from raft_messages import CLIENT_REPLY, CLIENT_REQUEST, NACK, RAFT_PORT, csv_header, pack, unpack


def percentile(values: List[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("leader_ip")
    parser.add_argument("--port", type=int, default=RAFT_PORT)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--payload-bytes", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout)
    leader = (args.leader_ip, args.port)
    rng = random.Random(42)
    rows = []
    latencies: List[int] = []
    failures = 0
    wall_start = time.perf_counter()

    for seq in range(1, args.count + 1):
        payload = (
            rng.randbytes(args.payload_bytes)
            if hasattr(rng, "randbytes")
            else os.urandom(args.payload_bytes)
        )
        start = time.perf_counter_ns()
        sock.sendto(pack(CLIENT_REQUEST, 0, seq, 0, payload), leader)
        status = "ok"
        latency_us = 0
        try:
            data, _ = sock.recvfrom(65535)
            latency_us = (time.perf_counter_ns() - start) // 1000
            msg = unpack(data)
            if msg.msg_type != CLIENT_REPLY:
                status = "nack" if msg.msg_type == NACK else msg.type_name
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            latency_us = (time.perf_counter_ns() - start) // 1000

        if status == "ok":
            latencies.append(latency_us)
        else:
            failures += 1
        rows.append(f"{seq},{status},{latency_us},{args.payload_bytes}\n")

        if seq % 500 == 0:
            print(f"completed {seq}/{args.count}", file=sys.stderr, flush=True)

    wall_elapsed = time.perf_counter() - wall_start
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(csv_header())
            fh.writelines(rows)

    throughput = len(latencies) / wall_elapsed if wall_elapsed else 0.0
    print(f"requests={args.count}")
    print(f"success={len(latencies)}")
    print(f"failures={failures}")
    print(f"mean_us={statistics.mean(latencies):.1f}" if latencies else "mean_us=0")
    print(f"p50_us={percentile(latencies, 50)}")
    print(f"p95_us={percentile(latencies, 95)}")
    print(f"p99_us={percentile(latencies, 99)}")
    print(f"wall_clock_throughput_req_s={throughput:.1f}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
