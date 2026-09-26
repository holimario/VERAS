# Initial Solve Log

## Rollback Incumbent

- Objective from disk audit: `174947.38211825`
- Audit pass: `yes`
- Rows: `503`
- Theta: `1.97433252e-11`
- Unsupported lane rows: `0`
- Unsupported plant-product rows: `0`

## Performance Phase

- MIP start accepted: `yes`
- Solve status: `timelimit`
- Time limit seconds: `60.0`
- Solve time seconds: `60.0449`
- Solver incumbent count: `97`
- Best bound: `174297.73670018`
- Relative gap: `0.00372722`
- Candidate objective from disk audit: `174947.38211825`
- Candidate audit pass: `yes`
- Published artifact: `rollback incumbent kept; no strict improvement`

## Final Artifact Audit

- Exported rows: `503`
- Max normalized hard residual theta: `1.97433252e-11`
- Violated hard-constraint groups: `0`
- Integrality residual: `0.00000000e+00`
- Decision coverage: `weeks=10/10`, `demand_keys=180` reconstructible from sparse export, `inventory_keys=120` reconstructible
- Corrupted-copy rejection test: `passed`

## Objective Breakdown

- `dc_to_customer_cost`: `21938.48748400`
- `emergency_material_cost`: `0.00000000`
- `handling_cost`: `9269.63207000`
- `holding_cost`: `2480.71834793`
- `overtime_production_cost`: `0.00000000`
- `plant_to_dc_cost`: `18817.69421632`
- `regular_production_cost`: `99370.85000000`
- `setup_cost`: `23070.00000000`
- `shortage_cost`: `0.00000000`
- `transfer_cost`: `0.00000000`
- `recomputed_total`: `174947.38211825`

## Constraint Residuals

- `dc_customer_lane`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `dc_transfer_lane`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `demand_balance`: normalized=`2.14535850e-16`, raw=`1.77635684e-15`
- `emergency_cap`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `inventory_balance`: normalized=`6.20648069e-16`, raw=`1.42108547e-14`
- `inventory_floor`: normalized=`2.14613609e-16`, raw=`1.77635684e-15`
- `material_balance`: normalized=`1.97433252e-11`, raw=`2.40001441e-09`
- `overtime_hours`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `plant_dc_lane`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `plant_ship_balance`: normalized=`2.15970436e-16`, raw=`1.42108547e-14`
- `regular_hours`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `service_floor`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `setup_hours`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `setup_lower`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `setup_upper`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`
- `warehouse_capacity`: normalized=`0.00000000e+00`, raw=`0.00000000e+00`

## Notes

- Reserve stock is enforced as safety stock plus 11% of next-week demand for customers in the same DC region.
- Shortage uses the strategic product penalty only for customers in the `public_health` segment.
- Production is shipped out of the plant in the same week; no plant finished-goods inventory or disposal is allowed.
- The public challenger's unsupported-lane concern was replayed directly on the exported CSV; zero unsupported shipment or transfer rows were found.
