# Reproducibility Notes

## Harness verification

From the repository root, run `./scripts/verify_artifact.sh`. With a
compatible ORAgentBench checkout, this runs 32 deterministic tests; without
it, the 14 dependency-free semantic-observer tests and anonymity scan run.
The endpoint and credential must be user-supplied; no private gateway setup
is distributed.

## Real-world algorithms

The data-center and power-grid generators, exact source/artifact hashes,
scopes, and sanitized external audits are indexed in `real_world/`. An
authorized user must restore the original task packet under `/app` and use
the original evaluator. The operational data, full outputs, and solver license
are deliberately excluded.

The datacenter archival/native/repeat sources reproduce their final Parquet
artifacts from public inputs within the 120-second generator limit. A separate
visible-date development candidate was scored on August 8--9 only, not
August 10. The prospectively ordered grid native--VERAS pair regenerates
both NPZ artifacts byte-identically under a stricter 750-second solver
setting and four-thread limit. The paired VERAS solver requires both generated
`solve.py` and `audit.py`; a solve-only replay fails at import. The lower-cost
separately audited grid result is likewise reproduced with its own source and
is not substituted into the matched pair. Both grid qualification replays
use a different invocation setting from the original agents' 900-second
solver contract. NumPy, Polars, PySCIPOpt/SCIP (datacenter), and MOSEK with
a valid license (grid) are required.
