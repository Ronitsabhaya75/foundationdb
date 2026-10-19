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
    "network_clog_mild": (15.0, 40.0),
    "network_clog_severe": (15.0, 45.0),
    "slow_storage": (15.0, 40.0),
}

WORKLOAD_DURATION_SECONDS = 75.0
OPERATIONS_PER_TRANSACTION = 10.0
REQUIRED_SCENARIOS = {
    "baseline",
    "network_clog_mild",
    "network_clog_severe",
    "slow_storage",
}


def fault_window(scenario: str) -> tuple[float, float]:
    if scenario.startswith("network_clog_mild"):
        return (15.0, 40.0)
    if scenario.startswith("network_clog_severe"):
        return (15.0, 45.0)
    if scenario.startswith("slow_storage"):
        return (15.0, 40.0)
    return (0.0, WORKLOAD_DURATION_SECONDS)


def baseline_for_scenario(scenario: str) -> str:
    if scenario.endswith("_low"):
        return "baseline_low"
    if scenario.endswith("_high"):
        return "baseline_high"
    return "baseline"


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

        if event_type in {
            "CloggingPair",
            "SetDiskFailure",
            "AddFailureInjectionWorkload",
        }:
            rows.append(
                {
                    "scenario": scenario,
                    "seed": seed,
                    "client_id": None,
                    "time_seconds": relative_time,
                    "source": "fault",
                    "metric": event_type,
                    "value": 1.0,
                }
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

        if event_type == "ReadWrite_CommitLatency":
            for field, label in (
                ("Median", "Commit p50 latency (ms)"),
                ("Percentile95", "Commit p95 latency (ms)"),
                ("Percentile99", "Commit p99 latency (ms)"),
            ):
                value = finite_float(attrs.get(field, ""))
                # An empty interval is emitted as zero; it has no latency percentile.
                if value is not None and value > 0:
                    rows.append(
                        {
                            "scenario": scenario,
                            "seed": seed,
                            "client_id": None,
                            "time_seconds": relative_time,
                            "source": event_type,
                            "metric": label,
                            "value": value * 1000.0,
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


def metric_values(
    rows: list[dict], scenario: str, seed: int, metric: str, start: float, end: float
) -> list[float]:
    return [
        row["value"]
        for row in rows
        if row["scenario"] == scenario
        and row["seed"] == seed
        and row["metric"] == metric
        and row["time_seconds"] is not None
        and start <= row["time_seconds"] <= end
    ]


def final_seed_value(rows: list[dict], scenario: str, seed: int, metric: str) -> float | None:
    values = [
        row["value"]
        for row in rows
        if row["scenario"] == scenario
        and row["seed"] == seed
        and row["source"] == "workload"
        and row["time_seconds"] is None
        and row["metric"] == metric
    ]
    return sum(values) if values else None


def make_paper_rows(
    rows: list[dict],
    statuses: dict[str, str],
    run_statuses: dict[tuple[str, int], str] | None = None,
) -> list[dict]:
    """Create one paired, analysis-ready row per scenario and deterministic seed."""
    output: list[dict] = []
    all_scenarios = sorted({row["scenario"] for row in rows} | set(statuses))
    for scenario in all_scenarios:
        seeds = sorted({row["seed"] for row in rows if row["scenario"] == scenario})
        start, end = fault_window(scenario)
        baseline_scenario = baseline_for_scenario(scenario)
        baseline_ops = periodic_seed_values(rows, baseline_scenario, "Operations/sec", "sum")
        baseline_p95 = periodic_seed_values(
            rows, baseline_scenario, "95% Commit Latency (ms, averaged)", "median"
        )
        ops_by_seed = periodic_seed_values(rows, scenario, "Operations/sec", "sum")
        phase_p95 = periodic_seed_values(
            rows, scenario, "95% Commit Latency (ms, averaged)", "median"
        )
        for seed in seeds:
            run_status = (
                run_statuses.get((scenario, seed), "unknown")
                if run_statuses is not None
                else statuses.get(scenario, "unknown")
            )
            phase_ops = [
                value
                for second, value in ops_by_seed.get(seed, {}).items()
                if start <= second <= end
            ]
            latency = {
                percentile: median(metric_values(rows, scenario, seed, metric, start, end))
                for percentile, metric in (
                    ("p50", "Commit p50 latency (ms)"),
                    ("p95", "Commit p95 latency (ms)"),
                    ("p99", "Commit p99 latency (ms)"),
                )
            }
            rk_limits = metric_values(rows, scenario, seed, "TPSLimit", start, end)
            released = metric_values(rows, scenario, seed, "ReleasedTPS", start, end)
            queues = metric_values(
                rows, scenario, seed, "WorstStorageServerQueue", start, end
            ) + metric_values(rows, scenario, seed, "WorstTLogQueue", start, end)
            durability = metric_values(
                rows, scenario, seed, "WorstStorageServerDurabilityLag", start, end
            )
            version_lag = metric_values(
                rows, scenario, seed, "WorstStorageServerVersionLag", start, end
            )

            recovery = None
            if not scenario.startswith("baseline") and seed in baseline_ops and seed in baseline_p95:
                baseline_ops_median = median(list(baseline_ops[seed].values()))
                baseline_p95_median = median(list(baseline_p95[seed].values()))
                if baseline_ops_median is not None and baseline_p95_median is not None:
                    recovery = recovery_seconds(
                        ops_by_seed.get(seed, {}),
                        phase_p95.get(seed, {}),
                        end,
                        baseline_ops_median,
                        baseline_p95_median,
                    )

            output.append(
                {
                    "scenario": scenario,
                    "seed": seed,
                    "status": run_status,
                    "phase_start_seconds": start,
                    "phase_end_seconds": end,
                    "commit_p50_ms": latency["p50"],
                    "commit_p95_ms": latency["p95"],
                    "commit_p99_ms": latency["p99"],
                    "successful_transactions_per_second": (
                        median(phase_ops) / OPERATIONS_PER_TRANSACTION if phase_ops else None
                    ),
                    "whole_run_successful_transactions_per_second": final_seed_value(
                        rows, scenario, seed, "Transactions/sec"
                    ),
                    "retryable_transaction_errors": final_seed_value(
                        rows, scenario, seed, "Retries"
                    ),
                    "terminal_run_failure": int(run_status != "pass"),
                    "recovery_seconds": recovery,
                    "ratekeeper_limit_min_tps": min(rk_limits) if rk_limits else None,
                    "ratekeeper_limit_median_tps": median(rk_limits),
                    "ratekeeper_released_median_tps": median(released),
                    "max_queue_bytes": max(queues) if queues else None,
                    "max_durability_lag_versions": max(durability) if durability else None,
                    "max_version_lag_versions": max(version_lag) if version_lag else None,
                    "server_cpu_percent": None,
                    "server_cpu_status": "not_applicable_in_deterministic_simulation",
                }
            )
    return output


def validate_dataset(
    rows: list[dict],
    statuses: dict[str, str],
    expected_seed_count: int | None,
    required_scenarios: set[str],
) -> list[str]:
    errors: list[str] = []
    found = set(statuses)
    if found != required_scenarios:
        errors.append(
            f"scenario set mismatch: expected {sorted(required_scenarios)}, found {sorted(found)}"
        )
    for scenario in sorted(required_scenarios):
        seeds = {row["seed"] for row in rows if row["scenario"] == scenario}
        if expected_seed_count is not None and len(seeds) != expected_seed_count:
            errors.append(f"{scenario}: expected {expected_seed_count} seeds, found {len(seeds)}")
        if statuses.get(scenario) != "pass":
            errors.append(f"{scenario}: simulation status is {statuses.get(scenario, 'missing')}")
        for metric in (
            "Commit p50 latency (ms)",
            "Commit p95 latency (ms)",
            "Commit p99 latency (ms)",
            "Transactions/sec",
            "Retries",
            "TPSLimit",
            "WorstStorageServerDurabilityLag",
        ):
            if not any(row["scenario"] == scenario and row["metric"] == metric for row in rows):
                errors.append(f"{scenario}: required metric missing: {metric}")

        fault_counts = {
            metric: sum(
                1 for row in rows if row["scenario"] == scenario and row["metric"] == metric
            )
            for metric in ("CloggingPair", "SetDiskFailure", "AddFailureInjectionWorkload")
        }
        if fault_counts["AddFailureInjectionWorkload"]:
            errors.append(f"{scenario}: unexpected automatic failure workload detected")
        if scenario.startswith("baseline") and (
            fault_counts["CloggingPair"] or fault_counts["SetDiskFailure"]
        ):
            errors.append(f"{scenario}: fault events detected")
        if scenario.startswith("network_clog") and not fault_counts["CloggingPair"]:
            errors.append(f"{scenario}: no CloggingPair events detected")
        if scenario.startswith("network_clog") and fault_counts["SetDiskFailure"]:
            errors.append(f"{scenario}: unexpected disk failure events detected")
        if scenario.startswith("slow_storage") and not fault_counts["SetDiskFailure"]:
            errors.append(f"{scenario}: no SetDiskFailure events detected")
        if scenario.startswith("slow_storage") and fault_counts["CloggingPair"]:
            errors.append(f"{scenario}: unexpected network clog events detected")
    return errors


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


def build_paper_evidence(
    rows: list[dict],
    statuses: dict[str, str],
    run_statuses: dict[tuple[str, int], str] | None = None,
) -> str:
    paper_rows = make_paper_rows(rows, statuses, run_statuses)
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for row in paper_rows:
        by_scenario[row["scenario"]].append(row)

    def med(scenario_rows: list[dict], field: str) -> float | None:
        values = [row[field] for row in scenario_rows if row[field] is not None]
        return median(values)

    lines = [
        "# Ratekeeper degradation benchmark",
        "",
        "Results are paired by deterministic seed. Latencies are client-side commit latencies; throughput is successful transactions per second during the measured phase. A simulation PASS establishes correctness, not acceptable performance.",
        "",
        "## Primary outcomes",
        "",
        "| Scenario | Seeds | Status | Commit p50 (ms) | Commit p95 (ms) | Commit p99 (ms) | Successful tx/s | Retryable errors | Recovery (runs) |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in sorted(by_scenario):
        scenario_rows = by_scenario[scenario]
        recoveries = [row["recovery_seconds"] for row in scenario_rows if row["recovery_seconds"] is not None]
        recovery = (
            "n/a"
            if scenario.startswith("baseline")
            else (
                f"{fmt(median(recoveries))}s ({len(recoveries)}/{len(scenario_rows)})"
                if recoveries
                else f"not recovered (0/{len(scenario_rows)})"
            )
        )
        lines.append(
            f"| {scenario} | {len(scenario_rows)} | {statuses.get(scenario, 'unknown')} | "
            f"{fmt(med(scenario_rows, 'commit_p50_ms'))} | "
            f"{fmt(med(scenario_rows, 'commit_p95_ms'))} | "
            f"{fmt(med(scenario_rows, 'commit_p99_ms'))} | "
            f"{fmt(med(scenario_rows, 'successful_transactions_per_second'))} | "
            f"{fmt(med(scenario_rows, 'retryable_transaction_errors'), 0)} | {recovery} |"
        )

    lines.extend(
        [
            "",
            "## Secondary signals",
            "",
            "| Scenario | Min Ratekeeper limit (tx/s) | Median released (tx/s) | Max queue (bytes) | Max durability lag (versions) | Max version lag (versions) | Server CPU |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for scenario in sorted(by_scenario):
        scenario_rows = by_scenario[scenario]
        lines.append(
            f"| {scenario} | {fmt(med(scenario_rows, 'ratekeeper_limit_min_tps'))} | "
            f"{fmt(med(scenario_rows, 'ratekeeper_released_median_tps'))} | "
            f"{fmt(med(scenario_rows, 'max_queue_bytes'), 0)} | "
            f"{fmt(med(scenario_rows, 'max_durability_lag_versions'), 0)} | "
            f"{fmt(med(scenario_rows, 'max_version_lag_versions'), 0)} | n/a (simulation) |"
        )

    lines.extend(
        [
            "",
            "## Interpretation guardrails",
            "",
            "- `retryable_transaction_errors` means ReadWrite workload retries. Terminal simulation failures are tracked separately in `paper_results.csv`.",
            "- CPU is not reported from deterministic simulation because it is not representative of physical server utilization. Add a controlled real-cluster experiment for that metric.",
            "- CI checks data completeness and fault isolation. It does not declare the research hypothesis proven.",
            "- Use `paper_results.csv` for paired statistics and plots, `metrics.csv` for time series, and `ratekeeper.csv` for controller analysis.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-seeds", type=int)
    parser.add_argument(
        "--expected-scenarios",
        help="Comma-separated scenario names; defaults to the four smoke scenarios",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    statuses: dict[str, str] = {}
    run_statuses: dict[tuple[str, int], str] = {}
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
            raw_status = status_path.read_text().strip() if status_path.exists() else "MISSING"
            scenario_statuses.append(raw_status)
            run_statuses[(scenario_dir.name, seed)] = (
                "pass" if raw_status == "PASS" else "failure"
            )
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

    paper_rows = make_paper_rows(rows, statuses, run_statuses)
    paper_fields = list(paper_rows[0]) if paper_rows else []
    with (args.output / "paper_results.csv").open("w", newline="") as handle:
        if paper_fields:
            writer = csv.DictWriter(handle, fieldnames=paper_fields)
            writer.writeheader()
            writer.writerows(paper_rows)

    required_scenarios = (
        {item for item in args.expected_scenarios.split(",") if item}
        if args.expected_scenarios
        else REQUIRED_SCENARIOS
    )
    validation_errors = validate_dataset(
        rows, statuses, args.expected_seeds, required_scenarios
    )
    validation_lines = ["# Benchmark validity", ""]
    if validation_errors:
        validation_lines.extend(["**FAIL**", ""] + [f"- {error}" for error in validation_errors])
    else:
        validation_lines.extend(
            [
                "**PASS**",
                "",
                "All scenarios, seeds, primary measurements, Ratekeeper signals, and expected fault-isolation checks are present.",
            ]
        )
    validation_lines.extend(
        [
            "",
            "`retryable_transaction_errors` is the workload retry count. Terminal simulation failure is reported separately. Server CPU is intentionally marked unavailable because deterministic simulation CPU is not representative of a physical FDB server.",
        ]
    )
    (args.output / "validity.md").write_text("\n".join(validation_lines) + "\n")

    evidence = build_paper_evidence(rows, statuses, run_statuses)
    (args.output / "evidence.md").write_text(evidence)
    (args.output / "summary.json").write_text(
        json.dumps(
            {
                "statuses": statuses,
                "metric_rows": len(rows),
                "paper_rows": len(paper_rows),
                "validation_errors": validation_errors,
            },
            indent=2,
        )
        + "\n"
    )
    print(evidence)
    print((args.output / "validity.md").read_text())
    if args.strict and validation_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

