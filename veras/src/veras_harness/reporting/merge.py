"""Merge separately executed benchmark splits into one evidence record."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def aggregate_scores(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = sorted(tasks, key=lambda row: (row["band"], row["task"]))

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(rows)
        return {
            "task_count": count,
            "completed": sum(bool(row["complete"]) for row in rows),
            "feasible": sum(bool(row["feasible"]) for row in rows),
            "passed": sum(bool(row["passed"]) for row in rows),
            "pass_rate": sum(bool(row["passed"]) for row in rows) / count,
            "feasibility_rate": sum(bool(row["feasible"]) for row in rows) / count,
            "mean_normalized_quality": sum(float(row["normalized_quality"]) for row in rows) / count,
        }

    result = summary(tasks)
    result["tasks"] = tasks
    result["by_band"] = {
        band: summary([row for row in tasks if row["band"] == band])
        for band in sorted({row["band"] for row in tasks})
    }
    return result


def aggregate_components(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = sorted(tasks, key=lambda row: (row["band"], row["task"]))
    totals: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    for row in tasks:
        totals.update(row.get("counts", {}))
        actions.update(row.get("actions", {}))
        modes.update(row.get("modes", {}))

    selectors = {
        "challenger": lambda row: row.get("counts", {}).get("challenger_attempts", 0),
        "recovery": lambda row: row.get("counts", {}).get("recovery_attempts", 0),
        "performance": lambda row: row.get("counts", {}).get("performance_attempts", 0),
        "local_solver": lambda row: row.get("counts", {}).get("local_solver_used", 0),
        "rollback": lambda row: row.get("counts", {}).get("rollback_count", 0),
        "structural_restoration": lambda row: any(a.get("structural_restoration") for a in row.get("audits", [])),
        "semantic_restoration": lambda row: any(a.get("semantic_restoration") for a in row.get("audits", [])),
    }
    by_mechanism = {}
    for name, selected_by in selectors.items():
        selected = [row for row in tasks if selected_by(row)]
        count = len(selected)
        by_mechanism[name] = {
            "tasks": count,
            "completed": sum(bool(row["complete"]) for row in selected),
            "feasible": sum(bool(row["feasible"]) for row in selected),
            "passed": sum(bool(row["passed"]) for row in selected),
            "mean_normalized_quality": (
                sum(float(row["normalized_quality"]) for row in selected) / count
                if count
                else 0.0
            ),
            "task_names": [row["task"] for row in selected],
        }

    audits = [audit for row in tasks for audit in row.get("audits", [])]
    return {
        "schema_version": 1,
        "scope": {
            "tasks": len(tasks),
            "bands": dict(Counter(row["band"] for row in tasks)),
            "evidence_type": "observational execution evidence, not factorial ablation",
        },
        "component_totals": dict(totals),
        "decision_actions": dict(actions),
        "decision_modes": dict(modes),
        "audit_transitions": {
            "stages": len(audits),
            "event_streams": sum(int(row.get("event_streams", 0)) for row in audits),
            "challenger_reports": sum(bool(row.get("challenger_report_present")) for row in audits),
            "structural_restorations": sum(bool(row.get("structural_restoration")) for row in audits),
            "semantic_restorations": sum(bool(row.get("semantic_restoration")) for row in audits),
            "semantic_residual_reductions": sum(bool(row.get("semantic_residual_reduced")) for row in audits),
        },
        "by_mechanism": by_mechanism,
        "tasks": tasks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scores", type=Path, nargs="+", required=True)
    parser.add_argument("--components", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    scores = aggregate_scores([task for path in args.scores for task in read(path)["tasks"]])
    components = aggregate_components([task for path in args.components for task in read(path)["tasks"]])
    (args.output / "official-score-summary.json").write_text(
        json.dumps(scores, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "component-evidence-summary.json").write_text(
        json.dumps(components, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
