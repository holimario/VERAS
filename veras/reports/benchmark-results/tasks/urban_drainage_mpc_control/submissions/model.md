## Urban Drainage MPC Model

This instance is best modeled as a small extensive-form multi-scenario inventory-flow MIP rather than as a routing, set-partitioning, or time-indexed scheduling model. The key state variable is basin storage over 24 short time slots, and the main coupling comes from delayed transfers, shared treatment and outfall capacities, operating binaries, and a locked nonanticipative prefix. With only 5 scenarios, 14 controls, 12 basins, and 24 slots, the extensive form is compact and gives a strong relaxation because storage balance is written explicitly at every basin, scenario, and time.

### Sets and indices

- `S`: scenarios.
- `T = {0, ..., H-1}`: time slots, with `H = 24`.
- `T_lock = {0, ..., L-1}`: locked nonanticipative prefix, with `L = 4`.
- `B`: basins.
- `C`: controls.
- `C_out(b)`: controls whose source basin is `b`.
- `C_to_basin(b)`: controls whose destination is basin `b`.
- `C_wwtp`: controls whose destination is `WWTP`.
- `C_outfall(o)`: controls whose destination is outfall `o`.
- `G`: control groups.
- `C_g`: controls in group `g`.
- `D`: districts.
- `B_d`: basins in district `d`.

### Parameters

From the public data:

- `p_s`: scenario probability.
- `I_sbt`: external inflow to basin `b` in slot `t`.
- `Cap_b`, `Init_b`, `Target_b`: basin capacity, initial storage, preferred final storage.
- `FloodCost_b`, `PollutionCost_b`, `Combined_b`: overflow damage coefficients.
- `U_c`: control maximum volume per slot.
- `L_c`: minimum positive operating level.
- `Ramp_c`: ramp limit between consecutive slots.
- `Energy_c`, `MoveCost_c`, `StartCost_c`, `StartMax_c`.
- `Delay_c`: transfer delay in slots.
- `Avail_sct`: capacity multiplier from `control_availability.csv`.
- `GroupActive_g`, `GroupStarts_g`: group active and group-start limits.
- `DutyLimit_{g,w}`: rolling volume limit for group `g` over windows of length `w`.
- `WWTP_{st}`: treatment capacity.
- `OutfallCap_{sot}`: outfall release capacity.
- `Tariff_t`: energy tariff.
- `DistrictTarget_d`, `DistrictMult_d`: district service parameters.
- `FFSlots`: number of early first-flush slots.
- `FFPenalty`, `FinalPenalty`, `DistrictPenalty`: objective weights from `config.json`.
- `alpha`: tail probability for CVaR, from `cvar_tail_probability`.
- `lambda`: tail-risk weight, from `tail_risk_weight`.

For controls with `L_c = 0`, the implementation uses a tiny activation threshold `eps_on` only to prevent a binary control from being declared active at exactly zero flow. This is not meant to create real minimum flow.

### Decision variables

For every scenario `s`, control `c`, slot `t`:

- `x_sct >= 0`: moved volume (ML).
- `y_sct in {0,1}`: control active indicator.
- `u_sct in {0,1}`: start indicator.

For every scenario `s`, basin `b`, slot `t`:

- `h_sbt >= 0`: end-of-slot stored volume after overflow capping.
- `o_sbt >= 0`: overflow volume in slot `t`.

Auxiliary variables:

- `f_sb >= 0`: final-storage excess above target at the horizon end.
- `DistrictOverflow_d >= 0`: expected district overflow excess above target.
- `Cost_s`: scenario operating and damage cost before CVaR.
- `eta`, `z_s >= 0`: CVaR linearization variables.

### Storage dynamics

For each scenario `s`, basin `b`, slot `t`, define arrived transfers:

`Arrive_sbt = sum x_sc(t-Delay_c)` over controls `c` ending at basin `b` with `t - Delay_c >= 0`.

Define available water before overflow:

- if `t = 0`: `Avail_sbt = Init_b + I_sbt + Arrive_sbt`
- if `t > 0`: `Avail_sbt = h_sb(t-1) + I_sbt + Arrive_sbt`

Let `Out_sbt = sum x_sct` over `c in C_out(b)`.

The basin balance is:

