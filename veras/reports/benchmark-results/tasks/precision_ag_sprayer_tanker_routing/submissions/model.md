# Precision Agriculture Sprayer-Tanker Routing Model

## Modeling choice

This instance is not best handled as a naive monolithic assignment MIP. The tight structure is:

- vehicle-routing with time windows for sprayers,
- inventory/state transitions for sprayer tanks and mix changes,
- synchronized refill rendezvous with tender trucks,
- sparse cross-route coupling through sensitive separations, cluster windows, and pad calendars.

The implemented solver therefore uses a **decomposed route-and-repair model**:

1. Fix a small number of sprayer mix-compatible route segments from the public instance structure.
2. Solve each segment as an exact single-vehicle routing problem with time windows.
3. Solve a continuous repair problem on applied liters so each sprayer segment fits tank limits with the chosen refill pattern.
4. Schedule refill/rinse/tender events exactly and audit all hard constraints on the exported artifact.

The mathematical model below matches that decomposition.

## Sets and indices

- `S`: sprayers.
- `N`: tenders.
- `F`: fields.
- `P`: pads.
- `M`: mixes.
- `W_f`: feasible weather-window alternatives for field `f` after intersecting the field hard window with public weather windows.
- `K_s`: ordered route segments assigned to sprayer `s`. Each segment uses one mix.
- `F_sk`: fields assigned to sprayer `s` in segment `k`.
- `R_s`: refill/rinse events on sprayer `s`.
- `E_n`: tender events on tender `n`.
- `C`: cluster-window rows.
- `H`: sensitive-separation pairs.

## Parameters

- `mix_f`: required mix of field `f`.
- `a_f`: field area.
- `qmin_f, qideal_f, qmax_f`: minimum, ideal, and maximum liters for field `f`.
- `prio_f`: priority weight of field `f`.
- `pref_f`: preferred latest start.
- `open_fw, close_fw`: feasible start interval of weather-window option `w in W_f` after subtracting service duration.
- `dist_{u,v}`: road-factor-adjusted distance between nodes `u,v`.
- `tt^S_{u,v,s}`: sprayer travel time from node `u` to `v` for sprayer `s`.
- `tt^N_{u,v,n}`: tender travel time from node `u` to `v` for tender `n`.
- `cap_s`: sprayer tank capacity.
- `init_s`: sprayer initial liters.
- `initmix_s`: sprayer initial mix.
- `dur_f(q,s)`: spray duration on field `f` with sprayer `s`.
  Implemented as
  `spray_setup_min + max(60 * area_f * terrain_f / base_ha_per_hour_s, q / nozzle_lpm_s)`.
- `syncdur(r,n)`: synchronized refill duration for refill event `r` served by tender `n`.
  Implemented as
  `sync_setup_min + max(refill_r / sprayer_refill_rate, refill_r / tender_refill_rate_n)`.
- `rinse_{m,m'}`: rinse time when changing from mix `m` to `m'`.
  Implemented conservatively as `0` if residue families match, otherwise `max(rinse_m, rinse_m')`.
- `blackout_{p,b}`: blackout interval `b` on pad `p`.
- `gap_h`: minimum start separation for sensitive pair `h in H`.
- `elapsed_c`: cluster completion limit for cluster `c in C`.
- cost coefficients from `config.json`.

## Decision variables

### Segment routing

For each sprayer segment `(s,k)`:

- `x_{i,j}^{sk} in {0,1}`: 1 if field `j` is visited immediately after node `i` in segment `(s,k)`. `i,j` range over assigned fields plus source/sink.
- `t_f`: spray start time for field `f`.
- `z_{f,w} in {0,1}`: 1 if field `f` uses feasible window option `w`.
- `L_f >= 0`: lateness above preferred latest start for field `f`.

### Applied liters and tank repair

- `q_f`: liters applied on field `f`.
- `u_r`: refill liters at sprayer refill event `r`.
- `I_r`: liters in the sprayer tank immediately after event `r`.

### Tender synchronization

- `y_{r,n} in {0,1}`: 1 if tender `n` serves refill event `r`.
- `T_r`: start time of refill event `r`.
- `A^S_r, A^N_r`: arrival times of sprayer and tender at refill event `r`.
- `load_{e}`: liters loaded in tender reload event `e`.
- `J_e`: tender onboard liters after tender event `e`.

## Objective

Minimize

`sprayer travel cost`
`+ tender travel cost`
`+ tender reload fixed cost`
`+ spray setup cost per field`
`+ wait cost at synchronized refills`
`+ sum_f late_penalty_per_priority_min * prio_f * L_f`
`+ sum_f under_ideal_penalty_per_priority_liter * prio_f * (qideal_f - q_f)_+`

