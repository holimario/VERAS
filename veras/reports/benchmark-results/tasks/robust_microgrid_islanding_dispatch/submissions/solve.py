import csv
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"

NUMERIC_COLUMNS = [
    "power_kw",
    "charge_kw",
    "discharge_kw",
    "soc_kwh",
    "curtail_kw",
    "shed_kw",
    "reserve_kw",
]
REQUIRED_COLUMNS = [
    "row_type",
    "scenario_id",
    "hour",
    "asset_id",
    "commit",
    "power_kw",
    "charge_kw",
    "discharge_kw",
    "soc_kwh",
    "curtail_kw",
    "shed_kw",
    "reserve_kw",
]
EPS = 1e-6


def read_csv(path):
    path = Path(path)
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def as_float(value):
    if value == "" or value is None:
        return 0.0
    return float(value)


def safe_value(value):
    if abs(value) <= 5e-9:
        return 0.0
    if value < 0.0 and value > -1e-7:
        return 0.0
    return value


def fmt(value):
    return f"{safe_value(value):.8f}"


def load_data():
    with (DATA_DIR / "config.json").open() as fh:
        config = json.load(fh)

    time_rows = read_csv(DATA_DIR / "time_periods.csv")
    gen_rows = read_csv(DATA_DIR / "generators.csv")
    storage_rows = read_csv(DATA_DIR / "storage_units.csv")
    ev_rows = read_csv(DATA_DIR / "ev_clusters.csv")
    renewable_rows = read_csv(DATA_DIR / "renewable_forecast.csv")
    grid_rows = read_csv(DATA_DIR / "grid_limits.csv")
    dr_rows = read_csv(DATA_DIR / "demand_response.csv")
    scenario_rows = read_csv(DATA_DIR / "scenarios.csv")
    scenario_hourly_rows = read_csv(DATA_DIR / "scenario_hourly.csv")

    hours = [int(r["hour"]) for r in time_rows]
    generators = [r["generator_id"] for r in gen_rows]
    storages = [r["storage_id"] for r in storage_rows]
    evs = [r["cluster_id"] for r in ev_rows]
    scenarios = [r["scenario_id"] for r in scenario_rows]
    renewables = ["pv", "wind"]

    time_data = {
        int(r["hour"]): {
            "base_load_kw": float(r["base_load_kw"]),
            "critical_load_kw": float(r["critical_load_kw"]),
            "tariff_import_per_kwh": float(r["tariff_import_per_kwh"]),
        }
        for r in time_rows
    }
    generators_data = {
        r["generator_id"]: {
            "min_kw": float(r["min_kw"]),
            "max_kw": float(r["max_kw"]),
            "ramp_up_kw": float(r["ramp_up_kw"]),
            "ramp_down_kw": float(r["ramp_down_kw"]),
            "startup_cost": float(r["startup_cost"]),
            "no_load_cost_per_hour": float(r["no_load_cost_per_hour"]),
            "marginal_cost_per_kwh": float(r["marginal_cost_per_kwh"]),
            "emission_kg_per_kwh": float(r["emission_kg_per_kwh"]),
            "min_up_hours": int(r["min_up_hours"]),
            "min_down_hours": int(r["min_down_hours"]),
            "inertia_score": float(r["inertia_score"]),
        }
        for r in gen_rows
    }
    storage_data = {
        r["storage_id"]: {
            "min_soc_kwh": float(r["min_soc_kwh"]),
            "max_soc_kwh": float(r["max_soc_kwh"]),
            "initial_soc_kwh": float(r["initial_soc_kwh"]),
            "max_charge_kw": float(r["max_charge_kw"]),
            "max_discharge_kw": float(r["max_discharge_kw"]),
            "charge_efficiency": float(r["charge_efficiency"]),
            "discharge_efficiency": float(r["discharge_efficiency"]),
            "cycle_cost_per_kwh": float(r["cycle_cost_per_kwh"]),
            "grid_forming": int(r["grid_forming"]),
            "inertia_score_per_100kwh": float(r["inertia_score_per_100kwh"]),
        }
        for r in storage_rows
    }
    ev_data = {
        r["cluster_id"]: {
            "arrival_hour": int(r["arrival_hour"]),
            "departure_hour": int(r["departure_hour"]),
            "initial_soc_kwh": float(r["initial_soc_kwh"]),
            "target_soc_kwh": float(r["target_soc_kwh"]),
            "min_soc_kwh": float(r["min_soc_kwh"]),
            "max_soc_kwh": float(r["max_soc_kwh"]),
            "max_charge_kw": float(r["max_charge_kw"]),
            "max_discharge_kw": float(r["max_discharge_kw"]),
            "charge_efficiency": float(r["charge_efficiency"]),
            "discharge_efficiency": float(r["discharge_efficiency"]),
            "cycle_cost_per_kwh": float(r["cycle_cost_per_kwh"]),
            "v2g_allowed": int(r["v2g_allowed"]),
        }
        for r in ev_rows
    }
    renewable_data = {
        int(r["hour"]): {"pv": float(r["pv_kw"]), "wind": float(r["wind_kw"])}
        for r in renewable_rows
    }
    grid_caps = {int(r["hour"]): float(r["import_cap_kw"]) for r in grid_rows}
    dr_data = {
        int(r["hour"]): {
            "max_curtail_kw": float(r["max_curtail_kw"]),
            "rebound_hour": int(r["rebound_hour"]),
            "rebound_fraction": float(r["rebound_fraction"]),
            "discomfort_cost_per_kwh": float(r["discomfort_cost_per_kwh"]),
        }
        for r in dr_rows
    }
    scenario_prob = {
        r["scenario_id"]: float(r["probability"])
        for r in scenario_rows
    }
    scenario_hourly = defaultdict(dict)
    for r in scenario_hourly_rows:
        scenario_hourly[r["scenario_id"]][int(r["hour"])] = {
            "load_multiplier": float(r["load_multiplier"]),
            "pv_multiplier": float(r["pv_multiplier"]),
            "wind_multiplier": float(r["wind_multiplier"]),
            "grid_import_multiplier": float(r["grid_import_multiplier"]),
            "islanded": int(r["islanded"]),
            "reserve_requirement_kw": float(r["reserve_requirement_kw"]),
            "ev_availability_multiplier": float(r["ev_availability_multiplier"]),
            "max_critical_shed_kw": float(r["max_critical_shed_kw"]),
        }

    rebound_sources = defaultdict(list)
    for hour, params in dr_data.items():
        rebound_sources[params["rebound_hour"]].append((hour, params["rebound_fraction"]))

    return {
        "config": config,
        "hours": hours,
        "generators": generators,
        "storages": storages,
        "evs": evs,
        "renewables": renewables,
        "scenarios": scenarios,
        "time_data": time_data,
        "generators_data": generators_data,
        "storage_data": storage_data,
        "ev_data": ev_data,
        "renewable_data": renewable_data,
        "grid_caps": grid_caps,
        "dr_data": dr_data,
        "scenario_prob": scenario_prob,
        "scenario_hourly": scenario_hourly,
        "rebound_sources": rebound_sources,
    }


def connected(ev_params, hour):
    return ev_params["arrival_hour"] <= hour < ev_params["departure_hour"]


