#!/usr/bin/env python3
"""Build a direct-evidence ledger for VERAS controller components."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


from ..constants import benchmark_root


OR_BENCH = benchmark_root()
DIFFICULTY = json.loads((OR_BENCH / "difficulty.json").read_text())["tasks"]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def score_by_task(output: Path) -> dict[str, dict[str, Any]]:
    summary = read_json(output / "official-score-summary.json")
    return {
        str(row["task"]): row
        for row in summary.get("tasks", [])
        if isinstance(row, dict) and "task" in row
    }


def stage_audit(stage: Path) -> dict[str, Any]:
    public_initial = read_json(stage / "public-audit.initial.json")
    public_final = read_json(stage / "public-audit.final.json")
    semantic_initial = read_json(stage / "semantic-observation.initial.json")
    semantic_final = read_json(stage / "semantic-observation.final.json")
    initial_residual = float(semantic_initial.get("max_normalized_residual", 0) or 0)
    final_residual = float(semantic_final.get("max_normalized_residual", 0) or 0)
    return {
        "stage": stage.name,
        "event_streams": len(list(stage.glob("*.events.jsonl"))),
        "challenger_report_present": (stage / "challenger-report.json").is_file(),
        "structural_restoration": (
            bool(public_initial)
            and not bool(public_initial.get("primary_target_valid"))
            and bool(public_final.get("primary_target_valid"))
        ),
        "semantic_restoration": (
            bool(semantic_initial.get("supported"))
            and not bool(semantic_initial.get("safe"))
            and bool(semantic_final.get("safe"))
        ),
        "semantic_residual_reduced": (
            bool(semantic_initial.get("supported"))
            and final_residual < initial_residual
        ),
        "initial_max_residual": initial_residual,
        "final_max_residual": final_residual,
        "initial_violation_count": int(
            semantic_initial.get("violation_count", 0) or 0
        ),
        "final_violation_count": int(semantic_final.get("violation_count", 0) or 0),
    }


def summarize(output: Path) -> dict[str, Any]:
    scores = score_by_task(output)
    records = []
    actions = Counter()
    modes = Counter()
    totals = Counter()
    mechanism_tasks: dict[str, set[str]] = {
        name: set()
        for name in (
            "challenger",
            "recovery",
            "performance",
            "local_solver",
            "rollback",
            "structural_restoration",
            "semantic_restoration",
        )
    }
    for task_dir in sorted((output / "codex").glob("*")):
        if not task_dir.is_dir():
            continue
        task = task_dir.name
        status = read_json(task_dir / "status.json")
        task_counts = Counter()
        task_actions = Counter()
        task_modes = Counter()
        audits = [
            stage_audit(stage)
            for stage in sorted((task_dir / "root" / "state").glob("[0-9][0-9]-*"))
            if stage.is_dir()
        ]
        for step in status.get("steps", []):
            observer = step.get("veras_controller") or {}
            for key in (
                "challenger_attempts",
                "recovery_attempts",
                "performance_attempts",
            ):
                value = int(observer.get(key, 0) or 0)
                task_counts[key] += value
                totals[key] += value
            for key in (
                "challenger_report_present",
                "recovery_accepted",
                "performance_accepted",
                "local_solver_used",
                "local_solver_accepted",
            ):
                value = int(bool(observer.get(key)))
                task_counts[key] += value
                totals[key] += value
            rollback_count = len(observer.get("rollback_reasons", []) or [])
            task_counts["rollback_count"] += rollback_count
            totals["rollback_count"] += rollback_count
            for decision in observer.get("decisions", []) or []:
                action = str(decision.get("action", "unknown"))
                mode = str(decision.get("mode", "unknown"))
                actions[action] += 1
                modes[mode] += 1
                task_actions[action] += 1
                task_modes[mode] += 1

        if task_counts["challenger_attempts"]:
            mechanism_tasks["challenger"].add(task)
        if task_counts["recovery_attempts"]:
            mechanism_tasks["recovery"].add(task)
        if task_counts["performance_attempts"]:
            mechanism_tasks["performance"].add(task)
        if task_counts["local_solver_used"]:
            mechanism_tasks["local_solver"].add(task)
        if task_counts["rollback_count"]:
            mechanism_tasks["rollback"].add(task)
        if any(audit["structural_restoration"] for audit in audits):
            mechanism_tasks["structural_restoration"].add(task)
        if any(audit["semantic_restoration"] for audit in audits):
            mechanism_tasks["semantic_restoration"].add(task)

        score = scores.get(task, {})
        records.append(
            {
                "task": task,
                "band": DIFFICULTY.get(task, {}).get("band", "unknown"),
                "complete": bool(score.get("complete")),
                "feasible": bool(score.get("feasible")),
                "passed": bool(score.get("passed")),
                "normalized_quality": float(score.get("normalized_quality", 0) or 0),
                "counts": dict(task_counts),
                "actions": dict(task_actions),
                "modes": dict(task_modes),
                "audits": audits,
            }
        )

    by_mechanism = {}
    rows_by_task = {row["task"]: row for row in records}
    for mechanism, tasks in mechanism_tasks.items():
        selected = [rows_by_task[task] for task in sorted(tasks)]
        by_mechanism[mechanism] = {
            "tasks": len(selected),
            "completed": sum(row["complete"] for row in selected),
            "feasible": sum(row["feasible"] for row in selected),
            "passed": sum(row["passed"] for row in selected),
            "mean_normalized_quality": (
                sum(row["normalized_quality"] for row in selected) / len(selected)
                if selected
                else 0.0
            ),
            "task_names": [row["task"] for row in selected],
        }

    audit_rows = [audit for row in records for audit in row["audits"]]
    return {
        "schema_version": 1,
        "scope": {
            "tasks": len(records),
            "bands": dict(Counter(row["band"] for row in records)),
            "evidence_type": "execution evidence",
        },
        "component_totals": dict(totals),
        "decision_actions": dict(actions),
        "decision_modes": dict(modes),
        "audit_transitions": {
            "stages": len(audit_rows),
            "event_streams": sum(row["event_streams"] for row in audit_rows),
            "challenger_reports": sum(
                row["challenger_report_present"] for row in audit_rows
            ),
            "structural_restorations": sum(
                row["structural_restoration"] for row in audit_rows
            ),
            "semantic_restorations": sum(
                row["semantic_restoration"] for row in audit_rows
            ),
            "semantic_residual_reductions": sum(
                row["semantic_residual_reduced"] for row in audit_rows
            ),
        },
        "by_mechanism": by_mechanism,
        "tasks": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    evidence = summarize(output)
    target = output / "component-evidence-summary.json"
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    transitions = evidence["audit_transitions"]
    print(
        f"component evidence: tasks={evidence['scope']['tasks']}; "
        f"stages={transitions['stages']}; streams={transitions['event_streams']}; "
        f"structural_restorations={transitions['structural_restorations']}; "
        f"semantic_restorations={transitions['semantic_restorations']}"
    )


if __name__ == "__main__":
    main()
