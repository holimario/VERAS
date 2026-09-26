# Steel SCC Ladle-Energy Reschedule

## Decision structure

The public requirement makes the problem a two-stage robust schedule:

- First-stage commitments must be identical in every scenario:
  - machine choice for every `(heat, stage)`
  - ladle choice for every `heat`
- Second-stage recourse may vary by scenario:
  - start time of every `(heat, stage)`
  - therefore all bucketed resource loads and all scenario cost terms

I used a decomposition that keeps the first-stage commitments fixed and solves
each scenario timing problem exactly with SCIP after those commitments are
chosen.

## Sets

- `H`: heats
- `C`: cast strings
- `S`: scenarios
- `K_h`: ordered route stages of heat `h`
- `M_{h,k}`: eligible machines for heat `h` at stage `k`
- `L_h`: eligible ladles for heat `h`
- `B`: 15-minute time buckets

Auxiliary order sets used by the implemented decomposition:

- `O^mach_{k,m}`: ordered heats assigned to machine `m` at stage `k`
- `O^ladle_l`: ordered heats assigned to ladle `l`

## Parameters

From the data files:

- `p_{h,k}^s`: scenario duration of heat `h` at stage `k`
- `r_h^s`: scenario release time for heat `h`
- `due_h`, `prio_h`
- `grade_h`, `width_h`, `weight_h`, `cast(h)`, `caster_h`
- `tf_{k,k'}`: minimum transfer time between consecutive stages
- `setup_{k,g,g'}`: sequence-dependent setup time on a machine
- `ready_m`: first-ready time of machine `m`
- `ready_l`, `clean_l`, maintenance window of ladle `l`
- `max_gap_c`, `target_gap_c` for each cast string
- bucket capacities:
  - `cranecap_b`
  - `alloycap_b`
  - `labcap_b`
  - `preheatcap_b`
- stage-start bucket requirements:
  - `crane_{k}`
  - `alloy_{k,g}`
  - `lab_{k,g}`
- penalty coefficients from `config.json`

## Implemented first-stage construction

I do not solve the full integrated nonanticipative MIP directly. Instead:

1. Fix a compact casting plan with zero intra-string gap and early cast-string
   anchors that keep the day inside the horizon.
2. Build a base-scenario backward schedule stage by stage.
   For each stage, heats are placed as late as possible before their downstream
   deadlines on eligible machines, including sequence-dependent setups.
3. Assign ladles greedily to the resulting base intervals, respecting ladle
   family, compatibility, capacity, cleanout, ready time, and maintenance.
4. Freeze the resulting machine assignment, ladle assignment, machine order, and
   ladle order across all scenarios.

This produces the published first-stage commitments.

## Scenario timing MIP

For each scenario `s`, solve the following MIP.

### Variables

- `t_{h,k}^s in Z_+`: start minute of heat `h` at stage `k`
- `T_h^s >= 0`: tardiness of heat `h`
- `W_h^s >= 0`: temperature-loss proxy for excess final waiting
- `G_{h,h'}^s >= 0`: excess cast gap above target for consecutive heats in the
  same cast string
- `z_{h,k,b}^s in {0,1}` for stages with bucketed start resources:
  `1` if operation `(h,k)` starts in bucket `b`

### Objective

For scenario `s`, minimize

`sum_h tardiness_penalty * prio_h * T_h^s`

`+ sum_h temperature_penalty * W_h^s`

`+ sum_(h,h' consecutive in same cast) caster_break_penalty * G_{h,h'}^s`

`+ epsilon * sum_h (t_{h,CC}^s - t_{h,BOF}^s)`

The small final term is a holding-time regularizer that keeps BOF starts as
late as possible, which reduces unnecessary ladle occupancy and idle waiting.

The reported score outside the MIP is recomputed from the exported artifact and
also includes electricity, oxygen shortage/venting, and ladle preheat costs.

### Constraints

Route precedence for each consecutive stage pair `(k, k')` of heat `h`:

`t_{h,k'}^s >= t_{h,k}^s + p_{h,k}^s + tf_{k,k'}`

Release and machine readiness:

- `t_{h,BOF}^s >= r_h^s`
- `t_{h,k}^s >= ready_{machine(h,k)}`

Fixed machine order on each committed machine:

For consecutive heats `i, j` in `O^mach_{k,m}`,

`t_{j,k}^s >= t_{i,k}^s + p_{i,k}^s + setup_{k,grade_i,grade_j}`

Cast-string continuity:

- first heat of each cast may not start before the committed cast anchor
- for consecutive heats `i, j` in the same cast string:
  - `t_{j,CC}^s >= t_{i,CC}^s + p_{i,CC}^s`
  - `t_{j,CC}^s <= t_{i,CC}^s + p_{i,CC}^s + max_gap_{cast(i)}`
  - `G_{i,j}^s >= t_{j,CC}^s - (t_{i,CC}^s + p_{i,CC}^s + target_gap_{cast(i)})`

Fixed ladle order:

For consecutive heats `i, j` on the same committed ladle,

`t_{j,BOF}^s >= t_{i,CC}^s + p_{i,CC}^s + cleanout`

Final waiting rule:

If `f(h)` is the final refining stage of heat `h`,

`t_{h,CC}^s - (t_{h,f(h)}^s + p_{h,f(h)}^s) <= max_wait_to_cast`

Tardiness definition:

`T_h^s >= t_{h,CC}^s + p_{h,CC}^s - due_h`

Temperature-loss definition:

Let `allow_h = min_superheat_h / temp_drop_h`. Then

`W_h^s >= temp_drop_h * (t_{h,CC}^s - (t_{h,f(h)}^s + p_{h,f(h)}^s + tf_{f(h),CC}) - allow_h)`

Bucket-start linking:

- `sum_b z_{h,k,b}^s = 1`
- if `z_{h,k,b}^s = 1`, then `t_{h,k}^s` lies in bucket `b`

Bucket capacities for each bucket `b`:

- `sum_(h,k) crane_k * z_{h,k,b}^s <= cranecap_b`
- `sum_(h,k) alloy_{k,grade_h} * z_{h,k,b}^s <= alloycap_b`
- `sum_(h,k) lab_{k,grade_h} * z_{h,k,b}^s <= labcap_b`

## Validation and score recomputation

After solving, I independently recompute from the exported schedule:

- route order and transfer feasibility
- machine eligibility and machine non-overlap with setups
- cast sequence, caster continuity, and maximum break
- ladle compatibility, capacity, maintenance, cleanout, and non-overlap
- crane, alloy-crew, laboratory, and ladle-preheat bucket loads
- tardiness, temperature-loss, cast-gap penalties
- electricity cost
- oxygen shortage and venting penalties
- total scenario-weighted public-data score

## Modeling assumptions

- `start_min` is enforced as an integer minute.
- Intra-string cast gaps are allowed to be zero. The statement gives only a
  maximum break as a hard rule; the target gap is treated as a soft target.
- BOF oxygen mode is set to `normal` for all heats. The public files provide a
  cutback penalty but do not provide quantitative cutback duration/oxygen
  multipliers, so using cutback would require inventing missing physics.
