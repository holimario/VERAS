# Week 2 Replan Solve Log

## Restoration Audit

- Baseline suffix theta under week-2 data: `5.17608696e-01`
- Baseline suffix violated groups: `3`
- Baseline violated families: `material_balance, dc_customer_lane, inventory_balance`
- Baseline suffix audit pass: `no`

## Public State Audit

- Week-2 initial inventory reconstructed from public inputs: `yes`
- Week-1 balance max raw residual: `1.42108547e-14`
- Reconstructed/public-state vs provided-state max raw diff: `0.00000000e+00`
- Current rollback incumbent audit pass: `yes`
- Rollback incumbent objective: `169893.73380179`

## Performance Run

- MIP start accepted: `yes`
- Solve status: `optimal`
- Time limit seconds: `180.0`
- Solve time seconds: `0.1361`
- Solver incumbent count: `6`
- Best bound: `169893.73380221`
- Relative gap: `0.00000000`
- Candidate objective: `169893.73380179`
- Candidate audit pass: `yes`
- Publication result: `rollback incumbent kept; no strict audited improvement`

## Final Audit

- Exported rows: `484`
- Theta: `2.73171967e-10`
- Violated groups: `0`
- Integrality residual: `0.00000000e+00`
- Decision coverage: `weeks=9` `ship_pd_keys=288` `ship_dc_keys=324` `transfer_keys=216`
- Corrupted-copy rejection test: `passed`

## Objective Breakdown

- `dc_to_customer_cost`: `20334.89549400`
- `emergency_material_cost`: `0.00000000`
- `handling_cost`: `8414.00212000`
- `holding_cost`: `2337.65380857`
- `overtime_production_cost`: `0.00000000`
- `plant_to_dc_cost`: `17310.18431375`
- `regular_production_cost`: `91936.16099969`
- `replan_change_cost`: `6689.48869442`
- `setup_cost`: `21540.00000000`
- `shortage_cost`: `0.00000000`
- `transfer_cost`: `1331.34837137`
- `recomputed_total`: `169893.73380179`

## Variable Counts

- `inventory`: `108`
- `produce_regular`: `42`
- `setup`: `42`
- `ship_dc_customer`: `186`
- `ship_plant_dc`: `94`
- `transfer_dc`: `12`

## Constraint Residuals

- `dc_customer_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `dc_transfer_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `demand_balance`: normalized=`1.62076354e-16` raw=`3.55271368e-15`
- `emergency_cap`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_prod_reg_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_prod_reg_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_dc_lb`: normalized=`1.37202197e-16` raw=`1.77635684e-15`
- `frozen_ship_dc_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_pd_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_pd_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `inventory_balance`: normalized=`2.73171967e-10` raw=`9.99999372e-09`
- `inventory_floor`: normalized=`1.47361697e-16` raw=`8.88178420e-16`
- `material_balance`: normalized=`1.42863526e-11` raw=`1.60001434e-09`
- `overtime_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `plant_dc_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `plant_ship_balance`: normalized=`1.94463585e-16` raw=`1.42108547e-14`
- `prod_replan_abs`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `regular_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `service_floor`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_lower`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_upper`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `warehouse_capacity`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
