"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

EPS = 1e-7

def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def optional_csv(path: Path) -> list[dict[str, str]]:
    return read_csv(path) if path.is_file() else []


def as_float(value: str) -> float:
    return float(value or 0.0)


def violation(
    group: str,
    message: str,
    residual: float = 1.0,
    **evidence: Any,
) -> dict[str, Any]:
    return {
        "group": group,
        "message": message,
        "normalized_residual": max(0.0, float(residual)),
        "evidence": evidence,
    }


def normalized_violation_mass(item: dict[str, Any]) -> float:
    """Measure both overload magnitude and how long an interval stays unsafe."""
    residual = max(0.0, float(item.get("normalized_residual", 0.0) or 0.0))
    evidence = item.get("evidence", {})
    if not isinstance(evidence, dict):
        return residual
    try:
        duration = float(evidence["end_min"]) - float(evidence["start_min"])
    except (KeyError, TypeError, ValueError):
        return residual
    return residual * max(0.0, duration)


def report(
    observer: str,
    covered_groups: list[str],
    violations: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
    coverage_complete: bool = False,
) -> dict[str, Any]:
    return {
        "supported": True,
        "observer": observer,
        "coverage_complete": coverage_complete,
        "covered_constraint_groups": covered_groups,
        "safe": not violations,
        "violation_count": len(violations),
        "max_normalized_residual": max(
            (item["normalized_residual"] for item in violations), default=0.0
        ),
        "total_normalized_violation_mass": sum(
            normalized_violation_mass(item) for item in violations
        ),
        "violations": violations,
        "metrics": metrics or {},
    }
