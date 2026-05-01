# Electrode Raft Lab

This repo contains a small three-node Raft benchmark designed for your Multipass
VMs:

- `node1`: fixed Raft leader
- `node2`, `node3`: Raft followers
- host machine: benchmark client and analysis

The baseline path runs all Raft follower logic in Python. The eBPF path uses:

- follower XDP fast ACK for `AppendEntries` when `prevLogIndex/prevLogTerm`
  matches the in-kernel last-log metadata
- follower XDP pass-up on mismatch, so Python returns optimized Raft conflict
  hints (`conflictTerm`, `conflictIndex`)
- leader XDP quorum filtering, where follower ACKs are dropped until the remote
  quorum needed for a three-node Raft majority is reached, and the
  quorum-reaching ACK is marked for userspace
- leader TC egress broadcast using destination IP/MAC rewrites plus
  `bpf_clone_redirect()`

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

The experiment API is importable:

```python
from benchmark.raft_experiment import run_without_ebpf, run_with_ebpf

baseline = run_without_ebpf()
xdp = run_with_ebpf()
```

By default, the main `baseline.csv` and `xdp.csv` runs measure normal steady
state replication. The experiment also writes one-request conflict probes
(`baseline_conflict.csv`, `xdp_conflict.csv`) where node3 starts with a
divergent log so the leader exercises optimized Raft backtracking.

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
make load-follower IFACE=ens3
make unload IFACE=ens3
make stats
```

On the leader VM:

```bash
cd ~/electrode-lab/xdp
make leader
make load-quorum IFACE=ens3
make load-broadcast IFACE=ens3
sudo python3 configure_broadcast.py ens3 <node2-ip> <node3-ip>
```

Use the actual VM interface name from:

```bash
ip -o link show | grep -v lo | awk -F': ' '{print $2}' | head -1
```