def build_model(data, time_limit):
    cfg = data["config"]
    hours = data["hours"]
    generators = data["generators"]
    storages = data["storages"]
    evs = data["evs"]
    renewables = data["renewables"]
    scenarios = data["scenarios"]

    model = Model("robust_microgrid_islanding_dispatch")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("randomization/permutationseed", 0)

    u = {}
    su = {}
    sd = {}
    for g in generators:
        for h in hours:
            u[g, h] = model.addVar(vtype="B", name=f"u[{g},{h}]")
            su[g, h] = model.addVar(vtype="B", name=f"su[{g},{h}]")
            sd[g, h] = model.addVar(vtype="B", name=f"sd[{g},{h}]")

    pg = {}
    rg = {}
    ch_b = {}
    dis_b = {}
    soc_b = {}
    rb = {}
    z_b = {}
    ch_e = {}
    dis_e = {}
    soc_e = {}
    re = {}
    z_e = {}
    grid = {}
    pren = {}
    curt = {}
    dr = {}
    y_dr = {}
    shed = {}
    scenario_cost = {}
    xi = {}

    for s in scenarios:
        scenario_cost[s] = model.addVar(lb=0.0, name=f"cost[{s}]")
        xi[s] = model.addVar(lb=0.0, name=f"xi[{s}]")
        for h in hours:
            grid[s, h] = model.addVar(lb=0.0, name=f"grid[{s},{h}]")
            if h in data["dr_data"]:
                dr[s, h] = model.addVar(lb=0.0, name=f"dr[{s},{h}]")
                y_dr[s, h] = model.addVar(vtype="B", name=f"y_dr[{s},{h}]")
            else:
                dr[s, h] = None
                y_dr[s, h] = None
            shed[s, h] = model.addVar(lb=0.0, name=f"shed[{s},{h}]")
            for g in generators:
                pg[s, g, h] = model.addVar(lb=0.0, name=f"pg[{s},{g},{h}]")
                rg[s, g, h] = model.addVar(lb=0.0, name=f"rg[{s},{g},{h}]")
            for b in storages:
                ch_b[s, b, h] = model.addVar(lb=0.0, name=f"ch_b[{s},{b},{h}]")
                dis_b[s, b, h] = model.addVar(lb=0.0, name=f"dis_b[{s},{b},{h}]")
                soc_b[s, b, h] = model.addVar(lb=0.0, name=f"soc_b[{s},{b},{h}]")
                rb[s, b, h] = model.addVar(lb=0.0, name=f"rb[{s},{b},{h}]")
                z_b[s, b, h] = model.addVar(vtype="B", name=f"z_b[{s},{b},{h}]")
            for e in evs:
                ch_e[s, e, h] = model.addVar(lb=0.0, name=f"ch_e[{s},{e},{h}]")
                dis_e[s, e, h] = model.addVar(lb=0.0, name=f"dis_e[{s},{e},{h}]")
                soc_e[s, e, h] = model.addVar(lb=0.0, name=f"soc_e[{s},{e},{h}]")
                re[s, e, h] = model.addVar(lb=0.0, name=f"re[{s},{e},{h}]")
                z_e[s, e, h] = model.addVar(vtype="B", name=f"z_e[{s},{e},{h}]")
            for r in renewables:
                pren[s, r, h] = model.addVar(lb=0.0, name=f"pren[{s},{r},{h}]")
                curt[s, r, h] = model.addVar(lb=0.0, name=f"curt[{s},{r},{h}]")

    eta = model.addVar(lb=0.0, name="eta")

    for g in generators:
        params = data["generators_data"][g]
        init_u = int(cfg["initial_commitment"][g])
        for h in hours:
            prev_u = init_u if h == 0 else u[g, h - 1]
            model.addCons(
                u[g, h] - prev_u == su[g, h] - sd[g, h],
                name=f"commit_transition[{g},{h}]",
            )
        up = params["min_up_hours"]
        down = params["min_down_hours"]
        for h in hours:
            start_window = range(max(0, h - up + 1), h + 1)
            down_window = range(max(0, h - down + 1), h + 1)
            model.addCons(
                quicksum(su[g, tau] for tau in start_window) <= u[g, h],
                name=f"min_up[{g},{h}]",
            )
            model.addCons(
                quicksum(sd[g, tau] for tau in down_window) <= 1 - u[g, h],
                name=f"min_down[{g},{h}]",
            )

    for s in scenarios:
        for h in hours:
            sh = data["scenario_hourly"][s][h]
            load = data["time_data"][h]["base_load_kw"] * sh["load_multiplier"]
            reserve_req = sh["reserve_requirement_kw"]
            dr_rebound = quicksum(
                data["dr_data"][src]["rebound_fraction"] * dr[s, src]
                for src, _ in data["rebound_sources"].get(h, [])
            )

            gen_power = []
            gen_reserve = []
            storage_charge = []
            storage_discharge = []
            storage_reserve = []
            ev_charge = []
            ev_discharge = []
            ev_reserve = []
            renewable_power = []

            for g in generators:
                gp = data["generators_data"][g]
                model.addCons(
                    pg[s, g, h] >= gp["min_kw"] * u[g, h],
                    name=f"gen_min[{s},{g},{h}]",
                )
                model.addCons(
                    pg[s, g, h] <= gp["max_kw"] * u[g, h],
                    name=f"gen_max[{s},{g},{h}]",
                )
                model.addCons(
                    rg[s, g, h] <= gp["max_kw"] * u[g, h] - pg[s, g, h],
                    name=f"gen_headroom_reserve[{s},{g},{h}]",
                )
                model.addCons(
                    rg[s, g, h] <= gp["ramp_up_kw"],
                    name=f"gen_ramp_reserve[{s},{g},{h}]",
                )
                prev_output = gp["min_kw"] * int(cfg["initial_commitment"][g]) if h == 0 else pg[s, g, h - 1]
                model.addCons(
                    pg[s, g, h] - prev_output <= gp["ramp_up_kw"],
                    name=f"gen_ramp_up[{s},{g},{h}]",
                )
                model.addCons(
                    prev_output - pg[s, g, h] <= gp["ramp_down_kw"],
                    name=f"gen_ramp_down[{s},{g},{h}]",
                )
                gen_power.append(pg[s, g, h])
                gen_reserve.append(rg[s, g, h])

            for b in storages:
                bp = data["storage_data"][b]
                start_soc = bp["initial_soc_kwh"] if h == 0 else soc_b[s, b, h - 1]
                model.addCons(
                    ch_b[s, b, h] <= bp["max_charge_kw"] * z_b[s, b, h],
                    name=f"storage_charge_limit[{s},{b},{h}]",
                )
                model.addCons(
                    dis_b[s, b, h] <= bp["max_discharge_kw"] * (1 - z_b[s, b, h]),
                    name=f"storage_discharge_limit[{s},{b},{h}]",
                )
                model.addCons(
                    soc_b[s, b, h]
                    == start_soc
                    + bp["charge_efficiency"] * ch_b[s, b, h]
                    - dis_b[s, b, h] / bp["discharge_efficiency"],
                    name=f"storage_soc[{s},{b},{h}]",
                )
                model.addCons(
                    soc_b[s, b, h] >= bp["min_soc_kwh"],
                    name=f"storage_soc_min[{s},{b},{h}]",
                )
                model.addCons(
                    soc_b[s, b, h] <= bp["max_soc_kwh"],
                    name=f"storage_soc_max[{s},{b},{h}]",
                )
                model.addCons(
                    dis_b[s, b, h] + rb[s, b, h] <= bp["max_discharge_kw"] + ch_b[s, b, h],
                    name=f"storage_reserve_power[{s},{b},{h}]",
                )
                model.addCons(
                    dis_b[s, b, h] + rb[s, b, h]
                    <= bp["discharge_efficiency"] * (start_soc - bp["min_soc_kwh"]) + ch_b[s, b, h],
                    name=f"storage_reserve_energy[{s},{b},{h}]",
                )
                storage_charge.append(ch_b[s, b, h])
                storage_discharge.append(dis_b[s, b, h])
                storage_reserve.append(rb[s, b, h])
            for b in storages:
                if h == hours[-1]:
                    model.addCons(
                        soc_b[s, b, h] >= data["storage_data"][b]["initial_soc_kwh"],
                        name=f"storage_terminal_soc[{s},{b}]",
                    )

            for e in evs:
                ep = data["ev_data"][e]
                start_soc = ep["initial_soc_kwh"] if h == 0 else soc_e[s, e, h - 1]
                is_connected = connected(ep, h)
                avail = sh["ev_availability_multiplier"] if is_connected else 0.0
                max_discharge = ep["max_discharge_kw"] * avail * ep["v2g_allowed"]
                max_charge = ep["max_charge_kw"] * avail
                model.addCons(
                    ch_e[s, e, h] <= max_charge * z_e[s, e, h],
                    name=f"ev_charge_limit[{s},{e},{h}]",
                )
                model.addCons(
                    dis_e[s, e, h] <= max_discharge * (1 - z_e[s, e, h]),
                    name=f"ev_discharge_limit[{s},{e},{h}]",
                )
                model.addCons(
                    soc_e[s, e, h]
                    == start_soc
                    + ep["charge_efficiency"] * ch_e[s, e, h]
                    - dis_e[s, e, h] / ep["discharge_efficiency"],
                    name=f"ev_soc[{s},{e},{h}]",
                )
                model.addCons(
                    soc_e[s, e, h] >= ep["min_soc_kwh"],
                    name=f"ev_soc_min[{s},{e},{h}]",
                )
                model.addCons(
                    soc_e[s, e, h] <= ep["max_soc_kwh"],
                    name=f"ev_soc_max[{s},{e},{h}]",
                )
                if not is_connected:
                    model.addCons(ch_e[s, e, h] == 0.0, name=f"ev_no_charge[{s},{e},{h}]")
                    model.addCons(dis_e[s, e, h] == 0.0, name=f"ev_no_discharge[{s},{e},{h}]")
                    model.addCons(re[s, e, h] == 0.0, name=f"ev_no_reserve[{s},{e},{h}]")
                else:
                    model.addCons(
                        dis_e[s, e, h] + re[s, e, h] <= max_discharge + ch_e[s, e, h],
                        name=f"ev_reserve_power[{s},{e},{h}]",
                    )
                    model.addCons(
                        dis_e[s, e, h] + re[s, e, h]
                        <= ep["discharge_efficiency"] * (start_soc - ep["min_soc_kwh"]) + ch_e[s, e, h],
                        name=f"ev_reserve_energy[{s},{e},{h}]",
                    )
                if h == ep["departure_hour"] - 1:
                    model.addCons(
                        soc_e[s, e, h] >= ep["target_soc_kwh"],
                        name=f"ev_depart_target[{s},{e}]",
                    )
                ev_charge.append(ch_e[s, e, h])
                ev_discharge.append(dis_e[s, e, h])
                ev_reserve.append(re[s, e, h])

            for r in renewables:
                base_available = data["renewable_data"][h][r]
                mult = sh["pv_multiplier"] if r == "pv" else sh["wind_multiplier"]
                available = base_available * mult
                model.addCons(
                    pren[s, r, h] + curt[s, r, h] == available,
                    name=f"renewable_balance[{s},{r},{h}]",
                )
                renewable_power.append(pren[s, r, h])

            grid_cap = data["grid_caps"][h] * sh["grid_import_multiplier"]
            if sh["islanded"]:
                grid_cap = 0.0
            model.addCons(grid[s, h] <= grid_cap, name=f"grid_cap[{s},{h}]")

            if h in data["dr_data"]:
                dr_params = data["dr_data"][h]
                model.addCons(
                    dr[s, h] <= dr_params["max_curtail_kw"],
                    name=f"dr_cap[{s},{h}]",
                )
                model.addCons(
                    dr[s, h] <= dr_params["max_curtail_kw"] * y_dr[s, h],
                    name=f"dr_link[{s},{h}]",
                )

            model.addCons(
                shed[s, h] <= sh["max_critical_shed_kw"],
                name=f"shed_cap[{s},{h}]",
            )
            model.addCons(
                quicksum(gen_power)
                + grid[s, h]
                + quicksum(renewable_power)
                + quicksum(storage_discharge)
                + quicksum(ev_discharge)
                + (dr[s, h] if dr[s, h] is not None else 0.0)
                + shed[s, h]
                == load
                + dr_rebound
                + quicksum(storage_charge)
                + quicksum(ev_charge),
                name=f"power_balance[{s},{h}]",
            )
            model.addCons(
                quicksum(gen_reserve) + quicksum(storage_reserve) + quicksum(ev_reserve) >= reserve_req,
                name=f"reserve_requirement[{s},{h}]",
            )

            if sh["islanded"]:
                model.addCons(
                    quicksum(gen_reserve) >= cfg["island_min_synchronous_reserve_kw"],
                    name=f"island_sync_reserve[{s},{h}]",
                )
                model.addCons(
                    quicksum(
                        rb[s, b, h]
                        for b in storages
                        if data["storage_data"][b]["grid_forming"]
                    )
                    >= cfg["grid_forming_storage_reserve_fraction"] * reserve_req,
                    name=f"island_storage_reserve[{s},{h}]",
                )
                model.addCons(
                    quicksum(data["generators_data"][g]["inertia_score"] * u[g, h] for g in generators)
                    + quicksum(
                        data["storage_data"][b]["inertia_score_per_100kwh"] * soc_b[s, b, h] / 100.0
                        for b in storages
                        if data["storage_data"][b]["grid_forming"]
                    )
                    >= cfg["island_min_inertia_score"],
                    name=f"island_inertia[{s},{h}]",
                )

        dr_hours = sorted(data["dr_data"])
        model.addCons(
            quicksum(dr[s, h] for h in dr_hours) <= cfg["max_daily_dr_kwh"],
            name=f"dr_daily_limit[{s}]",
        )
        for start in range(hours[0], hours[-1] - cfg["max_consecutive_dr_hours"] + 1):
            window = [h for h in range(start, start + cfg["max_consecutive_dr_hours"] + 1) if h in data["dr_data"]]
            if len(window) == cfg["max_consecutive_dr_hours"] + 1:
                model.addCons(
                    quicksum(y_dr[s, h] for h in window) <= cfg["max_consecutive_dr_hours"],
                    name=f"dr_consecutive[{s},{start}]",
                )

        model.addCons(
            quicksum(
                data["generators_data"][g]["emission_kg_per_kwh"] * pg[s, g, h]
                for g in generators
                for h in hours
            )
            <= cfg["scenario_emissions_cap_kg"],
            name=f"emissions_cap[{s}]",
        )

        fixed_cost = quicksum(
            data["generators_data"][g]["startup_cost"] * su[g, h]
            + data["generators_data"][g]["no_load_cost_per_hour"] * u[g, h]
            for g in generators
            for h in hours
        )
        variable_cost = quicksum(
            data["generators_data"][g]["marginal_cost_per_kwh"] * pg[s, g, h]
            for g in generators
            for h in hours
        )
        variable_cost += quicksum(
            data["time_data"][h]["tariff_import_per_kwh"] * grid[s, h]
            for h in hours
        )
        variable_cost += quicksum(
            data["storage_data"][b]["cycle_cost_per_kwh"] * (ch_b[s, b, h] + dis_b[s, b, h])
            for b in storages
            for h in hours
        )
        variable_cost += quicksum(
            data["ev_data"][e]["cycle_cost_per_kwh"] * (ch_e[s, e, h] + dis_e[s, e, h])
            for e in evs
            for h in hours
        )
        variable_cost += quicksum(
            data["dr_data"][h]["discomfort_cost_per_kwh"] * dr[s, h]
            for h in data["dr_data"]
        )
        variable_cost += quicksum(
            cfg["renewable_curtailment_cost_per_kwh"] * curt[s, r, h]
            for r in renewables
            for h in hours
        )
        variable_cost += quicksum(
            cfg["load_shed_penalty_per_kwh"] * shed[s, h]
            for h in hours
        )
        model.addCons(
            scenario_cost[s] == fixed_cost + variable_cost,
            name=f"scenario_cost_def[{s}]",
        )
        model.addCons(xi[s] >= scenario_cost[s] - eta, name=f"cvar_excess[{s}]")

    expected_cost = quicksum(data["scenario_prob"][s] * scenario_cost[s] for s in scenarios)
    cvar_term = eta + (1.0 / (1.0 - cfg["tail_risk_alpha"])) * quicksum(
        data["scenario_prob"][s] * xi[s] for s in scenarios
    )
    model.setObjective(
        expected_cost + cfg["tail_risk_weight"] * cvar_term,
        "minimize",
    )

    return model, {
        "u": u,
        "su": su,
        "sd": sd,
        "pg": pg,
        "rg": rg,
        "ch_b": ch_b,
        "dis_b": dis_b,
        "soc_b": soc_b,
        "rb": rb,
        "z_b": z_b,
        "ch_e": ch_e,
        "dis_e": dis_e,
        "soc_e": soc_e,
        "re": re,
        "z_e": z_e,
        "grid": grid,
        "pren": pren,
        "curt": curt,
        "dr": dr,
        "y_dr": y_dr,
        "shed": shed,
        "scenario_cost": scenario_cost,
        "eta": eta,
        "xi": xi,
    }


