#!/usr/bin/env python3
"""Minimal fixed-leader Raft node for baseline vs XDP experiments.

This is deliberately scoped for benchmarking:
  - node1 is the fixed leader; elections are not implemented.
  - node2-node4 are followers.
  - the leader counts itself as one replication vote and waits for a quorum.
  - followers apply COMMIT_NOTICE messages in userspace.

The XDP fast path can consume follower APPEND_ENTRIES and heartbeat packets,
reply with APPEND_RESPONSE from the kernel, and let COMMIT_NOTICE packets pass
to this process.
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

from raft_messages import (
    APPEND_ENTRIES,
    APPEND_RESPONSE,
    CLIENT_REPLY,
    CLIENT_REQUEST,
    COMMIT_NOTICE,
    NACK,
    RAFT_PORT,
    pack,
    unpack,
)


Address = Tuple[str, int]


def node_name() -> str:
    return os.environ.get("NODE_NAME") or socket.gethostname()


class RaftFollower:
    def __init__(self, bind: str, port: int, verbose: bool = False) -> None:
        self.bind = bind
        self.port = port
        self.verbose = verbose
        self.current_term = 1
        self.leader_addr: Optional[Address] = None
        self.last_seen_leader_ns = 0
        self.log_entries: Dict[int, bytes] = {}
        self.commit_index = 0
        self.append_count = 0
        self.commit_count = 0
        self.heartbeat_count = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))

    def log(self, text: str) -> None:
        print(f"[follower {node_name()}] {text}", flush=True)

    def serve(self) -> None:
        self.log(f"listening on {self.bind}:{self.port}")
        while True:
            data, addr = self.sock.recvfrom(65535)
            try:
                msg = unpack(data)
            except ValueError as exc:
                if self.verbose:
                    self.log(f"bad packet from {addr}: {exc}")
                continue

            if msg.msg_type == APPEND_ENTRIES:
                self.handle_append_entries(msg, addr)
            elif msg.msg_type == COMMIT_NOTICE:
                self.handle_commit(msg, addr)
            else:
                if self.verbose:
                    self.log(f"ignored {msg.type_name} from {addr}")

    def handle_append_entries(self, msg, addr: Address) -> None:
        if msg.term < self.current_term:
            self.sock.sendto(pack(NACK, self.current_term, msg.index, self.commit_index), addr)
            return

        self.current_term = msg.term
        self.leader_addr = addr
        self.last_seen_leader_ns = time.time_ns()

        if msg.index == 0 and not msg.payload:
            self.heartbeat_count += 1
        else:
            self.log_entries[msg.index] = msg.payload
            self.append_count += 1

        self.sock.sendto(
            pack(APPEND_RESPONSE, self.current_term, msg.index, self.commit_index),
            addr,
        )

    def handle_commit(self, msg, addr: Address) -> None:
        if msg.term < self.current_term:
            return
        self.current_term = msg.term
        self.leader_addr = addr
        self.last_seen_leader_ns = time.time_ns()

        if msg.payload:
            self.log_entries[msg.index] = msg.payload
        if msg.index > self.commit_index:
            self.commit_index = msg.index
            self.commit_count += 1

        if self.verbose or self.commit_count % 1000 == 0:
            self.log(
                "committed=%d commit_index=%d userspace_appends=%d heartbeats=%d"
                % (
                    self.commit_count,
                    self.commit_index,
                    self.append_count,
                    self.heartbeat_count,
                )
            )


class RaftLeader:
    def __init__(
        self,
        follower_ips: Iterable[str],
        bind: str,
        port: int,
        term: int,
        timeout: float,
        heartbeat_interval: float,
        verbose: bool = False,
    ) -> None:
        self.followers: List[Address] = [(ip, port) for ip in follower_ips]
        self.bind = bind
        self.port = port
        self.current_term = term
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.verbose = verbose
        self.cluster_size = 1 + len(self.followers)
        self.quorum = self.cluster_size // 2 + 1
        self.log_entries: Dict[int, bytes] = {}
        self.commit_index = 0
        self.next_index = 1
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.sock.settimeout(timeout)

    def log(self, text: str) -> None:
        print(f"[leader {node_name()}] {text}", flush=True)

    def start_heartbeats(self) -> None:
        thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        thread.start()

    def heartbeat_loop(self) -> None:
        while self.running:
            frame = pack(APPEND_ENTRIES, self.current_term, 0, self.commit_index)
            for follower in self.followers:
                self.sock.sendto(frame, follower)
            time.sleep(self.heartbeat_interval)

    def replicate(self, payload: bytes) -> int:
        index = self.next_index
        self.next_index += 1
        self.log_entries[index] = payload

        frame = pack(APPEND_ENTRIES, self.current_term, index, self.commit_index, payload)
        for follower in self.followers:
            self.sock.sendto(frame, follower)

        accepted: Set[str] = {"leader-local"}
        deadline = time.monotonic() + self.timeout
        while len(accepted) < self.quorum and time.monotonic() < deadline:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                msg = unpack(data)
            except ValueError:
                continue

            if (
                msg.msg_type == APPEND_RESPONSE
                and msg.term == self.current_term
                and msg.index == index
            ):
                accepted.add(addr[0])
            elif msg.msg_type == NACK and msg.term > self.current_term:
                raise RuntimeError(f"higher term observed: {msg.term}")

        if len(accepted) < self.quorum:
            raise TimeoutError(f"index {index}: got {len(accepted)} replicas, need {self.quorum}")

        self.commit_index = index
        commit = pack(COMMIT_NOTICE, self.current_term, index, self.commit_index, payload)
        for follower in self.followers:
            self.sock.sendto(commit, follower)
        return index

    def serve(self) -> None:
        self.log(
            f"listening on {self.bind}:{self.port}, followers={len(self.followers)}, quorum={self.quorum}"
        )
        self.start_heartbeats()
        completed = 0
        while True:
            try:
                data, client_addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                msg = unpack(data)
            except ValueError:
                continue
            if msg.msg_type != CLIENT_REQUEST:
                continue

            start = time.perf_counter_ns()
            try:
                index = self.replicate(msg.payload)
                elapsed_us = (time.perf_counter_ns() - start) // 1000
                reply = f"index={index} latency_us={elapsed_us}".encode()
                self.sock.sendto(
                    pack(CLIENT_REPLY, self.current_term, index, self.commit_index, reply),
                    client_addr,
                )
                completed += 1
                if self.verbose or completed % 1000 == 0:
                    self.log(f"committed={completed} commit_index={self.commit_index}")
            except Exception as exc:
                self.sock.sendto(
                    pack(NACK, self.current_term, self.commit_index, self.commit_index, str(exc).encode()),
                    client_addr,
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["leader", "follower"], required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=RAFT_PORT)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--heartbeat-interval", type=float, default=0.2)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("followers", nargs="*", help="follower IPs, leader role only")
    args = parser.parse_args()

    if args.role == "leader":
        if not args.followers:
            raise SystemExit("leader role requires follower IP arguments")
        RaftLeader(
            args.followers,
            bind=args.bind,
            port=args.port,
            term=args.term,
            timeout=args.timeout,
            heartbeat_interval=args.heartbeat_interval,
            verbose=args.verbose,
        ).serve()
    else:
        RaftFollower(args.bind, args.port, args.verbose).serve()


if __name__ == "__main__":
    main()
