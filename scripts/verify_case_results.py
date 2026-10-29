#!/usr/bin/env python3
"""Check the two released case comparisons against sources and reported values."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METHODS = {"native_codex": "Native Codex", "veras": "VERAS"}


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(case: str, paper: dict, manifest: dict) -> None:
    directory = ROOT / "real_world" / case
    assert {path.name for path in directory.iterdir()} == {
        "README.md", "comparison.json", *METHODS
    }, f"{case}: unexpected case files or method directories"
    comparison = read(directory / "comparison.json")
    assert set(comparison["methods"]) == set(METHODS)
    assert set(manifest["methods"]) == set(METHODS)
    assert manifest["comparison_path"] == f"real_world/{case}/comparison.json"
    published = {row["method"]: row for row in paper["rows"]}

    for arm, method in METHODS.items():
        entry = comparison["methods"][arm]
        assert entry["method"] == method
        assert entry["source_path"] == f"{arm}/algorithm/solve.py"
        assert entry["result_path"] == f"{arm}/evidence/result.json"
        assert {
            path.relative_to(directory / arm).as_posix()
            for path in (directory / arm).rglob("*") if path.is_file()
        } == {"algorithm/solve.py", "evidence/result.json"}, f"{case}/{arm}: unexpected artifacts"

        source = directory / entry["source_path"]
        result = read(directory / entry["result_path"])
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert digest == entry["source_sha256"] == result["source_sha256"]
        assert result["method"] == method
        assert result["model"] == comparison["model"]
        assert result["reasoning_effort"] == comparison["reasoning_effort"]
        ast.parse(source.read_bytes(), filename=str(source))
        assert result["source_replay_byte_identical_to_agent_artifact"] is True

        listing = manifest["methods"][arm]
        assert listing["source_path"] == f"real_world/{case}/{entry['source_path']}"
        assert listing["result_path"] == f"real_world/{case}/{entry['result_path']}"
        assert listing["source_sha256"] == digest
        assert listing["metrics"] == entry["metrics"]
        assert published[method]["source"] == listing["source_path"]

        if case == "data_center":
            assert result["evaluation_date"] == comparison["evaluation_date"] == "2025-08-10"
            assert result["strict_resource_feasible"] is True
            peak = result["august10_peak_gbps"]
            migration = result["migration_bytes_auxiliary"] / 1e15
            assert entry["metrics"] == {"peak_gbps": peak, "migration_pb": migration}
            assert f"{peak:.3f}" == f"{published[method]['peak_gbps']:.3f}"
            assert f"{migration:.3f}" == f"{published[method]['migration_pb']:.3f}"
        else:
            assert result["constraints_feasible"] is True
            assert result["independent_audit_status"] == "independently_audited"
            assert result["solver_threads"] == comparison["solver_threads"] == 4
            assert result["agent_solver_limit_seconds"] == comparison["agent_solver_ceiling_seconds"] == 900
            assert result["replay_solver_limit_seconds"] == 750
            if "integrality_exact" in result:
                assert result["integrality_exact"] is True
            if "integrality_residual" in result:
                assert result["integrality_residual"] == 0
            for key, published_key in [
                ("annualized_objective_usd", "total_million_usd_per_year"),
                ("annualized_investment_usd", "investment_million_usd_per_year"),
                ("annualized_operation_usd", "operation_million_usd_per_year"),
            ]:
                value = result[key]
                assert entry["metrics"][key] == value
                assert f"{value / 1e6:.3f}" == f"{published[method][published_key]:.3f}"
            assert math.isclose(
                result["annualized_objective_usd"],
                result["annualized_investment_usd"] + result["annualized_operation_usd"],
                abs_tol=0.01,
            )

    key = "peak_gbps" if case == "data_center" else "annualized_objective_usd"
    baseline = comparison["methods"]["native_codex"]["metrics"][key]
    selected = comparison["methods"]["veras"]["metrics"][key]
    assert math.isclose(comparison["absolute_reduction"], baseline - selected)
    assert math.isclose(comparison["relative_reduction_percent"], (baseline - selected) / baseline * 100)
    reported = paper["peak_reduction_percent"] if case == "data_center" else paper["native_codex_cost_reduction_percent"]
    assert comparison["reported_reduction_percent"] == manifest["reported_reduction_percent"] == reported
    print(f"{case}: one Native Codex/VERAS comparison; source hashes and reported metrics verified")


def main() -> None:
    paper = read(ROOT / "veras/evidence/paper-results.json")
    manifest = read(ROOT / "ARTIFACT_MANIFEST.json")["real_world"]
    assert set(manifest) == {"data_center", "power_grid"}
    verify("data_center", paper["datacenter"], manifest["data_center"])
    verify("power_grid", paper["power_grid"], manifest["power_grid"])


if __name__ == "__main__":
    main()
