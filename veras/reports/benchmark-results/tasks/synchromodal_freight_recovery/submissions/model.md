# Synchromodal Freight Recovery Model

## Modeling choices

The instance is small enough to enumerate feasible shipment paths. I therefore use
a path-based two-stage stochastic mixed-integer model:

- first-stage binary variables choose whether to accept each order and which
  nominal reserved path to book;
- second-stage binary variables choose one recourse path for each accepted
  order in each public scenario;
- paired first-stage/second-stage binaries are used so that the benchmark's
  order-specific "unreserved lane" recovery premium is priced exactly.

This is stronger and simpler than a lane-by-lane flow model because path
feasibility with release times, transfer times, cargo compatibility, transfer
limits, and lateness tolerance is handled during preprocessing.

## Assumptions fixed from the public prompt

The prompt leaves a few costing details implicit. I use the least risky linear
interpretation consistent with the data:

1. The tail-risk term is a scenario-cost CVaR at confidence
   `alpha = tail_risk_alpha`, weighted in the objective by `tail_risk_weight`.
2. A scenario lane used by order `o` but not present in `o`'s reserved path
   pays, per TEU, the sum of:
   `spot_surcharge_per_teu` from `config.json`,
   `unreserved_lane_penalty_per_teu` from `config.json`, and
   the scenario-specific `spot_surcharge_per_teu` from
   `scenario_lane_adjustments.csv` for that lane/scenario pair.
3. Terminal throughput and specialty-resource usage are counted at the origin
   terminal of each used lane, on that lane's actual departure day in the
   relevant plan. This matches "origin loading and every intermediate transfer"
   while excluding the final arrival.
4. The modal-shift share is measured on TEU-lane volume:
   nonroad TEU-legs / total TEU-legs, where nonroad means rail or barge.
5. Missing rows in `scenario_terminal_limits.csv` are treated as nonbinding,
   while missing rows in `terminal_resource_limits.csv` mean no extra specialty
   capacity charge, exactly as stated in the prompt.

## Sets and indices

- `O`: orders.
- `S`: scenarios.
- `L`: scheduled lanes.
- `C`: contracted operator-day pairs from `operator_contracts.csv`.
- `R`: priority classes from `service_requirements.csv`.
- `G`: scenario-terminal-day specialty-resource rows from
  `terminal_resource_limits.csv`.
- For each order `o`, `P_o`: feasible nominal reserved paths.
- For each order `o` and scenario `s`, `Q_os`: feasible recourse paths under
  scenario-adjusted times and lane cancellations.

Each path is a sequence of 1 to `max_path_legs` lanes that:

- starts at the order origin and ends at the order destination,
- departs no earlier than the order ready time,
- respects minimum transfer time at every intermediate terminal,
- uses only lanes and terminals compatible with the order's reefer/hazmat
  requirements,
- uses at most `max_transfers(o)` transfers, and
- arrives no later than `due(o) + max_lateness_minutes`.

## Parameters

### Order and scenario data

- `v_o`: TEU volume of order `o`.
- `penrej_o`: rejection penalty.
- `late_o`: lateness cost per TEU-hour.
- `prio_or`: indicator that order `o` belongs to priority class `r`.
- `prob_s`: probability of scenario `s`.

### Lane and contract data

- `bookcap_l`: booking capacity of lane `l`.
- `scencap_ls`: usable scenario capacity of lane `l` in scenario `s`
  after applying disruption adjustments; cancelled lanes have `scencap_ls = 0`.
- `bookcost_l`: advance reservation cost per TEU on lane `l`.
- `movecost_l`: operating cost per TEU on lane `l`.
- `emit_l`: emissions per TEU on lane `l`.
- `is_nonroad_l`: 1 if lane `l` is rail or barge, else 0.
- `minday_cd`, `maxday_cd`, `actcost_cd`, `shortcost_cd`: minimum booking,
  maximum booking, activation cost, and shortfall cost for contracted
  operator-day pair `c = (operator, day)`.

### Global policy data

