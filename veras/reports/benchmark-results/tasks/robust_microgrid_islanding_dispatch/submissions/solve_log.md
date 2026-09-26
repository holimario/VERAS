# Solve Log

## Repair Notes

- Hardened the independent disk audit so it accepts string or `Path` inputs, checks exact CSV headers, validates row identifiers and numeric parsing, and handles missing keys without crashing.
- Treated the existing `solution.csv` as the rollback incumbent and only allowed publication of a candidate with zero hard-check violations and strictly lower independently recomputed objective.
- Used a bounded exact SCIP solve with the incumbent loaded as a MIP start.

## Commands

- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Rollback Incumbent

- Audit objective from disk: `4179795.68387844`
- Audit theta: `6.50543370e-12`
- Audit passed: `True`
- Prior solve status: `gaplimit`
- Prior best bound: `4178861.65578497`
- Prior relative gap: `0.00022351`

## Candidate Solve

- Time limit: `120.0` seconds
- MIP start accepted: `True`
- MIP start values set: `6633`
- Status: `gaplimit`
- Incumbent count: `2`
- Solver objective: `4179795.68387844`
- Best bound: `4178131.44709568`
- Relative gap: `0.00039832`
- Candidate audited objective: `4179795.68387844`
- Candidate audit theta: `8.05144464e-12`
- Candidate audit passed: `True`

## Final Artifact

- Published artifact: `rollback_incumbent_kept`
- Final objective from exported artifact: `4179795.68387844`
- Final theta: `6.50543370e-12`
- Final hard-check pass: `True`

## Scenario Costs

- `S1`: `2109834.59714149`
- `S2`: `2655583.72011254`
- `S3`: `3091653.47671117`
- `S4`: `2933968.00509244`
- `S5`: `2084032.15246670`
- `S6`: `3520106.67043022`
- `S7`: `3186193.31755038`
- `S8`: `4108008.55895338`

## Validation

- Header exact: `True`
- Row coverage: `2304/2304` rows
- Coverage exact: `True`
- Max normalized hard-constraint residual theta: `6.50543370e-12`
- Violated constraint groups: `0`
- Integrality residual: `0.00000000e+00`
- Expected cost from exported artifact: `2871245.81921625`
- CVaR from exported artifact: `3738713.89903483`
- Total objective from exported artifact: `4179795.68387844`
- Audit coverage complete: `True`
- Checked groups: `['schema/header', 'row coverage and duplicates', 'identifier domains and numeric parsing', 'generator commitment consistency', 'generator bounds/ramping/reserve', 'minimum up/down', 'storage soc/mode/reserve/terminal', 'ev connection/soc/reserve/departure target', 'renewable availability and curtailment', 'grid limits and islanding', 'demand response cap/rebound/daily/consecutive', 'load shedding cap', 'power balance', 'reserve and island stability floors', 'emissions cap', 'objective recomputation']`
- Corrupted-copy audit: forcing `grid` import to `1.0` in islanded scenario `S6`, hour `17`, triggered `2` violated groups ['grid', 'power_balance'] with `theta = 9.09090909e-04`.
- Independent audit: all checked public constraints passed.
