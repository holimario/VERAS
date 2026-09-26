# Recycling MRF Stochastic Sorting Plan

## Modeling choice

This instance is best modeled as a **two-stage stochastic mixed-integer assignment/flow problem**.

- First-stage decisions are shared across all scenarios:
  - line opening
  - assigned labor hours
  - assigned quality-control tests
- Second-stage recourse decisions are scenario-specific:
  - batch-to-line processing quantities
  - commodity sales

Because processing must respect a `0.1` ton increment, processing is modeled in integer units of size `delta = 0.1` ton. Sales and labor remain continuous. The deterministic equivalent is small enough to solve directly in SCIP.

## Sets and indices

- `L`: sorting lines, indexed by `l`
- `B`: inbound batches, indexed by `b`
- `S`: scenarios, indexed by `s`
- `C`: commodities, indexed by `c`
- `E subseteq B x L`: eligible batch-line pairs
  - `stream(b)` must be accepted by line `l`
  - if `wet_stream(b)=1`, then `wet_stream_allowed(l)=1`
- `R subseteq E`: risky eligible pairs, where batch `b` is risky
  - `stream(b)="mixed"` or `high_contamination_risk(b)=1`
- `F_fiber subseteq L`: lines with family `fiber`
- `F_plastic subseteq L`: lines with family `plastic`
- `F_container subseteq L`: lines with family `metal`, `glass_metal`, or `mixed`
- `C_pri subseteq C`: commodities with `priority_contract=1`

## Parameters

All numeric parameters come from the public data files.

- `p_s`: probability of scenario `s`
- `cap_l`: nominal tons capacity of line `l`
- `minopen_l`: minimum tons processed on line `l` if opened
- `fix_l`: fixed opening cost of line `l`
- `vcost_l`: variable processing cost per ton on line `l`
- `base_lab_l`: base labor hours for line `l`
- `labpt_l`: labor hours per processed ton on line `l`
- `labmin_l, labmax_l`: labor-hour bounds on line `l`
- `qcmin_l, qcmax_l`: QC-test bounds on line `l`
- `avail_sb`: scenario-available tons of batch `b`
- `share_sbc`: commodity share of commodity `c` in batch `b` under scenario `s`
- `rec_lc`: recovery efficiency of commodity `c` on line `l`
- `recmult_s`: scenario recovery multiplier
- `labmult_s`: scenario labor-drag multiplier
- `demmult_s`: scenario demand multiplier
- `contmult_s`: scenario contamination multiplier
- `price_c`: sale price per ton of commodity `c`
- `floor_c`: contract floor tons of commodity `c`
- `capdem_c`: contract maximum tons of commodity `c`
- `shortpen_c`: shortfall penalty per ton of commodity `c`
- `mpr_b`: must-process rate of batch `b`
- `priority_b`: municipal-priority flag of batch `b`
- `pen_unproc_b`: unprocessed penalty per ton of batch `b`
  - `unprocessed_priority_penalty_per_ton` if `priority_b=1`
  - `unprocessed_regular_penalty_per_ton` otherwise
- `labor_cost`: labor cost per hour
- `qc_cost`: cost per QC test
- `residue_cost`: residue disposal cost per ton
- `tail_pen`: tail penalty on worst priority-contract shortfall
- `H`: total labor-hour budget
- `Q`: total QC-test budget
- `reqfiber, reqplastic, reqcontainer`: required line counts by family group
- `delta = 0.1`: processing ton increment

Derived parameters:

- `Ucap_sl = floor((cap_l / labmult_s) / delta)`: scenario-adjusted line-capacity upper bound in processing units
- `Uavail_sb = avail_sb / delta`: batch availability in processing units
- `Lmust_sb = ceil(mpr_b * Uavail_sb)`: minimum required processed units for batch `b` in scenario `s`
- `Lopen_l = ceil(minopen_l / delta)`: minimum processed units on an open line
- `alpha_sblc = delta * share_sbc * recmult_s * rec_lc`: clean tons of commodity `c` recovered from one processing unit of `(s,b,l)`
- `beta_sbl = sum_{c in C} alpha_sblc`: total clean recovered tons from one processing unit of `(s,b,l)`
- `qcreq_sl = ceil(qcmin_l * contmult_s)`: QC tests required on line `l` in scenario `s` for risky processing

## Decision variables

First stage:

- `y_l in {0,1}`: `1` if line `l` is opened
- `h_l >= 0`: labor hours assigned to line `l`
- `q_l in Z_+`: QC tests assigned to line `l`

Second stage:

