# VERAS

This anonymous artifact contains the VERAS harness, complete ORAgentBench
evaluation evidence, retained sanitized task trajectories, and generated
algorithms from two real-world optimization case studies.

## Quick start

### 1. Prepare the workspace

Place this repository next to a compatible ORAgentBench checkout:

```text
workspace/
|- ORAgentBench/
`- VERAS/
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

Compared with the published GPT-5.4 Codex 38/107 pass result, VERAS passes
46/107; Hard contributes 12/34 versus 7/34, while Medium remains 16/41
for both. In saved VERAS trajectories (117 stages), 15 structural restorations
and three witnessed semantic-residual reductions are observed; for the online
electric-bus case, 42 trip-row nullability violations fall to zero. These
events are observational, not a mechanism ablation. No natural checkpoint
rollback occurred.

All retained trajectories are stored under
`veras/reports/benchmark-results/tasks/` as part of the benchmark report. The
evaluated release retained raw event streams for 34 tasks; all 107 task-level
scores remain available in the same report.

## Real-world case studies

The August 10 datacenter rows below use GPT-5.6 Sol/xhigh, the same public
input/evaluator hashes, timeout scale 3, and a 19,800-second configured agent
cap. The archived VERAS R11 and the later native run are a **retrospective
cross-run configuration match**, not a jointly precommitted or equal-inference-
compute pair. August 10 event rows were absent from each agent run, but earlier
research had inspected that date's scores. The later native arm was a single
precommitted attempt.

| 2025-08-10 datacenter arm | Peak bandwidth (Gbps, lower better) | Public-input source replay | Strict resource feasibility |
| --- | ---: | ---: | --- |
| Archived VERAS R11 | **1,110.602830** | 45.842 s | Pass |
| Later native Codex | 1,238.625115 | 86.851 s | Pass |
| Fresh VERAS R11-text replicate | 1,567.302579 | 100.306 s | Pass |

The archived R11 peak is **128.022 Gbps (10.336%) lower** than the later
native arm. The prospectively launched fresh VERAS replicate reverses this
ordering; the archival difference is descriptive, not a stable causal harness
estimate. All three final Parquet artifacts regenerate byte-identically under
the 120-second dispatcher contract. The three unaltered generator sources and
sanitized audit summaries are in `real_world/data_center/{veras_r11_archived,
native_r11_comparator,veras_r11_replicate}/`; shared configuration and
comparison facts are in `real_world/data_center/august10_r11_comparison.json`.
Operational input Parquets and row-level output mappings are not redistributed.

The separate Grid R1 solution has an independently reconstructed annualized
objective of **USD 39,423,636.2501**, 6.279% below the expert hourly model.
All audited hard constraints and integrality checks pass. Its stricter
750-second-solver source replay reproduces the agent NPZ byte-identically in
829.552 seconds end-to-end; it uses no external warm start. The historical
native grid run used different instructions and compute and is not a matched
comparator for R1. Source and audit summary are in
`real_world/power_grid/veras_v030_transfer/`.

The earlier historical datacenter and grid artifacts remain under the
`native_codex/` and `veras/` directories. Datacenter R3 under
`veras_v030_transfer/` was evaluated on visible August 8--9 only; its
1,072.233-Gbps result is **not** an August 10 score. These earlier studies are
retained for provenance rather than substituted into the new comparison.

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
algorithm source, aggregate metrics, and sanitized independent audit records
are included for artifact inspection.
