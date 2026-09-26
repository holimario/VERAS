# Solve Log

Command:
`PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

SCIP probe status: `optimal` with relative gap `0.0005`.
Incumbent origin: `disk`
Incumbent objective: `32959.917638`
Portfolio candidate objective: `32959.917638`
Portfolio solve status: `optimal`, dual bound `32959.917638`, gap `0.000000`
Published artifact: `disk_incumbent` with objective `32959.917638`
Audit result: `feasible` with `0` violations.
Elapsed wall time: `13.377` seconds.

Independent audit actions:
- reparsed existing solution.json from disk and recomputed all hard constraints independently

Seed / neighborhood attempts:
- `release_ready`: feasible, objective=32959.917638
- `incumbent_order`: feasible, objective=34172.931232
- `due_order`: feasible, objective=41002.153737
- `critical_order`: feasible, objective=41646.260503
- `hot_due_order`: feasible, objective=48252.944925
- `promote_L047_2`: feasible, objective=34272.035754
- `promote_L047_4`: feasible, objective=33811.990860
- `promote_L047_6`: feasible, objective=33870.371259
- `swap_up_L047`: feasible, objective=34336.885262
- `promote_L014_2`: feasible, objective=34631.584918
- `promote_L014_4`: feasible, objective=34234.003354
- `promote_L014_6`: feasible, objective=34300.991282
- `swap_up_L014`: feasible, objective=34440.111232
- `promote_L042_2`: feasible, objective=34320.197540
- `promote_L042_4`: feasible, objective=34432.148878
- `promote_L042_6`: feasible, objective=34400.502674
- `swap_up_L042`: feasible, objective=34330.650224
- `promote_L035_2`: feasible, objective=34170.112034
- `promote_L035_4`: feasible, objective=34675.098018
- `promote_L035_6`: feasible, objective=33977.515561
- `swap_up_L035`: feasible, objective=34170.793693
- `promote_L037_2`: feasible, objective=34166.226113
- `promote_L037_4`: feasible, objective=33761.488740
- `promote_L037_6`: feasible, objective=34263.646766
- `swap_up_L037`: feasible, objective=34172.931232
- `promote_L012_2`: feasible, objective=35245.815635
- `promote_L012_4`: feasible, objective=34948.549169
- `promote_L012_6`: feasible, objective=34715.969295
- `swap_up_L012`: feasible, objective=34172.931232
- `promote_L040_2`: feasible, objective=34474.004324
- `promote_L040_4`: feasible, objective=33167.381453
- `promote_L040_6`: feasible, objective=33167.381453
- `swap_up_L040`: feasible, objective=34172.931232
- `promote_L036_2`: feasible, objective=34172.931232
- `promote_L036_4`: feasible, objective=35146.724198
- `promote_L036_6`: feasible, objective=34804.098997
- `swap_up_L036`: feasible, objective=34172.931232

Highest incumbent lot-cost contributors:
- `L047`: completion=1995.025, flow_cost=735.180, tardy_cost=0.000
- `L014`: completion=1771.579, flow_cost=699.388, tardy_cost=0.000
- `L042`: completion=1939.332, flow_cost=686.204, tardy_cost=0.000
- `L035`: completion=1713.840, flow_cost=643.659, tardy_cost=0.000
- `L037`: completion=1807.154, flow_cost=639.944, tardy_cost=0.000

Validation checks performed:
- solution.json schema, event domains, and unique identifiers
- schema and one process event per operation
- machine qualification, expiry, availability, maintenance, non-overlap, and setup gaps
- route order with release, min-wait, and max-wait constraints
- implant clean-threshold resets
- reticle non-overlap and reticle hold windows
- operator and maintenance technician calendar capacities
- FOUP buffer-zone capacity after implant and before furnace
- batch consistency and objective recomputation from exported events
