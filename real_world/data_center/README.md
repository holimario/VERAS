# Data-Center Placement

The revised manuscript reports **VERAS R3** in `veras_v030_transfer/`: a
**1,072.233445 Gbps** maximum directional peak on the visible August 8--9
traces, independently resource-feasible, 29 Huailai tasks, with a source-only
21.154-second clean replay. The 0.050-Gbps soft near-tie rule also admits
21-task candidates with a 1,072.234840-Gbps peak. R3 does **not** have a new
August 10 or sealed-date evaluation.

The original comparison remains archived without changing native Codex:

| Historical system | August 10 peak | Migration | Feasible |
| --- | ---: | ---: | --- |
| Native Codex (`native_codex/`) | 1,599.050419 Gbps | 33.838 PB | Yes |
| Earlier VERAS (`veras/`) | 1,110.640670 Gbps | 25.986 PB | Yes |

These historical August 10 numbers are **not paired** with the revised R3
August 8--9 peak. The old `veras/evidence/holdout_0810_reproduction.json`
verifies the earlier algorithm, not R3. No cross-window percentage reduction
is claimed. The operational inputs and generated row-level mappings are omitted.
