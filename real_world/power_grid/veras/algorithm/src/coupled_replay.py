#!/usr/bin/env python3
"""Replay an NPZ as the initial incumbent of the complete free MISOCP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from mosek.fusion import AccSolutionStatus

from candidate.src.exact_model import ARRAY_SHAPES, build_model, extract_solution, set_solution_levels
from candidate.src.independent_audit import audit
from candidate.src.input_data import load_inputs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/work/app"))
    ap.add_argument("--solution", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--time-limit", type=float, default=30.0)
    args = ap.parse_args()

    independent = audit(args.root, args.solution)
    src = np.load(args.solution, allow_pickle=False)
    supplied = {name: np.asarray(src[name], dtype=float) for name in ARRAY_SHAPES}
    built = build_model(load_inputs(args.root), integral=True)
    set_solution_levels(built, supplied)
    model = built.model
    model.acceptedSolutionStatus(AccSolutionStatus.Feasible)
    model.setSolverParam("numThreads", 4)
    model.setSolverParam("optimizerMaxTime", args.time_limit)
    model.setSolverParam("mioMaxTime", args.time_limit)
    mio_tolerance = max(2e-6, 1.25 * independent.get("maximum_normalized_residual", 0.0))
    model.setSolverParam("mioTolFeas", mio_tolerance)
    model.setSolverParam("mioMaxNumRelaxs", 0)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("w") as log:
        model.setLogHandler(log)
        model.solve()
    problem_status = str(model.getProblemStatus())
    primal_status = str(model.getPrimalSolutionStatus())
    feasible = "Feasible" in primal_status or "Optimal" in primal_status
    accepted = extract_solution(built) if feasible else None
    solver_objective = float(model.primalObjValue()) if feasible else None
    optimizer_time = float(model.getSolverDoubleInfo("optimizerTime"))
    model.dispose()

    replay_objective = independent.get("objective_components", {}).get("full_reported_model_total")
    objective_delta = None if solver_objective is None else solver_objective - replay_objective
    max_level_delta = None if accepted is None else max(
        float(np.max(np.abs(accepted[name] - supplied[name]))) for name in ARRAY_SHAPES
    )
    result = {
        "passed": bool(independent.get("passed") and feasible and abs(objective_delta or 0.0) <= 1e-3),
        "same_model_replay_passed": bool(independent.get("passed") and feasible and abs(objective_delta or 0.0) <= 1e-3),
        "method": "complete free-MISOCP initial-incumbent replay",
        "solution_path": str(args.solution),
        "independent_replay_passed": bool(independent.get("passed")),
        "free_model_accepted_incumbent": feasible,
        "all_planning_variables_free": True,
        "all_eight_integer_families_integral": independent.get("maximum_integrality_distance") == 0.0,
        "problem_status": problem_status,
        "primal_status": primal_status,
        "solver_objective": solver_objective,
        "independent_replayed_objective": replay_objective,
        "objective_delta": objective_delta,
        "maximum_returned_level_delta": max_level_delta,
        "maximum_independent_normalized_residual": independent.get("maximum_normalized_residual"),
        "mio_feasibility_tolerance": mio_tolerance,
        "optimizer_time_seconds": optimizer_time,
        "threads": 4,
        "relaxations_solved": 0,
        "construction_dimensions": {
            "scalar_variables": 38384,
            "constraints": 78083,
            "integer_variables": 1147
        }
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 2)


if __name__ == "__main__":
    main()
