#!/usr/bin/env python3
"""Create the reproducibility manifest after the independent audit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    args = parser.parse_args()
    with (args.candidate_root / "audit_report.json").open("r", encoding="utf-8") as handle:
        audit = json.load(handle)
    with (args.candidate_root / "results" / "solver_summary.json").open("r", encoding="utf-8") as handle:
        summary = json.load(handle)

    input_names = [
        "EXPERIMENT_CONTRACT.md",
        "NEUTRAL_SPEC.md",
        "model_spec.json",
        "OUTPUT_CONTRACT.md",
        "SEARCH_GUIDANCE.md",
        "line_load.xlsx",
    ]
    artifact_names = [
        "README.md",
        "run_candidate.sh",
        "src/solve.py",
        "src/staged_solve.py",
        "src/audit.py",
        "src/finalize.py",
        "results/solution.npz",
        "results/solver_summary.json",
        "audit_report.json",
        "logs/mosek.log",
        "logs/mosek_relax.log",
        "logs/mosek_planning_screen.log",
        "logs/mosek_integer_screen.log",
    ]
    manifest = {
        "schema_version": 1,
        "candidate_id": "mosek_exact_free_planning",
        "same_model_claim": bool(audit.get("audit_passed", False)),
        "audit_passed": bool(audit.get("audit_passed", False)),
        "entry_point": "candidate/run_candidate.sh",
        "solver": summary.get("solver"),
        "objective": audit.get("objective_components", {}).get("total"),
        "status": summary.get("status"),
        "optimizer_time_seconds": summary.get("optimizer_time_seconds"),
        "optimizer_time_limit_seconds": summary.get("optimizer_time_limit_seconds"),
        "threads": summary.get("threads"),
        "planning_was_fixed": summary.get("planning_was_fixed"),
        "used_warm_start": summary.get("used_warm_start"),
        "used_external_warm_start": summary.get("used_external_warm_start"),
        "internal_mip_start_source": summary.get("internal_mip_start_source"),
        "inputs_sha256": {name: sha256(args.input_root / name) for name in input_names},
        "artifacts_sha256": {
            name: sha256(args.candidate_root / name)
            for name in artifact_names
            if (args.candidate_root / name).is_file()
        },
    }
    with (args.candidate_root / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if not manifest["same_model_claim"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