- `rho`: minimum nonroad share from `config.json`.
- `Ecap`: scenario emission cap.
- `Epen`: emission penalty per excess kg.
- `alpha`: CVaR confidence level.
- `lambda`: CVaR weight in the objective.
- `spotbase`: global spot surcharge per TEU.
- `unresbase`: global unreserved-lane penalty per TEU.

### Path-derived constants

For every nominal path `p in P_o`:

- `a_opl`: 1 if lane `l` belongs to path `p`, else 0.
- `book_op`: total advance reservation cost of path `p` for order `o`:
  `book_op = v_o * sum_{l in p} bookcost_l`.
- `nr_op`: nonroad TEU-legs on path `p`:
  `nr_op = v_o * sum_{l in p} is_nonroad_l`.
- `totleg_op`: total TEU-legs on path `p`:
  `totleg_op = v_o * |p|`.
- `b_opc`: reserved TEU booked by path `p` on contract pair `c`
  (sum of `v_o` over lanes in `p` whose operator-day equals `c`).

For every recourse path `q in Q_os`:

- `u_osql`: 1 if lane `l` belongs to scenario path `q`, else 0.
- `move_osq = v_o * sum_{l in q} movecost_l`.
- `nr_osq = v_o * sum_{l in q} is_nonroad_l`.
- `totleg_osq = v_o * |q|`.
- `emit_osq = v_o * sum_{l in q} emit_l`.
- `late_osq = v_o * late_o * lateness_hours(osq)`.
- `trans_osq = v_o * sum_{k in transfers(q)} transfer_cost_k`.
- `h_osqtd`: handling TEU contributed by path `q` at terminal-day `(t,d)`;
  this is `v_o` for each lane in `q` whose actual scenario departure is from
  terminal `t` on day `d`, and 0 otherwise.
- `g_osqg`: specialty-resource TEU contributed by path `q` on resource row `g`;
  this equals the same handling TEU if the order requires the resource named by
  row `g`, else 0.

For every order `o`, scenario `s`, nominal path `p`, and recourse path
`q in Q_os`:

- `spot_ospq`: extra recovery premium for using lanes in `q` that are not in
  `p`:

  `spot_ospq = v_o * sum_{l in q \\ p} (spotbase + unresbase + spotscen_ls)`,

  where `spotscen_ls` is the scenario-specific spot surcharge for lane `l` in
  scenario `s`, defaulting to 0 when the row is missing.
- `paircost_ospq = move_osq + trans_osq + late_osq + spot_ospq`.

### Service and terminal limits

- `servmin_r`: minimum accepted TEU for priority class `r`.
- `thru_std`: hard throughput limit from `scenario_terminal_limits.csv` for
  scenario-terminal-day tuple `(s,t,d)`.
- `rescap_g`, `overflow_g`: ordinary specialty capacity and overflow cost on
  resource row `g`.

## Decision variables

- `x_op in {0,1}`: 1 if order `o` chooses nominal reserved path `p`.
- `z_ospq in {0,1}`: 1 if, for scenario `s`, order `o` uses nominal path `p`
  and recourse path `q`.
- `y_cd in {0,1}`: 1 if contract pair `c` is activated.
- `short_cd >= 0`: under-use shortfall TEU for contract pair `c`.
- `overflow_g >= 0`: paid specialty overflow on row `g`.
- `excess_s >= 0`: emission excess in scenario `s`.
- `C_s`: total second-stage cost in scenario `s`.
- `eta`: CVaR threshold variable.
- `xi_s >= 0`: CVaR excess variable for scenario `s`.

Acceptance is implicit:

`acc_o = sum_{p in P_o} x_op`.

Because rejected orders must have no paths, `acc_o` is binary under the path
selection constraints.

## Objective

Minimize

`sum_{o in O} penrej_o * (1 - acc_o)`

`+ sum_{o in O} sum_{p in P_o} book_op * x_op`

`+ sum_{c in C} (actcost_c * y_c + shortcost_c * short_c)`

`+ sum_{s in S} prob_s * C_s`

