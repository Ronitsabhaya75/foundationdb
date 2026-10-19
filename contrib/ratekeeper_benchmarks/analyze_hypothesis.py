#!/usr/bin/env python3
"""Paired statistical analysis for the Ratekeeper network-degradation hypothesis."""

from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path


COMPARISONS = {
    "network_clog_mild_low": "baseline_low",
    "network_clog_mild": "baseline",
    "network_clog_mild_high": "baseline_high",
    "network_clog_severe_low": "baseline_low",
    "network_clog_severe": "baseline",
    "network_clog_severe_high": "baseline_high",
}
WINDOWS = {
    "network_clog_mild_low": (15.0, 40.0),
    "network_clog_mild": (15.0, 40.0),
    "network_clog_mild_high": (15.0, 40.0),
    "network_clog_severe_low": (15.0, 45.0),
    "network_clog_severe": (15.0, 45.0),
    "network_clog_severe_high": (15.0, 45.0),
}
OFFERED_TPS = {"low": 2500.0, "default": 5000.0, "high": 7500.0}
OPS_PER_TRANSACTION = 10.0


def load_name(scenario: str) -> str:
    if scenario.endswith("_low"):
        return "low"
    if scenario.endswith("_high"):
        return "high"
    return "default"


def read_metrics(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            row["seed"] = int(row["seed"])
            row["value"] = float(row["value"])
            row["time_seconds"] = (
                float(row["time_seconds"]) if row["time_seconds"] else None
            )
            rows.append(row)
    return rows


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def latency_by_seed(
    rows: list[dict], scenario: str, metric: str, start: float, end: float
) -> dict[int, float]:
    values: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if (
            row["scenario"] == scenario
            and row["metric"] == metric
            and row["time_seconds"] is not None
            and start <= row["time_seconds"] <= end
        ):
            values[row["seed"]].append(row["value"])
    return {seed: statistics.median(samples) for seed, samples in values.items()}


def throughput_by_seed(
    rows: list[dict], scenario: str, start: float, end: float
) -> dict[int, float]:
    per_interval: dict[tuple[int, float], float] = defaultdict(float)
    for row in rows:
        if (
            row["scenario"] == scenario
            and row["source"] == "workload"
            and row["metric"] == "Operations/sec"
            and row["time_seconds"] is not None
            and start <= row["time_seconds"] <= end
        ):
            per_interval[(row["seed"], row["time_seconds"])] += row["value"]
    values: dict[int, list[float]] = defaultdict(list)
    for (seed, _), operations in per_interval.items():
        values[seed].append(operations / OPS_PER_TRANSACTION)
    return {seed: statistics.median(samples) for seed, samples in values.items()}


def retry_by_seed(rows: list[dict], scenario: str) -> dict[int, float]:
    values: dict[int, float] = defaultdict(float)
    for row in rows:
        if (
            row["scenario"] == scenario
            and row["source"] == "workload"
            and row["metric"] == "Retries"
            and row["time_seconds"] is None
        ):
            values[row["seed"]] += row["value"]
    return dict(values)


def exact_sign_test(direction_values: list[float]) -> tuple[int, int, float]:
    nonzero = [value for value in direction_values if value != 0]
    n = len(nonzero)
    wins = sum(value > 0 for value in nonzero)
    if not n:
        return 0, 0, 1.0
    probability = sum(math.comb(n, count) for count in range(wins, n + 1)) / (2**n)
    return wins, n, probability


def bootstrap_median_ci(values: list[float], seed: int, samples: int = 10000) -> tuple[float, float]:
    generator = random.Random(seed)
    bootstrapped = []
    for _ in range(samples):
        sample = [generator.choice(values) for _ in values]
        bootstrapped.append(statistics.median(sample))
    bootstrapped.sort()
    return (
        bootstrapped[int(0.025 * samples)],
        bootstrapped[int(0.975 * samples) - 1],
    )


def holm_adjust(rows: list[dict]) -> None:
    ordered = sorted(enumerate(rows), key=lambda item: item[1]["p_value_one_sided"])
    running = 0.0
    total = len(rows)
    for rank, (index, row) in enumerate(ordered):
        adjusted = min(1.0, (total - rank) * row["p_value_one_sided"])
        running = max(running, adjusted)
        rows[index]["p_value_holm"] = running


def comparison_rows(metrics: list[dict]) -> list[dict]:
    results: list[dict] = []
    metric_specs = (
        ("commit_p99_ms", "Commit p99 latency (ms)", "increase"),
        ("successful_tps", "Successful transactions/s", "decrease"),
    )
    for degraded, baseline in COMPARISONS.items():
        start, end = WINDOWS[degraded]
        for metric_key, label, expected in metric_specs:
            if metric_key == "successful_tps":
                degraded_values = throughput_by_seed(metrics, degraded, start, end)
                baseline_values = throughput_by_seed(metrics, baseline, start, end)
            else:
                degraded_values = latency_by_seed(
                    metrics, degraded, "Commit p99 latency (ms)", start, end
                )
                baseline_values = latency_by_seed(
                    metrics, baseline, "Commit p99 latency (ms)", start, end
                )
            seeds = sorted(set(degraded_values) & set(baseline_values))
            if not seeds:
                raise ValueError(f"No paired samples for {degraded} / {metric_key}")
            raw_differences = [degraded_values[s] - baseline_values[s] for s in seeds]
            direction_values = (
                raw_differences
                if expected == "increase"
                else [-difference for difference in raw_differences]
            )
            percent_changes = [
                100.0 * (degraded_values[s] - baseline_values[s]) / baseline_values[s]
                for s in seeds
                if baseline_values[s] != 0
            ]
            wins, nonzero, p_value = exact_sign_test(direction_values)
            ci_low, ci_high = bootstrap_median_ci(
                percent_changes,
                seed=sum(ord(char) for char in f"{degraded}:{metric_key}"),
            )
            results.append(
                {
                    "comparison": f"{degraded}_vs_{baseline}",
                    "degraded_scenario": degraded,
                    "baseline_scenario": baseline,
                    "load": load_name(degraded),
                    "offered_tps": OFFERED_TPS[load_name(degraded)],
                    "metric": metric_key,
                    "metric_label": label,
                    "paired_seeds": len(seeds),
                    "baseline_median": statistics.median([baseline_values[s] for s in seeds]),
                    "degraded_median": statistics.median([degraded_values[s] for s in seeds]),
                    "median_percent_change": statistics.median(percent_changes),
                    "percent_change_ci95_low": ci_low,
                    "percent_change_ci95_high": ci_high,
                    "expected_direction": expected,
                    "seeds_in_expected_direction": wins,
                    "nonzero_pairs": nonzero,
                    "p_value_one_sided": p_value,
                    "p_value_holm": None,
                }
            )
    holm_adjust(results)
    for row in results:
        row["supports_hypothesis_at_0_05"] = row["p_value_holm"] < 0.05
    return results


def controller_rows(metrics: list[dict]) -> list[dict]:
    output = []
    for scenario in COMPARISONS:
        start, end = WINDOWS[scenario]
        offered = OFFERED_TPS[load_name(scenario)]
        samples: dict[int, list[float]] = defaultdict(list)
        for row in metrics:
            if (
                row["scenario"] == scenario
                and row["metric"] == "TPSLimit"
                and row["time_seconds"] is not None
                and start <= row["time_seconds"] <= end
            ):
                samples[row["seed"]].append(row["value"])
        for seed, values in sorted(samples.items()):
            output.append(
                {
                    "scenario": scenario,
                    "seed": seed,
                    "offered_tps": offered,
                    "median_ratekeeper_limit_tps": statistics.median(values),
                    "minimum_ratekeeper_limit_tps": min(values),
                    "fraction_samples_limit_below_offered": sum(value < offered for value in values)
                    / len(values),
                }
            )
    return output


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"No rows for {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, stats: list[dict], controller: list[dict]) -> None:
    lines = [
        "# Paired hypothesis analysis",
        "",
        "**Pre-specified hypothesis:** partial network degradation increases client commit p99 latency and decreases successful transaction throughput relative to the same-load, same-seed healthy baseline.",
        "",
        "Inference uses a one-sided exact paired sign test. P-values are Holm-corrected across all primary comparisons. Effect intervals are deterministic 10,000-sample paired bootstrap 95% confidence intervals for the median percentage change.",
        "",
        "| Degradation | Load | Metric | Pairs | Baseline | Degraded | Median change | 95% CI | Holm p | Supports H1 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in stats:
        lines.append(
            f"| {row['degraded_scenario']} | {row['load']} | {row['metric_label']} | "
            f"{row['paired_seeds']} | {row['baseline_median']:.2f} | "
            f"{row['degraded_median']:.2f} | {row['median_percent_change']:+.2f}% | "
            f"[{row['percent_change_ci95_low']:+.2f}%, {row['percent_change_ci95_high']:+.2f}%] | "
            f"{row['p_value_holm']:.4g} | {'yes' if row['supports_hypothesis_at_0_05'] else 'no'} |"
        )

    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for row in controller:
        by_scenario[row["scenario"]].append(row)
    lines.extend(
        [
            "",
            "## Ratekeeper response diagnostic",
            "",
            "| Scenario | Offered tx/s | Median RK limit | Median fraction of samples limited below offered load |",
            "|---|---:|---:|---:|",
        ]
    )
    for scenario, rows in sorted(by_scenario.items()):
        lines.append(
            f"| {scenario} | {rows[0]['offered_tps']:.0f} | "
            f"{statistics.median(row['median_ratekeeper_limit_tps'] for row in rows):.2f} | "
            f"{statistics.median(row['fraction_samples_limit_below_offered'] for row in rows):.2%} |"
        )
    lines.extend(
        [
            "",
            "The statistical decision concerns the stated characterization hypothesis only. It does not show that a proposed controller is better; that requires running the same paired design for OSS Ratekeeper, PID, and the new controller.",
        ]
    )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    metrics = read_metrics(args.metrics)
    stats = comparison_rows(metrics)
    controller = controller_rows(metrics)
    write_csv(args.output / "hypothesis_tests.csv", stats)
    write_csv(args.output / "ratekeeper_response.csv", controller)
    write_report(args.output / "hypothesis.md", stats, controller)
    print((args.output / "hypothesis.md").read_text())


if __name__ == "__main__":
    main()