with `(qideal_f - q_f)_+ = qideal_f - q_f` because the model enforces `q_f <= qideal_f` in the implemented repair step.

## Constraints

### 1. Exact field coverage

For every field `f`, exactly one sprayer segment contains it, and inside that segment it has exactly one predecessor and one successor:

- `sum_{i} x_{i,f}^{sk} = 1`
- `sum_{j} x_{f,j}^{sk} = 1`

for the unique segment `(s,k)` containing `f`.

### 2. Segment route continuity

For every segment `(s,k)`:

- one arc leaves the source,
- one arc enters the sink,
- MTZ/order constraints eliminate subtours.

### 3. Time propagation along a segment

If `x_{i,j}^{sk} = 1`, then

`t_j >= t_i + dur_i(q_i,s) + tt^S_{i,j,s}`.

For the first visited field of a segment,

`t_j >= segment_start_{sk} + tt^S_{source,j,s}`.

### 4. Hard field time-window choice

Each field chooses exactly one feasible weather-window option:

- `sum_{w in W_f} z_{f,w} = 1`
- `t_f >= open_fw - M * (1 - z_{f,w})`
- `t_f <= close_fw - M * (1 - z_{f,w})`

where `close_fw` is the latest feasible **start** after subtracting service duration.

### 5. Lateness accounting

- `L_f >= t_f - pref_f`
- `L_f >= 0`

### 6. Application-rate bounds

For every field:

- `qmin_f <= q_f <= qideal_f <= qmax_f`

The implementation allows under-ideal application with penalty but never exceeds ideal because doing so only worsens cost and refill feasibility on this instance.

### 7. Sprayer tank conservation

Within each same-mix segment, tank content evolves by subtracting sprayed liters.
At a refill event `r`:

- `0 <= u_r <= cap_s - I_r^-`
- `I_r = I_r^- + u_r`

where `I_r^-` is the tank level just before the refill.

At a rinse before a mix change:

- the tank is emptied,
- the next segment starts with zero liters before its refill.

### 8. Mix compatibility and rinse logic

- Every field served in segment `(s,k)` uses the segment mix.
- The first segment of sprayer `s` starts in `initmix_s`.
- If consecutive sprayer segments use different residue families, a rinse event at a water-capable compatible pad is mandatory before the next refill.

### 9. Refill synchronization

Every refill event must be served by exactly one tender:

- `sum_n y_{r,n} = 1`

and if `y_{r,n} = 1`, then

- same pad,
- same mix,
- same quantity,
- same start and end time on the sprayer and tender routes,
- `T_r >= A^S_r`,
- `T_r >= A^N_r`.

### 10. Tender inventory and reload logic

Tender events are ordered reloads and refill deliveries.

For each tender route:

- event times are nondecreasing,
- travel-time propagation holds between consecutive events,
- after a reload, the tender carries one mix,
- onboard liters decrease by delivered liters,
- delivered liters between reloads cannot exceed loaded liters,
- reload liters cannot exceed tender capacity.

### 11. Vehicle shifts

All sprayer and tender event start/end times must lie within the corresponding shift bounds.

### 12. Sensitive-separation constraints

For each pair `h = (f,g)`:

`|t_f - t_g| >= gap_h`.

The implementation enforces these on the chosen route schedule by delaying one of the two starts when needed.

### 13. Cluster-window constraints

For each cluster `c` with field set `F_c`:

- `alpha_c <= t_f` for all `f in F_c`
- `end_f <= alpha_c + elapsed_c` for all `f in F_c`

where `end_f = t_f + dur_f(q_f,s(f))`.

### 14. Pad blackouts, capacity, and turnover

For every pad service interval `r` at pad `p`:

- it may not overlap any blackout interval on `p`,
- because public pad capacity is 1 for every pad, no two service intervals at `p` may overlap,
- consecutive services at the same pad must be separated by at least `pad_turnover_min`.

## Implemented decomposition for this instance

The public instance structure supports the following route decomposition:

- `SP01`: north `MIX_A` fields, then rinse, then west `MIX_B` fields.
- `SP04`: remaining `MIX_A` fields.
- `SP02`: south `MIX_B` fields, then rinse, then south `MIX_C` fields.
- `SP03`: east `MIX_C` fields.

Given that decomposition, the solver:

1. solves exact route order subproblems for the route segments,
2. repairs liters so each refill pattern is tank-feasible,
3. schedules refill/rinse/tender events,
4. validates every exported event directly from public data.

## Numeric conventions

- SCIP relative gap: `0.0005`.
- If `ORCLAW_SOLVE_TIME_LIMIT_SECONDS` is set, the solver uses `min(user_limit, 300)`.
- Exported times and liters are written with up to `1e-8` precision.