- `x_sbl in Z_+`: processing quantity on eligible pair `(b,l)` in scenario `s`, in units of `delta`
- `u_sbl in {0,1}` for `(b,l) in R`: `1` if risky batch `b` is processed on line `l` in scenario `s`
- `sell_sc >= 0`: clean tons of commodity `c` sold in scenario `s`
- `short_sc >= 0`: contract-floor shortfall for commodity `c` in scenario `s`
- `tail >= 0`: worst aggregate priority-contract shortfall across scenarios

## Objective

Maximize expected operating margin:

```text
max
  sum_{s in S} p_s [
      sum_{c in C} price_c * sell_sc
    - sum_{(b,l) in E} vcost_l * delta * x_sbl
    - sum_{c in C} shortpen_c * short_sc
    - sum_{b in B} pen_unproc_b * (avail_sb - delta * sum_{l:(b,l) in E} x_sbl)
    - residue_cost * (delta * sum_{(b,l) in E} x_sbl - sum_{(b,l) in E} beta_sbl * x_sbl)
  ]
  - sum_{l in L} fix_l * y_l
  - sum_{l in L} labor_cost * h_l
  - sum_{l in L} qc_cost * q_l
  - tail_pen * tail
```

## Constraints

### 1. First-stage operating-plan constraints

Line-family coverage:

```text
sum_{l in F_fiber} y_l >= reqfiber
sum_{l in F_plastic} y_l >= reqplastic
sum_{l in F_container} y_l >= reqcontainer
```

Line staffing and QC bounds:

```text
labmin_l * y_l <= h_l <= labmax_l * y_l          for all l in L
qcmin_l  * y_l <= q_l <= qcmax_l  * y_l          for all l in L
```

Facility budgets:

```text
sum_{l in L} h_l <= H
sum_{l in L} q_l <= Q
```

### 2. Scenario line-operation constraints

Scenario-adjusted line capacity:

```text
sum_{b:(b,l) in E} x_sbl <= Ucap_sl * y_l        for all s in S, l in L
```

Minimum tons on an opened line:

```text
sum_{b:(b,l) in E} x_sbl >= Lopen_l * y_l        for all s in S, l in L
```

Labor sufficiency for each scenario:

```text
base_lab_l * y_l + labpt_l * delta * sum_{b:(b,l) in E} x_sbl <= h_l
                                                 for all s in S, l in L
```

### 3. Batch feasibility and commitments

Availability:

```text
sum_{l:(b,l) in E} x_sbl <= Uavail_sb            for all s in S, b in B
```

Must-process commitments:

```text
sum_{l:(b,l) in E} x_sbl >= Lmust_sb             for all s in S, b in B with mpr_b > 0
```

### 4. QC logic for risky processing

Risky-use activation:

```text
x_sbl <= U_sbl * u_sbl                           for all s in S, (b,l) in R
```

QC requirement for risky processing:

```text
q_l >= qcreq_sl * u_sbl                          for all s in S, (b,l) in R
```

where `U_sbl` is the processing-unit upper bound for that arc.

### 5. Recovery and sales constraints

Recovered clean tons by commodity:

```text
sell_sc <= sum_{(b,l) in E} alpha_sblc * x_sbl   for all s in S, c in C
```

Scenario-adjusted sales cap:

```text
sell_sc <= capdem_c * demmult_s                  for all s in S, c in C
```

Contract-floor shortfall definition:

```text
short_sc >= floor_c - sell_sc                    for all s in S, c in C
short_sc >= 0                                    for all s in S, c in C
```

### 6. Tail-risk penalty

Worst priority-contract shortfall:

```text
tail >= sum_{c in C_pri} short_sc                for all s in S
```

## Exported quantities

- Process tons exported to `solution.csv` are `delta * x_sbl`
- `sell_sc`, `h_l`, and all objective calculations remain in ton/hour units

## Public-data assumptions fixed in the implementation

The statement leaves several semantics implicit. The solver fixes them as follows and validates against the exported artifact:

1. **Scenario-adjusted line capacity** uses the labor-drag multiplier:
   - `cap_l / labmult_s`
2. **Must-process commitments** are enforced for every batch with `must_process_rate > 0`, using scenario-specific available tons.
3. **An opened line must process at least `min_open_tons` in every scenario**.
4. **Contract maximum tons** are scenario-adjusted by `demand_multiplier`; **contract floor tons are not scaled**.
5. **Risky processing QC requirement** is `ceil(qcmin_l * contmult_s)` when a batch is mixed or flagged high contamination.
6. **Tail-risk penalty** applies to the worst scenario total shortfall across priority-contract commodities.
7. **Processing residue** equals processed tons minus total recovered clean tons.
