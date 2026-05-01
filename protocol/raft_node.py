#!/usr/bin/env python3
"""Fixed-leader Raft node used by the benchmark.

Implemented Raft pieces:
  - client sends commands to the leader
  - leader appends a term/indexed log entry
  - leader broadcasts AppendEntries to followers
  - followers validate prevLogIndex/prevLogTerm
  - followers return optimized conflict hints on mismatch
  - leader updates nextIndex using those hints and retransmits
  - leader commits after a majority and broadcasts commit notices

Election is intentionally out of scope because this lab measures the steady
state leader bottleneck and eBPF acceleration paths.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from raft_messages import (
    APPEND_ENTRIES,
    APPEND_RESPONSE,
    CLIENT_REPLY,
    CLIENT_REQUEST,
    COMMIT_NOTICE,
    FLAG_SUCCESS,
    NACK,
    RAFT_PORT,
    pack,
    unpack,
)


Address = Tuple[str, int]


@dataclass
class LogEntry:
    index: int
    term: int
    command: bytes


def node_name() -> str:
    return os.environ.get("NODE_NAME") or socket.gethostname()


def parse_seed_log(seed_terms: str) -> List[LogEntry]:
    """Build deterministic seed entries from a comma-separated term list."""
    entries: List[LogEntry] = []
    if not seed_terms:
        return entries
    for i, raw in enumerate(seed_terms.split(","), start=1):
        term = int(raw.strip())
        entries.append(LogEntry(i, term, f"seed-{i}-term-{term}".encode()))
    return entries


class RaftLog:
    def __init__(self, entries: Optional[List[LogEntry]] = None) -> None:
        self.entries: List[LogEntry] = []
        for entry in entries or []:
            self.append_or_replace(entry)

    @property
    def last_index(self) -> int:
        return self.entries[-1].index if self.entries else 0

    @property
    def last_term(self) -> int:
        return self.entries[-1].term if self.entries else 0

    def term_at(self, index: int) -> int:
        if index == 0:
            return 0
        if 1 <= index <= len(self.entries):
            return self.entries[index - 1].term
        return -1

    def command_at(self, index: int) -> bytes:
        if 1 <= index <= len(self.entries):
            return self.entries[index - 1].command
        return b""

    def entry_at(self, index: int) -> Optional[LogEntry]:
        if 1 <= index <= len(self.entries):
            return self.entries[index - 1]
        return None

    def append_or_replace(self, entry: LogEntry) -> None:
        if entry.index <= 0:
            return
        if entry.index <= len(self.entries):
            existing = self.entries[entry.index - 1]
            if existing.term != entry.term:
                self.truncate_from(entry.index)
                self.entries.append(entry)
            elif not existing.command and entry.command:
                self.entries[entry.index - 1] = entry
        elif entry.index == len(self.entries) + 1:
            self.entries.append(entry)
        else:
            raise ValueError(f"log gap: cannot append index {entry.index}, last={self.last_index}")

    def truncate_from(self, index: int) -> None:
        if index <= 1:
            self.entries = []
        elif index <= len(self.entries):
            self.entries = self.entries[: index - 1]

    def first_index_for_term(self, term: int) -> int:
        for entry in self.entries:
            if entry.term == term:
                return entry.index
        return 0

    def last_index_for_term(self, term: int) -> int:
        for entry in reversed(self.entries):
            if entry.term == term:
                return entry.index
        return 0

    def matches(self, prev_log_index: int, prev_log_term: int) -> bool:
        if prev_log_index == 0:
            return prev_log_term == 0
        return self.term_at(prev_log_index) == prev_log_term


class BpfLogDrainer:
    """Best-effort mirror of XDP fast-append metadata into follower userspace.

    The XDP program stores term/index metadata, not command payloads. That is
    enough for future prevLogTerm checks; COMMIT_NOTICE later carries the actual
    command bytes to apply in userspace.
    """

    def __init__(self, enabled: bool, raft_log: RaftLog, log_fn) -> None:
        self.enabled = enabled
        self.raft_log = raft_log
        self.log_fn = log_fn
        self.seen: Set[int] = set()

    def drain(self) -> None:
        if not self.enabled:
            return
        try:
            proc = subprocess.run(
                ["sudo", "bpftool", "-j", "map", "dump", "name", "raft_fast_log"],
                check=False,
                capture_output=True,
                text=True,
                timeout=0.2,
            )
        except Exception:
            return
        if proc.returncode != 0 or not proc.stdout.strip():
            return
        try:
            rows = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return
        candidates: List[Tuple[int, int]] = []
        for row in rows:
            key = self._bytes(row.get("key", []))
            value = self._bytes(row.get("value", []))
            if len(key) < 4 or len(value) < 4:
                continue
            index = int.from_bytes(key[:4], "little")
            term = int.from_bytes(value[:4], "little")
            if index <= 0 or index in self.seen:
                continue
            candidates.append((index, term))
        drained = 0
        for index, term in sorted(candidates):
            if index == self.raft_log.last_index + 1:
                self.raft_log.append_or_replace(LogEntry(index, term, b""))
                self.seen.add(index)
                drained += 1
        if drained:
            self.log_fn(f"drained {drained} XDP log metadata entries")

    @staticmethod
    def _bytes(items) -> bytes:
        out = bytearray()
        for item in items:
            if isinstance(item, str):
                out.append(int(item, 16))
            else:
                out.append(int(item))
        return bytes(out)


class RaftFollower:
    def __init__(
        self,
        bind: str,
        port: int,
        node_id: int,
        seed_terms: str = "",
        drain_bpf: bool = False,
        verbose: bool = False,
    ) -> None:
        self.bind = bind
        self.port = port
        self.node_id = node_id
        self.verbose = verbose
        self.current_term = 1
        self.leader_addr: Optional[Address] = None
        self.last_seen_leader_ns = 0
        self.log_store = RaftLog(parse_seed_log(seed_terms))
        self.commit_index = 0
        self.last_applied = 0
        self.append_count = 0
        self.commit_count = 0
        self.heartbeat_count = 0
        self.conflict_count = 0
        self.drainer = BpfLogDrainer(drain_bpf, self.log_store, self.log)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))

    def log(self, text: str) -> None:
        print(f"[follower {node_name()} id={self.node_id}] {text}", flush=True)

    def serve(self) -> None:
        self.log(
            "listening on %s:%d last_index=%d last_term=%d"
            % (self.bind, self.port, self.log_store.last_index, self.log_store.last_term)
        )
        while True:
            data, addr = self.sock.recvfrom(65535)
            self.drainer.drain()
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
            self.send_response(addr, msg.index, success=False)
            return

        if msg.term > self.current_term:
            self.current_term = msg.term
        self.leader_addr = addr
        self.last_seen_leader_ns = time.time_ns()

        if not self.log_store.matches(msg.prev_log_index, msg.prev_log_term):
            self.conflict_count += 1
            conflict_term, conflict_index = self.conflict_hint(msg.prev_log_index)
            self.sock.sendto(
                pack(
                    APPEND_RESPONSE,
                    term=self.current_term,
                    index=self.log_store.last_index,
                    prev_log_index=msg.prev_log_index,
                    prev_log_term=msg.prev_log_term,
                    leader_commit=self.commit_index,
                    node_id=self.node_id,
                    flags=0,
                    conflict_term=conflict_term,
                    conflict_index=conflict_index,
                ),
                addr,
            )
            self.log(
                "conflict prev=(idx=%d term=%d) local_last=(idx=%d term=%d) hint=(term=%d index=%d)"
                % (
                    msg.prev_log_index,
                    msg.prev_log_term,
                    self.log_store.last_index,
                    self.log_store.last_term,
                    conflict_term,
                    conflict_index,
                )
            )
            return

        if msg.index == 0 and not msg.payload:
            self.heartbeat_count += 1
        elif msg.index > 0:
            self.log_store.append_or_replace(LogEntry(msg.index, msg.term, msg.payload))
            self.append_count += 1

        self.advance_commit(msg.leader_commit)
        self.send_response(addr, msg.index if msg.index else self.log_store.last_index, success=True)

    def conflict_hint(self, prev_log_index: int) -> Tuple[int, int]:
        if prev_log_index > self.log_store.last_index:
            return 0, self.log_store.last_index + 1
        local_term = self.log_store.term_at(prev_log_index)
        if local_term <= 0:
            return 0, self.log_store.last_index + 1
        return local_term, self.log_store.first_index_for_term(local_term)

    def send_response(self, addr: Address, index: int, success: bool) -> None:
        self.sock.sendto(
            pack(
                APPEND_RESPONSE,
                term=self.current_term,
                index=index,
                leader_commit=self.commit_index,
                node_id=self.node_id,
                flags=FLAG_SUCCESS if success else 0,
            ),
            addr,
        )

    def handle_commit(self, msg, addr: Address) -> None:
        if msg.term < self.current_term:
            return
        if msg.term > self.current_term:
            self.current_term = msg.term
        self.leader_addr = addr
        self.last_seen_leader_ns = time.time_ns()

        if msg.index > 0 and msg.payload:
            try:
                self.log_store.append_or_replace(LogEntry(msg.index, msg.term, msg.payload))
            except ValueError:
                self.log(
                    "commit gap index=%d local_last=%d; waiting for AppendEntries repair"
                    % (msg.index, self.log_store.last_index)
                )

        self.advance_commit(max(msg.leader_commit, msg.index))
        if self.verbose or self.commit_count % 1000 == 0:
            self.log(
                "committed=%d commit_index=%d last_index=%d appends=%d conflicts=%d heartbeats=%d"
                % (
                    self.commit_count,
                    self.commit_index,
                    self.log_store.last_index,
                    self.append_count,
                    self.conflict_count,
                    self.heartbeat_count,
                )
            )

    def advance_commit(self, leader_commit: int) -> None:
        new_commit = min(leader_commit, self.log_store.last_index)
        while self.last_applied < new_commit:
            self.last_applied += 1
            self.commit_count += 1
        self.commit_index = max(self.commit_index, new_commit)


class RaftLeader:
    def __init__(
        self,
        follower_ips: Iterable[str],
        bind: str,
        port: int,
        term: int,
        timeout: float,
        heartbeat_interval: float,
        seed_terms: str = "",
        use_kernel_quorum: bool = False,
        wait_for_all: bool = False,
        verbose: bool = False,
    ) -> None:
        self.followers: List[Address] = [(ip, port) for ip in follower_ips]
        self.follower_ids: Dict[Address, int] = {
            addr: i + 2 for i, addr in enumerate(self.followers)
        }
        self.bind = bind
        self.port = port
        self.current_term = term
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.use_kernel_quorum = use_kernel_quorum
        self.wait_for_all = wait_for_all
        self.verbose = verbose
        self.cluster_size = 1 + len(self.followers)
        self.quorum = self.cluster_size // 2 + 1
        self.remote_quorum = self.quorum - 1
        self.log_store = RaftLog(parse_seed_log(seed_terms))
        self.commit_index = min(self.log_store.last_index, 0)
        initial_next = self.log_store.last_index + 1
        self.next_index: Dict[Address, int] = {
            follower: initial_next for follower in self.followers
        }
        self.match_index: Dict[Address, int] = {follower: 0 for follower in self.followers}
        self.running = True
        self.last_retries = 0
        self.last_conflict_hints = 0
        self.last_quorum_source = "userspace"
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
            for follower in self.followers:
                self.send_append(follower, heartbeat=True)
            time.sleep(self.heartbeat_interval)

    def send_append(self, follower: Address, heartbeat: bool = False) -> None:
        next_idx = self.next_index[follower]
        prev_idx = max(0, next_idx - 1)
        prev_term = self.log_store.term_at(prev_idx)
        node_id = self.follower_ids[follower]

        if heartbeat or next_idx > self.log_store.last_index:
            frame = pack(
                APPEND_ENTRIES,
                term=self.current_term,
                index=0,
                prev_log_index=prev_idx,
                prev_log_term=max(prev_term, 0),
                leader_commit=self.commit_index,
                node_id=node_id,
            )
        else:
            entry = self.log_store.entry_at(next_idx)
            if entry is None:
                return
            frame = pack(
                APPEND_ENTRIES,
                term=self.current_term,
                index=entry.index,
                prev_log_index=prev_idx,
                prev_log_term=max(prev_term, 0),
                leader_commit=self.commit_index,
                node_id=node_id,
                payload=entry.command,
            )
        self.sock.sendto(frame, follower)

    def replicate(self, payload: bytes) -> int:
        entry = LogEntry(self.log_store.last_index + 1, self.current_term, payload)
        self.log_store.append_or_replace(entry)
        target_index = entry.index
        accepted: Set[str] = {"leader-local"}
        self.last_retries = 0
        self.last_conflict_hints = 0
        self.last_quorum_source = "userspace"

        for follower in self.followers:
            self.send_append(follower)

        deadline = time.monotonic() + self.timeout
        last_resend = 0.0
        while time.monotonic() < deadline:
            if self.has_quorum(target_index, accepted):
                if not self.wait_for_all:
                    break
                if all(self.match_index[f] >= target_index for f in self.followers):
                    break

            now = time.monotonic()
            if now - last_resend > 0.05:
                for follower in self.followers:
                    if self.match_index[follower] < target_index:
                        self.send_append(follower)
                        self.last_retries += 1
                last_resend = now

            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                msg = unpack(data)
            except ValueError:
                continue
            if msg.msg_type != APPEND_RESPONSE or msg.term != self.current_term:
                if msg.msg_type == NACK and msg.term > self.current_term:
                    raise RuntimeError(f"higher term observed: {msg.term}")
                continue

            if msg.success:
                if msg.index == 0:
                    continue
                matched = msg.index
                self.match_index[addr] = max(self.match_index.get(addr, 0), matched)
                self.next_index[addr] = max(self.next_index.get(addr, 1), matched + 1)
                accepted.add(addr[0])
                if msg.quorum_reached:
                    for follower in self.followers:
                        self.match_index[follower] = max(self.match_index[follower], matched)
                        self.next_index[follower] = max(self.next_index[follower], matched + 1)
                    self.last_quorum_source = "ebpf"
                    break
            else:
                self.last_conflict_hints += 1
                self.apply_conflict_hint(addr, msg)
                self.send_append(addr)
                self.last_retries += 1

        if not self.has_quorum(target_index, accepted) and self.last_quorum_source != "ebpf":
            replicated = 1 + sum(1 for idx in self.match_index.values() if idx >= target_index)
            raise TimeoutError(
                f"index {target_index}: got {replicated} replicas, need {self.quorum}"
            )

        self.commit_index = target_index
        self.broadcast_commit(entry)
        return target_index

    def has_quorum(self, target_index: int, accepted: Set[str]) -> bool:
        if self.last_quorum_source == "ebpf":
            return True
        replicated = 1 + sum(1 for idx in self.match_index.values() if idx >= target_index)
        return replicated >= self.quorum or len(accepted) >= self.quorum

    def apply_conflict_hint(self, follower: Address, msg) -> None:
        if msg.conflict_term:
            leader_last_for_term = self.log_store.last_index_for_term(msg.conflict_term)
            if leader_last_for_term:
                self.next_index[follower] = leader_last_for_term + 1
            else:
                self.next_index[follower] = max(1, msg.conflict_index)
        else:
            self.next_index[follower] = max(1, msg.conflict_index)
        self.log(
            "optimized retry for %s conflict_term=%d conflict_index=%d next_index=%d"
            % (
                follower[0],
                msg.conflict_term,
                msg.conflict_index,
                self.next_index[follower],
            )
        )

    def broadcast_commit(self, entry: LogEntry) -> None:
        prev_idx = max(0, entry.index - 1)
        prev_term = max(0, self.log_store.term_at(prev_idx))
        for follower in self.followers:
            self.sock.sendto(
                pack(
                    COMMIT_NOTICE,
                    term=self.current_term,
                    index=entry.index,
                    prev_log_index=prev_idx,
                    prev_log_term=prev_term,
                    leader_commit=self.commit_index,
                    node_id=self.follower_ids[follower],
                    payload=entry.command,
                ),
                follower,
            )

    def serve(self) -> None:
        self.log(
            "listening on %s:%d followers=%d quorum=%d last_index=%d kernel_quorum=%s"
            % (
                self.bind,
                self.port,
                len(self.followers),
                self.quorum,
                self.log_store.last_index,
                self.use_kernel_quorum,
            )
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
                reply = json.dumps(
                    {
                        "index": index,
                        "latency_us": elapsed_us,
                        "retries": self.last_retries,
                        "conflict_hints": self.last_conflict_hints,
                        "quorum_source": self.last_quorum_source,
                    },
                    separators=(",", ":"),
                ).encode()
                self.sock.sendto(
                    pack(
                        CLIENT_REPLY,
                        term=self.current_term,
                        index=index,
                        leader_commit=self.commit_index,
                        flags=FLAG_SUCCESS,
                        payload=reply,
                    ),
                    client_addr,
                )
                completed += 1
                if self.verbose or completed % 1000 == 0:
                    self.log(
                        "committed=%d commit_index=%d retries=%d conflicts=%d quorum=%s"
                        % (
                            completed,
                            self.commit_index,
                            self.last_retries,
                            self.last_conflict_hints,
                            self.last_quorum_source,
                        )
                    )
            except Exception as exc:
                self.sock.sendto(
                    pack(
                        NACK,
                        term=self.current_term,
                        index=self.commit_index,
                        leader_commit=self.commit_index,
                        payload=str(exc).encode(),
                    ),
                    client_addr,
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["leader", "follower"], required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=RAFT_PORT)
    parser.add_argument("--node-id", type=int, default=0)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--heartbeat-interval", type=float, default=0.2)
    parser.add_argument("--seed-log-terms", default="")
    parser.add_argument("--drain-bpf", action="store_true")
    parser.add_argument("--use-kernel-quorum", action="store_true")
    parser.add_argument("--wait-for-all", action="store_true")
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
            seed_terms=args.seed_log_terms,
            use_kernel_quorum=args.use_kernel_quorum,
            wait_for_all=args.wait_for_all,
            verbose=args.verbose,
        ).serve()
    else:
        node_id = args.node_id or int(os.environ.get("NODE_ID", "0") or 0)
        RaftFollower(
            args.bind,
            args.port,
            node_id=node_id,
            seed_terms=args.seed_log_terms,
            drain_bpf=args.drain_bpf,
            verbose=args.verbose,
        ).serve()


if __name__ == "__main__":
    main()