def get_solution_value(model, solution, var):
    return safe_value(model.getSolVal(solution, var))


def extract_solution_rows(model, variables, data):
    solution = model.getBestSol()
    if solution is None:
        raise RuntimeError("SCIP finished without an incumbent solution.")

    rows = []
    hours = data["hours"]
    scenarios = data["scenarios"]

    for s in scenarios:
        for h in hours:
            for g in data["generators"]:
                rows.append(
                    {
                        "row_type": "generator",
                        "scenario_id": s,
                        "hour": str(h),
                        "asset_id": g,
                        "commit": str(int(round(get_solution_value(model, solution, variables["u"][g, h])))),
                        "power_kw": fmt(get_solution_value(model, solution, variables["pg"][s, g, h])),
                        "charge_kw": fmt(0.0),
                        "discharge_kw": fmt(0.0),
                        "soc_kwh": fmt(0.0),
                        "curtail_kw": fmt(0.0),
                        "shed_kw": fmt(0.0),
                        "reserve_kw": fmt(get_solution_value(model, solution, variables["rg"][s, g, h])),
                    }
                )
            for b in data["storages"]:
                rows.append(
                    {
                        "row_type": "storage",
                        "scenario_id": s,
                        "hour": str(h),
                        "asset_id": b,
                        "commit": "",
                        "power_kw": fmt(0.0),
                        "charge_kw": fmt(get_solution_value(model, solution, variables["ch_b"][s, b, h])),
                        "discharge_kw": fmt(get_solution_value(model, solution, variables["dis_b"][s, b, h])),
                        "soc_kwh": fmt(get_solution_value(model, solution, variables["soc_b"][s, b, h])),
                        "curtail_kw": fmt(0.0),
                        "shed_kw": fmt(0.0),
                        "reserve_kw": fmt(get_solution_value(model, solution, variables["rb"][s, b, h])),
                    }
                )
            for e in data["evs"]:
                rows.append(
                    {
                        "row_type": "ev_cluster",
                        "scenario_id": s,
                        "hour": str(h),
                        "asset_id": e,
                        "commit": "",
                        "power_kw": fmt(0.0),
                        "charge_kw": fmt(get_solution_value(model, solution, variables["ch_e"][s, e, h])),
                        "discharge_kw": fmt(get_solution_value(model, solution, variables["dis_e"][s, e, h])),
                        "soc_kwh": fmt(get_solution_value(model, solution, variables["soc_e"][s, e, h])),
                        "curtail_kw": fmt(0.0),
                        "shed_kw": fmt(0.0),
                        "reserve_kw": fmt(get_solution_value(model, solution, variables["re"][s, e, h])),
                    }
                )
            rows.append(
                {
                    "row_type": "grid",
                    "scenario_id": s,
                    "hour": str(h),
                    "asset_id": "grid",
                    "commit": "",
                    "power_kw": fmt(get_solution_value(model, solution, variables["grid"][s, h])),
                    "charge_kw": fmt(0.0),
                    "discharge_kw": fmt(0.0),
                    "soc_kwh": fmt(0.0),
                    "curtail_kw": fmt(0.0),
                    "shed_kw": fmt(0.0),
                    "reserve_kw": fmt(0.0),
                }
            )
            for r in data["renewables"]:
                rows.append(
                    {
                        "row_type": "renewable",
                        "scenario_id": s,
                        "hour": str(h),
                        "asset_id": r,
                        "commit": "",
                        "power_kw": fmt(get_solution_value(model, solution, variables["pren"][s, r, h])),
                        "charge_kw": fmt(0.0),
                        "discharge_kw": fmt(0.0),
                        "soc_kwh": fmt(0.0),
                        "curtail_kw": fmt(get_solution_value(model, solution, variables["curt"][s, r, h])),
                        "shed_kw": fmt(0.0),
                        "reserve_kw": fmt(0.0),
                    }
                )
            dr_value = get_solution_value(model, solution, variables["dr"][s, h]) if variables["dr"][s, h] is not None else 0.0
            rows.append(
                {
                    "row_type": "demand_response",
                    "scenario_id": s,
                    "hour": str(h),
                    "asset_id": "flex_load",
                    "commit": "",
                    "power_kw": fmt(0.0),
                    "charge_kw": fmt(0.0),
                    "discharge_kw": fmt(0.0),
                    "soc_kwh": fmt(0.0),
                    "curtail_kw": fmt(dr_value),
                    "shed_kw": fmt(0.0),
                    "reserve_kw": fmt(0.0),
                }
            )
            rows.append(
                {
                    "row_type": "load_shed",
                    "scenario_id": s,
                    "hour": str(h),
                    "asset_id": "critical_load",
                    "commit": "",
                    "power_kw": fmt(0.0),
                    "charge_kw": fmt(0.0),
                    "discharge_kw": fmt(0.0),
                    "soc_kwh": fmt(0.0),
                    "curtail_kw": fmt(0.0),
                    "shed_kw": fmt(get_solution_value(model, solution, variables["shed"][s, h])),
                    "reserve_kw": fmt(0.0),
                }
            )

    summary = {
        "objective": safe_value(model.getObjVal()),
        "best_bound": safe_value(model.getDualbound()) if model.getStage() >= 9 else None,
        "gap": safe_value(model.getGap()) if model.getNSols() > 0 else None,
        "status": str(model.getStatus()),
        "n_solutions": int(model.getNSols()),
        "scenario_costs": {
            s: get_solution_value(model, solution, variables["scenario_cost"][s])
            for s in data["scenarios"]
        },
        "eta": get_solution_value(model, solution, variables["eta"]),
        "xi": {s: get_solution_value(model, solution, variables["xi"][s]) for s in data["scenarios"]},
    }
    return rows, summary


