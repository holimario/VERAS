# Solve Log

## Commands
- `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Incumbent Audit
- Incumbent rows `42`, scheduled cases `0`, valid `True`, theta `0.0`, violated groups `0`, objective `1694823.2999999998`.
- Mandatory feasibility probe: `('C01', 'OR1', 45, 1648934.736)`.

## Repair And Search
- Greedy/LNS candidate objective `868733.9389999999`, scheduled `20`, moves `21`.
- Accepted moves: `add:C39:OR5:415:1645732.9091999999; add:C17:OR7:330:1597211.978; add:C31:OR7:70:1550188.0468000001; add:C19:OR6:455:1504211.3046000001; add:C01:OR2:50:1458320.1278000001; add:C33:OR6:245:1412695.5329999998; add:C25:OR5:55:1367488.9514; add:C10:OR3:245:1322722.3231999998; add:C21:OR1:65:1279893.3551999999; add:C35:OR2:475:1238570.6783999999; add:C15:OR1:515:1198084.9504; add:C20:OR4:530:1157603.5808; add:C24:OR3:465:1118368.7246000003; add:C42:OR2:215:1079338.6548000001; add:C34:OR4:295:1040992.9852; add:C28:OR1:270:1002733.1961999999; add:C02:OR4:50:966711.5246; add:C40:OR7:550:933512.0718; add:C09:OR4:185:901292.4254; add:C37:OR4:410:869828.9389999999`.
- Bounded master with MIP start: status `timelimit`, has_solution `True`, obj `593477.7999999992`, bound `574936.8171024425`, gap `0.03224873124493819`.
- Master-derived candidate rejected: `{'reason': 'cart_mip_no_solution', 'case_ids': []}`.
- Published candidate source `heuristic` reparsed valid `True`, theta `0.0`, violated groups `0`, objective `868733.9389999999`.

## Final
- Incumbent objective: `1694823.2999999998`.
- Best candidate objective: `868733.9389999999` from `heuristic`.
- Published new artifact: `True`.
- Final validation valid: `True`.
- Final validation theta: `0.0`.
- Final violated groups: `0`.
- Final total cost: `868733.9389999999`.
- Final cancellation cost: `844798.8999999999`.
- Final room overtime cost: `13067.0`.
- Final expected scenario cost: `6579.187999999998`.
- Final tail component: `4288.851`.
- Final scenario costs: `{'nominal': 3419.4499999999994, 'staff_callout': 8530.449999999997, 'wet_pack_surge': 10211.55, 'add_on_trauma': 9178.449999999997}`.
- Elapsed seconds: `167.055`.
