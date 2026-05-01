#!/usr/bin/env python3
"""Experiment API for the three-node Raft/eBPF lab.

Importable API:
    run_without_ebpf()
    run_with_ebpf()

Both functions return a dictionary with CSV paths, summary paths, node logs, and
XDP map dumps where applicable.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ExperimentConfig:
    requests: int = int(os.environ.get("N_REQUESTS", "5"))
    payload_bytes: int = int(os.environ.get("PAYLOAD_BYTES", "64"))
    heartbeat_interval: float = float(os.environ.get("HEARTBEAT_INTERVAL", "0.1"))
    timeout: float = float(os.environ.get("RAFT_TIMEOUT", "2.0"))
    lag_demo: bool = os.environ.get("LAG_DEMO", "1") != "0"
    leader_seed_terms: str = os.environ.get("LEADER_SEED_TERMS", "1,1,1,4,4")
    follower_seed_terms: str = os.environ.get("FOLLOWER_SEED_TERMS", "1,1,1,4,4")
    lagged_seed_terms: str = os.environ.get("LAGGED_FOLLOWER_SEED_TERMS", "1,1,1,3,3")
    enable_leader_quorum: bool = os.environ.get("ENABLE_LEADER_QUORUM", "1") != "0"
    enable_tc_broadcast: bool = os.environ.get("ENABLE_TC_BROADCAST", "1") != "0"


def sh(cmd: List[str], *, cwd: Path = ROOT, check: bool = True, capture: bool = True) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=capture,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            "command failed (%s):\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(cmd), proc.stdout, proc.stderr)
        )
    return proc.stdout if capture else ""


def multipass(vm: str, command: str, *, check: bool = True) -> str:
    return sh(["multipass", "exec", vm, "--", "bash", "-c", command], check=check)


def ip_of(vm: str) -> str:
    out = sh(["multipass", "info", vm])
    for line in out.splitlines():
        if "IPv4" in line:
            return line.split()[1]
    raise RuntimeError(f"could not find IPv4 for {vm}")


def iface_of(vm: str) -> str:
    return multipass(
        vm,
        "ip -o link show | grep -v lo | awk -F': ' '{print $2}' | head -1",
    ).strip()


def discover_cluster() -> Dict[str, object]:
    nodes = ["node1", "node2", "node3"]
    return {
        "ips": {vm: ip_of(vm) for vm in nodes},
        "ifaces": {vm: iface_of(vm) for vm in nodes},
    }


def stop_all() -> None:
    for vm in ["node1", "node2", "node3"]:
        multipass(vm, "pkill -f 'python3.*raft_node.py' 2>/dev/null; true", check=False)
    time.sleep(1)


def unload_ebpf(cluster: Dict[str, object]) -> None:
    ifaces: Dict[str, str] = cluster["ifaces"]  # type: ignore[assignment]
    for vm in ["node1", "node2", "node3"]:
        iface = ifaces[vm]
        multipass(
            vm,
            f"cd ~/electrode-lab/xdp && sudo make unload IFACE={iface} 2>/dev/null; true",
            check=False,
        )


def compile_and_load_ebpf(cluster: Dict[str, object], cfg: ExperimentConfig) -> None:
    ifaces: Dict[str, str] = cluster["ifaces"]  # type: ignore[assignment]

    for vm in ["node2", "node3"]:
        multipass(vm, "cd ~/electrode-lab/xdp && make clean && make follower")
        multipass(vm, f"cd ~/electrode-lab/xdp && sudo make load-follower IFACE={ifaces[vm]}")

    if cfg.enable_leader_quorum:
        multipass("node1", "cd ~/electrode-lab/xdp && make leader")
        multipass("node1", f"cd ~/electrode-lab/xdp && sudo make load-quorum IFACE={ifaces['node1']}")

    if cfg.enable_tc_broadcast:
        multipass("node1", "cd ~/electrode-lab/xdp && make leader")
        multipass(
            "node1",
            f"cd ~/electrode-lab/xdp && sudo make load-broadcast IFACE={ifaces['node1']}",
        )
        ips: Dict[str, str] = cluster["ips"]  # type: ignore[assignment]
        multipass(
            "node1",
            "cd ~/electrode-lab && "
            f"sudo python3 xdp/configure_broadcast.py {ifaces['node1']} "
            f"{ips['node2']} {ips['node3']}",
        )


def seed_for(vm: str, cfg: ExperimentConfig) -> str:
    if not cfg.lag_demo:
        return ""
    if vm == "node3":
        return cfg.lagged_seed_terms
    return cfg.follower_seed_terms

def start_followers(label: str, cfg: ExperimentConfig, drain_bpf: bool) -> None:
    for node_id, vm in enumerate(["node2", "node3"], start=2):
        args = [
            "python3 protocol/raft_node.py --role follower",
            f"--node-id {node_id}",
            f"--timeout {cfg.timeout}",
        ]
        seed = seed_for(vm, cfg)
        if seed:
            args.append(f"--seed-log-terms {seed}")
        if drain_bpf:
            args.append("--drain-bpf")
        cmd = " ".join(args)
        
        full_cmd = f"cd ~/electrode-lab && {cmd} > /tmp/raft_follower_{label}.log 2>&1"
        subprocess.Popen(
            ["multipass", "exec", vm, "--", "bash", "-c", full_cmd], 
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    time.sleep(1)


def start_leader(label: str, cfg: ExperimentConfig, cluster: Dict[str, object], *, use_kernel_quorum: bool) -> None:
    ips: Dict[str, str] = cluster["ips"]  # type: ignore[assignment]
    args = [
        "python3 protocol/raft_node.py --role leader",
        "--node-id 1",
        f"--timeout {cfg.timeout}",
        f"--heartbeat-interval {cfg.heartbeat_interval}",
    ]
    if cfg.lag_demo:
        args.append(f"--seed-log-terms {cfg.leader_seed_terms}")
    if use_kernel_quorum:
        args.append("--use-kernel-quorum")
    elif cfg.lag_demo:
        args.append("--wait-for-all")
    if cfg.enable_tc_broadcast:
        args.append("--use-kernel-broadcast")
    args.extend([ips["node2"], ips["node3"]])
    cmd = " ".join(args)
    
    full_cmd = f"cd ~/electrode-lab && {cmd} > /tmp/raft_leader_{label}.log 2>&1"
    subprocess.Popen(
        ["multipass", "exec", "node1", "--", "bash", "-c", full_cmd], 
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)


def run_client(label: str, cfg: ExperimentConfig, cluster: Dict[str, object], results_dir: Path) -> Dict[str, str]:
    ips: Dict[str, str] = cluster["ips"]  # type: ignore[assignment]
    csv_path = results_dir / f"{label}.csv"
    summary_path = results_dir / f"{label}_summary.txt"
    cmd = [
        sys.executable,
        "protocol/raft_client.py",
        ips["node1"],
        "--count",
        str(cfg.requests),
        "--payload-bytes",
        str(cfg.payload_bytes),
        "--timeout",
        str(cfg.timeout),
        "--out",
        str(csv_path),
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, check=False)
    summary_path.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    # if proc.returncode != 0:
    #     raise RuntimeError(f"client failed for {label}; see {summary_path}")
    return {"csv": str(csv_path), "summary": str(summary_path)}


def collect_logs(label: str, results_dir: Path, include_ebpf: bool) -> Dict[str, str]:
    out: Dict[str, str] = {}
    leader_log = results_dir / f"node1_leader_{label}.log"
    leader_log.write_text(
        multipass("node1", f"cat /tmp/raft_leader_{label}.log 2>/dev/null || true", check=False),
        encoding="utf-8",
    )
    out["node1_leader_log"] = str(leader_log)

    for vm in ["node2", "node3"]:
        path = results_dir / f"{vm}_follower_{label}.log"
        path.write_text(
            multipass(vm, f"cat /tmp/raft_follower_{label}.log 2>/dev/null || true", check=False),
            encoding="utf-8",
        )
        out[f"{vm}_follower_log"] = str(path)

    if include_ebpf:
        for vm in ["node1", "node2", "node3"]:
            stats = results_dir / f"{vm}_ebpf_{label}.txt"
            stats.write_text(
                multipass(
                    vm,
                    "cd ~/electrode-lab/xdp && sudo make stats 2>/dev/null || true",
                    check=False,
                ),
                encoding="utf-8",
            )
            out[f"{vm}_ebpf_stats"] = str(stats)
    return out


def result_dir(path: Optional[str] = None) -> Path:
    if path:
        out = Path(path)
    else:
        out = ROOT / f"results_{time.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def run_without_ebpf(results_dir: Optional[str] = None, config: Optional[ExperimentConfig] = None) -> Dict[str, object]:
    cfg = config or ExperimentConfig()
    bench_cfg = replace(cfg, lag_demo=False)
    out_dir = result_dir(results_dir)
    cluster = discover_cluster()
    stop_all()
    unload_ebpf(cluster)
    start_followers("baseline", bench_cfg, drain_bpf=False)
    start_leader("baseline", bench_cfg, cluster, use_kernel_quorum=False)
    client = run_client("baseline", bench_cfg, cluster, out_dir)
    logs = collect_logs("baseline", out_dir, include_ebpf=False)
    stop_all()
    conflict = {}
    if cfg.lag_demo:
        conflict_cfg = replace(cfg, requests=1, lag_demo=True)
        start_followers("baseline_conflict", conflict_cfg, drain_bpf=False)
        start_leader("baseline_conflict", conflict_cfg, cluster, use_kernel_quorum=False)
        conflict_client = run_client("baseline_conflict", conflict_cfg, cluster, out_dir)
        conflict_logs = collect_logs("baseline_conflict", out_dir, include_ebpf=False)
        stop_all()
        conflict = {"client": conflict_client, "logs": conflict_logs}
    return {
        "mode": "baseline",
        "results_dir": str(out_dir),
        "client": client,
        "logs": logs,
        "conflict_probe": conflict,
    }


def run_with_ebpf(results_dir: Optional[str] = None, config: Optional[ExperimentConfig] = None) -> Dict[str, object]:
    cfg = config or ExperimentConfig()
    bench_cfg = replace(cfg, lag_demo=False)
    out_dir = result_dir(results_dir)
    cluster = discover_cluster()
    stop_all()
    unload_ebpf(cluster)
    compile_and_load_ebpf(cluster, bench_cfg)
    start_followers("xdp", bench_cfg, drain_bpf=True)
    start_leader("xdp", bench_cfg, cluster, use_kernel_quorum=bench_cfg.enable_leader_quorum)
    client = run_client("xdp", bench_cfg, cluster, out_dir)
    logs = collect_logs("xdp", out_dir, include_ebpf=True)
    stop_all()
    unload_ebpf(cluster)
    conflict = {}
    if cfg.lag_demo:
        conflict_cfg = replace(cfg, requests=1, lag_demo=True, enable_leader_quorum=False)
        compile_and_load_ebpf(cluster, conflict_cfg)
        start_followers("xdp_conflict", conflict_cfg, drain_bpf=True)
        start_leader("xdp_conflict", conflict_cfg, cluster, use_kernel_quorum=False)
        conflict_client = run_client("xdp_conflict", conflict_cfg, cluster, out_dir)
        conflict_logs = collect_logs("xdp_conflict", out_dir, include_ebpf=True)
        stop_all()
        unload_ebpf(cluster)
        conflict = {"client": conflict_client, "logs": conflict_logs}
    return {
        "mode": "xdp",
        "results_dir": str(out_dir),
        "client": client,
        "logs": logs,
        "conflict_probe": conflict,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["without", "with", "both"], default="both", nargs="?")
    parser.add_argument("--results-dir", default="")
    args = parser.parse_args()

    cfg = ExperimentConfig()
    out_dir = result_dir(args.results_dir or None)
    results: Dict[str, object] = {}
    try:
        if args.mode in {"without", "both"}:
            results["baseline"] = run_without_ebpf(str(out_dir), cfg)
        if args.mode in {"with", "both"}:
            results["xdp"] = run_with_ebpf(str(out_dir), cfg)
        if args.mode == "both":
            sh(
                [
                    sys.executable,
                    "benchmark/analyze_raft.py",
                    str(out_dir / "baseline.csv"),
                    str(out_dir / "xdp.csv"),
                    "--out",
                    str(out_dir / "comparison.csv"),
                ],
                capture=False,
            )
    finally:
        try:
            cluster = discover_cluster()
            stop_all()
            unload_ebpf(cluster)
        except Exception:
            pass

    print(f"results_dir={out_dir}")


if __name__ == "__main__":
    main()
