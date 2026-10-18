# Ratekeeper partial-failure benchmark

This suite is the reproducible baseline for research on latency-aware overload control in FoundationDB. It builds `fdbserver` from the checked-out commit and runs the same ReadWrite workload and deterministic seeds under four conditions:

- `baseline`: no injected failure
- `network_clog_mild`: mild network clogging for approximately 25 seconds
- `network_clog_severe`: severe network clogging for approximately 30 seconds
- `slow_storage`: one throttled storage worker from approximately 10 to 35 seconds

Automatic simulation failure workloads are disabled so each run contains only the named treatment.

## CI profiles

- `smoke`: 3 paired seeds. Runs on relevant pull requests and checks that the experiment is valid and parseable.
- `paper`: 10 paired seeds. Run manually with **Actions → Ratekeeper Degradation Benchmarks → Run workflow**. The seed input can override this list.

The paper profile is a collection run, not a claim of statistical sufficiency. Increase the seed set after inspecting variance and selecting a power/effect-size target.

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

## Publication boundary

This simulation suite can establish deterministic behavior and controller response. A research paper should additionally include repeated real-cluster experiments for CPU and external validity, plus the same paired scenarios for OSS Ratekeeper, the PID implementation, and the proposed controller.
