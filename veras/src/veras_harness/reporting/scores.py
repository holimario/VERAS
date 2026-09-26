#!/usr/bin/env python3
"""Summarize a benchmark run with the official feasibility and quality rule."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any


from ..constants import benchmark_root


OR_BENCH = benchmark_root()
DIFFICULTY = json.loads((OR_BENCH / "difficulty.json").read_text())["tasks"]


def expected_steps(task: str) -> int:
    config = tomllib.loads((OR_BENCH / "harbor_tasks" / task / "task.toml").read_text())
    steps = config.get("steps")
    return len(steps) if isinstance(steps, list) and steps else 1


def summarize(output: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    codex = output / "codex"
    if not codex.is_dir():
        return {"task_count": 0, "completed": 0, "passed": 0, "tasks": []}
    for task_dir in sorted(path for path in codex.iterdir() if path.is_dir()):
        status_path = task_dir / "status.json"
        status = json.loads(status_path.read_text()) if status_path.is_file() else {}
        expected = expected_steps(task_dir.name)
        steps = status.get("steps", [])
        qualities = [
            float(step.get("verifier", {}).get("reward_details", {}).get("quality", 0.0) or 0.0)
            for step in steps
        ]
        feasibility = [
            float(step.get("verifier", {}).get("reward_details", {}).get("feasibility", 0.0) or 0.0)
            for step in steps
        ]
        complete = status.get("status") == "complete" and len(steps) == expected
        feasible = complete and all(value > 0.0 for value in feasibility)
        normalized_quality = sum(qualities) / (2.0 * expected)
        passed = feasible and normalized_quality > 0.4
        rows.append({
            "task": task_dir.name,
            "band": DIFFICULTY.get(task_dir.name, {}).get("band", "unknown"),
            "complete": complete,
            "expected_steps": expected,
            "observed_steps": len(steps),
            "feasible": feasible,
            "normalized_quality": normalized_quality,
            "passed": passed,
        })
    count = len(rows)
    result = {
        "task_count": count,
        "completed": sum(row["complete"] for row in rows),
        "feasible": sum(row["feasible"] for row in rows),
        "passed": sum(row["passed"] for row in rows),
        "pass_rate": sum(row["passed"] for row in rows) / count if count else 0.0,
        "feasibility_rate": sum(row["feasible"] for row in rows) / count if count else 0.0,
        "mean_normalized_quality": sum(row["normalized_quality"] for row in rows) / count if count else 0.0,
        "tasks": rows,
    }
    result["by_band"] = {}
    for band in sorted({row["band"] for row in rows}):
        selected = [row for row in rows if row["band"] == band]
        band_count = len(selected)
        result["by_band"][band] = {
            "task_count": band_count,
            "completed": sum(row["complete"] for row in selected),
            "feasible": sum(row["feasible"] for row in selected),
            "passed": sum(row["passed"] for row in selected),
            "pass_rate": sum(row["passed"] for row in selected) / band_count,
            "feasibility_rate": sum(row["feasible"] for row in selected) / band_count,
            "mean_normalized_quality": sum(
                row["normalized_quality"] for row in selected
            ) / band_count,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    summary = summarize(output)
    target = output / "official-score-summary.json"
    target.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"official pass: {summary['passed']}/{summary['task_count']} "
        f"({summary['pass_rate']:.2%}); feasible={summary['feasible']} "
        f"({summary['feasibility_rate']:.2%}); "
        f"mean_normalized_quality={summary['mean_normalized_quality']:.4f}; "
        f"completed={summary['completed']}"
    )


if __name__ == "__main__":
    main()
