# Revised case-study controller deltas

The two revised results were generated with isolated forks of the frozen
ORAgentBench v0.30 controller. The open-source `veras/` module layout implements
the same decision flow but is not a byte-identical copy of the private gateway
runner. Neither fork introduced another agent stage, changed the observer or
gates, or modified the original benchmark release. Both used `gpt-5.6-sol`
with `xhigh` reasoning. These runs used task-specific performance text and
more agent compute than the historical native rows; they are not controlled
paired tests of the harness effect.

## Data-center R3

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

## Power-grid R1

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

The later grid R1 qualification replay used a stricter *invocation* setting of
750 seconds for the solver and returned the same final NPZ in 829.552 seconds
end-to-end. This does not turn the original agent run into a 750-second run.
The earlier historical native Codex candidate values remain unchanged in the
repository, but their prompts and budgets were different.
