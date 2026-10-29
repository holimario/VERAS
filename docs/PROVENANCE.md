# Artifact Provenance

## VERAS harness

The `veras/` directory contains the evaluated v0.30 controller, benchmark
results, and execution trajectories. The `datacenter` and `power-grid`
profiles provide the case-specific performance instructions and budgets in
`veras/src/veras_harness/profiles.py`.

## Industrial case studies

Each case contains one Native Codex result and one VERAS result:

- `real_world/data_center/`: August 10, 2025 peak bandwidth of
  1,238.625 Gbps for Native Codex and 1,110.603 Gbps for VERAS.
- `real_world/power_grid/`: annualized cost of USD 45.690M for Native
  Codex and USD 39.424M for VERAS.

Each method's generated source is in `algorithm/solve.py`, and its evaluation
record is in `evidence/result.json`. The case's `comparison.json` contains
the two methods' metrics, source hashes, public-input hashes, and evaluation
configuration. `ARTIFACT_MANIFEST.json` indexes these files.

The source SHA-256 values identify the evaluated programs. Evaluation records
include output hashes, replay timing, and constraint-audit results.
