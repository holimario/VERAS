# Robust Kidney Exchange Clearing Model

## Formulation Choice

This instance is best modeled as a **set-packing model over enumerated exchange structures with day assignment**, not as an arc-flow model.

- Cycles are limited to length 2 or 3.
- Altruistic chains are limited to 1 to 3 patient-donor pairs.
- The hard constraints act on whole structures assigned to days: pair disjointness, altruist usage, daily hospital resources, team calendars, lane calendars, service floors, and regional balance.
- With 88 patient-donor pairs, 11 altruists, and 1,908 arcs, explicit enumeration of feasible short structures is tractable and yields a much tighter relaxation than a loose link-based formulation.

## Public-Data Assumptions

The public brief leaves a few score details implicit. The solver uses the following explicit assumptions and applies them consistently in optimization and validation.

1. `planning_days.regional_slot_factor` scales the nominal daily retrieval and transplant capacities in `hospitals.csv`, and also scales the regional immunology-lab capacity obtained by summing hospital lab capacities within each region. Effective integer capacity is `floor(base_capacity * regional_slot_factor)`.
2. `chain_max_length = 3` means an altruistic chain may contain at most 3 patient-donor pairs, matching the operational rule text even though the schema example shows a longer sample sequence.
3. A chain with pair sequence `(p1, p2, p3)` uses links
   - altruist -> `p1` on the start day,
   - `p1` donor -> `p2` on the start day,
   - `p2` donor -> `p3` on the following day.
   The terminal pair in a chain receives but does not donate.
4. Scenario field `long_courier_additive` applies to links with `travel_hours > 2.5`, because the public data provide travel duration but no explicit long-courier threshold.
5. `cold_ischemia_risk_multiplier` multiplies failure probability on cross-region links scheduled on that day.
6. `region_policy.credit_weight` multiplies the clinical value of a transplant received in that region.
7. `coordination_cost`, blood-type fields, dialysis time, and waiting-round counts are not used directly because the public statement does not provide a rule that links them to feasibility or score. Compatibility is already encoded by `donor_edges.csv`, and clinical priority is already reflected by `transplant_value` plus the explicit hard-service rules.

## Sets and Indices

- `P`: patient-donor pairs.
- `A`: altruistic donors.
- `H`: hospitals.
- `R`: regions.
- `D = {1, ..., T}`: planning days in day order.
- `S`: stress scenarios, with distinguished scenario `BASELINE`.
- `C`: feasible directed cycles of length 2 or 3.
- `K`: feasible directed altruistic chains of length 1 to 3 pairs.

For each cycle `c in C`, let:

- `P(c)`: pairs contained in cycle `c`.
- `L(c)`: directed links in cycle `c`.

For each chain `k in K`, let:

- `a(k) in A`: its altruistic donor.
- `P(k)`: ordered patient-pair sequence.
- `L(k)`: ordered directed links `(0,1), (1,2), ...` where link 1 is altruist -> first pair.

## Parameters

From the public data:

- `capRet_h`: nominal donor-retrieval capacity per day of hospital `h`.
- `capTx_h`: nominal recipient-transplant capacity per day of hospital `h`.
- `capLab_h`: nominal immunology-lab capacity per day of hospital `h`.
- `capSenior_hd`, `capPed_hd`: daily senior-team and pediatric-team capacities.
- `laneCap_rr'd`: daily cross-region shipment capacity from region `r` to region `r'`, `r != r'`.
- `laneRisk_rr'd`: cross-region cold-ischemia multiplier for that lane/day.
- `slotFac_d`: `regional_slot_factor` for day `d`.
- `minTx_r`: minimum transplant floor for region `r`.
- `maxExport_r`: maximum net export deficit for region `r`.
- `credit_r`: regional transplant credit weight for region `r`.
- `maxTravel_p`: recipient pair `p` maximum accepted travel time.
- `cpra_p`, `ped_p`, `priority_p`, `region_p`, `hosp_p`: pair attributes.
- `avail_a`: first available day of altruist `a`.
- `bonus_a`: chain-start bonus of altruist `a`.
- `travel_l`, `xmatch_l`, `fail_l`, `value_l`, `same_l`: edge attributes.
- `prob_s`, `multFail_s`, `addLong_s`, `multValue_s`: scenario parameters.
- Global constants from `config.json`:
  - `LmaxCycle = 3`
  - `LmaxChain = 3`
  - `succLimit = 18`
  - `minHS = 7`
  - `minPed = 6`
  - `maxChains = 7`
  - `gap = 0.0005`
  - `thetaHS = 85`
  - `thetaWeak = 68`
  - `travelCost = 5.5`
  - `cyclePenalty = 85`
  - `chainPenalty = 38`
  - `twoCycleBonus = 18`
  - `tailWeight = 0.34`

