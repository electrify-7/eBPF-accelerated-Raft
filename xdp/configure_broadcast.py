#!/usr/bin/env python3
"""Populate the leader TC fan-out map.

Run on node1 after loading raft_leader_broadcast_tc.o:

    sudo python3 xdp/configure_broadcast.py ens3 10.0.0.2 10.0.0.3
"""

from __future__ import annotations

import argparse
import socket
import subprocess
from pathlib import Path


def run(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True).stdout


def hex_bytes(data: bytes) -> list[str]:
    return [f"{b:02x}" for b in data]


def neigh_mac(iface: str, ip: str) -> bytes:
    subprocess.run(["ping", "-c", "1", "-W", "1", ip], check=False, capture_output=True)
    out = run(["ip", "neigh", "show", "to", ip, "dev", iface])
    parts = out.split()
    if "lladdr" not in parts:
        raise RuntimeError(f"no lladdr for {ip} on {iface}: {out.strip()}")
    mac = parts[parts.index("lladdr") + 1]
    return bytes(int(part, 16) for part in mac.split(":"))


def update_slot(slot: int, iface: str, ip: str, node_id: int) -> None:
    ifindex = int(Path(f"/sys/class/net/{iface}/ifindex").read_text().strip())
    value = (
        ifindex.to_bytes(4, "little")
        + socket.inet_aton(ip)
        + neigh_mac(iface, ip)
        + node_id.to_bytes(2, "little")
        + b"\x00\x00\x00\x00"
    )
    key = slot.to_bytes(4, "little")
    subprocess.run(
        [
            "sudo",
            "bpftool",
            "map",
            "update",
            "name",
            "raft_fanout",
            "key",
            "hex",
            *hex_bytes(key),
            "value",
            "hex",
            *hex_bytes(value),
        ],
        check=True,
    )
    print(f"slot={slot} ifindex={ifindex} ip={ip} node_id={node_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("iface")
    parser.add_argument("followers", nargs=2)
    args = parser.parse_args()

    for slot, ip in enumerate(args.followers):
        update_slot(slot, args.iface, ip, node_id=slot + 2)


if __name__ == "__main__":
    main()
