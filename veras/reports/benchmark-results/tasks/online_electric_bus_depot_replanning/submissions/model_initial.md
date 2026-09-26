# Initial Model: Visible Trips at Minute 0

`/app/AGENT_BRIEF.md` is not present in the workspace, so this model uses the public rules in `/app/PROBLEM_STATEMENT.md`, `/app/EVENT_NOTICE.md`, and the visible files under `/app/data/`.

## Visible Decision Scope

- Time horizon: the currently visible full day, with `current_min = 0`.
- Trips: every row in `data/trips.csv` with `reveal_step <= visible_step`.
- Buses: all rows in `data/buses.csv`.
- Chargers: all rows in `data/chargers.csv`.
- Current packet facts:
  - no active charger outage,
  - no active bus derating,
  - no event-specific charger session restriction,
  - no frozen prior commitments.

## Stage Strategy

The solver uses a hybrid two-stage method.

1. Build a constructive incumbent from the published `block_hint` values.
2. Audit the exported artifact exactly against the public hard rules.
3. If the incumbent is infeasible, invoke a SCIP restricted repair MIP with a warm start.
4. If the incumbent is feasible and has no cancellations and no charging rows, stop in performance mode because every public objective term is nonnegative and the resulting objective is exactly `0`.

For this initial packet the constructive incumbent is already feasible:

- each visible trip is assigned to its hinted bus,
- no charging sessions are needed,
- all trip start-SOC and terminal SOC checks pass,
- deadhead timing between consecutive hinted trips passes,
- four unused buses remain at the depot all day, so each reserve checkpoint is satisfied.

Because the plan has no canceled trips and no charging rows, every public objective component is zero:

- cancellation penalties: `0`,
- electricity purchases: `0`,
- carbon cost: `0`,
- battery-stress cost: `0`,
- change penalties: `0` at the initial step.

That gives a certificate of optimality without expanding the charging MIP.

## Constructive Incumbent

For each visible trip `t`, let `hint(t)` be its `block_hint` bus. The incumbent sets:

- `operate(t) = 1`,
- `assign(hint(t), t) = 1`,
- no `charge` rows.

The audit then verifies:

- exact trip-row coverage,
- trip rows leave `item_id` blank and charge rows use unique nonblank `item_id` values,
- bus-class and low-floor compatibility,
- chronological feasibility from the depot to the first trip and between consecutive trips using `deadhead_times.csv`,
- battery state propagation:
  - initial SOC from `buses.csv`,
  - trip energy from `trips.csv`,
  - start reserve `min_start_soc_kwh`,
  - lower bound `min_soc_kwh`,
  - upper bound `max_soc_kwh`,
- reserve checkpoints from `reserve_requirements.csv`,
- CSV nullability rules from the problem statement.

## Restricted Repair MIP

The fallback MIP is only used if the constructive incumbent fails.

### Decision variables

- `x[b,t] in {0,1}`: bus `b` operates trip `t`.
- `z[t] in {0,1}`: trip `t` is canceled.

### Objective

Minimize

`sum_t cancel_penalty[t] * z[t]`

This is sufficient for restoration because all charging-related cost terms are nonnegative and the fallback MIP is entered only to repair hard-rule violations in the zero-charge incumbent.

### Constraints

- Trip coverage:
  - `sum_b x[b,t] + z[t] = 1` for every visible trip `t`.
- Critical service:
  - `z[t] = 0` for every critical trip.
- Bus-trip compatibility:
  - `x[b,t] = 0` if bus class or low-floor capability is insufficient.
  - `artic` buses may cover `standard` trips; `standard` buses may not cover `artic` trips.
- Depot reachability for the first assigned trip:
  - disallow `x[b,t] = 1` if `deadhead(DEPOT, start(t)) > depart(t)`.
- Pairwise time-feasibility:
  - for any pair with `depart(i) < depart(j)` and
    `arrive(i) + deadhead(end(i), start(j)) > depart(j)`,
    enforce `x[b,i] + x[b,j] <= 1`.
- Start-SOC reserve:
  - if bus `b` operates trip `t`, then
    `initial_soc[b] - sum_{k: depart(k) < depart(t)} energy[k] * x[b,k] >= min_start_soc[t]`.
- End-of-horizon minimum SOC:
  - `initial_soc[b] - sum_t energy[t] * x[b,t] >= min_soc[b]`.

The repair MIP is intentionally restricted. It is a restoration operator, not the primary optimizer for this packet.

## Independent Audit and `theta`

The script computes a hard-constraint residual summary directly from the exported CSV, not from solver variables. The main state vector includes:

- artifact presence and schema status,
- incumbent count,
- maximum normalized hard-constraint residual `theta`,
- number of violated hard-rule groups,
- integrality residual,
- decision-universe coverage,
- objective / best bound / relative gap when SCIP is used,
- elapsed time.

`theta = 0` is required before entering performance mode.
