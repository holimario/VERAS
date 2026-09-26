# Initial Kidney Exchange Model

## Current Solve Mode

The guarded repair/performance pass keeps the existing CSV as the rollback
incumbent, reparses it from disk, and audits public hard constraints
independently of the optimization model before attempting any improvement.

To address the challenger's bridge-capacity ambiguity, the repair search uses
the stricter interpretation that every continuing chain position consumes
regional bridge capacity on its surgery day, including same-day continuations.
Publication still requires a zero-violation re-audit from the exported CSV.

## Objective

The optimization objective remains the explicit stage scoring function already
used in the baseline model:

- reward clinical value, altruist chain-start value, waiting time, pediatric
  service, high-priority service, and highly sensitized service;
- penalize failure probability, travel time, late-courier moves, lab-review
  burden, and delayed bridge handoffs across days.

The public brief does not publish a fuller numeric weighting scheme beyond
those modeled terms.

## Performance Method

1. Reparse `/app/submissions/initial_plan.csv` and audit it under both the
   public bridge interpretation and the stricter robustness interpretation.
2. Enumerate all valid cycles and chains with the strict bridge interpretation
   enforced as a hard capacity rule.
3. Build a repair MIP start from the disk incumbent and a strict-feasible
   greedy repair start.
4. Solve the strict exact set-packing model in SCIP with relative gap
   `0.0005` and time limit bounded by `ORCLAW_SOLVE_TIME_LIMIT_SECONDS` when set.
5. Recompute objective and hard constraints independently from the candidate
   CSV rows, and overwrite the published artifact only on strict improvement.