Derived parameters:

- Arc feasibility filter:
  A public edge may be used only if `travel_l <= maxTravel_recipient(l)` and, if `late_courier_required = 1`, the donor hospital can ship late.
- Successor pruning:
  for each donor, keep only the best `succLimit` recipients by a robust single-link score. This limits enumeration while preserving high-quality candidates.
- Effective capacities:
  - `effRet_hd = floor(capRet_h * slotFac_d)`
  - `effTx_hd = floor(capTx_h * slotFac_d)`
  - `effLab_rd = floor((sum_{h in region r} capLab_h) * slotFac_d)`
- Team requirement indicators for any transplant into pair `p` via link `l`:
  - `needSenior_lp = 1` if `priority_p = critical` or `cpra_p >= thetaHS` or `xmatch_l < thetaWeak`
  - `needPed_p = 1` if `ped_p = 1`
- Cross-region indicator `cross_l = 1 - same_l`
- Long-courier indicator `long_l = 1` if `travel_l > 2.5`, else `0`

## Scenario-Specific Link Values

For a link `l` performed on day `d` in scenario `s`:

- Lane multiplier
  - `laneMult_ld = laneRisk_rr'd` if `cross_l = 1` and the link ships from region `r` to `r'`
  - `laneMult_ld = 1` otherwise
- Scenario-adjusted failure probability

```text
f_{l d s} = min(
    0.999999,
    fail_l * multFail_s * laneMult_ld + addLong_s * long_l
)
```

- Success probability `q_{l d s} = 1 - f_{l d s}`
- Scenario-adjusted transplant value

```text
v_{l d s} = value_l * multValue_s * credit_{recipient_region(l)}
```

- Courier burden term `t_l = travelCost * travel_l`

## Structure-Day Scores

The model uses a precomputed constant score for every feasible cycle-day and chain-start-day candidate.

### Cycle `c` on day `d`

All cycle surgeries happen on the same day, and the cycle succeeds only if every link succeeds:

```text
Q_{c d s} = product_{l in L(c)} q_{l d s}
```

Scenario score:

```text
ScoreCycle_{c d s}
  = Q_{c d s} * sum_{l in L(c)} v_{l d s}
    - sum_{l in L(c)} t_l
    - cyclePenalty * (1 - Q_{c d s})
    + twoCycleBonus * 1[|P(c)| = 2]
```

### Chain `k` starting on day `d`

For a chain with ordered links `l_1, ..., l_m`, let `day(l_j, d)` be the surgery day of link `j` when the chain starts on day `d`.

The `j`th transplant occurs only if the first `j` links succeed:

```text
Prefix_{k d s j} = product_{u = 1..j} q_{l_u, day(l_u,d), s}
```

Scenario score:

```text
ScoreChain_{k d s}
  = sum_{j = 1..m} Prefix_{k d s j} * v_{l_j, day(l_j,d), s}
    - sum_{j = 1..m} t_{l_j}
    - chainPenalty * sum_{j = 1..m-1} (Prefix_{k d s j} - Prefix_{k d s, j+1})
    + bonus_{a(k)}
```

The break penalty applies only to failures after at least one successful transplant, which matches the operational meaning of a broken altruistic chain.

## Feasible Day Sets

- `DayC(c)`: all planning days.
- `DayK(k)`: start days `d` such that
  - `d >= avail_{a(k)}`
  - every required surgery day of the chain exists in the planning horizon.

## Decision Variables

- `x_{c d} in {0,1}`: 1 if cycle `c` is selected and assigned to day `d`.
- `y_{k d} in {0,1}`: 1 if chain `k` is selected and starts on day `d`.
- `z in R`: lower bound on stressed-scenario performance.

## Objective

Let `Sstress = S \ {BASELINE}`.

Expected score:

```text
ExpScore
  = sum_{s in S} prob_s * (
        sum_{c in C} sum_{d in DayC(c)} ScoreCycle_{c d s} * x_{c d}
      + sum_{k in K} sum_{d in DayK(k)} ScoreChain_{k d s} * y_{k d}
    )
```

Tail-risk floor:

```text
z <= sum_{c in C} sum_{d in DayC(c)} ScoreCycle_{c d s} * x_{c d}
   + sum_{k in K} sum_{d in DayK(k)} ScoreChain_{k d s} * y_{k d}
   for all s in Sstress
```

Maximize:

```text
(1 - tailWeight) * ExpScore + tailWeight * z
```

This is a linear robust objective after candidate scores are precomputed.

## Constraints

### 1. Pair disjointness

Each patient-donor pair can appear in at most one selected structure:

