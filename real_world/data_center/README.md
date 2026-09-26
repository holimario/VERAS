# Data-Center Placement

The task assigns jobs to two regions while respecting four peak resource
limits and minimizing the maximum directional transfer-bandwidth peak. August
8 and 9 were visible for algorithm construction and validation; August 10 was
reserved for the agent-blind replay. Migration volume is an auxiliary metric.

| System | August 10 peak | Migration | Feasible |
| --- | ---: | ---: | --- |
| Native Codex | 1599.050419 Gbps | 33.838 PB | Yes |
| VERAS | **1110.640670 Gbps** | **25.986 PB** | Yes |

VERAS reduces the primary objective by `30.544%`. Its selected phase budget was
`3000/600/3000s` for producer, challenger, and repair. The harness structure
and prompts were fixed; only these phase-time hyperparameters differed.

`veras/evidence/holdout_0810_reproduction.json` is an independent rerun of the
selected algorithm and reproduces the same held-out metric.

The data files are intentionally omitted. The expected input filenames are
listed in `veras/algorithm/manifest.json`.
