# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
- `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=60 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Solver

- Phase `disk_incumbent_audit`: status `valid`, runtime `0.00000000`, incumbents `1`, objective `3997836.51952056`
- Phase `performance_exact`: status `timelimit`, runtime `41.92053962`, incumbents `1`, objective `3997836.51952056`
- Phase `local_branching`: status `timelimit`, runtime `17.99267673`, incumbents `1`, objective `3997836.51952056`

- Status: `timelimit`
- Time limit seconds: `60.00000000`
- Runtime seconds: `61.10510540`
- Incumbent count: `1`
- Solver objective: `3997836.51952056`
- Dual bound: `8418153.94932752`
- Relative gap: `1.10567739`

## Validation

- Rollback incumbent objective: `3997836.51952056`
- Candidate source: `local_branching`
- Candidate objective: `3997836.51952056`
- Artifact updated: `no`

- Selected parcels: `54`
- Capital used: `3872039.43160000`
- Maintenance used: `68743.14660000`
- Staff used: `192.33880000`
- Nursery units used: `8.00000000`
- Corridor parcels selected: `24`
- Expected covered value: `863.18153502`
- Worst-scenario covered value: `449.67000000`
- Recomputed objective: `3997836.51952056`
- Max normalized hard-constraint residual: `0.00000000`
- Violated constraint groups: `0`
- Integrality residual: `0.00000000`
- Decision universe coverage: `0.20769231`

## Action Counts

- `protect`: `8`
- `restore`: `4`
- `migration_easement`: `42`

## Violations

- None

## Scenario Scores

- `C01`: `1004.98800000`
- `C02`: `959.98800000`
- `C03`: `842.39300000`
- `C04`: `570.98600000`
- `C05`: `808.52700000`
- `C06`: `979.02800000`
- `C07`: `947.62100000`
- `C08`: `959.98800000`
- `C09`: `842.39300000`
- `C10`: `979.02800000`
- `C11`: `854.76000000`
- `C12`: `979.02800000`
- `C13`: `944.03800000`
- `C14`: `567.73300000`
- `C15`: `893.99700000`
- `C16`: `979.02800000`
- `C17`: `881.02800000`
- `C18`: `979.02800000`
- `C19`: `932.63200000`
- `C20`: `979.02800000`
- `C21`: `867.72200000`
- `C22`: `640.34200000`
- `C23`: `449.67000000`
- `C24`: `979.02800000`
