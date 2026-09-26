# Exact hourly 54-bus candidate

This directory contains a from-scratch implementation of the neutral
mixed-integer second-order-cone formulation. The canonical solve keeps all
planning and operational integer variables free, uses four threads, and supplies
no pre-existing or external warm start. Its deterministic internal start is
derived only from the supplied inputs during the same entry point. The four
optimizer stages have a combined 212-second limit (12 + 25 + 25 + 150).

Run from any directory with:

```bash
/work/app/candidate/run_candidate.sh
```

The entry point reads only the six supplied inputs in `/work/app`, writes only
under `/work/app/candidate`, derives a short-path forest, screens capacity for
that forest, restores an input-only integer candidate, then
solves the complete free-planning model. It exports every array required by
`OUTPUT_CONTRACT.md`, runs an independent replay audit that does not import the
model builder, and then writes the run manifest.

Key artifacts:

- `results/solution.npz`: complete float64 solution arrays.
- `results/solver_summary.json`: solver status, timing, bound, and provenance.
- `audit_report.json`: independent residual and objective reconstruction.
- `logs/mosek.log`: complete native optimizer log.
- `logs/runner.log`, `logs/audit.log`, `logs/finalize.log`: entry-point logs.

Python dependencies are used from the installed environment; none are vendored.
