# Revised case-study controller deltas

The application-specific VERAS runs use isolated forks of the frozen
ORAgentBench v0.30 controller. The public `veras/` module layout implements
the same decision flow, but does not include private gateway configuration.
The controller forks change performance text and, where stated, one challenger
budget; they do not add a new agent stage or alter the benchmark release.
The fixed-instance grid study also includes a fresh native Codex arm under
the same allowed task cap. See each comparison record for matching details;
actual wall time and token counts need not be equal.

## Data-center visible-date development candidate

- Frozen controller SHA-256: `47e8a7a68f59f558daf254299b208041c3bd15a60d53543ebb42c211d9edfc69`.
- Isolated fork SHA-256: `d2b76e9f1792dd593ec9e2c6e13cef7717b9904b054eb589a9d1027c86a541c8`.
- Numeric challenger cap: `180 -> 900` seconds; `timeout_scale=3.0`, `reserve_fraction=0.45`.
- The following paragraph was appended only to the existing performance prompt:

> For this two-day data-center placement task, a visible peak at a structural
> floor is not evidence of temporal generalization. Use the available agent wall
> time to compare several *public-data-only* assignment families, including
> capacity-repair starts and table-writer-group/peak-time-influence neighborhoods
> derived from the visible August 8/9 transfers. Recompute the complete two-day
> transfer-interval sweep and all four resource time sweeps for each serious
> candidate; do not optimize a single timestamp. Preserve the task's ordering:
> feasibility, minimum worst-day directional peak, then its stated near-tie
> intervention regularizer and visible mean/spread/slack. Explicitly examine the
> Pareto tradeoff between writer-induced transfer interactions, worst visible
> resource slack, and intervention count among candidates with near-identical
> visible peak. Never use August 10, previous assignments, or any reference
> solution. Before finishing, run the exact final generator from public inputs
> with no prior Parquet or embedded mapping, measure <=120 seconds, and replay
> its exported artifact rather than trusting an in-memory surrogate.

## Power-grid paired and separate VERAS settings

- Frozen controller SHA-256: `47e8a7a68f59f558daf254299b208041c3bd15a60d53543ebb42c211d9edfc69`.
- Isolated fork SHA-256: `809bf5629dc1bb578dffbbea4db888ec1d0742b0a32fed74a6a4a4d07fc061d4`.
- Numeric challenger cap: `180 -> 480` seconds; `timeout_scale=2.0`, `reserve_fraction=0.40`.
- The following paragraph was appended only to the existing performance prompt:

> For this power-grid task, use the available agent wall time for substantive guarded
> performance work instead of ending at the first feasible incumbent when a material
> public-objective gap remains. Compare genuinely different investment/topology
> neighborhoods across `uL`, `uNS`, `uEX`, `zESS`, `zPVG`, and `zWTG` by their complete
> independently recomputed objective. A fixed-planning screen is only a diagnostic
> internal start; acceptance requires an unrestricted full free-planning solve.
> The larger agent wall budget does NOT relax the separate <=900-second total solver
> sequence (including presolve) and <=4-thread contract for a single reproducible
> from-scratch generator. Before finalizing, run the exact final `solve.py` from a
> fresh directory containing only public workbook/spec and source, no previous NPZ
> or development checkpoint; time the solver sequence and independently audit the
> newly generated artifact. If this clean invocation fails, fix reproducibility
> within the remaining wall time or clearly state that a checkpoint-only score is
> not an eligible final result. Do not import any outside solution or private data.

The separate lower-cost grid qualification replay used a stricter *invocation* setting of
750 seconds for the solver and returned the same final NPZ in 829.552 seconds
end-to-end. This does not turn the original agent run into a 750-second run.
The earlier historical native Codex candidate values remain unchanged in the
repository, but their prompts and budgets were different.

## Data-center archival and fresh-repeat setting

- Frozen controller SHA-256: `47e8a7a68f59f558daf254299b208041c3bd15a60d53543ebb42c211d9edfc69`.
- Isolated same-prompt fork SHA-256: `2bb70f5d7fca26c17e7fb13614e7d77dce96ff3c54991f5bd2eef13a7c908360`.
- The only controller edit is one appended performance-prompt paragraph; no
  control-flow, observer, gate, or numeric change. Released challenger cap:
  180 seconds. `timeout_scale=3.0`, `reserve_fraction=0.40`, and configured
  agent cap 19,800 seconds. The later native arm uses the unmodified runner.
- The added paragraph directs public-input-only sparse capacity repair and
  reversible writer/task-neighborhood search, prioritizes the nonbinding
  visible day's peak and two-day mean once near the public worst-day floor,
  and requires strict resource slack plus a from-scratch <=120-second output
  replay. It forbids August 10 rows, prior mappings, and reference solutions.

The archived VERAS and later native arm are a retrospective configuration match,
not a fresh jointly preregistered pair. A fresh VERAS same-prompt repeat is
also included and performs worse than native on August 10. See
`data_center/august10_comparison.json` for all three outcomes.

The paired grid VERAS arm uses the same performance-text controller and
480-second challenger setting as the separate lower-cost grid run. In the
paired study, both agent arms share the public input, original evaluator,
`gpt-5.6-sol`/`xhigh`, a 15,360-second allowed agent cap, a 900-second solver
contract, and at most four threads; actual wall time differs. The native
runner has no VERAS controller prompts. Source-only replays use a separate
750-second solver setting. See `power_grid/paired/comparison.json`.
