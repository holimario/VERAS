"""Fusion construction of the exact neutral-specification model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from mosek.fusion import Domain, Expr, Model, ObjectiveSense


ARRAY_SHAPES = {
    "uL": (63, 1), "uNS": (2, 2), "uEX": (2, 2),
    "zESS": (7, 1), "zPVG": (7, 1), "zWTG": (6, 1),
    "u_ess": (7, 96), "kt": (4, 96), "FF": (63, 4), "FS": (54, 4),
    "P": (63, 96), "Q": (63, 96), "l": (63, 96), "v": (54, 96),
    "P_s": (4, 96), "Q_s": (4, 96), "P_pv": (7, 96), "Q_pv": (7, 96),
    "P_wt": (6, 96), "Q_wt": (6, 96), "E_ess": (7, 96),
    "P_ess": (7, 96), "P_ess_cha": (7, 96), "P_ess_dis": (7, 96),
    "P_sop_1": (4, 96), "P_sop_2": (4, 96), "Q_sop_1": (4, 96),
    "Q_sop_2": (4, 96), "Pl_sop_1": (4, 96), "Pl_sop_2": (4, 96),
    "P_ls": (54, 96),
}

INTEGER_NAMES = ("uL", "uNS", "uEX", "zESS", "zPVG", "zWTG", "u_ess", "kt")


@dataclass
class BuiltModel:
    model: Model
    variables: dict[str, Any]
    fixed_constant: float


def _var(m: Model, name: str, shape: tuple[int, ...], domain: Domain):
    return m.variable(name, list(shape), domain)


def _integer_domain(lo: float, hi: float, integral: bool):
    base = Domain.inRange(lo, hi)
    return Domain.integral(base) if integral else base


def _scalar(x, *idx):
    return x.index(*idx)


def _fix(m: Model, var, value: np.ndarray, name: str) -> None:
    m.constraint(f"fix_{name}", Expr.sub(var, np.asarray(value, dtype=float)), Domain.equalsTo(0.0))


def build_model(data: dict, *, integral: bool | set[str], fixed_values: dict[str, np.ndarray] | None = None) -> BuiltModel:
    spec = data["spec"]
    net, plan, op, econ = spec["network"], spec["planning"], spec["operation"], spec["economics"]
    m = Model("hourly_54bus_exact")
    V: dict[str, Any] = {}

    def is_integer(name: str) -> bool:
        return bool(integral) if isinstance(integral, bool) else name in integral

    V["uL"] = _var(m, "uL", (63, 1), _integer_domain(0, 1, is_integer("uL")))
    V["uNS"] = _var(m, "uNS", (2, 2), _integer_domain(0, 1, is_integer("uNS")))
    V["uEX"] = _var(m, "uEX", (2, 2), _integer_domain(0, 1, is_integer("uEX")))
    V["zESS"] = _var(m, "zESS", (7, 1), _integer_domain(plan["ess_units_per_bus_min"], plan["ess_units_per_bus_max"], is_integer("zESS")))
    V["zPVG"] = _var(m, "zPVG", (7, 1), _integer_domain(plan["pv_units_per_bus_min"], plan["pv_units_per_bus_max"], is_integer("zPVG")))
    V["zWTG"] = _var(m, "zWTG", (6, 1), _integer_domain(plan["wind_units_per_bus_min"], plan["wind_units_per_bus_max"], is_integer("zWTG")))
    V["u_ess"] = _var(m, "u_ess", (7, 96), _integer_domain(0, 1, is_integer("u_ess")))
    V["kt"] = _var(m, "kt", (4, 96), _integer_domain(-plan["oltc_step_max_abs"], plan["oltc_step_max_abs"], is_integer("kt")))

    V["FF"] = _var(m, "FF", (63, 4), Domain.unbounded())
    V["FS"] = _var(m, "FS", (54, 4), Domain.greaterThan(0.0))
    V["P"] = _var(m, "P", (63, 96), Domain.unbounded())
    V["Q"] = _var(m, "Q", (63, 96), Domain.unbounded())
    V["l"] = _var(m, "l", (63, 96), Domain.greaterThan(0.0))
    V["v"] = _var(m, "v", (54, 96), Domain.unbounded())
    V["P_s"] = _var(m, "P_s", (4, 96), Domain.greaterThan(0.0))
    V["Q_s"] = _var(m, "Q_s", (4, 96), Domain.unbounded())
    V["P_pv"] = _var(m, "P_pv", (7, 96), Domain.greaterThan(0.0))
    V["Q_pv"] = _var(m, "Q_pv", (7, 96), Domain.unbounded())
    V["P_wt"] = _var(m, "P_wt", (6, 96), Domain.greaterThan(0.0))
    V["Q_wt"] = _var(m, "Q_wt", (6, 96), Domain.unbounded())
    V["E_ess"] = _var(m, "E_ess", (7, 96), Domain.greaterThan(0.0))
    V["P_ess"] = _var(m, "P_ess", (7, 96), Domain.unbounded())
    V["P_ess_cha"] = _var(m, "P_ess_cha", (7, 96), Domain.greaterThan(0.0))
    V["P_ess_dis"] = _var(m, "P_ess_dis", (7, 96), Domain.greaterThan(0.0))
    for name in ("P_sop_1", "P_sop_2", "Q_sop_1", "Q_sop_2", "Pl_sop_1", "Pl_sop_2"):
        dom = Domain.greaterThan(0.0) if name.startswith(("Q_", "Pl_")) else Domain.unbounded()
        V[name] = _var(m, name, (4, 96), dom)
    V["P_ls"] = _var(m, "P_ls", (54, 96), Domain.greaterThan(0.0))

    # Planning domains and selection.
    m.constraint("existing_lines", V["uL"].pick(data["existing_rows"].tolist(), [0] * len(data["existing_rows"])), Domain.equalsTo(1.0))
    new_count = Expr.sum(V["uNS"])
    m.constraint("line_count_upper", Expr.add(Expr.sum(V["uL"]), new_count), Domain.lessThan(52.0))
    m.constraint("ess_total", Expr.sum(V["zESS"]), Domain.lessThan(float(plan["ess_units_total_max"])))
    m.constraint("pv_total", Expr.sum(V["zPVG"]), Domain.lessThan(float(plan["pv_units_total_max"])))
    for i in range(2):
        m.constraint(f"ex_select_{i}", Expr.sum(V["uEX"].slice([i, 0], [i + 1, 2])), Domain.lessThan(1.0))
        m.constraint(f"ns_select_{i}", Expr.sum(V["uNS"].slice([i, 0], [i + 1, 2])), Domain.lessThan(1.0))

    # Station capacity expressions.
    sbase = float(net["s_base_mva"])
    scap = []
    for i in range(2):
        excoef = np.asarray(plan["existing_station_expansion_mva"][i], dtype=float) / sbase
        scap.append(Expr.add(float(plan["existing_station_base_mva"][i]) / sbase, Expr.dot(excoef, V["uEX"].slice([i, 0], [i + 1, 2]))))
    for i in range(2):
        nscoef = np.asarray(plan["candidate_station_sizes_mva"][i], dtype=float) / sbase
        scap.append(Expr.dot(nscoef, V["uNS"].slice([i, 0], [i + 1, 2])))
    for r in range(4):
        for h in range(96):
            derate = float(spec["fault"]["substation_remaining_capacity_fraction"]) if (r < 2 and h in data["fault_hours"]) else 1.0
            m.constraint(f"station_cone_{r}_{h}", Expr.vstack(Expr.mul(0.5 * derate, scap[r]), _scalar(V["P_s"], r, h), _scalar(V["Q_s"], r, h)), Domain.inQCone())

    # Connectivity: yL is exactly uL and the fault mask never enters here.
    for s in range(4):
        m.constraint(f"topology_cardinality_{s}", Expr.add(Expr.sum(V["uL"]), new_count), Domain.equalsTo(52.0))
        ff = V["FF"].slice([0, s], [63, s + 1])
        m.constraint(f"ff_upper_{s}", Expr.sub(ff, Expr.mul(54.0, V["uL"])), Domain.lessThan(0.0))
        m.constraint(f"ff_lower_{s}", Expr.add(ff, Expr.mul(54.0, V["uL"])), Domain.greaterThan(0.0))
    m.constraint("connectivity_balance", Expr.add(Expr.sub(Expr.mul(data["A"], V["FF"]), 1.0), V["FS"]), Domain.equalsTo(0.0))
    m.constraint("nonroot_FS", V["FS"].slice([0, 0], [50, 4]), Domain.equalsTo(0.0))
    root_gates = [Expr.sum(V["uEX"].slice([0, 0], [1, 2])), Expr.sum(V["uEX"].slice([1, 0], [2, 2])), Expr.sum(V["uNS"].slice([0, 0], [1, 2])), Expr.sum(V["uNS"].slice([1, 0], [2, 2]))]
    for r in range(4):
        for s in range(4):
            m.constraint(f"root_gate_{r}_{s}", Expr.sub(_scalar(V["FS"], 50 + r, s), Expr.mul(54.0, root_gates[r])), Domain.lessThan(0.0))

    # Voltage and tap semantics.
    vmin2 = float(net["v_min_pu"]) ** 2
    vmax2 = float(net["v_max_pu"]) ** 2
    bigm = float(net["big_m"])
    m.constraint("ordinary_voltage", V["v"].slice([0, 0], [50, 96]), Domain.inRange(vmin2, vmax2))
    for h in range(96):
        for r in range(2):
            m.constraint(f"existing_voltage_{r}_{h}", Expr.sub(_scalar(V["v"], 50 + r, h), Expr.add(1.01, Expr.mul(0.025, _scalar(V["kt"], r, h)))), Domain.equalsTo(0.0))
        for i in range(2):
            on = root_gates[2 + i]
            target = Expr.add(1.01, Expr.mul(0.025, _scalar(V["kt"], 2 + i, h)))
            vv = _scalar(V["v"], 52 + i, h)
            off_slack = Expr.mul(bigm, Expr.sub(1.0, on))
            m.constraint(f"new_v_eq_lo_{i}_{h}", Expr.add(Expr.sub(vv, target), off_slack), Domain.greaterThan(0.0))
            m.constraint(f"new_v_eq_hi_{i}_{h}", Expr.sub(Expr.sub(vv, target), off_slack), Domain.lessThan(0.0))
            m.constraint(f"new_v_off_lo_{i}_{h}", Expr.add(Expr.sub(vv, vmin2), Expr.mul(bigm, on)), Domain.greaterThan(0.0))
            m.constraint(f"new_v_off_hi_{i}_{h}", Expr.sub(Expr.sub(vv, vmax2), Expr.mul(bigm, on)), Domain.lessThan(0.0))
            m.constraint(f"new_tap_lo_{i}_{h}", Expr.add(_scalar(V["kt"], 2 + i, h), Expr.mul(8.0, on)), Domain.greaterThan(0.0))
            m.constraint(f"new_tap_hi_{i}_{h}", Expr.sub(_scalar(V["kt"], 2 + i, h), Expr.mul(8.0, on)), Domain.lessThan(0.0))

    # Branch limits, voltage drop, and branch SOC cones.
    for h in range(96):
        ycoef = data["mask"][:, h:h + 1]
        yeff = Expr.mulElm(ycoef, V["uL"])
        m.constraint(f"line_l_upper_{h}", Expr.sub(V["l"].slice([0, h], [63, h + 1]), Expr.mul(data["I_sq_max"], yeff)), Domain.lessThan(0.0))
        for qname in ("P", "Q"):
            col = V[qname].slice([0, h], [63, h + 1])
            lim = Expr.mul(data["S_line_max"], yeff)
            m.constraint(f"line_{qname}_upper_{h}", Expr.sub(col, lim), Domain.lessThan(0.0))
            m.constraint(f"line_{qname}_lower_{h}", Expr.add(col, lim), Domain.greaterThan(0.0))
        vcol = V["v"].slice([0, h], [54, h + 1])
        pcol = V["P"].slice([0, h], [63, h + 1])
        qcol = V["Q"].slice([0, h], [63, h + 1])
        lcol = V["l"].slice([0, h], [63, h + 1])
        drop = Expr.add(Expr.sub(Expr.mul(data["A_fr"].T, vcol), Expr.mul(data["A_to"].T, vcol)), Expr.add(Expr.mulElm(-2.0 * data["R"][:, None], pcol), Expr.add(Expr.mulElm(-2.0 * data["X"][:, None], qcol), Expr.mulElm((data["R"] ** 2 + data["X"] ** 2)[:, None], lcol))))
        dlim = Expr.mul(bigm, Expr.sub(1.0, yeff))
        m.constraint(f"drop_upper_{h}", Expr.sub(drop, dlim), Domain.lessThan(0.0))
        m.constraint(f"drop_lower_{h}", Expr.add(drop, dlim), Domain.greaterThan(0.0))
        for e in range(63):
            vf = _scalar(V["v"], int(data["fr"][e]), h)
            ll = _scalar(V["l"], e, h)
            cone_vec = Expr.vstack(
                Expr.vstack(Expr.add(vf, ll), Expr.mul(2.0, _scalar(V["P"], e, h))),
                Expr.vstack(Expr.mul(2.0, _scalar(V["Q"], e, h)), Expr.sub(vf, ll)),
            )
            m.constraint(f"branch_soc_{e}_{h}", cone_vec, Domain.inQCone())

    # Renewable dispatch, installed capacity, and actual-P PF bounds.
    for name, zname, fc, ng in (("pv", "zPVG", data["pv_fc"], 7), ("wt", "zWTG", data["wt_fc"], 6)):
        pg, qg, zg = V[f"P_{name}"], V[f"Q_{name}"], V[zname]
        for i in range(ng):
            for h in range(96):
                avail = Expr.mul(float(fc[h] * data["Sdg_rated"]), _scalar(zg, i, 0))
                p = _scalar(pg, i, h); q = _scalar(qg, i, h)
                m.constraint(f"{name}_avail_lo_{i}_{h}", Expr.sub(p, Expr.mul(float(op["dg_min_dispatch_fraction"]), avail)), Domain.greaterThan(0.0))
                m.constraint(f"{name}_avail_hi_{i}_{h}", Expr.sub(p, avail), Domain.lessThan(0.0))
                m.constraint(f"{name}_capacity_{i}_{h}", Expr.vstack(Expr.mul(data["Sdg_rated"], _scalar(zg, i, 0)), p, q), Domain.inQCone())
                m.constraint(f"{name}_pf_hi_{i}_{h}", Expr.sub(q, Expr.mul(data["dg_qfac"], p)), Domain.lessThan(0.0))
                m.constraint(f"{name}_pf_lo_{i}_{h}", Expr.add(q, Expr.mul(data["dg_qfac"], p)), Domain.greaterThan(0.0))

    # Storage, including the unusual hour-24 wrap into hour 1 and E[24]=E0.
    eta = float(op["ess_efficiency"])
    for i in range(7):
        cap = Expr.mul(float(data["Sess_rated"][i]), _scalar(V["zESS"], i, 0))
        pmax = Expr.mul(float(op["ess_power_to_energy_ratio"]), cap)
        e0 = Expr.mul(float(op["ess_soc_initial"]), cap)
        for h in range(96):
            p, ch, dis = _scalar(V["P_ess"], i, h), _scalar(V["P_ess_cha"], i, h), _scalar(V["P_ess_dis"], i, h)
            mode, ee = _scalar(V["u_ess"], i, h), _scalar(V["E_ess"], i, h)
            m.constraint(f"ess_power_def_{i}_{h}", Expr.sub(p, Expr.sub(dis, ch)), Domain.equalsTo(0.0))
            m.constraint(f"ess_charge_cap_{i}_{h}", Expr.sub(ch, pmax), Domain.lessThan(0.0))
            m.constraint(f"ess_discharge_cap_{i}_{h}", Expr.sub(dis, pmax), Domain.lessThan(0.0))
            m.constraint(f"ess_charge_mode_{i}_{h}", Expr.sub(ch, Expr.mul(bigm, mode)), Domain.lessThan(0.0))
            m.constraint(f"ess_discharge_mode_{i}_{h}", Expr.sub(dis, Expr.mul(bigm, Expr.sub(1.0, mode))), Domain.lessThan(0.0))
            m.constraint(f"ess_energy_lo_{i}_{h}", Expr.sub(ee, Expr.mul(float(op["ess_soc_min"]), cap)), Domain.greaterThan(0.0))
            m.constraint(f"ess_energy_hi_{i}_{h}", Expr.sub(ee, cap), Domain.lessThan(0.0))
        for s in range(4):
            o = 24 * s
            for t in range(23):
                rhs = Expr.add(_scalar(V["E_ess"], i, o + t), Expr.sub(Expr.mul(eta, _scalar(V["P_ess_cha"], i, o + t)), Expr.mul(1.0 / eta, _scalar(V["P_ess_dis"], i, o + t))))
                m.constraint(f"ess_chron_{i}_{s}_{t}", Expr.sub(_scalar(V["E_ess"], i, o + t + 1), rhs), Domain.equalsTo(0.0))
            wrap_rhs = Expr.add(e0, Expr.sub(Expr.mul(eta, _scalar(V["P_ess_cha"], i, o + 23)), Expr.mul(1.0 / eta, _scalar(V["P_ess_dis"], i, o + 23))))
            m.constraint(f"ess_wrap_{i}_{s}", Expr.sub(_scalar(V["E_ess"], i, o), wrap_rhs), Domain.equalsTo(0.0))
            m.constraint(f"ess_terminal_{i}_{s}", Expr.sub(_scalar(V["E_ess"], i, o + 23), e0), Domain.equalsTo(0.0))

    # Fixed SOP semantics independent of corresponding branch build status.
    s1 = np.asarray(plan["sop_units_endpoint_1"], dtype=float) * float(plan["sop_unit_mva"]) / sbase
    s2 = np.asarray(plan["sop_units_endpoint_2"], dtype=float) * float(plan["sop_unit_mva"]) / sbase
    loss = float(op["sop_loss_factor"])
    for i in range(4):
        for h in range(96):
            for k, sk in ((1, s1[i]), (2, s2[i])):
                p = _scalar(V[f"P_sop_{k}"], i, h); q = _scalar(V[f"Q_sop_{k}"], i, h); ploss = _scalar(V[f"Pl_sop_{k}"], i, h)
                m.constraint(f"sop_p_{k}_{i}_{h}", p, Domain.inRange(-float(sk), float(sk)))
                m.constraint(f"sop_q_{k}_{i}_{h}", q, Domain.lessThan(float(op["sop_q_upper_fraction"]) * float(sk)))
                m.constraint(f"sop_loss_{k}_{i}_{h}", ploss, Domain.lessThan(loss * float(sk)))
                m.constraint(f"sop_loss_cone_{k}_{i}_{h}", Expr.vstack(Expr.mul(1.0 / loss, ploss), p, q), Domain.inQCone())
                m.constraint(f"sop_capacity_cone_{k}_{i}_{h}", Expr.vstack(float(sk), p, q), Domain.inQCone())
            m.constraint(f"sop_balance_{i}_{h}", Expr.add(Expr.add(_scalar(V["P_sop_1"], i, h), _scalar(V["P_sop_2"], i, h)), Expr.add(_scalar(V["Pl_sop_1"], i, h), _scalar(V["Pl_sop_2"], i, h))), Domain.equalsTo(0.0))

    # Shedding bounds and IDC service requirement.
    m.constraint("shed_upper", Expr.sub(V["P_ls"], data["PL"] * data["shed_mask"]), Domain.lessThan(0.0))
    for h in data["fault_hours"]:
        m.constraint(f"idc_service_{h}", _scalar(V["P_ls"], 49, int(h)), Domain.lessThan(0.10 * float(data["PL"][49, h])))

    # Nodal active and reactive balances.
    station_inc = np.zeros((54, 4)); station_inc[data["station_buses"], np.arange(4)] = 1.0
    pv_inc = np.zeros((54, 7)); pv_inc[data["pv_buses"], np.arange(7)] = 1.0
    wt_inc = np.zeros((54, 6)); wt_inc[data["wt_buses"], np.arange(6)] = 1.0
    ess_inc = np.zeros((54, 7)); ess_inc[data["ess_buses"], np.arange(7)] = 1.0
    sop1_inc = np.zeros((54, 4)); sop1_inc[data["fr"][data["sop_rows"]], np.arange(4)] = 1.0
    sop2_inc = np.zeros((54, 4)); sop2_inc[data["to"][data["sop_rows"]], np.arange(4)] = 1.0
    for h in range(96):
        pcol, qcol, lcol = V["P"].slice([0, h], [63, h + 1]), V["Q"].slice([0, h], [63, h + 1]), V["l"].slice([0, h], [63, h + 1])
        pls = V["P_ls"].slice([0, h], [54, h + 1])
        pb = Expr.add(Expr.add(Expr.mul(-data["A_fr"], pcol), Expr.mul(data["A_to"], pcol)), Expr.add(Expr.mul(data["A_to"], Expr.mulElm(data["R"][:, None], lcol)), Expr.sub(pls, data["PL"][:, h:h + 1])))
        pb = Expr.add(pb, Expr.add(Expr.mul(station_inc, V["P_s"].slice([0, h], [4, h + 1])), Expr.add(Expr.mul(pv_inc, V["P_pv"].slice([0, h], [7, h + 1])), Expr.add(Expr.mul(wt_inc, V["P_wt"].slice([0, h], [6, h + 1])), Expr.add(Expr.mul(ess_inc, V["P_ess"].slice([0, h], [7, h + 1])), Expr.add(Expr.mul(sop1_inc, V["P_sop_1"].slice([0, h], [4, h + 1])), Expr.mul(sop2_inc, V["P_sop_2"].slice([0, h], [4, h + 1]))))))))
        qls = Expr.mul(data["load_qfac"], pls)
        qb = Expr.add(Expr.add(Expr.mul(-data["A_fr"], qcol), Expr.mul(data["A_to"], qcol)), Expr.add(Expr.mul(data["A_to"], Expr.mulElm(data["X"][:, None], lcol)), Expr.sub(qls, data["QL"][:, h:h + 1])))
        qb = Expr.add(qb, Expr.add(Expr.mul(station_inc, V["Q_s"].slice([0, h], [4, h + 1])), Expr.add(Expr.mul(pv_inc, V["Q_pv"].slice([0, h], [7, h + 1])), Expr.add(Expr.mul(wt_inc, V["Q_wt"].slice([0, h], [6, h + 1])), Expr.add(Expr.mul(sop1_inc, V["Q_sop_1"].slice([0, h], [4, h + 1])), Expr.mul(sop2_inc, V["Q_sop_2"].slice([0, h], [4, h + 1])))))))
        m.constraint(f"active_balance_{h}", pb, Domain.equalsTo(0.0))
        m.constraint(f"reactive_balance_{h}", qb, Domain.equalsTo(0.0))

    # Full objective, including both fixed SOP constants.
    obj = Expr.constTerm(0.0)
    obj = Expr.add(obj, Expr.sum(Expr.mulElm(np.asarray(plan["existing_station_option_cost_usd"], dtype=float), V["uEX"])))
    obj = Expr.add(obj, Expr.sum(Expr.mulElm(np.asarray(plan["candidate_station_option_cost_usd"], dtype=float), V["uNS"])))
    line_inv = float(plan["line_build_cost_usd_per_km"]) * data["length"]
    line_inv[data["existing_rows"]] = 0.0
    obj = Expr.add(obj, Expr.sum(Expr.mulElm(line_inv[:, None], V["uL"])))
    obj = Expr.add(obj, Expr.mul(float(econ["wind_investment_usd_per_mw"]) * float(plan["dg_unit_mw"]), Expr.sum(V["zWTG"])))
    obj = Expr.add(obj, Expr.mul(float(econ["pv_investment_usd_per_mw"]) * float(plan["dg_unit_mw"]), Expr.sum(V["zPVG"])))
    obj = Expr.add(obj, Expr.mul(float(econ["ess_investment_usd_per_mwh"]) * sbase, Expr.dot(data["Sess_rated"], V["zESS"])))
    fixed_sop_inv = float(econ["sop_investment_usd_per_mva"]) * float(plan["sop_unit_mva"]) * float(np.sum(plan["sop_units_endpoint_1"]) + np.sum(plan["sop_units_endpoint_2"]))
    investment = Expr.add(obj, fixed_sop_inv)
    annual = Expr.mul(data["crf"], investment)

    pv_ma_coef = float(plan["dg_unit_mw"]) * float(econ["pv_maintenance_usd_per_mwh"]) * float(econ["pv_capacity_factor"]) * 8760.0
    wt_ma_coef = float(plan["dg_unit_mw"]) * float(econ["wind_maintenance_usd_per_mwh"]) * float(econ["wind_capacity_factor"]) * 8760.0
    annual = Expr.add(annual, Expr.mul(pv_ma_coef, Expr.sum(V["zPVG"])))
    annual = Expr.add(annual, Expr.mul(wt_ma_coef, Expr.sum(V["zWTG"])))
    fixed_sop_ma = float(np.sum(plan["sop_units_endpoint_1"]) + np.sum(plan["sop_units_endpoint_2"])) * float(plan["sop_unit_mva"]) * float(econ["sop_maintenance_fraction"]) * float(econ["sop_investment_usd_per_mva"])
    annual = Expr.add(annual, fixed_sop_ma)
    annual = Expr.add(annual, Expr.sum(Expr.mulElm((float(plan["line_maintenance_usd_per_km_year"]) * data["length"])[:, None], V["uL"])))

    ope = Expr.sum(Expr.mulElm(np.tile((sbase * data["price96"])[None, :], (4, 1)), V["P_s"]))
    wt_av_coef = data["Sdg_rated"] * float(np.sum(data["wt_fc"]))
    pv_av_coef = data["Sdg_rated"] * float(np.sum(data["pv_fc"]))
    wt_curt = Expr.sub(Expr.mul(wt_av_coef, Expr.sum(V["zWTG"])), Expr.sum(V["P_wt"]))
    pv_curt = Expr.sub(Expr.mul(pv_av_coef, Expr.sum(V["zPVG"])), Expr.sum(V["P_pv"]))
    ope = Expr.add(ope, Expr.mul(sbase * float(econ["curtailment_cost_usd_per_mwh"]), wt_curt))
    ope = Expr.add(ope, Expr.mul(sbase * float(econ["curtailment_cost_usd_per_mwh"]), pv_curt))
    ope = Expr.add(ope, Expr.mul(sbase * float(econ["load_shed_cost_usd_per_mwh"]), Expr.sum(V["P_ls"])))
    annual = Expr.add(annual, Expr.mul(float(econ["scenario_day_annualization_factor"]), ope))
    m.objective("full_reported_total", ObjectiveSense.Minimize, annual)

    if fixed_values:
        for name, value in fixed_values.items():
            _fix(m, V[name], value, name)

    fixed_constant = data["crf"] * fixed_sop_inv + fixed_sop_ma
    return BuiltModel(m, V, fixed_constant)


def extract_solution(built: BuiltModel) -> dict[str, np.ndarray]:
    out = {}
    for name, shape in ARRAY_SHAPES.items():
        out[name] = np.asarray(built.variables[name].level(), dtype=np.float64).reshape(shape)
    return out


def set_solution_levels(built: BuiltModel, solution: dict[str, np.ndarray]) -> None:
    for name in ARRAY_SHAPES:
        built.variables[name].setLevel(np.asarray(solution[name], dtype=float).reshape(-1).tolist())
