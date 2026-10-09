# Artifact Provenance

## VERAS harness

The `veras/` directory contains the evaluated v0.30 ORAgentBench controller
and benchmark evidence. The public module organization and API configuration
are a sanitized refactor of the frozen runner's decision flow, not the exact
private gateway setup. Benchmark results and trajectories have not changed.

## Revised real-world results

The later selected candidate sources in
`real_world/{data_center,power_grid}/veras_v030_transfer/algorithm/solve.py`
are copied byte-for-byte from their completed R3 and R1 agent runs; their
hashes and minimal external audit/replay facts are in adjacent
`evidence/selected_result.json`. Operational data and full output artifacts
are omitted. Each isolated controller fork differed from the frozen benchmark
controller by one application-specific performance-prompt paragraph and one
numeric challenger cap, not by an additional agent stage or new observer.
`real_world/CONTROLLER_DELTAS.md` records these differences and budgets.

Original native results and the earlier VERAS case algorithms remain intact
in `real_world/*/{native_codex,veras}/`. The older VERAS held-out and grid
audit reports substantiate the **historical** study only; they must not be
presented as audit evidence for R3 or Grid R1. The native datacenter outcome
is from August 10 whereas R3 is visible August 8--9. The grid protocols and
agent budgets differ. No new paired percentage gain is claimed.
