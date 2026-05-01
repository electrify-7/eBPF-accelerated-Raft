#!/usr/bin/env python3
"""Compare baseline and XDP Raft benchmark CSV files."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path
from typing import Dict, List


def load_rows(path: Path) -> Dict[str, List[int]]:
    values: Dict[str, List[int]] = {
        "latency_us": [],
        "leader_retries": [],
        "conflict_hints": [],
        "kernel_quorum": [],
    }
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["status"] == "ok":
                values["latency_us"].append(int(row["latency_us"]))
                values["leader_retries"].append(int(row.get("leader_retries", 0) or 0))
                values["conflict_hints"].append(int(row.get("conflict_hints", 0) or 0))
                values["kernel_quorum"].append(1 if row.get("quorum_source") == "ebpf" else 0)
    return values


def percentile(values: List[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def summarize(name: str, rows: Dict[str, List[int]]) -> Dict[str, float]:
    values = rows["latency_us"]
    return {
        "name": name,
        "count": len(values),
        "mean_us": statistics.mean(values) if values else 0,
        "median_us": statistics.median(values) if values else 0,
        "p95_us": percentile(values, 95),
        "p99_us": percentile(values, 99),
        "leader_retries": sum(rows["leader_retries"]),
        "conflict_hints": sum(rows["conflict_hints"]),
        "kernel_quorum_replies": sum(rows["kernel_quorum"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_csv", type=Path)
    parser.add_argument("xdp_csv", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="optional CSV summary path")
    args = parser.parse_args()

    baseline = summarize("baseline", load_rows(args.baseline_csv))
    xdp = summarize("xdp", load_rows(args.xdp_csv))
    speedup = (
        baseline["mean_us"] / xdp["mean_us"]
        if baseline["mean_us"] and xdp["mean_us"]
        else 0
    )
    reduction = (
        (baseline["mean_us"] - xdp["mean_us"]) / baseline["mean_us"] * 100.0
        if baseline["mean_us"]
        else 0
    )

    lines = [
        "metric,baseline,xdp",
        f"count,{baseline['count']},{xdp['count']}",
        f"mean_us,{baseline['mean_us']:.1f},{xdp['mean_us']:.1f}",
        f"median_us,{baseline['median_us']:.1f},{xdp['median_us']:.1f}",
        f"p95_us,{baseline['p95_us']},{xdp['p95_us']}",
        f"p99_us,{baseline['p99_us']},{xdp['p99_us']}",
        f"leader_retries,{baseline['leader_retries']},{xdp['leader_retries']}",
        f"conflict_hints,{baseline['conflict_hints']},{xdp['conflict_hints']}",
        f"kernel_quorum_replies,{baseline['kernel_quorum_replies']},{xdp['kernel_quorum_replies']}",
        f"mean_latency_speedup,{speedup:.3f},",
        f"mean_latency_reduction_pct,{reduction:.1f},",
    ]
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