- `h_sbt + o_sbt = Avail_sbt - Out_sbt`
- `h_sbt <= Cap_b`

Together with `h_sbt >= 0` and `o_sbt >= 0`, this enforces:

- no basin can send more than the water available in that slot;
- storage is capped at basin capacity;
- excess water is recorded as overflow.

If a transfer to another basin would arrive after the horizon, the corresponding `x_sct` is fixed to zero.

### Control operating rules

For every `s,c,t`:

- `x_sct <= U_c * Avail_sct * y_sct`
- `x_sct >= max(L_c, eps_on if L_c = 0 else L_c) * y_sct`

This means unavailable controls cannot move water, positive flow implies activity, and active controls respect any minimum operating level.

Ramping, with pre-horizon flow fixed at zero:

- `x_sc0 <= Ramp_c`
- `x_sct - x_sc(t-1) <= Ramp_c` for `t > 0`
- `x_sc(t-1) - x_sct <= Ramp_c` for `t > 0`

Starts are counted exactly with pre-horizon inactivity:

- `u_sc0 = y_sc0`
- for `t > 0`:
  - `u_sct >= y_sct - y_sc(t-1)`
  - `u_sct <= y_sct`
  - `u_sct <= 1 - y_sc(t-1)`

Per-control start budgets:

- `sum_t u_sct <= StartMax_c`

### Shared resource limits

For each scenario `s`, group `g`, slot `t`:

- `sum_{c in C_g} y_sct <= GroupActive_g`

For each scenario `s`, group `g`:

- `sum_{c in C_g} sum_t u_sct <= GroupStarts_g`

For each rolling-duty record `(g,w,limit)`, every scenario `s`, and every feasible window start `tau`:

- `sum_{c in C_g} sum_{t=tau}^{tau+w-1} x_sct <= limit`

Treatment capacity, for each `s,t`:

- `sum_{c in C_wwtp} x_sct <= WWTP_st`

Outfall release capacity, for each `s,o,t`:

- `sum_{c in C_outfall(o)} x_sct <= OutfallCap_sot`

### Nonanticipativity

The prompt requires identical control movements across all scenarios in the locked prefix. Therefore, for every control `c`, slot `t < L`, and every scenario `s` other than one anchor scenario `s0`:

- `x_sct = x_s0ct`

No extra nonanticipativity is imposed after slot `L-1`.

### Objective

For each scenario, the operating-and-damage cost is modeled as:

- overflow damage:
  - `sum_{b,t} o_sbt * (FloodCost_b + Combined_b * PollutionCost_b)`
- energy cost:
  - `sum_{c,t} x_sct * Energy_c * Tariff_t`
- movement cost:
  - `sum_{c,t} x_sct * MoveCost_c`
- start cost:
  - `sum_{c,t} StartCost_c * u_sct`
- first-flush penalty:
  - `sum_{c,t<FFSlots and c sensitive to outfall} FFPenalty * x_sct`
- final storage penalty:
  - `sum_b FinalPenalty * f_sb`, with `f_sb >= h_sb(H-1) - Target_b`

Let this total be `Cost_s`.

District service is an expected-value term:

- `ExpectedOverflow_d = sum_s p_s * sum_{b in B_d} sum_t o_sbt`
- `DistrictOverflow_d >= ExpectedOverflow_d - DistrictTarget_d`
- district penalty:
  - `sum_d DistrictPenalty * DistrictMult_d * DistrictOverflow_d`

Tail risk is modeled with CVaR of the scenario costs:

- `z_s >= Cost_s - eta`
- `z_s >= 0`
- `CVaR = eta + (1 / alpha) * sum_s p_s * z_s`

The optimized objective is:

- expected scenario cost `sum_s p_s * Cost_s`
- plus district penalty
- plus `lambda * CVaR`

This matches the prompt’s expected-cost-plus-tail-risk structure while keeping the model linear.

### Why this formulation

- Storage balance with explicit delayed arrivals is the dominant structural feature, so a basin-time network formulation is strongest.
- The instance is too small to justify decomposition, but large enough that binary operating logic matters.
- Writing starts, ramping, rolling windows, treatment limits, and outfall caps directly in the extensive form avoids weak heuristic repair loops.
- Because overflow is modeled explicitly, the MIP is always feasible and can focus on minimizing damage rather than on artificial slack penalties.