```text
sum_{c: p in P(c)} sum_{d in DayC(c)} x_{c d}
+ sum_{k: p in P(k)} sum_{d in DayK(k)} y_{k d}
<= 1
for all p in P
```

### 2. Altruist usage

Each altruistic donor starts at most one chain:

```text
sum_{k: a(k) = a} sum_{d in DayK(k)} y_{k d} <= 1
for all a in A
```

### 3. Active-chain limit

```text
sum_{k in K} sum_{d in DayK(k)} y_{k d} <= maxChains
```

### 4. Daily donor-retrieval capacity

For each hospital `h` and day `d`, total retrievals scheduled that day cannot exceed `effRet_hd`.

```text
sum_{c in C} retUse_{c h d} * x_{c d}
+ sum_{k in K} sum_{s in DayK(k)} retUse_{k h d s} * y_{k s}
<= effRet_{h d}
```

where `retUse` is a precomputed count of links on that day whose donor hospital is `h`.

### 5. Daily recipient-transplant capacity

For each hospital `h` and day `d`:

```text
sum_{c in C} txUse_{c h d} * x_{c d}
+ sum_{k in K} sum_{s in DayK(k)} txUse_{k h d s} * y_{k s}
<= effTx_{h d}
```

### 6. Regional immunology-lab capacity

Weak-crossmatch transplants consume one regional lab slot on the transplant day:

```text
sum_{c in C} labUse_{c r d} * x_{c d}
+ sum_{k in K} sum_{s in DayK(k)} labUse_{k r d s} * y_{k s}
<= effLab_{r d}
for all r in R, d in D
```

### 7. Senior-team capacity

Critical, highly sensitized, or weak-crossmatch transplants consume one senior-team slot:

```text
sum_{c in C} seniorUse_{c h d} * x_{c d}
+ sum_{k in K} sum_{s in DayK(k)} seniorUse_{k h d s} * y_{k s}
<= capSenior_{h d}
for all h in H, d in D
```

### 8. Pediatric-team capacity

```text
sum_{c in C} pedUse_{c h d} * x_{c d}
+ sum_{k in K} sum_{s in DayK(k)} pedUse_{k h d s} * y_{k s}
<= capPed_{h d}
for all h in H, d in D
```

### 9. Cross-region courier-lane capacity

Each cross-region link consumes one lane slot on its surgery day:

```text
sum_{c in C} laneUse_{c r r' d} * x_{c d}
+ sum_{k in K} sum_{s in DayK(k)} laneUse_{k r r' d s} * y_{k s}
<= laneCap_{r r' d}
for all r != r', d in D
```

### 10. Highly sensitized service floor

```text
sum_{c in C} hsCount_c * sum_{d in DayC(c)} x_{c d}
+ sum_{k in K} hsCount_k * sum_{d in DayK(k)} y_{k d}
>= minHS
```

### 11. Pediatric service floor

```text
sum_{c in C} pedCount_c * sum_{d in DayC(c)} x_{c d}
+ sum_{k in K} pedCount_k * sum_{d in DayK(k)} y_{k d}
>= minPed
```

### 12. Regional transplant floors

```text
sum_{c in C} regTx_{c r} * sum_{d in DayC(c)} x_{c d}
+ sum_{k in K} regTx_{k r} * sum_{d in DayK(k)} y_{k d}
>= minTx_r
for all r in R
```

### 13. Regional net-export balance

Let `export_{structure,r}` be the number of cross-region links whose donor region is `r`, and `import_{structure,r}` the number of cross-region links whose recipient region is `r`.

```text
sum_{c in C} (export_{c r} - import_{c r}) * sum_{d in DayC(c)} x_{c d}
+ sum_{k in K} (export_{k r} - import_{k r}) * sum_{d in DayK(k)} y_{k d}
<= maxExport_r
for all r in R
```

This enforces that no region exports substantially more cross-region donor opportunities than it receives as cross-region patient transplants.

## Output Construction

From any feasible incumbent:

- Export one JSON object with keys `cycles` and `chains`.
- For each selected cycle variable `x_{c d} = 1`, write:
  - a stable `cycle_id`
  - `day_id = d`
  - `pair_sequence` equal to the directed cycle order
- For each selected chain variable `y_{k d} = 1`, write:
  - a stable `chain_id`
  - `start_day_id = d`
  - `altruistic_donor_id = a(k)`
  - `pair_sequence` equal to the ordered patient sequence

## Validation

The solver independently re-audits the exported `solution.json` against:

- schema and identifier domains
- pair disjointness
- altruist uniqueness
- day feasibility
- hospital retrieval and transplant capacities
- regional immunology-lab capacities
- hospital senior/pediatric team capacities
- courier-lane capacities
- highly sensitized and pediatric floors
- regional transplant floors
- regional net-export limits

Only a solver-backed incumbent is exported.
