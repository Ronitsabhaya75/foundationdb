# Ratekeeper partial-failure benchmark

This suite is the reproducible baseline for research on latency-aware overload control in FoundationDB. It builds `fdbserver` from the checked-out commit. The delayed-fault instrumentation is part of `fdbserver/workloads/RandomClogging.cpp`, so network experiments contain healthy, fault, and recovery phases without modifying the checkout during CI.

The smoke workflow runs the same ReadWrite workload and deterministic seeds under four conditions:

- `baseline`: no injected failure
- `network_clog_mild`: mild network clogging from approximately T=15–40 seconds
- `network_clog_severe`: severe network clogging from approximately T=15–45 seconds
- `slow_storage`: one throttled storage worker from approximately T=15–40 seconds

Automatic simulation failure workloads are disabled so each run contains only the named treatment.

## CI profiles

- `smoke`: 3 paired seeds. Runs on relevant pull requests and checks that the experiment is valid and parseable.
- `paper`: 10 paired seeds in the compact workflow. This remains useful for iteration.
- `Ratekeeper Paper Evidence`: a separate manual workflow with 30 paired seeds, three offered loads, mild/severe network degradation, a storage control, parallel scenario jobs, and inferential statistics.

The full paper workflow requires at least 20 seeds and defaults to 30. It runs 300 simulations, so expect materially more GitHub Actions time than the smoke workflow.

## Reported metrics

Primary results in `summary/paper_results.csv`:

- client-side commit p50, p95, and p99 latency
- successful transactions per second
- retryable transaction errors (ReadWrite retries)
- terminal simulation failure
- recovery time after the treatment window

Secondary results:

- Ratekeeper TPS limit and released TPS
- storage-server/TLog queue size
- durability lag and version lag
- server CPU status

Server CPU is deliberately marked `not_applicable_in_deterministic_simulation`. Physical CPU utilization must come from a separate controlled cluster experiment; CI runner CPU is not an FDB server measurement.

## Validity checks

The parser fails the CI job if a scenario or seed is missing, a simulation fails, a required metric is absent, an automatic failure workload appears, or fault events leak into the wrong scenario. Performance values remain observational until a controller comparison and statistical acceptance criteria are defined.

Artifacts also include raw XML traces, simulation logs, controller time series, and `manifest.json` with the commit, binary hash, build image ID, seeds, and scenario hashes.

The full workflow additionally creates:

- `hypothesis_tests.csv`: paired medians, percentage changes, bootstrap confidence intervals, exact sign-test p-values, and Holm-adjusted p-values
- `hypothesis.md`: the paper-oriented primary result table
- `ratekeeper_response.csv`: per-seed Ratekeeper limit response relative to offered load

The pre-specified characterization hypothesis is: partial network degradation increases client commit p99 latency and decreases successful transaction throughput relative to the same-load, same-seed healthy baseline. Statistical significance is evaluated at adjusted p < 0.05, but CI does not fail when the hypothesis is unsupported.

## Publication boundary

This simulation suite can produce paper-grade evidence for the characterization hypothesis. A paper claiming that a new controller improves FoundationDB still needs repeated real-cluster experiments for CPU and external validity, plus the same paired scenarios for OSS Ratekeeper, the PID implementation, and the proposed controller.
