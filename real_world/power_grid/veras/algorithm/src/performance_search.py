#!/usr/bin/env python3
"""Investment-focused single-edge LNS screens for the verified exact model."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from mosek.fusion import AccSolutionStatus

from candidate.src.exact_model import build_model, extract_solution
from candidate.src.input_data import load_inputs


ROOT = Path("/work/app")
OUT = ROOT / "candidate" / "tmp" / "performance"
LOGS = ROOT / "candidate" / "logs" / "performance"


class DSU:
    def __init__(self, n): self.p = list(range(n))
    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]; a = self.p[a]
        return a
    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b: return False
        self.p[b] = a; return True


def components(data, u):
    d = DSU(54)
    for e in np.flatnonzero(u): d.union(int(data["fr"][e]), int(data["to"][e]))
    roots = data["station_buses"].tolist()
    if len({d.find(r) for r in roots}) != 4: return None
    loads = data["bus"][:, 1]
    return np.asarray([sum(loads[b] for b in range(54) if d.find(b) == d.find(r)) for r in roots])


def neighbor_plans(data, base_u):
    roots = data["station_buses"].tolist(); supernode = 54
    candidate_roots = set(roots[2:])
    mandatory = set(data["existing_rows"].tolist()) | {
        e for e in range(63)
        if int(data["fr"][e]) in candidate_roots or int(data["to"][e]) in candidate_roots
    }
    adj = [[] for _ in range(55)]
    for e in np.flatnonzero(base_u):
        a, b = int(data["fr"][e]), int(data["to"][e])
        adj[a].append((b, e)); adj[b].append((a, e))
    for k, root in enumerate(roots):
        adj[supernode].append((root, 63 + k)); adj[root].append((supernode, 63 + k))

    def path_edges(a, b):
        parent = {a: (-1, -1)}; queue = [a]
        for node in queue:
            if node == b: break
            for nb, edge in adj[node]:
                if nb not in parent:
                    parent[nb] = (node, edge); queue.append(nb)
        ans = []; cur = b
        while cur != a:
            cur, edge = parent[cur]
            ans.append(edge)
        return ans

    station_cap = np.asarray([16.7, 16.7, 11.1, 11.1])
    candidates = []
    for add in np.flatnonzero(1 - base_u):
        for remove in path_edges(int(data["fr"][add]), int(data["to"][add])):
            if remove >= 63 or remove in mandatory: continue
            trial = base_u.copy(); trial[add] = 1; trial[remove] = 0
            loads = components(data, trial)
            if loads is None: continue
            score = float(np.max(loads / station_cap) + 0.02 * np.sum((loads / station_cap) ** 2))
            candidates.append((score, int(add), int(remove), trial, loads))
    candidates.sort(key=lambda q: (q[0], q[1], q[2]))
    # Diverse top-ranked exchanges; avoid spending the budget on near-duplicates.
    selected = []
    used_add = set()
    for item in candidates:
        if item[1] not in used_add or len(selected) < 3:
            selected.append(item); used_add.add(item[1])
        if len(selected) == 8: break
    return selected


def station_options(data, smaller_existing_2=False, smaller_new=None):
    plan = data["spec"]["planning"]
    uex = np.zeros((2, 2)); uns = np.zeros((2, 2))
    for i in range(2):
        uex[i, int(np.argmax(plan["existing_station_expansion_mva"][i]))] = 1
        uns[i, int(np.argmax(plan["candidate_station_sizes_mva"][i]))] = 1
    if smaller_existing_2:
        uex[1] = [1, 0]
    if smaller_new is not None:
        uns[smaller_new] = [1, 0]
    return uex, uns


def main():
    OUT.mkdir(parents=True, exist_ok=True); LOGS.mkdir(parents=True, exist_ok=True)
    data = load_inputs(ROOT)
    base = np.load(ROOT / "candidate" / "results" / "solution.npz", allow_pickle=False)
    base_u = np.rint(base["uL"][:, 0]).astype(int)
    max_ex, max_ns = station_options(data)
    cases = [("baseline", base_u.copy(), max_ex, max_ns, {})]
    for rank, (_, add, remove, u, loads) in enumerate(neighbor_plans(data, base_u), 1):
        cases.append((f"swap_{rank:02d}_add{add+1}_remove{remove+1}", u, max_ex, max_ns,
                      {"add_line": add + 1, "remove_line": remove + 1, "component_base_load_mw": loads.tolist()}))
    ex_small, ns = station_options(data, smaller_existing_2=True)
    cases.append(("station_existing52_small", base_u.copy(), ex_small, ns, {}))
    ex, ns_small53 = station_options(data, smaller_new=0)
    cases.append(("station_candidate53_small", base_u.copy(), ex, ns_small53, {}))
    ex, ns_small54 = station_options(data, smaller_new=1)
    cases.append(("station_candidate54_small", base_u.copy(), ex, ns_small54, {}))

    reports = []; used = 0.0; max_optimizer = 48.0
    for label, u, uex, uns, meta in cases:
        if used >= max_optimizer - 1.0: break
        fixed = {"uL": u[:, None].astype(float), "uEX": uex, "uNS": uns}
        built = build_model(data, integral=False, fixed_values=fixed); model = built.model
        model.acceptedSolutionStatus(AccSolutionStatus.Feasible)
        model.setSolverParam("numThreads", 4)
        model.setSolverParam("optimizerMaxTime", min(10.0, max_optimizer - used))
        log_path = LOGS / f"{label}.log"
        with log_path.open("w") as log:
            model.setLogHandler(log); model.solve()
        status = str(model.getPrimalSolutionStatus())
        elapsed = float(model.getSolverDoubleInfo("optimizerTime")); used += elapsed
        feasible = "Feasible" in status or "Optimal" in status
        row = {"label": label, "primal_status": status, "optimizer_time_seconds": elapsed, **meta}
        if feasible:
            row["relaxed_objective"] = float(model.primalObjValue())
            np.savez_compressed(OUT / f"{label}.npz", **extract_solution(built))
        reports.append(row); model.dispose()
        print(json.dumps(row, sort_keys=True), flush=True)
    feasible_rows = sorted((r for r in reports if "relaxed_objective" in r), key=lambda r: r["relaxed_objective"])
    summary = {"optimizer_time_seconds": used, "cases": reports,
               "ranking": [{"label": r["label"], "relaxed_objective": r["relaxed_objective"]} for r in feasible_rows]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary["ranking"], indent=2))


if __name__ == "__main__": main()
