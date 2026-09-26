You are now at week 2. The environment has been updated with an event notice, revised data, frozen commitment rules, pipeline receipts, and the state produced from the approved initial plan.

Work only inside `/app`. Read `/app/EVENT_NOTICE.md`, `/app/PROBLEM_STATEMENT.md`, `/app/state/week2_state.json`, and all files under `/app/data/`.

Required outputs:

- `/app/submissions/model_step2.md`
- `/app/submissions/solve_step2.py`
- `/app/submissions/step2_replan.csv`
- `/app/submissions/solve_log_step2.md`

Use PySCIPOpt. Replan weeks 2-10 only. This is a recovery plan: start from `/app/state/week2_state.json`, use `/app/submissions/initial_plan.csv` as the approved baseline, preserve frozen commitments where required, include pipeline receipts as already committed inbound orders, and account for replanning costs.

This step has a 20 minute agent budget. First produce a correct feasible replan, then improve operating cost if time remains.


Runtime note from the benchmark harness:
- PySCIPOpt is provided at `/opt/or-deps`; invoke Python as `PYTHONPATH=/opt/or-deps python`.
- Do not search for or depend on conda. Validate the import with
  `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`.


Closed-loop feasibility and performance protocol:
- Required current-stage decision artifact: `/app/submissions/step2_replan.csv`. Earlier-stage files are
  state and frozen commitments, not evidence that this stage is complete.
- For online stages, treat prior accepted artifacts as state: preserve every frozen decision and validate the state transition before changing the suffix.
- Treat the stage as a finite-horizon control problem. Keep a known incumbent as
  the backup state and operate in three modes: RESTORATION for known violations,
  ADVERSARIAL AUDIT for structurally valid but incompletely checked artifacts,
  then PERFORMANCE. Never trade a hard violation for objective value.
- Build the required solver and a complete baseline artifact early. The model
  connection may be interrupted, so checkpoint executable code and incumbents;
  never export variable values after a time limit unless `getNSols()` or
  `getBestSol()` proves that a solution exists.
- Maintain a solver-derived state vector: artifact presence/schema, incumbent
  count, maximum normalized constraint residual, number of violated constraint
  groups, integrality residual, objective/best bound/gap when available, decision
  universe coverage, and elapsed time. A self-written checker is diagnostic, not
  proof; derive residuals independently from the exact exported artifact and
  public data rather than importing the submitted model.
- Use the maximum normalized hard-constraint residual as a Lyapunov-like
  feasibility measure `theta`. Accept restoration iterates only when `theta`
  decreases or a feasible incumbent is reached. If the same method stalls twice,
  change the search operator instead of repeating the same reasoning.
- When the exact model has no incumbent, solve an elastic Phase-I problem first:
  add explicit nonnegative slacks, lexicographically minimize maximum normalized
  violation, then total/count of violations, then distance to the best candidate.
  Keep schema, identifier domains, frozen commitments, and nonanticipativity as
  hard invariants unless the public statement explicitly permits violation.
- Project heuristic or rounded candidates back into the feasible set with a
  repair MIP. First minimize `theta`; after `theta=0`, minimize L1/Hamming distance
  to the candidate. Do not hand-edit coupled rows. For nonlinear continuous
  constraints, an exact L1 penalty or augmented-Lagrangian restoration is valid
  only with explicit residual and multiplier updates.
- Apply robust constraint tightening at numeric boundaries: leave a small
  scale-aware margin below capacities and time limits, round in the conservative
  direction, and recompute units, denominators, temporal propagation, reserves,
  ramps, conservation, activation equivalence, and both Big-M directions.
- Enter PERFORMANCE mode only after exact feasibility. Preserve the feasible
  incumbent while optimizing the real objective. Use solver bounds and gap, not
  confidence, as the stopping signal. A restricted candidate set, greedy subset,
  or decomposed master must be expanded unless there is a valid coverage or bound
  certificate showing that excluded decisions cannot improve the solution.
- Allocate short sequential portfolio slices only after a measurable stall:
  exact MIP with a warm start; feasibility pump/fix-and-propagate; local branching
  or large-neighborhood search around the incumbent; then an alternative exact
  formulation or decomposition if the coupling structure supports it. All slices
  share the incumbent and must return to it on regression.
- Before finishing, rerun the final generator from scratch and audit the exact
  artifact for schema, full key coverage, blank-field rules, domains, capacities,
  conservation, timing, scenario coupling, frozen decisions, and objective terms.
  Where practical, verify that the independent audit rejects a corrupted copy.
