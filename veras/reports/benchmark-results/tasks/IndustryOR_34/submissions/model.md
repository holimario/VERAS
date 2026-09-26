# Mathematical Model

## Formulation Choice

This instance is best modeled as a direct integrated mixed-integer assignment and
scheduling model:

- `load` decisions assign integer SKU quantities to specific containers.
- `use` decisions activate containers and enforce fixed-charge, minimum-load, and
  rule activation logic.
- `schedule` decisions assign each used container to exactly one feasible loading
  slot.

A network-flow or decomposition model would add complexity without improving the
LP relaxation for this instance size. The strongest practical formulation here is
a sparse container-SKU assignment model coupled directly to slot assignment and
resource constraints, with eligibility preprocessing and tight quantity bounds.

## Sets and Indices

- `C`: candidate containers, index `c`
- `S`: SKUs, index `s`
- `L`: loading slots, index `l`
- `P`: loading periods, index `p`
- `K ⊆ C × S`: compatible container-SKU pairs after applying general rules and
  explicit overrides
- `E_c ⊆ L`: feasible loading slots for container `c` after enforcing customs
  window, accepted container type, departure-wave deadline, dangerous-goods
  capability, and reefer-plug capability
- `S_f ⊆ S`: SKUs in family `f`
- `C_q ⊆ C`: containers in customs window `q`
- `C_w ⊆ C`: containers on departure wave `w`
- `C_a ⊆ C`: containers on lane `a`
- `L_p ⊆ L`: slots in period `p`

## Parameters

From the data:

- `d_s`: required units of SKU `s`
- `wt_s`: unit weight of SKU `s` in tons
- `pal_s`: pallet positions per unit of SKU `s`
- `hc_s`: handling cost per unit of SKU `s`
- `Wmax_c`, `Wmin_c`: maximum and minimum loaded weight of container `c`
- `Pmax_c`: pallet capacity of container `c`
- `fc_c`: fixed cost of using container `c`
- `slotcost_l`: cost of loading slot `l`
- `slotcap_l`: maximum loaded weight allowed in slot `l`
- `sec_c`: security tier of container `c`
- `U_cs`: tight upper bound on units of SKU `s` that can fit in container `c`
  for `(c,s) ∈ K`, computed as the minimum of:
  - total required units `d_s`
  - container weight fit `floor(Wmax_c / wt_s)`
  - container pallet fit `floor(Pmax_c / pal_s)`
  - SKU split-rule maximum per container, if present
- `r_{t,u}`: companion ratio for trigger SKU `t` and companion SKU `u`
- `m_{s,typ}`: minimum units of SKU `s` required in each used container of the
  specified type (or all types)
- `alpha_{f,typ}`: maximum family weight share for family `f` in a used container
  of the specified type (or all types)
- `splitmax_s`: maximum units of SKU `s` allowed in one container, when present
- `splitmin_s`: minimum number of distinct containers carrying SKU `s`, when
  present
- `lane_min_{s,a}`, `lane_max_{s,a}`: lane commitments
- `wave_min_{s,w}`, `wave_max_{s,w}`: wave commitments
- `docratio_{a,s,u}`: minimum units of documentation SKU `u` required on lane `a`
  per unit of controlled SKU `s`
- `win_cont_q`, `win_wt_q`: customs-window limits on used containers and loaded
  weight
- `res^cont_p`, `res^fork_p`, `res^insp_p`, `res^dg_p`, `res^plug_p`,
  `res^wt_p`: period resource capacities
- `fork_c`, `insp_c`, `dg_c`, `plug_c`: handling resource requirements implied by
  container `c`'s type

## Decision Variables

- `x_cs ∈ Z_+` for `(c,s) ∈ K`: units of SKU `s` loaded into container `c`
- `b_cs ∈ {0,1}` for `(c,s) ∈ K`: 1 if container `c` carries SKU `s`
- `y_c ∈ {0,1}` for `c ∈ C`: 1 if container `c` is used
- `z_cl ∈ {0,1}` for `l ∈ E_c`: 1 if used container `c` is assigned to slot `l`
- `w_c ≥ 0`: total loaded weight in container `c`
- `g_cl ≥ 0` for `l ∈ E_c`: loaded weight of container `c` handled in slot `l`
  (used to linearize slot and period weight constraints)

## Objective

Minimize total fixed container cost, SKU handling cost, and loading-slot cost:

```text
min  Σ_{c∈C} fc_c y_c
   + Σ_{(c,s)∈K} hc_s x_cs
   + Σ_{c∈C} Σ_{l∈E_c} slotcost_l z_cl
```

## Constraints

### 1. Demand satisfaction

Every required SKU unit is loaded exactly once:

```text
Σ_{c:(c,s)∈K} x_cs = d_s                              ∀ s ∈ S
```

### 2. Presence and container activation links

