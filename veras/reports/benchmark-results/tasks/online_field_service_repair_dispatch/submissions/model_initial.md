# Initial Wave-1 Dispatch Model

The model covers only the currently visible wave-1 tickets. In this instance there are no prior-wave frozen commitments, no active replenishment rows, no access windows, and no crew staging caps.

## Sets

- `T`: technicians.
- `J`: visible tickets.
- `O_j`: feasible crew options for ticket `j`. Each option fixes:
  - the primary technician,
  - the optional assistant technician,
  - the repair duration implied by the primary technician's skill level.

## Main parameters

- Travel time `tau[a,b]` from `travel_times.csv`.
- Earliest start `r_j = max(release_j, current_time)`.
- Due minute `due_j` and hard deadline `ddl_j`.
- Ticket deferral penalty and lateness penalty.
- Technician shift start/end, overtime limit, wage, overtime rate, home node, stock, skills, bucket-truck flag, and seniority.
- Region service-floor requirement `min_service_r = ceil(rate_r * visible_P1P2_r)`.

## Decision variables

- `u[j,o] in {0,1}`: crew option `o` is chosen for ticket `j`.
- `d[j] in {0,1}`: ticket `j` is deferred.
- `s[j] in Z_+`: start minute of ticket `j`.
- `late[j] >= 0`: lateness after the SLA due minute.
- `z[t,j] in {0,1}`: technician `t` participates on ticket `j`.
- `use[t] in {0,1}`: technician `t` is used.
- `x[t,i,k] in {0,1}`: on technician `t`'s route, task `k` follows `i`, where `i` and `k` are either visible tickets or that technician's home node.
- `route_end[t] >= 0`: return-home time for technician `t`.
- `ot[t] >= 0`: overtime minutes for technician `t`.

## Objective

Minimize

- travel labor cost,
- service labor cost,
- overtime premium,
- SLA lateness cost,
- deferral penalties.

Wave 1 has no appointment-change penalties because there is no earlier accepted plan to preserve.

The implemented cost assumption is:

- regular travel and service minutes are charged at `wage_per_min`,
- overtime minutes after shift end are charged with an added premium of `overtime_cost_per_min - wage_per_min`,
- ticket lateness is `max(0, finish - due_j)`,
- deferred tickets pay their published deferral penalty.

## Hard constraints

1. Exactly one decision per ticket: either one feasible crew option is scheduled or the ticket is deferred.
2. Every `P1` ticket must be scheduled.
3. Start times respect release and current replanning time.
4. Scheduled tickets finish by their hard deadline.
5. Lateness is measured only for scheduled tickets.
6. Each region meets its minimum service floor over visible `P1` and `P2` tickets.
7. Crew eligibility:
   - the primary technician has the required primary skill,
   - one-person tickets have no assistant,
   - two-person tickets use a distinct assistant,
   - a bucket requirement can be satisfied by either crew member,
   - a secondary-skill requirement can be satisfied by either crew member,
   - high-risk two-person work includes at least one technician from the highest seniority tier.
8. Primary-van stock is respected for every part type, including currently visible replenishments.
9. For each technician route:
   - if the technician is used, exactly one route leaves home and one route returns home,
   - every participating job has exactly one predecessor and one successor on that technician's route,
   - time propagation enforces non-overlap and travel continuity,
   - the route returns home within shift end plus overtime limit.
10. Overtime minutes are at least `route_end[t] - shift_end[t]`.
11. If a visible predecessor ticket exists, the successor cannot be scheduled unless the predecessor is scheduled, and it must start at least 15 minutes after predecessor completion.
12. If access windows are present, a scheduled ticket must choose one valid window and start inside it.

## Solution method

1. Reparse any existing plan artifact from disk and audit it directly against the public data before optimization; that audited plan is the rollback incumbent.
2. If needed, build a feasible greedy incumbent with route-aware append decisions and an independent audit.
3. Improve the incumbent with randomized large-neighborhood style route construction and a bounded SCIP MIP warm-started from the incumbent and the best repaired candidate.
4. Export a changed artifact only if its independently recomputed objective is strictly better and it still passes a fresh disk-based audit over:
   - schema and ticket coverage,
   - crew validity,
   - release/deadline/precedence,
   - technician route continuity and overtime,
   - stock limits,
   - service floors,
   - optional access-window and staging checks.

## Modeling assumptions called out explicitly

- Parts are consumed from the primary technician's van, matching the public rule text.
- For two-technician jobs, either crew member may satisfy bucket-truck and secondary-skill requirements.
- Travel and service time are both paid labor.
- No later repair waves are assumed or used.
