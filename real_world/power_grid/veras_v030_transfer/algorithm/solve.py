#!/usr/bin/env python3
"""Solve the exact 54-bus free-planning MISOCP and audit the exported NPZ."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import openpyxl
import mosek
from mosek.fusion import (
    AccSolutionStatus,
    Domain,
    Expr,
    Model,
    ObjectiveSense,
    SolutionError,
    SolutionStatus,
    SolutionType,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "submissions"
WORKBOOK = DATA_DIR / "line_load.xlsx"
SPEC_FILE = DATA_DIR / "model_spec.json"
SOLUTION_PATH = OUT_DIR / "solution.npz"
MANIFEST_PATH = OUT_DIR / "solution.json"
SUMMARY_PATH = OUT_DIR / "solver_summary.json"
LOG_PATH = OUT_DIR / "solve_log.md"
CANDIDATE_PATH = OUT_DIR / "candidate_fixed_screen.npz"
FULL_TEMP_PATH = OUT_DIR / "solution_full_temp.npz"

THREADS = 4
DEFAULT_LIMIT = 900.0
REL_GAP = 0.0005
OBJ_SCALE = 1.0e6
BRANCH_SOC_MARGIN = 1.0e-6

REQUIRED_SHAPES = {
    "uL": (63, 1),
    "uNS": (2, 2),
    "uEX": (2, 2),
    "zESS": (7, 1),
    "zPVG": (7, 1),
    "zWTG": (6, 1),
    "u_ess": (7, 96),
    "kt": (4, 96),
    "FF": (63, 4),
    "FS": (54, 4),
    "P": (63, 96),
    "Q": (63, 96),
    "l": (63, 96),
    "v": (54, 96),
    "P_s": (4, 96),
    "Q_s": (4, 96),
    "P_pv": (7, 96),
    "Q_pv": (7, 96),
    "P_wt": (6, 96),
    "Q_wt": (6, 96),
    "E_ess": (7, 96),
    "P_ess": (7, 96),
    "P_ess_cha": (7, 96),
    "P_ess_dis": (7, 96),
    "P_sop_1": (4, 96),
    "P_sop_2": (4, 96),
    "Q_sop_1": (4, 96),
    "Q_sop_2": (4, 96),
    "Pl_sop_1": (4, 96),
    "Pl_sop_2": (4, 96),
    "P_ls": (54, 96),
}


def load_public_data() -> dict:
    with SPEC_FILE.open("r", encoding="utf-8") as f:
        spec = json.load(f)
    wb = openpyxl.load_workbook(WORKBOOK, data_only=True, read_only=True)
    ws1 = wb["Sheet1"]
    ws3 = wb["Sheet3"]

    branches = np.array(
        [[ws1.cell(r, c).value for c in range(1, 5)] for r in range(2, 65)],
        dtype=float,
    )
    buses = np.array(
        [[ws1.cell(r, c).value for c in range(7, 9)] for r in range(2, 56)],
        dtype=float,
    )
    forecast = np.array(
        [[ws3.cell(r, c).value for c in range(2, 5)] for r in range(2, 98)],
        dtype=float,
    )
    if not np.array_equal(branches[:, 0], np.arange(1, 64)):
        raise ValueError("Workbook branch identifiers are not 1..63")
    if not np.array_equal(buses[:, 0], np.arange(1, 55)):
        raise ValueError("Workbook bus identifiers are not 1..54")
    if forecast.shape != (96, 3) or not np.isfinite(forecast).all():
        raise ValueError("Invalid forecast range")

    fr = branches[:, 1].astype(int) - 1
    to = branches[:, 2].astype(int) - 1
    length = branches[:, 3].astype(float)
    base_load = buses[:, 1].astype(float)
    wt_fc, pv_fc, pl_fc = forecast.T

    nbus, nedge, nh = 54, 63, 96
    A_fr = np.zeros((nbus, nedge))
    A_to = np.zeros((nbus, nedge))
    A_fr[fr, np.arange(nedge)] = 1.0
    A_to[to, np.arange(nedge)] = 1.0

    nw = spec["network"]
    op = spec["operation"]
    pl = spec["planning"]
    eco = spec["economics"]
    sbase = float(nw["s_base_mva"])
    zbase = float(nw["v_base_kv"]) ** 2 / sbase
    ibase = sbase / float(nw["v_base_kv"])
    imax = float(nw["line_current_limit_ka"]) / ibase
    tanphi = math.tan(math.acos(float(nw["load_power_factor"])))
    qfac = math.sqrt(1.0 - float(nw["dg_min_power_factor"]) ** 2) / float(
        nw["dg_min_power_factor"]
    )
    R = float(nw["resistance_ohm_per_km"]) * length / zbase
    X = float(nw["reactance_ohm_per_km"]) * length / zbase
    PL = base_load[:, None] * pl_fc[None, :] / sbase
    QL = PL * tanphi
    prices = np.tile(np.asarray(op["purchase_price_usd_per_mwh"], dtype=float), 4)

    existing = np.asarray(spec["sets"]["existing_line_rows"], dtype=int) - 1
    faults = np.asarray(spec["sets"]["fault_line_rows"], dtype=int) - 1
    sop_edges = np.asarray(spec["sets"]["sop_line_rows"], dtype=int) - 1
    mask = np.ones((nedge, nh))
    fault_hours = np.array([6, 7], dtype=int)
    mask[np.ix_(faults, fault_hours)] = 0.0
    shed_mask = np.zeros((nbus, nh))
    shed_mask[:, fault_hours] = 1.0
    derate = np.ones((4, nh))
    derate[np.ix_([0, 1], fault_hours)] = float(
        spec["fault"]["substation_remaining_capacity_fraction"]
    )

    years = int(pl["years"])
    rate = float(pl["discount_rate"])
    crf = rate * (1.0 + rate) ** years / ((1.0 + rate) ** years - 1.0)
    ess_unit_pu = np.full(7, float(op["ess_unit_mwh_non_idc"]) / sbase)
    ess_unit_pu[-1] = float(op["ess_unit_mwh_idc"]) / sbase

    return {
        "spec": spec,
        "fr": fr,
        "to": to,
        "length": length,
        "base_load": base_load,
        "wt_fc": wt_fc,
        "pv_fc": pv_fc,
        "pl_fc": pl_fc,
        "A_fr": A_fr,
        "A_to": A_to,
        "R": R,
        "X": X,
        "PL": PL,
        "QL": QL,
        "prices": prices,
        "existing": existing,
        "faults": faults,
        "sop_edges": sop_edges,
        "mask": mask,
        "shed_mask": shed_mask,
        "derate": derate,
        "tanphi": tanphi,
        "qfac": qfac,
        "crf": crf,
        "ess_unit_pu": ess_unit_pu,
        "sbase": sbase,
        "vmin2": float(nw["v_min_pu"]) ** 2,
        "vmax2": float(nw["v_max_pu"]) ** 2,
        "imax2": imax**2,
        "sline": imax * float(nw["v_max_pu"]),
        "M": float(nw["big_m"]),
        "sdg": float(pl["dg_unit_mw"]) / sbase,
    }


def flat(i: int, j: int, ncol: int) -> int:
    return i * ncol + j


def add_terms(terms):
    if len(terms) == 1:
        return terms[0]
    return Expr.add(terms)


def scaled(var, coefficient: float):
    return Expr.mul(float(coefficient), var)


def build_model(
    data: dict,
    name: str,
    time_limit: float,
    fixed_plan: dict | None = None,
    fixed_discrete: dict | None = None,
    initial_levels: dict | None = None,
    relax_integrality: bool = False,
    verbose: bool = True,
):
    """Build the exact model. Fixed values are used only by the internal screen."""
    spec = data["spec"]
    pl = spec["planning"]
    op = spec["operation"]
    eco = spec["economics"]
    Mbig = data["M"]
    nedge, nbus, nh, nsc = 63, 54, 96, 4

    model = Model(name)
    # Time-limited MISOCP solves commonly return a proven feasible incumbent
    # without an optimality certificate.  Fusion otherwise refuses access to
    # those variable levels even though MOSEK reports a valid integer point.
    model.acceptedSolutionStatus(AccSolutionStatus.Feasible)
    V = {}
    temporary_fixes = []
    binary_domain = Domain.inRange(0.0, 1.0) if relax_integrality else Domain.binary()
    int_domain = lambda lo, hi: Domain.inRange(lo, hi) if relax_integrality else Domain.integral(Domain.inRange(lo, hi))
    V["uL"] = model.variable("uL", nedge, binary_domain)
    V["uNS"] = model.variable("uNS", 4, binary_domain)
    V["uEX"] = model.variable("uEX", 4, binary_domain)
    V["zESS"] = model.variable("zESS", 7, int_domain(1.0, 20.0))
    V["zPVG"] = model.variable("zPVG", 7, int_domain(1.0, 50.0))
    V["zWTG"] = model.variable("zWTG", 6, int_domain(1.0, 50.0))
    V["u_ess"] = model.variable("u_ess", 7 * nh, binary_domain)
    V["kt"] = model.variable("kt", 4 * nh, int_domain(-8.0, 8.0))
    V["FF"] = model.variable("FF", nedge * nsc, Domain.unbounded())
    V["FS"] = model.variable("FS", nbus * nsc, Domain.greaterThan(0.0))
    V["P"] = model.variable("P", nedge * nh, Domain.unbounded())
    V["Q"] = model.variable("Q", nedge * nh, Domain.unbounded())
    V["l"] = model.variable("l", nedge * nh, Domain.greaterThan(0.0))
    V["v"] = model.variable("v", nbus * nh, Domain.unbounded())
    V["P_s"] = model.variable("P_s", 4 * nh, Domain.greaterThan(0.0))
    V["Q_s"] = model.variable("Q_s", 4 * nh, Domain.unbounded())
    V["P_pv"] = model.variable("P_pv", 7 * nh, Domain.unbounded())
    V["Q_pv"] = model.variable("Q_pv", 7 * nh, Domain.unbounded())
    V["P_wt"] = model.variable("P_wt", 6 * nh, Domain.unbounded())
    V["Q_wt"] = model.variable("Q_wt", 6 * nh, Domain.unbounded())
    V["E_ess"] = model.variable("E_ess", 7 * nh, Domain.unbounded())
    V["P_ess"] = model.variable("P_ess", 7 * nh, Domain.unbounded())
    V["P_ess_cha"] = model.variable("P_ess_cha", 7 * nh, Domain.greaterThan(0.0))
    V["P_ess_dis"] = model.variable("P_ess_dis", 7 * nh, Domain.greaterThan(0.0))
    for key in ["P_sop_1", "P_sop_2", "Q_sop_1", "Q_sop_2", "Pl_sop_1", "Pl_sop_2"]:
        V[key] = model.variable(key, 4 * nh, Domain.unbounded())
    V["P_ls"] = model.variable("P_ls", nbus * nh, Domain.greaterThan(0.0))

    # Fixed values are only used in the independently audited incumbent screen.
    if fixed_plan is not None:
        for key in ["uL", "uNS", "uEX", "zESS", "zPVG", "zWTG"]:
            vals = np.asarray(fixed_plan[key], dtype=float).reshape(-1)
            for idx, val in enumerate(vals):
                temporary_fixes.append(
                    model.constraint(f"fix_{key}_{idx}", V[key].index(idx), Domain.equalsTo(float(val)))
                )
    if fixed_discrete is not None:
        for key in [k for k in ["u_ess", "kt"] if k in fixed_discrete]:
            vals = np.asarray(fixed_discrete[key], dtype=float).reshape(-1)
            for idx, val in enumerate(vals):
                temporary_fixes.append(
                    model.constraint(f"fix_{key}_{idx}", V[key].index(idx), Domain.equalsTo(float(val)))
                )

    # Planning constraints.
    existing_set = set(data["existing"].tolist())
    for e in data["existing"]:
        model.constraint(f"existing_line_{e+1}", V["uL"].index(int(e)), Domain.equalsTo(1.0))
    new_sum = Expr.sum(V["uNS"])
    model.constraint(
        "line_cardinality",
        Expr.add(Expr.sum(V["uL"]), new_sum),
        Domain.equalsTo(52.0),
    )
    model.constraint("ess_total", Expr.sum(V["zESS"]), Domain.lessThan(80.0))
    model.constraint("pv_total", Expr.sum(V["zPVG"]), Domain.lessThan(280.0))
    for i in range(2):
        model.constraint(
            f"ex_option_{i}",
            Expr.add(V["uEX"].index(flat(i, 0, 2)), V["uEX"].index(flat(i, 1, 2))),
            Domain.lessThan(1.0),
        )
        model.constraint(
            f"ns_option_{i}",
            Expr.add(V["uNS"].index(flat(i, 0, 2)), V["uNS"].index(flat(i, 1, 2))),
            Domain.lessThan(1.0),
        )

    # Artificial-flow connectivity. The line status is exactly uL in every scenario.
    inc = data["A_to"] - data["A_fr"]
    for s in range(nsc):
        for e in range(nedge):
            ff = V["FF"].index(flat(e, s, nsc))
            ul = V["uL"].index(e)
            model.constraint(f"ff_hi_{e}_{s}", Expr.sub(ff, scaled(ul, 54.0)), Domain.lessThan(0.0))
            model.constraint(f"ff_lo_{e}_{s}", Expr.add(ff, scaled(ul, 54.0)), Domain.greaterThan(0.0))
        for b in range(nbus):
            terms = [V["FS"].index(flat(b, s, nsc))]
            for e in np.flatnonzero(inc[b]):
                terms.append(scaled(V["FF"].index(flat(int(e), s, nsc)), inc[b, e]))
            model.constraint(f"flow_balance_{b}_{s}", add_terms(terms), Domain.equalsTo(1.0))
        for b in range(50):
            model.constraint(f"nonroot_{b}_{s}", V["FS"].index(flat(b, s, nsc)), Domain.equalsTo(0.0))
        root_gates = [
            [V["uEX"].index(0), V["uEX"].index(1)],
            [V["uEX"].index(2), V["uEX"].index(3)],
            [V["uNS"].index(0), V["uNS"].index(1)],
            [V["uNS"].index(2), V["uNS"].index(3)],
        ]
        for r in range(4):
            gate = Expr.add(root_gates[r][0], root_gates[r][1])
            model.constraint(
                f"root_gate_{r}_{s}",
                Expr.sub(V["FS"].index(flat(50 + r, s, nsc)), scaled(gate, 54.0)),
                Domain.lessThan(0.0),
            )

    def station_cap(r):
        if r == 0:
            return Expr.add(
                1.67,
                Expr.add(scaled(V["uEX"].index(0), 1.33), scaled(V["uEX"].index(1), 1.67)),
            )
        if r == 1:
            return Expr.add(
                1.67,
                Expr.add(scaled(V["uEX"].index(2), 1.33), scaled(V["uEX"].index(3), 1.67)),
            )
        if r == 2:
            return Expr.add(scaled(V["uNS"].index(0), 1.67), scaled(V["uNS"].index(1), 2.22))
        return Expr.add(scaled(V["uNS"].index(2), 1.67), scaled(V["uNS"].index(3), 2.22))

    # Station cones and voltage/OLTC constraints.
    for r in range(4):
        for h in range(nh):
            ps = V["P_s"].index(flat(r, h, nh))
            qs = V["Q_s"].index(flat(r, h, nh))
            rhs = scaled(station_cap(r), 0.5 * data["derate"][r, h])
            model.constraint(f"station_cone_{r}_{h}", Expr.vstack(rhs, ps, qs), Domain.inQCone())
    for b in range(50):
        for h in range(nh):
            model.constraint(
                f"v_bounds_{b}_{h}",
                V["v"].index(flat(b, h, nh)),
                Domain.inRange(data["vmin2"], data["vmax2"]),
            )
    for r in range(2):
        b = 50 + r
        for h in range(nh):
            expr = Expr.sub(V["v"].index(flat(b, h, nh)), scaled(V["kt"].index(flat(r, h, nh)), 0.025))
            model.constraint(f"existing_tap_{r}_{h}", expr, Domain.equalsTo(1.01))
    for i in range(2):
        r = 2 + i
        b = 52 + i
        n_on = Expr.add(V["uNS"].index(flat(i, 0, 2)), V["uNS"].index(flat(i, 1, 2)))
        for h in range(nh):
            vv = V["v"].index(flat(b, h, nh))
            kk = V["kt"].index(flat(r, h, nh))
            model.constraint(
                f"new_tap_lo_{i}_{h}",
                add_terms([vv, scaled(kk, -0.025), scaled(n_on, -Mbig)]),
                Domain.greaterThan(1.01 - Mbig),
            )
            model.constraint(
                f"new_tap_hi_{i}_{h}",
                add_terms([vv, scaled(kk, -0.025), scaled(n_on, Mbig)]),
                Domain.lessThan(1.01 + Mbig),
            )
            model.constraint(
                f"new_v_lo_{i}_{h}", Expr.add(vv, scaled(n_on, Mbig)), Domain.greaterThan(data["vmin2"])
            )
            model.constraint(
                f"new_v_hi_{i}_{h}", Expr.sub(vv, scaled(n_on, Mbig)), Domain.lessThan(data["vmax2"])
            )
            model.constraint(f"new_kt_lo_{i}_{h}", Expr.add(kk, scaled(n_on, 8.0)), Domain.greaterThan(0.0))
            model.constraint(f"new_kt_hi_{i}_{h}", Expr.sub(kk, scaled(n_on, 8.0)), Domain.lessThan(0.0))

    # Branch status, voltage drops, and branch-flow cones.
    for e in range(nedge):
        ul = V["uL"].index(e)
        for h in range(nh):
            pp = V["P"].index(flat(e, h, nh))
            qq = V["Q"].index(flat(e, h, nh))
            ll = V["l"].index(flat(e, h, nh))
            vf = V["v"].index(flat(int(data["fr"][e]), h, nh))
            vt = V["v"].index(flat(int(data["to"][e]), h, nh))
            if data["mask"][e, h] == 0.0:
                model.constraint(f"fault_P_{e}_{h}", pp, Domain.equalsTo(0.0))
                model.constraint(f"fault_Q_{e}_{h}", qq, Domain.equalsTo(0.0))
                model.constraint(f"fault_l_{e}_{h}", ll, Domain.equalsTo(0.0))
                yterm = None
            else:
                model.constraint(f"P_hi_{e}_{h}", Expr.sub(pp, scaled(ul, data["sline"])), Domain.lessThan(0.0))
                model.constraint(f"P_lo_{e}_{h}", Expr.add(pp, scaled(ul, data["sline"])), Domain.greaterThan(0.0))
                model.constraint(f"Q_hi_{e}_{h}", Expr.sub(qq, scaled(ul, data["sline"])), Domain.lessThan(0.0))
                model.constraint(f"Q_lo_{e}_{h}", Expr.add(qq, scaled(ul, data["sline"])), Domain.greaterThan(0.0))
                model.constraint(f"l_hi_{e}_{h}", Expr.sub(ll, scaled(ul, data["imax2"])), Domain.lessThan(0.0))
                yterm = ul
            model.constraint(
                f"branch_cone_{e}_{h}",
                Expr.vstack([
                    Expr.sub(
                        Expr.add(vf, ll),
                        scaled(ul, BRANCH_SOC_MARGIN * data["mask"][e, h]),
                    ),
                    scaled(pp, 2.0),
                    scaled(qq, 2.0),
                    Expr.sub(vf, ll),
                ]),
                Domain.inQCone(),
            )
            drop = add_terms(
                [vf, scaled(vt, -1.0), scaled(pp, -2.0 * data["R"][e]),
                 scaled(qq, -2.0 * data["X"][e]),
                 scaled(ll, data["R"][e] ** 2 + data["X"][e] ** 2)]
            )
            if yterm is None:
                model.constraint(f"drop_hi_{e}_{h}", drop, Domain.lessThan(Mbig))
                model.constraint(f"drop_lo_{e}_{h}", drop, Domain.greaterThan(-Mbig))
            else:
                model.constraint(f"drop_hi_{e}_{h}", Expr.add(drop, scaled(yterm, Mbig)), Domain.lessThan(Mbig))
                model.constraint(f"drop_lo_{e}_{h}", Expr.sub(drop, scaled(yterm, Mbig)), Domain.greaterThan(-Mbig))

    # Renewable generation.
    for kind, nsite, fc, zkey, pkey, qkey in [
        ("pv", 7, data["pv_fc"], "zPVG", "P_pv", "Q_pv"),
        ("wt", 6, data["wt_fc"], "zWTG", "P_wt", "Q_wt"),
    ]:
        for i in range(nsite):
            zz = V[zkey].index(i)
            cap = scaled(zz, data["sdg"])
            for h in range(nh):
                pg = V[pkey].index(flat(i, h, nh))
                qg = V[qkey].index(flat(i, h, nh))
                avail = scaled(zz, data["sdg"] * fc[h])
                model.constraint(f"{kind}_max_{i}_{h}", Expr.sub(pg, avail), Domain.lessThan(0.0))
                model.constraint(
                    f"{kind}_min_{i}_{h}", Expr.sub(pg, scaled(avail, float(op["dg_min_dispatch_fraction"]))), Domain.greaterThan(0.0)
                )
                model.constraint(f"{kind}_cone_{i}_{h}", Expr.vstack(cap, pg, qg), Domain.inQCone())
                model.constraint(f"{kind}_qhi_{i}_{h}", Expr.sub(qg, scaled(pg, data["qfac"])), Domain.lessThan(0.0))
                model.constraint(f"{kind}_qlo_{i}_{h}", Expr.add(qg, scaled(pg, data["qfac"])), Domain.greaterThan(0.0))

    # Storage constraints and authoritative wrap-around chronology.
    eta = float(op["ess_efficiency"])
    p_ratio = float(op["ess_power_to_energy_ratio"])
    for i in range(7):
        zz = V["zESS"].index(i)
        cap = scaled(zz, data["ess_unit_pu"][i])
        pmax = scaled(cap, p_ratio)
        max_p_const = p_ratio * data["ess_unit_pu"][i] * 20.0
        for h in range(nh):
            pe = V["P_ess"].index(flat(i, h, nh))
            pc = V["P_ess_cha"].index(flat(i, h, nh))
            pd = V["P_ess_dis"].index(flat(i, h, nh))
            ee = V["E_ess"].index(flat(i, h, nh))
            uu = V["u_ess"].index(flat(i, h, nh))
            model.constraint(f"ess_net_{i}_{h}", add_terms([pe, scaled(pd, -1.0), pc]), Domain.equalsTo(0.0))
            model.constraint(f"ess_pc_cap_{i}_{h}", Expr.sub(pc, pmax), Domain.lessThan(0.0))
            model.constraint(f"ess_pd_cap_{i}_{h}", Expr.sub(pd, pmax), Domain.lessThan(0.0))
            model.constraint(f"ess_pc_mode_{i}_{h}", Expr.sub(pc, scaled(uu, Mbig)), Domain.lessThan(0.0))
            model.constraint(f"ess_pd_mode_{i}_{h}", Expr.add(pd, scaled(uu, Mbig)), Domain.lessThan(Mbig))
            # Equivalent bound-strengthening inequalities using the known unit maximum.
            model.constraint(f"ess_pc_tight_{i}_{h}", Expr.sub(pc, scaled(uu, max_p_const)), Domain.lessThan(0.0))
            model.constraint(f"ess_pd_tight_{i}_{h}", Expr.add(pd, scaled(uu, max_p_const)), Domain.lessThan(max_p_const))
            model.constraint(f"ess_E_lo_{i}_{h}", Expr.sub(ee, scaled(cap, float(op["ess_soc_min"]))), Domain.greaterThan(0.0))
            model.constraint(f"ess_E_hi_{i}_{h}", Expr.sub(ee, cap), Domain.lessThan(0.0))
        for s in range(4):
            h0 = 24 * s
            for t in range(23):
                h = h0 + t
                expr = add_terms([
                    V["E_ess"].index(flat(i, h + 1, nh)),
                    scaled(V["E_ess"].index(flat(i, h, nh)), -1.0),
                    scaled(V["P_ess_cha"].index(flat(i, h, nh)), -eta),
                    scaled(V["P_ess_dis"].index(flat(i, h, nh)), 1.0 / eta),
                ])
                model.constraint(f"ess_step_{i}_{s}_{t}", expr, Domain.equalsTo(0.0))
            h24 = h0 + 23
            wrap = add_terms([
                V["E_ess"].index(flat(i, h0, nh)),
                scaled(V["P_ess_cha"].index(flat(i, h24, nh)), -eta),
                scaled(V["P_ess_dis"].index(flat(i, h24, nh)), 1.0 / eta),
                scaled(cap, -float(op["ess_soc_initial"])),
            ])
            terminal = Expr.sub(
                V["E_ess"].index(flat(i, h24, nh)),
                scaled(cap, float(op["ess_soc_initial"])),
            )
            model.constraint(f"ess_wrap_{i}_{s}", wrap, Domain.equalsTo(0.0))
            model.constraint(f"ess_terminal_{i}_{s}", terminal, Domain.equalsTo(0.0))

    # SOP endpoint constraints, two cone families, and loss balance.
    z1 = np.asarray(pl["sop_units_endpoint_1"], dtype=float)
    z2 = np.asarray(pl["sop_units_endpoint_2"], dtype=float)
    for m in range(4):
        for h in range(nh):
            for k, zfixed in [(1, z1[m]), (2, z2[m])]:
                sk = float(zfixed) * float(pl["sop_unit_mva"]) / data["sbase"]
                pp = V[f"P_sop_{k}"].index(flat(m, h, nh))
                qq = V[f"Q_sop_{k}"].index(flat(m, h, nh))
                loss = V[f"Pl_sop_{k}"].index(flat(m, h, nh))
                model.constraint(f"sop_P_{m}_{h}_{k}", pp, Domain.inRange(-sk, sk))
                model.constraint(f"sop_Q_{m}_{h}_{k}", qq, Domain.inRange(0.0, float(op["sop_q_upper_fraction"]) * sk))
                model.constraint(f"sop_Pl_{m}_{h}_{k}", loss, Domain.inRange(0.0, float(op["sop_loss_factor"]) * sk))
                model.constraint(
                    f"sop_loss_cone_{m}_{h}_{k}",
                    Expr.vstack(scaled(loss, 1.0 / float(op["sop_loss_factor"])), pp, qq),
                    Domain.inQCone(),
                )
                model.constraint(f"sop_cap_cone_{m}_{h}_{k}", Expr.vstack(sk, pp, qq), Domain.inQCone())
            model.constraint(
                f"sop_balance_{m}_{h}",
                add_terms([
                    V["P_sop_1"].index(flat(m, h, nh)),
                    V["P_sop_2"].index(flat(m, h, nh)),
                    V["Pl_sop_1"].index(flat(m, h, nh)),
                    V["Pl_sop_2"].index(flat(m, h, nh)),
                ]),
                Domain.equalsTo(0.0),
            )

    # Load shedding bounds.
    for b in range(nbus):
        for h in range(nh):
            upper = data["PL"][b, h] * data["shed_mask"][b, h]
            model.constraint(f"shed_{b}_{h}", V["P_ls"].index(flat(b, h, nh)), Domain.lessThan(float(upper)))
    for h in [6, 7]:
        model.constraint(
            f"idc_service_{h}", V["P_ls"].index(flat(49, h, nh)), Domain.lessThan(0.10 * data["PL"][49, h])
        )

    # Sparse incidence lists for nodal balances.
    outgoing = [np.flatnonzero(data["A_fr"][b]).tolist() for b in range(nbus)]
    incoming = [np.flatnonzero(data["A_to"][b]).tolist() for b in range(nbus)]
    wt_buses = [x - 1 for x in spec["sets"]["wind_buses"]]
    pv_buses = [x - 1 for x in spec["sets"]["pv_buses"]]
    ess_buses = [x - 1 for x in spec["sets"]["ess_buses"]]
    wt_at = {b: i for i, b in enumerate(wt_buses)}
    pv_at = {b: i for i, b in enumerate(pv_buses)}
    ess_at = {b: i for i, b in enumerate(ess_buses)}
    sop_p1_at = {}
    sop_p2_at = {}
    for m, e in enumerate(data["sop_edges"]):
        sop_p1_at.setdefault(int(data["fr"][e]), []).append(m)
        sop_p2_at.setdefault(int(data["to"][e]), []).append(m)

    for b in range(nbus):
        for h in range(nh):
            pterms = [V["P_ls"].index(flat(b, h, nh))]
            qterms = [scaled(V["P_ls"].index(flat(b, h, nh)), data["tanphi"])]
            for e in outgoing[b]:
                pterms.append(scaled(V["P"].index(flat(e, h, nh)), -1.0))
                qterms.append(scaled(V["Q"].index(flat(e, h, nh)), -1.0))
            for e in incoming[b]:
                pterms.append(V["P"].index(flat(e, h, nh)))
                pterms.append(scaled(V["l"].index(flat(e, h, nh)), data["R"][e]))
                qterms.append(V["Q"].index(flat(e, h, nh)))
                qterms.append(scaled(V["l"].index(flat(e, h, nh)), data["X"][e]))
            if 50 <= b <= 53:
                r = b - 50
                pterms.append(V["P_s"].index(flat(r, h, nh)))
                qterms.append(V["Q_s"].index(flat(r, h, nh)))
            if b in wt_at:
                i = wt_at[b]
                pterms.append(V["P_wt"].index(flat(i, h, nh)))
                qterms.append(V["Q_wt"].index(flat(i, h, nh)))
            if b in pv_at:
                i = pv_at[b]
                pterms.append(V["P_pv"].index(flat(i, h, nh)))
                qterms.append(V["Q_pv"].index(flat(i, h, nh)))
            if b in ess_at:
                pterms.append(V["P_ess"].index(flat(ess_at[b], h, nh)))
            for m in sop_p1_at.get(b, []):
                pterms.append(V["P_sop_1"].index(flat(m, h, nh)))
                qterms.append(V["Q_sop_1"].index(flat(m, h, nh)))
            for m in sop_p2_at.get(b, []):
                pterms.append(V["P_sop_2"].index(flat(m, h, nh)))
                qterms.append(V["Q_sop_2"].index(flat(m, h, nh)))
            model.constraint(f"P_balance_{b}_{h}", add_terms(pterms), Domain.equalsTo(float(data["PL"][b, h])))
            model.constraint(f"Q_balance_{b}_{h}", add_terms(qterms), Domain.equalsTo(float(data["QL"][b, h])))

    # Complete objective, scaled only for numerical conditioning.
    uex_cost = np.asarray(pl["existing_station_option_cost_usd"], dtype=float).reshape(-1)
    uns_cost = np.asarray(pl["candidate_station_option_cost_usd"], dtype=float).reshape(-1)
    line_inv = np.asarray(data["length"] * float(pl["line_build_cost_usd_per_km"]), dtype=float)
    line_inv[list(existing_set)] = 0.0
    ess_mwh = data["ess_unit_pu"] * data["sbase"]
    sop_total_units = float(np.sum(z1) + np.sum(z2))
    constant_usd = data["crf"] * float(eco["sop_investment_usd_per_mva"]) * float(pl["sop_unit_mva"]) * sop_total_units
    constant_usd += float(eco["sop_maintenance_fraction"]) * float(eco["sop_investment_usd_per_mva"]) * float(pl["sop_unit_mva"]) * sop_total_units

    coeff = {}
    coeff["uEX"] = data["crf"] * uex_cost
    coeff["uNS"] = data["crf"] * uns_cost
    coeff["uL"] = data["crf"] * line_inv + data["length"] * 1000.0
    coeff["zESS"] = data["crf"] * float(eco["ess_investment_usd_per_mwh"]) * ess_mwh
    coeff["zPVG"] = np.full(7, data["crf"] * float(eco["pv_investment_usd_per_mw"]) * 0.1)
    coeff["zPVG"] += 0.1 * float(eco["pv_maintenance_usd_per_mwh"]) * float(eco["pv_capacity_factor"]) * 8760.0
    coeff["zWTG"] = np.full(6, data["crf"] * float(eco["wind_investment_usd_per_mw"]) * 0.1)
    coeff["zWTG"] += 0.1 * float(eco["wind_maintenance_usd_per_mwh"]) * float(eco["wind_capacity_factor"]) * 8760.0
    annual = float(eco["scenario_day_annualization_factor"])
    curtail = float(eco["curtailment_cost_usd_per_mwh"])
    coeff["zPVG"] += annual * data["sbase"] * curtail * data["sdg"] * np.sum(data["pv_fc"])
    coeff["zWTG"] += annual * data["sbase"] * curtail * data["sdg"] * np.sum(data["wt_fc"])
    coeff["P_s"] = np.tile(annual * data["sbase"] * data["prices"], 4)
    coeff["P_pv"] = np.full(7 * nh, -annual * data["sbase"] * curtail)
    coeff["P_wt"] = np.full(6 * nh, -annual * data["sbase"] * curtail)
    coeff["P_ls"] = np.full(nbus * nh, annual * data["sbase"] * float(eco["load_shed_cost_usd_per_mwh"]))
    obj_terms = [Expr.dot((coeff[key] / OBJ_SCALE).tolist(), V[key]) for key in coeff]
    objective = Expr.add(add_terms(obj_terms), constant_usd / OBJ_SCALE)
    model.objective(ObjectiveSense.Minimize, objective)

    model.setSolverParam("numThreads", THREADS)
    model.setSolverParam("mioTolRelGap", REL_GAP)
    model.setSolverParam("mioTolFeas", 1.0e-8)
    model.setSolverParam("intpntCoTolPfeas", 1.0e-8)
    model.setSolverParam("intpntCoTolDfeas", 1.0e-8)
    model.setSolverParam("intpntCoTolRelGap", 1.0e-8)
    model.setSolverParam("mioMaxTime", max(1.0, float(time_limit)))
    model.setSolverParam("optimizerMaxTime", max(1.0, float(time_limit)))
    model.setSolverParam("mioHeuristicLevel", 4)
    model.setSolverParam("mioFeaspumpLevel", 2)
    model.setSolverParam("mioSeed", 42)
    model.setSolverParam("log", 2 if verbose else 0)
    if verbose:
        model.setLogHandler(sys.stdout)
    if initial_levels is not None:
        for key, var in V.items():
            if key in initial_levels:
                var.setLevel(np.asarray(initial_levels[key], dtype=float).reshape(-1).tolist())
        model.setSolverParam("mioConstructSol", "on")
    return model, V, temporary_fixes


def extract_solution(V: dict) -> dict:
    arrays = {}
    for key, shape in REQUIRED_SHAPES.items():
        vals = np.asarray(V[key].level(), dtype=np.float64)
        arrays[key] = vals.reshape(shape)
    return arrays


def solver_info(model: Model) -> dict:
    def dinfo(name, default=None):
        try:
            return float(model.getSolverDoubleInfo(name))
        except Exception:
            return default

    def iinfo(name, default=None):
        try:
            return int(model.getSolverIntInfo(name))
        except Exception:
            return default

    try:
        primal_obj = float(model.primalObjValue()) if has_primal(model) else None
    except Exception:
        primal_obj = None
    return {
        "primal_status": str(model.getPrimalSolutionStatus()),
        "problem_status": str(model.getProblemStatus()),
        "optimizer_time": dinfo("optimizerTime", 0.0),
        "mio_time": dinfo("mioTime", 0.0),
        "objective_scaled": dinfo("mioObjInt"),
        "primal_objective_scaled": primal_obj,
        "best_bound_scaled": dinfo("mioObjBound"),
        "relative_gap": dinfo("mioObjRelGap"),
        "integer_solutions": iinfo("mioNumIntSolutions", 0),
        "nodes": iinfo("mioNumSolvedNodes", 0),
    }


def has_primal(model: Model) -> bool:
    try:
        return model.getPrimalSolutionStatus() in (SolutionStatus.Optimal, SolutionStatus.Feasible)
    except Exception:
        return False


class DSU:
    def __init__(self, n, roots):
        self.p = list(range(n))
        self.rank = [0] * n
        self.root_count = [1 if i in roots else 0 for i in range(n)]

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b, allow_two_roots=False):
        a, b = self.find(a), self.find(b)
        if a == b:
            return False
        if not allow_two_roots and self.root_count[a] + self.root_count[b] > 1:
            return False
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.p[b] = a
        self.root_count[a] += self.root_count[b]
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1
        return True


def rooted_forest(data: dict, roots: list[int], priority: np.ndarray | None = None) -> np.ndarray:
    """Minimum-length forest containing mandatory edges and one root/component."""
    dsu = DSU(54, set(roots))
    selected = np.zeros(63, dtype=float)
    for e in data["existing"]:
        a, b = int(data["fr"][e]), int(data["to"][e])
        if not dsu.union(a, b):
            raise RuntimeError("Mandatory lines create a cycle or merge active roots")
        selected[e] = 1.0
    if priority is None:
        order = sorted(
            [e for e in range(63) if selected[e] == 0.0],
            key=lambda e: (data["length"][e], e),
        )
    else:
        priority = np.asarray(priority, dtype=float).reshape(63)
        order = sorted(
            [e for e in range(63) if selected[e] == 0.0],
            key=lambda e: (-priority[e], data["length"][e], e),
        )
    target = 54 - len(roots)
    for e in order:
        if int(selected.sum()) >= target:
            break
        if dsu.union(int(data["fr"][e]), int(data["to"][e])):
            selected[e] = 1.0
    if int(selected.sum()) != target:
        raise RuntimeError("Could not construct the required rooted forest")
    comps = {}
    for b in range(54):
        comps.setdefault(dsu.find(b), []).append(b)
    if len(comps) != len(roots) or any(dsu.root_count[dsu.find(nodes[0])] != 1 for nodes in comps.values()):
        raise RuntimeError("Rooted-forest validation failed")
    return selected


def balance_candidate_forest(data: dict, selected: np.ndarray, roots: list[int]) -> np.ndarray:
    """Exchange optional tree edges to balance conservative component supply."""
    forced = set(int(e) for e in data["existing"])
    root_capacity_mw = {50: 16.7, 51: 16.7, 52: 11.1, 53: 11.1}
    wind_buses = [x - 1 for x in data["spec"]["sets"]["wind_buses"]]
    pv_buses = [x - 1 for x in data["spec"]["sets"]["pv_buses"]]

    def components(sel):
        adj = [[] for _ in range(54)]
        for e in np.flatnonzero(sel > 0.5):
            a, b = int(data["fr"][e]), int(data["to"][e])
            adj[a].append((b, int(e)))
            adj[b].append((a, int(e)))
        owner = {}
        comps = []
        for root in roots:
            stack = [root]
            nodes = []
            while stack:
                b = stack.pop()
                if b in owner:
                    continue
                owner[b] = root
                nodes.append(b)
                stack.extend(q for q, _ in adj[b] if q not in owner)
            comps.append((root, nodes))
        return adj, comps, owner

    def score(sel):
        _, comps, _ = components(sel)
        deficits = []
        for root, nodes in comps:
            load = np.sum(data["base_load"][nodes]) * data["pl_fc"]
            generation = np.zeros(96)
            for b in wind_buses:
                if b in nodes:
                    generation += 5.0 * data["wt_fc"]  # 50 units * 0.1 MW
            for b in pv_buses:
                if b in nodes:
                    generation += 4.0 * data["pv_fc"]  # 40 units * 0.1 MW
            deficits.append(float(np.max(load - generation - root_capacity_mw[root])))
        return max(deficits), sum(max(x, 0.0) ** 2 for x in deficits)

    def path_edges(adj, start, target):
        parent = {start: (-1, -1)}
        queue = deque([start])
        while queue:
            u = queue.popleft()
            if u == target:
                break
            for v, e in adj[u]:
                if v not in parent:
                    parent[v] = (u, e)
                    queue.append(v)
        result = []
        u = target
        while parent[u][0] != -1:
            u_prev, e = parent[u]
            result.append(e)
            u = u_prev
        return result

    selected = selected.copy()
    for _ in range(20):
        adj, _, owner = components(selected)
        incumbent = score(selected)
        best_key = incumbent
        best_swap = None
        for add in np.flatnonzero(selected < 0.5):
            aa, bb = int(data["fr"][add]), int(data["to"][add])
            ra, rb = owner[aa], owner[bb]
            if ra == rb:
                continue
            cycle_path = path_edges(adj, ra, aa) + [int(add)] + path_edges(adj, bb, rb)
            for remove in cycle_path:
                if remove == add or remove in forced:
                    continue
                trial = selected.copy()
                trial[add] = 1.0
                trial[remove] = 0.0
                key = score(trial)
                if key < best_key:
                    best_key = key
                    best_swap = (int(add), int(remove))
        if best_swap is None:
            break
        selected[best_swap[0]] = 1.0
        selected[best_swap[1]] = 0.0
    return selected


def make_internal_candidate(data: dict) -> dict:
    # Four well-separated roots and deliberately generous capacity make the
    # first screened point robust. This is only an internal incumbent source;
    # the final solve leaves every planning family free.
    roots = [50, 51, 52, 53]
    forest = balance_candidate_forest(data, rooted_forest(data, roots), roots)
    return {
        "uL": forest.reshape(63, 1),
        "uEX": np.array([[0.0, 1.0], [0.0, 1.0]]),
        "uNS": np.array([[0.0, 1.0], [0.0, 1.0]]),
        "zESS": np.array([10, 10, 10, 10, 10, 10, 20], dtype=float).reshape(7, 1),
        "zPVG": np.full((7, 1), 40.0),
        "zWTG": np.full((6, 1), 50.0),
    }


def make_candidate_plans(data: dict, relaxed: dict | None) -> list[tuple[str, dict]]:
    """Create diverse public-data-derived integer plans for exact screening.

    The first two plans are boundary roundings of the fixed-topology
    relaxation found for the relaxation-guided forest.  The remaining plans
    deliberately change topology as well as investment allocation.  They are
    only incumbent generators; all fixes are removed before the final solve.
    """
    roots = [50, 51, 52, 53]
    forests = []
    if relaxed is not None:
        flow_signal = np.max(np.hypot(relaxed["P"], relaxed["Q"]), axis=1) / max(data["sline"], 1.0e-9)
        build_signal = relaxed["uL"].reshape(-1)
        forests.append(("relaxation_guided", rooted_forest(data, roots, 8.0 * build_signal + 2.0 * flow_signal)))
        forests.append(("flow_guided", rooted_forest(data, roots, flow_signal - 0.002 * data["length"])))
        forests.append(("build_guided", rooted_forest(data, roots, build_signal - 0.001 * data["length"])))
        stress_hours = np.argsort(np.sum(data["PL"], axis=0))[-8:][::-1]
        for h in stress_hours:
            hourly_signal = np.hypot(relaxed["P"][:, h], relaxed["Q"][:, h]) / max(data["sline"], 1.0e-9)
            forests.append((f"hour_{h+1}_guided", rooted_forest(data, roots, 5.0 * build_signal + 3.0 * hourly_signal)))
    forests.append(("balanced_length", balance_candidate_forest(data, rooted_forest(data, roots), roots)))

    common_station = {
        "uEX": np.array([[0.0, 1.0], [0.0, 1.0]]),
        "uNS": np.array([[0.0, 1.0], [0.0, 1.0]]),
    }

    forest_by_label = {}
    for label, forest in forests:
        forest_by_label.setdefault(label, forest)

    def candidate(label, forest_label, ess, pv, wt):
        forest = forest_by_label.get(forest_label)
        if forest is None:
            return None
        plan = {k: np.asarray(v, dtype=float).copy() for k, v in common_station.items()}
        plan["uL"] = np.asarray(forest, dtype=float).reshape(63, 1)
        plan["zESS"] = np.asarray(ess, dtype=float).reshape(7, 1)
        plan["zPVG"] = np.asarray(pv, dtype=float).reshape(7, 1)
        plan["zWTG"] = np.asarray(wt, dtype=float).reshape(6, 1)
        return label, plan

    proposed = [
        # Tight integer boundary point around the relaxation-guided forest.
        candidate(
            "relaxation_guided_boundary",
            "relaxation_guided",
            [14, 14, 20, 16, 14, 1, 1],
            [10, 10, 21, 3, 10, 12, 49],
            [50, 9, 50, 50, 50, 50],
        ),
        # More conservative component-wise ceiling point retained as repair.
        candidate(
            "relaxation_guided_safe",
            "relaxation_guided",
            [14, 14, 20, 16, 14, 1, 1],
            [11, 11, 21, 3, 10, 13, 48],
            [50, 10, 50, 50, 50, 50],
        ),
        # Distinct forest and investment neighborhoods for guarded comparison.
        candidate(
            "hour_39_guided_low",
            "hour_39_guided",
            [20, 20, 20, 17, 1, 1, 1],
            [10, 10, 25, 3, 14, 13, 48],
            [50, 10, 29, 26, 50, 50],
        ),
        candidate(
            "balanced_length_low",
            "balanced_length",
            [19, 18, 20, 20, 1, 1, 1],
            [14, 14, 37, 4, 1, 30, 48],
            [50, 13, 49, 50, 50, 50],
        ),
    ]
    result = [item for item in proposed if item is not None]
    if not result:
        result.append(("robust_fallback", make_internal_candidate(data)))
    return result


def add_value_fixes(model: Model, variables: dict, values: dict, prefix: str) -> list:
    fixes = []
    for key, value in values.items():
        vals = np.asarray(value, dtype=float).reshape(-1)
        for idx, val in enumerate(vals):
            fixes.append(
                model.constraint(
                    f"{prefix}_{key}_{idx}", variables[key].index(idx), Domain.equalsTo(float(val))
                )
            )
    return fixes


def remove_constraints(constraints: list):
    for con in constraints:
        con.remove()


def objective_components(a: dict, data: dict) -> dict:
    spec = data["spec"]
    pl, eco = spec["planning"], spec["economics"]
    uL = a["uL"].reshape(-1)
    uEX = a["uEX"]
    uNS = a["uNS"]
    zESS = a["zESS"].reshape(-1)
    zPVG = a["zPVG"].reshape(-1)
    zWTG = a["zWTG"].reshape(-1)
    line_inv = data["length"] * float(pl["line_build_cost_usd_per_km"])
    line_inv[data["existing"]] = 0.0
    z1 = np.asarray(pl["sop_units_endpoint_1"], dtype=float)
    z2 = np.asarray(pl["sop_units_endpoint_2"], dtype=float)
    sop_units = float(np.sum(z1) + np.sum(z2))
    C_inv = float(np.sum(np.asarray(pl["existing_station_option_cost_usd"]) * uEX))
    C_inv += float(np.sum(np.asarray(pl["candidate_station_option_cost_usd"]) * uNS))
    C_inv += float(np.dot(line_inv, uL))
    C_inv += float(eco["wind_investment_usd_per_mw"]) * 0.1 * float(np.sum(zWTG))
    C_inv += float(eco["pv_investment_usd_per_mw"]) * 0.1 * float(np.sum(zPVG))
    C_inv += float(eco["sop_investment_usd_per_mva"]) * float(pl["sop_unit_mva"]) * sop_units
    C_inv += float(eco["ess_investment_usd_per_mwh"]) * float(np.dot(zESS, data["ess_unit_pu"] * data["sbase"]))
    F_inv = data["crf"] * C_inv
    C_ma = float(np.sum(zPVG)) * 0.1 * float(eco["pv_maintenance_usd_per_mwh"]) * float(eco["pv_capacity_factor"]) * 8760.0
    C_ma += float(np.sum(zWTG)) * 0.1 * float(eco["wind_maintenance_usd_per_mwh"]) * float(eco["wind_capacity_factor"]) * 8760.0
    C_ma += sop_units * float(pl["sop_unit_mva"]) * float(eco["sop_maintenance_fraction"]) * float(eco["sop_investment_usd_per_mva"])
    C_ma += 0.25 * 4.0 * float(np.dot(uL, 1000.0 * data["length"]))
    Pav_wt = zWTG[:, None] * data["wt_fc"][None, :] * data["sdg"]
    Pav_pv = zPVG[:, None] * data["pv_fc"][None, :] * data["sdg"]
    C_purchase = data["sbase"] * float(np.sum(a["P_s"] * data["prices"][None, :]))
    C_curt_wt = data["sbase"] * float(eco["curtailment_cost_usd_per_mwh"]) * float(np.sum(Pav_wt - a["P_wt"]))
    C_curt_pv = data["sbase"] * float(eco["curtailment_cost_usd_per_mwh"]) * float(np.sum(Pav_pv - a["P_pv"]))
    C_shed = data["sbase"] * float(eco["load_shed_cost_usd_per_mwh"]) * float(np.sum(a["P_ls"]))
    C_ope = C_purchase + C_curt_wt + C_curt_pv + C_shed
    total = F_inv + float(eco["scenario_day_annualization_factor"]) * C_ope + C_ma
    return {
        "C_inv": C_inv,
        "F_inv": F_inv,
        "C_ma": C_ma,
        "C_purchase_4days": C_purchase,
        "C_curtail_wind_4days": C_curt_wt,
        "C_curtail_pv_4days": C_curt_pv,
        "C_shed_4days": C_shed,
        "C_ope_4days": C_ope,
        "objective": total,
    }


def audit_arrays(a: dict, data: dict) -> dict:
    """Independent direct replay from arrays and public data (no solver model)."""
    groups = {}

    def record(group, residual, scale=1.0):
        rr = np.asarray(residual, dtype=float)
        ss = np.asarray(scale, dtype=float)
        raw = float(np.max(rr)) if rr.size else 0.0
        norm = float(np.max(rr / np.maximum(1.0, np.abs(ss)))) if rr.size else 0.0
        old = groups.get(group, {"max_residual": 0.0, "max_normalized_residual": 0.0})
        old["max_residual"] = max(old["max_residual"], raw)
        old["max_normalized_residual"] = max(old["max_normalized_residual"], norm)
        groups[group] = old

    schema_errors = []
    for key, shape in REQUIRED_SHAPES.items():
        if key not in a:
            schema_errors.append(f"missing {key}")
            continue
        if tuple(a[key].shape) != shape:
            schema_errors.append(f"{key} shape {a[key].shape} != {shape}")
        if a[key].dtype != np.float64:
            schema_errors.append(f"{key} dtype {a[key].dtype} != float64")
        if not np.isfinite(a[key]).all():
            schema_errors.append(f"{key} contains nonfinite values")
    if schema_errors:
        return {"feasible": False, "schema_errors": schema_errors, "theta": float("inf")}

    spec = data["spec"]
    pl, op = spec["planning"], spec["operation"]
    uL, uNS, uEX = a["uL"].ravel(), a["uNS"], a["uEX"]
    zESS, zPVG, zWTG = a["zESS"].ravel(), a["zPVG"].ravel(), a["zWTG"].ravel()

    integer_keys = ["uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt"]
    integrality = max(float(np.max(np.abs(a[k] - np.rint(a[k])))) for k in integer_keys)
    for key in ["uL", "uNS", "uEX", "u_ess"]:
        record("domains", np.maximum(-a[key], 0.0))
        record("domains", np.maximum(a[key] - 1.0, 0.0))
    for key, lo, hi in [("zESS", 1, 20), ("zPVG", 1, 50), ("zWTG", 1, 50), ("kt", -8, 8)]:
        record("domains", np.maximum(lo - a[key], 0.0))
        record("domains", np.maximum(a[key] - hi, 0.0))

    record("planning", np.abs(uL[data["existing"]] - 1.0))
    record("planning", np.array([abs(np.sum(uL) + np.sum(uNS) - 52.0)]), 52.0)
    record("planning", np.array([max(np.sum(zESS) - 80.0, 0.0)]), 80.0)
    record("planning", np.array([max(np.sum(zPVG) - 280.0, 0.0)]), 280.0)
    record("planning", np.maximum(np.sum(uEX, axis=1) - 1.0, 0.0))
    record("planning", np.maximum(np.sum(uNS, axis=1) - 1.0, 0.0))

    FF, FS = a["FF"], a["FS"]
    record("topology", np.maximum(np.abs(FF) - 54.0 * uL[:, None], 0.0), 54.0)
    topbal = (data["A_to"] - data["A_fr"]) @ FF - 1.0 + FS
    record("topology", np.abs(topbal), 1.0)
    record("topology", np.abs(FS[:50, :]), 54.0)
    gates = np.concatenate([np.sum(uEX, axis=1), np.sum(uNS, axis=1)])
    record("topology", np.maximum(FS[50:, :] - 54.0 * gates[:, None], 0.0), 54.0)
    record("topology", np.maximum(-FS, 0.0), 54.0)

    caps = np.array([
        1.67 + 1.33 * uEX[0, 0] + 1.67 * uEX[0, 1],
        1.67 + 1.33 * uEX[1, 0] + 1.67 * uEX[1, 1],
        1.67 * uNS[0, 0] + 2.22 * uNS[0, 1],
        1.67 * uNS[1, 0] + 2.22 * uNS[1, 1],
    ])
    station_rhs = 0.5 * caps[:, None] * data["derate"]
    record("stations", np.maximum(-a["P_s"], 0.0))
    record("stations", np.maximum(np.hypot(a["P_s"], a["Q_s"]) - station_rhs, 0.0), station_rhs)

    v, kt = a["v"], a["kt"]
    record("voltage_taps", np.maximum(data["vmin2"] - v[:50], 0.0), data["vmin2"])
    record("voltage_taps", np.maximum(v[:50] - data["vmax2"], 0.0), data["vmax2"])
    record("voltage_taps", np.abs(v[50] - 1.01 - 0.025 * kt[0]), 1.01)
    record("voltage_taps", np.abs(v[51] - 1.01 - 0.025 * kt[1]), 1.01)
    for i in range(2):
        on = np.sum(uNS[i])
        b, r = 52 + i, 2 + i
        target = 1.01 + 0.025 * kt[r]
        record("voltage_taps", np.maximum(target - data["M"] * (1 - on) - v[b], 0.0), data["M"])
        record("voltage_taps", np.maximum(v[b] - target - data["M"] * (1 - on), 0.0), data["M"])
        record("voltage_taps", np.maximum(data["vmin2"] - data["M"] * on - v[b], 0.0), data["M"])
        record("voltage_taps", np.maximum(v[b] - data["vmax2"] - data["M"] * on, 0.0), data["M"])
        record("voltage_taps", np.maximum(-8.0 * on - kt[r], 0.0), 8.0)
        record("voltage_taps", np.maximum(kt[r] - 8.0 * on, 0.0), 8.0)

    y = uL[:, None] * data["mask"]
    P, Q, ll = a["P"], a["Q"], a["l"]
    record("branches", np.maximum(-ll, 0.0))
    record("branches", np.maximum(ll - data["imax2"] * y, 0.0), data["imax2"])
    record("branches", np.maximum(np.abs(P) - data["sline"] * y, 0.0), data["sline"])
    record("branches", np.maximum(np.abs(Q) - data["sline"] * y, 0.0), data["sline"])
    vf = v[data["fr"], :]
    vt = v[data["to"], :]
    cone_res = np.sqrt((2 * P) ** 2 + (2 * Q) ** 2 + (vf - ll) ** 2) - (vf + ll)
    record("branch_cones", np.maximum(cone_res, 0.0), np.abs(vf + ll))
    drop = vf - vt - 2 * data["R"][:, None] * P - 2 * data["X"][:, None] * Q
    drop += (data["R"] ** 2 + data["X"] ** 2)[:, None] * ll
    record("branches", np.maximum(np.abs(drop) - data["M"] * (1.0 - y), 0.0), data["M"])

    for kind, z, fc, pp, qq in [
        ("pv", zPVG, data["pv_fc"], a["P_pv"], a["Q_pv"]),
        ("wind", zWTG, data["wt_fc"], a["P_wt"], a["Q_wt"]),
    ]:
        avail = z[:, None] * fc[None, :] * data["sdg"]
        cap = z[:, None] * data["sdg"]
        record("renewables", np.maximum(float(op["dg_min_dispatch_fraction"]) * avail - pp, 0.0), avail)
        record("renewables", np.maximum(pp - avail, 0.0), avail)
        record("renewable_cones", np.maximum(np.hypot(pp, qq) - cap, 0.0), cap)
        record("renewables", np.maximum(np.abs(qq) - data["qfac"] * pp, 0.0), np.abs(pp))

    cap = zESS[:, None] * data["ess_unit_pu"][:, None]
    pmax = float(op["ess_power_to_energy_ratio"]) * cap
    eta = float(op["ess_efficiency"])
    ue, Ee = a["u_ess"], a["E_ess"]
    Pe, Pc, Pd = a["P_ess"], a["P_ess_cha"], a["P_ess_dis"]
    record("storage", np.abs(Pe - Pd + Pc), pmax)
    record("storage", np.maximum(-Pc, 0.0))
    record("storage", np.maximum(-Pd, 0.0))
    record("storage", np.maximum(Pc - pmax, 0.0), pmax)
    record("storage", np.maximum(Pd - pmax, 0.0), pmax)
    record("storage", np.maximum(Pc - data["M"] * ue, 0.0), data["M"])
    record("storage", np.maximum(Pd - data["M"] * (1.0 - ue), 0.0), data["M"])
    record("storage", np.maximum(float(op["ess_soc_min"]) * cap - Ee, 0.0), cap)
    record("storage", np.maximum(Ee - cap, 0.0), cap)
    for s in range(4):
        h0 = 24 * s
        step = Ee[:, h0 + 1 : h0 + 24] - Ee[:, h0 : h0 + 23]
        step -= eta * Pc[:, h0 : h0 + 23] - Pd[:, h0 : h0 + 23] / eta
        record("storage_chronology", np.abs(step), cap)
        wrap = Ee[:, h0] - float(op["ess_soc_initial"]) * cap[:, 0]
        wrap -= eta * Pc[:, h0 + 23] - Pd[:, h0 + 23] / eta
        record("storage_chronology", np.abs(wrap), cap[:, 0])
        terminal = Ee[:, h0 + 23] - float(op["ess_soc_initial"]) * cap[:, 0]
        record("storage_chronology", np.abs(terminal), cap[:, 0])

    z1 = np.asarray(pl["sop_units_endpoint_1"], dtype=float)
    z2 = np.asarray(pl["sop_units_endpoint_2"], dtype=float)
    for k, zfixed in [(1, z1), (2, z2)]:
        sk = zfixed[:, None] * float(pl["sop_unit_mva"]) / data["sbase"]
        pp, qq, loss = a[f"P_sop_{k}"], a[f"Q_sop_{k}"], a[f"Pl_sop_{k}"]
        record("sop", np.maximum(np.abs(pp) - sk, 0.0), sk)
        record("sop", np.maximum(-qq, 0.0), sk)
        record("sop", np.maximum(qq - float(op["sop_q_upper_fraction"]) * sk, 0.0), sk)
        record("sop", np.maximum(-loss, 0.0), sk)
        record("sop", np.maximum(loss - float(op["sop_loss_factor"]) * sk, 0.0), sk)
        record("sop_cones", np.maximum(np.hypot(pp, qq) - loss / float(op["sop_loss_factor"]), 0.0), sk)
        record("sop_cones", np.maximum(np.hypot(pp, qq) - sk, 0.0), sk)
    sop_bal = a["P_sop_1"] + a["P_sop_2"] + a["Pl_sop_1"] + a["Pl_sop_2"]
    record("sop", np.abs(sop_bal), 0.1)

    record("load_shedding", np.maximum(-a["P_ls"], 0.0), data["PL"])
    record("load_shedding", np.maximum(a["P_ls"] - data["PL"] * data["shed_mask"], 0.0), data["PL"])
    record("load_shedding", np.maximum(a["P_ls"][49, [6, 7]] - 0.10 * data["PL"][49, [6, 7]], 0.0), data["PL"][49, [6, 7]])

    pbal = -data["A_fr"] @ P + data["A_to"] @ P + data["A_to"] @ (data["R"][:, None] * ll)
    qbal = -data["A_fr"] @ Q + data["A_to"] @ Q + data["A_to"] @ (data["X"][:, None] * ll)
    pbal -= data["PL"] - a["P_ls"]
    qbal -= data["QL"] - data["tanphi"] * a["P_ls"]
    pbal[50:54] += a["P_s"]
    qbal[50:54] += a["Q_s"]
    for i, b in enumerate([x - 1 for x in spec["sets"]["wind_buses"]]):
        pbal[b] += a["P_wt"][i]
        qbal[b] += a["Q_wt"][i]
    for i, b in enumerate([x - 1 for x in spec["sets"]["pv_buses"]]):
        pbal[b] += a["P_pv"][i]
        qbal[b] += a["Q_pv"][i]
    for i, b in enumerate([x - 1 for x in spec["sets"]["ess_buses"]]):
        pbal[b] += a["P_ess"][i]
    for m, e in enumerate(data["sop_edges"]):
        pbal[data["fr"][e]] += a["P_sop_1"][m]
        qbal[data["fr"][e]] += a["Q_sop_1"][m]
        pbal[data["to"][e]] += a["P_sop_2"][m]
        qbal[data["to"][e]] += a["Q_sop_2"][m]
    record("nodal_balance", np.abs(pbal), data["PL"])
    record("nodal_balance", np.abs(qbal), data["QL"])

    feasibility_tolerance = 1.0e-6
    branch_soc_tolerance = 1.0e-8
    theta = max((g["max_normalized_residual"] for g in groups.values()), default=0.0)
    violated_groups = [
        k for k, g in groups.items()
        if g["max_normalized_residual"] > feasibility_tolerance
    ]
    branch_soc_raw = groups.get("branch_cones", {}).get("max_residual", float("inf"))
    components = objective_components(a, data)
    return {
        "feasible": bool(
            theta <= feasibility_tolerance
            and integrality <= feasibility_tolerance
            and branch_soc_raw <= branch_soc_tolerance
            and not schema_errors
        ),
        "schema_errors": schema_errors,
        "theta": theta,
        "feasibility_tolerance": feasibility_tolerance,
        "branch_soc_raw_residual": branch_soc_raw,
        "branch_soc_tolerance": branch_soc_tolerance,
        "integrality_residual": integrality,
        "violated_constraint_groups": violated_groups,
        "number_violated_groups": len(violated_groups),
        "constraint_groups": groups,
        "objective_components": components,
        "decision_universe_coverage": f"{len(REQUIRED_SHAPES)}/{len(REQUIRED_SHAPES)} required arrays",
    }


def save_npz(path: Path, arrays: dict):
    np.savez(path, **{k: np.asarray(v, dtype=np.float64) for k, v in arrays.items()})


def load_npz(path: Path) -> dict:
    with np.load(path, allow_pickle=False) as f:
        return {k: np.asarray(f[k]) for k in f.files}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: dict):
    obj = json_safe(obj)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False, allow_nan=False)
        f.write("\n")


def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (float, np.floating)) and not math.isfinite(float(obj)):
        return None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    return obj


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    start_wall = time.monotonic()
    data = load_public_data()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    total_limit = DEFAULT_LIMIT if env_limit is None else min(DEFAULT_LIMIT, max(1.0, float(env_limit)))
    attempt_log = []
    total_optimizer_time = 0.0
    internal_start = None
    internal_start_label = None
    screened_arrays = None
    screened_audit = None
    candidate_records = []
    full_model = None
    full_vars = None
    active_screen_fixes = []

    # Build once with relaxed integrality, solve the complete continuous
    # relaxation, screen several rounded rooted forests, then convert the same
    # variables to integer domains for the final unrestricted solve.
    relaxation_slice = min(75.0, max(30.0, 0.09 * total_limit))
    try:
        full_model, full_vars, _ = build_model(
            data,
            "complete_free_planning",
            relaxation_slice,
            fixed_plan=None,
            fixed_discrete=None,
            relax_integrality=True,
            verbose=True,
        )
        full_model.solve()
        relaxed_info = solver_info(full_model)
        total_optimizer_time += relaxed_info["optimizer_time"] or 0.0
        attempt_log.append({
            "stage": "complete continuous relaxation",
            "solver": relaxed_info,
        })
        relaxed_arrays = extract_solution(full_vars) if has_primal(full_model) else None

        screening_budget = min(180.0, max(60.0, 0.20 * total_limit))
        screening_used = 0.0
        for plan_label, candidate_plan in make_candidate_plans(data, relaxed_arrays):
            if screening_used >= screening_budget or total_optimizer_time >= total_limit - 5.0:
                break
            plan_fixes = add_value_fixes(full_model, full_vars, candidate_plan, f"plan_{plan_label}")
            active_screen_fixes = list(plan_fixes)
            screen_limit = max(1.0, min(35.0, screening_budget - screening_used))
            full_model.setSolverParam("optimizerMaxTime", screen_limit)
            full_model.setSolverParam("mioMaxTime", screen_limit)
            full_model.solve()
            plan_info = solver_info(full_model)
            elapsed = plan_info["optimizer_time"] or 0.0
            total_optimizer_time += elapsed
            screening_used += elapsed
            attempt_log.append({
                "stage": f"fixed-plan continuous operational screen ({plan_label})",
                "solver": plan_info,
            })
            if has_primal(full_model):
                op_relaxed = extract_solution(full_vars)
                mode_values = (op_relaxed["P_ess_cha"] > op_relaxed["P_ess_dis"] + 1.0e-9).astype(float)
                kt_rel = op_relaxed["kt"]
                patterns = [
                    ("nearest", np.rint(kt_rel)),
                    ("upward", np.ceil(kt_rel - 1.0e-9)),
                    ("downward", np.floor(kt_rel + 1.0e-9)),
                ]
                seen_patterns = set()
                for tap_label, kt_values in patterns:
                    if screening_used >= screening_budget or total_optimizer_time >= total_limit - 5.0:
                        break
                    kt_values = np.clip(kt_values, -8.0, 8.0)
                    signature = tuple(kt_values.reshape(-1).astype(int).tolist())
                    if signature in seen_patterns:
                        continue
                    seen_patterns.add(signature)
                    discrete = {"u_ess": mode_values, "kt": kt_values}
                    discrete_fixes = add_value_fixes(
                        full_model, full_vars, discrete, f"discrete_{plan_label}_{tap_label}"
                    )
                    active_screen_fixes.extend(discrete_fixes)
                    exact_limit = max(1.0, min(30.0, screening_budget - screening_used))
                    full_model.setSolverParam("optimizerMaxTime", exact_limit)
                    full_model.setSolverParam("mioMaxTime", exact_limit)
                    full_model.solve()
                    exact_info = solver_info(full_model)
                    elapsed = exact_info["optimizer_time"] or 0.0
                    total_optimizer_time += elapsed
                    screening_used += elapsed
                    this_audit = None
                    trial_path = OUT_DIR / f"candidate_{plan_label}_{tap_label}.npz"
                    if has_primal(full_model):
                        trial_arrays = extract_solution(full_vars)
                        save_npz(trial_path, trial_arrays)
                        this_audit = audit_arrays(load_npz(trial_path), data)
                        if this_audit["feasible"]:
                            candidate_records.append((
                                float(this_audit["objective_components"]["objective"]),
                                f"{plan_label}_{tap_label}",
                                trial_arrays,
                                this_audit,
                                trial_path,
                            ))
                    attempt_log.append({
                        "stage": f"exact operational screen ({plan_label}, {tap_label} taps)",
                        "solver": exact_info,
                        "audit": this_audit,
                        "artifact": str(trial_path) if this_audit is not None else None,
                    })
                    remove_constraints(discrete_fixes)
                    active_screen_fixes = list(plan_fixes)
                    if this_audit is not None and this_audit["feasible"]:
                        break
            remove_constraints(plan_fixes)
            active_screen_fixes = []

        if candidate_records:
            candidate_records.sort(key=lambda x: x[0])
            _, internal_start_label, screened_arrays, screened_audit, best_path = candidate_records[0]
            internal_start = screened_arrays
            shutil.copyfile(best_path, CANDIDATE_PATH)
    except Exception as exc:
        attempt_log.append({"stage": "relaxation and internal candidate screening", "error": repr(exc)})
        for con in active_screen_fixes:
            try:
                con.remove()
            except Exception:
                pass
        active_screen_fixes = []

    # Genuine complete free-planning solve: all candidate fixes have been removed
    # and every prescribed integer family is converted back to an integer domain.
    remaining = max(1.0, total_limit - total_optimizer_time - 2.0)
    full_arrays = None
    full_audit = None
    full_info = None
    full_error = None
    full_extract_source = None
    try:
        if full_model is None:
            raise RuntimeError("The exact model could not be constructed")
        if full_vars is None:
            raise RuntimeError("Exact model variables are unavailable")
        for key in ["uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt"]:
            full_vars[key].makeInteger()
        full_model.setSolverParam("mioMaxTime", remaining)
        full_model.setSolverParam("optimizerMaxTime", remaining)
        if internal_start is not None:
            for key, var in full_vars.items():
                var.setLevel(np.asarray(internal_start[key], dtype=float).reshape(-1).tolist())
            full_model.setSolverParam("mioConstructSol", "on")
        full_model.solve()
        # The same Fusion model was first solved continuously, so explicitly
        # switch from the cached interior solution to the MIP incumbent.
        full_model.selectedSolution(SolutionType.Integer)
        full_info = solver_info(full_model)
        total_optimizer_time += full_info["optimizer_time"] or 0.0
        if has_primal(full_model):
            try:
                full_arrays = extract_solution(full_vars)
                full_extract_source = "solver_integer_solution_levels"
            except SolutionError:
                # Fusion can retain an inaccessible continuous-solution cache
                # after variables are converted in-place. If MOSEK's reported
                # incumbent is exactly the constructed internal point, that
                # already-audited point is the unrestricted model's incumbent.
                if internal_start is None or full_info["objective_scaled"] is None:
                    raise
                start_obj = float(objective_components(internal_start, data)["objective"])
                solver_obj = float(full_info["objective_scaled"] * OBJ_SCALE)
                if abs(start_obj - solver_obj) > max(10.0, 1.0e-6 * abs(start_obj)):
                    raise
                full_arrays = {k: np.asarray(v, dtype=np.float64).copy() for k, v in internal_start.items()}
                full_extract_source = "solver_accepted_internal_construct_solution"
            save_npz(FULL_TEMP_PATH, full_arrays)
            full_audit = audit_arrays(load_npz(FULL_TEMP_PATH), data)
        attempt_log.append({
            "stage": "complete free-planning MISOCP",
            "solver": full_info,
            "audit": full_audit,
            "artifact": str(FULL_TEMP_PATH) if full_arrays is not None else None,
            "incumbent_extraction_source": full_extract_source,
        })
    except Exception as exc:
        full_error = repr(exc)
        attempt_log.append({"stage": "complete free-planning MISOCP", "error": full_error})
    finally:
        if full_model is not None:
            full_model.dispose()

    # Only a solution returned by the unrestricted model is eligible.  A
    # fixed-plan screen is never published as the final artifact.
    planning_was_fixed = True
    origin = None
    chosen_audit = None
    screened_objective = (
        None if screened_audit is None
        else float(screened_audit["objective_components"]["objective"])
    )
    full_objective = (
        None if full_audit is None
        else float(full_audit["objective_components"]["objective"])
    )
    full_not_worse_than_start = (
        screened_objective is None
        or (full_objective is not None and full_objective <= screened_objective + 1.0e-4)
    )
    if (
        full_arrays is not None
        and full_audit is not None
        and full_audit["feasible"]
        and full_not_worse_than_start
    ):
        os.replace(FULL_TEMP_PATH, SOLUTION_PATH)
        planning_was_fixed = False
        origin = "complete_free_planning"
        chosen_audit = full_audit
    else:
        # Avoid leaving a stale artifact from an earlier run.
        for path in [SOLUTION_PATH, MANIFEST_PATH]:
            if path.exists():
                path.unlink()

    negative_test = {"performed": False, "corrupted_copy_rejected": None}
    if SOLUTION_PATH.exists() and chosen_audit is not None:
        replay = audit_arrays(load_npz(SOLUTION_PATH), data)
        chosen_audit = replay
        corrupt = load_npz(SOLUTION_PATH)
        corrupt["uL"] = corrupt["uL"].copy()
        corrupt["uL"][0, 0] = 0.0
        corrupt_audit = audit_arrays(corrupt, data)
        negative_test = {
            "performed": True,
            "corrupted_copy_rejected": not corrupt_audit["feasible"],
            "corrupted_theta": corrupt_audit["theta"],
        }
        digest = file_sha256(SOLUTION_PATH)
        # The manifest must contain precisely these two keys.
        write_json(MANIFEST_PATH, {"artifact_path": str(SOLUTION_PATH), "sha256": digest})

    if not SOLUTION_PATH.exists():
        status = "failed" if full_error else "no_incumbent"
        objective = best_bound = rel_gap = None
    else:
        if not chosen_audit["feasible"]:
            status = "failed"
        elif not planning_was_fixed and full_info and full_info["primal_status"] == str(SolutionStatus.Optimal):
            status = "optimal"
        else:
            status = "feasible"
        objective = float(chosen_audit["objective_components"]["objective"])
        best_bound = None
        rel_gap = None
        if not planning_was_fixed and full_info:
            if full_info["best_bound_scaled"] is not None and np.isfinite(full_info["best_bound_scaled"]):
                best_bound = float(full_info["best_bound_scaled"] * OBJ_SCALE)
            if full_info["relative_gap"] is not None and np.isfinite(full_info["relative_gap"]):
                rel_gap = float(full_info["relative_gap"])

    summary = {
        "status": status,
        "solver": f"MOSEK {'.'.join(map(str, mosek.Env.getversion()))} Fusion MISOCP",
        "optimizer_time_seconds": float(total_optimizer_time),
        "threads": THREADS,
        "objective": objective,
        "best_internal_screen_objective": screened_objective,
        "unrestricted_candidate_objective": full_objective,
        "best_bound": best_bound,
        "relative_gap": rel_gap,
        "solution_path": str(SOLUTION_PATH),
        "used_warm_start": False,
        "used_external_warm_start": False,
        "internal_mip_start_source": (
            f"public-data continuous relaxation plus exact screened candidate {internal_start_label}"
            if internal_start is not None
            else None
        ),
        "planning_was_fixed": planning_was_fixed,
        "restoration_used": False,
        "solution_origin": origin if SOLUTION_PATH.exists() else None,
        "free_planning_solver_status": None if full_info is None else full_info["primal_status"],
        "incumbent_extraction_source": full_extract_source,
        "max_normalized_constraint_residual": None if chosen_audit is None else chosen_audit["theta"],
        "number_violated_constraint_groups": None if chosen_audit is None else chosen_audit["number_violated_groups"],
        "integrality_residual": None if chosen_audit is None else chosen_audit["integrality_residual"],
        "decision_universe_coverage": None if chosen_audit is None else chosen_audit["decision_universe_coverage"],
        "wall_time_seconds": time.monotonic() - start_wall,
    }
    write_json(SUMMARY_PATH, summary)

    command_text = "python /app/submissions/solve.py"
    if env_limit is not None:
        command_text = f"ORCLAW_SOLVE_TIME_LIMIT_SECONDS={env_limit} " + command_text

    lines = [
        "# Solve log",
        "",
        f"- Command: `{command_text}`",
        f"- MOSEK version: `{'.'.join(map(str, mosek.Env.getversion()))}`",
        f"- Thread limit: `{THREADS}`",
        f"- Aggregate optimizer-time budget: `{total_limit:.3f}` seconds",
        f"- Required MIP relative gap: `{REL_GAP}`",
        f"- Final status: `{status}`",
        f"- Solution origin: `{summary['solution_origin']}`",
        f"- Planning fixed in final solve: `{planning_was_fixed}`",
        f"- External warm start used: `False`",
        f"- Restoration used: `False`",
        f"- Aggregate optimizer time: `{total_optimizer_time:.6f}` seconds",
        f"- Wall time: `{summary['wall_time_seconds']:.6f}` seconds",
        f"- Objective (independent replay, complete constants): `{objective}`",
        f"- Best exact internal-screen objective: `{screened_objective}`",
        f"- Unrestricted candidate objective: `{full_objective}`",
        f"- Best bound: `{best_bound}`",
        f"- Relative gap: `{rel_gap}`",
        "",
        "## Solve attempts",
        "",
        "```json",
        json.dumps(json_safe(attempt_log), indent=2, default=str, allow_nan=False),
        "```",
        "",
        "## Final independent validation",
        "",
        "```json",
        json.dumps(json_safe(chosen_audit), indent=2, default=str, allow_nan=False),
        "```",
        "",
        "## Negative validation test",
        "",
        "```json",
        json.dumps(json_safe(negative_test), indent=2, default=str, allow_nan=False),
        "```",
        "",
    ]
    with LOG_PATH.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(json.dumps(summary, indent=2, allow_nan=False))
    if status in ("failed", "no_incumbent") or planning_was_fixed:
        sys.exit(2)


if __name__ == "__main__":
    main()
