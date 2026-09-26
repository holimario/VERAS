# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
- Reported `pyscipopt` version: `5.6.0`
- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Approach

- Fixed dominant first-stage decisions: open `S1,S2,S3,S5`; activate all corridors.
- Solved a staging assignment MIP.
- Built each scenario plan with an exact-simulation constructive heuristic using 4 trip slots per bus.
- Audited the exported artifact independently from the exact JSON output.

## First-Stage Decisions

- Open shelters: `S1, S2, S3, S5`
- Active corridors: `C1, C2, C3, C4, C5`
- Staging: `B01->D3, B02->D1, B03->D2, B04->D1, B05->D2, B06->D3, B07->D3, B08->D2, B09->D2, B10->D3`

## Scenario Solve Status

- `NIGHT_IGNITION`: status `heuristic_constructive`, constructed trips `21`, internal solve time `0.0` seconds
- `RED_FLAG`: status `heuristic_constructive`, constructed trips `22`, internal solve time `0.0` seconds
- `SLOW_WIND`: status `heuristic_constructive`, constructed trips `19`, internal solve time `0.0` seconds
- `SPOT_FIRE`: status `heuristic_constructive`, constructed trips `22`, internal solve time `0.0` seconds
- `WIND_SHIFT`: status `heuristic_constructive`, constructed trips `21`, internal solve time `0.0` seconds

## Validation

- PASS: schema coverage, timing, shift limits, corridor capacities, shelter capacities, demand balance, service floors, fairness, and boarding-bay limits all satisfied.

## Objective Audit

- Shelter opening cost: `49000.0`
- Staging cost: `3520.0`
- Contraflow cost: `50000.0`
- Expected scenario cost: `449916.58779754`
- CVaR_0.8: `660789.55293057`
- Total score: `783712.93132324`

## Scenario Cost Breakdown

- `NIGHT_IGNITION`: operating `34919.96316322`, late `326605.48643793`, smoke `36561.6`, unserved `0.0`, shelter-in-place `0.0`, trips `21`
- `RED_FLAG`: operating `46613.23800575`, late `383124.52534407`, smoke `142569.6`, unserved `21600.0`, shelter-in-place `96200.0`, trips `22`
- `SLOW_WIND`: operating `27545.00172027`, late `263293.95964962`, smoke `30336.0`, unserved `0.0`, shelter-in-place `0.0`, trips `19`
- `SPOT_FIRE`: operating `45197.74676301`, late `402956.56449054`, smoke `95364.0`, unserved `0.0`, shelter-in-place `0.0`, trips `22`
- `WIND_SHIFT`: operating `39616.8072492`, late `325698.92180067`, smoke `77472.0`, unserved `0.0`, shelter-in-place `0.0`, trips `21`
