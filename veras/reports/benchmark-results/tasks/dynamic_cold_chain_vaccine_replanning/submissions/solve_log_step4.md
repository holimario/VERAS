# Week 6 Replan Solve Log

## Restoration Audit

- Baseline suffix theta under week-6 data: `1.50000000e+01`
- Baseline suffix violated groups: `4`
- Baseline violated families: `validated_shipper_cover, demand_balance, service_floor, inventory_balance`
- Baseline suffix audit pass: `no`

## Challenger Response

- Concrete challenger violations listed: `0`
- Actions taken: `reparsed rollback incumbent from disk, recomputed public constraints from exported rows and public data only, then ran guarded exact re-optimization with the rollback incumbent as the MIP start`
- Rollback incumbent exact-artifact audit pass: `yes`
- Rollback incumbent theta from disk audit: `6.31874562e-08`
- Public no-overlap constraint family present in this instance: `no`

## Repair Findings

- Rollback incumbent frozen-window violations reproduced: `0`

## Public State Audit

- Accepted step-3 prior-week inventory rows complete: `yes`
- Prior-week used for state bridge: `5`
- Accepted/prior-inventory vs provided-state max raw diff: `2.60000000e-07`
- Current rollback incumbent audit pass: `yes`
- Rollback incumbent objective: `119716.58727080`

## Performance Run

- MIP start accepted: `yes`
- Solve status: `optimal`
- Time limit seconds: `180.0`
- Solve time seconds: `1.4037`
- Solver incumbent count: `100`
- Best bound: `119716.58726063`
- Relative gap: `0.00000000`
- Candidate objective: `119716.58727080`
- Candidate audit pass: `yes`
- Publication result: `rollback incumbent kept; no strict audited improvement`

## Final Audit

- Published artifact audit pass: `yes`
- Exported rows: `316`
- Theta: `6.31874562e-08`
- Violated groups: `0`
- Integrality residual: `0.00000000e+00`
- Decision coverage: `weeks=5` `ship_pd_keys=160` `ship_dc_keys=180` `transfer_keys=120` `shipper_keys=15`
- Corrupted-copy rejection test: `passed`

## Objective Breakdown

- `dc_to_customer_cost`: `13259.87577578`
- `emergency_material_cost`: `1288.38031224`
- `handling_cost`: `5811.09830954`
- `holding_cost`: `1116.48969689`
- `overtime_production_cost`: `0.00000000`
- `plant_to_dc_cost`: `10853.01339114`
- `regular_production_cost`: `57416.61286800`
- `replan_change_cost`: `8972.00405190`
- `setup_cost`: `14100.00000000`
- `shortage_cost`: `182.40000000`
- `transfer_cost`: `742.71286530`
- `validated_shipper_cost`: `5974.00000000`
- `recomputed_total`: `119716.58727080`

## Variable Counts

- `emergency_material`: `3`
- `inventory`: `60`
- `produce_regular`: `27`
- `setup`: `27`
- `ship_dc_customer`: `112`
- `ship_plant_dc`: `62`
- `shortage`: `1`
- `transfer_dc`: `14`
- `validated_shipper`: `10`

## Constraint Residuals

- `dc_customer_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `dc_transfer_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `demand_balance`: normalized=`3.60000000e-08` raw=`1.80000000e-07`
- `emergency_cap`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_prod_reg_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_prod_reg_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_dc_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_dc_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_pd_lb`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `frozen_ship_pd_ub`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `inventory_balance`: normalized=`6.31874562e-08` raw=`2.60000000e-07`
- `inventory_floor`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `material_balance`: normalized=`1.23098555e-16` raw=`1.42108547e-14`
- `overtime_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `plant_dc_lane`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `plant_ship_balance`: normalized=`1.97775351e-16` raw=`7.10542736e-15`
- `prod_replan_abs`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `regular_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `service_floor`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_hours`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_lower`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `setup_upper`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `validated_shipper_cover`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `validated_shipper_dc_limit`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `validated_shipper_key_limit`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
- `warehouse_capacity`: normalized=`0.00000000e+00` raw=`0.00000000e+00`