```text
x_cs ≤ U_cs b_cs                                      ∀ (c,s) ∈ K
x_cs ≥ b_cs                                           ∀ (c,s) ∈ K
b_cs ≤ y_c                                            ∀ (c,s) ∈ K
```

These constraints ensure a positive load implies both SKU presence and container
usage, while `b_cs = 1` means at least one unit is loaded.

### 3. Container weight and pallet feasibility

```text
w_c = Σ_{s:(c,s)∈K} wt_s x_cs                         ∀ c ∈ C
Wmin_c y_c ≤ w_c ≤ Wmax_c y_c                         ∀ c ∈ C
Σ_{s:(c,s)∈K} pal_s x_cs ≤ Pmax_c y_c                 ∀ c ∈ C
```

### 4. Minimum required SKUs in used containers

For each minimum-SKU rule affecting container `c`:

```text
x_cs ≥ m_{s,typ(c)} y_c
```

### 5. Companion-cargo rules

For each companion rule `(t,u,r)` and each container where both pairs exist:

```text
x_cu ≥ r x_ct
```

### 6. Pairwise co-loading conflicts

For each conflicting pair `(s,u)` and each container with both pairs compatible:

```text
b_cs + b_cu ≤ 1
```

### 7. Wave commitments

```text
Σ_{c∈C_w:(c,s)∈K} x_cs ≥ wave_min_{s,w}               when defined
Σ_{c∈C_w:(c,s)∈K} x_cs ≤ wave_max_{s,w}               when defined
```

### 8. Lane commitments

```text
Σ_{c∈C_a:(c,s)∈K} x_cs ≥ lane_min_{s,a}               when defined
Σ_{c∈C_a:(c,s)∈K} x_cs ≤ lane_max_{s,a}               when defined
```

### 9. Family weight-share limits

For each rule `(f, alpha, typ)` affecting container `c`:

```text
Σ_{s∈S_f:(c,s)∈K} wt_s x_cs ≤ alpha_{f,typ(c)} w_c
```

### 10. SKU split rules

For each SKU with a split rule:

```text
x_cs ≤ splitmax_s y_c                                 ∀ (c,s) ∈ K
Σ_{c:(c,s)∈K} b_cs ≥ splitmin_s
```

### 11. Lane documentation rules

For each documentation rule `(a, controlled, doc, ratio)`:

```text
Σ_{c∈C_a:(c,doc)∈K} x_c,doc
  ≥ ratio · Σ_{c∈C_a:(c,controlled)∈K} x_c,controlled
```

### 12. Customs-window capacity

```text
Σ_{c∈C_q} y_c ≤ win_cont_q                            ∀ customs windows q
Σ_{c∈C_q} w_c ≤ win_wt_q                              ∀ customs windows q
```

### 13. Slot assignment

Each used container is scheduled exactly once in a feasible slot:

```text
Σ_{l∈E_c} z_cl = y_c                                  ∀ c ∈ C
Σ_{c:l∈E_c} z_cl ≤ 1                                  ∀ l ∈ L
```

Only feasible slots are created in `E_c`, so customs window, slot type,
dangerous-goods approval, reefer-plug availability, and wave deadline are
enforced structurally.

### 14. Slot and period loaded-weight linearization

The variable `g_cl` equals the container's loaded weight if slot `l` is chosen,
and zero otherwise:

```text
Σ_{l∈E_c} g_cl = w_c                                  ∀ c ∈ C
g_cl ≤ slotcap_l z_cl                                 ∀ c ∈ C, l ∈ E_c
g_cl ≤ w_c                                            ∀ c ∈ C, l ∈ E_c
g_cl ≥ w_c - Wmax_c (1 - z_cl)                        ∀ c ∈ C, l ∈ E_c
```

This also enforces per-slot loaded-weight limits.

### 15. Period resource capacities

For each loading period `p`:

```text
Σ_{c∈C} Σ_{l∈E_c∩L_p} z_cl ≤ res^cont_p
Σ_{c∈C} fork_c Σ_{l∈E_c∩L_p} z_cl ≤ res^fork_p
Σ_{c∈C} insp_c Σ_{l∈E_c∩L_p} z_cl ≤ res^insp_p
Σ_{c∈C} dg_c   Σ_{l∈E_c∩L_p} z_cl ≤ res^dg_p
Σ_{c∈C} plug_c Σ_{l∈E_c∩L_p} z_cl ≤ res^plug_p
Σ_{c∈C} Σ_{l∈E_c∩L_p} g_cl ≤ res^wt_p
```

## Notes on Strengthening

- Compatibility preprocessing removes all impossible `(c,s)` and `(c,l)` pairs.
- The upper bounds `U_cs` are calibrated from demand, weight, pallet, and split
  rules, which is stronger than a generic big-M.
- Slot feasibility is enforced by variable creation rather than by weak logical
  constraints.
- The `g_cl` linearization keeps schedule-dependent weight accounting exact for
  both slot limits and shared period weight capacity.
