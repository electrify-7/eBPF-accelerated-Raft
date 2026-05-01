#!/usr/bin/env python3
"""Wire format helpers for the Raft/eBPF benchmark.

This protocol is intentionally UDP-based so XDP can parse and rewrite packets
without dealing with TCP sequence state.

Header layout, in network byte order:
    u8  message type
    u32 term
    u32 index              entry index, or matched index in responses
    u32 prev_log_index
    u32 prev_log_term
    u32 leader_commit
    u16 node_id            follower id in AppendEntries/AppendResponse
    u16 flags              SUCCESS / QUORUM_REACHED / EBPF_FAST
    u32 conflict_term      optimized Raft conflict hint
    u32 conflict_index     optimized Raft conflict hint
    u16 payload length
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


RAFT_PORT = 9000
HEADER = struct.Struct("!BIIIIIHHIIH")
HEADER_LEN = HEADER.size

CLIENT_REQUEST = 0x01
CLIENT_REPLY = 0x02
APPEND_ENTRIES = 0x20
APPEND_RESPONSE = 0x21
COMMIT_NOTICE = 0x30
NACK = 0x7F

FLAG_SUCCESS = 1 << 0
FLAG_QUORUM_REACHED = 1 << 1
FLAG_EBPF_FAST = 1 << 2
FLAG_BROADCAST_REQUEST = 1 << 3

TYPE_NAMES = {
    CLIENT_REQUEST: "CLIENT_REQUEST",
    CLIENT_REPLY: "CLIENT_REPLY",
    APPEND_ENTRIES: "APPEND_ENTRIES",
    APPEND_RESPONSE: "APPEND_RESPONSE",
    COMMIT_NOTICE: "COMMIT_NOTICE",
    NACK: "NACK",
}


@dataclass(frozen=True)
class Message:
    msg_type: int
    term: int
    index: int
    prev_log_index: int
    prev_log_term: int
    leader_commit: int
    node_id: int
    flags: int
    conflict_term: int
    conflict_index: int
    payload: bytes = b""

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.msg_type, f"UNKNOWN({self.msg_type})")

    @property
    def success(self) -> bool:
        return bool(self.flags & FLAG_SUCCESS)

    @property
    def quorum_reached(self) -> bool:
        return bool(self.flags & FLAG_QUORUM_REACHED)


def pack(
    msg_type: int,
    term: int = 0,
    index: int = 0,
    prev_log_index: int = 0,
    prev_log_term: int = 0,
    leader_commit: int = 0,
    node_id: int = 0,
    flags: int = 0,
    conflict_term: int = 0,
    conflict_index: int = 0,
    payload: bytes = b"",
) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for UDP benchmark frame")
    return (
        HEADER.pack(
            msg_type,
            term,
            index,
            prev_log_index,
            prev_log_term,
            leader_commit,
            node_id,
            flags,
            conflict_term,
            conflict_index,
            len(payload),
        )
        + payload
    )


def unpack(data: bytes) -> Message:
    if len(data) < HEADER_LEN:
        raise ValueError("datagram shorter than Raft header")
    (
        msg_type,
        term,
        index,
        prev_log_index,
        prev_log_term,
        leader_commit,
        node_id,
        flags,
        conflict_term,
        conflict_index,
        payload_len,
    ) = HEADER.unpack_from(data)
    end = HEADER_LEN + payload_len
    if end > len(data):
        raise ValueError("datagram payload length exceeds packet size")
    return Message(
        msg_type,
        term,
        index,
        prev_log_index,
        prev_log_term,
        leader_commit,
        node_id,
        flags,
        conflict_term,
        conflict_index,
        data[HEADER_LEN:end],
    )


def csv_header() -> str:
    return (
        "seq,status,latency_us,payload_bytes,leader_index,"
        "leader_retries,conflict_hints,quorum_source\n"
    )
