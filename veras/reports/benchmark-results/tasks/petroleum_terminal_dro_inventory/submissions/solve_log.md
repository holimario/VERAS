# Solve Log

- Environment check:
  `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
  Result: `5.6.0`

## Attempt 1

- Command:
  `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=30 python /app/submissions/solve.py`
- Purpose:
  short diagnostic solve to verify model build, incumbent generation, and export.
- Outcome:
  SCIP produced an incumbent, but the independent export audit failed with
  `service_floor` violations.
- Diagnosis:
  the initial station formulation allowed voluntary shortage slack in a
  time-limited incumbent, which created non-exported station inventory that the
  file-based audit would not credit.
- Fix applied:
  added an exact binary reserve-binding branch for each station-day-product so
  shortage can be positive only when the station ends exactly at reserve.

## Attempt 2

- Command:
  `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=30 python /app/submissions/solve.py`
- SCIP status:
  `timelimit`
- Incumbent count:
  `1`
- Solver incumbent objective:
  `222289.563428`
- Solver dual bound:
  `107933.538404`
- Solver relative gap:
  `1.059504`
- Validation:
  feasible `True`
- Validation max theta:
  `0.000000e+00`
- Validation violated groups:
  `[]`
- Validation objective:
  `222289.563428`
- Validation expected scenario cost:
  `179940.081334`
- Validation worst scenario cost:
  `209530.415184`

## Attempt 3

- Command:
  `python /app/submissions/solve.py`
- Time limit used:
  `300.00` seconds
- SCIP status:
  `timelimit`
- Incumbent count:
  `1`
- Solver incumbent objective:
  `222289.563428`
- Solver dual bound:
  `109728.161341`
- Solver relative gap:
  `1.025821`
- Validation:
  feasible `True`
- Validation max theta:
  `0.000000e+00`
- Validation violated groups:
  `[]`
- Validation objective:
  `222289.563428`
- Validation expected scenario cost:
  `179940.081334`
- Validation worst scenario cost:
  `209530.415184`

## Final Artifact Check

- Command:
  `python - <<'PY' ... audit_solution(load_data(), Path('/app/submissions/solution.csv')) ... PY`
- Result:
  feasible `True`
- Objective from exported artifact:
  `222289.563428`
- Expected scenario cost:
  `179940.081334`
- Worst scenario cost:
  `209530.415184`
- Violated groups:
  `[]`
- Exported rows:
  `1788`

## Guarded Phase 2026-09-10 07:27:23
- Command: `/app/submissions/solve.py`
- Challenger report: none provided
- Incumbent rows on disk: `1788`
- Incumbent audit feasible: `True`
- Incumbent max theta: `0.000000e+00`
- Incumbent violated groups: `[]`
- Incumbent objective: `222289.563428`
- Incumbent expected scenario cost: `179940.081334`
- Incumbent worst scenario cost: `209530.415184`

## Guarded Phase 2026-09-10 07:27:45
- Command: `/app/submissions/solve.py`
- Challenger report: none provided
- Incumbent rows on disk: `1788`
- Incumbent audit feasible: `True`
- Incumbent max theta: `0.000000e+00`
- Incumbent violated groups: `[]`
- Incumbent objective: `222289.563428`
- Incumbent expected scenario cost: `179940.081334`
- Incumbent worst scenario cost: `209530.415184`

## Guarded Phase 2026-09-10 07:30:48
- Command: `/app/submissions/solve.py`
- Challenger report: none provided
- Incumbent rows on disk: `1788`
- Incumbent audit feasible: `True`
- Incumbent max theta: `0.000000e+00`
- Incumbent violated groups: `[]`
- Incumbent objective: `222289.563428`
- Incumbent expected scenario cost: `179940.081334`
- Incumbent worst scenario cost: `209530.415184`

## Fixed-X Reopt
- Budget (s): `10.50`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `222289.563428`
- Solver status: `timelimit`
- Incumbent count: `1`
- Dual bound: `121526.32857654695`
- Relative gap: `0.6823322131957233`
- Build time (s): `0.54`
- Solve time (s): `10.63`
- Candidate objective: `204447.657316`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `True`

## Guarded Phase Result
- Published improved artifact: `204447.657316` < `222289.563428`
- Final audit feasible: `True`
- Final violated groups: `[]`

## Guarded Phase 2026-09-10 07:31:17
- Command: `/app/submissions/solve.py`
- Challenger report: none provided
- Incumbent rows on disk: `1772`
- Incumbent audit feasible: `True`
- Incumbent max theta: `0.000000e+00`
- Incumbent violated groups: `[]`
- Incumbent objective: `204447.657316`
- Incumbent expected scenario cost: `163268.046348`
- Incumbent worst scenario cost: `188680.269031`

## Fixed-X Reopt
- Budget (s): `105.00`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `204447.657316`
- Solver status: `timelimit`
- Incumbent count: `3`
- Dual bound: `122796.22762449284`
- Relative gap: `0.04212546783741221`
- Build time (s): `0.53`
- Solve time (s): `105.09`
- Candidate objective: `127969.076162`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `True`

## Inbound L1 Neighborhood
- Budget (s): `90.00`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `127969.076162`
- Solver status: `timelimit`
- Incumbent count: `1`
- Dual bound: `116173.62799444527`
- Relative gap: `0.06228303036270064`
- Build time (s): `0.53`
- Solve time (s): `90.00`
- Candidate objective: `123395.371690`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `True`

## Inbound L1 Neighborhood Wide
- Budget (s): `105.00`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `123395.371690`
- Solver status: `timelimit`
- Incumbent count: `1`
- Dual bound: `110632.07331126617`
- Relative gap: `0.10851559906097642`
- Build time (s): `0.51`
- Solve time (s): `105.14`
- Candidate objective: `122631.330087`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `True`

## Guarded Phase Result
- Published improved artifact: `122631.330087` < `204447.657316`
- Final audit feasible: `True`
- Final violated groups: `[]`

## Guarded Phase 2026-09-10 07:36:46
- Command: `/app/submissions/solve.py`
- Challenger report: none provided
- Incumbent rows on disk: `1567`
- Incumbent audit feasible: `True`
- Incumbent max theta: `0.000000e+00`
- Incumbent violated groups: `[]`
- Incumbent objective: `122631.330087`
- Incumbent expected scenario cost: `83638.222173`
- Incumbent worst scenario cost: `101867.575117`

## Fixed-X Reopt
- Budget (s): `105.00`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `122631.330087`
- Solver status: `timelimit`
- Incumbent count: `2`
- Dual bound: `117880.34690151742`
- Relative gap: `0.04030343743047967`
- Build time (s): `0.53`
- Solve time (s): `105.09`
- Candidate objective: `122631.330087`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `False`

## Inbound L1 Neighborhood
- Budget (s): `90.00`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `122631.330087`
- Solver status: `timelimit`
- Incumbent count: `1`
- Dual bound: `114039.85691013595`
- Relative gap: `0.07468678481095715`
- Build time (s): `0.52`
- Solve time (s): `90.00`
- Candidate objective: `122557.127164`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `True`

## Inbound L1 Neighborhood Wide
- Budget (s): `105.00`
- MIP start accepted by SCIP: `True`
- Incumbent objective before attempt: `122557.127164`
- Solver status: `timelimit`
- Incumbent count: `1`
- Dual bound: `110132.42064980774`
- Relative gap: `0.11207301177887673`
- Build time (s): `0.52`
- Solve time (s): `105.00`
- Candidate objective: `122475.292727`
- Candidate audit feasible: `True`
- Candidate violated groups: `[]`
- Accepted: `True`

## Guarded Phase Result
- Published improved artifact: `122475.292727` < `122631.330087`
- Final audit feasible: `True`
- Final violated groups: `[]`
