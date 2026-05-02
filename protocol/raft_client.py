#!/usr/bin/env python3
"""Sequential UDP client for the Raft benchmark."""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import statistics
import sys
import time
from typing import Dict, List

from raft_messages import (
    CLIENT_REPLY,
    CLIENT_REQUEST,
    FLAG_SUCCESS,
    NACK,
    RAFT_PORT,
    csv_header,
    pack,
    unpack,
)


def percentile(values: List[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def parse_reply(payload: bytes) -> Dict[str, object]:
    if not payload:
        return {}
    try:
        obj = json.loads(payload.decode())
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("leader_ip")
    parser.add_argument("--port", type=int, default=RAFT_PORT)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--payload-bytes", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--command-prefix", default="cmd")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout)
    leader = (args.leader_ip, args.port)
    rng = random.Random(42)
    rows = []
    latencies: List[int] = []
    failures = 0
    total_retries = 0
    total_conflicts = 0
    kernel_quorum = 0
    failure_samples: List[str] = []
    wall_start = time.perf_counter()

    for seq in range(1, args.count + 1):
        random_bytes = (
            rng.randbytes(max(0, args.payload_bytes - 32))
            if hasattr(rng, "randbytes")
            else os.urandom(max(0, args.payload_bytes - 32))
        )
        command = f"{args.command_prefix}-{seq}:".encode() + random_bytes
        start = time.perf_counter_ns()
        sock.sendto(pack(CLIENT_REQUEST, index=seq, payload=command), leader)

        status = "ok"
        latency_us = 0
        leader_index = 0
        retries = 0
        conflict_hints = 0
        quorum_source = "userspace"
        detail = ""
        try:
            data, _ = sock.recvfrom(65535)
            latency_us = (time.perf_counter_ns() - start) // 1000
            msg = unpack(data)
            if msg.msg_type == CLIENT_REPLY and (msg.flags & FLAG_SUCCESS):
                reply = parse_reply(msg.payload)
                leader_index = int(reply.get("index", msg.index) or 0)
                retries = int(reply.get("retries", 0) or 0)
                conflict_hints = int(reply.get("conflict_hints", 0) or 0)
                quorum_source = str(reply.get("quorum_source", "userspace"))
            else:
                status = "nack" if msg.msg_type == NACK else msg.type_name
                detail = msg.payload.decode(errors="replace")
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            detail = str(exc)
            latency_us = (time.perf_counter_ns() - start) // 1000

        if status == "ok":
            latencies.append(latency_us)
        else:
            failures += 1
            if len(failure_samples) < 10:
                failure_samples.append(f"seq={seq} status={status} detail={detail}")
        total_retries += retries
        total_conflicts += conflict_hints
        if quorum_source == "ebpf":
            kernel_quorum += 1

        rows.append(
            f"{seq},{status},{latency_us},{len(command)},{leader_index},"
            f"{retries},{conflict_hints},{quorum_source}\n"
        )

        if seq % 500 == 0:
            print(f"completed {seq}/{args.count}", file=sys.stderr, flush=True)

    wall_elapsed = time.perf_counter() - wall_start
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(csv_header())
            fh.writelines(rows)

    success_throughput = len(latencies) / wall_elapsed if wall_elapsed else 0.0
    offered_throughput = args.count / wall_elapsed if wall_elapsed else 0.0
    print(f"requests={args.count}")
    print(f"success={len(latencies)}")
    print(f"failures={failures}")
    print(f"mean_us={statistics.mean(latencies):.1f}" if latencies else "mean_us=0")
    print(f"p50_us={percentile(latencies, 50)}")
    print(f"p95_us={percentile(latencies, 95)}")
    print(f"p99_us={percentile(latencies, 99)}")
    print(f"wall_clock_elapsed_s={wall_elapsed:.6f}")
    print(f"success_throughput_req_s={success_throughput:.1f}")
    print(f"offered_throughput_req_s={offered_throughput:.1f}")
    print(f"wall_clock_throughput_req_s={success_throughput:.1f}")
    print(f"leader_retries={total_retries}")
    print(f"conflict_hints={total_conflicts}")
    print(f"kernel_quorum_replies={kernel_quorum}")
    for sample in failure_samples:
        print(f"failure_sample={sample}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
