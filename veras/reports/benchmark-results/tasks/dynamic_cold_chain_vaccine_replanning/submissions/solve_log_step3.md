# Week 4 Replan Solve Log

## Restoration Audit

- Baseline suffix theta under week-4 data: `7.83699060e-01`
- Baseline suffix violated groups: `4`
- Baseline violated families: `demand_balance, service_floor, inventory_balance, inventory_floor`
- Baseline suffix audit pass: `no`

## Repair Findings

- Rollback incumbent frozen-window violations reproduced: `5`
- `ship_plant_dc` week `5` `V_PED` `FILL_EAST` -> `DC_NORTHEAST` baseline=`0.00000000` allowed=`[0.00000000, 0.00000000]` actual=`22.51895000`
- `ship_plant_dc` week `5` `V_PED` `FILL_EAST` -> `DC_MIDWEST` baseline=`0.00000000` allowed=`[0.00000000, 0.00000000]` actual=`4.20024074`
- `ship_plant_dc` week `5` `V_PED` `FILL_EAST` -> `DC_SOUTH` baseline=`0.00000000` allowed=`[0.00000000, 0.00000000]` actual=`15.07905000`
- `transfer_dc` week `4` `V_PED` `DC_MIDWEST` -> `DC_SOUTH` baseline=`0.00000000` allowed=`[0.00000000, 0.00000000]` actual=`4.51074074`
- `transfer_dc` week `4` `V_PED` `DC_WEST` -> `DC_SOUTH` baseline=`0.00000000` allowed=`[0.00000000, 0.00000000]` actual=`0.55175926`

## Public State Audit

- Accepted step-2 prior-week inventory rows complete: `yes`
- Prior-week used for state bridge: `3`
- Accepted/prior-inventory vs provided-state max raw diff: `0.00000000e+00`
- Current rollback incumbent audit pass: `no`
- Rollback incumbent objective: `136018.36645997`

## Performance Run

- MIP start accepted: `yes`
- Solve status: `optimal`
- Time limit seconds: `180.0`
- Solve time seconds: `0.7715`
- Solver incumbent count: `6`
- Best bound: `138063.39866277`
- Relative gap: `0.00000000`
- Candidate objective: `138063.39866252`
- Candidate audit pass: `yes`
- Publication result: `rollback incumbent kept; no strict audited improvement`

## Final Audit

- Published artifact audit pass: `no`
- Exported rows: `391`
- Theta: `2.25189500e+01`
- Violated groups: `2`
- Integrality residual: `0.00000000e+00`
- Decision coverage: `weeks=7` `ship_pd_keys=224` `ship_dc_keys=252` `transfer_keys=168`
- Corrupted-copy rejection test: `passed`

## Objective Breakdown

- `dc_to_customer_cost`: `16761.88517980`
- `emergency_material_cost`: `0.00000000`
- `handling_cost`: `7294.79306320`
- `holding_cost`: `1761.72055329`
- `overtime_production_cost`: `0.00000000`
- `plant_to_dc_cost`: `13861.04889478`
- `regular_production_cost`: `73762.61966600`
- `replan_change_cost`: `4102.87500168`
- `setup_cost`: `17470.00000000`
- `shortage_cost`: `0.00000000`
- `transfer_cost`: `1003.42410121`
- `recomputed_total`: `136018.36645997`

## Variable Counts

- `inventory`: `84`
- `produce_regular`: `34`
- `setup`: `34`
- `ship_dc_customer`: `151`
- `ship_plant_dc`: `78`
- `transfer_dc`: `10`

## Constraint Residuals

- `dc_customer_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `dc_transfer_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `demand_balance`: normalized=`1.62076354e-16` raw=`3.55271368e-15`
- `emergency_cap`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_dc_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_dc_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_pd_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_pd_ub`: normalized=`2.25189500e+01` raw=`2.25189500e+01`
- `frozen_transfer_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_transfer_ub`: normalized=`4.51074074e+00` raw=`4.51074074e+00`
- `inventory_balance`: normalized=`1.25563221e-15` raw=`8.88178420e-15`
- `inventory_floor`: normalized=`2.17052400e-16` raw=`1.77635684e-15`
- `material_balance`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `overtime_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `plant_dc_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `plant_ship_balance`: normalized=`1.94463585e-16` raw=`1.42108547e-14`
- `prod_replan_abs`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `regular_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `service_floor`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_lower`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_upper`: normalized=`1.45687554e-10` raw=`9.99999372e-09`
- `warehouse_capacity`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
