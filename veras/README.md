# VERAS Harness

This directory contains the evaluated VERAS harness, its deterministic tests,
machine-readable score summaries, and sanitized benchmark trajectories.

## Quick start

Place this artifact beside a compatible ORAgentBench checkout and provide your
own OpenAI-compatible API configuration:

```bash
export ORAGENTBENCH_ROOT=/path/to/ORAgentBench
export VERAS_MODEL=your-model-id
export VERAS_BASE_URL=https://your-api-endpoint.example/v1
export VERAS_API_KEY=your-api-key
./bin/verify.sh
./bin/run_benchmark.sh all
```

If your credential uses another environment-variable name, set
`VERAS_API_KEY_ENV` accordingly. Runtime settings may also be supplied through
environment variables:

```bash
CONCURRENCY=2 TIMEOUT_SCALE=1.0 ./bin/run_benchmark.sh all
```

The command accepts `all`, `easy-medium`, or `hard` as a task selector. The
default benchmark location is `../../ORAgentBench`; `ORAGENTBENCH_ROOT`
overrides it. API endpoints, keys, and credential-source names are runtime-only
and are not serialized into result metadata.

## Configuration

- Default reasoning effort: `high`
- Default timeout scale: `1.0`
- Default recovery reserve: `0.30`
- Default challenger fraction: `0.22`
- Minimum recovery budget: `120` seconds

The complete machine-readable configuration is in `CONFIGURATION.json`.

## Harness structure

VERAS combines artifact-aware state tracking, independent semantic observers,
a non-actuating challenger, feasibility-first restoration, guarded performance
optimization, and incumbent preservation. The controller accepts candidate
changes only after structural and semantic checks.

## Benchmark record

The official rule is `feasibility > 0` and `quality / 2 > 0.4`. Observer safety,
artifact completeness, and raw verifier execution are diagnostics rather than
official passes.

| Split | Passed | Feasible | Completed | Mean normalized quality |
| --- | ---: | ---: | ---: | ---: |
| Easy | 18/32 (56.25%) | 27/32 | 32/32 | 0.546670 |
| Medium | 16/41 (39.02%) | 28/41 | 40/41 | 0.341544 |
| Hard | 12/34 (35.29%) | 17/34 | 31/34 | 0.308334 |
| Overall | 46/107 (42.99%) | 72/107 | 103/107 | 0.392337 |

The retained evidence preserves the original task denominators, including
incomplete runs. Easy and Medium task-level scores are included; their raw
event streams were not retained.

## Contents

- `src/veras_harness/controller.py`: per-task state machine and orchestration.
- `src/veras_harness/audits.py`: structural/semantic guards and control decisions.
- `src/veras_harness/runtime.py`: resilient agent execution and event parsing.
- `src/veras_harness/prompts.py`: recovery, challenger, and performance prompts.
- `src/veras_harness/observers/`: task-family semantic observers.
- `src/veras_harness/reporting/`: score and component-evidence tools.
- `bin/`: benchmark and verification entry points.
- `tests/`: deterministic controller and observer tests.
- `evidence/`: final score and component-evidence summaries.
- `reports/benchmark-results/`: unified results and retained task trajectories.
