# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Incumbent Audit

- incumbent valid: `False`
- incumbent hard-constraint errors: `62`
- incumbent approximate score: `97728.24`

### Incumbent Errors

- Demand mismatch for O002 / SKU17: picked 0 but required 2
- Demand mismatch for O002 / SKU26: picked 0 but required 2
- Demand mismatch for O004 / SKU06: picked 0 but required 2
- Demand mismatch for O004 / SKU33: picked 0 but required 2
- Demand mismatch for O005 / SKU04: picked 0 but required 1
- Demand mismatch for O005 / SKU09: picked 0 but required 3
- Demand mismatch for O006 / SKU08: picked 0 but required 3
- Demand mismatch for O006 / SKU17: picked 0 but required 3
- Demand mismatch for O006 / SKU26: picked 0 but required 3
- Demand mismatch for O007 / SKU03: picked 0 but required 2
- Demand mismatch for O007 / SKU25: picked 0 but required 3
- Demand mismatch for O008 / SKU24: picked 0 but required 3
- Demand mismatch for O008 / SKU33: picked 0 but required 3
- Demand mismatch for O012 / SKU04: picked 0 but required 2
- Demand mismatch for O012 / SKU24: picked 0 but required 1
- Demand mismatch for O012 / SKU33: picked 0 but required 1
- Demand mismatch for O013 / SKU23: picked 0 but required 1
- Demand mismatch for O013 / SKU32: picked 0 but required 1
- Demand mismatch for O014 / SKU04: picked 0 but required 1
- Demand mismatch for O014 / SKU31: picked 0 but required 1
- ... and `42` more

## Attempts

### Attempt 1

- imbalance multiplier: `1.0`
- assignment status: `optimal`
- assignment gap: `0.0`
- order-start status: `timelimit`
- order-start gap: `0.46158067546903125`
- schedule fallback used: `False`
- feasible artifact: `False`
- validation errors:
  - No feasible robot/time assignment for order O015, pod P14

### Attempt 2

- imbalance multiplier: `2.0`
- assignment status: `optimal`
- assignment gap: `0.0`
- order-start status: `timelimit`
- order-start gap: `0.5148157303615951`
- schedule fallback used: `False`
- feasible artifact: `False`
- validation errors:
  - No feasible robot/time assignment for order O015, pod P14

## Manual Repair Search

- incumbent rollback artifact preserved at `submissions/solution.csv`
- independent disk audit of the incumbent still reports `62` hard violations, dominated by missing fulfillment rows
- assignment-level diagnosis from public data shows the selected pattern set overloads shared pods `P11` and `P26`; with the current public travel-duration assumption, the raw assignment places about `85-91` occupied slots on each of those pods before batching
- a guarded batched-visit repair heuristic that aggregates picks across open orders reduced the implied pod usage to about `61-63` blocked slots on `P11` and `P26`, but still did not yield a zero-violation artifact
- best bounded batched-repair candidate remained incomplete with `20` units unserved across orders `O015`, `O024`, `O032`, and `O036`; later neighborhood moves improved that only to `17` residual units and still failed the hard audit
- because no candidate passed the independent audit, no candidate objective was eligible to replace the incumbent

## Candidate Decision

- publish decision: `no_fully_feasible_candidate_found`
- best candidate objective: `nan`
- best candidate audit: `no fully feasible candidate found`

## Final Artifact Audit

- elapsed seconds: `21.30`
- hard-constraint errors: `62`
- weighted tardiness: `898.0`
- priority late excess: `90.0`
- promise group spread excess: `88.0`
- visit travel: `700.0`
- energy used: `168.0`
- split order count: `15.0`
- station imbalance: `34.666666666666664`
- peak congestion proxy: `57.0`
- approximate score: `97728.24`

## Notes

- `solution.csv` is only replaced after a disk-based audit with zero hard-constraint violations.
- If no audited candidate is strictly better than the incumbent, the incumbent artifact is retained unchanged.
- The public prompt does not specify an exact soft-score formula for every term; the reported score is the solver's consistent public-data proxy.
- Validation is recomputed from the exported `solution.csv`, not from in-memory solver variables.

### Final Errors

