You are solving an operations-research benchmark task inside a sandbox.

Work only inside `/app`. Do not use the internet, hidden evaluator files, reference models, package registries, or remote data.

Required workflow:
1. Read `/app/PROBLEM_STATEMENT.md`, and every file under `/app/data/`.
2. Write a complete mathematical model to `/app/submissions/model.md` before finalizing code.
3. Implement the solver in `/app/submissions/solve.py` using PySCIPOpt.
4. Use SCIP relative gap `0.0005`. If `ORCLAW_SOLVE_TIME_LIMIT_SECONDS` is set, use a time limit no larger than that value. Preserve solution values with precision up to `1e-8` after the decimal point.
5. Make `/app/submissions/solve.py` solve from scratch when run as `python /app/submissions/solve.py`.
6. Write the required solution file under `/app/submissions/` using the task schema.
7. Write `/app/submissions/solve_log.md` with commands, solver status, objective or score if available, and validation checks.

Note:
You may improve the modeling and solving strategy based on preliminary results, but each individual solve attempt is limited to 5 minutes. The whole workflow is also time-limited, so do not perform unlimited full solve-and-iterate cycles.

Before running expensive solves, reason about the problem structure and choose an efficient approach. You may:
1. Use stronger mathematical formulations and more sophisticated modeling strategies tailored to the structure, special properties, and scale of the given problem instance.
2. Solve directly with SCIP when the model is tractable.
3. Design effective heuristic, local-search, repair, or rounding methods when exact optimization is too slow.
4. Combine heuristics with SCIP, for example by generating an initial feasible solution, fixing or relaxing selected variables, using warm starts if supported, or solving restricted subproblems.
5. Run short diagnostic solves to estimate difficulty, then refine the model or solution method.

Do not default to a naive formulation. Before coding, explicitly analyze whether the problem is better represented as an assignment model, network flow model, set partitioning model, time-indexed model, interval/order-based scheduling model, routing model, or decomposition-based model and so on. Choose the formulation that is likely to give the strongest relaxation and fastest solution for the given instance size. Use preprocessing, dominance filtering, bound tightening, valid inequalities, symmetry breaking, and heuristic warm starts whenever applicable.

Use the limited iteration budget wisely:
- First ensure feasibility and correct output schema.
- Then improve objective quality.
- Prefer targeted refinements over repeated full solves.
- Stop iterating when additional improvements are unlikely within the remaining time.
- Record every meaningful solve attempt and validation result in `/app/submissions/solve_log.md`.

The final submission should prioritize correctness, feasibility, and robust execution from scratch under the time limit.


Runtime note from the benchmark harness:
- PySCIPOpt is provided at `/opt/or-deps`; invoke Python as `PYTHONPATH=/opt/or-deps python`.
- Do not search for or depend on conda. Validate the import with
  `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`.


Closed-loop feasibility and performance protocol:
- Required current-stage decision artifact: `/app/submissions/solution.json`. Earlier-stage files are
  state and frozen commitments, not evidence that this stage is complete.
- For scenario models, keep first-stage decisions literally identical across scenarios and require complete feasible recourse in every public scenario; averaging violations across scenarios is invalid.
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
