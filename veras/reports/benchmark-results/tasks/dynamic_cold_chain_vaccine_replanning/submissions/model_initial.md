# Initial Cold-Chain Model

## Scope

This model builds the week 1-10 initial production and distribution plan for the
current `initial` stage in `/app/data/current_week.json`.

All quantities are treated in the problem's thousand-dose equivalent units. The
model is a mixed-integer linear program with binary setup decisions and
continuous flow, inventory, shortage, and emergency-material variables.

## Key Modeling Assumptions

1. Customer minimum service rates are enforced for every `(week, customer,
   product)` demand row, not only in aggregate.
2. Strategic shortage penalties apply only to customers whose segment is
   `public_health`.
3. Plants do not hold finished-goods inventory and cannot dispose production, so
   same-week plant-to-DC shipments equal same-week production by plant-product.
4. The stage policy reserve is interpreted as:

   `ending_inventory[week, dc, product] >= safety_stock[dc, product]`

   plus `11%` of the next week's demand for customers in the same warehouse
   region. In week 10 there is no week-11 reserve term, so only safety stock is
   required.

## Sets And Parameters

- Weeks `t in {1,...,10}`.
- Plants `p`, products `k`, materials `m`, distribution centers `d`, customers
  `c`.
- Feasible plant-product pairs from `plant_products.csv`.
- Demand `dem[t,c,k]`.
- Plant regular/overtime hours, setup-team hours, run-rate, min/max run size,
  and setup cost.
- Material BOM coefficients, robust regular material availability
  `0.94 * regular_available`, and emergency purchase caps/costs.
- Plant-to-DC, DC-to-customer, and DC-transfer lane capacities/costs.
- Initial DC inventory, DC freezer capacity, DC handling cost, and safety stock.
- Product holding cost and shortage penalty.

## Decision Variables

- `setup[t,p,k] in {0,1}`.
- `produce_regular[t,p,k] >= 0`.
- `produce_overtime[t,p,k] >= 0`.
- `ship_plant_dc[t,p,d,k] >= 0`.
- `ship_dc_customer[t,d,c,k] >= 0`.
- `transfer_dc[t,d1,d2,k] >= 0`.
- `inventory[t,d,k] >= 0`.
- `shortage[t,c,k] >= 0`.
- `emergency_material[t,p,m] >= 0`.

## Objective

Minimize total operating cost:

- setup cost
- regular production cost
- overtime production cost
- emergency material purchase cost
- plant-to-DC transport cost
- DC-to-customer transport cost
- DC handling cost on outbound customer shipments
- DC transfer cost
- inventory holding cost
- shortage penalty

## Constraints

1. Setup activation and run-size logic:
   `min_run * setup <= regular + overtime <= max_run * setup`
2. Weekly plant regular-hour, overtime-hour, and setup-team capacity limits.
3. Weekly material feasibility at each plant-material:
   total BOM usage `<= 0.94 * regular supply + emergency purchase`.
4. Emergency material purchase cap by week, plant, and material.
5. Same-week plant flow conservation:
   total plant-to-DC shipments `=` total plant production.
6. Lane capacity limits for plant-to-DC, DC-to-customer, and DC-transfer arcs.
7. Demand balance for every demand row:
   customer shipments `+ shortage = demand`.
8. Per-row service floor:
   customer shipments `>= min_service_rate * demand`.
9. DC inventory balance each week:
   previous inventory + inbound plant shipments + inbound transfers
   - outbound customer shipments - outbound transfers = ending inventory.
10. DC freezer capacity by week.
11. Inventory floor by week, DC, and product:
    ending inventory `>= safety stock + next-week regional reserve`.

## Solve Configuration

- Solver: PySCIPOpt / SCIP.
- Relative gap target: `0.0005`.
- Default time limit in `solve_initial.py`: `60` seconds, reduced further only if
  `ORCLAW_SOLVE_TIME_LIMIT_SECONDS` is smaller.
- Export precision: quantities written with 8 digits after the decimal point.
- Sparse export: zero-valued rows are omitted.

## Final Incumbent Summary

The exported plan is exactly feasible under the independent audit:

- zero shortage
- zero overtime
- zero emergency material purchases
- zero DC transfers
- final week inventory equal to total required safety stock

The audited incumbent objective is `174947.38211825`, with SCIP best bound
`174266.94553020` and relative gap `0.00390456` at the 60-second limit.
