# Solve Log

## Commands
- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Base Commitment
- anchor search choice: {'CAST_A1': 160, 'CAST_A2': 0, 'CAST_A3': 0, 'CAST_A4': 0, 'CAST_B1': 170, 'CAST_B2': 540, 'CAST_B3': 0, 'CAST_B4': 0}
- actual cast first starts: {'CAST_A1': 160, 'CAST_A2': 358, 'CAST_A3': 556, 'CAST_A4': 768, 'CAST_B1': 170, 'CAST_B2': 540, 'CAST_B3': 738, 'CAST_B4': 950}
- base makespan: 1148.00000000
- committed ladles used: 22

## SCIP Runs
- BASE: status=gaplimit, obj=15918.15000000, best_bound=15917.338920001115, gap=5.095575353112718e-05, solve_time_sec=2.5074
- BOF_SLOW: status=gaplimit, obj=23082.45000000, best_bound=23081.649999999965, gap=3.465956723147434e-05, solve_time_sec=4.0797
- RH_OUTAGE: status=gaplimit, obj=17370.50000000, best_bound=17362.192948298296, gap=0.0004784563635701344, solve_time_sec=2.3933
- OXYGEN_TIGHT: status=gaplimit, obj=15919.05000000, best_bound=15917.362583284063, gap=0.00010601107483081819, solve_time_sec=3.3615

## Validation
- hard errors: 0
- violated hard-constraint groups: 0
- max normalized hard residual: 0.00000000
- weighted public-data score estimate: 151494.16378760

## Scenario Metrics
- BASE: score=136778.37373700, tardiness=15606.00000000, temp=0.00000000, cast_gap=0.00000000, energy=23529.40373700, oxygen_shortage=53827.00000000, oxygen_vent=41718.60000000, preheat=2097.37000000
- BOF_SLOW: score=178486.97138754, tardiness=22410.00000000, temp=0.00000000, cast_gap=324.00000000, energy=24788.69790754, oxygen_shortage=96286.61160000, oxygen_vent=32666.36688000, preheat=2011.29500000
- RH_OUTAGE: score=141759.48346456, tardiness=17046.00000000, temp=0.00000000, cast_gap=0.00000000, energy=24099.33926456, oxygen_shortage=57311.06800000, oxygen_vent=41276.49120000, preheat=2026.58500000
- OXYGEN_TIGHT: score=171452.23694900, tardiness=15606.00000000, temp=0.00000000, cast_gap=0.00000000, energy=23549.84034900, oxygen_shortage=93603.23400000, oxygen_vent=36574.61760000, preheat=2118.54500000

## Feasibility Checks
- checked schema, full operation coverage, and cross-scenario fixed machine/ladle commitments
- checked route order, machine eligibility, machine non-overlap with setups, cast continuity, and ladle maintenance/non-overlap
- checked crane, alloy-crew, laboratory, and ladle-preheat bucket capacities from the exported artifact

## Guarded Performance Phase
- public challenger report contained no explicit violated rule; the actionable request was to rerun a disk-based independent audit before any improvement attempt
- incumbent artifact reparsed from disk after restoration: weighted_score=151494.16378760, hard_errors=0, violated_groups=0, max_residual=0.00000000
- performance method: bounded exact neighborhood search over cast-anchor commitments, with each scenario MIP warm-started from the incumbent exported schedule
- incumbent candidate: anchors={'CAST_A1': 160, 'CAST_A2': 0, 'CAST_A3': 0, 'CAST_A4': 0, 'CAST_B1': 170, 'CAST_B2': 540, 'CAST_B3': 0, 'CAST_B4': 0}, weighted_score=151494.16378760, audit=pass, scenario_gaps=[5.095575353112718e-05, 3.465956723147434e-05, 0.0004784563635701344, 0.00010601107483081819]
- tested feasible warm-start candidates:
- anchors={'CAST_A1': 160, 'CAST_A2': 0, 'CAST_A3': 0, 'CAST_A4': 0, 'CAST_B1': 180, 'CAST_B2': 540, 'CAST_B3': 0, 'CAST_B4': 0}, weighted_score=163952.49480900, audit=pass, rejected
- anchors={'CAST_A1': 160, 'CAST_A2': 0, 'CAST_A3': 0, 'CAST_A4': 0, 'CAST_B1': 170, 'CAST_B2': 550, 'CAST_B3': 0, 'CAST_B4': 0}, weighted_score=153217.20010100, audit=pass, rejected
- anchors={'CAST_A1': 160, 'CAST_A2': 0, 'CAST_A3': 0, 'CAST_A4': 0, 'CAST_B1': 170, 'CAST_B2': 560, 'CAST_B3': 0, 'CAST_B4': 0}, weighted_score=163384.35590800, audit=pass, rejected
- anchors={'CAST_A1': 160, 'CAST_A2': 0, 'CAST_A3': 0, 'CAST_A4': 0, 'CAST_B1': 170, 'CAST_B2': 600, 'CAST_B3': 0, 'CAST_B4': 0}, weighted_score=181018.14231000, audit=pass, rejected
- infeasible or non-usable neighborhood points: A1 in {140, 150, 170, 180}; B1 in {150, 160, 190}; B2 in {500, 520, 530}; B2=580 failed in scenario OXYGEN_TIGHT; B2=620 failed in scenario BOF_SLOW
- best candidate objective among audited neighbors did not strictly improve on the incumbent, so the incumbent artifact was retained unchanged
