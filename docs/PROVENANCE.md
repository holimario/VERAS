# Artifact Provenance

## VERAS harness

The `veras/` directory contains the evaluated VERAS harness used for the
reported results. The verifier runs 32 deterministic tests when ORAgentBench is
present. The public artifact contains the evaluation configuration and does not
include unrelated development tooling.

## Real-world artifacts

The algorithm files are copied from the selected completed runs. Generated
solutions that encode operational rows are omitted.

The power-grid VERAS result additionally includes an independent exact replay
audit. The data-center VERAS result includes both the held-out evaluation and a
separate reproduction record.
