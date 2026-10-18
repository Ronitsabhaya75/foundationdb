# Ratekeeper benchmark CI bundle

Copy the contents of this directory into the root of the FoundationDB fork.

The scenario TOMLs intentionally live under `contrib/ratekeeper_benchmarks/scenarios/`. FoundationDB's CMake configuration verifies that every TOML below `tests/` is registered in `tests/CMakeLists.txt`; these research-only scenarios are invoked directly by the workflow instead.

The workflow builds `fdbserver` from the checked-out source and runs four paired deterministic simulation scenarios. Pull requests use the 3-seed `smoke` profile; a manual `paper` profile uses 10 seeds:

1. Healthy baseline.
2. Mild random network clogging.
3. Severe/swizzled network clogging.
4. One deliberately slowed storage worker.

It uploads raw simulation logs and XML traces, plus:

- `summary/metrics.csv`: workload and trace metrics.
- `summary/ratekeeper.csv`: Ratekeeper TPS, queue, version-lag, and durability-lag samples.
- `summary/paper_results.csv`: one analysis-ready row per scenario and seed with the primary and secondary outcomes.
- `summary/evidence.md`: primary and secondary result tables shown in the Actions job summary.
- `summary/validity.md`: completeness, fault-isolation, and measurement checks.
- `summary/summary.json`: machine-readable run status.
- `manifest.json`: commit, binary hash, build-image ID, seed list, and scenario hashes.

The workflow fails on invalid experiments, missing metrics, or failed simulations. It intentionally does not fail on a performance threshold yet. Establish paired baseline distributions first, then select statistically defensible regression thresholds.

See `contrib/ratekeeper_benchmarks/README.md` for metric definitions, limitations, and the publication boundary.
