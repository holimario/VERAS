# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Environment

- `pyscipopt` version: `5.6.0`
- SCIP relative gap: `0.0005`
- SCIP time limit used: `120.0` seconds

## Candidate Sets

- `east` patterns generated: `10000`
- `north_central` patterns generated: `6`
- `south_pair` patterns generated: `40`
- `southeast` patterns generated: `238`
- `southwest` patterns generated: `10000`
- `west_pair` patterns generated: `20`

## Incumbent Audit

- Incumbent rows on disk: `72`
- Incumbent objective from independent audit: `14155.57797944`
- Incumbent audit violations: `0`

## Candidate Solve

- Status: `gaplimit`
- Incumbent available: `True`
- Master objective: `13884.73168229`
- Reported gap: `0.00009424`
- Candidate objective from independent audit: `13884.73168229`
- Candidate audit violations: `0`
- Published candidate: `True`

## Selected Patterns

- `east` -> `east_3_12016` with objective `1628.13284769`
- Vehicle `EV2` events: `pickup:H14 -> pickup:H20 -> unload:F5 -> pickup:H26 -> pickup:H02 -> unload:F2 -> pickup:H08 -> pickup:H32 -> pickup:H38 -> unload:F2`
- `north_central` -> `north_central_5` with objective `2352.30612272`
- Vehicle `EV8` events: `pickup:H44 -> pickup:H43 -> unload:F5 -> pickup:H39 -> pickup:H17 -> pickup:H11 -> pickup:H35 -> pickup:H29 -> pickup:H05 -> pickup:H23 -> unload:F5`
- `south_pair` -> `south_pair_3` with objective `2001.81262728`
- Vehicle `EV3` events: `pickup:H18 -> pickup:H36 -> pickup:H30 -> pickup:H40 -> unload:F3`
- Vehicle `EV6` events: `pickup:H24 -> pickup:H12 -> pickup:H06 -> unload:F3`
- `southeast` -> `southeast_108` with objective `2086.21108645`
- Vehicle `EV4` events: `pickup:H34 -> pickup:H22 -> pickup:H04 -> pickup:H28 -> unload:F2 -> pickup:H42 -> pickup:H10 -> pickup:H16 -> unload:F2`
- `southwest` -> `southwest_3_1398` with objective `1295.10959462`
- Vehicle `EV1` events: `pickup:H03 -> pickup:H21 -> unload:F3 -> pickup:H15 -> pickup:H27 -> pickup:H09 -> unload:F3 -> pickup:H33 -> pickup:H41 -> unload:F3`
- `west_pair` -> `west_pair_0` with objective `2010.15940352`
- Vehicle `EV5` events: `pickup:H07 -> pickup:H01 -> pickup:H19 -> unload:F1`
- Vehicle `EV7` events: `pickup:H31 -> pickup:H37 -> pickup:H13 -> pickup:H25 -> unload:F1`

## Final Artifact Audit

- Violations found by independent audit: `0`
- Objective estimate from exported CSV: `13884.73168229`
- Open facilities: `F1, F2, F3, F5`
- Facility `F1` total kg: `512.90000000`
- Facility `F2` total kg: `882.40000000`
- Facility `F3` total kg: `941.30000000`
- Facility `F5` total kg: `802.70000000`
- Window `F1:early` -> unloads `2`, kg `512.90000000`
- Window `F2:early` -> unloads `2`, kg `425.20000000`
- Window `F2:midday` -> unloads `2`, kg `457.20000000`
- Window `F3:early` -> unloads `3`, kg `514.50000000`
- Window `F3:midday` -> unloads `2`, kg `426.80000000`
- Window `F5:early` -> unloads `2`, kg `334.10000000`
- Window `F5:midday` -> unloads `1`, kg `468.60000000`
- Tightest slack `battery_kwh`: `14.77280836`
- Tightest slack `charger_kwh`: `305.00000000`
- Tightest slack `charger_sessions`: `7.00000000`
- Tightest slack `facility_daily_capacity_kg`: `7.60000000`
- Tightest slack `facility_min_throughput_kg`: `362.90000000`
- Tightest slack `facility_red_capacity_kg`: `8.80000000`
- Tightest slack `facility_window_count`: `0.00000000`
- Tightest slack `facility_window_kg`: `54.60000000`
- Tightest slack `facility_yellow_capacity_kg`: `162.60000000`
- Tightest slack `isolation_pickup_margin`: `0.00000000`
- Tightest slack `latest_service_start_min`: `101.50000000`
- Tightest slack `max_open_facilities`: `1.00000000`
- Tightest slack `red_onboard_min`: `57.10000000`
- Tightest slack `shift_min`: `288.10000000`
- Tightest slack `vehicle_red_capacity_kg`: `24.10000000`
- Tightest slack `vehicle_total_capacity_kg`: `7.40000000`
- Tightest slack `vehicle_yellow_capacity_kg`: `112.30000000`
- Tightest slack `yellow_onboard_min`: `138.10000000`

## Notes

- The audit objective is recomputed directly from the exported CSV, including travel, treatment, fixed/open costs, curfew waiting, early waiting, service-time risk, and charging-time risk.
- The performance phase expands the neighborhood with three-unload patterns for `EV1` and `EV2`, then solves an exact pattern master with the on-disk incumbent as the warm start.
