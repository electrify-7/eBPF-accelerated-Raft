# Electrode Raft Lab

This repo contains a small four-node Raft benchmark designed for your Multipass
VMs:

- `node1`: fixed Raft leader
- `node2`, `node3`, `node4`: Raft followers
- host machine: benchmark client and analysis

The baseline path runs all Raft follower logic in Python. The XDP path attaches
an eBPF program to the followers that fast-acks Raft `AppendEntries` packets and
heartbeats in the kernel, records volatile state in BPF maps, emits ring-buffer
events, and keeps those packets away from follower userspace.

This is an experiment harness, not a production Raft implementation. It uses a
fixed leader to focus on replication latency. The XDP fast path keeps volatile
in-kernel observations and does not replace durable Raft logging.

## Quick Start

```bash
./01_setup_host.sh
./02_provision_vms.sh
./03_deploy_raft.sh
./benchmark/run_raft_experiment.sh
```

Results are written under `results_YYYYmmdd_HHMMSS/`.

## Useful Manual Commands

```bash
multipass list
multipass shell node1
multipass shell node2
```

On a follower VM:

```bash
cd ~/electrode-lab/xdp
make
make load IFACE=ens3
make unload IFACE=ens3
make stats
```

Use the actual VM interface name from:

```bash
ip -o link show | grep -v lo | awk -F': ' '{print $2}' | head -1
```
