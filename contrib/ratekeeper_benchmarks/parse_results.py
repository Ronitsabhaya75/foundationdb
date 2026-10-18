#!/usr/bin/env python3
"""Convert FDB simulation benchmark output into research-friendly evidence."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path


METRIC_RE = re.compile(
    r"^Metric \([^)]*\):\s*(?P<name>.*?),\s*"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?),"
)
PERIODIC_RE = re.compile(r"T=(?P<second>\d+)s:(?P<metric>.+)$")
EVENT_RE = re.compile(r"<Event\s+(?P<attrs>.*?)/?>")
ATTRIBUTE_RE = re.compile(r'(\w+)="([^"]*)"')

FAULT_WINDOWS = {
    "network_clog_mild": (0.0, 25.0),
    "network_clog_severe": (0.0, 30.0),
    "slow_storage": (10.0, 35.0),
}


def finite_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_metric_name(name: str) -> tuple[float | None, str]:
    # Remove the workload prefix while preserving the actual metric text.
    short = name.split(".", 1)[-1]
    match = PERIODIC_RE.search(short)
    if match:
        return float(match.group("second")), match.group("metric")
    return None, short


def parse_log(path: Path, scenario: str, seed: int) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        match = METRIC_RE.match(line.strip())
        if not match:
            continue
        value = finite_float(match.group("value"))
        if value is None:
            continue
        second, metric = parse_metric_name(match.group("name"))
        rows.append(
            {
                "scenario": scenario,
                "seed": seed,
                "time_seconds": second,
                "source": "workload",
                "metric": metric,
                "value": value,
            }
        )
    return rows


def parse_trace(path: Path, scenario: str, seed: int) -> list[dict]:
    rows: list[dict] = []
    wanted = {
        "TPSLimit",
        "ReleasedTPS",
        "TPSBasis",
        "WorstStorageServerQueue",
        "LimitingStorageServerQueue",
        "WorstTLogQueue",
        "WorstStorageServerVersionLag",
        "LimitingStorageServerVersionLag",
        "WorstStorageServerDurabilityLag",
        "LimitingStorageServerDurabilityLag",
    }
    for line in path.read_text(errors="replace").splitlines():
        event_match = EVENT_RE.search(line)
        if not event_match:
            continue
        attrs = {
            key: html.unescape(value)
            for key, value in ATTRIBUTE_RE.findall(event_match.group("attrs"))
        }
        event_type = attrs.get("Type", "")
        event_time = finite_float(attrs.get("Time", ""))

        if event_type.startswith("RkUpdate"):
            for metric in wanted:
                value = finite_float(attrs.get(metric, ""))
                if value is not None:
                    rows.append(
                        {
                            "scenario": scenario,
                            "seed": seed,
                            "time_seconds": event_time,
                            "source": event_type,
                            "metric": metric,
                            "value": value,
                        }
                    )

        if event_type.endswith("_TotalLatency"):
            for field, label in (
                ("Percentile95", "Trace p95 latency (s)"),
                ("Percentile99", "Trace p99 latency (s)"),
                ("Percentile99_9", "Trace p99.9 latency (s)"),
                ("Max", "Trace max latency (s)"),
            ):
                value = finite_float(attrs.get(field, ""))
                if value is not None:
                    rows.append(
                        {
                            "scenario": scenario,
                            "seed": seed,
                            "time_seconds": event_time,
                            "source": event_type,
                            "metric": label,
                            "value": value,
                        }
                    )
    return rows


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def periodic_values(rows: list[dict], scenario: str, metric: str) -> dict[float, list[float]]:
    values: dict[float, list[float]] = defaultdict(list)
    for row in rows:
        if (
            row["scenario"] == scenario
            and row["source"] == "workload"
            and row["metric"] == metric
            and row["time_seconds"] is not None
        ):
            values[row["time_seconds"]].append(row["value"])
    return values


def recovery_seconds(
    rows: list[dict], scenario: str, fault_end: float, baseline_ops: float, baseline_p95: float
) -> float | None:
    ops = periodic_values(rows, scenario, "Operations/sec")
    p95 = periodic_values(rows, scenario, "95% Latency (ms, averaged)")
    times = sorted(set(ops) & set(p95))
    good_streak = 0
    for second in times:
        if second <= fault_end:
            continue
        recovered = (
            median(ops[second]) >= baseline_ops * 0.90
            and median(p95[second]) <= baseline_p95 * 1.25
        )
        good_streak = good_streak + 1 if recovered else 0
        if good_streak >= 2:
            return max(0.0, second - 5.0 - fault_end)
    return None


def fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def build_evidence(rows: list[dict], statuses: dict[str, str]) -> str:
    baseline_ops_by_time = periodic_values(rows, "baseline", "Operations/sec")
    baseline_p95_by_time = periodic_values(rows, "baseline", "95% Latency (ms, averaged)")
    baseline_ops = median([v for values in baseline_ops_by_time.values() for v in values])
    baseline_p95 = median([v for values in baseline_p95_by_time.values() for v in values])

    lines = [
        "# Ratekeeper degradation benchmark",
        "",
        "This report is observational: performance differences are recorded but are not yet CI failure thresholds.",
        "",
        "| Scenario | Runs | Status | Fault-window p95 max (ms) | p95 / baseline | Fault-window minimum ops/s | Throughput drop | Recovery after fault |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]

    scenarios = sorted({row["scenario"] for row in rows} | set(statuses))
    for scenario in scenarios:
        seeds = {row["seed"] for row in rows if row["scenario"] == scenario}
        if scenario == "baseline":
            lines.append(
                f"| baseline | {len(seeds)} | {statuses.get(scenario, 'unknown')} | "
                f"{fmt(baseline_p95)} | 1.00x | {fmt(baseline_ops)} | 0.00% | n/a |"
            )
            continue

        start, end = FAULT_WINDOWS.get(scenario, (0.0, 60.0))
        ops_by_time = periodic_values(rows, scenario, "Operations/sec")
        p95_by_time = periodic_values(rows, scenario, "95% Latency (ms, averaged)")
        fault_ops = [
            value
            for second, values in ops_by_time.items()
            if start <= second <= end
            for value in values
        ]
        fault_p95 = [
            value
            for second, values in p95_by_time.items()
            if start <= second <= end
            for value in values
        ]
        min_ops = min(fault_ops) if fault_ops else None
        max_p95 = max(fault_p95) if fault_p95 else None
        p95_factor = max_p95 / baseline_p95 if max_p95 is not None and baseline_p95 else None
        throughput_drop = (
            100.0 * max(0.0, 1.0 - min_ops / baseline_ops)
            if min_ops is not None and baseline_ops
            else None
        )
        recovery = (
            recovery_seconds(rows, scenario, end, baseline_ops, baseline_p95)
            if baseline_ops and baseline_p95
            else None
        )
        recovery_text = "n/a" if recovery is None else f"{recovery:.2f}s"
        lines.append(
            f"| {scenario} | {len(seeds)} | {statuses.get(scenario, 'unknown')} | "
            f"{fmt(max_p95)} | {fmt(p95_factor)}x | {fmt(min_ops)} | "
            f"{fmt(throughput_drop)}% | {recovery_text} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Evidence for the proposed problem is present when degraded scenarios repeatedly show materially higher tail latency, lower achieved throughput, growing Ratekeeper queues/lag, or slow recovery relative to the same-seed baseline.",
            "",
            "Use `metrics.csv` for plots and statistical analysis. `ratekeeper.csv` contains TPS limits, released TPS, queue depth, version lag, and durability lag extracted from trace events.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    statuses: dict[str, str] = {}
    for scenario_dir in sorted(path for path in args.results.iterdir() if path.is_dir()):
        if scenario_dir == args.output:
            continue
        scenario_statuses: list[str] = []
        for seed_dir in sorted(scenario_dir.glob("seed-*")):
            try:
                seed = int(seed_dir.name.removeprefix("seed-"))
            except ValueError:
                continue
            rows.extend(parse_log(seed_dir / "simulation.log", scenario_dir.name, seed))
            for trace in seed_dir.glob("trace.*.xml"):
                rows.extend(parse_trace(trace, scenario_dir.name, seed))
            status_path = seed_dir / "status.txt"
            scenario_statuses.append(status_path.read_text().strip() if status_path.exists() else "MISSING")
        if scenario_statuses:
            statuses[scenario_dir.name] = (
                "pass" if all(status == "PASS" for status in scenario_statuses) else "failure"
            )

    fields = ["scenario", "seed", "time_seconds", "source", "metric", "value"]
    with (args.output / "metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    ratekeeper_rows = [row for row in rows if row["source"].startswith("RkUpdate")]
    with (args.output / "ratekeeper.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ratekeeper_rows)

    evidence = build_evidence(rows, statuses)
    (args.output / "evidence.md").write_text(evidence)
    (args.output / "summary.json").write_text(
        json.dumps({"statuses": statuses, "metric_rows": len(rows)}, indent=2) + "\n"
    )
    print(evidence)


if __name__ == "__main__":
    main()

