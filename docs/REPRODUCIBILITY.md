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

Each case under `real_world/` contains `native_codex/` and `veras/`, with one
generated `algorithm/solve.py` and one `evidence/result.json` per method.
The case's `comparison.json` records the evaluation configuration and metrics.

Run the generated programs with their task inputs under `/app/data` and the
original task evaluator. The data-center generators produce their placement
Parquet files within the 120-second execution contract. Recorded source
replay times are 86.851 seconds for Native Codex and 45.842 seconds for VERAS.
The grid programs reproduce their NPZ solutions with a 750-second replay
solver limit and four threads, taking 823.924 and 829.552 seconds end-to-end
for Native Codex and VERAS respectively.

The data-center sources use NumPy, pandas, PyArrow, and PySCIPOpt/SCIP.
The grid sources use NumPy, openpyxl, and MOSEK with a valid license.

`scripts/verify_case_results.py` checks directory structure, source hashes,
result records, and agreement with the reported tables.
