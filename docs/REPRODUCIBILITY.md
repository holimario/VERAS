# Reproducibility Notes

## Harness verification

From the repository root, run `./scripts/verify_artifact.sh`. With a
compatible ORAgentBench checkout, this runs 32 deterministic unit tests;
without it, the 14 dependency-free semantic-observer tests run. In both
cases, it also checks prompt hashes/controller forwarding and release content.
The endpoint and credential must be user-supplied; no private gateway setup
is distributed.

## Controller-profile differential check

The case profiles are in `veras/src/veras_harness/profiles.py`. With access to
the pinned frozen v0.30 runner and the two original case fork source files,
run the stronger offline check below (no model request or operational data
required):

```bash
python scripts/verify_controller_profiles.py \
  --frozen /path/to/frozen/run_benchmark.py \
  --datacenter /path/to/datacenter-fork/run_benchmark.py \
  --power-grid /path/to/power-grid-fork/run_benchmark.py
```

Without those original sources, `./scripts/verify_artifact.sh` still checks
public prompt outputs against their pinned golden hashes and verifies the
controller/CLI forwarding points. With the three source paths, the checker
also checks source hashes and reconstructs each entire fork from the
frozen source using only its listed prompt addition (and, for grid, a
`180 -> 480`-second cap change), compares modular prompt outputs on
representative inputs, and checks that
the public controller forwards both values to the original decision points.
The original runner source is not redistributed. In an authorized checkout,
`./veras/bin/run_case.sh datacenter --dry-run` (or `power-grid`) additionally
checks task and runner hashes and CLI task/timeout forwarding without running
the agent. It still requires the configured endpoint/key for the runner's
normal dry-run setup. Neither check asserts identical private API transport
or stochastic inference. The profile run uses the original task's own time
limit multiplied by the pinned timeout scale; it does not change the task.

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
