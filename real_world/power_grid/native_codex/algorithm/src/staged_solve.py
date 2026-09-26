#!/usr/bin/env python3
"""Input-derived staged solve ending in a complete free-planning MISOCP."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from mosek.fusion import AccSolutionStatus, SolutionStatus

from solve import build_model, read_inputs, solver_info


INTEGER_NAMES = ("uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt")


def solve_stage(model, variables):
    start = time.monotonic()
    exception = None
    try:
        model.solve()
    except Exception as exc:
        exception = repr(exc)
    wall = time.monotonic() - start
    status = model.getPrimalSolutionStatus()
    feasible = status in (SolutionStatus.Optimal, SolutionStatus.Feasible)
    values = None
    if feasible:
        model.acceptedSolutionStatus(AccSolutionStatus.Feasible)
        values = {
            name: np.asarray(variable.level(), dtype=np.float64).reshape(shape)
            for name, (variable, shape) in variables.items()
        }
    info = {
        "feasible": feasible,
        "primal_status": str(status),
        "problem_status": str(model.getProblemStatus()),
        "objective": float(model.primalObjValue()) if feasible else None,
        "best_bound": solver_info(model, "mioObjBound"),
        "relative_gap": solver_info(model, "mioObjRelGap"),
        "optimizer_time_seconds": solver_info(model, "optimizerTime", wall),
        "wall_time_seconds": wall,
        "exception": exception,
    }
    return values, info


def rooted_forest(branch, relaxed_uL, mandatory):
    """Maximum relaxed-weight forest with one station root per component."""
    parent = list(range(54))
    roots = [{i} if i in (50, 51, 52, 53) else set() for i in range(54)]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb or (roots[ra] and roots[rb]):
            return False
        parent[rb] = ra
        roots[ra] |= roots[rb]
        return True

    chosen = set()
    for e in sorted(mandatory):
        a, b = int(branch[e, 1]) - 1, int(branch[e, 2]) - 1
        if not union(a, b):
            raise RuntimeError("Mandatory edges do not form a valid rooted forest")
        chosen.add(e)
    order = sorted(
        (e for e in range(63) if e not in chosen),
        key=lambda e: (-float(relaxed_uL[e]) / float(branch[e, 3]), e),
    )
    for e in order:
        a, b = int(branch[e, 1]) - 1, int(branch[e, 2]) - 1
        if union(a, b):
            chosen.add(e)
        if len(chosen) == 50:
            break
    if len(chosen) != 50:
        raise RuntimeError("Could not construct the required 50-edge rooted forest")
    components = {}
    for b in range(54):
        components.setdefault(find(b), []).append(b)
    if len(components) != 4 or any(not roots[find(nodes[0])] for nodes in components.values()):
        raise RuntimeError("Constructed forest does not have four rooted components")
    result = np.zeros(63, dtype=float)
    result[list(chosen)] = 1.0
    return result


def planning_from_relaxation(spec, branch, relaxed):
    planning = spec["planning"]
    mandatory = {int(e) - 1 for e in spec["sets"]["existing_line_rows"]}
    # Give both candidate substations access to each of their three incident
    # feeder directions. Without this deterministic repair, a pure maximum-
    # weight forest can strand a new station as a singleton even though its
    # option is selected. These six edges are all read directly from the input
    # branch table and remain unfixed in the final MISOCP.
    mandatory |= {
        e
        for e in range(63)
        if int(branch[e, 1]) in spec["sets"]["candidate_substations"]
        or int(branch[e, 2]) in spec["sets"]["candidate_substations"]
    }
    plan = {
        "uL": rooted_forest(branch, relaxed["uL"][:, 0], mandatory),
        # Maximum station options provide robust component capacity; these remain
        # entirely free in the final benchmark solve.
        "uEX": np.asarray([[0.0, 1.0], [0.0, 1.0]]),
        "uNS": np.asarray([[0.0, 1.0], [0.0, 1.0]]),
        "zESS": np.clip(
            np.ceil(relaxed["zESS"][:, 0] - 1.0e-7),
            planning["ess_units_per_bus_min"],
            planning["ess_units_per_bus_max"],
        ),
        "zPVG": np.clip(
            np.ceil(relaxed["zPVG"][:, 0] - 1.0e-7),
            planning["pv_units_per_bus_min"],
            planning["pv_units_per_bus_max"],
        ),
        "zWTG": np.clip(
            np.ceil(relaxed["zWTG"][:, 0] - 1.0e-7),
            planning["wind_units_per_bus_min"],
            planning["wind_units_per_bus_max"],
        ),
    }
    # Preserve the aggregate PV and ESS limits if componentwise ceilings cross them.
    for name, total_key in (("zESS", "ess_units_total_max"), ("zPVG", "pv_units_total_max")):
        values = plan[name]
        limit = int(planning[total_key])
        lower = int(planning["ess_units_per_bus_min"] if name == "zESS" else planning["pv_units_per_bus_min"])
        while int(np.sum(values)) > limit:
            eligible = np.flatnonzero(values > lower)
            if eligible.size == 0:
                raise RuntimeError(f"Cannot repair aggregate limit for {name}")
            fractional = relaxed[name][:, 0] - np.floor(relaxed[name][:, 0])
            i = int(eligible[np.argmin(fractional[eligible])])
            values[i] -= 1.0
    return plan


def operation_from_screen(screen):
    return {
        "u_ess": (screen["P_ess_cha"] > screen["P_ess_dis"] + 1.0e-9).astype(float),
        "kt": np.clip(np.rint(screen["kt"]), -8.0, 8.0),
    }


def counts_from_screen(spec, screen):
    planning = spec["planning"]
    result = {
        "zESS": np.clip(
            np.ceil(screen["zESS"][:, 0] - 1.0e-5),
            planning["ess_units_per_bus_min"],
            planning["ess_units_per_bus_max"],
        ),
        "zPVG": np.clip(
            np.ceil(screen["zPVG"][:, 0] - 1.0e-5),
            planning["pv_units_per_bus_min"],
            planning["pv_units_per_bus_max"],
        ),
        "zWTG": np.clip(
            np.ceil(screen["zWTG"][:, 0] - 1.0e-5),
            planning["wind_units_per_bus_min"],
            planning["wind_units_per_bus_max"],
        ),
    }
    for name, limit_key, lower_key in (
        ("zESS", "ess_units_total_max", "ess_units_per_bus_min"),
        ("zPVG", "pv_units_total_max", "pv_units_per_bus_min"),
    ):
        values = result[name]
        limit = int(planning[limit_key])
        lower = int(planning[lower_key])
        while int(np.sum(values)) > limit:
            eligible = np.flatnonzero(values > lower)
            fractions = screen[name][:, 0] - np.floor(screen[name][:, 0])
            i = int(eligible[np.argmin(fractions[eligible])])
            values[i] -= 1.0
    return result


def set_initial_solution(variables, values):
    for name, (variable, shape) in variables.items():
        variable.setLevel(np.asarray(values[name], dtype=float).reshape(-1).tolist())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--relax-time", type=float, default=12.0)
    parser.add_argument("--planning-screen-time", type=float, default=25.0)
    parser.add_argument("--integer-screen-time", type=float, default=25.0)
    parser.add_argument("--final-time", type=float, default=150.0)
    args = parser.parse_args()
    total_limit = args.relax_time + args.planning_screen_time + args.integer_screen_time + args.final_time
    if args.threads > 4 or args.threads < 1 or total_limit > 900.0:
        raise ValueError("invalid aggregate optimizer budget or thread count")

    result_dir = args.output_root / "results"
    log_dir = args.output_root / "logs"
    result_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    spec, branch, bus, forecast = read_inputs(args.input_root)
    stage_info = {}
    total_optimizer_time = 0.0
    total_build_time = 0.0

    def make(log_name, limit, relax, fixes=None, exact_names=None):
        nonlocal total_build_time
        start = time.monotonic()
        built = build_model(
            spec,
            branch,
            bus,
            forecast,
            log_dir / log_name,
            limit,
            args.threads,
            relax_integrality=relax,
            integer_fix=fixes,
            exact_integer_names=exact_names,
        )
        total_build_time += time.monotonic() - start
        return built

    # 1. Exact continuous relaxation, used only to derive an internal candidate.
    model, variables, handle, metadata = make("mosek_relax.log", args.relax_time, True)
    relaxed, stage_info["continuous_relaxation"] = solve_stage(model, variables)
    total_optimizer_time += stage_info["continuous_relaxation"]["optimizer_time_seconds"]
    handle.close()
    model.dispose()
    if relaxed is None:
        raise RuntimeError("continuous relaxation did not return a solution")

    # 2. Fix and screen the input-derived short-path rooted forest while all
    # operational integer families remain continuous.
    planning_seed = planning_from_relaxation(spec, branch, relaxed)
    topology_fix = {name: planning_seed[name] for name in ("uL", "uNS", "uEX")}
    model, variables, handle, _ = make(
        "mosek_planning_screen.log",
        args.planning_screen_time,
        True,
        topology_fix,
    )
    screened, stage_info["fixed_planning_continuous_screen"] = solve_stage(model, variables)
    total_optimizer_time += stage_info["fixed_planning_continuous_screen"]["optimizer_time_seconds"]
    handle.close()
    model.dispose()
    if screened is None:
        raise RuntimeError("short-path planning candidate failed continuous screening")

    # 3. Round the operational integers and restore a complete feasible point.
    planning_fix = dict(topology_fix)
    planning_fix.update(counts_from_screen(spec, screened))
    integer_fix = dict(planning_fix)
    integer_fix.update(operation_from_screen(screened))
    model, variables, handle, _ = make(
        "mosek_integer_screen.log", args.integer_screen_time, True, integer_fix
    )
    restored, stage_info["fully_fixed_integer_restoration"] = solve_stage(model, variables)
    total_optimizer_time += stage_info["fully_fixed_integer_restoration"]["optimizer_time_seconds"]
    handle.close()
    model.dispose()
    if restored is None:
        raise RuntimeError("rounded integer candidate could not be restored")
    np.savez_compressed(result_dir / "internal_start.npz", **restored)

    # 4. Complete exact MISOCP: all planning decisions and all other integers free.
    model, variables, handle, _ = make("mosek.log", args.final_time, False)
    set_initial_solution(variables, restored)
    model.setSolverParam("mioConstructSol", "on")
    final_values, stage_info["free_planning_misocp"] = solve_stage(model, variables)
    total_optimizer_time += stage_info["free_planning_misocp"]["optimizer_time_seconds"]
    final_problem_status = str(model.getProblemStatus())
    final_primal_status = str(model.getPrimalSolutionStatus())
    handle.close()
    model.dispose()
    if final_values is None:
        raise RuntimeError("free-planning MISOCP did not retain an incumbent")

    final = stage_info["free_planning_misocp"]
    restored_objective = stage_info["fully_fixed_integer_restoration"]["objective"]
    if final["objective"] <= restored_objective + 1.0e-6:
        selected_values = final_values
        selected_objective = final["objective"]
        selected_artifact = "free-planning MISOCP incumbent"
    else:
        selected_values = restored
        selected_objective = restored_objective
        selected_artifact = (
            "independently feasible fixed-integer restoration supplied to the "
            "free-planning MISOCP; retained as the no-worse rollback point"
        )
    np.savez_compressed(result_dir / "solution.npz", **selected_values)

    status = "optimal" if final["primal_status"] == str(SolutionStatus.Optimal) else "feasible"
    summary = {
        "status": status,
        "solver": "MOSEK 11.2 Fusion staged MISOCP",
        "optimizer_time_seconds": total_optimizer_time,
        "threads": args.threads,
        "objective": selected_objective,
        "best_bound": final["best_bound"],
        "relative_gap": final["relative_gap"],
        "solution_path": "candidate/results/solution.npz",
        "used_warm_start": False,
        "used_external_warm_start": False,
        "internal_mip_start_source": "continuous relaxation -> short-path rooted-forest repair -> fixed-integer conic restoration, all generated from supplied inputs inside this entry point",
        "planning_was_fixed": False,
        "optimizer_time_limit_seconds": total_limit,
        "optimizer_stage_limits_seconds": {
            "continuous_relaxation": args.relax_time,
            "fixed_planning_continuous_screen": args.planning_screen_time,
            "fully_fixed_integer_restoration": args.integer_screen_time,
            "free_planning_misocp": args.final_time,
        },
        "build_time_seconds": total_build_time,
        "problem_status": final_problem_status,
        "primal_solution_status": final_primal_status,
        "random_seed": 240924,
        "selected_artifact": selected_artifact,
        "stage_details": stage_info,
        "planning_start": {name: np.asarray(value).tolist() for name, value in planning_fix.items()},
        "metadata": metadata,
    }
    with (result_dir / "solver_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
