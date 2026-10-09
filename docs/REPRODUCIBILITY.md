# Reproducibility Notes

## Harness verification

From the repository root, run `./scripts/verify_artifact.sh`. With a
compatible ORAgentBench checkout, this runs 32 deterministic tests; without
it, the 14 dependency-free semantic-observer tests and anonymity scan run.
The endpoint and credential must be user-supplied; no private gateway setup
is distributed.

## Real-world algorithms

The **revised** datacenter and grid source files, run scope, exact source and
artifact hashes, and minimal independent audit summaries live in
`real_world/{data_center,power_grid}/veras_v030_transfer/`. Authorized users
should restore the original task packet under `/app` (the datacenter generator
expects `/app/data`; the grid generator expects `/app/data` and its source
under `/app/submissions`) and use the original task evaluator. NumPy, Polars,
PySCIPOpt/SCIP (datacenter), and MOSEK with a valid license (grid) are needed.
The private operational data, generated full artifacts, and solver license
are deliberately excluded.

R3's public-input-only source replay produced the same Parquet under the
120-second contract; its reported score is for **visible August 8--9 only**.
Grid R1's source-only replay with a stricter 750-second solver limit produced
the exact agent NPZ in under 900 seconds end-to-end on four threads. This
qualification invocation is not the original agent's 900-second setting.
Neither study provides a new matched native-versus-VERAS causal comparison:
the preserved native values are historical. Historical case algorithms and
evidence remain under `real_world/*/veras/` and `native_codex/`.
