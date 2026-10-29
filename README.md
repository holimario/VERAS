# VERAS

VERAS is a verification-guided adaptive search harness for operations research.
This repository includes the implementation, ORAgentBench results and execution
trajectories, and algorithms for inter-datacenter placement and distribution
network planning.

## Quick start

### 1. Prepare the workspace

Place this repository next to a compatible ORAgentBench checkout:

```text
workspace/
|- ORAgentBench/
`- VERAS/
```

The benchmark checkout must provide `local_runner/harness_benchmark.py` and
`difficulty.json`. Install the benchmark runtime, then configure an
OpenAI-compatible model endpoint and credential:

```bash
export ORAGENTBENCH_ROOT=/path/to/ORAgentBench
export VERAS_MODEL=your-model-id
export VERAS_BASE_URL=https://your-api-endpoint.example/v1
export VERAS_API_KEY=your-api-key
```

Use `VERAS_API_KEY_ENV` if the credential is stored under a different variable
name.

### 2. Verify the artifact

```bash
./scripts/verify_artifact.sh
```

This runs release-content checks, controller-prompt verification, and the
available unit tests.

### 3. Run VERAS

```bash
./veras/bin/run_benchmark.sh all
```

The default concurrency is two. Override runtime settings with:

```bash
CONCURRENCY=2 TIMEOUT_SCALE=1.0 ./veras/bin/run_benchmark.sh all
```

For difficulty-specific runs, replace `all` with `easy-medium` or `hard`.
Outputs are written below `veras/runs/` unless `OUTPUT` is set explicitly.
For case-study configurations, see `veras/README.md` and
`veras/bin/run_case.sh`.

### 4. Browse the results

```bash
python -m http.server 8000
```

Open `http://localhost:8000/veras/reports/benchmark-results/` for the paper's
benchmark and industrial case-study results, task-level scores, and execution
trajectories.

<!-- BEGIN PAPER RESULTS -->
## ORAgentBench results

Results from Table 1 of the paper, covering 107 tasks: 32 Easy, 41 Medium, and 34 Hard.
A task passes when feasibility is positive and normalized quality exceeds `0.4`.

| Harness | Model | Overall feasibility | Overall quality | Easy pass | Medium pass | Hard pass | Overall pass |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Codex | GPT-5.4 | 0.57 | 0.32 | 46.88% | 39.02% | 20.59% | 35.51% |
|  | GPT-5.3-Codex | 0.51 | 0.29 | 31.25% | 43.90% | 20.59% | 32.71% |
|  | GPT-5.4-mini | 0.46 | 0.24 | 43.75% | 29.27% | 5.88% | 26.17% |
| Claude Code | Claude Opus 4.6 | 0.49 | 0.31 | 50.00% | 39.02% | 14.71% | 34.58% |
|  | DeepSeek V4 Pro | 0.41 | 0.25 | 39.58% | 28.46% | 13.73% | 27.10% |
|  | Kimi K2.6 | 0.43 | 0.22 | 31.25% | 36.59% | 11.76% | 27.10% |
|  | GLM 5.1 | 0.38 | 0.25 | 37.50% | 31.71% | 8.82% | 26.17% |
|  | DeepSeek V4 Flash | 0.39 | 0.24 | 46.88% | 26.83% | 5.88% | 26.17% |
|  | Claude Sonnet 4.6 | 0.42 | 0.23 | 31.25% | 29.27% | 14.71% | 25.23% |
| **VERAS** | **GPT-5.4** | **0.67** | **0.39** | **56.25%** | **39.02%** | **35.29%** | **42.99%** |

VERAS achieves **42.99%** overall pass rate compared with **35.51%** for Codex with GPT-5.4.
Hard-task pass rate increases from **20.59%** to **35.29%**.

## Industrial case studies

### Inter-datacenter placement

VERAS reduces peak inter-datacenter bandwidth by **10.34%** compared with Native Codex.

Peak bandwidth and migration volume.

| Method | Peak (Gbps) ↓ | Migration (PB) ↓ |
| --- | ---: | ---: |
| Operational mapping | 19,495.167 | 0.000 |
| Historical-majority heuristic | 18,276.071 | 0.333 |
| [Native Codex](real_world/data_center/native_codex/algorithm/solve.py) | 1,238.625 | 25.473 |
| [VERAS](real_world/data_center/veras/algorithm/solve.py) | 1,110.603 | 25.901 |

### 54-bus distribution network planning

VERAS achieves an annualized cost of **USD 39.424M/year**, reducing cost by **6.28%** relative to Hourly MISOCP and **13.71%** relative to Native Codex.

Annualized costs in million USD/year.

| Method | Total ↓ | Investment ↓ | Operation ↓ |
| --- | ---: | ---: | ---: |
| Hourly MISOCP | 42.065 | 18.984 | 23.081 |
| Varying-timestep MISOCP | 42.923 | 19.012 | 23.911 |
| [Native Codex](real_world/power_grid/native_codex/algorithm/solve.py) | 45.690 | 18.490 | 27.200 |
| [VERAS](real_world/power_grid/veras/algorithm/solve.py) | 39.424 | 16.154 | 23.269 |
<!-- END PAPER RESULTS -->

## Layout

- `veras/`: harness implementation, tests, score summaries, and interactive results.
- `benchmark/`: benchmark guide and scoring rules.
- `real_world/`: case-study algorithms and evaluation records.
- `docs/`: reproducibility and data-processing documentation.
- `scripts/`: artifact verification and report generation.

## Data availability

ORAgentBench task data is available from the benchmark distribution. Place it
in the layout described by `veras/README.md`. Algorithm sources and evaluation
records for the industrial case studies are included under `real_world/`.
