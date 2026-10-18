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
    r"^Metric \((?P<client_id>\d+),[^)]*\):\s*(?P<name>.*?),\s*"
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

WORKLOAD_DURATION_SECONDS = 60.0


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
                "client_id": int(match.group("client_id")),
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
    events: list[dict[str, str]] = []
    workload_start: float | None = None
    for line in path.read_text(errors="replace").splitlines():
        event_match = EVENT_RE.search(line)
        if not event_match:
            continue
        attrs = {
            key: html.unescape(value)
            for key, value in ATTRIBUTE_RE.findall(event_match.group("attrs"))
        }
        events.append(attrs)
        if (
            attrs.get("Type") == "TestStarting"
            and attrs.get("Machine") == "0.0.0.0:0"
            and attrs.get("WorkloadTitle", "").startswith("Ratekeeper")
        ):
            workload_start = finite_float(attrs.get("Time", ""))

    for attrs in events:
        event_type = attrs.get("Type", "")
        event_time = finite_float(attrs.get("Time", ""))
        relative_time = (
            event_time - workload_start
            if event_time is not None and workload_start is not None
            else event_time
        )

        # Only export controller and latency events from the measured workload.
        if (
            workload_start is not None
            and relative_time is not None
            and not 0.0 <= relative_time <= WORKLOAD_DURATION_SECONDS
        ):
            continue

        if event_type.startswith("RkUpdate"):
            for metric in wanted:
                value = finite_float(attrs.get(metric, ""))
                if value is not None:
                    rows.append(
                        {
                            "scenario": scenario,
                            "seed": seed,
                            "client_id": None,
                            "time_seconds": relative_time,
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
                            "client_id": None,
                            "time_seconds": relative_time,
                            "source": event_type,
                            "metric": label,
                            "value": value,
                        }
                    )
    return rows


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def periodic_seed_values(
    rows: list[dict], scenario: str, metric: str, aggregation: str
) -> dict[int, dict[float, float]]:
    """Aggregate tester-client metrics into one value per seed and interval."""
    client_values: dict[tuple[int, float], list[float]] = defaultdict(list)
    for row in rows:
        if (
            row["scenario"] == scenario
            and row["source"] == "workload"
            and row["metric"] == metric
            and row["time_seconds"] is not None
        ):
            client_values[(row["seed"], row["time_seconds"])].append(row["value"])

    values: dict[int, dict[float, float]] = defaultdict(dict)
    for (seed, second), samples in client_values.items():
        if aggregation == "sum":
            value = sum(samples)
        elif aggregation == "median":
            value = statistics.median(samples)
        else:
            raise ValueError(f"Unknown aggregation: {aggregation}")
        values[seed][second] = value
    return values


def recovery_seconds(
    ops: dict[float, float],
    p95: dict[float, float],
    fault_end: float,
    baseline_ops: float,
    baseline_p95: float,
) -> float | None:
    times = sorted(set(ops) & set(p95))
    good_start: float | None = None
    for second in times:
        if second <= fault_end:
            continue
        recovered = (
            ops[second] >= baseline_ops * 0.90
            and p95[second] <= baseline_p95 * 1.25
        )
        if not recovered:
            good_start = None
        elif good_start is None:
            good_start = second
        else:
            return max(0.0, good_start - fault_end)
    return None


def fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def build_evidence(rows: list[dict], statuses: dict[str, str]) -> str:
    baseline_ops = periodic_seed_values(rows, "baseline", "Operations/sec", "sum")
    baseline_p95 = periodic_seed_values(
        rows, "baseline", "95% Latency (ms, averaged)", "median"
    )
    baseline_seeds = sorted(set(baseline_ops) & set(baseline_p95))
    baseline_ops_per_seed = {
        seed: statistics.median(baseline_ops[seed].values()) for seed in baseline_seeds
    }
    baseline_p95_per_seed = {
        seed: statistics.median(baseline_p95[seed].values()) for seed in baseline_seeds
    }
    baseline_ops_median = median(list(baseline_ops_per_seed.values()))
    baseline_p95_median = median(list(baseline_p95_per_seed.values()))

    lines = [
        "# Ratekeeper degradation benchmark",
        "",
        "This report is observational: performance differences are recorded but are not yet CI failure thresholds. Throughput is summed across tester clients; latency is the median client p95 for each seed and interval. Scenario comparisons are paired by deterministic seed.",
        "",
        "| Scenario | Runs | Status | Phase median client p95 (ms) | p95 / paired baseline | Phase median ops/s | Throughput drop | Median recovery (runs) |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]

    scenarios = sorted({row["scenario"] for row in rows} | set(statuses))
    for scenario in scenarios:
        seeds = {row["seed"] for row in rows if row["scenario"] == scenario}
        if scenario == "baseline":
            lines.append(
                f"| baseline | {len(seeds)} | {statuses.get(scenario, 'unknown')} | "
                f"{fmt(baseline_p95_median)} | 1.00x | {fmt(baseline_ops_median)} | 0.00% | n/a |"
            )
            continue

        start, end = FAULT_WINDOWS.get(scenario, (0.0, 60.0))
        ops_by_seed = periodic_seed_values(rows, scenario, "Operations/sec", "sum")
        p95_by_seed = periodic_seed_values(
            rows, scenario, "95% Latency (ms, averaged)", "median"
        )
        paired_seeds = sorted(
            set(ops_by_seed)
            & set(p95_by_seed)
            & set(baseline_ops_per_seed)
            & set(baseline_p95_per_seed)
        )
        phase_ops: list[float] = []
        phase_p95: list[float] = []
        p95_factors: list[float] = []
        throughput_drops: list[float] = []
        recoveries: list[float] = []
        for seed in paired_seeds:
            scenario_ops = [
                value for second, value in ops_by_seed[seed].items() if start <= second <= end
            ]
            scenario_p95 = [
                value for second, value in p95_by_seed[seed].items() if start <= second <= end
            ]
            baseline_window_ops = [
                value for second, value in baseline_ops[seed].items() if start <= second <= end
            ]
            baseline_window_p95 = [
                value for second, value in baseline_p95[seed].items() if start <= second <= end
            ]
            if not scenario_ops or not scenario_p95 or not baseline_window_ops or not baseline_window_p95:
                continue
            seed_ops = statistics.median(scenario_ops)
            seed_p95 = statistics.median(scenario_p95)
            seed_baseline_ops = statistics.median(baseline_window_ops)
            seed_baseline_p95 = statistics.median(baseline_window_p95)
            phase_ops.append(seed_ops)
            phase_p95.append(seed_p95)
            if seed_baseline_p95:
                p95_factors.append(seed_p95 / seed_baseline_p95)
            if seed_baseline_ops:
                throughput_drops.append(100.0 * (1.0 - seed_ops / seed_baseline_ops))
            recovery = recovery_seconds(
                ops_by_seed[seed],
                p95_by_seed[seed],
                end,
                baseline_ops_per_seed[seed],
                baseline_p95_per_seed[seed],
            )
            if recovery is not None:
                recoveries.append(recovery)

        recovery_text = (
            "n/a"
            if not recoveries
            else f"{statistics.median(recoveries):.2f}s ({len(recoveries)}/{len(paired_seeds)})"
        )
        lines.append(
            f"| {scenario} | {len(seeds)} | {statuses.get(scenario, 'unknown')} | "
            f"{fmt(median(phase_p95))} | {fmt(median(p95_factors))}x | "
            f"{fmt(median(phase_ops))} | {fmt(median(throughput_drops))}% | {recovery_text} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Evidence for the proposed problem is present when degraded scenarios repeatedly show materially higher tail latency, lower achieved throughput, growing Ratekeeper queues/lag, or slow recovery relative to the same-seed baseline. A simulation PASS establishes correctness only, not acceptable performance.",
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

    fields = ["scenario", "seed", "client_id", "time_seconds", "source", "metric", "value"]
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

