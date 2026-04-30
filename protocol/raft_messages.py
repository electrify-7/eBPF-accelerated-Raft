#!/usr/bin/env python3
"""Wire format helpers for the Raft/XDP benchmark.

Header layout, in network byte order:
    u8  message type
    u32 term
    u32 log index
    u32 leader commit index
    u16 payload length

The XDP program parses this same header. It fast-acks APPEND_ENTRIES messages
from the learned leader and lets COMMIT_NOTICE packets reach follower userspace.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


RAFT_PORT = 9000
HEADER = struct.Struct("!BIIIH")
HEADER_LEN = HEADER.size

CLIENT_REQUEST = 0x01
CLIENT_REPLY = 0x02
APPEND_ENTRIES = 0x20
APPEND_RESPONSE = 0x21
COMMIT_NOTICE = 0x30
NACK = 0x7F

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
    commit_index: int
    payload: bytes = b""

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.msg_type, f"UNKNOWN({self.msg_type})")


def pack(
    msg_type: int,
    term: int = 0,
    index: int = 0,
    commit_index: int = 0,
    payload: bytes = b"",
) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for UDP benchmark frame")
    return HEADER.pack(msg_type, term, index, commit_index, len(payload)) + payload


def unpack(data: bytes) -> Message:
    if len(data) < HEADER_LEN:
        raise ValueError("datagram shorter than Raft header")
    msg_type, term, index, commit_index, payload_len = HEADER.unpack_from(data)
    end = HEADER_LEN + payload_len
    if end > len(data):
        raise ValueError("datagram payload length exceeds packet size")
    return Message(msg_type, term, index, commit_index, data[HEADER_LEN:end])


def csv_header() -> str:
    return "seq,status,latency_us,payload_bytes\n"