`+ lambda * (eta + (1 / (1 - alpha)) * sum_{s in S} prob_s * xi_s)`.

## Constraints

### 1. First-stage path choice

At most one nominal path can be reserved for each order:

`sum_{p in P_o} x_op <= 1  for all o in O`.

If an order has no feasible recourse path in some scenario, then all `x_op` are
forced to 0 during preprocessing.

### 2. Scenario recourse choice and first/second-stage consistency

For each order and scenario, the paired variables must reproduce the chosen
nominal path:

`sum_{q in Q_os} z_ospq = x_op  for all o in O, s in S, p in P_o`.

This implies that an accepted order gets exactly one recourse path in every
scenario and a rejected order gets none:

`sum_{p in P_o} sum_{q in Q_os} z_ospq = acc_o  for all o in O, s in S`.

### 3. Reserved lane booking capacities

`sum_{o in O} sum_{p in P_o} v_o * a_opl * x_op <= bookcap_l  for all l in L`.

### 4. Scenario lane capacities

`sum_{o in O} sum_{p in P_o} sum_{q in Q_os} v_o * u_osql * z_ospq`
`<= scencap_ls  for all s in S, l in L`.

### 5. Operator-day reservation contracts

Let reserved booked TEU on contract pair `c` be

`B_c = sum_{o in O} sum_{p in P_o} b_opc * x_op`.

Activation and bounds:

`B_c <= maxday_c * y_c  for all c in C`.

Under-use shortfall:

`short_c >= minday_c * y_c - B_c  for all c in C`.

`short_c >= 0  for all c in C`.

### 6. Scenario terminal throughput

For every scenario-terminal-day tuple with a listed hard throughput limit:

`sum_{o in O} sum_{p in P_o} sum_{q in Q_os} h_osqtd * z_ospq`
`<= thru_std  for all (s,t,d)`.

### 7. Scenario specialty-resource overflow

For each listed specialty row `g = (s,t,d,rtype)`:

`sum_{o in O} sum_{p in P_o} sum_{q in Q_os} g_osqg * z_ospq`
`<= rescap_g + overflow_g`.

### 8. Portfolio service-floor commitments

Accepted TEU must satisfy each priority minimum:

`sum_{o in O} sum_{p in P_o} prio_or * v_o * x_op >= servmin_r  for all r in R`.

### 9. Modal-shift commitments

Reserved plan:

`sum_{o in O} sum_{p in P_o} (nr_op - rho * totleg_op) * x_op >= 0`.

Every scenario plan:

`sum_{o in O} sum_{p in P_o} sum_{q in Q_os}`
`(nr_osq - rho * totleg_osq) * z_ospq >= 0  for all s in S`.

### 10. Scenario emission penalties

For each scenario:

`excess_s >= sum_{o in O} sum_{p in P_o} sum_{q in Q_os} emit_osq * z_ospq - Ecap`.

`excess_s >= 0`.

### 11. Scenario cost definition

For each scenario:

`C_s = sum_{o in O} sum_{p in P_o} sum_{q in Q_os} paircost_ospq * z_ospq`
`+ sum_{g in G_s} overflowcost_g * overflow_g`
`+ Epen * excess_s`.

### 12. CVaR linearization

`xi_s >= C_s - eta  for all s in S`.

`xi_s >= 0  for all s in S`.

## Exported solution

The exported JSON contains one row per order:

- `accept = true` iff `acc_o = 1`,
- `reserved_path` is the ordered lane list of the selected nominal path, or `[]`
  if rejected,
- `scenario_plans` contains one ordered path per scenario, obtained from the
  unique `z_ospq = 1` combination, or `[]` if rejected.

## Why this formulation is efficient for this instance

- Path enumeration removes infeasible lane combinations up front.
- Only 86 nominal paths and 625 scenario paths exist in this instance, so the
  paired path set is still small enough for exact optimization.
- The paired recourse variables eliminate weak big-M linearizations for
  "scenario lane used but not reserved by that order".
- Contract charges, emission penalties, and CVaR all remain linear.
