# Artifact Provenance

## VERAS harness

The `veras/` directory contains the evaluated v0.30 ORAgentBench controller
and benchmark evidence. The public module organization and API configuration
are a sanitized refactor of the frozen runner's decision flow, not the exact
private gateway setup. The benchmark profile keeps its original prompt and
controller budgets. The `datacenter` and `power-grid` profiles select the
verbatim performance text and challenger cap of the respective evaluated
forks while retaining the same guard/observer path. They reject divergent
case settings and hash-check the authorized task and local runner before
execution. Benchmark results and trajectories have not changed.

## Real-world result provenance

The archived datacenter VERAS generator, later native comparator, and fresh
VERAS repeat are byte-for-byte copies of their qualified final sources.
Source/artifact hashes, independent replay results, actual agent wall/tokens,
and 2025-08-10 peaks are recorded in
`real_world/data_center/{veras_archived_aug10,native_aug10_later,veras_aug10_repeat}/`
and `real_world/data_center/august10_comparison.json`. The archived VERAS
controller differs from the frozen version by one performance-prompt paragraph;
its challenger cap remains 180 seconds. Public inputs, evaluator, model,
effort, and allowed agent cap match the later native run, but the comparison
was retrospective, actual inference cost differs, and a fresh VERAS repeat
reverses the ordering. Earlier research had inspected August 10 scores;
August 10 event rows and prior mappings were not supplied to the agents.

A separate fixed-instance grid experiment prospectively ordered a fresh native
Codex arm followed by a fresh VERAS arm, with shared inputs, evaluator,
model/effort, cap, and solver contract. Their generators and independent
audits are under `real_world/power_grid/paired/`. The first VERAS source-only
replay omitted its generated `audit.py` dependency and failed; replay with
both final source files returned the same audited NPZ. The selected lower-cost VERAS candidate under
`real_world/power_grid/veras_separate_audited/` is a different run, not the
paired VERAS artifact. The selected-versus-later-native descriptive comparison
is indexed by `real_world/power_grid/selected_vs_native_reference.json`; its
13.715% difference is not a prospective paired effect. Both grid results are feasible,
time-limited solutions rather than global optimum proofs.

The older `real_world/*/{native_codex,veras}/` directories preserve historical
studies. The datacenter `visible_date_candidate/` candidate was evaluated on
visible August 8--9 only. Operational input datasets and final row-level
Parquet/NPZ outputs are not redistributed. The benchmark public harness
preserves the frozen controller's decision flow but uses a generic model
adapter instead of private API routing. The published case profiles align
controller prompts, budgets, gates, and input/runner hashes, but cannot
reproduce private transport, omitted operational data, or stochastic model
sampling bit-for-bit. No model run was performed merely by adding profiles.