- Demand mismatch for O002 / SKU17: picked 0 but required 2
- Demand mismatch for O002 / SKU26: picked 0 but required 2
- Demand mismatch for O004 / SKU06: picked 0 but required 2
- Demand mismatch for O004 / SKU33: picked 0 but required 2
- Demand mismatch for O005 / SKU04: picked 0 but required 1
- Demand mismatch for O005 / SKU09: picked 0 but required 3
- Demand mismatch for O006 / SKU08: picked 0 but required 3
- Demand mismatch for O006 / SKU17: picked 0 but required 3
- Demand mismatch for O006 / SKU26: picked 0 but required 3
- Demand mismatch for O007 / SKU03: picked 0 but required 2
- Demand mismatch for O007 / SKU25: picked 0 but required 3
- Demand mismatch for O008 / SKU24: picked 0 but required 3
- Demand mismatch for O008 / SKU33: picked 0 but required 3
- Demand mismatch for O012 / SKU04: picked 0 but required 2
- Demand mismatch for O012 / SKU24: picked 0 but required 1
- Demand mismatch for O012 / SKU33: picked 0 but required 1
- Demand mismatch for O013 / SKU23: picked 0 but required 1
- Demand mismatch for O013 / SKU32: picked 0 but required 1
- Demand mismatch for O014 / SKU04: picked 0 but required 1
- Demand mismatch for O014 / SKU31: picked 0 but required 1
- Demand mismatch for O015 / SKU07: picked 0 but required 2
- Demand mismatch for O015 / SKU16: picked 0 but required 2
- Demand mismatch for O015 / SKU34: picked 0 but required 2
- Demand mismatch for O016 / SKU03: picked 0 but required 2
- Demand mismatch for O016 / SKU15: picked 0 but required 2
- Demand mismatch for O020 / SKU04: picked 0 but required 1
- Demand mismatch for O020 / SKU06: picked 0 but required 3
- Demand mismatch for O022 / SKU13: picked 0 but required 3
- Demand mismatch for O022 / SKU22: picked 0 but required 3
- Demand mismatch for O023 / SKU21: picked 0 but required 3
- Demand mismatch for O023 / SKU30: picked 0 but required 3
- Demand mismatch for O024 / SKU02: picked 0 but required 3
- Demand mismatch for O024 / SKU03: picked 0 but required 1
- Demand mismatch for O024 / SKU11: picked 0 but required 3
- Demand mismatch for O025 / SKU05: picked 0 but required 1
- Demand mismatch for O025 / SKU32: picked 0 but required 1
- Demand mismatch for O027 / SKU12: picked 0 but required 1
- Demand mismatch for O027 / SKU17: picked 0 but required 3
- Demand mismatch for O027 / SKU21: picked 0 but required 1
- Demand mismatch for O028 / SKU04: picked 0 but required 3
- Demand mismatch for O028 / SKU29: picked 0 but required 1
- Demand mismatch for O029 / SKU01: picked 0 but required 1
- Demand mismatch for O029 / SKU28: picked 0 but required 1
- Demand mismatch for O030 / SKU04: picked 0 but required 2
- Demand mismatch for O030 / SKU13: picked 0 but required 2
- Demand mismatch for O030 / SKU31: picked 0 but required 2
- Demand mismatch for O031 / SKU03: picked 0 but required 2
- Demand mismatch for O031 / SKU12: picked 0 but required 2
- Demand mismatch for O032 / SKU11: picked 0 but required 2
- Demand mismatch for O032 / SKU20: picked 0 but required 2
- Demand mismatch for O033 / SKU01: picked 0 but required 2
- Demand mismatch for O033 / SKU03: picked 0 but required 1
- Demand mismatch for O033 / SKU28: picked 0 but required 2
- Demand mismatch for O038 / SKU18: picked 0 but required 3
- Demand mismatch for O038 / SKU27: picked 0 but required 3
- Demand mismatch for O039 / SKU08: picked 0 but required 3
- Demand mismatch for O039 / SKU26: picked 0 but required 3
- Demand mismatch for O039 / SKU35: picked 0 but required 3
- Demand mismatch for O040 / SKU17: picked 0 but required 1
- Demand mismatch for O040 / SKU29: picked 0 but required 1
- Demand mismatch for O041 / SKU01: picked 0 but required 1
- Demand mismatch for O041 / SKU10: picked 0 but required 1
