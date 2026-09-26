# ORAgentBench Evidence

The VERAS harness and retained benchmark evidence are under `../veras/`.

## Interactive reports

- Overall results: `../veras/reports/benchmark-results/index.html`
- Unified results and retained trajectories: `../veras/reports/benchmark-results/index.html`
- Per-task trajectories: `../veras/reports/benchmark-results/tasks/<task>/index.html`

The per-task pages contain sanitized event timelines, generated code, verifier
outputs, scores, and component activation evidence where available.

## Machine-readable results

- All 107 tasks: `../veras/evidence/official-score-summary.json`
- Component evidence: `../veras/evidence/component-evidence-summary.json`
- Component evidence: `../veras/evidence/*-component-evidence-summary.json`

Easy and Medium raw trajectories are unavailable in the retained evaluation
record. This repository reports that limitation rather than reconstructing or
substituting traces after the fact.

## Evaluation protocol

A task passes only if the official evaluator reports positive feasibility and
the normalized quality criterion `quality / 2 > 0.4`. Diagnostic observer
signals, artifact completeness, and raw verifier execution do not replace the
official pass condition.
