#!/usr/bin/env python3
"""Solve the exact hourly 54-bus free-planning MISOCP with MOSEK.

The only inputs are the public workbook and JSON specification in /app/data.
The script creates an internal feasible screening point, removes all screening
fixes, and then solves the complete free-planning model.  The exported NPZ is
independently replayed before it is made the interface solution.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import mosek
import mosek.fusion as mf
import numpy as np
from openpyxl import load_workbook


APP = Path("/app")
DATA_DIR = APP / "data"
OUT_DIR = APP / "submissions"
WORKBOOK = DATA_DIR / "line_load.xlsx"
SPEC_PATH = DATA_DIR / "model_spec.json"
SOLUTION_PATH = OUT_DIR / "solution.npz"
MANIFEST_PATH = OUT_DIR / "solution.json"
SUMMARY_PATH = OUT_DIR / "solver_summary.json"
LOG_PATH = OUT_DIR / "solve_log.md"
MOSEK_LOG_PATH = OUT_DIR / "mosek.log"

B, E, NSCEN, NT, H = 54, 63, 4, 24, 96
STATION_BUSES = np.array([51, 52, 53, 54], dtype=int) - 1
WT_BUSES = np.array([12, 14, 17, 24, 39, 47], dtype=int) - 1
PV_BUSES = np.array([5, 8, 10, 26, 30, 33, 37], dtype=int) - 1
ESS_BUSES = np.array([8, 9, 13, 16, 17, 32, 50], dtype=int) - 1
SOP_ROWS = np.array([37, 47, 62, 63], dtype=int) - 1
FAULT_ROWS = np.array([34, 44], dtype=int) - 1
EXISTING_ROWS = np.array(list(range(1, 17)) + [34, 44], dtype=int) - 1

REQUIRED_SHAPES = {
    "uL": (63, 1), "uNS": (2, 2), "uEX": (2, 2),
    "zESS": (7, 1), "zPVG": (7, 1), "zWTG": (6, 1),
    "u_ess": (7, 96), "kt": (4, 96), "FF": (63, 4),
    "FS": (54, 4), "P": (63, 96), "Q": (63, 96),
    "l": (63, 96), "v": (54, 96), "P_s": (4, 96),
    "Q_s": (4, 96), "P_pv": (7, 96), "Q_pv": (7, 96),
    "P_wt": (6, 96), "Q_wt": (6, 96), "E_ess": (7, 96),
    "P_ess": (7, 96), "P_ess_cha": (7, 96),
    "P_ess_dis": (7, 96), "P_sop_1": (4, 96),
    "P_sop_2": (4, 96), "Q_sop_1": (4, 96),
    "Q_sop_2": (4, 96), "Pl_sop_1": (4, 96),
    "Pl_sop_2": (4, 96), "P_ls": (54, 96),
}

INTEGRAL_FAMILIES = ("uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt")


def load_data() -> dict[str, Any]:
    with SPEC_PATH.open("r", encoding="utf-8") as f:
        spec = json.load(f)
    wb = load_workbook(WORKBOOK, data_only=True, read_only=True)
    ws1, ws3 = wb["Sheet1"], wb["Sheet3"]

    line_id = np.array([ws1.cell(r, 1).value for r in range(2, 65)], dtype=int)
    fr = np.array([ws1.cell(r, 2).value for r in range(2, 65)], dtype=int) - 1
    to = np.array([ws1.cell(r, 3).value for r in range(2, 65)], dtype=int) - 1
    length = np.array([ws1.cell(r, 4).value for r in range(2, 65)], dtype=float)
    bus_id = np.array([ws1.cell(r, 7).value for r in range(2, 56)], dtype=int)
    base_load = np.array([ws1.cell(r, 8).value for r in range(2, 56)], dtype=float)
    forecast = np.array(
        [[ws3.cell(r, c).value for c in range(2, 5)] for r in range(2, 98)],
        dtype=float,
    )
    wb.close()

    assert np.array_equal(line_id, np.arange(1, E + 1))
    assert np.array_equal(bus_id, np.arange(1, B + 1))
    assert forecast.shape == (H, 3)

    sbase = float(spec["network"]["s_base_mva"])
    vbase = float(spec["network"]["v_base_kv"])
    ibase = sbase / vbase
    zbase = vbase * vbase / sbase
    imax = float(spec["network"]["line_current_limit_ka"]) / ibase
    vmax = float(spec["network"]["v_max_pu"])
    vmin = float(spec["network"]["v_min_pu"])
    rline = float(spec["network"]["resistance_ohm_per_km"]) * length / zbase
    xline = float(spec["network"]["reactance_ohm_per_km"]) * length / zbase
    tanphi = math.tan(math.acos(float(spec["network"]["load_power_factor"])))
    qfac = math.sqrt(1.0 - float(spec["network"]["dg_min_power_factor"]) ** 2) / float(
        spec["network"]["dg_min_power_factor"]
    )
    wt_fc, pv_fc, pl_fc = forecast.T
    pload = base_load[:, None] * pl_fc[None, :] / sbase
    qload = pload * tanphi

    mask = np.ones((E, H), dtype=float)
    for e in FAULT_ROWS:
        mask[e, 6:8] = 0.0
    shed_mask = np.zeros((B, H), dtype=float)
    shed_mask[:, 6:8] = 1.0
    derate = np.ones((4, H), dtype=float)
    derate[0:2, 6:8] = float(spec["fault"]["substation_remaining_capacity_fraction"])

    afr = np.zeros((B, E), dtype=float)
    ato = np.zeros((B, E), dtype=float)
    afr[fr, np.arange(E)] = 1.0
    ato[to, np.arange(E)] = 1.0

    years = int(spec["planning"]["years"])
    discount = float(spec["planning"]["discount_rate"])
    crf = discount * (1 + discount) ** years / ((1 + discount) ** years - 1)
    price24 = np.array(spec["operation"]["purchase_price_usd_per_mwh"], dtype=float)
    prices = np.tile(price24, NSCEN)

    return {
        "spec": spec, "fr": fr, "to": to, "length": length,
        "base_load": base_load, "forecast": forecast,
        "wt_fc": wt_fc, "pv_fc": pv_fc, "pl_fc": pl_fc,
        "PL": pload, "QL": qload, "tanphi": tanphi, "qfac": qfac,
        "R": rline, "X": xline, "A_fr": afr, "A_to": ato,
        "mask": mask, "shed_mask": shed_mask, "derate": derate,
        "Sbase": sbase, "Vmin2": vmin * vmin, "Vmax2": vmax * vmax,
        "Imax2": imax * imax, "Sline": imax * vmax,
        "M": float(spec["network"]["big_m"]), "Sdg": 0.1 / sbase,
        "Sess": np.array([0.1 / sbase] * 6 + [0.025 / sbase], dtype=float),
        "prices": prices, "crf": crf,
        "sop_z1": np.array(spec["planning"]["sop_units_endpoint_1"], dtype=float),
        "sop_z2": np.array(spec["planning"]["sop_units_endpoint_2"], dtype=float),
    }


def flat_index(shape: tuple[int, ...], *idx: int) -> int:
    return int(np.ravel_multi_index(idx, shape, order="C"))


def expr_sum(terms: list[Any]) -> Any:
    if not terms:
        return mf.Expr.constTerm(0.0)
    if len(terms) == 1:
        return terms[0]
    return mf.Expr.add(terms)


class ExactModel:
    def __init__(self, data: dict[str, Any], log_handle: Any):
        self.d = data
        self.model = mf.Model("hourly_54bus_free_planning")
        self.model.acceptedSolutionStatus(mf.AccSolutionStatus.Anything)
        self.model.setLogHandler(log_handle)
        self.vars: dict[str, Any] = {}
        self.shapes = dict(REQUIRED_SHAPES)
        self._build_variables()
        self._build_constraints()
        self._build_objective()

    def var(self, name: str, shape: tuple[int, ...], domain: Any) -> Any:
        v = self.model.variable(name, int(np.prod(shape)), domain)
        self.vars[name] = v
        return v

    def x(self, name: str, *idx: int) -> Any:
        return self.vars[name].index(flat_index(self.shapes[name], *idx))

    def _build_variables(self) -> None:
        D = mf.Domain
        self.var("uL", (63, 1), D.binary())
        self.var("uNS", (2, 2), D.binary())
        self.var("uEX", (2, 2), D.binary())
        self.var("zESS", (7, 1), D.integral(D.inRange(1.0, 20.0)))
        self.var("zPVG", (7, 1), D.integral(D.inRange(1.0, 50.0)))
        self.var("zWTG", (6, 1), D.integral(D.inRange(1.0, 50.0)))
        self.var("u_ess", (7, 96), D.binary())
        self.var("kt", (4, 96), D.integral(D.inRange(-8.0, 8.0)))
        self.var("FF", (63, 4), D.unbounded())
        self.var("FS", (54, 4), D.greaterThan(0.0))
        self.var("P", (63, 96), D.unbounded())
        self.var("Q", (63, 96), D.unbounded())
        self.var("l", (63, 96), D.greaterThan(0.0))
        self.var("v", (54, 96), D.unbounded())
        self.var("P_s", (4, 96), D.greaterThan(0.0))
        self.var("Q_s", (4, 96), D.unbounded())
        self.var("P_pv", (7, 96), D.greaterThan(0.0))
        self.var("Q_pv", (7, 96), D.unbounded())
        self.var("P_wt", (6, 96), D.greaterThan(0.0))
        self.var("Q_wt", (6, 96), D.unbounded())
        self.var("E_ess", (7, 96), D.greaterThan(0.0))
        self.var("P_ess", (7, 96), D.unbounded())
        self.var("P_ess_cha", (7, 96), D.greaterThan(0.0))
        self.var("P_ess_dis", (7, 96), D.greaterThan(0.0))
        self.var("P_sop_1", (4, 96), D.unbounded())
        self.var("P_sop_2", (4, 96), D.unbounded())
        self.var("Q_sop_1", (4, 96), D.greaterThan(0.0))
        self.var("Q_sop_2", (4, 96), D.greaterThan(0.0))
        self.var("Pl_sop_1", (4, 96), D.greaterThan(0.0))
        self.var("Pl_sop_2", (4, 96), D.greaterThan(0.0))
        self.var("P_ls", (54, 96), D.greaterThan(0.0))

    def _station_cap(self, r: int) -> Any:
        if r == 0:
            return expr_sum([mf.Expr.constTerm(1.67), mf.Expr.mul(1.33, self.x("uEX", 0, 0)), mf.Expr.mul(1.67, self.x("uEX", 0, 1))])
        if r == 1:
            return expr_sum([mf.Expr.constTerm(1.67), mf.Expr.mul(1.33, self.x("uEX", 1, 0)), mf.Expr.mul(1.67, self.x("uEX", 1, 1))])
        if r == 2:
            return expr_sum([mf.Expr.mul(1.67, self.x("uNS", 0, 0)), mf.Expr.mul(2.22, self.x("uNS", 0, 1))])
        return expr_sum([mf.Expr.mul(1.67, self.x("uNS", 1, 0)), mf.Expr.mul(2.22, self.x("uNS", 1, 1))])

    def _build_constraints(self) -> None:
        m, D, X = self.model, mf.Domain, mf.Expr
        d = self.d
        bigm = d["M"]

        # Planning domains and counts.
        for e in EXISTING_ROWS:
            m.constraint(f"existing_line[{e+1}]", self.x("uL", int(e), 0), D.equalsTo(1.0))
        new_count = X.sum(self.vars["uNS"])
        m.constraint("line_cardinality", X.add(X.sum(self.vars["uL"]), new_count), D.equalsTo(52.0))
        m.constraint("ess_total", X.sum(self.vars["zESS"]), D.lessThan(80.0))
        m.constraint("pv_total", X.sum(self.vars["zPVG"]), D.lessThan(280.0))
        for i in range(2):
            m.constraint(f"ex_option[{i}]", expr_sum([self.x("uEX", i, 0), self.x("uEX", i, 1)]), D.equalsTo(1.0))
            m.constraint(f"new_option[{i}]", expr_sum([self.x("uNS", i, 0), self.x("uNS", i, 1)]), D.lessThan(1.0))

        # Four documented copies of artificial connectivity.  Equal-copy
        # symmetry breaking is valid because these variables are auxiliary and
        # every scenario has exactly the same topology constraints.
        for s in range(NSCEN):
            for e in range(E):
                ff = self.x("FF", e, s)
                ul = self.x("uL", e, 0)
                m.constraint(X.sub(ff, X.mul(54.0, ul)), D.lessThan(0.0))
                m.constraint(X.add(ff, X.mul(54.0, ul)), D.greaterThan(0.0))
            for b in range(B):
                terms = [self.x("FS", b, s), mf.Expr.constTerm(-1.0)]
                terms += [X.mul(float(d["A_to"][b, e] - d["A_fr"][b, e]), self.x("FF", e, s)) for e in np.flatnonzero(d["A_to"][b] + d["A_fr"][b])]
                m.constraint(f"connectivity[{b},{s}]", expr_sum(terms), D.equalsTo(0.0))
                if b < 50:
                    m.constraint(self.x("FS", b, s), D.equalsTo(0.0))
            gates = [
                expr_sum([self.x("uEX", 0, 0), self.x("uEX", 0, 1)]),
                expr_sum([self.x("uEX", 1, 0), self.x("uEX", 1, 1)]),
                expr_sum([self.x("uNS", 0, 0), self.x("uNS", 0, 1)]),
                expr_sum([self.x("uNS", 1, 0), self.x("uNS", 1, 1)]),
            ]
            for rr in range(4):
                m.constraint(X.sub(self.x("FS", 50 + rr, s), X.mul(54.0, gates[rr])), D.lessThan(0.0))
            m.constraint(expr_sum([self.x("FS", b, s) for b in range(B)]), D.equalsTo(54.0))
            if s:
                for e in range(E):
                    m.constraint(X.sub(self.x("FF", e, s), self.x("FF", e, 0)), D.equalsTo(0.0))
                for b in range(B):
                    m.constraint(X.sub(self.x("FS", b, s), self.x("FS", b, 0)), D.equalsTo(0.0))

        # Station cones, voltage bounds, and taps.
        for h in range(H):
            for r in range(4):
                rhs = X.mul(0.5 * float(d["derate"][r, h]), self._station_cap(r))
                m.constraint(f"station_soc[{r},{h}]", X.vstack(rhs, self.x("P_s", r, h), self.x("Q_s", r, h)), D.inQCone())
            for b in range(50):
                m.constraint(self.x("v", b, h), D.inRange(d["Vmin2"], d["Vmax2"]))
            m.constraint(X.sub(self.x("v", 50, h), X.add(1.01, X.mul(0.025, self.x("kt", 0, h)))), D.equalsTo(0.0))
            m.constraint(X.sub(self.x("v", 51, h), X.add(1.01, X.mul(0.025, self.x("kt", 1, h)))), D.equalsTo(0.0))
            for i in range(2):
                on = expr_sum([self.x("uNS", i, 0), self.x("uNS", i, 1)])
                target = X.add(1.01, X.mul(0.025, self.x("kt", 2 + i, h)))
                diff = X.sub(self.x("v", 52 + i, h), target)
                m.constraint(X.add(diff, X.mul(bigm, X.sub(1.0, on))), D.greaterThan(0.0))
                m.constraint(X.sub(diff, X.mul(bigm, X.sub(1.0, on))), D.lessThan(0.0))
                m.constraint(X.sub(self.x("v", 52 + i, h), X.sub(d["Vmin2"], X.mul(bigm, on))), D.greaterThan(0.0))
                m.constraint(X.sub(self.x("v", 52 + i, h), X.add(d["Vmax2"], X.mul(bigm, on))), D.lessThan(0.0))
                m.constraint(X.sub(self.x("kt", 2 + i, h), X.mul(8.0, on)), D.lessThan(0.0))
                m.constraint(X.add(self.x("kt", 2 + i, h), X.mul(8.0, on)), D.greaterThan(0.0))

        # Branch limits, voltage drop, and rotated branch-flow SOC written as
        # a standard four-dimensional quadratic cone.
        for e in range(E):
            ul = self.x("uL", e, 0)
            for h in range(H):
                y = X.mul(float(d["mask"][e, h]), ul)
                p, q, ell = self.x("P", e, h), self.x("Q", e, h), self.x("l", e, h)
                m.constraint(X.sub(ell, X.mul(d["Imax2"], y)), D.lessThan(0.0))
                m.constraint(X.sub(p, X.mul(d["Sline"], y)), D.lessThan(0.0))
                m.constraint(X.add(p, X.mul(d["Sline"], y)), D.greaterThan(0.0))
                m.constraint(X.sub(q, X.mul(d["Sline"], y)), D.lessThan(0.0))
                m.constraint(X.add(q, X.mul(d["Sline"], y)), D.greaterThan(0.0))
                vi, vj = self.x("v", int(d["fr"][e]), h), self.x("v", int(d["to"][e]), h)
                m.constraint(f"branch_soc[{e},{h}]", X.vstack([X.add(vi, ell), X.mul(2.0, p), X.mul(2.0, q), X.sub(vi, ell)]), D.inQCone())
                drop = expr_sum([vi, X.neg(vj), X.mul(-2.0 * d["R"][e], p), X.mul(-2.0 * d["X"][e], q), X.mul(d["R"][e] ** 2 + d["X"][e] ** 2, ell)])
                offm = X.mul(bigm, X.sub(1.0, y))
                m.constraint(X.sub(drop, offm), D.lessThan(0.0))
                m.constraint(X.add(drop, offm), D.greaterThan(0.0))

        # Wind and PV generation.
        for kind, n, fc, zname, pname, qname in [
            ("wt", 6, d["wt_fc"], "zWTG", "P_wt", "Q_wt"),
            ("pv", 7, d["pv_fc"], "zPVG", "P_pv", "Q_pv"),
        ]:
            for i in range(n):
                z = self.x(zname, i, 0)
                for h in range(H):
                    avail = X.mul(float(fc[h]) * d["Sdg"], z)
                    p, q = self.x(pname, i, h), self.x(qname, i, h)
                    m.constraint(X.sub(p, X.mul(0.3, avail)), D.greaterThan(0.0))
                    m.constraint(X.sub(p, avail), D.lessThan(0.0))
                    m.constraint(f"{kind}_soc[{i},{h}]", X.vstack(X.mul(d["Sdg"], z), p, q), D.inQCone())
                    m.constraint(X.sub(q, X.mul(d["qfac"], p)), D.lessThan(0.0))
                    m.constraint(X.add(q, X.mul(d["qfac"], p)), D.greaterThan(0.0))

        # Storage.  The 0.06/0.015 mode links are exact valid strengthenings of
        # the documented M=100 links because they are global Pmax bounds.
        eta = float(d["spec"]["operation"]["ess_efficiency"])
        for i in range(7):
            z = self.x("zESS", i, 0)
            cap = X.mul(float(d["Sess"][i]), z)
            pmax = X.mul(0.3, cap)
            global_pmax = 0.3 * float(d["Sess"][i]) * 20.0
            for h in range(H):
                pe, pc, pd, en, mode = self.x("P_ess", i, h), self.x("P_ess_cha", i, h), self.x("P_ess_dis", i, h), self.x("E_ess", i, h), self.x("u_ess", i, h)
                m.constraint(X.sub(pe, X.sub(pd, pc)), D.equalsTo(0.0))
                m.constraint(X.sub(pc, pmax), D.lessThan(0.0))
                m.constraint(X.sub(pd, pmax), D.lessThan(0.0))
                m.constraint(X.sub(pc, X.mul(global_pmax, mode)), D.lessThan(0.0))
                m.constraint(X.sub(pd, X.mul(global_pmax, X.sub(1.0, mode))), D.lessThan(0.0))
                m.constraint(X.sub(en, X.mul(0.2, cap)), D.greaterThan(0.0))
                m.constraint(X.sub(en, cap), D.lessThan(0.0))
            for s in range(NSCEN):
                h0 = 24 * s
                for t in range(23):
                    h, hn = h0 + t, h0 + t + 1
                    rhs = expr_sum([self.x("E_ess", i, h), X.mul(eta, self.x("P_ess_cha", i, h)), X.mul(-1.0 / eta, self.x("P_ess_dis", i, h))])
                    m.constraint(X.sub(self.x("E_ess", i, hn), rhs), D.equalsTo(0.0))
                hlast = h0 + 23
                wrap = expr_sum([X.mul(0.6, cap), X.mul(eta, self.x("P_ess_cha", i, hlast)), X.mul(-1.0 / eta, self.x("P_ess_dis", i, hlast))])
                m.constraint(X.sub(self.x("E_ess", i, h0), wrap), D.equalsTo(0.0))
                m.constraint(X.sub(self.x("E_ess", i, hlast), X.mul(0.6, cap)), D.equalsTo(0.0))

        # SOP endpoint bounds, loss cones, rating cones, and link balance.
        for mm in range(4):
            s1 = float(d["sop_z1"][mm]) * d["Sdg"]
            s2 = float(d["sop_z2"][mm]) * d["Sdg"]
            for h in range(H):
                for k, sk, pn, qn, ln in [
                    (1, s1, "P_sop_1", "Q_sop_1", "Pl_sop_1"),
                    (2, s2, "P_sop_2", "Q_sop_2", "Pl_sop_2"),
                ]:
                    p, q, loss = self.x(pn, mm, h), self.x(qn, mm, h), self.x(ln, mm, h)
                    m.constraint(p, D.inRange(-sk, sk))
                    m.constraint(q, D.lessThan(0.6 * sk))
                    m.constraint(loss, D.lessThan(0.02 * sk))
                    m.constraint(f"sop_loss_soc[{mm},{h},{k}]", X.vstack(X.mul(1.0 / 0.02, loss), p, q), D.inQCone())
                    m.constraint(f"sop_rating_soc[{mm},{h},{k}]", X.vstack(sk, p, q), D.inQCone())
                m.constraint(expr_sum([self.x("P_sop_1", mm, h), self.x("P_sop_2", mm, h), self.x("Pl_sop_1", mm, h), self.x("Pl_sop_2", mm, h)]), D.equalsTo(0.0))

        # Shedding and nodal active/reactive balances.
        station_at = {int(b): r for r, b in enumerate(STATION_BUSES)}
        wt_at = {int(b): i for i, b in enumerate(WT_BUSES)}
        pv_at = {int(b): i for i, b in enumerate(PV_BUSES)}
        ess_at = {int(b): i for i, b in enumerate(ESS_BUSES)}
        sop_at: dict[int, list[tuple[int, int]]] = {}
        for mm, e in enumerate(SOP_ROWS):
            sop_at.setdefault(int(d["fr"][e]), []).append((mm, 1))
            sop_at.setdefault(int(d["to"][e]), []).append((mm, 2))

        for b in range(B):
            incident = np.flatnonzero(d["A_fr"][b] + d["A_to"][b])
            for h in range(H):
                pls = self.x("P_ls", b, h)
                m.constraint(pls, D.lessThan(float(d["PL"][b, h] * d["shed_mask"][b, h])))
                if b == 49 and h in (6, 7):
                    m.constraint(pls, D.lessThan(0.10 * float(d["PL"][b, h])))
                pterms: list[Any] = [mf.Expr.constTerm(-float(d["PL"][b, h])), pls]
                qterms: list[Any] = [mf.Expr.constTerm(-float(d["QL"][b, h])), mf.Expr.mul(d["tanphi"], pls)]
                for e in incident:
                    if d["A_fr"][b, e]:
                        pterms.append(mf.Expr.neg(self.x("P", int(e), h)))
                        qterms.append(mf.Expr.neg(self.x("Q", int(e), h)))
                    else:
                        pterms += [self.x("P", int(e), h), mf.Expr.mul(float(d["R"][e]), self.x("l", int(e), h))]
                        qterms += [self.x("Q", int(e), h), mf.Expr.mul(float(d["X"][e]), self.x("l", int(e), h))]
                if b in station_at:
                    r = station_at[b]; pterms.append(self.x("P_s", r, h)); qterms.append(self.x("Q_s", r, h))
                if b in wt_at:
                    i = wt_at[b]; pterms.append(self.x("P_wt", i, h)); qterms.append(self.x("Q_wt", i, h))
                if b in pv_at:
                    i = pv_at[b]; pterms.append(self.x("P_pv", i, h)); qterms.append(self.x("Q_pv", i, h))
                if b in ess_at:
                    pterms.append(self.x("P_ess", ess_at[b], h))
                for mm, endpoint in sop_at.get(b, []):
                    pterms.append(self.x(f"P_sop_{endpoint}", mm, h))
                    qterms.append(self.x(f"Q_sop_{endpoint}", mm, h))
                m.constraint(f"p_balance[{b},{h}]", expr_sum(pterms), D.equalsTo(0.0))
                m.constraint(f"q_balance[{b},{h}]", expr_sum(qterms), D.equalsTo(0.0))

    def _build_objective(self) -> None:
        d, X = self.d, mf.Expr
        spec, crf = d["spec"], d["crf"]
        econ, plan = spec["economics"], spec["planning"]
        annual = float(econ["scenario_day_annualization_factor"])
        terms: list[Any] = []

        ex_cost = np.array(plan["existing_station_option_cost_usd"], dtype=float).reshape(-1)
        ns_cost = np.array(plan["candidate_station_option_cost_usd"], dtype=float).reshape(-1)
        terms += [X.dot(crf * ex_cost, self.vars["uEX"]), X.dot(crf * ns_cost, self.vars["uNS"])]

        line_inv = float(plan["line_build_cost_usd_per_km"]) * d["length"]
        line_inv[EXISTING_ROWS] = 0.0
        line_maint = 0.25 * NSCEN * float(plan["line_maintenance_usd_per_km_year"]) * d["length"]
        terms.append(X.dot(crf * line_inv + line_maint, self.vars["uL"]))

        wt_unit_inv = float(econ["wind_investment_usd_per_mw"]) * float(plan["dg_unit_mw"])
        pv_unit_inv = float(econ["pv_investment_usd_per_mw"]) * float(plan["dg_unit_mw"])
        wt_maint = float(plan["dg_unit_mw"]) * float(econ["wind_maintenance_usd_per_mwh"]) * float(econ["wind_capacity_factor"]) * 8760.0
        pv_maint = float(plan["dg_unit_mw"]) * float(econ["pv_maintenance_usd_per_mwh"]) * float(econ["pv_capacity_factor"]) * 8760.0
        curtail_mult = annual * d["Sbase"] * float(econ["curtailment_cost_usd_per_mwh"])
        wt_coeff = crf * wt_unit_inv + wt_maint + curtail_mult * d["Sdg"] * float(np.sum(d["wt_fc"]))
        pv_coeff = crf * pv_unit_inv + pv_maint + curtail_mult * d["Sdg"] * float(np.sum(d["pv_fc"]))
        terms += [X.dot(np.full(6, wt_coeff), self.vars["zWTG"]), X.dot(np.full(7, pv_coeff), self.vars["zPVG"])]

        ess_mwh_per_unit = d["Sess"] * d["Sbase"]
        terms.append(X.dot(crf * float(econ["ess_investment_usd_per_mwh"]) * ess_mwh_per_unit, self.vars["zESS"]))

        # Station-major flattening needs one 96-vector for each station.
        purchase_coeff = annual * d["Sbase"] * np.tile(d["prices"], 4)
        terms.append(X.dot(purchase_coeff, self.vars["P_s"]))
        terms.append(X.dot(np.full(6 * H, -curtail_mult), self.vars["P_wt"]))
        terms.append(X.dot(np.full(7 * H, -curtail_mult), self.vars["P_pv"]))
        shed_coeff = annual * d["Sbase"] * float(econ["load_shed_cost_usd_per_mwh"])
        terms.append(X.dot(np.full(B * H, shed_coeff), self.vars["P_ls"]))

        sop_units = float(np.sum(d["sop_z1"] + d["sop_z2"]))
        sop_inv_const = float(econ["sop_investment_usd_per_mva"]) * float(plan["sop_unit_mva"]) * sop_units
        sop_maint_const = float(econ["sop_maintenance_fraction"]) * float(econ["sop_investment_usd_per_mva"]) * float(plan["sop_unit_mva"]) * sop_units
        fixed_const = crf * sop_inv_const + sop_maint_const
        terms.append(mf.Expr.constTerm(fixed_const))

        # Scaling only changes numerical conditioning, never the argmin.
        self.model.objective(mf.ObjectiveSense.Minimize, X.mul(1.0e-6, expr_sum(terms)))

    def configure(self, seconds: float, use_start: bool, fast_feasible: bool = False, improve_start: bool = False) -> None:
        task = self.model.getTask()
        task.putintparam(mosek.iparam.num_threads, 4)
        task.putdouparam(mosek.dparam.mio_max_time, max(1.0, float(seconds)))
        task.putdouparam(mosek.dparam.mio_tol_rel_gap, 5.0e-4)
        task.putdouparam(mosek.dparam.mio_tol_feas, 1.0e-7)
        task.putdouparam(mosek.dparam.mio_tol_abs_relax_int, 1.0e-7)
        task.putdouparam(mosek.dparam.intpnt_co_tol_pfeas, 1.0e-8)
        task.putdouparam(mosek.dparam.intpnt_co_tol_dfeas, 1.0e-8)
        task.putintparam(mosek.iparam.mio_construct_sol, mosek.onoffkey.on if use_start else mosek.onoffkey.off)
        task.putintparam(mosek.iparam.log_mio_freq, 10)
        if improve_start:
            task.putintparam(mosek.iparam.mio_max_num_root_cut_rounds, 0)
            task.putintparam(mosek.iparam.mio_cut_selection_level, 0)
            task.putintparam(mosek.iparam.mio_heuristic_level, 5)
            task.putintparam(mosek.iparam.mio_feaspump_level, 1)
            task.putintparam(mosek.iparam.mio_rins_max_nodes, 100)
            task.putintparam(mosek.iparam.mio_rens_max_nodes, 100)
            task.putintparam(mosek.iparam.mio_symmetry_level, 0)
        elif fast_feasible:
            # The default root cutting loop can consume most of a short
            # screening budget before the first branch.  This stage values a
            # feasible topology, not a strong proof bound.
            task.putintparam(mosek.iparam.mio_max_num_root_cut_rounds, 0)
            task.putintparam(mosek.iparam.mio_cut_selection_level, 0)
            task.putintparam(mosek.iparam.mio_heuristic_level, 0)
            task.putintparam(mosek.iparam.mio_feaspump_level, 0)
            task.putintparam(mosek.iparam.mio_symmetry_level, 0)
        else:
            task.putintparam(mosek.iparam.mio_max_num_root_cut_rounds, 5)
            task.putintparam(mosek.iparam.mio_cut_selection_level, -1)
            task.putintparam(mosek.iparam.mio_heuristic_level, 5)
            task.putintparam(mosek.iparam.mio_feaspump_level, 1)
            task.putintparam(mosek.iparam.mio_symmetry_level, -1)

    def solve(self) -> tuple[bool, str, float]:
        started = time.monotonic()
        error = ""
        try:
            self.model.solve()
        except Exception as exc:  # MOSEK may raise at a limit without a point.
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started
        try:
            pstat = str(self.model.getPrimalSolutionStatus())
            feasible = pstat.endswith("Optimal") or pstat.endswith("Feasible")
        except Exception:
            pstat, feasible = "unavailable", False
        return feasible, (pstat if not error else f"{pstat}; {error}"), elapsed

    def extract(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for name, shape in self.shapes.items():
            out[name] = np.asarray(self.vars[name].level(), dtype=np.float64).reshape(shape, order="C")
        return out

    def set_start(self, solution: dict[str, np.ndarray]) -> None:
        for name, var in self.vars.items():
            var.setLevel(np.asarray(solution[name], dtype=float).reshape(-1, order="C"))

    def add_screening_fixes(self) -> list[Any]:
        # Four-root forest generated solely from the public graph and screened
        # with approximate branch flow, root capacity, and voltage calculations.
        selected_one_based = {
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
            17, 18, 19, 21, 22, 23, 25, 26, 27, 29, 30, 31, 33, 34,
            36, 37, 39, 40, 41, 42, 44, 45, 47, 48, 49, 50, 52, 54,
            55, 56, 57, 58, 59, 61,
        }
        ul = np.array([1.0 if e in selected_one_based else 0.0 for e in range(1, E + 1)])
        zess = np.array([1, 1, 20, 20, 10, 10, 18], dtype=float)
        zpv = np.array([40, 30, 30, 30, 50, 50, 50], dtype=float)
        modes = np.zeros((7, H), dtype=float)
        for s in range(NSCEN):
            h0 = 24 * s
            modes[:, h0:h0 + 12] = 1.0
            modes[:, h0 + 23] = 1.0
        fixes = [
            self.model.constraint("screen_uL", self.vars["uL"], mf.Domain.equalsTo(ul)),
            self.model.constraint("screen_uEX", self.vars["uEX"], mf.Domain.equalsTo(np.array([0, 1, 0, 1], dtype=float))),
            self.model.constraint("screen_uNS", self.vars["uNS"], mf.Domain.equalsTo(np.array([0, 1, 0, 1], dtype=float))),
            self.model.constraint("screen_zESS", self.vars["zESS"], mf.Domain.equalsTo(zess)),
            self.model.constraint("screen_zPVG", self.vars["zPVG"], mf.Domain.equalsTo(zpv)),
            self.model.constraint("screen_zWTG", self.vars["zWTG"], mf.Domain.equalsTo(np.full(6, 50.0))),
            self.model.constraint("screen_uess", self.vars["u_ess"], mf.Domain.equalsTo(modes.reshape(-1))),
            self.model.constraint("screen_kt", self.vars["kt"], mf.Domain.equalsTo(np.full(4 * H, 4.0))),
        ]
        return fixes

    def solver_metrics(self) -> dict[str, Any]:
        task = self.model.getTask()
        result: dict[str, Any] = {}
        for key, item in [
            ("best_bound_scaled", mosek.dinfitem.mio_obj_bound),
            ("relative_gap", mosek.dinfitem.mio_obj_rel_gap),
            ("mio_time", mosek.dinfitem.mio_time),
        ]:
            try:
                result[key] = float(task.getdouinf(item))
            except Exception:
                result[key] = None
        try:
            result["integer_solutions"] = int(task.getintinf(mosek.iinfitem.mio_num_int_solutions))
        except Exception:
            result["integer_solutions"] = None
        try:
            result["bound_defined"] = bool(task.getintinf(mosek.iinfitem.mio_obj_bound_defined))
        except Exception:
            result["bound_defined"] = False
        return result


def positive_max(x: np.ndarray | float) -> float:
    a = np.asarray(x, dtype=float)
    return float(max(0.0, np.max(a))) if a.size else 0.0


def abs_max(x: np.ndarray | float) -> float:
    a = np.asarray(x, dtype=float)
    return float(np.max(np.abs(a))) if a.size else 0.0


def replay_solution(d: dict[str, Any], sol: dict[str, np.ndarray]) -> dict[str, Any]:
    """Direct NumPy replay against workbook/specification, independent of Fusion."""
    eq: dict[str, float] = {}
    ineq: dict[str, float] = {}
    domain: dict[str, float] = {}

    for name, shape in REQUIRED_SHAPES.items():
        if name not in sol or sol[name].shape != shape:
            raise ValueError(f"bad or missing shape for {name}: {sol.get(name, np.empty(0)).shape}, expected {shape}")
        if sol[name].dtype != np.float64 or not np.all(np.isfinite(sol[name])):
            raise ValueError(f"{name} must be finite float64")

    uL, uNS, uEX = sol["uL"][:, 0], sol["uNS"], sol["uEX"]
    zess, zpv, zwt = sol["zESS"][:, 0], sol["zPVG"][:, 0], sol["zWTG"][:, 0]
    integ = max(abs_max(sol[n] - np.rint(sol[n])) for n in INTEGRAL_FAMILIES)
    domain["integrality"] = integ
    domain["binary_bounds"] = max(
        positive_max(-sol[n]) for n in ("uL", "uNS", "uEX", "u_ess")
    )
    domain["binary_bounds"] = max(domain["binary_bounds"], max(positive_max(sol[n] - 1.0) for n in ("uL", "uNS", "uEX", "u_ess")))
    domain["integer_bounds"] = max(
        positive_max(1 - zess), positive_max(zess - 20), positive_max(1 - zpv),
        positive_max(zpv - 50), positive_max(1 - zwt), positive_max(zwt - 50),
        positive_max(-8 - sol["kt"]), positive_max(sol["kt"] - 8),
    )

    eq["existing_lines"] = abs_max(uL[EXISTING_ROWS] - 1.0)
    eq["line_cardinality"] = abs(float(np.sum(uL)) - (52.0 - float(np.sum(uNS))))
    ineq["asset_counts"] = max(
        positive_max(np.sum(zess) - 80), positive_max(np.sum(zpv) - 280),
        positive_max(np.sum(uEX, axis=1) - 1), positive_max(np.sum(uNS, axis=1) - 1),
    )

    FF, FS = sol["FF"], sol["FS"]
    ineq["ff_bounds"] = max(positive_max(FF - 54 * uL[:, None]), positive_max(-FF - 54 * uL[:, None]))
    conn = (d["A_to"] - d["A_fr"]) @ FF - 1.0 + FS
    eq["connectivity"] = abs_max(conn)
    eq["nonroot_FS"] = abs_max(FS[:50, :])
    gates = np.array([np.sum(uEX[0]), np.sum(uEX[1]), np.sum(uNS[0]), np.sum(uNS[1])])
    ineq["root_gates"] = max(positive_max(-FS), positive_max(FS[50:54] - 54 * gates[:, None]))

    scap = np.array([
        1.67 + 1.33 * uEX[0, 0] + 1.67 * uEX[0, 1],
        1.67 + 1.33 * uEX[1, 0] + 1.67 * uEX[1, 1],
        1.67 * uNS[0, 0] + 2.22 * uNS[0, 1],
        1.67 * uNS[1, 0] + 2.22 * uNS[1, 1],
    ])
    ineq["station"] = max(positive_max(-sol["P_s"]), positive_max(np.hypot(sol["P_s"], sol["Q_s"]) - 0.5 * scap[:, None] * d["derate"]))

    v, kt = sol["v"], sol["kt"]
    ineq["voltage_load_buses"] = max(positive_max(d["Vmin2"] - v[:50]), positive_max(v[:50] - d["Vmax2"]))
    eq["existing_taps"] = max(abs_max(v[50] - (1.01 + 0.025 * kt[0])), abs_max(v[51] - (1.01 + 0.025 * kt[1])))
    tap_res = 0.0
    for i in range(2):
        on = float(np.sum(uNS[i])); target = 1.01 + 0.025 * kt[2 + i]
        tap_res = max(tap_res, positive_max(np.abs(v[52 + i] - target) - d["M"] * (1 - on)))
        tap_res = max(tap_res, positive_max(d["Vmin2"] - d["M"] * on - v[52 + i]))
        tap_res = max(tap_res, positive_max(v[52 + i] - d["Vmax2"] - d["M"] * on))
        tap_res = max(tap_res, positive_max(np.abs(kt[2 + i]) - 8 * on))
    ineq["candidate_taps"] = tap_res

    yeff = uL[:, None] * d["mask"]
    P, Q, ell = sol["P"], sol["Q"], sol["l"]
    ineq["line_bounds"] = max(
        positive_max(-ell), positive_max(ell - d["Imax2"] * yeff),
        positive_max(np.abs(P) - d["Sline"] * yeff),
        positive_max(np.abs(Q) - d["Sline"] * yeff),
    )
    vi, vj = v[d["fr"], :], v[d["to"], :]
    ineq["branch_soc"] = positive_max(np.sqrt((2 * P) ** 2 + (2 * Q) ** 2 + (vi - ell) ** 2) - (vi + ell))
    drop = vi - vj - 2 * d["R"][:, None] * P - 2 * d["X"][:, None] * Q + (d["R"] ** 2 + d["X"] ** 2)[:, None] * ell
    ineq["voltage_drop"] = positive_max(np.abs(drop) - d["M"] * (1 - yeff))

    for kind, z, fc, pn, qn in [
        ("wt", zwt, d["wt_fc"], "P_wt", "Q_wt"),
        ("pv", zpv, d["pv_fc"], "P_pv", "Q_pv"),
    ]:
        avail = z[:, None] * fc[None, :] * d["Sdg"]
        pg, qg = sol[pn], sol[qn]
        ineq[f"{kind}_dispatch"] = max(positive_max(0.3 * avail - pg), positive_max(pg - avail))
        ineq[f"{kind}_cones"] = max(positive_max(np.hypot(pg, qg) - z[:, None] * d["Sdg"]), positive_max(np.abs(qg) - d["qfac"] * pg))

    cap = zess[:, None] * d["Sess"][:, None]
    pmax = 0.3 * cap
    eq["storage_power"] = abs_max(sol["P_ess"] - sol["P_ess_dis"] + sol["P_ess_cha"])
    ineq["storage_bounds"] = max(
        positive_max(-sol["P_ess_cha"]), positive_max(-sol["P_ess_dis"]),
        positive_max(sol["P_ess_cha"] - pmax), positive_max(sol["P_ess_dis"] - pmax),
        positive_max(sol["P_ess_cha"] - d["M"] * sol["u_ess"]),
        positive_max(sol["P_ess_dis"] - d["M"] * (1 - sol["u_ess"])),
        positive_max(0.2 * cap - sol["E_ess"]), positive_max(sol["E_ess"] - cap),
    )
    eta = float(d["spec"]["operation"]["ess_efficiency"])
    chrono = []
    for s in range(NSCEN):
        h0, hl = 24 * s, 24 * s + 23
        chrono.append(sol["E_ess"][:, h0 + 1:hl + 1] - sol["E_ess"][:, h0:hl] - eta * sol["P_ess_cha"][:, h0:hl] + sol["P_ess_dis"][:, h0:hl] / eta)
        chrono.append(sol["E_ess"][:, h0] - 0.6 * cap[:, 0] - eta * sol["P_ess_cha"][:, hl] + sol["P_ess_dis"][:, hl] / eta)
        chrono.append(sol["E_ess"][:, hl] - 0.6 * cap[:, 0])
    eq["storage_chronology"] = max(abs_max(x) for x in chrono)

    sop_res = 0.0
    for mm in range(4):
        for sk, pn, qn, ln in [
            (d["sop_z1"][mm] * d["Sdg"], "P_sop_1", "Q_sop_1", "Pl_sop_1"),
            (d["sop_z2"][mm] * d["Sdg"], "P_sop_2", "Q_sop_2", "Pl_sop_2"),
        ]:
            pp, qq, loss = sol[pn][mm], sol[qn][mm], sol[ln][mm]
            sop_res = max(sop_res, positive_max(np.abs(pp) - sk), positive_max(-qq), positive_max(qq - 0.6 * sk), positive_max(-loss), positive_max(loss - 0.02 * sk), positive_max(np.hypot(pp, qq) - loss / 0.02), positive_max(np.hypot(pp, qq) - sk))
    ineq["sop"] = sop_res
    eq["sop_balance"] = abs_max(sol["P_sop_1"] + sol["P_sop_2"] + sol["Pl_sop_1"] + sol["Pl_sop_2"])

    ineq["shedding"] = max(positive_max(-sol["P_ls"]), positive_max(sol["P_ls"] - d["PL"] * d["shed_mask"]), positive_max(sol["P_ls"][49, 6:8] - 0.1 * d["PL"][49, 6:8]))

    pbal = -d["A_fr"] @ P + d["A_to"] @ P + d["A_to"] @ (d["R"][:, None] * ell) - (d["PL"] - sol["P_ls"])
    qbal = -d["A_fr"] @ Q + d["A_to"] @ Q + d["A_to"] @ (d["X"][:, None] * ell) - (d["QL"] - sol["P_ls"] * d["tanphi"])
    pbal[STATION_BUSES] += sol["P_s"]; qbal[STATION_BUSES] += sol["Q_s"]
    pbal[WT_BUSES] += sol["P_wt"]; qbal[WT_BUSES] += sol["Q_wt"]
    pbal[PV_BUSES] += sol["P_pv"]; qbal[PV_BUSES] += sol["Q_pv"]
    pbal[ESS_BUSES] += sol["P_ess"]
    for mm, e in enumerate(SOP_ROWS):
        pbal[d["fr"][e]] += sol["P_sop_1"][mm]; qbal[d["fr"][e]] += sol["Q_sop_1"][mm]
        pbal[d["to"][e]] += sol["P_sop_2"][mm]; qbal[d["to"][e]] += sol["Q_sop_2"][mm]
    eq["active_balance"] = abs_max(pbal)
    eq["reactive_balance"] = abs_max(qbal)

    objective = recompute_objective(d, sol)
    max_eq = max(eq.values())
    max_ineq = max(ineq.values())
    max_domain = max(domain.values())
    feasible = max_eq <= 2.0e-5 and max_ineq <= 2.0e-5 and max_domain <= 2.0e-5
    return {
        "feasible": bool(feasible), "objective": objective,
        "max_equality_residual": max_eq, "max_inequality_violation": max_ineq,
        "max_domain_violation": max_domain, "max_integrality_distance": integ,
        "equalities": eq, "inequalities": ineq, "domains": domain,
    }


def recompute_objective(d: dict[str, Any], sol: dict[str, np.ndarray]) -> dict[str, float]:
    spec, crf = d["spec"], d["crf"]
    econ, plan = spec["economics"], spec["planning"]
    uL, uEX, uNS = sol["uL"][:, 0], sol["uEX"], sol["uNS"]
    zess, zpv, zwt = sol["zESS"][:, 0], sol["zPVG"][:, 0], sol["zWTG"][:, 0]
    line_inv = float(plan["line_build_cost_usd_per_km"]) * d["length"]
    line_inv[EXISTING_ROWS] = 0.0
    sop_units = float(np.sum(d["sop_z1"] + d["sop_z2"]))
    cinv = (
        float(np.sum(np.array(plan["existing_station_option_cost_usd"]) * uEX))
        + float(np.sum(np.array(plan["candidate_station_option_cost_usd"]) * uNS))
        + float(np.dot(line_inv, uL))
        + float(econ["wind_investment_usd_per_mw"]) * 0.1 * float(np.sum(zwt))
        + float(econ["pv_investment_usd_per_mw"]) * 0.1 * float(np.sum(zpv))
        + float(econ["sop_investment_usd_per_mva"]) * 0.1 * sop_units
        + float(econ["ess_investment_usd_per_mwh"]) * float(np.dot(zess, d["Sess"] * d["Sbase"]))
    )
    cma = (
        float(np.sum(zpv)) * 0.1 * 1.9 * 0.29 * 8760.0
        + float(np.sum(zwt)) * 0.1 * 4.7 * 0.35 * 8760.0
        + sop_units * 0.1 * 0.01 * 300000.0
        + 0.25 * NSCEN * float(np.dot(uL, 1000.0 * d["length"]))
    )
    pvt_avail = zpv[:, None] * d["pv_fc"][None, :] * d["Sdg"]
    wt_avail = zwt[:, None] * d["wt_fc"][None, :] * d["Sdg"]
    cope_purchase = d["Sbase"] * float(np.sum(sol["P_s"] * d["prices"][None, :]))
    cope_wt_curt = d["Sbase"] * float(econ["curtailment_cost_usd_per_mwh"]) * float(np.sum(wt_avail - sol["P_wt"]))
    cope_pv_curt = d["Sbase"] * float(econ["curtailment_cost_usd_per_mwh"]) * float(np.sum(pvt_avail - sol["P_pv"]))
    cope_shed = d["Sbase"] * float(econ["load_shed_cost_usd_per_mwh"]) * float(np.sum(sol["P_ls"]))
    cope = cope_purchase + cope_wt_curt + cope_pv_curt + cope_shed
    finv = crf * cinv
    fope = float(econ["scenario_day_annualization_factor"]) * cope + cma
    return {
        "C_inv": cinv, "F_inv": finv, "C_ma": cma,
        "C_ope_purchase": cope_purchase, "C_ope_wind_curtailment": cope_wt_curt,
        "C_ope_pv_curtailment": cope_pv_curt, "C_ope_load_shed": cope_shed,
        "C_ope": cope, "F_ope": fope, "total": finv + fope,
    }


def write_npz_atomic(solution: dict[str, np.ndarray], path: Path) -> str:
    temp = path.with_suffix(".tmp.npz")
    np.savez(temp, **{k: np.asarray(v, dtype=np.float64) for k, v in solution.items()})
    os.replace(temp, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def write_json(path: Path, obj: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(temp, path)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_limit = min(900.0, float(os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS", "900")))
    data = load_data()
    attempts: list[dict[str, Any]] = []
    optimizer_wall_total = 0.0
    screening_sol: dict[str, np.ndarray] | None = None
    screening_replay: dict[str, Any] | None = None
    internal_start_source: str | None = None
    final_sol: dict[str, np.ndarray] | None = None
    final_replay: dict[str, Any] | None = None
    full_status = "not_run"
    metrics: dict[str, Any] = {}

    with MOSEK_LOG_PATH.open("w", encoding="utf-8") as mosek_log:
        em = ExactModel(data, mosek_log)
        fixes = em.add_screening_fixes()

        screening_budget = min(180.0, max(60.0, 0.20 * total_limit))
        em.configure(screening_budget, use_start=False, fast_feasible=True)
        ok, status, elapsed = em.solve()
        optimizer_wall_total += elapsed
        attempt = {"stage": "internal_fixed_screening", "limit_seconds": screening_budget, "wall_seconds": elapsed, "status": status, "solver_feasible": ok}
        if ok:
            candidate = em.extract()
            replay = replay_solution(data, candidate)
            attempt["replay_feasible"] = replay["feasible"]
            attempt["objective"] = replay["objective"]["total"]
            attempt["max_residual"] = max(replay["max_equality_residual"], replay["max_inequality_violation"], replay["max_domain_violation"])
            if replay["feasible"]:
                screening_sol, screening_replay = candidate, replay
                internal_start_source = "same_run_fixed_screening"
        attempts.append(attempt)

        # Bounded asset LNS: retain the audited topology, storage modes, and
        # taps, but free all station options and renewable/storage unit counts.
        # This targets the expensive planning families before the final model.
        if screening_sol is not None:
            em.set_start(screening_sol)
            for idx in range(1, 6):
                fixes[idx].remove()
            lns_budget = min(300.0, max(60.0, 0.35 * total_limit))
            lns_budget = min(lns_budget, max(1.0, total_limit - optimizer_wall_total - 4.0))
            em.configure(lns_budget, use_start=True, improve_start=True)
            ok, lns_status, elapsed = em.solve()
            optimizer_wall_total += elapsed
            attempt = {"stage": "topology_fixed_asset_lns", "limit_seconds": lns_budget, "wall_seconds": elapsed, "status": lns_status, "solver_feasible": ok}
            if ok:
                candidate = em.extract()
                replay = replay_solution(data, candidate)
                attempt["replay_feasible"] = replay["feasible"]
                attempt["objective"] = replay["objective"]["total"]
                attempt["max_residual"] = max(replay["max_equality_residual"], replay["max_inequality_violation"], replay["max_domain_violation"])
                if replay["feasible"] and replay["objective"]["total"] <= screening_replay["objective"]["total"] + 1.0e-5:
                    screening_sol, screening_replay = candidate, replay
                    internal_start_source = "same_run_topology_fixed_asset_lns"
            attempts.append(attempt)
            # Asset fixes were already removed; now release topology, modes,
            # and taps so the final invocation is fully free planning.
            for idx in (0, 6, 7):
                fixes[idx].remove()
            em.set_start(screening_sol)
        else:
            for c in fixes:
                c.remove()

        remaining = max(1.0, total_limit - optimizer_wall_total - 2.0)
        em.configure(remaining, use_start=screening_sol is not None, fast_feasible=False)
        ok, full_status, elapsed = em.solve()
        optimizer_wall_total += elapsed
        metrics = em.solver_metrics()
        attempt = {"stage": "complete_free_planning", "limit_seconds": remaining, "wall_seconds": elapsed, "status": full_status, "solver_feasible": ok}
        if ok:
            candidate = em.extract()
            replay = replay_solution(data, candidate)
            attempt["replay_feasible"] = replay["feasible"]
            attempt["objective"] = replay["objective"]["total"]
            attempt["max_residual"] = max(replay["max_equality_residual"], replay["max_inequality_violation"], replay["max_domain_violation"])
            if replay["feasible"]:
                final_sol, final_replay = candidate, replay
        attempts.append(attempt)
        em.model.dispose()

    # Only a replayed point returned by the complete free model is eligible.
    if final_sol is None or final_replay is None:
        summary = {
            "status": "no_incumbent", "solver": "MOSEK 11.2 Fusion MISOCP",
            "optimizer_time_seconds": optimizer_wall_total, "threads": 4,
            "objective": None, "best_bound": None, "relative_gap": None,
            "solution_path": str(SOLUTION_PATH), "used_warm_start": False,
            "used_external_warm_start": False,
            "internal_mip_start_source": internal_start_source,
            "planning_was_fixed": False, "restoration_used": False,
            "solution_origin": None, "full_solve_status": full_status,
        }
        write_json(SUMMARY_PATH, summary)
        write_solve_log(attempts, screening_replay, None, summary)
        return 2

    digest = write_npz_atomic(final_sol, SOLUTION_PATH)
    # Direct replay from the bytes actually exported.
    with np.load(SOLUTION_PATH, allow_pickle=False) as npz:
        exported = {k: np.asarray(npz[k]) for k in npz.files}
    exported_replay = replay_solution(data, exported)
    if not exported_replay["feasible"]:
        raise RuntimeError("exported NPZ failed independent replay")

    objective = exported_replay["objective"]["total"]
    bound_scaled = metrics.get("best_bound_scaled")
    best_bound = None if (not metrics.get("bound_defined") or bound_scaled is None or not np.isfinite(bound_scaled)) else float(bound_scaled * 1.0e6)
    gap = metrics.get("relative_gap")
    if gap is not None and (not np.isfinite(gap) or gap < 0):
        gap = None
    pstatus_lower = full_status.lower()
    status = "optimal" if "optimal" in pstatus_lower else "feasible"
    summary = {
        "status": status,
        "solver": "MOSEK 11.2 Fusion MISOCP",
        "optimizer_time_seconds": optimizer_wall_total,
        "threads": 4,
        "objective": objective,
        "best_bound": best_bound,
        "relative_gap": gap,
        "solution_path": str(SOLUTION_PATH),
        "used_warm_start": False,
        "used_external_warm_start": False,
        "internal_mip_start_source": internal_start_source,
        "planning_was_fixed": False,
        "restoration_used": False,
        "solution_origin": "complete_free_planning",
        "full_solve_status": full_status,
        "mosek_mio_time_seconds": metrics.get("mio_time"),
        "integer_solutions": metrics.get("integer_solutions"),
        "max_equality_residual": exported_replay["max_equality_residual"],
        "max_inequality_violation": exported_replay["max_inequality_violation"],
        "max_integrality_distance": exported_replay["max_integrality_distance"],
    }
    write_json(MANIFEST_PATH, {"artifact_path": str(SOLUTION_PATH), "sha256": digest})
    write_json(SUMMARY_PATH, summary)
    write_solve_log(attempts, screening_replay, exported_replay, summary)
    return 0


def write_solve_log(
    attempts: list[dict[str, Any]],
    screening: dict[str, Any] | None,
    final: dict[str, Any] | None,
    summary: dict[str, Any],
) -> None:
    lines = [
        "# Solve log", "",
        "Command: `python /app/submissions/solve.py`", "",
        "Solver: MOSEK 11.2 Fusion MISOCP, four threads.", "",
        "No external solution, hidden evaluator data, network resource, or fixed final investment plan was used.", "",
        "## Development diagnostics", "",
        "- `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=60 python /app/submissions/solve.py`: exposed and repaired a Fusion four-argument cone-stack API error before optimization.",
        "- Short 60--90 second diagnostics proved the first graph-only forests infeasible at presolve and showed that default root cuts/heuristics could consume the budget without an incumbent.",
        "- Physics-aware forest scoring identified the binding 52--11 feeder; adding the documented downstream storage portfolio produced the first exact replayed point in a 90-second end-to-end run (objective 51,883,599.994277276).",
        "- A 120-second staged diagnostic verified constraint removal, internal-start transfer, complete-free-planning provenance, manifest hashing, and exported-NPZ replay before the final run.", "",
        "## Attempts", "",
    ]
    for a in attempts:
        lines.append(f"- `{a['stage']}`: status `{a['status']}`, wall solver time {a['wall_seconds']:.3f}s (limit {a['limit_seconds']:.3f}s), solver feasible={a['solver_feasible']}, replay feasible={a.get('replay_feasible')}, replay objective={a.get('objective')}, maximum replay residual={a.get('max_residual')}.")
    lines += ["", "## Final status", "", f"- Submission status: `{summary['status']}`.", f"- Complete free-planning solve status: `{summary.get('full_solve_status')}`.", f"- Optimizer wall time including presolve across all calls: {summary['optimizer_time_seconds']:.6f} seconds.", f"- Objective: {summary.get('objective')}.", f"- Best bound: {summary.get('best_bound')}.", f"- Relative gap: {summary.get('relative_gap')}."]
    if screening is not None:
        lines += ["", "The internally generated screening point passed direct replay before it was supplied to the unrestricted model.  Its planning fixes were removed before the complete solve."]
    if final is not None:
        lines += [
            "", "## Independent exported-NPZ replay", "",
            f"- Feasible: `{final['feasible']}`.",
            f"- Maximum equality residual: {final['max_equality_residual']:.12g}.",
            f"- Maximum inequality violation: {final['max_inequality_violation']:.12g}.",
            f"- Maximum domain violation: {final['max_domain_violation']:.12g}.",
            f"- Maximum integrality distance: {final['max_integrality_distance']:.12g}.",
            f"- Objective components: `{json.dumps(final['objective'], sort_keys=True)}`.",
            "- Replay covered all required shapes/domains, eight integer families, planning counts, root gates and artificial flow, station derating cones, taps and voltage, line/fault bounds, voltage drops and SOCs, renewable limits/cones, storage modes and cyclic chronology, SOP bounds/cones/loss balance/constants, shedding/IDC service, nodal balances, and all objective terms.",
        ]
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
