# VERAS Research Artifact

This anonymous artifact contains the VERAS harness, complete ORAgentBench
evaluation evidence, retained sanitized task trajectories, and generated
algorithms from two real-world optimization case studies.

## Quick start

### 1. Prepare the workspace

Place this repository next to a compatible ORAgentBench checkout:

```text
workspace/
|- ORAgentBench/
`- veras-artifact/
```

The benchmark checkout must provide `local_runner/harness_benchmark.py` and
`difficulty.json`. Install the benchmark runtime, then provide an
OpenAI-compatible model endpoint and credential through your own environment:

```bash
export ORAGENTBENCH_ROOT=/path/to/ORAgentBench
export VERAS_MODEL=your-model-id
export VERAS_BASE_URL=https://your-api-endpoint.example/v1
export VERAS_API_KEY=your-api-key
```

Use `VERAS_API_KEY_ENV` if the credential is stored under a different variable
name. Endpoints and credential-source metadata are not written to result files.

### 2. Verify the artifact

```bash
./scripts/verify_artifact.sh
```

This runs the anonymization scan and available unit tests. The complete
32-test suite runs when ORAgentBench is installed at the sibling path above;
otherwise the 14 dependency-free semantic-observer tests run.

### 3. Run VERAS

```bash
./veras/bin/run_benchmark.sh all
```

The default concurrency is two. Runtime settings can be overridden without
changing the harness:

```bash
CONCURRENCY=2 TIMEOUT_SCALE=1.0 ./veras/bin/run_benchmark.sh all
```

For diagnostic split runs, replace `all` with `easy-medium` or `hard`. These
selectors do not create separate report hierarchies.

Outputs are written below `veras/runs/` unless `OUTPUT` is set explicitly.

### 4. Browse the saved results

```bash
python -m http.server 8000
```

Then open `http://localhost:8000/veras/reports/benchmark-results/`. The unified
107-task table links retained trajectories directly from the corresponding task
rows. Each trajectory contains model interactions, generated files, verifier
output, scores, and component evidence.

## ORAgentBench results

The official pass rule is `feasibility > 0` and `quality / 2 > 0.4`.

| Split | Passed | Feasible | Runs completed | Mean normalized quality |
| --- | ---: | ---: | ---: | ---: |
| Easy | 18/32 (56.25%) | 27/32 | 32/32 | 0.546670 |
| Medium | 16/41 (39.02%) | 28/41 | 40/41 | 0.341544 |
| Hard | 12/34 (35.29%) | 17/34 | 31/34 | 0.308334 |
| Overall | 46/107 (42.99%) | 72/107 | 103/107 | 0.392337 |

`Runs completed` counts tasks whose runner finished normally and produced all
expected stages. It does not imply feasibility or a passing score.

All retained trajectories are stored under
`veras/reports/benchmark-results/tasks/` as part of the benchmark report. The
evaluated release retained raw event streams for 34 tasks; all 107 task-level
scores remain available in the same report.

## Real-world case studies

The manuscript now reports two **later, independently audited v0.30-harness transfer**
results. The previously published native Codex values remain unchanged, but
come from historical runs and are **not paired controls** for the later VERAS
runs. In particular, the data-center evaluation dates differ, and the grid
prompts and agent compute budgets differ. We therefore do not compute a new
native-relative percentage gain from the rows below.

| Case | Historical native Codex (unchanged) | Revised VERAS | Evaluation boundary |
| --- | ---: | ---: | --- |
| Data-center peak | 1,599.050419 Gbps (August 10) | **1,072.233445 Gbps** | VERAS: visible August 8--9 only; no new August 10 result |
| Power-grid annual objective | 40,726,008.4147 USD/year (raw) | **39,423,636.2501 USD/year** | Fixed instance, different prompt/budget; independent feasibility and clean source replay |

The datacenter R3 result meets a public two-day primary lower bound with 29
Huailai tasks, but other candidates use 21 tasks within the soft near-tie band.
The grid R1 source-only replay under a stricter 750-second solver setting
reproduces the agent NPZ byte-for-byte and finishes end-to-end in 829.552
seconds; it has no external warm start. These are selected candidate results,
not guarantees on a new sealed holdout or proof of global optimality.

New source and sanitized audit summaries are under
`real_world/{data_center,power_grid}/veras_v030_transfer/`. The older
`real_world/{data_center,power_grid}/veras/` directories preserve the
**historical** algorithms and evidence (1,110.640670 Gbps on August 10 and
39,379,929.1225 USD/year). They are not evidence for the revised rows.
The historical native power-grid point was strict-selector ineligible because
its maximum positive residual exceeded that study's admission tolerance;
we retain its original raw objective for provenance, not as an eligible
matched comparison.

## Layout

- `veras/`: packaged harness, tests, score summaries, and unified benchmark report.
- `benchmark/`: benchmark evidence guide and scoring notes.
- `real_world/`: revised v0.30-transfer algorithms and sanitized audits, plus archived historical algorithms and unchanged native records.
- `docs/`: provenance, reproducibility, and anonymization notes.
- `scripts/verify_artifact.sh`: unit-test and anonymity checks.

## Data availability

ORAgentBench task data must be obtained from the benchmark distribution and
placed in the layout described by `veras/README.md`. The two real-world input
datasets are not redistributed because they contain operational data. Their
algorithm source, evaluation metrics, and independent audit records are
included for artifact inspection.
