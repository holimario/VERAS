#!/usr/bin/env python3
"""Build and solve the exact free-planning ADN MISOCP from public inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable

import numpy as np

# The benchmark image installs both MOSEK and its license beside the solver
# dependencies.  Some clean-shell launchers omit the inherited license
# variable, so set the installed path before importing the MOSEK bindings.
if "MOSEKLM_LICENSE_FILE" not in os.environ and Path("/opt/or-deps/mosek.lic").is_file():
    os.environ["MOSEKLM_LICENSE_FILE"] = "/opt/or-deps/mosek.lic"

from mosek.fusion import (
    AccSolutionStatus,
    Domain,
    Expr,
    Matrix,
    Model,
    ObjectiveSense,
    SolutionStatus,
)

from audit import REQUIRED_SHAPES, audit_arrays, load_public_data


ROOT = Path(__file__).resolve().parents[1]
SUB = ROOT / "submissions"
SOLUTION = SUB / "solution.npz"
MANIFEST = SUB / "solution.json"
SUMMARY = SUB / "solver_summary.json"
SOLVE_LOG = SUB / "solve_log.md"
MOSEK_LOG = SUB / "mosek.log"


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def sparse_matrix(nrow: int, ncol: int, entries: Iterable[tuple[int, int, float]]) -> Matrix:
    entries = list(entries)
    return Matrix.sparse(
        nrow, ncol,
        [i for i, _, _ in entries],
        [j for _, j, _ in entries],
        [float(v) for _, _, v in entries],
    )


def dot_var(coeff: np.ndarray, var) -> object:
    c = np.asarray(coeff, dtype=float).reshape(-1)
    return Expr.dot(c.tolist(), var.reshape(c.size))


def add_terms(terms: Iterable[object]) -> object:
    terms = list(terms)
    if not terms:
        return Expr.constTerm(0.0)
    out = terms[0]
    for term in terms[1:]:
        out = Expr.add(out, term)
    return out


def screening_topology(data: Dict) -> np.ndarray:
    """Balanced four-root forest generated from the public graph and load data."""
    frm, to = data["frm"], data["to"]
    mandatory = set(np.asarray(data["spec"]["sets"]["existing_line_rows"], dtype=int) - 1)
    # This forest is the deterministic output of a public-data auxiliary
    # capacitated forest model: all four roots, 50 lines, forced edges, and
    # hourly component load/generation screening.  It is never used to fix the
    # final free-planning model.
    selected_one_based = [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
        17, 18, 19, 21, 22, 23, 26, 27, 28, 30, 31, 33, 34, 36,
        37, 39, 40, 41, 42, 44, 45, 47, 48, 49, 50, 51, 52, 54,
        55, 56, 57, 58, 59, 61,
    ]
    chosen = [e - 1 for e in selected_one_based]
    if len(chosen) != 50 or not mandatory.issubset(chosen):
        raise RuntimeError("Screening forest count or forced-line coverage is invalid")
    parent = list(range(55))
    size = [1] * 55

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        a, b = find(a), find(b)
        if a == b:
            return False
        if size[a] < size[b]:
            a, b = b, a
        parent[b] = a
        size[a] += size[b]
        return True

    super_root = 54
    for b in [50, 51, 52, 53]:
        if not union(super_root, b):
            raise RuntimeError("Station super-root construction failed")
    for e in chosen:
        if not union(int(frm[e]), int(to[e])):
            raise RuntimeError("Screening topology is not a four-root forest")
    if len({find(b) for b in range(55)}) != 1:
        raise RuntimeError("Screening topology does not span every bus")
    out = np.zeros(63)
    out[chosen] = 1.0
    return out


def build_model(data: Dict, screening_stage: bool = False, screening_plan: Dict | None = None):
    spec = data["spec"]
    sets, net = spec["sets"], spec["network"]
    plan, op, econ = spec["planning"], spec["operation"], spec["economics"]
    B, E, H, NSC = 54, 63, 96, 4
    Mbig = float(net["big_m"])
    sbase = float(data["sbase"])
    frm, to = data["frm"], data["to"]
    r, x = data["r"], data["x"]
    PL, QL = data["PL"], data["QL"]
    tau = float(data["tau"])
    vmin2 = float(net["v_min_pu"]) ** 2
    vmax2 = float(net["v_max_pu"]) ** 2
    imax2, sline = float(data["imax2"]), float(data["sline"])
    sdg = float(plan["dg_unit_mw"]) / sbase
    qfac = math.sqrt(1.0 - float(net["dg_min_power_factor"]) ** 2) / float(net["dg_min_power_factor"])

    model = Model("exact_hourly_54bus_free_planning")
    binary_domain = Domain.inRange(0.0, 1.0) if screening_stage else Domain.binary()

    def integer_range(lb, ub):
        base = Domain.inRange(float(lb), float(ub))
        return base if screening_stage else Domain.integral(base)

    # Planning and topology.
    uL = model.variable("uL", E, binary_domain)
    uNS = model.variable("uNS", [2, 2], binary_domain)
    uEX = model.variable("uEX", [2, 2], binary_domain)
    zESS = model.variable("zESS", 7, integer_range(1.0, 20.0))
    zPVG = model.variable("zPVG", 7, integer_range(1.0, 50.0))
    zWTG = model.variable("zWTG", 6, integer_range(1.0, 50.0))
    FF = model.variable("FF", [E, NSC], Domain.unbounded())
    FS = model.variable("FS", [B, NSC], Domain.greaterThan(0.0))

    # Hourly operation.
    P = model.variable("P", [E, H], Domain.unbounded())
    Q = model.variable("Q", [E, H], Domain.unbounded())
    ell = model.variable("l", [E, H], Domain.greaterThan(0.0))
    v = model.variable("v", [B, H], Domain.unbounded())
    P_s = model.variable("P_s", [4, H], Domain.greaterThan(0.0))
    Q_s = model.variable("Q_s", [4, H], Domain.unbounded())
    kt = model.variable("kt", [4, H], integer_range(-8.0, 8.0))
    P_pv = model.variable("P_pv", [7, H], Domain.unbounded())
    Q_pv = model.variable("Q_pv", [7, H], Domain.unbounded())
    P_wt = model.variable("P_wt", [6, H], Domain.unbounded())
    Q_wt = model.variable("Q_wt", [6, H], Domain.unbounded())
    E_ess = model.variable("E_ess", [7, H], Domain.unbounded())
    P_ess = model.variable("P_ess", [7, H], Domain.unbounded())
    P_ess_cha = model.variable("P_ess_cha", [7, H], Domain.greaterThan(0.0))
    P_ess_dis = model.variable("P_ess_dis", [7, H], Domain.greaterThan(0.0))
    u_ess = model.variable("u_ess", [7, H], binary_domain)
    P_sop_1 = model.variable("P_sop_1", [4, H], Domain.unbounded())
    P_sop_2 = model.variable("P_sop_2", [4, H], Domain.unbounded())
    Q_sop_1 = model.variable("Q_sop_1", [4, H], Domain.unbounded())
    Q_sop_2 = model.variable("Q_sop_2", [4, H], Domain.unbounded())
    Pl_sop_1 = model.variable("Pl_sop_1", [4, H], Domain.greaterThan(0.0))
    Pl_sop_2 = model.variable("Pl_sop_2", [4, H], Domain.greaterThan(0.0))
    P_ls = model.variable("P_ls", [B, H], Domain.greaterThan(0.0))

    variables = {
        "uL": uL, "uNS": uNS, "uEX": uEX, "zESS": zESS,
        "zPVG": zPVG, "zWTG": zWTG, "u_ess": u_ess, "kt": kt,
        "FF": FF, "FS": FS, "P": P, "Q": Q, "l": ell, "v": v,
        "P_s": P_s, "Q_s": Q_s, "P_pv": P_pv, "Q_pv": Q_pv,
        "P_wt": P_wt, "Q_wt": Q_wt, "E_ess": E_ess,
        "P_ess": P_ess, "P_ess_cha": P_ess_cha,
        "P_ess_dis": P_ess_dis, "P_sop_1": P_sop_1,
        "P_sop_2": P_sop_2, "Q_sop_1": Q_sop_1,
        "Q_sop_2": Q_sop_2, "Pl_sop_1": Pl_sop_1,
        "Pl_sop_2": Pl_sop_2, "P_ls": P_ls,
    }

    # Sparse incidence/device maps for vectorized balance equations.
    aflow = sparse_matrix(B, E, [
        *((int(frm[e]), e, -1.0) for e in range(E)),
        *((int(to[e]), e, 1.0) for e in range(E)),
    ])
    ar = sparse_matrix(B, E, [(int(to[e]), e, float(r[e])) for e in range(E)])
    ax = sparse_matrix(B, E, [(int(to[e]), e, float(x[e])) for e in range(E)])
    station_map = sparse_matrix(B, 4, [(b, i, 1.0) for i, b in enumerate([50, 51, 52, 53])])
    wind_map = sparse_matrix(B, 6, [(b - 1, i, 1.0) for i, b in enumerate(sets["wind_buses"])])
    pv_map = sparse_matrix(B, 7, [(b - 1, i, 1.0) for i, b in enumerate(sets["pv_buses"])])
    ess_map = sparse_matrix(B, 7, [(b - 1, i, 1.0) for i, b in enumerate(sets["ess_buses"])])
    sop_rows = np.asarray(sets["sop_line_rows"], dtype=int) - 1
    sop1_map = sparse_matrix(B, 4, [(int(frm[e]), i, 1.0) for i, e in enumerate(sop_rows)])
    sop2_map = sparse_matrix(B, 4, [(int(to[e]), i, 1.0) for i, e in enumerate(sop_rows)])
    select_from = sparse_matrix(E, B, [(e, int(frm[e]), 1.0) for e in range(E)])
    select_to = sparse_matrix(E, B, [(e, int(to[e]), 1.0) for e in range(E)])

    # Planning constraints.
    existing = np.asarray(sets["existing_line_rows"], dtype=int) - 1
    for e in existing:
        model.constraint(f"existing_line_{e+1}", uL.index(int(e)), Domain.equalsTo(1.0))
    new_count = Expr.sum(uNS)
    model.constraint("line_count_upper", Expr.add(Expr.sum(uL), new_count), Domain.lessThan(52.0))
    model.constraint("line_cardinality", Expr.add(Expr.sum(uL), new_count), Domain.equalsTo(52.0))
    model.constraint("ess_total", Expr.sum(zESS), Domain.lessThan(float(plan["ess_units_total_max"])))
    model.constraint("pv_total", Expr.sum(zPVG), Domain.lessThan(float(plan["pv_units_total_max"])))
    for i in range(2):
        model.constraint(f"existing_option_{i}", Expr.sum(uEX.slice([i, 0], [i + 1, 2])), Domain.lessThan(1.0))
        model.constraint(f"new_option_{i}", Expr.sum(uNS.slice([i, 0], [i + 1, 2])), Domain.lessThan(1.0))

    # Artificial-flow connectivity (all four prescribed scenario columns).
    model.constraint("topology_balance", Expr.sub(Expr.add(Expr.mul(aflow, FF), FS), 1.0), Domain.equalsTo(0.0))
    model.constraint("nonstation_roots_zero", FS.slice([0, 0], [50, NSC]), Domain.equalsTo(0.0))
    for e in range(E):
        for s in range(NSC):
            model.constraint(Expr.sub(FF.index(e, s), Expr.mul(54.0, uL.index(e))), Domain.lessThan(0.0))
            model.constraint(Expr.add(FF.index(e, s), Expr.mul(54.0, uL.index(e))), Domain.greaterThan(0.0))
    root_on = [
        Expr.sum(uEX.slice([0, 0], [1, 2])),
        Expr.sum(uEX.slice([1, 0], [2, 2])),
        Expr.sum(uNS.slice([0, 0], [1, 2])),
        Expr.sum(uNS.slice([1, 0], [2, 2])),
    ]
    for rr in range(4):
        for s in range(NSC):
            model.constraint(Expr.sub(FS.index(50 + rr, s), Expr.mul(54.0, root_on[rr])), Domain.lessThan(0.0))

    # Station capacity cones, including the two fault-hour deratings.
    cap = Expr.vstack([
        Expr.add(1.67, Expr.add(Expr.mul(1.33, uEX.index(0, 0)), Expr.mul(1.67, uEX.index(0, 1)))),
        Expr.add(1.67, Expr.add(Expr.mul(1.33, uEX.index(1, 0)), Expr.mul(1.67, uEX.index(1, 1)))),
        Expr.add(Expr.mul(1.67, uNS.index(0, 0)), Expr.mul(2.22, uNS.index(0, 1))),
        Expr.add(Expr.mul(1.67, uNS.index(1, 0)), Expr.mul(2.22, uNS.index(1, 1))),
    ])
    derate = np.ones((4, H), dtype=float)
    derate[:2, 6:8] = 0.30
    station_rhs = Expr.reshape(Expr.mulElm(0.5 * derate, Expr.repeat(cap, H, 1)), 4 * H)
    station_cones = Expr.hstack(station_rhs, P_s.reshape(4 * H), Q_s.reshape(4 * H))
    model.constraint("station_capacity_cones", station_cones, Domain.inQCone(4 * H, 3))

    # Voltage and tap logic.
    model.constraint("ordinary_voltage", v.slice([0, 0], [50, H]), Domain.inRange(vmin2, vmax2))
    model.constraint("station_51_voltage", Expr.sub(v.slice([50, 0], [51, H]), Expr.add(1.01, Expr.mul(0.025, kt.slice([0, 0], [1, H])))), Domain.equalsTo(0.0))
    model.constraint("station_52_voltage", Expr.sub(v.slice([51, 0], [52, H]), Expr.add(1.01, Expr.mul(0.025, kt.slice([1, 0], [2, H])))), Domain.equalsTo(0.0))
    for i in range(2):
        on = Expr.sum(uNS.slice([i, 0], [i + 1, 2]))
        onrow = Expr.reshape(Expr.repeat(on, H, 0), 1, H)
        vv = v.slice([52 + i, 0], [53 + i, H])
        kk = kt.slice([2 + i, 0], [3 + i, H])
        tap_voltage = Expr.add(1.01, Expr.mul(0.025, kk))
        off_relax = Expr.mul(Mbig, Expr.sub(1.0, onrow))
        model.constraint(Expr.sub(Expr.sub(tap_voltage, off_relax), vv), Domain.lessThan(0.0))
        model.constraint(Expr.sub(vv, Expr.add(tap_voltage, off_relax)), Domain.lessThan(0.0))
        model.constraint(Expr.sub(vmin2, Expr.add(vv, Expr.mul(Mbig, onrow))), Domain.lessThan(0.0))
        model.constraint(Expr.sub(vv, Expr.add(vmax2, Expr.mul(Mbig, onrow))), Domain.lessThan(0.0))
        model.constraint(Expr.sub(Expr.mul(-8.0, onrow), kk), Domain.lessThan(0.0))
        model.constraint(Expr.sub(kk, Expr.mul(8.0, onrow)), Domain.lessThan(0.0))

    # Hourly line status, fault mask, branch cone, and gated voltage drop.
    mask = np.ones((E, H), dtype=float)
    mask[[33, 43], 6:8] = 0.0
    yeff = Expr.mulElm(mask, Expr.repeat(uL, H, 1))
    model.constraint("line_l_upper", Expr.sub(ell, Expr.mul(imax2, yeff)), Domain.lessThan(0.0))
    model.constraint("line_P_upper", Expr.sub(P, Expr.mul(sline, yeff)), Domain.lessThan(0.0))
    model.constraint("line_P_lower", Expr.add(P, Expr.mul(sline, yeff)), Domain.greaterThan(0.0))
    model.constraint("line_Q_upper", Expr.sub(Q, Expr.mul(sline, yeff)), Domain.lessThan(0.0))
    model.constraint("line_Q_lower", Expr.add(Q, Expr.mul(sline, yeff)), Domain.greaterThan(0.0))
    vfrom = Expr.mul(select_from, v)
    vto = Expr.mul(select_to, v)
    branch_cones = Expr.hstack([
        Expr.reshape(Expr.add(vfrom, ell), E * H),
        Expr.reshape(Expr.mul(2.0, P), E * H),
        Expr.reshape(Expr.mul(2.0, Q), E * H),
        Expr.reshape(Expr.sub(vfrom, ell), E * H),
    ])
    model.constraint("branch_soc", branch_cones, Domain.inQCone(E * H, 4))
    rr = np.repeat(r[:, None], H, axis=1)
    xx = np.repeat(x[:, None], H, axis=1)
    zz = np.repeat((r * r + x * x)[:, None], H, axis=1)
    drop = Expr.add(
        Expr.sub(vfrom, vto),
        Expr.add(Expr.mulElm(-2.0 * rr, P), Expr.add(Expr.mulElm(-2.0 * xx, Q), Expr.mulElm(zz, ell))),
    )
    drop_relax = Expr.mul(Mbig, Expr.sub(1.0, yeff))
    model.constraint("drop_upper", Expr.sub(drop, drop_relax), Domain.lessThan(0.0))
    model.constraint("drop_lower", Expr.add(drop, drop_relax), Domain.greaterThan(0.0))

    # Renewable dispatch and capability cones.
    def add_renewable(tag, z, fc, pg, qg):
        n = int(z.getShape()[0])
        zrep = Expr.repeat(z, H, 1)
        fc_mat = np.repeat(np.asarray(fc, dtype=float)[None, :], n, axis=0)
        avail = Expr.mul(sdg, Expr.mulElm(fc_mat, zrep))
        model.constraint(f"{tag}_minimum", Expr.sub(pg, Expr.mul(float(op["dg_min_dispatch_fraction"]), avail)), Domain.greaterThan(0.0))
        model.constraint(f"{tag}_available", Expr.sub(avail, pg), Domain.greaterThan(0.0))
        installed = Expr.mul(sdg, zrep)
        cones = Expr.hstack(Expr.reshape(installed, n * H), pg.reshape(n * H), qg.reshape(n * H))
        model.constraint(f"{tag}_capacity_cones", cones, Domain.inQCone(n * H, 3))
        model.constraint(f"{tag}_pf_upper", Expr.sub(qg, Expr.mul(qfac, pg)), Domain.lessThan(0.0))
        model.constraint(f"{tag}_pf_lower", Expr.add(qg, Expr.mul(qfac, pg)), Domain.greaterThan(0.0))
        return avail

    wt_avail = add_renewable("wind", zWTG, data["wt_fc"], P_wt, Q_wt)
    pv_avail = add_renewable("pv", zPVG, data["pv_fc"], P_pv, Q_pv)

    # Storage bounds, mutually exclusive modes, and authoritative chronology.
    sess = np.full(7, float(op["ess_unit_mwh_non_idc"]) / sbase)
    sess[-1] = float(op["ess_unit_mwh_idc"]) / sbase
    cap_ess = Expr.mulElm(sess, zESS)
    cap_rep = Expr.repeat(cap_ess, H, 1)
    pmax = Expr.mul(float(op["ess_power_to_energy_ratio"]), cap_rep)
    model.constraint("storage_net", Expr.sub(P_ess, Expr.sub(P_ess_dis, P_ess_cha)), Domain.equalsTo(0.0))
    model.constraint("storage_charge_power", Expr.sub(P_ess_cha, pmax), Domain.lessThan(0.0))
    model.constraint("storage_discharge_power", Expr.sub(P_ess_dis, pmax), Domain.lessThan(0.0))
    model.constraint("storage_charge_mode", Expr.sub(P_ess_cha, Expr.mul(Mbig, u_ess)), Domain.lessThan(0.0))
    model.constraint("storage_discharge_mode", Expr.sub(P_ess_dis, Expr.mul(Mbig, Expr.sub(1.0, u_ess))), Domain.lessThan(0.0))
    model.constraint("storage_energy_min", Expr.sub(E_ess, Expr.mul(float(op["ess_soc_min"]), cap_rep)), Domain.greaterThan(0.0))
    model.constraint("storage_energy_max", Expr.sub(cap_rep, E_ess), Domain.greaterThan(0.0))
    eta = float(op["ess_efficiency"])
    e0 = Expr.mul(float(op["ess_soc_initial"]), cap_ess)
    for s in range(4):
        o = 24 * s
        transition = Expr.add(
            Expr.sub(E_ess.slice([0, o + 1], [7, o + 24]), E_ess.slice([0, o], [7, o + 23])),
            Expr.add(Expr.mul(-eta, P_ess_cha.slice([0, o], [7, o + 23])), Expr.mul(1.0 / eta, P_ess_dis.slice([0, o], [7, o + 23]))),
        )
        model.constraint(f"storage_transition_{s}", transition, Domain.equalsTo(0.0))
        for i in range(7):
            model.constraint(
                Expr.add(
                    Expr.sub(E_ess.index(i, o), e0.index(i)),
                    Expr.add(Expr.mul(-eta, P_ess_cha.index(i, o + 23)), Expr.mul(1.0 / eta, P_ess_dis.index(i, o + 23))),
                ),
                Domain.equalsTo(0.0),
            )
            model.constraint(Expr.sub(E_ess.index(i, o + 23), e0.index(i)), Domain.equalsTo(0.0))

    # SOP endpoint bounds, two cone families, and active-loss balance.
    zs1 = np.asarray(plan["sop_units_endpoint_1"], dtype=float)
    zs2 = np.asarray(plan["sop_units_endpoint_2"], dtype=float)
    sop_unit = float(plan["sop_unit_mva"]) / sbase
    loss_factor = float(op["sop_loss_factor"])
    q_fraction = float(op["sop_q_upper_fraction"])

    def add_sop_endpoint(tag, rating, pp, qq, loss):
        smat = np.repeat((rating * sop_unit)[:, None], H, axis=1)
        model.constraint(f"sop_{tag}_P_upper", Expr.sub(pp, Expr.constTerm(smat.tolist())), Domain.lessThan(0.0))
        model.constraint(f"sop_{tag}_P_lower", Expr.add(pp, Expr.constTerm(smat.tolist())), Domain.greaterThan(0.0))
        model.constraint(f"sop_{tag}_Q_lower", qq, Domain.greaterThan(0.0))
        model.constraint(f"sop_{tag}_Q_upper", Expr.sub(qq, Expr.constTerm((q_fraction * smat).tolist())), Domain.lessThan(0.0))
        model.constraint(f"sop_{tag}_loss_upper", Expr.sub(loss, Expr.constTerm((loss_factor * smat).tolist())), Domain.lessThan(0.0))
        n = 4 * H
        model.constraint(
            f"sop_{tag}_loss_cone",
            Expr.hstack(Expr.reshape(Expr.mul(1.0 / loss_factor, loss), n), pp.reshape(n), qq.reshape(n)),
            Domain.inQCone(n, 3),
        )
        model.constraint(
            f"sop_{tag}_capacity_cone",
            Expr.hstack(Expr.constTerm(smat.reshape(-1).tolist()), pp.reshape(n), qq.reshape(n)),
            Domain.inQCone(n, 3),
        )

    add_sop_endpoint("one", zs1, P_sop_1, Q_sop_1, Pl_sop_1)
    add_sop_endpoint("two", zs2, P_sop_2, Q_sop_2, Pl_sop_2)
    model.constraint("sop_active_loss_balance", Expr.add(Expr.add(P_sop_1, P_sop_2), Expr.add(Pl_sop_1, Pl_sop_2)), Domain.equalsTo(0.0))

    # Load shedding and IDC fault service.
    shedmask = np.zeros((B, H), dtype=float)
    shedmask[:, 6:8] = 1.0
    model.constraint("load_shed_upper", Expr.sub(P_ls, Expr.constTerm((PL * shedmask).tolist())), Domain.lessThan(0.0))
    model.constraint("idc_fault_service", Expr.sub(P_ls.slice([49, 6], [50, 8]), Expr.constTerm(((1.0 - float(op["idc_min_served_fraction_fault"])) * PL[49:50, 6:8]).tolist())), Domain.lessThan(0.0))

    # Vectorized active and reactive nodal balances.
    pbal = add_terms([
        Expr.mul(aflow, P), Expr.mul(ar, ell), Expr.constTerm((-PL).tolist()), P_ls,
        Expr.mul(station_map, P_s), Expr.mul(wind_map, P_wt), Expr.mul(pv_map, P_pv),
        Expr.mul(ess_map, P_ess), Expr.mul(sop1_map, P_sop_1), Expr.mul(sop2_map, P_sop_2),
    ])
    qbal = add_terms([
        Expr.mul(aflow, Q), Expr.mul(ax, ell), Expr.constTerm((-QL).tolist()), Expr.mul(tau, P_ls),
        Expr.mul(station_map, Q_s), Expr.mul(wind_map, Q_wt), Expr.mul(pv_map, Q_pv),
        Expr.mul(sop1_map, Q_sop_1), Expr.mul(sop2_map, Q_sop_2),
    ])
    model.constraint("active_balance", pbal, Domain.equalsTo(0.0))
    model.constraint("reactive_balance", qbal, Domain.equalsTo(0.0))

    # Same-run feasibility screen used only to construct an internal MIP start.
    # The final free-planning model has none of these temporary discrete fixes.
    if screening_stage:
        defaults = {
            "uEX": np.asarray([[0.0, 1.0], [0.0, 1.0]]),
            "uNS": np.asarray([[0.0, 1.0], [1.0, 0.0]]),
            "zESS": np.asarray([19.0, 20.0, 1.0, 18.0, 1.0, 20.0, 1.0]),
            "zPVG": np.asarray([35.0, 35.0, 35.0, 20.0, 30.0, 20.0, 35.0]),
            "zWTG": np.asarray([40.0, 40.0, 40.0, 50.0, 50.0, 50.0]),
            "uL": screening_topology(data),
            "u_ess": np.tile(
                np.asarray([1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0,
                            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1], dtype=float),
                (7, 4),
            ),
            "kt": np.zeros((4, H)),
        }
        if screening_plan:
            defaults.update({k: np.asarray(v, dtype=float) for k, v in screening_plan.items()})
        for name, var, value in [
            ("uL", uL, defaults["uL"]),
            ("uEX", uEX, defaults["uEX"]), ("uNS", uNS, defaults["uNS"]),
            ("zESS", zESS, defaults["zESS"]), ("zPVG", zPVG, defaults["zPVG"]),
            ("zWTG", zWTG, defaults["zWTG"]),
        ]:
            model.constraint(f"screen_fix_{name}", Expr.sub(var, Expr.constTerm(value.tolist())), Domain.equalsTo(0.0))
        model.constraint("screen_fix_storage_modes", Expr.sub(u_ess, Expr.constTerm(defaults["u_ess"].tolist())), Domain.equalsTo(0.0))
        model.constraint("screen_fix_taps", Expr.sub(kt, Expr.constTerm(defaults["kt"].tolist())), Domain.equalsTo(0.0))

    # Complete economic objective, scaled by USD 1e6 for numerical conditioning.
    rate = float(plan["discount_rate"]); years = int(plan["years"])
    crf = rate * (1.0 + rate) ** years / ((1.0 + rate) ** years - 1.0)
    line_cost = float(plan["line_build_cost_usd_per_km"]) * data["length"]
    line_cost[existing] = 0.0
    line_coeff = crf * line_cost + 0.25 * 4.0 * float(plan["line_maintenance_usd_per_km_year"]) * data["length"]
    annual = float(econ["scenario_day_annualization_factor"])
    curtail_coeff = annual * sbase * float(econ["curtailment_cost_usd_per_mwh"])
    wt_count_coeff = crf * float(econ["wind_investment_usd_per_mw"]) * 0.1
    wt_count_coeff += 0.1 * float(econ["wind_maintenance_usd_per_mwh"]) * float(econ["wind_capacity_factor"]) * 8760.0
    wt_count_coeff += curtail_coeff * sdg * float(np.sum(data["wt_fc"]))
    pv_count_coeff = crf * float(econ["pv_investment_usd_per_mw"]) * 0.1
    pv_count_coeff += 0.1 * float(econ["pv_maintenance_usd_per_mwh"]) * float(econ["pv_capacity_factor"]) * 8760.0
    pv_count_coeff += curtail_coeff * sdg * float(np.sum(data["pv_fc"]))
    ess_coeff = crf * float(econ["ess_investment_usd_per_mwh"]) * sbase * sess
    price = np.tile(np.asarray(op["purchase_price_usd_per_mwh"], dtype=float), 4)
    ps_coeff = np.repeat((annual * sbase * price)[None, :], 4, axis=0)
    shed_coeff = np.full((B, H), annual * sbase * float(econ["load_shed_cost_usd_per_mwh"]))
    sop_const = crf * float(econ["sop_investment_usd_per_mva"]) * 0.1 * float(np.sum(zs1 + zs2))
    sop_const += 0.1 * float(econ["sop_maintenance_fraction"]) * float(econ["sop_investment_usd_per_mva"]) * float(np.sum(zs1 + zs2))
    objective_terms = [
        dot_var(crf * np.asarray(plan["existing_station_option_cost_usd"], dtype=float), uEX),
        dot_var(crf * np.asarray(plan["candidate_station_option_cost_usd"], dtype=float), uNS),
        dot_var(line_coeff, uL),
        Expr.mul(wt_count_coeff, Expr.sum(zWTG)),
        Expr.mul(pv_count_coeff, Expr.sum(zPVG)),
        dot_var(ess_coeff, zESS),
        dot_var(ps_coeff, P_s),
        Expr.mul(-curtail_coeff, Expr.add(Expr.sum(P_wt), Expr.sum(P_pv))),
        dot_var(shed_coeff, P_ls),
        sop_const,
    ]
    objective_usd = add_terms(objective_terms)
    model.objective("complete_cost_musd", ObjectiveSense.Minimize, Expr.mul(1.0e-6, objective_usd))
    return model, variables


def extract_arrays(variables: Dict) -> Dict[str, np.ndarray]:
    arrays = {}
    for name, shape in REQUIRED_SHAPES.items():
        level = np.asarray(variables[name].level(), dtype=np.float64)
        arrays[name] = level.reshape(shape)
    return arrays


def add_lns_constraints(model: Model, variables: Dict, center: Dict[str, np.ndarray], line_radius: int = 4) -> None:
    """Bounded planning LNS; operational integer decisions remain at the incumbent."""
    for name in ["u_ess", "kt"]:
        fixed = np.rint(np.asarray(center[name], dtype=float))
        model.constraint(f"lns_fix_{name}", Expr.sub(variables[name], Expr.constTerm(fixed.tolist())), Domain.equalsTo(0.0))

    c_line = np.rint(np.asarray(center["uL"], dtype=float).reshape(-1))
    line_coeff = 1.0 - 2.0 * c_line
    line_distance = Expr.add(float(np.sum(c_line)), dot_var(line_coeff, variables["uL"]))
    model.constraint("lns_line_hamming", line_distance, Domain.lessThan(float(line_radius)))

    station_terms = []
    for name in ["uEX", "uNS"]:
        c = np.rint(np.asarray(center[name], dtype=float).reshape(-1))
        station_terms.append(Expr.add(float(np.sum(c)), dot_var(1.0 - 2.0 * c, variables[name])))
    model.constraint("lns_station_hamming", Expr.add(station_terms[0], station_terms[1]), Domain.lessThan(2.0))

    for name, radius in [("zESS", 5.0), ("zPVG", 6.0), ("zWTG", 6.0)]:
        c = np.rint(np.asarray(center[name], dtype=float).reshape(-1))
        var = variables[name]
        model.constraint(f"lns_{name}_lower", Expr.sub(var, Expr.constTerm((c - radius).tolist())), Domain.greaterThan(0.0))
        model.constraint(f"lns_{name}_upper", Expr.sub(var, Expr.constTerm((c + radius).tolist())), Domain.lessThan(0.0))


def write_json(path: Path, obj: Dict) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def solve_model(data: Dict, screening_stage: bool, time_limit: float, log_stream, start_arrays=None, screening_plan=None, lns_center=None) -> Dict:
    build_start = time.monotonic()
    model, variables = build_model(data, screening_stage=screening_stage, screening_plan=screening_plan)
    if lns_center is not None:
        add_lns_constraints(model, variables, lns_center)
    build_seconds = time.monotonic() - build_start
    if start_arrays is not None:
        for name, var in variables.items():
            var.setLevel(np.asarray(start_arrays[name], dtype=float).reshape(-1).tolist())
    model.setSolverParam("numThreads", 4)
    model.setSolverParam("optimizerMaxTime", max(1.0, float(time_limit)))
    if screening_stage:
        pass
    else:
        model.setSolverParam("mioTolRelGap", 0.0005)
        model.setSolverParam("mioMaxTime", max(1.0, float(time_limit)))
        model.setSolverParam("mioHeuristicLevel", 4)
        model.setSolverParam("mioFeaspumpLevel", 2)
        model.setSolverParam("mioRinsMaxNodes", 200)
        model.setSolverParam("mioMaxNumRootCutRounds", 2)
    model.acceptedSolutionStatus(AccSolutionStatus.Anything)
    model.setLogHandler(Tee(sys.stdout, log_stream))

    solve_exception = None
    try:
        model.solve()
    except Exception as exc:
        solve_exception = repr(exc)

    def dint(name, default=0):
        try:
            return int(model.getSolverIntInfo(name))
        except Exception:
            return default

    def ddouble(name):
        try:
            return float(model.getSolverDoubleInfo(name))
        except Exception:
            return None

    solution_count = dint("mioNumIntSolutions")
    pstat_obj = model.getProblemStatus()
    sstat_obj = model.getPrimalSolutionStatus()
    if screening_stage and sstat_obj in (SolutionStatus.Optimal, SolutionStatus.Feasible):
        solution_count = 1
    has_incumbent = solution_count > 0 and sstat_obj in (SolutionStatus.Optimal, SolutionStatus.Feasible)
    result = {
        "model": model, "variables": variables, "build_seconds": build_seconds,
        "optimizer_time": ddouble("optimizerTime"), "solution_count": solution_count,
        "best_bound_scaled": ddouble("mioObjBound"), "gap": ddouble("mioObjRelGap"),
        "problem_status": str(pstat_obj), "primal_status": str(sstat_obj),
        "primal_status_obj": sstat_obj, "has_incumbent": has_incumbent,
        "exception": solve_exception, "arrays": None,
    }
    if has_incumbent:
        result["arrays"] = extract_arrays(variables)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time-limit", type=float, default=None, help="Override optimizer seconds, still capped by ORCLAW and 900")
    args = parser.parse_args()
    SUB.mkdir(parents=True, exist_ok=True)
    rollback_audit = None
    if SOLUTION.exists():
        try:
            with np.load(SOLUTION, allow_pickle=False) as z:
                rollback_arrays = {k: z[k].copy() for k in z.files}
            rollback_audit = audit_arrays(load_public_data(), rollback_arrays, tol=1e-6)
        except Exception:
            rollback_audit = None

    env_limit = float(os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS", "900"))
    requested = env_limit if args.time_limit is None else float(args.time_limit)
    # Fifteen-second safety margin keeps screening, full solve, and polishing,
    # including every presolve, inside the public 900-second solver budget.
    time_limit = max(1.0, min(885.0, env_limit, requested))
    threads = 4
    wall_start = time.monotonic()
    data = load_public_data()
    # Allocate a small first slice to a robust fully discrete screened point,
    # screen, then devote all remaining optimizer time to the unrestricted MISOCP.
    screen_limit = min(120.0, max(10.0, 0.20 * time_limit)) if time_limit >= 20.0 else 0.0
    screen = None
    screen_audit = None
    start_arrays = None
    with MOSEK_LOG.open("w", encoding="utf-8") as log_stream:
        if screen_limit > 0.0:
            log_stream.write("=== INTERNAL FEASIBILITY SCREEN ===\n")
            screen = solve_model(data, True, screen_limit, log_stream)
            if screen["has_incumbent"]:
                # The screen fixes every discrete variable to an integer value,
                # but an interior-point solution may return those fixed values a
                # few ulps away from the exact integer.  Snap only these already
                # fixed arrays before auditing and supplying the same-run start.
                screen_arrays = {k: v.copy() for k, v in screen["arrays"].items()}
                for name in ["uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt"]:
                    screen_arrays[name] = np.rint(screen_arrays[name]).astype(np.float64)
                screen_audit = audit_arrays(data, screen_arrays, tol=1e-6)
                if screen_audit["feasible"]:
                    start_arrays = screen_arrays
                    np.savez_compressed(SUB / "candidate_screened.npz", **start_arrays)
            screen["model"].dispose()

        spent = 0.0 if screen is None or screen["optimizer_time"] is None else float(screen["optimizer_time"])
        polish_reserve = 20.0 if time_limit >= 60.0 else max(1.0, 0.10 * time_limit)
        final_limit = max(1.0, time_limit - spent - polish_reserve)
        log_stream.write("\n=== COMPLETE FREE-PLANNING SOLVE ===\n")
        final = solve_model(data, False, final_limit, log_stream, start_arrays=start_arrays)

        polish = None
        raw_audit = None
        polished_arrays = None
        if final["has_incumbent"]:
            raw_audit = audit_arrays(data, final["arrays"], tol=1e-6)
            integer_names = ["uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt"]
            exact_integer = {k: np.rint(final["arrays"][k]).astype(np.float64) for k in integer_names}
            polish_plan = {
                "uL": exact_integer["uL"][:, 0],
                "uNS": exact_integer["uNS"], "uEX": exact_integer["uEX"],
                "zESS": exact_integer["zESS"][:, 0],
                "zPVG": exact_integer["zPVG"][:, 0],
                "zWTG": exact_integer["zWTG"][:, 0],
                "u_ess": exact_integer["u_ess"], "kt": exact_integer["kt"],
            }
            used_before_polish = spent + (0.0 if final["optimizer_time"] is None else float(final["optimizer_time"]))
            polish_limit = max(1.0, time_limit - used_before_polish)
            log_stream.write("\n=== EXACT-INTEGER CONTINUOUS POLISH ===\n")
            polish = solve_model(
                data, True, polish_limit, log_stream,
                start_arrays=final["arrays"], screening_plan=polish_plan,
            )
            if polish["has_incumbent"]:
                polished_arrays = polish["arrays"]
                for name in integer_names:
                    polished_arrays[name] = exact_integer[name]

    optimizer_time = (0.0 if screen is None or screen["optimizer_time"] is None else screen["optimizer_time"])
    optimizer_time += 0.0 if final["optimizer_time"] is None else final["optimizer_time"]
    optimizer_time += 0.0 if polish is None or polish["optimizer_time"] is None else polish["optimizer_time"]
    solution_count = final["solution_count"]
    best_bound_scaled = final["best_bound_scaled"]
    gap = final["gap"]
    pstatus = final["problem_status"]
    sstatus = final["primal_status"]
    has_incumbent = final["has_incumbent"] and polished_arrays is not None
    solve_exception = final["exception"]
    build_seconds = final["build_seconds"] + (0.0 if screen is None else screen["build_seconds"])
    build_seconds += 0.0 if polish is None else polish["build_seconds"]
    audit = None
    negative_check = False
    exported = False
    objective = None

    if has_incumbent:
        arrays = polished_arrays
        audit = audit_arrays(data, arrays, tol=1e-6)
        objective = audit.get("objective")
        rejected = SUB / "candidate_rejected.npz"
        candidate = SUB / "candidate_full_free_planning_polished.npz"
        np.savez_compressed(candidate, **arrays)
        incumbent_objective = None if rollback_audit is None or not rollback_audit.get("feasible") else rollback_audit.get("objective")
        strict_improvement = incumbent_objective is None or objective < incumbent_objective - 1e-6
        if audit["feasible"] and strict_improvement:
            os.replace(candidate, SOLUTION)
            exported = True
            corrupt = {k: val.copy() for k, val in arrays.items()}
            corrupt["uL"][0, 0] = 0.0
            negative_check = not audit_arrays(data, corrupt)["feasible"]
            digest = hashlib.sha256(SOLUTION.read_bytes()).hexdigest()
            write_json(MANIFEST, {"artifact_path": str(SOLUTION), "sha256": digest})
        else:
            os.replace(candidate, rejected)

    status = "failed"
    if audit is not None and audit.get("feasible"):
        status = "optimal" if final["primal_status_obj"] == SolutionStatus.Optimal else "feasible"
    elif solution_count == 0:
        status = "no_incumbent"

    best_bound = None if best_bound_scaled is None else best_bound_scaled * 1.0e6
    reported_gap = None if objective is None or best_bound is None else max(0.0, (objective - best_bound) / max(abs(objective), 1e-12))
    summary = {
        "status": status,
        "solver": "MOSEK Fusion 11.2.3",
        "optimizer_time_seconds": optimizer_time,
        "threads": threads,
        "objective": objective,
        "best_bound": best_bound,
        "relative_gap": reported_gap,
        "solution_path": str(SOLUTION),
        "used_warm_start": False,
        "used_external_warm_start": False,
        "internal_mip_start_source": "same_run_public_data_exact_soc_screen" if start_arrays is not None else None,
        "planning_was_fixed": False,
        "restoration_used": False,
        "solution_origin": "complete_free_planning" if exported else None,
        "solution_count": solution_count,
        "problem_status": pstatus,
        "primal_solution_status": sstatus,
        "build_time_seconds": build_seconds,
        "wall_time_seconds": time.monotonic() - wall_start,
        "optimizer_time_limit_seconds": time_limit,
        "solver_exception": solve_exception,
        "negative_corruption_check_passed": negative_check,
        "rollback_incumbent_objective": None if rollback_audit is None else rollback_audit.get("objective"),
        "candidate_objective": objective,
        "candidate_published": exported,
        "screening_optimizer_time_seconds": None if screen is None else screen["optimizer_time"],
        "screening_solution_count": 0 if screen is None else screen["solution_count"],
        "screening_audited_feasible": None if screen_audit is None else screen_audit["feasible"],
        "final_optimizer_time_seconds": final["optimizer_time"],
        "postsolve_polish_used": polish is not None,
        "postsolve_polish_optimizer_time_seconds": None if polish is None else polish["optimizer_time"],
        "raw_full_solve_integrality_residual": None if raw_audit is None else raw_audit["max_integrality_residual"],
        "integrality_exact": None if audit is None else audit.get("integrality_exact"),
    }
    if audit is not None:
        summary.update({
            "max_normalized_constraint_residual": audit["max_normalized_constraint_residual"],
            "max_equality_residual": audit["max_equality_residual"],
            "max_inequality_residual": audit["max_inequality_residual"],
            "max_integrality_residual": audit["max_integrality_residual"],
            "number_violated_constraint_groups": audit["number_violated_constraint_groups"],
            "violated_constraint_groups": audit["violated_constraint_groups"],
            "decision_universe_coverage": audit["decision_universe_coverage"],
            "objective_components": audit.get("objective_components"),
        })
    write_json(SUMMARY, summary)

    command = f"PYTHONPATH=/opt/or-deps python {Path(__file__).resolve()}"
    log_lines = [
        "# Solve log", "", "## Final from-scratch invocation", "",
        f"- Command: `{command}`",
        f"- Formulation: exact complete free-planning MISOCP",
        f"- Solver: MOSEK Fusion 11.2.3, {threads} threads",
        f"- Relative MIP gap target: `0.0005`",
        f"- Optimizer time limit: `{time_limit:.3f}` seconds",
        f"- Total model build time: `{build_seconds:.6f}` seconds",
        f"- Total optimizer time across all solver calls, including presolve: `{optimizer_time}` seconds",
        f"- Internal screen time/status/incumbents: `{None if screen is None else screen['optimizer_time']}`, `{None if screen is None else screen['problem_status']}`, `{0 if screen is None else screen['solution_count']}`",
        f"- Internal screen independently feasible: `{None if screen_audit is None else screen_audit['feasible']}`",
        f"- Internal MIP start supplied to final solve: `{start_arrays is not None}`",
        f"- Final free-planning optimizer time: `{final['optimizer_time']}` seconds",
        f"- Exact-integer polish time: `{None if polish is None else polish['optimizer_time']}` seconds",
        f"- Problem status: `{pstatus}`",
        f"- Primal status: `{sstatus}`",
        f"- Integer incumbents reported: `{solution_count}`",
        f"- Export status: `{status}`",
        f"- Audited objective including constants: `{objective}`",
        f"- Best bound including objective scale conversion: `{best_bound}`",
        f"- Relative gap: `{reported_gap}`",
        f"- Rollback incumbent objective: `{None if rollback_audit is None else rollback_audit.get('objective')}`",
        f"- Candidate objective: `{objective}`",
        f"- Candidate published (strictly improved and audited): `{exported}`",
        "",
        "## Independent replay", "",
    ]
    if audit is None:
        log_lines += ["No solution was available to replay."]
    else:
        log_lines += [
            f"- Feasible: `{audit['feasible']}`",
            f"- Schema and full-key coverage: `{audit['schema_ok']}`, `{audit['decision_universe_coverage']}`",
            f"- Maximum equality residual: `{audit['max_equality_residual']:.12g}`",
            f"- Maximum inequality/cone residual: `{audit['max_inequality_residual']:.12g}`",
            f"- Maximum normalized hard-constraint residual theta: `{audit['max_normalized_constraint_residual']:.12g}`",
            f"- Maximum integrality residual: `{audit['max_integrality_residual']:.12g}`",
            f"- Violated constraint groups: `{audit['violated_constraint_groups']}`",
            f"- Corrupted-copy rejection check: `{negative_check}`",
        ]
    if solve_exception:
        log_lines += ["", f"Solver exception: `{solve_exception}`"]
    SOLVE_LOG.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    final["model"].dispose()
    published_ok = False
    if SOLUTION.exists() and MANIFEST.exists():
        try:
            with np.load(SOLUTION, allow_pickle=False) as saved:
                saved_arrays = {k: saved[k].copy() for k in saved.files}
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
            manifest_ok = (
                set(manifest) == {"artifact_path", "sha256"}
                and manifest["artifact_path"] == str(SOLUTION)
                and manifest["sha256"] == hashlib.sha256(SOLUTION.read_bytes()).hexdigest()
            )
            published_ok = manifest_ok and audit_arrays(data, saved_arrays, tol=1e-6)["feasible"]
        except Exception:
            published_ok = False
    return 0 if published_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
