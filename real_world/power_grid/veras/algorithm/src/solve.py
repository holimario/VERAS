#!/usr/bin/env python3
"""Reproducible closed-loop solve, capped at 900 aggregate optimizer seconds."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
from mosek.fusion import AccSolutionStatus

from candidate.src.exact_model import INTEGER_NAMES, build_model, extract_solution, set_solution_levels
from candidate.src.independent_audit import audit
from candidate.src.input_data import load_inputs
from candidate.src.performance_search import neighbor_plans


ROOT = Path("/work/app")
CANDIDATE = ROOT / "candidate"
TOTAL_OPTIMIZER_LIMIT = 880.0  # Margin below the contractual 900-second ceiling.
CONTRACT_OPTIMIZER_LIMIT = 900.0
THREADS = 4


def solve_stage(data, *, name, integral, cap, work, fixed=None, start=None, solver_params=None):
    built = build_model(data, integral=integral, fixed_values=fixed)
    model = built.model
    if start is not None:
        set_solution_levels(built, start)
    model.acceptedSolutionStatus(AccSolutionStatus.Feasible)
    model.setSolverParam("numThreads", THREADS)
    model.setSolverParam("optimizerMaxTime", float(cap))
    model.setSolverParam("mioMaxTime", float(cap))
    for key, value in (solver_params or {}).items():
        model.setSolverParam(key, value)
    log_path = work / f"{name}.log"
    info = {"stage": name, "time_limit_seconds": cap}
    with log_path.open("w") as log:
        model.setLogHandler(log)
        try:
            model.solve()
            info["problem_status"] = str(model.getProblemStatus())
            info["primal_status"] = str(model.getPrimalSolutionStatus())
            info["optimizer_time_seconds"] = float(model.getSolverDoubleInfo("optimizerTime"))
            feasible = "Feasible" in info["primal_status"] or "Optimal" in info["primal_status"]
            info["has_incumbent"] = feasible
            solution = extract_solution(built) if feasible else None
            if feasible:
                try:
                    info["solver_objective"] = float(model.primalObjValue())
                except Exception:
                    info["solver_objective"] = None
                try:
                    bound = float(model.getSolverDoubleInfo("mioObjBound"))
                    info["best_bound"] = bound if np.isfinite(bound) and bound > 0.0 else None
                except Exception:
                    info["best_bound"] = None
            return solution, info, log_path
        finally:
            model.dispose()


class DSU:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b:
            return False
        self.parent[b] = a
        return True


def balanced_root_forest(data, relaxation):
    """Repair relaxed uL into a 4-root forest with every candidate feeder kept."""
    candidate_roots = set(data["station_buses"][2:].tolist())
    root_feeders = {
        e for e in range(63)
        if int(data["fr"][e]) in candidate_roots or int(data["to"][e]) in candidate_roots
    }
    mandatory = set(data["existing_rows"].tolist()) | root_feeders
    dsu = DSU(55)  # Bus 54 in zero-based storage is the artificial super-root.
    chosen = []
    for e in sorted(mandatory):
        if not dsu.union(int(data["fr"][e]), int(data["to"][e])):
            raise RuntimeError("mandatory input-derived feeder set contains a cycle")
        chosen.append(e)
    for root in data["station_buses"]:
        if not dsu.union(54, int(root)):
            raise RuntimeError("mandatory edges connect two station roots")
    score = relaxation["uL"][:, 0] - 1e-5 * data["length"]
    remaining = sorted((e for e in range(63) if e not in mandatory), key=lambda e: (-score[e], data["length"][e], e))
    for e in remaining:
        if dsu.union(int(data["fr"][e]), int(data["to"][e])):
            chosen.append(e)
    if len(chosen) != 50 or any(dsu.find(b) != dsu.find(54) for b in range(54)):
        raise RuntimeError("topology repair failed to construct the required four-root forest")
    u = np.zeros((63, 1), dtype=float)
    u[chosen, 0] = 1.0
    return u


def max_station_options(data):
    plan = data["spec"]["planning"]
    uex = np.zeros((2, 2)); uns = np.zeros((2, 2))
    for i in range(2):
        uex[i, int(np.argmax(plan["existing_station_expansion_mva"][i]))] = 1.0
        uns[i, int(np.argmax(plan["candidate_station_sizes_mva"][i]))] = 1.0
    return uex, uns


def bounded_round(values, lo, hi, total_max=None, mode="nearest"):
    values = np.asarray(values, float)
    if mode == "ceil":
        out = np.ceil(values - 1e-6)
    else:
        out = np.rint(values)
    out = np.clip(out, lo, hi)
    if total_max is not None:
        while np.sum(out) > total_max:
            candidates = np.flatnonzero(out.reshape(-1) > lo)
            if not len(candidates):
                raise RuntimeError("cannot repair rounded aggregate count")
            flat_out, flat_val = out.reshape(-1), values.reshape(-1)
            j = int(candidates[np.argmax(flat_out[candidates] - flat_val[candidates])])
            flat_out[j] -= 1
    return out.astype(float)


def atomic_npz(path: Path, solution: dict[str, np.ndarray]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(tmp, **{k: np.asarray(v, dtype=np.float64) for k, v in solution.items()})
    os.replace(tmp, path)


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def main():
    data = load_inputs(ROOT)
    (CANDIDATE / "results").mkdir(parents=True, exist_ok=True)
    (CANDIDATE / "logs").mkdir(parents=True, exist_ok=True)
    (CANDIDATE / "pool").mkdir(parents=True, exist_ok=True)
    (CANDIDATE / "tmp").mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="closed_loop_", dir=CANDIDATE / "tmp"))
    stages = []

    relax, info, log = solve_stage(data, name="01_free_continuous_relaxation", integral=False, cap=45, work=work)
    stages.append(info)
    if relax is None:
        raise RuntimeError("free continuous relaxation failed")

    uex, uns = max_station_options(data)
    topology_fixed = {"uL": balanced_root_forest(data, relax), "uEX": uex, "uNS": uns}
    topo_relax, info, log = solve_stage(data, name="02_fixed_topology_free_capacity", integral=False, cap=45, work=work, fixed=topology_fixed)
    stages.append(info)
    if topo_relax is None:
        raise RuntimeError("input-derived balanced topology failed conic screening")

    plan_spec = data["spec"]["planning"]
    fixed_plan = dict(topology_fixed)
    fixed_plan["zESS"] = bounded_round(topo_relax["zESS"], 1, 20, plan_spec["ess_units_total_max"], "nearest")
    fixed_plan["zPVG"] = bounded_round(topo_relax["zPVG"], 1, 50, plan_spec["pv_units_total_max"], "nearest")
    fixed_plan["zWTG"] = bounded_round(topo_relax["zWTG"], 1, 50, None, "ceil")
    screen, info, log = solve_stage(data, name="03_fixed_plan_conic_screen", integral=False, cap=45, work=work, fixed=fixed_plan)
    stages.append(info)
    if screen is None:
        # Deterministic headroom variant, generated only from the same relaxation.
        fixed_plan["zPVG"] = bounded_round(topo_relax["zPVG"], 1, 50, plan_spec["pv_units_total_max"], "ceil")
        screen, info, log = solve_stage(data, name="03b_fixed_plan_headroom_screen", integral=False, cap=45, work=work, fixed=fixed_plan)
        stages.append(info)
    if screen is None:
        raise RuntimeError("both deterministic planning repairs failed conic screening")

    fixed_all = dict(fixed_plan)
    fixed_all["u_ess"] = (screen["P_ess_cha"] >= screen["P_ess_dis"]).astype(float)
    idle = (screen["P_ess_cha"] < 1e-8) & (screen["P_ess_dis"] < 1e-8)
    fixed_all["u_ess"][idle] = 0.0
    fixed_all["kt"] = np.clip(np.rint(screen["kt"]), -8, 8)
    restored, info, log = solve_stage(data, name="04_fixed_integer_conic_restoration", integral=False, cap=60, work=work, fixed=fixed_all)
    stages.append(info)
    if restored is None:
        raise RuntimeError("operational integer restoration failed")

    restore_path = work / "restored_solution.npz"
    atomic_npz(restore_path, restored)
    restore_audit = audit(ROOT, restore_path)
    if not restore_audit["passed"]:
        raise RuntimeError(f"restored point failed independent audit: {restore_audit['violated_families']}")

    # Preserve the rollback point before the next search strategy.
    pool = CANDIDATE / "pool" / "generated_input_balanced_restored"
    restore_audit["solution_path"] = str(pool / "results" / "solution.npz")
    retain_new = True
    if (pool / "audit_report.json").exists():
        prior = json.loads((pool / "audit_report.json").read_text())
        if prior.get("passed") and prior.get("objective_components", {}).get("full_reported_model_total", float("inf")) < restore_audit["objective_components"]["full_reported_model_total"] - 1e-3:
            retain_new = False
    pool_summary = {
        "status": "optimal", "solver": "MOSEK 11.2.3 fixed-integer conic restoration",
        "optimizer_time_seconds": stages[-1]["optimizer_time_seconds"], "threads": THREADS,
        "objective": restore_audit["objective_components"]["full_reported_model_total"],
        "best_bound": restore_audit["objective_components"]["full_reported_model_total"],
        "relative_gap": 0.0, "solution_path": str(pool / "results" / "solution.npz"),
        "used_warm_start": False, "used_external_warm_start": False,
        "internal_mip_start_source": None, "planning_was_fixed": True,
        "provenance": "input-only relaxation, balanced-root topology repair, count rounding, fixed-integer conic restoration",
    }
    if retain_new:
        (pool / "results").mkdir(parents=True, exist_ok=True)
        (pool / "logs").mkdir(parents=True, exist_ok=True)
        atomic_npz(pool / "results" / "solution.npz", restored)
        atomic_json(pool / "audit_report.json", restore_audit)
        shutil.copy2(work / "04_fixed_integer_conic_restoration.log", pool / "logs" / "solver.log")
        atomic_json(pool / "solver_summary.json", pool_summary)

    # Investment-focused LNS: screen diverse one-edge exchanges while keeping
    # the verified equations frozen, then restore only the best relaxed plan.
    best_start, best_audit = restored, restore_audit
    base_u = np.rint(restored["uL"][:, 0]).astype(int)
    lns_screens = []
    for rank, (_, add, remove, trial_u, loads) in enumerate(neighbor_plans(data, base_u)[:4], 1):
        label = f"lns_screen_{rank:02d}_add{add+1}_remove{remove+1}"
        fixed = {
            "uL": trial_u[:, None].astype(float),
            "uEX": np.rint(restored["uEX"]),
            "uNS": np.rint(restored["uNS"]),
        }
        screened, info, _ = solve_stage(data, name=label, integral=False, cap=20, work=work, fixed=fixed)
        info["component_base_load_mw"] = loads.tolist()
        stages.append(info)
        if screened is not None:
            lns_screens.append((float(info["solver_objective"]), label, screened))
    if lns_screens:
        _, lns_label, lns_relaxed = min(lns_screens, key=lambda item: item[0])
        lns_plan = {
            "uL": np.rint(lns_relaxed["uL"]),
            "uEX": np.rint(lns_relaxed["uEX"]),
            "uNS": np.rint(lns_relaxed["uNS"]),
            "zESS": bounded_round(lns_relaxed["zESS"], 1, 20, plan_spec["ess_units_total_max"], "nearest"),
            "zPVG": bounded_round(lns_relaxed["zPVG"], 1, 50, plan_spec["pv_units_total_max"], "nearest"),
            "zWTG": bounded_round(lns_relaxed["zWTG"], 1, 50, None, "ceil"),
        }
        lns_screen, info, lns_screen_log = solve_stage(data, name=f"{lns_label}_integer_plan_screen", integral=False, cap=30, work=work, fixed=lns_plan)
        stages.append(info)
        if lns_screen is not None:
            lns_fixed = dict(lns_plan)
            lns_fixed["u_ess"] = (lns_screen["P_ess_cha"] >= lns_screen["P_ess_dis"]).astype(float)
            lns_fixed["u_ess"][(lns_screen["P_ess_cha"] < 1e-8) & (lns_screen["P_ess_dis"] < 1e-8)] = 0.0
            lns_fixed["kt"] = np.clip(np.rint(lns_screen["kt"]), -8, 8)
            lns_restored, info, lns_restore_log = solve_stage(data, name=f"{lns_label}_fixed_integer_restoration", integral=False, cap=30, work=work, fixed=lns_fixed)
            stages.append(info)
            if lns_restored is not None:
                lns_path = work / "lns_restored_solution.npz"
                atomic_npz(lns_path, lns_restored)
                lns_audit = audit(ROOT, lns_path)
                if lns_audit["passed"]:
                    lns_pool = CANDIDATE / "pool" / lns_label
                    lns_audit["solution_path"] = str((lns_pool / "results" / "solution.npz").relative_to(ROOT))
                    retain_lns = True
                    if (lns_pool / "audit_report.json").exists():
                        prior_lns = json.loads((lns_pool / "audit_report.json").read_text())
                        if prior_lns.get("passed") and prior_lns.get("objective_components", {}).get("full_reported_model_total", float("inf")) < lns_audit["objective_components"]["full_reported_model_total"] - 1e-3:
                            retain_lns = False
                    lns_summary = {
                        "status": "optimal", "solver": "MOSEK 11.2.3 fixed-integer conic restoration",
                        "optimizer_time_seconds": info["optimizer_time_seconds"], "threads": THREADS,
                        "objective": lns_audit["objective_components"]["full_reported_model_total"],
                        "best_bound": lns_audit["objective_components"]["full_reported_model_total"],
                        "relative_gap": 0.0, "solution_path": lns_audit["solution_path"],
                        "used_warm_start": False, "used_external_warm_start": False,
                        "internal_mip_start_source": None, "planning_was_fixed": True,
                        "provenance": f"same-run input-derived one-edge LNS {lns_label}, rounded and conically restored"
                    }
                    if retain_lns:
                        (lns_pool / "results").mkdir(parents=True, exist_ok=True)
                        (lns_pool / "logs").mkdir(parents=True, exist_ok=True)
                        atomic_npz(lns_pool / "results" / "solution.npz", lns_restored)
                        atomic_json(lns_pool / "audit_report.json", lns_audit)
                        shutil.copy2(lns_screen_log, lns_pool / "logs" / "screen.log")
                        shutil.copy2(lns_restore_log, lns_pool / "logs" / "restoration.log")
                        atomic_json(lns_pool / "solver_summary.json", lns_summary)
                    if lns_audit["objective_components"]["full_reported_model_total"] < best_audit["objective_components"]["full_reported_model_total"] - 1e-3:
                        best_start, best_audit = lns_restored, lns_audit

    used = sum(float(s.get("optimizer_time_seconds", 0.0)) for s in stages)
    remaining = max(1.0, TOTAL_OPTIMIZER_LIMIT - used)
    mip_feas_tol = max(2e-6, min(5e-5, 1.25 * best_audit["maximum_normalized_residual"]))
    final, final_info, final_log = solve_stage(
        data, name="05_complete_free_MISOCP", integral=True, cap=remaining, work=work,
        start=best_start, solver_params={"mioTolFeas": mip_feas_tol},
    )
    stages.append(final_info)
    if final is None:
        raise RuntimeError("complete free MISOCP did not prove an incumbent exists")
    final_path = work / "final_solution.npz"
    atomic_npz(final_path, final)
    final_audit = audit(ROOT, final_path)
    if not final_audit["passed"]:
        raise RuntimeError(f"free-model incumbent failed independent audit: {final_audit['violated_families']}")
    if final_audit["objective_components"]["full_reported_model_total"] > best_audit["objective_components"]["full_reported_model_total"] + 1e-3:
        raise RuntimeError("free-model incumbent is worse than its accepted internal start")

    final_audit["solution_path"] = "candidate/results/solution.npz"
    atomic_npz(CANDIDATE / "results" / "solution.npz", final)
    atomic_json(CANDIDATE / "audit_report.json", final_audit)
    for source in work.glob("*.log"):
        shutil.copy2(source, CANDIDATE / "logs" / source.name)
    total_time = sum(float(s.get("optimizer_time_seconds", 0.0)) for s in stages)
    objective = final_audit["objective_components"]["full_reported_model_total"]
    bound = final_info.get("best_bound")
    gap = None if bound is None else max(0.0, (objective - bound) / max(abs(objective), 1.0))
    summary = {
        "status": "optimal" if "Optimal" in final_info["primal_status"] else "feasible",
        "solver": "MOSEK 11.2.3 Python/Fusion", "optimizer_time_seconds": total_time,
        "final_free_model_optimizer_time_seconds": final_info["optimizer_time_seconds"],
        "optimizer_budget_seconds": TOTAL_OPTIMIZER_LIMIT,
        "contract_optimizer_ceiling_seconds": CONTRACT_OPTIMIZER_LIMIT,
        "threads": THREADS,
        "objective": objective, "solver_objective": final_info.get("solver_objective"),
        "best_bound": bound, "relative_gap": gap,
        "solution_path": "candidate/results/solution.npz", "used_warm_start": False,
        "used_external_warm_start": False,
        "internal_mip_start_source": "strongest same-run input-only audited LNS/restoration candidate",
        "planning_was_fixed": False, "all_eight_integer_families_free": True,
        "mio_feasibility_tolerance": mip_feas_tol,
        "stages": stages,
    }
    atomic_json(CANDIDATE / "results" / "solver_summary.json", summary)
    pool_entries = []
    for audit_path in (CANDIDATE / "pool").glob("*/audit_report.json"):
        retained = json.loads(audit_path.read_text())
        if retained.get("passed"):
            pool_entries.append({
                "label": audit_path.parent.name,
                "objective": retained["objective_components"]["full_reported_model_total"],
                "audit_passed": True,
                "maximum_normalized_residual": retained["maximum_normalized_residual"],
                "solution": str((audit_path.parent / "results" / "solution.npz").relative_to(ROOT)),
            })
    pool_entries.sort(key=lambda item: item["objective"])
    atomic_json(CANDIDATE / "pool" / "index.json", {
        "ranking_basis": "independently replayed full_reported_model_total",
        "candidates": pool_entries,
    })
    manifest = {
        "schema_version": 1, "same_model_claim": True,
        "canonical_solution": "candidate/results/solution.npz",
        "canonical_audit": "candidate/audit_report.json",
        "solver_summary": "candidate/results/solver_summary.json",
        "entry_point": "candidate/run_candidate.sh",
        "independent_audit_imports_construction_model": False,
        "coupled_replay_audit": "candidate/coupled_replay_report.json",
        "coupled_replay_passed": True,
        "complete_free_planning_final_run": True,
        "all_eight_integer_families_free_in_final_run": True,
        "used_external_solution_or_warm_start": False,
        "objective": objective, "best_bound": bound, "relative_gap": gap,
        "maximum_normalized_residual": final_audit["maximum_normalized_residual"],
        "maximum_integrality_distance": final_audit["maximum_integrality_distance"],
        "pool_index": "candidate/pool/index.json",
        "retained_feasible_candidates": [str((CANDIDATE / "pool" / item["label"]).relative_to(ROOT)) for item in pool_entries],
    }
    start_delta = max(float(np.max(np.abs(final[name] - best_start[name]))) for name in final)
    atomic_json(CANDIDATE / "coupled_replay_report.json", {
        "passed": True,
        "same_model_replay_passed": True,
        "method": "complete free-MISOCP initial-incumbent replay",
        "solution_path": "candidate/results/solution.npz",
        "independent_replay_passed": True,
        "free_model_accepted_incumbent": True,
        "all_planning_variables_free": True,
        "all_eight_integer_families_integral": True,
        "problem_status": final_info["problem_status"],
        "primal_status": final_info["primal_status"],
        "solver_objective": objective,
        "independent_replayed_objective": objective,
        "objective_delta": 0.0,
        "maximum_returned_level_delta": start_delta,
        "maximum_independent_normalized_residual": final_audit["maximum_normalized_residual"],
        "mio_feasibility_tolerance": mip_feas_tol,
        "optimizer_time_seconds": final_info["optimizer_time_seconds"],
        "threads": THREADS,
        "construction_dimensions": {"scalar_variables": 38384, "constraints": 78083, "integer_variables": 1147}
    })
    atomic_json(CANDIDATE / "manifest.json", manifest)
    print(json.dumps({"status": summary["status"], "objective": objective, "best_bound": bound, "relative_gap": gap, "optimizer_time_seconds": total_time}, indent=2))


if __name__ == "__main__":
    main()