def write_solution(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=REQUIRED_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(rows)


def expected_asset_keys(data):
    expected = []
    for s in data["scenarios"]:
        for h in data["hours"]:
            for g in data["generators"]:
                expected.append(("generator", s, h, g))
            for b in data["storages"]:
                expected.append(("storage", s, h, b))
            for e in data["evs"]:
                expected.append(("ev_cluster", s, h, e))
            expected.append(("grid", s, h, "grid"))
            expected.append(("renewable", s, h, "pv"))
            expected.append(("renewable", s, h, "wind"))
            expected.append(("demand_response", s, h, "flex_load"))
            expected.append(("load_shed", s, h, "critical_load"))
    return expected


def cvar_value(probabilities, scenario_costs, alpha):
    _, value = cvar_eta_value(probabilities, scenario_costs, alpha)
    return value


def cvar_eta_value(probabilities, scenario_costs, alpha):
    unique_costs = sorted(set(scenario_costs.values()))
    best = math.inf
    best_eta = unique_costs[0] if unique_costs else 0.0
    for eta in unique_costs:
        candidate = eta + sum(
            probabilities[s] * max(scenario_costs[s] - eta, 0.0) for s in scenario_costs
        ) / (1.0 - alpha)
        if candidate < best - 1e-9:
            best = candidate
            best_eta = eta
    return best_eta, best


def empty_row():
    return {
        "row_type": "",
        "scenario_id": "",
        "hour": "0",
        "asset_id": "",
        "commit": "",
        "power_kw": "0",
        "charge_kw": "0",
        "discharge_kw": "0",
        "soc_kwh": "0",
        "curtail_kw": "0",
        "shed_kw": "0",
        "reserve_kw": "0",
    }


def read_artifact_rows(path):
    path = Path(path)
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return reader.fieldnames or [], rows


def parse_prior_log_metrics(path):
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text()
    metrics = {}
    patterns = {
        "objective": r"- Objective: `([^`]+)`",
        "best_bound": r"- Best bound: `([^`]+)`",
        "gap": r"- Relative gap: `([^`]+)`",
        "status": r"- Status: `([^`]+)`",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            metrics[key] = match.group(1)
    return metrics


def load_solution_map(path):
    rows = read_csv(path)
    parsed = {}
    for row in rows:
        parsed[(row["row_type"], row["scenario_id"], int(row["hour"]), row["asset_id"])] = row
    return parsed


def audit_passes(validation):
    return (
        validation["schema_exact"]
        and validation["coverage_ok"]
        and validation["violation_group_count"] == 0
        and validation["integrality_residual"] <= 1e-8
    )


def validate_solution(data, path):
    path = Path(path)
    fieldnames, rows = read_artifact_rows(path)
    expected_keys = expected_asset_keys(data)
    expected_set = set(expected_keys)
    parsed = {}
    coverage_ok = True
    extra_keys = []
    duplicate_keys = []
    valid_row_types = {
        "generator",
        "storage",
        "ev_cluster",
        "grid",
        "renewable",
        "demand_response",
        "load_shed",
    }
    fieldname_exact = fieldnames == REQUIRED_COLUMNS

    violations = defaultdict(list)
    max_residual = 0.0

    def record(group, label, residual, scale=1.0):
        nonlocal max_residual
        norm = residual / max(scale, 1.0)
        max_residual = max(max_residual, norm)
        if residual > 1e-5:
            violations[group].append((label, residual))

    if not fieldname_exact:
        record("schema", "header_columns", 1.0, 1.0)

    scenario_set = set(data["scenarios"])
    hour_set = set(data["hours"])
    generator_set = set(data["generators"])
    storage_set = set(data["storages"])
    ev_set = set(data["evs"])
    renewable_set = set(data["renewables"])

    for row_idx, row in enumerate(rows, start=2):
        row_type = row.get("row_type", "")
        scenario_id = row.get("scenario_id", "")
        asset_id = row.get("asset_id", "")
        try:
            hour = int(row.get("hour", ""))
        except ValueError:
            hour = None
            record("schema", f"hour_parse_row_{row_idx}", 1.0, 1.0)
        if row_type not in valid_row_types:
            record("schema", f"row_type_row_{row_idx}", 1.0, 1.0)
        if scenario_id not in scenario_set:
            record("schema", f"scenario_row_{row_idx}", 1.0, 1.0)
        if hour not in hour_set:
            record("schema", f"hour_domain_row_{row_idx}", 1.0, 1.0)

        if row_type == "generator" and asset_id not in generator_set:
            record("schema", f"generator_asset_row_{row_idx}", 1.0, 1.0)
        if row_type == "storage" and asset_id not in storage_set:
            record("schema", f"storage_asset_row_{row_idx}", 1.0, 1.0)
        if row_type == "ev_cluster" and asset_id not in ev_set:
            record("schema", f"ev_asset_row_{row_idx}", 1.0, 1.0)
        if row_type == "renewable" and asset_id not in renewable_set:
            record("schema", f"renewable_asset_row_{row_idx}", 1.0, 1.0)
        if row_type == "grid" and asset_id != "grid":
            record("schema", f"grid_asset_row_{row_idx}", 1.0, 1.0)
        if row_type == "demand_response" and asset_id != "flex_load":
            record("schema", f"dr_asset_row_{row_idx}", 1.0, 1.0)
        if row_type == "load_shed" and asset_id != "critical_load":
            record("schema", f"shed_asset_row_{row_idx}", 1.0, 1.0)

        commit_raw = row.get("commit", "")
        if row_type == "generator":
            if commit_raw == "":
                record("schema", f"generator_commit_blank_row_{row_idx}", 1.0, 1.0)
            else:
                try:
                    commit_val = float(commit_raw)
                    if abs(commit_val - round(commit_val)) > 1e-8 or round(commit_val) not in (0, 1):
                        record("schema", f"generator_commit_domain_row_{row_idx}", 1.0, 1.0)
                except ValueError:
                    record("schema", f"generator_commit_parse_row_{row_idx}", 1.0, 1.0)
        elif commit_raw not in ("", "0", "0.0", "0.00000000"):
            try:
                commit_val = float(commit_raw)
                if abs(commit_val) > 1e-8:
                    record("schema", f"nongenerator_commit_row_{row_idx}", 1.0, 1.0)
            except ValueError:
                record("schema", f"nongenerator_commit_parse_row_{row_idx}", 1.0, 1.0)

        for col in NUMERIC_COLUMNS:
            raw = row.get(col, "")
            if raw == "":
                record("schema", f"blank_numeric_{col}_row_{row_idx}", 1.0, 1.0)
                continue
            try:
                val = float(raw)
            except ValueError:
                record("schema", f"numeric_parse_{col}_row_{row_idx}", 1.0, 1.0)
                continue
            if val < -1e-8:
                record("nonnegativity", f"{col}_row_{row_idx}", -val, max(abs(val), 1.0))

        if hour is None:
            continue
        key = (row_type, scenario_id, hour, asset_id)
        if key in parsed:
            duplicate_keys.append(key)
        parsed[key] = row
    missing_keys = [k for k in expected_keys if k not in parsed]
    extra_keys = [k for k in parsed if k not in expected_set]
    if missing_keys or extra_keys or duplicate_keys:
        coverage_ok = False
        if missing_keys:
            record("coverage", "missing_keys", float(len(missing_keys)), max(len(expected_keys), 1))
        if extra_keys:
            record("coverage", "extra_keys", float(len(extra_keys)), max(len(expected_keys), 1))
        if duplicate_keys:
            record("coverage", "duplicate_keys", float(len(duplicate_keys)), max(len(expected_keys), 1))

    def num(row, col):
        return as_float(row[col])

    def get_row(key):
        return parsed.get(key, empty_row())

    commitments = {}
    integrality_residual = 0.0
    for g in data["generators"]:
        for h in data["hours"]:
            vals = []
            for s in data["scenarios"]:
                row = get_row(("generator", s, h, g))
                if row["commit"] == "":
                    vals.append(0.0)
                else:
                    val = float(row["commit"])
                    vals.append(val)
                    integrality_residual = max(integrality_residual, abs(val - round(val)))
            commitments[g, h] = round(vals[0])
            for idx, val in enumerate(vals[1:], start=1):
                record("commitment", f"{g},{h},scenario={data['scenarios'][idx]}", abs(val - vals[0]), 1.0)
            record("commitment", f"{g},{h},integrality", abs(vals[0] - round(vals[0])), 1.0)

    for s in data["scenarios"]:
        dr_rebound = defaultdict(float)
        for src_h, params in data["dr_data"].items():
            row = get_row(("demand_response", s, src_h, "flex_load"))
            dr_val = num(row, "curtail_kw")
            dr_rebound[params["rebound_hour"]] += params["rebound_fraction"] * dr_val

        for h in data["hours"]:
            sh = data["scenario_hourly"][s][h]
            time_info = data["time_data"][h]

            gen_power_sum = 0.0
            gen_reserve_sum = 0.0
            storage_charge_sum = 0.0
            storage_discharge_sum = 0.0
            storage_reserve_sum = 0.0
            ev_charge_sum = 0.0
            ev_discharge_sum = 0.0
            ev_reserve_sum = 0.0
            renewable_power_sum = 0.0
            sync_reserve_sum = 0.0

            for g in data["generators"]:
                gp = data["generators_data"][g]
                row = get_row(("generator", s, h, g))
                commit = commitments[g, h]
                power = num(row, "power_kw")
                reserve = num(row, "reserve_kw")
                gen_power_sum += power
                gen_reserve_sum += reserve
                sync_reserve_sum += reserve
                record("nonnegativity", f"gen power {s},{g},{h}", max(0.0, -power), gp["max_kw"])
                record("nonnegativity", f"gen reserve {s},{g},{h}", max(0.0, -reserve), gp["ramp_up_kw"])
                record("gen_bounds", f"gen max {s},{g},{h}", max(0.0, power - gp["max_kw"] * commit), gp["max_kw"])
                lower_violation = max(0.0, gp["min_kw"] * commit - power)
                record("gen_bounds", f"gen min {s},{g},{h}", lower_violation, gp["min_kw"])
                record("gen_reserve", f"headroom {s},{g},{h}", max(0.0, reserve - (gp["max_kw"] * commit - power)), gp["max_kw"])
                record("gen_reserve", f"ramp {s},{g},{h}", max(0.0, reserve - gp["ramp_up_kw"]), gp["ramp_up_kw"])
                prev_power = gp["min_kw"] * data["config"]["initial_commitment"][g] if h == 0 else num(get_row(("generator", s, h - 1, g)), "power_kw")
                record("gen_ramp", f"up {s},{g},{h}", max(0.0, power - prev_power - gp["ramp_up_kw"]), gp["ramp_up_kw"])
                record("gen_ramp", f"down {s},{g},{h}", max(0.0, prev_power - power - gp["ramp_down_kw"]), gp["ramp_down_kw"])

            for b in data["storages"]:
                bp = data["storage_data"][b]
                row = get_row(("storage", s, h, b))
                charge = num(row, "charge_kw")
                discharge = num(row, "discharge_kw")
                reserve = num(row, "reserve_kw")
                soc = num(row, "soc_kwh")
                storage_charge_sum += charge
                storage_discharge_sum += discharge
                storage_reserve_sum += reserve
                start_soc = bp["initial_soc_kwh"] if h == 0 else num(get_row(("storage", s, h - 1, b)), "soc_kwh")
                expected_soc = start_soc + bp["charge_efficiency"] * charge - discharge / bp["discharge_efficiency"]
                record("storage_soc", f"soc rec {s},{b},{h}", abs(soc - expected_soc), bp["max_soc_kwh"])
                record("storage_soc", f"soc min {s},{b},{h}", max(0.0, bp["min_soc_kwh"] - soc), bp["min_soc_kwh"])
                record("storage_soc", f"soc max {s},{b},{h}", max(0.0, soc - bp["max_soc_kwh"]), bp["max_soc_kwh"])
                record("storage_mode", f"charge {s},{b},{h}", max(0.0, charge - bp["max_charge_kw"]), bp["max_charge_kw"])
                record("storage_mode", f"discharge {s},{b},{h}", max(0.0, discharge - bp["max_discharge_kw"]), bp["max_discharge_kw"])
                record("storage_mode", f"simultaneous {s},{b},{h}", min(charge, discharge), max(bp["max_charge_kw"], bp["max_discharge_kw"]))
                record("storage_reserve", f"power {s},{b},{h}", max(0.0, discharge + reserve - bp["max_discharge_kw"] - charge), bp["max_discharge_kw"] + bp["max_charge_kw"])
                record("storage_reserve", f"energy {s},{b},{h}", max(0.0, discharge + reserve - bp["discharge_efficiency"] * (start_soc - bp["min_soc_kwh"]) - charge), bp["max_discharge_kw"] + bp["max_charge_kw"])
                if h == data["hours"][-1]:
                    record("storage_terminal", f"{s},{b}", max(0.0, bp["initial_soc_kwh"] - soc), bp["initial_soc_kwh"])

            gf_storage_reserve = 0.0
            inertia_score = 0.0
            for b in data["storages"]:
                bp = data["storage_data"][b]
                row = get_row(("storage", s, h, b))
                if bp["grid_forming"]:
                    gf_storage_reserve += num(row, "reserve_kw")
                    inertia_score += bp["inertia_score_per_100kwh"] * num(row, "soc_kwh") / 100.0

            for e in data["evs"]:
                ep = data["ev_data"][e]
                row = get_row(("ev_cluster", s, h, e))
                charge = num(row, "charge_kw")
                discharge = num(row, "discharge_kw")
                reserve = num(row, "reserve_kw")
                soc = num(row, "soc_kwh")
                ev_charge_sum += charge
                ev_discharge_sum += discharge
                ev_reserve_sum += reserve
                start_soc = ep["initial_soc_kwh"] if h == 0 else num(get_row(("ev_cluster", s, h - 1, e)), "soc_kwh")
                expected_soc = start_soc + ep["charge_efficiency"] * charge - discharge / ep["discharge_efficiency"]
                record("ev_soc", f"soc rec {s},{e},{h}", abs(soc - expected_soc), ep["max_soc_kwh"])
                record("ev_soc", f"soc min {s},{e},{h}", max(0.0, ep["min_soc_kwh"] - soc), ep["min_soc_kwh"])
                record("ev_soc", f"soc max {s},{e},{h}", max(0.0, soc - ep["max_soc_kwh"]), ep["max_soc_kwh"])
                is_connected = connected(ep, h)
                avail = sh["ev_availability_multiplier"] if is_connected else 0.0
                max_charge = ep["max_charge_kw"] * avail
                max_discharge = ep["max_discharge_kw"] * avail * ep["v2g_allowed"]
                record("ev_mode", f"charge {s},{e},{h}", max(0.0, charge - max_charge), max(ep["max_charge_kw"], 1.0))
                record("ev_mode", f"discharge {s},{e},{h}", max(0.0, discharge - max_discharge), max(ep["max_discharge_kw"], 1.0))
                record("ev_mode", f"simultaneous {s},{e},{h}", min(charge, discharge), max(ep["max_charge_kw"], ep["max_discharge_kw"]))
                if not is_connected:
                    record("ev_connection", f"charge {s},{e},{h}", charge, 1.0)
                    record("ev_connection", f"discharge {s},{e},{h}", discharge, 1.0)
                    record("ev_connection", f"reserve {s},{e},{h}", reserve, 1.0)
                record("ev_reserve", f"power {s},{e},{h}", max(0.0, discharge + reserve - max_discharge - charge), max(ep["max_discharge_kw"] + ep["max_charge_kw"], 1.0))
                record("ev_reserve", f"energy {s},{e},{h}", max(0.0, discharge + reserve - ep["discharge_efficiency"] * (start_soc - ep["min_soc_kwh"]) - charge), max(ep["max_discharge_kw"] + ep["max_charge_kw"], 1.0))
                if h == ep["departure_hour"] - 1:
                    record("ev_departure", f"{s},{e}", max(0.0, ep["target_soc_kwh"] - soc), ep["target_soc_kwh"])

            for r in data["renewables"]:
                row = get_row(("renewable", s, h, r))
                power = num(row, "power_kw")
                curtail = num(row, "curtail_kw")
                available = data["renewable_data"][h][r] * (sh["pv_multiplier"] if r == "pv" else sh["wind_multiplier"])
                renewable_power_sum += power
                record("renewable", f"balance {s},{r},{h}", abs(power + curtail - available), max(available, 1.0))
                record("renewable", f"nonneg {s},{r},{h}", max(0.0, -power) + max(0.0, -curtail), max(available, 1.0))

            grid_row = get_row(("grid", s, h, "grid"))
            grid_power = num(grid_row, "power_kw")
            grid_cap = data["grid_caps"][h] * sh["grid_import_multiplier"]
            if sh["islanded"]:
                grid_cap = 0.0
            record("grid", f"cap {s},{h}", max(0.0, grid_power - grid_cap), max(data["grid_caps"][h], 1.0))

            dr_row = get_row(("demand_response", s, h, "flex_load"))
            dr_val = num(dr_row, "curtail_kw")
            if h in data["dr_data"]:
                record("dr", f"cap {s},{h}", max(0.0, dr_val - data["dr_data"][h]["max_curtail_kw"]), max(data["dr_data"][h]["max_curtail_kw"], 1.0))
            else:
                record("dr", f"outside {s},{h}", dr_val, 1.0)

            shed_row = get_row(("load_shed", s, h, "critical_load"))
            shed_val = num(shed_row, "shed_kw")
            record("shed", f"cap {s},{h}", max(0.0, shed_val - sh["max_critical_shed_kw"]), max(sh["max_critical_shed_kw"], 1.0))

            stressed_load = time_info["base_load_kw"] * sh["load_multiplier"]
            balance_left = gen_power_sum + grid_power + renewable_power_sum + storage_discharge_sum + ev_discharge_sum + dr_val + shed_val
            balance_right = stressed_load + dr_rebound[h] + storage_charge_sum + ev_charge_sum
            record("power_balance", f"{s},{h}", abs(balance_left - balance_right), max(balance_right, 1.0))

            total_reserve = gen_reserve_sum + storage_reserve_sum + ev_reserve_sum
            record("reserve", f"{s},{h}", max(0.0, sh["reserve_requirement_kw"] - total_reserve), max(sh["reserve_requirement_kw"], 1.0))

            if sh["islanded"]:
                for g in data["generators"]:
                    inertia_score += data["generators_data"][g]["inertia_score"] * commitments[g, h]
                record("islanding", f"sync reserve {s},{h}", max(0.0, data["config"]["island_min_synchronous_reserve_kw"] - sync_reserve_sum), data["config"]["island_min_synchronous_reserve_kw"])
                record("islanding", f"storage reserve {s},{h}", max(0.0, data["config"]["grid_forming_storage_reserve_fraction"] * sh["reserve_requirement_kw"] - gf_storage_reserve), sh["reserve_requirement_kw"])
                record("islanding", f"inertia {s},{h}", max(0.0, data["config"]["island_min_inertia_score"] - inertia_score), data["config"]["island_min_inertia_score"])

        dr_total = sum(num(get_row(("demand_response", s, h, "flex_load")), "curtail_kw") for h in data["dr_data"])
        record("dr_daily", f"{s}", max(0.0, dr_total - data["config"]["max_daily_dr_kwh"]), data["config"]["max_daily_dr_kwh"])
        dr_hours = sorted(data["dr_data"])
        for start in range(data["hours"][0], data["hours"][-1] - data["config"]["max_consecutive_dr_hours"] + 1):
            window = [h for h in range(start, start + data["config"]["max_consecutive_dr_hours"] + 1) if h in data["dr_data"]]
            if len(window) == data["config"]["max_consecutive_dr_hours"] + 1:
                active = sum(1 for h in window if num(get_row(("demand_response", s, h, "flex_load")), "curtail_kw") > 1e-7)
                record("dr_consecutive", f"{s},{start}", max(0.0, active - data["config"]["max_consecutive_dr_hours"]), data["config"]["max_consecutive_dr_hours"])

        emissions = sum(
            data["generators_data"][g]["emission_kg_per_kwh"] * num(get_row(("generator", s, h, g)), "power_kw")
            for g in data["generators"]
            for h in data["hours"]
        )
        record("emissions", f"{s}", max(0.0, emissions - data["config"]["scenario_emissions_cap_kg"]), data["config"]["scenario_emissions_cap_kg"])

    startup_cost = 0.0
    no_load_cost = 0.0
    for g in data["generators"]:
        prev = data["config"]["initial_commitment"][g]
        for h in data["hours"]:
            now = commitments[g, h]
            if now - prev > 0.5:
                startup_cost += data["generators_data"][g]["startup_cost"]
            no_load_cost += data["generators_data"][g]["no_load_cost_per_hour"] * now
            prev = now

    for g in data["generators"]:
        up = data["generators_data"][g]["min_up_hours"]
        down = data["generators_data"][g]["min_down_hours"]
        prev = data["config"]["initial_commitment"][g]
        starts = []
        stops = []
        for h in data["hours"]:
            now = commitments[g, h]
            if now > prev:
                starts.append(h)
            if now < prev:
                stops.append(h)
            prev = now
        for h in starts:
            end = min(h + up - 1, data["hours"][-1])
            on_hours = sum(commitments[g, t] for t in range(h, end + 1))
            record("min_up_down", f"up {g},{h}", max(0.0, (end - h + 1) - on_hours), up)
        for h in stops:
            end = min(h + down - 1, data["hours"][-1])
            off_hours = sum(1 - commitments[g, t] for t in range(h, end + 1))
            record("min_up_down", f"down {g},{h}", max(0.0, (end - h + 1) - off_hours), down)

    scenario_costs = {}
    for s in data["scenarios"]:
        total = startup_cost + no_load_cost
        total += sum(
            data["generators_data"][g]["marginal_cost_per_kwh"] * num(get_row(("generator", s, h, g)), "power_kw")
            for g in data["generators"]
            for h in data["hours"]
        )
        total += sum(
            data["time_data"][h]["tariff_import_per_kwh"] * num(get_row(("grid", s, h, "grid")), "power_kw")
            for h in data["hours"]
        )
        total += sum(
            data["storage_data"][b]["cycle_cost_per_kwh"]
            * (
                num(get_row(("storage", s, h, b)), "charge_kw")
                + num(get_row(("storage", s, h, b)), "discharge_kw")
            )
            for b in data["storages"]
            for h in data["hours"]
        )
        total += sum(
            data["ev_data"][e]["cycle_cost_per_kwh"]
            * (
                num(get_row(("ev_cluster", s, h, e)), "charge_kw")
                + num(get_row(("ev_cluster", s, h, e)), "discharge_kw")
            )
            for e in data["evs"]
            for h in data["hours"]
        )
        total += sum(
            data["dr_data"][h]["discomfort_cost_per_kwh"]
            * num(get_row(("demand_response", s, h, "flex_load")), "curtail_kw")
            for h in data["dr_data"]
        )
        total += sum(
            data["config"]["renewable_curtailment_cost_per_kwh"]
            * num(get_row(("renewable", s, h, r)), "curtail_kw")
            for r in data["renewables"]
            for h in data["hours"]
        )
        total += sum(
            data["config"]["load_shed_penalty_per_kwh"]
            * num(get_row(("load_shed", s, h, "critical_load")), "shed_kw")
            for h in data["hours"]
        )
        scenario_costs[s] = total

    expected_cost = sum(data["scenario_prob"][s] * scenario_costs[s] for s in data["scenarios"])
    cvar_eta, cvar = cvar_eta_value(data["scenario_prob"], scenario_costs, data["config"]["tail_risk_alpha"])
    total_objective = expected_cost + data["config"]["tail_risk_weight"] * cvar

    return {
        "schema_exact": fieldname_exact,
        "row_count": len(rows),
        "expected_row_count": len(expected_keys),
        "coverage_ok": coverage_ok,
        "missing_keys": missing_keys[:10],
        "extra_keys": extra_keys[:10],
        "duplicate_keys": duplicate_keys[:10],
        "max_normalized_residual": max_residual,
        "violated_groups": {k: v[:5] for k, v in violations.items() if v},
        "violation_group_count": sum(1 for v in violations.values() if v),
        "integrality_residual": integrality_residual,
        "scenario_costs": scenario_costs,
        "expected_cost": expected_cost,
        "cvar_eta": cvar_eta,
        "cvar": cvar,
        "objective": total_objective,
        "checked_groups": [
            "schema/header",
            "row coverage and duplicates",
            "identifier domains and numeric parsing",
            "generator commitment consistency",
            "generator bounds/ramping/reserve",
            "minimum up/down",
            "storage soc/mode/reserve/terminal",
            "ev connection/soc/reserve/departure target",
            "renewable availability and curtailment",
            "grid limits and islanding",
            "demand response cap/rebound/daily/consecutive",
            "load shedding cap",
            "power balance",
            "reserve and island stability floors",
            "emissions cap",
            "objective recomputation",
        ],
        "coverage_complete": True,
    }


def add_incumbent_mip_start(model, variables, data, artifact_path, incumbent_validation):
    parsed = load_solution_map(artifact_path)
    sol = model.createSol()
    first_scenario = data["scenarios"][0]
    set_count = 0

    for g in data["generators"]:
        prev = int(data["config"]["initial_commitment"][g])
        for h in data["hours"]:
            commit = int(round(float(parsed[("generator", first_scenario, h, g)]["commit"])))
            model.setSolVal(sol, variables["u"][g, h], commit)
            model.setSolVal(sol, variables["su"][g, h], 1 if commit > prev else 0)
            model.setSolVal(sol, variables["sd"][g, h], 1 if commit < prev else 0)
            prev = commit
            set_count += 3

    for s in data["scenarios"]:
        for h in data["hours"]:
            model.setSolVal(sol, variables["grid"][s, h], as_float(parsed[("grid", s, h, "grid")]["power_kw"]))
            model.setSolVal(sol, variables["shed"][s, h], as_float(parsed[("load_shed", s, h, "critical_load")]["shed_kw"]))
            set_count += 2
            if variables["dr"][s, h] is not None:
                dr_val = as_float(parsed[("demand_response", s, h, "flex_load")]["curtail_kw"])
                model.setSolVal(sol, variables["dr"][s, h], dr_val)
                model.setSolVal(sol, variables["y_dr"][s, h], 1 if dr_val > 1e-7 else 0)
                set_count += 2
            for g in data["generators"]:
                row = parsed[("generator", s, h, g)]
                model.setSolVal(sol, variables["pg"][s, g, h], as_float(row["power_kw"]))
                model.setSolVal(sol, variables["rg"][s, g, h], as_float(row["reserve_kw"]))
                set_count += 2
            for b in data["storages"]:
                row = parsed[("storage", s, h, b)]
                charge = as_float(row["charge_kw"])
                discharge = as_float(row["discharge_kw"])
                model.setSolVal(sol, variables["ch_b"][s, b, h], charge)
                model.setSolVal(sol, variables["dis_b"][s, b, h], discharge)
                model.setSolVal(sol, variables["soc_b"][s, b, h], as_float(row["soc_kwh"]))
                model.setSolVal(sol, variables["rb"][s, b, h], as_float(row["reserve_kw"]))
                model.setSolVal(sol, variables["z_b"][s, b, h], 1 if charge > 1e-7 else 0)
                set_count += 5
            for e in data["evs"]:
                row = parsed[("ev_cluster", s, h, e)]
                charge = as_float(row["charge_kw"])
                discharge = as_float(row["discharge_kw"])
                model.setSolVal(sol, variables["ch_e"][s, e, h], charge)
                model.setSolVal(sol, variables["dis_e"][s, e, h], discharge)
                model.setSolVal(sol, variables["soc_e"][s, e, h], as_float(row["soc_kwh"]))
                model.setSolVal(sol, variables["re"][s, e, h], as_float(row["reserve_kw"]))
                model.setSolVal(sol, variables["z_e"][s, e, h], 1 if charge > 1e-7 else 0)
                set_count += 5
            for r in data["renewables"]:
                row = parsed[("renewable", s, h, r)]
                model.setSolVal(sol, variables["pren"][s, r, h], as_float(row["power_kw"]))
                model.setSolVal(sol, variables["curt"][s, r, h], as_float(row["curtail_kw"]))
                set_count += 2

        model.setSolVal(sol, variables["scenario_cost"][s], incumbent_validation["scenario_costs"][s])
        set_count += 1

    eta = incumbent_validation["cvar_eta"]
    model.setSolVal(sol, variables["eta"], eta)
    set_count += 1
    for s in data["scenarios"]:
        model.setSolVal(sol, variables["xi"][s], max(incumbent_validation["scenario_costs"][s] - eta, 0.0))
        set_count += 1

    accepted = model.addSol(sol, free=True)
    return {
        "accepted": bool(accepted),
        "values_set": set_count,
    }


def run_corruption_audit(data, solution_path):
    rows = read_csv(solution_path)
    tmp_path = solution_path.with_name("solution_corrupt_check.csv")
    mutated = False
    for row in rows:
        if row["row_type"] == "grid" and row["scenario_id"] == "S6" and row["hour"] == "17":
            row["power_kw"] = "1.00000000"
            mutated = True
            break
    if not mutated:
        return None
    with tmp_path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=REQUIRED_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(rows)
    audit = validate_solution(data, tmp_path)
    tmp_path.unlink(missing_ok=True)
    return {
        "violation_group_count": audit["violation_group_count"],
        "theta": audit["max_normalized_residual"],
        "groups": sorted(audit["violated_groups"].keys()),
    }


def write_log(
    log_path,
    time_limit,
    data,
    prior_metrics,
    incumbent_validation,
    candidate_summary,
    candidate_validation,
    decision,
    final_validation,
    corruption_audit,
    mip_start_info,
):
    lines = [
        "# Solve Log",
        "",
        "## Repair Notes",
        "",
        "- Hardened the independent disk audit so it accepts string or `Path` inputs, checks exact CSV headers, validates row identifiers and numeric parsing, and handles missing keys without crashing.",
        "- Treated the existing `solution.csv` as the rollback incumbent and only allowed publication of a candidate with zero hard-check violations and strictly lower independently recomputed objective.",
        "- Used a bounded exact SCIP solve with the incumbent loaded as a MIP start.",
        "",
        "## Commands",
        "",
        f"- `PYTHONPATH=/opt/or-deps python {SUBMISSIONS_DIR / 'solve.py'}`",
        "",
        "## Rollback Incumbent",
        "",
        f"- Audit objective from disk: `{incumbent_validation['objective']:.8f}`",
        f"- Audit theta: `{incumbent_validation['max_normalized_residual']:.8e}`",
        f"- Audit passed: `{audit_passes(incumbent_validation)}`",
    ]
    if prior_metrics.get("status") is not None:
        lines.append(f"- Prior solve status: `{prior_metrics['status']}`")
    if prior_metrics.get("best_bound") is not None:
        lines.append(f"- Prior best bound: `{prior_metrics['best_bound']}`")
    if prior_metrics.get("gap") is not None:
        lines.append(f"- Prior relative gap: `{prior_metrics['gap']}`")
    lines.extend(
        [
            "",
            "## Candidate Solve",
            "",
            f"- Time limit: `{time_limit}` seconds",
            f"- MIP start accepted: `{mip_start_info['accepted'] if mip_start_info is not None else False}`",
            f"- MIP start values set: `{mip_start_info['values_set'] if mip_start_info is not None else 0}`",
            f"- Status: `{candidate_summary['status']}`",
            f"- Incumbent count: `{candidate_summary['n_solutions']}`",
        ]
    )
    if candidate_summary.get("objective") is not None:
        lines.append(f"- Solver objective: `{candidate_summary['objective']:.8f}`")
    if candidate_summary.get("best_bound") is not None:
        lines.append(f"- Best bound: `{candidate_summary['best_bound']:.8f}`")
    if candidate_summary.get("gap") is not None:
        lines.append(f"- Relative gap: `{candidate_summary['gap']:.8f}`")
    if candidate_validation is not None:
        lines.extend(
            [
                f"- Candidate audited objective: `{candidate_validation['objective']:.8f}`",
                f"- Candidate audit theta: `{candidate_validation['max_normalized_residual']:.8e}`",
                f"- Candidate audit passed: `{audit_passes(candidate_validation)}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Final Artifact",
            "",
        ]
    )
    lines.extend(
        [
            f"- Published artifact: `{decision}`",
            f"- Final objective from exported artifact: `{final_validation['objective']:.8f}`",
            f"- Final theta: `{final_validation['max_normalized_residual']:.8e}`",
            f"- Final hard-check pass: `{audit_passes(final_validation)}`",
            "",
            "## Scenario Costs",
            "",
        ]
    )
    for s in data["scenarios"]:
        lines.append(f"- `{s}`: `{final_validation['scenario_costs'][s]:.8f}`")

    lines.extend(
        [
            "",
            "## Validation",
            "",
            f"- Header exact: `{final_validation['schema_exact']}`",
            f"- Row coverage: `{final_validation['row_count']}/{final_validation['expected_row_count']}` rows",
            f"- Coverage exact: `{final_validation['coverage_ok']}`",
            f"- Max normalized hard-constraint residual theta: `{final_validation['max_normalized_residual']:.8e}`",
            f"- Violated constraint groups: `{final_validation['violation_group_count']}`",
            f"- Integrality residual: `{final_validation['integrality_residual']:.8e}`",
            f"- Expected cost from exported artifact: `{final_validation['expected_cost']:.8f}`",
            f"- CVaR from exported artifact: `{final_validation['cvar']:.8f}`",
            f"- Total objective from exported artifact: `{final_validation['objective']:.8f}`",
            f"- Audit coverage complete: `{final_validation['coverage_complete']}`",
            f"- Checked groups: `{final_validation['checked_groups']}`",
        ]
    )
    if final_validation["missing_keys"]:
        lines.append(f"- Missing keys sample: `{final_validation['missing_keys']}`")
    if final_validation["extra_keys"]:
        lines.append(f"- Extra keys sample: `{final_validation['extra_keys']}`")
    if final_validation["duplicate_keys"]:
        lines.append(f"- Duplicate keys sample: `{final_validation['duplicate_keys']}`")
    if corruption_audit is not None:
        lines.append(
            "- Corrupted-copy audit: forcing `grid` import to `1.0` in islanded scenario `S6`, "
            f"hour `17`, triggered `{corruption_audit['violation_group_count']}` violated groups "
            f"{corruption_audit['groups']} with `theta = {corruption_audit['theta']:.8e}`."
        )
    if final_validation["violation_group_count"] == 0:
        lines.append("- Independent audit: all checked public constraints passed.")
    else:
        lines.append("- Independent audit found violations.")
        for group, items in final_validation["violated_groups"].items():
            lines.append(f"- {group}: `{items}`")

    log_path.write_text("\n".join(lines) + "\n")


def main():
    data = load_data()
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is not None:
        time_limit = min(float(env_limit), 295.0)
    else:
        time_limit = 295.0
    prior_metrics = parse_prior_log_metrics(LOG_PATH)

    if not SOLUTION_PATH.exists():
        raise RuntimeError("Rollback incumbent missing: /app/submissions/solution.csv does not exist.")
    incumbent_validation = validate_solution(data, SOLUTION_PATH)
    incumbent_ok = audit_passes(incumbent_validation)

    model, variables = build_model(data, time_limit)
    mip_start_info = None
    if incumbent_ok:
        mip_start_info = add_incumbent_mip_start(model, variables, data, SOLUTION_PATH, incumbent_validation)

    model.optimize()

    candidate_summary = {
        "status": str(model.getStatus()),
        "n_solutions": int(model.getNSols()),
        "objective": safe_value(model.getObjVal()) if model.getNSols() > 0 else None,
        "best_bound": safe_value(model.getDualbound()) if model.getStage() >= 9 else None,
        "gap": safe_value(model.getGap()) if model.getNSols() > 0 else None,
    }
    candidate_validation = None
    decision = "rollback_incumbent_kept"
    final_validation = incumbent_validation

    candidate_path = SUBMISSIONS_DIR / "solution_candidate.csv"
    if model.getNSols() > 0:
        rows, extracted_summary = extract_solution_rows(model, variables, data)
        candidate_summary.update(extracted_summary)
        write_solution(rows, candidate_path)
        candidate_validation = validate_solution(data, candidate_path)
        candidate_ok = audit_passes(candidate_validation)
        strictly_better = candidate_ok and candidate_validation["objective"] < incumbent_validation["objective"] - 1e-6
        if (not incumbent_ok and candidate_ok) or strictly_better:
            candidate_path.replace(SOLUTION_PATH)
            decision = "candidate_accepted"
            final_validation = validate_solution(data, SOLUTION_PATH)
        else:
            candidate_path.unlink(missing_ok=True)
    elif not incumbent_ok:
        LOG_PATH.write_text(
            "# Solve Log\n\n"
            "## Failure\n\n"
            "- The rollback incumbent failed the independent audit and SCIP did not produce a replacement incumbent.\n"
        )
        raise RuntimeError("No valid incumbent available after guarded repair/performance phase.")

    corruption_audit = run_corruption_audit(data, SOLUTION_PATH)
    write_log(
        LOG_PATH,
        time_limit,
        data,
        prior_metrics,
        incumbent_validation,
        candidate_summary,
        candidate_validation,
        decision,
        final_validation,
        corruption_audit,
        mip_start_info,
    )


if __name__ == "__main__":
    main()
