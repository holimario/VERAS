# Artifact Provenance

## VERAS harness

The `veras/` directory contains the evaluated v0.30 ORAgentBench controller
and benchmark evidence. The public module organization and API configuration
are a sanitized refactor of the frozen runner's decision flow, not the exact
private gateway setup. Benchmark results and trajectories have not changed.

## Real-world result provenance

The datacenter R11 sources, later native comparator, and fresh VERAS replicate
are byte-for-byte copies of their respective qualified final generators. Their
SHA-256 hashes, independent replay results, actual agent wall/tokens, and
2025-08-10 peak scores are recorded separately in
`real_world/data_center/*r11*/evidence/selected_result.json` and jointly in
`real_world/data_center/august10_r11_comparison.json`. The archived R11 changes
one performance-prompt paragraph from the frozen controller and retains the
180-second challenger cap; the new native arm uses its unmodified runner.
Public inputs, evaluator, model, effort, and allowed cap match, but archived
R11 and native were not simultaneously preregistered and actual inference
compute differs. The prospectively launched VERAS replicate is unfavorable
and remains in this artifact. Previous research had inspected August 10
scores, although no August 10 event rows or historical mappings were supplied
to these agent runs.

Grid R1 retains its independently audited objective and byte-identical
source-only replay in `real_world/power_grid/veras_v030_transfer/`. The
750-second replay is a different invocation from the original 900-second
agent setting. The older native grid candidate used different prompts and
compute; no matched native-relative Grid R1 gain is claimed.

The older `real_world/*/{native_codex,veras}/` artifacts remain historical.
Datacenter R3 under `veras_v030_transfer/` uses visible August 8--9 only.
Operational input datasets and final row-level Parquet/NPZ outputs are not
redistributed. The benchmark public harness code preserves the frozen
controller's decision flow but replaces private API routing with a generic
adapter; it does not claim to reproduce model sampling bit-for-bit.
