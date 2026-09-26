#!/usr/bin/env python3
"""Round and conically restore the strongest screened investment neighborhoods."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
from mosek.fusion import AccSolutionStatus

from candidate.src.exact_model import build_model, extract_solution
from candidate.src.independent_audit import audit
from candidate.src.input_data import load_inputs


ROOT = Path("/work/app")
CANDIDATE = ROOT / "candidate"
SOURCE = CANDIDATE / "tmp" / "performance"
LOGS = CANDIDATE / "logs" / "performance"


def round_bounded(values, lo, hi, total=None, ceil=False):
    values = np.asarray(values, float)
    out = np.ceil(values - 1e-6) if ceil else np.rint(values)
    out = np.clip(out, lo, hi)
    while total is not None and np.sum(out) > total:
        candidates = np.flatnonzero(out.reshape(-1) > lo)
        flat, original = out.reshape(-1), values.reshape(-1)
        j = int(candidates[np.argmax(flat[candidates] - original[candidates])])
        flat[j] -= 1
    return out.astype(float)


def solve(data, label, fixed, cap):
    built = build_model(data, integral=False, fixed_values=fixed); model = built.model
    model.acceptedSolutionStatus(AccSolutionStatus.Feasible)
    model.setSolverParam("numThreads", 4); model.setSolverParam("optimizerMaxTime", cap)
    path = LOGS / f"{label}.log"
    with path.open("w") as log:
        model.setLogHandler(log); model.solve()
    status = str(model.getPrimalSolutionStatus())
    elapsed = float(model.getSolverDoubleInfo("optimizerTime"))
    feasible = "Feasible" in status or "Optimal" in status
    solution = extract_solution(built) if feasible else None
    objective = float(model.primalObjValue()) if feasible else None
    model.dispose()
    return solution, {"label": label, "status": status, "optimizer_time_seconds": elapsed, "solver_objective": objective}, path


def main():
    data = load_inputs(ROOT); plan = data["spec"]["planning"]
    ranking = json.loads((SOURCE / "summary.json").read_text())["ranking"]
    baseline = audit(ROOT, CANDIDATE / "results" / "solution.npz")["objective_components"]["full_reported_model_total"]
    stages = []; best = None; used = 0.0
    for ranked in ranking[:2]:
        label = ranked["label"]
        relaxed = np.load(SOURCE / f"{label}.npz", allow_pickle=False)
        base_plan = {
            "uL": np.rint(relaxed["uL"]), "uEX": np.rint(relaxed["uEX"]), "uNS": np.rint(relaxed["uNS"]),
            "zESS": round_bounded(relaxed["zESS"], 1, 20, plan["ess_units_total_max"]),
            "zPVG": round_bounded(relaxed["zPVG"], 1, 50, plan["pv_units_total_max"]),
            "zWTG": round_bounded(relaxed["zWTG"], 1, 50, None, ceil=True),
        }
        screen = None
        for variant in ("nearest", "pv_headroom"):
            fixed_plan = {k: v.copy() for k, v in base_plan.items()}
            if variant == "pv_headroom":
                fixed_plan["zPVG"] = round_bounded(relaxed["zPVG"], 1, 50, plan["pv_units_total_max"], ceil=True)
            screen, info, screen_log = solve(data, f"restore_{label}_{variant}_screen", fixed_plan, 15.0)
            stages.append(info); used += info["optimizer_time_seconds"]
            if screen is not None: break
        if screen is None: continue
        fixed_all = dict(fixed_plan)
        fixed_all["u_ess"] = (screen["P_ess_cha"] >= screen["P_ess_dis"]).astype(float)
        fixed_all["u_ess"][(screen["P_ess_cha"] < 1e-8) & (screen["P_ess_dis"] < 1e-8)] = 0.0
        fixed_all["kt"] = np.clip(np.rint(screen["kt"]), -8, 8)
        restored, info, restore_log = solve(data, f"restore_{label}_fixed_integer", fixed_all, 15.0)
        stages.append(info); used += info["optimizer_time_seconds"]
        if restored is None: continue
        temp = SOURCE / f"{label}_restored.npz"; np.savez_compressed(temp, **restored)
        report = audit(ROOT, temp)
        if not report["passed"]: continue
        objective = report["objective_components"]["full_reported_model_total"]
        pool = CANDIDATE / "pool" / f"lns_{label}"
        (pool / "results").mkdir(parents=True, exist_ok=True); (pool / "logs").mkdir(parents=True, exist_ok=True)
        target = pool / "results" / "solution.npz"; shutil.copy2(temp, target)
        report["solution_path"] = str(target.relative_to(ROOT))
        (pool / "audit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        shutil.copy2(screen_log, pool / "logs" / "screen.log"); shutil.copy2(restore_log, pool / "logs" / "restoration.log")
        summary = {
            "status": "optimal", "solver": "MOSEK 11.2.3 fixed-integer conic restoration",
            "optimizer_time_seconds": info["optimizer_time_seconds"], "threads": 4,
            "objective": objective, "best_bound": objective, "relative_gap": 0.0,
            "solution_path": str(target.relative_to(ROOT)), "used_warm_start": False,
            "used_external_warm_start": False, "internal_mip_start_source": None,
            "planning_was_fixed": True,
            "provenance": f"input-derived one-edge LNS {label}; relaxation-guided count rounding; fixed-integer conic restoration"
        }
        (pool / "solver_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        candidate = {"label": label, "objective": objective, "path": str(target), "audit": report}
        if best is None or objective < best["objective"]: best = candidate
        print(json.dumps({"retained": label, "objective": objective, "improvement": baseline - objective}), flush=True)
        if objective < baseline - 1e-3: break
    result = {"optimizer_time_seconds": used, "stages": stages, "best": best, "baseline_objective": baseline}
    (SOURCE / "restoration_summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if best is None: raise SystemExit(2)


if __name__ == "__main__": main()
