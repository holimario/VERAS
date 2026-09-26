#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import shlex
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

if "/opt/or-deps" not in sys.path:
    sys.path.insert(0, "/opt/or-deps")

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
CANDIDATE_PATH = SUBMISSIONS_DIR / ".candidate_solution.csv"

SCIP_GAP = 0.0005
MAX_SINGLE_RUN_SECONDS = 300.0
EXPORT_EPS = 1e-9
CHECK_TOL = 1e-7
IMPROVEMENT_TOL = 1e-6


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def as_float(value: str) -> float:
    return float(value)


def ordered_periods(rows: list[dict[str, str]]) -> list[str]:
    return [row["period_id"] for row in sorted(rows, key=lambda row: int(row["day_index"]))]


def safe_gap(model: Model) -> float | None:
    try:
        return float(model.getGap())
    except Exception:
        return None


def safe_dualbound(model: Model) -> float | None:
    try:
        value = float(model.getDualbound())
        if math.isfinite(value):
            return value
    except Exception:
        return None
    return None


def get_time_limit_seconds() -> float:
    raw = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if raw is None:
        return MAX_SINGLE_RUN_SECONDS
    try:
        limit = float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid ORCLAW_SOLVE_TIME_LIMIT_SECONDS={raw!r}") from exc
    if limit <= 0:
        raise ValueError("ORCLAW_SOLVE_TIME_LIMIT_SECONDS must be positive")
    return min(limit, MAX_SINGLE_RUN_SECONDS)


def format_qty(value: float) -> str:
    clipped = 0.0 if abs(value) < 5e-10 else value
    return f"{clipped:.8f}"


def read_solution_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def load_data() -> dict:
    config = json.loads((DATA_DIR / "config.json").read_text())

    periods_rows = read_csv(DATA_DIR / "periods.csv")
    product_rows = read_csv(DATA_DIR / "products.csv")
    terminal_rows = read_csv(DATA_DIR / "terminals.csv")
    depot_rows = read_csv(DATA_DIR / "depots.csv")
    station_rows = read_csv(DATA_DIR / "stations.csv")
    scenario_rows = read_csv(DATA_DIR / "scenarios.csv")
    station_tank_rows = read_csv(DATA_DIR / "station_tanks.csv")
    inbound_lane_rows = read_csv(DATA_DIR / "terminal_depot_lanes.csv")
    inbound_commit_rows = read_csv(DATA_DIR / "terminal_depot_commitments.csv")
    terminal_capacity_rows = read_csv(DATA_DIR / "terminal_product_capacity.csv")
    terminal_mult_rows = read_csv(DATA_DIR / "scenario_terminal_capacity.csv")
    depot_fleet_rows = read_csv(DATA_DIR / "depot_fleet.csv")
    depot_shift_rows = read_csv(DATA_DIR / "depot_product_shift_capacity.csv")
    outbound_lane_rows = read_csv(DATA_DIR / "depot_station_lanes.csv")
    demand_rows = read_csv(DATA_DIR / "scenario_demands.csv")
    route_mult_rows = read_csv(DATA_DIR / "scenario_lane_impacts.csv")
    emergency_cap_rows = read_csv(DATA_DIR / "scenario_emergency_caps.csv")

    periods = ordered_periods(periods_rows)
    period_index = {period: idx for idx, period in enumerate(periods)}
    products = [row["product_id"] for row in product_rows]
    terminals = [row["terminal_id"] for row in terminal_rows]
    depots = [row["depot_id"] for row in depot_rows]
    stations = [row["station_id"] for row in station_rows]
    scenarios = [row["scenario_id"] for row in scenario_rows]

    product_params = {
        row["product_id"]: {
            "shortage_penalty": as_float(row["shortage_penalty_per_kl"]),
            "emergency_cost": as_float(row["emergency_cost_per_kl"]),
            "holding_station": as_float(row["holding_cost_station_per_kl_day"]),
            "holding_depot": as_float(row["holding_cost_depot_per_kl_day"]),
            "carbon_cost": as_float(row["carbon_cost_per_kg"]),
        }
        for row in product_rows
    }
    scenario_prob = {row["scenario_id"]: as_float(row["probability"]) for row in scenario_rows}

    depot_init: dict[tuple[str, str], float] = {}
    depot_cap: dict[tuple[str, str], float] = {}
    depot_reserve: dict[tuple[str, str], float] = {}
    for row in depot_rows:
        depot = row["depot_id"]
        for product in products:
            depot_init[(depot, product)] = as_float(row[f"initial_{product}"])
            depot_cap[(depot, product)] = as_float(row[f"cap_{product}"])
            depot_reserve[(depot, product)] = as_float(row[f"reserve_{product}"])

    station_tanks = {}
    for row in station_tank_rows:
        key = (row["station_id"], row["product_id"])
        station_tanks[key] = {
            "tank_capacity": as_float(row["tank_capacity_kl"]),
            "initial_inventory": as_float(row["initial_inventory_kl"]),
            "reserve": as_float(row["emergency_reserve_kl"]),
            "service_floor": as_float(row["daily_service_floor"]),
        }

    demand = {(row["scenario_id"], row["period_id"], row["station_id"], row["product_id"]): as_float(row["demand_kl"]) for row in demand_rows}
    active_station_products = sorted({(row["station_id"], row["product_id"]) for row in demand_rows})

    for station, product in active_station_products:
        tank = station_tanks.get((station, product))
        if tank is None:
            raise ValueError(f"Missing station_tanks row for {(station, product)}")
        if tank["tank_capacity"] <= 0:
            raise ValueError(f"Active demand pair {(station, product)} has nonpositive tank capacity")

    station_init = {(station, product): station_tanks[(station, product)]["initial_inventory"] for station, product in active_station_products}
    station_cap = {(station, product): station_tanks[(station, product)]["tank_capacity"] for station, product in active_station_products}
    station_reserve = {(station, product): station_tanks[(station, product)]["reserve"] for station, product in active_station_products}
    station_floor = {(station, product): station_tanks[(station, product)]["service_floor"] for station, product in active_station_products}

    inbound_arcs = []
    inbound_cap = {}
    inbound_unit_cost = {}
    inbound_depots_by_tp: dict[tuple[str, str], list[str]] = defaultdict(list)
    inbound_terminals_by_dp: dict[tuple[str, str], list[str]] = defaultdict(list)
    inbound_arc_set = set()
    for row in inbound_lane_rows:
        key = (row["terminal_id"], row["depot_id"], row["product_id"])
        inbound_arc_set.add(key)
        inbound_arcs.append(key)
        inbound_cap[key] = as_float(row["max_daily_kl"])
        inbound_unit_cost[key] = as_float(row["cost_per_kl"]) + product_params[row["product_id"]]["carbon_cost"] * as_float(row["carbon_kg_per_kl"])
        inbound_depots_by_tp[(row["terminal_id"], row["product_id"])].append(row["depot_id"])
        inbound_terminals_by_dp[(row["depot_id"], row["product_id"])].append(row["terminal_id"])

    commitments = defaultdict(float)
    for row in inbound_commit_rows:
        key = (row["period_id"], row["terminal_id"], row["depot_id"], row["product_id"])
        commitments[key] += as_float(row["minimum_commitment_kl"])

    term_nom_cap = {(row["period_id"], row["terminal_id"], row["product_id"]): as_float(row["max_load_kl"]) for row in terminal_capacity_rows}
    min_term_multiplier: dict[tuple[str, str, str], float] = {}
    for row in terminal_mult_rows:
        key = (row["period_id"], row["terminal_id"], row["product_id"])
        value = as_float(row["capacity_multiplier"])
        if key not in min_term_multiplier:
            min_term_multiplier[key] = value
        else:
            min_term_multiplier[key] = min(min_term_multiplier[key], value)
    robust_terminal_cap = {}
    for key, nominal in term_nom_cap.items():
        if key not in min_term_multiplier:
            raise ValueError(f"Missing scenario terminal multiplier for {key}")
        robust_terminal_cap[key] = nominal * min_term_multiplier[key]

    outbound_arcs = []
    outbound_arc_set = set()
    outbound_products_by_pair: dict[tuple[str, str], list[str]] = defaultdict(list)
    outbound_stations_by_dp: dict[tuple[str, str], list[str]] = defaultdict(list)
    outbound_depots_by_jp: dict[tuple[str, str], list[str]] = defaultdict(list)
    outbound_unit_cost = {}
    outbound_base_cap = {}
    for row in outbound_lane_rows:
        key = (row["depot_id"], row["station_id"], row["product_id"])
        outbound_arc_set.add(key)
        outbound_arcs.append(key)
        outbound_products_by_pair[(row["depot_id"], row["station_id"])].append(row["product_id"])
        outbound_stations_by_dp[(row["depot_id"], row["product_id"])].append(row["station_id"])
        outbound_depots_by_jp[(row["station_id"], row["product_id"])].append(row["depot_id"])
        outbound_unit_cost[key] = as_float(row["cost_per_kl"]) + product_params[row["product_id"]]["carbon_cost"] * as_float(row["carbon_kg_per_kl"])
        outbound_base_cap[key] = as_float(row["max_daily_kl"])

    route_pairs = sorted(outbound_products_by_pair)
    route_pair_set = set(route_pairs)

    route_multiplier = {}
    route_eff_cap = {}
    for row in route_mult_rows:
        arc = (row["depot_id"], row["station_id"], row["product_id"])
        if arc not in outbound_arc_set:
            continue
        key = (row["scenario_id"], row["period_id"], row["depot_id"], row["station_id"], row["product_id"])
        value = as_float(row["capacity_multiplier"])
        route_multiplier[key] = value
        route_eff_cap[key] = value * outbound_base_cap[arc]

    fleet_cap = {(row["period_id"], row["depot_id"]): as_float(row["max_outbound_kl"]) for row in depot_fleet_rows}
    fleet_stops = {(row["period_id"], row["depot_id"]): int(float(row["max_active_stops"])) for row in depot_fleet_rows}
    shift_cap = {(row["period_id"], row["depot_id"], row["product_id"]): as_float(row["max_product_outbound_kl"]) for row in depot_shift_rows}
    emergency_cap = {(row["scenario_id"], row["product_id"]): as_float(row["max_emergency_kl"]) for row in emergency_cap_rows}

    depot_products = [(depot, product) for depot in depots for product in products]
    station_products = active_station_products
    outbound_keys = [(scenario, period, depot, station, product) for scenario in scenarios for period in periods for (depot, station, product) in outbound_arcs]
    stop_keys = [(scenario, period, depot, station) for scenario in scenarios for period in periods for (depot, station) in route_pairs]
    depot_inventory_keys = [(scenario, period, depot, product) for scenario in scenarios for period in periods for depot in depots for product in products]
    station_keys = [(scenario, period, station, product) for scenario in scenarios for period in periods for (station, product) in station_products]
    inbound_keys = [(period, terminal, depot, product) for period in periods for (terminal, depot, product) in inbound_arcs]

    return {
        "config": config,
        "periods": periods,
        "period_index": period_index,
        "products": products,
        "terminals": terminals,
        "depots": depots,
        "stations": stations,
        "scenarios": scenarios,
        "scenario_prob": scenario_prob,
        "product_params": product_params,
        "active_station_products": station_products,
        "station_cap": station_cap,
        "station_init": station_init,
        "station_reserve": station_reserve,
        "station_floor": station_floor,
        "demand": demand,
        "depot_init": depot_init,
        "depot_cap": depot_cap,
        "depot_reserve": depot_reserve,
        "inbound_arcs": inbound_arcs,
        "inbound_arc_set": inbound_arc_set,
        "inbound_cap": inbound_cap,
        "inbound_unit_cost": inbound_unit_cost,
        "commitments": commitments,
        "inbound_depots_by_tp": inbound_depots_by_tp,
        "inbound_terminals_by_dp": inbound_terminals_by_dp,
        "robust_terminal_cap": robust_terminal_cap,
        "outbound_arcs": outbound_arcs,
        "outbound_arc_set": outbound_arc_set,
        "outbound_products_by_pair": outbound_products_by_pair,
        "outbound_stations_by_dp": outbound_stations_by_dp,
        "outbound_depots_by_jp": outbound_depots_by_jp,
        "route_pairs": route_pairs,
        "route_pair_set": route_pair_set,
        "route_eff_cap": route_eff_cap,
        "outbound_unit_cost": outbound_unit_cost,
        "fleet_cap": fleet_cap,
        "fleet_stops": fleet_stops,
        "shift_cap": shift_cap,
        "emergency_cap": emergency_cap,
        "depot_products": depot_products,
        "inbound_keys": inbound_keys,
        "outbound_keys": outbound_keys,
        "stop_keys": stop_keys,
        "depot_inventory_keys": depot_inventory_keys,
        "station_keys": station_keys,
    }


def build_model(
    data: dict,
    time_limit: float,
    fixed_inbound: dict[tuple[str, str, str, str], float] | None = None,
    inbound_l1_radius: float | None = None,
    inbound_center: dict[tuple[str, str, str, str], float] | None = None,
) -> tuple[Model, dict[str, dict], dict[str, float]]:
    model = Model("petroleum_terminal_dro_inventory")
    model.hideOutput()
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", SCIP_GAP)
    model.setParam("limits/time", time_limit)

    config = data["config"]
    periods = data["periods"]
    scenarios = data["scenarios"]
    products = data["products"]
    depots = data["depots"]
    inbound_arcs = data["inbound_arcs"]
    outbound_arcs = data["outbound_arcs"]
    route_pairs = data["route_pairs"]
    station_products = data["active_station_products"]

    x = {}
    for period in periods:
        for terminal, depot, product in inbound_arcs:
            key = (period, terminal, depot, product)
            fixed_value = None if fixed_inbound is None else fixed_inbound.get(key, 0.0)
            x[key] = model.addVar(
                lb=data["commitments"].get(key, 0.0) if fixed_value is None else fixed_value,
                ub=data["inbound_cap"][(terminal, depot, product)] if fixed_value is None else fixed_value,
                vtype="C",
                name=f"x[{period},{terminal},{depot},{product}]",
            )

    if inbound_l1_radius is not None:
        if inbound_center is None:
            raise ValueError("inbound_center is required when inbound_l1_radius is set")
        l1_terms = []
        for key in data["inbound_keys"]:
            center = inbound_center.get(key, 0.0)
            dev_pos = model.addVar(lb=0.0, vtype="C", name=f"dx_pos[{','.join(key)}]")
            dev_neg = model.addVar(lb=0.0, vtype="C", name=f"dx_neg[{','.join(key)}]")
            model.addCons(x[key] - center == dev_pos - dev_neg, name=f"inbound_dev[{','.join(key)}]")
            l1_terms.append(dev_pos)
            l1_terms.append(dev_neg)
        model.addCons(quicksum(l1_terms) <= inbound_l1_radius, name="inbound_l1_radius")

    q = {}
    for scenario, period, depot, station, product in data["outbound_keys"]:
        eff_cap = data["route_eff_cap"][(scenario, period, depot, station, product)]
        q[(scenario, period, depot, station, product)] = model.addVar(
            lb=0.0,
            ub=eff_cap,
            vtype="C",
            name=f"q[{scenario},{period},{depot},{station},{product}]",
        )

    y = {}
    for scenario, period, depot, station in data["stop_keys"]:
        y[(scenario, period, depot, station)] = model.addVar(
            vtype="B",
            name=f"y[{scenario},{period},{depot},{station}]",
        )

    depot_inventory = {}
    for scenario, period, depot, product in data["depot_inventory_keys"]:
        depot_inventory[(scenario, period, depot, product)] = model.addVar(
            lb=data["depot_reserve"][(depot, product)],
            ub=data["depot_cap"][(depot, product)],
            vtype="C",
            name=f"I[{scenario},{period},{depot},{product}]",
        )

    emergency = {}
    shortage = {}
    station_inventory = {}
    reserve_binding = {}
    for scenario, period, station, product in data["station_keys"]:
        demand = data["demand"][(scenario, period, station, product)]
        floor = data["station_floor"][(station, product)]
        reserve = data["station_reserve"][(station, product)]
        cap = data["station_cap"][(station, product)]
        emergency[(scenario, period, station, product)] = model.addVar(
            lb=0.0,
            vtype="C",
            name=f"e[{scenario},{period},{station},{product}]",
        )
        shortage[(scenario, period, station, product)] = model.addVar(
            lb=0.0,
            ub=(1.0 - floor) * demand,
            vtype="C",
            name=f"u[{scenario},{period},{station},{product}]",
        )
        station_inventory[(scenario, period, station, product)] = model.addVar(
            lb=data["station_reserve"][(station, product)],
            ub=data["station_cap"][(station, product)],
            vtype="C",
            name=f"V[{scenario},{period},{station},{product}]",
        )
        reserve_binding[(scenario, period, station, product)] = model.addVar(
            vtype="B",
            name=f"b[{scenario},{period},{station},{product}]",
        )
        model.addCons(
            shortage[(scenario, period, station, product)]
            <= (1.0 - floor) * demand * reserve_binding[(scenario, period, station, product)],
            name=f"shortage_link[{scenario},{period},{station},{product}]",
        )
        model.addCons(
            station_inventory[(scenario, period, station, product)]
            <= reserve + (cap - reserve) * (1 - reserve_binding[(scenario, period, station, product)]),
            name=f"reserve_branch[{scenario},{period},{station},{product}]",
        )

    scenario_cost = {scenario: model.addVar(lb=0.0, vtype="C", name=f"c[{scenario}]") for scenario in scenarios}
    worst_cost = model.addVar(lb=0.0, vtype="C", name="worst_cost")

    for period in periods:
        for terminal in data["terminals"]:
            for product in products:
                depots_for_arc = data["inbound_depots_by_tp"].get((terminal, product), [])
                if not depots_for_arc:
                    continue
                model.addCons(
                    quicksum(x[(period, terminal, depot, product)] for depot in depots_for_arc)
                    <= data["robust_terminal_cap"][(period, terminal, product)],
                    name=f"terminal_cap[{period},{terminal},{product}]",
                )

    for scenario in scenarios:
        for period_idx, period in enumerate(periods):
            prev_period = periods[period_idx - 1] if period_idx > 0 else None

            for depot in depots:
                for product in products:
                    inbound_terms = data["inbound_terminals_by_dp"].get((depot, product), [])
                    outbound_stations = data["outbound_stations_by_dp"].get((depot, product), [])
                    prev_inventory = (
                        data["depot_init"][(depot, product)]
                        if prev_period is None
                        else depot_inventory[(scenario, prev_period, depot, product)]
                    )
                    model.addCons(
                        depot_inventory[(scenario, period, depot, product)]
                        == prev_inventory
                        + quicksum(x[(period, terminal, depot, product)] for terminal in inbound_terms)
                        - quicksum(q[(scenario, period, depot, station, product)] for station in outbound_stations),
                        name=f"depot_balance[{scenario},{period},{depot},{product}]",
                    )
                    model.addCons(
                        quicksum(q[(scenario, period, depot, station, product)] for station in outbound_stations)
                        <= data["shift_cap"][(period, depot, product)],
                        name=f"shift_cap[{scenario},{period},{depot},{product}]",
                    )

                model.addCons(
                    quicksum(
                        q[(scenario, period, depot, station, product)]
                        for station, product in (
                            (station, product)
                            for station in data["stations"]
                            for product in data["outbound_products_by_pair"].get((depot, station), [])
                        )
                    )
                    <= data["fleet_cap"][(period, depot)],
                    name=f"fleet_cap[{scenario},{period},{depot}]",
                )
                model.addCons(
                    quicksum(y[(scenario, period, depot, station)] for station in data["stations"] if (depot, station) in data["route_pair_set"])
                    <= data["fleet_stops"][(period, depot)],
                    name=f"fleet_stops[{scenario},{period},{depot}]",
                )

            for station, product in station_products:
                inbound_depots = data["outbound_depots_by_jp"][(station, product)]
                prev_inventory = (
                    data["station_init"][(station, product)]
                    if prev_period is None
                    else station_inventory[(scenario, prev_period, station, product)]
                )
                model.addCons(
                    station_inventory[(scenario, period, station, product)]
                    == prev_inventory
                    + quicksum(q[(scenario, period, depot, station, product)] for depot in inbound_depots)
                    + emergency[(scenario, period, station, product)]
                    - data["demand"][(scenario, period, station, product)]
                    + shortage[(scenario, period, station, product)],
                    name=f"station_balance[{scenario},{period},{station},{product}]",
                )

            for depot, station in route_pairs:
                products_on_pair = data["outbound_products_by_pair"][(depot, station)]
                for product in products_on_pair:
                    model.addCons(
                        q[(scenario, period, depot, station, product)]
                        <= data["route_eff_cap"][(scenario, period, depot, station, product)] * y[(scenario, period, depot, station)],
                        name=f"route_link[{scenario},{period},{depot},{station},{product}]",
                    )
                model.addCons(
                    quicksum(q[(scenario, period, depot, station, product)] for product in products_on_pair)
                    <= config["max_kl_per_active_stop"] * y[(scenario, period, depot, station)],
                    name=f"stop_cap[{scenario},{period},{depot},{station}]",
                )

        for product in products:
            stations_for_product = [station for station, product2 in station_products if product2 == product]
            model.addCons(
                quicksum(emergency[(scenario, period, station, product)] for period in periods for station in stations_for_product)
                <= data["emergency_cap"][(scenario, product)],
                name=f"emergency_cap[{scenario},{product}]",
            )

        scenario_expr = (
            quicksum(
                data["outbound_unit_cost"][(depot, station, product)] * q[(scenario, period, depot, station, product)]
                for period in periods
                for depot, station, product in outbound_arcs
            )
            + config["active_stop_cost"]
            * quicksum(y[(scenario, period, depot, station)] for period in periods for depot, station in route_pairs)
            + quicksum(
                data["product_params"][product]["emergency_cost"] * emergency[(scenario, period, station, product)]
                for period in periods
                for station, product in station_products
            )
            + quicksum(
                data["product_params"][product]["shortage_penalty"] * shortage[(scenario, period, station, product)]
                for period in periods
                for station, product in station_products
            )
            + quicksum(
                data["product_params"][product]["holding_depot"] * depot_inventory[(scenario, period, depot, product)]
                for period in periods
                for depot in depots
                for product in products
            )
            + quicksum(
                data["product_params"][product]["holding_station"] * station_inventory[(scenario, period, station, product)]
                for period in periods
                for station, product in station_products
            )
        )
        model.addCons(scenario_cost[scenario] == scenario_expr, name=f"scenario_cost[{scenario}]")
        model.addCons(worst_cost >= scenario_cost[scenario], name=f"worst_cost_lb[{scenario}]")

    inbound_cost_expr = quicksum(
        data["inbound_unit_cost"][(terminal, depot, product)] * x[(period, terminal, depot, product)]
        for period, terminal, depot, product in data["inbound_keys"]
    )

    model.setObjective(
        inbound_cost_expr
        + (1.0 - config["ambiguity_worst_case_weight"])
        * quicksum(data["scenario_prob"][scenario] * scenario_cost[scenario] for scenario in scenarios)
        + config["ambiguity_worst_case_weight"] * worst_cost,
        "minimize",
    )

    vars_by_name = {
        "x": x,
        "q": q,
        "y": y,
        "depot_inventory": depot_inventory,
        "emergency": emergency,
        "shortage": shortage,
        "station_inventory": station_inventory,
        "reserve_binding": reserve_binding,
        "scenario_cost": scenario_cost,
        "worst_cost": {"worst_cost": worst_cost},
    }
    return model, vars_by_name, {"time_limit": time_limit}


def extract_solution(model: Model, vars_by_name: dict[str, dict], data: dict) -> list[dict[str, str]]:
    if model.getNSols() <= 0 or model.getBestSol() is None:
        raise RuntimeError("SCIP finished without an incumbent solution")

    best_sol = model.getBestSol()
    rows: list[tuple[str, str, str, str, str, str, float]] = []

    for key, var in vars_by_name["x"].items():
        value = model.getSolVal(best_sol, var)
        if value > EXPORT_EPS:
            period, terminal, depot, product = key
            rows.append(("inbound", "", period, terminal, depot, product, value))

    for key, var in vars_by_name["q"].items():
        value = model.getSolVal(best_sol, var)
        if value > EXPORT_EPS:
            scenario, period, depot, station, product = key
            rows.append(("delivery", scenario, period, depot, station, product, value))

    for key, var in vars_by_name["emergency"].items():
        value = model.getSolVal(best_sol, var)
        if value > EXPORT_EPS:
            scenario, period, station, product = key
            rows.append(("emergency", scenario, period, "EMERGENCY", station, product, value))

    row_order = {"inbound": 0, "delivery": 1, "emergency": 2}
    rows.sort(key=lambda row: (row_order[row[0]], row[1], data["period_index"][row[2]], row[3], row[4], row[5]))

    export_rows = []
    for row_type, scenario, period, origin, destination, product, quantity in rows:
        export_rows.append(
            {
                "row_type": row_type,
                "scenario_id": scenario,
                "period_id": period,
                "origin_id": origin,
                "destination_id": destination,
                "product_id": product,
                "quantity": format_qty(quantity),
            }
        )
    return export_rows


def write_solution(rows: list[dict[str, str]], path: Path) -> None:
    fieldnames = ["row_type", "scenario_id", "period_id", "origin_id", "destination_id", "product_id", "quantity"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def audit_solution(data: dict, path: Path) -> dict:
    expected_fields = ["row_type", "scenario_id", "period_id", "origin_id", "destination_id", "product_id", "quantity"]
    inbound = defaultdict(float)
    delivery = defaultdict(float)
    emergency = defaultdict(float)

    violated_groups: set[str] = set()
    messages: list[str] = []
    max_theta = 0.0

    def record_violation(group: str, message: str, magnitude: float, scale: float) -> None:
        nonlocal max_theta
        violated_groups.add(group)
        theta = max(0.0, magnitude) / max(1.0, abs(scale))
        max_theta = max(max_theta, theta)
        messages.append(message)

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_fields:
            record_violation("schema", f"Unexpected columns: {reader.fieldnames}", 1.0, 1.0)
        for idx, row in enumerate(reader, start=2):
            try:
                quantity = float(row["quantity"])
            except Exception:
                record_violation("schema", f"Row {idx}: invalid quantity {row['quantity']!r}", 1.0, 1.0)
                continue
            if not math.isfinite(quantity) or quantity < -CHECK_TOL:
                record_violation("nonnegativity", f"Row {idx}: invalid quantity {quantity}", max(0.0, -quantity), 1.0)
                continue
            quantity = max(0.0, quantity)
            row_type = row["row_type"]
            period = row["period_id"]
            product = row["product_id"]
            scenario = row["scenario_id"]
            origin = row["origin_id"]
            destination = row["destination_id"]

            if period not in data["period_index"]:
                record_violation("schema", f"Row {idx}: invalid period {period}", 1.0, 1.0)
                continue
            if product not in data["products"]:
                record_violation("schema", f"Row {idx}: invalid product {product}", 1.0, 1.0)
                continue

            if row_type == "inbound":
                if scenario not in ("", None):
                    record_violation("schema", f"Row {idx}: inbound row must have blank scenario_id", 1.0, 1.0)
                key = (period, origin, destination, product)
                if (origin, destination, product) not in data["inbound_arc_set"]:
                    record_violation("schema", f"Row {idx}: invalid inbound lane {key}", 1.0, 1.0)
                    continue
                inbound[key] += quantity
            elif row_type == "delivery":
                if scenario not in data["scenarios"]:
                    record_violation("schema", f"Row {idx}: invalid scenario {scenario}", 1.0, 1.0)
                    continue
                key = (scenario, period, origin, destination, product)
                if (origin, destination, product) not in data["outbound_arc_set"]:
                    record_violation("schema", f"Row {idx}: invalid delivery lane {key}", 1.0, 1.0)
                    continue
                delivery[key] += quantity
            elif row_type == "emergency":
                if scenario not in data["scenarios"]:
                    record_violation("schema", f"Row {idx}: invalid scenario {scenario}", 1.0, 1.0)
                    continue
                if origin != "EMERGENCY":
                    record_violation("schema", f"Row {idx}: emergency origin must be EMERGENCY", 1.0, 1.0)
                    continue
                if (destination, product) not in data["active_station_products"]:
                    record_violation("schema", f"Row {idx}: invalid emergency station-product {(destination, product)}", 1.0, 1.0)
                    continue
                emergency[(scenario, period, destination, product)] += quantity
            else:
                record_violation("schema", f"Row {idx}: invalid row_type {row_type}", 1.0, 1.0)

    for period, terminal, depot, product in data["inbound_keys"]:
        quantity = inbound.get((period, terminal, depot, product), 0.0)
        cap = data["inbound_cap"][(terminal, depot, product)]
        commit = data["commitments"].get((period, terminal, depot, product), 0.0)
        if quantity > cap + CHECK_TOL:
            record_violation(
                "inbound_lane_cap",
                f"Inbound {period, terminal, depot, product} exceeds lane cap: {quantity:.6f} > {cap:.6f}",
                quantity - cap,
                cap,
            )
        if quantity + CHECK_TOL < commit:
            record_violation(
                "commitment",
                f"Inbound {period, terminal, depot, product} misses commitment: {quantity:.6f} < {commit:.6f}",
                commit - quantity,
                commit,
            )

    for period in data["periods"]:
        for terminal in data["terminals"]:
            for product in data["products"]:
                depots_for_arc = data["inbound_depots_by_tp"].get((terminal, product), [])
                if not depots_for_arc:
                    continue
                total = sum(inbound.get((period, terminal, depot, product), 0.0) for depot in depots_for_arc)
                cap = data["robust_terminal_cap"][(period, terminal, product)]
                if total > cap + CHECK_TOL:
                    record_violation(
                        "terminal_capacity",
                        f"Terminal cap {period, terminal, product} exceeded: {total:.6f} > {cap:.6f}",
                        total - cap,
                        cap,
                    )

    inbound_cost = 0.0
    for period, terminal, depot, product in data["inbound_keys"]:
        inbound_cost += data["inbound_unit_cost"][(terminal, depot, product)] * inbound.get((period, terminal, depot, product), 0.0)

    scenario_costs: dict[str, float] = {}
    x_values = {key: inbound.get(key, 0.0) for key in data["inbound_keys"]}
    q_values = {key: delivery.get(key, 0.0) for key in data["outbound_keys"]}
    emergency_values = {key: emergency.get(key, 0.0) for key in data["station_keys"]}
    y_values: dict[tuple[str, str, str, str], float] = {}
    depot_inventory_values: dict[tuple[str, str, str, str], float] = {}
    station_inventory_values: dict[tuple[str, str, str, str], float] = {}
    shortage_values: dict[tuple[str, str, str, str], float] = {}
    reserve_binding_values: dict[tuple[str, str, str, str], float] = {}

    for scenario in data["scenarios"]:
        depot_prev = {key: data["depot_init"][key] for key in data["depot_products"]}
        station_prev = {key: data["station_init"][key] for key in data["active_station_products"]}
        scenario_cost = 0.0
        emergency_used = defaultdict(float)

        for period in data["periods"]:
            active_stop_count = defaultdict(int)
            fleet_outbound = defaultdict(float)
            shift_outbound = defaultdict(float)
            pair_totals = defaultdict(float)
            depot_outbound = defaultdict(float)
            station_inbound = defaultdict(float)

            for depot, station, product in data["outbound_arcs"]:
                qty = delivery.get((scenario, period, depot, station, product), 0.0)
                cap = data["route_eff_cap"][(scenario, period, depot, station, product)]
                if qty > cap + CHECK_TOL:
                    record_violation(
                        "delivery_lane_cap",
                        f"Delivery cap {scenario, period, depot, station, product} exceeded: {qty:.6f} > {cap:.6f}",
                        qty - cap,
                        cap,
                    )
                depot_outbound[(depot, product)] += qty
                fleet_outbound[depot] += qty
                shift_outbound[(depot, product)] += qty
                pair_totals[(depot, station)] += qty
                station_inbound[(station, product)] += qty
                scenario_cost += data["outbound_unit_cost"][(depot, station, product)] * qty

            for depot, station in data["route_pairs"]:
                total = pair_totals[(depot, station)]
                y_values[(scenario, period, depot, station)] = 1.0 if total > CHECK_TOL else 0.0
                if total > CHECK_TOL:
                    active_stop_count[depot] += 1
                    scenario_cost += data["config"]["active_stop_cost"]
                if total > data["config"]["max_kl_per_active_stop"] + CHECK_TOL:
                    record_violation(
                        "stop_cap",
                        f"Stop cap {scenario, period, depot, station} exceeded: {total:.6f} > {data['config']['max_kl_per_active_stop']:.6f}",
                        total - data["config"]["max_kl_per_active_stop"],
                        data["config"]["max_kl_per_active_stop"],
                    )

            for depot in data["depots"]:
                if fleet_outbound[depot] > data["fleet_cap"][(period, depot)] + CHECK_TOL:
                    record_violation(
                        "fleet_cap",
                        f"Fleet cap {scenario, period, depot} exceeded: {fleet_outbound[depot]:.6f} > {data['fleet_cap'][(period, depot)]:.6f}",
                        fleet_outbound[depot] - data["fleet_cap"][(period, depot)],
                        data["fleet_cap"][(period, depot)],
                    )
                if active_stop_count[depot] > data["fleet_stops"][(period, depot)]:
                    record_violation(
                        "fleet_stops",
                        f"Stop limit {scenario, period, depot} exceeded: {active_stop_count[depot]} > {data['fleet_stops'][(period, depot)]}",
                        active_stop_count[depot] - data["fleet_stops"][(period, depot)],
                        data["fleet_stops"][(period, depot)],
                    )
                for product in data["products"]:
                    if shift_outbound[(depot, product)] > data["shift_cap"][(period, depot, product)] + CHECK_TOL:
                        record_violation(
                            "shift_cap",
                            f"Shift cap {scenario, period, depot, product} exceeded: {shift_outbound[(depot, product)]:.6f} > {data['shift_cap'][(period, depot, product)]:.6f}",
                            shift_outbound[(depot, product)] - data["shift_cap"][(period, depot, product)],
                            data["shift_cap"][(period, depot, product)],
                        )

                    end_inventory = depot_prev[(depot, product)] + sum(
                        inbound.get((period, terminal, depot, product), 0.0)
                        for terminal in data["inbound_terminals_by_dp"].get((depot, product), [])
                    ) - depot_outbound[(depot, product)]
                    if end_inventory < data["depot_reserve"][(depot, product)] - CHECK_TOL:
                        record_violation(
                            "depot_reserve",
                            f"Depot reserve {scenario, period, depot, product} violated: {end_inventory:.6f} < {data['depot_reserve'][(depot, product)]:.6f}",
                            data["depot_reserve"][(depot, product)] - end_inventory,
                            data["depot_reserve"][(depot, product)],
                        )
                    if end_inventory > data["depot_cap"][(depot, product)] + CHECK_TOL:
                        record_violation(
                            "depot_capacity",
                            f"Depot capacity {scenario, period, depot, product} exceeded: {end_inventory:.6f} > {data['depot_cap'][(depot, product)]:.6f}",
                            end_inventory - data["depot_cap"][(depot, product)],
                            data["depot_cap"][(depot, product)],
                        )
                    depot_prev[(depot, product)] = end_inventory
                    depot_inventory_values[(scenario, period, depot, product)] = end_inventory
                    scenario_cost += data["product_params"][product]["holding_depot"] * end_inventory

            for station, product in data["active_station_products"]:
                em_qty = emergency.get((scenario, period, station, product), 0.0)
                emergency_used[product] += em_qty
                scenario_cost += data["product_params"][product]["emergency_cost"] * em_qty
                available = station_prev[(station, product)] + station_inbound[(station, product)] + em_qty
                reserve = data["station_reserve"][(station, product)]
                demand = data["demand"][(scenario, period, station, product)]
                shortage = max(0.0, demand + reserve - available)
                max_shortage = (1.0 - data["station_floor"][(station, product)]) * demand
                if shortage > max_shortage + CHECK_TOL:
                    record_violation(
                        "service_floor",
                        f"Service floor {scenario, period, station, product} violated: shortage {shortage:.6f} > {max_shortage:.6f}",
                        shortage - max_shortage,
                        max_shortage if max_shortage > 0 else 1.0,
                    )
                if shortage > demand + CHECK_TOL:
                    record_violation(
                        "station_balance",
                        f"Station balance impossible at {scenario, period, station, product}: implied shortage {shortage:.6f} exceeds demand {demand:.6f}",
                        shortage - demand,
                        demand if demand > 0 else 1.0,
                    )
                end_inventory = available - demand + shortage
                if end_inventory < reserve - CHECK_TOL:
                    record_violation(
                        "station_reserve",
                        f"Station reserve {scenario, period, station, product} violated: {end_inventory:.6f} < {reserve:.6f}",
                        reserve - end_inventory,
                        reserve,
                    )
                if end_inventory > data["station_cap"][(station, product)] + CHECK_TOL:
                    record_violation(
                        "station_capacity",
                        f"Station capacity {scenario, period, station, product} exceeded: {end_inventory:.6f} > {data['station_cap'][(station, product)]:.6f}",
                        end_inventory - data["station_cap"][(station, product)],
                        data["station_cap"][(station, product)],
                    )
                station_prev[(station, product)] = end_inventory
                shortage_values[(scenario, period, station, product)] = shortage
                station_inventory_values[(scenario, period, station, product)] = end_inventory
                reserve_binding_values[(scenario, period, station, product)] = 1.0 if (shortage > CHECK_TOL or end_inventory <= reserve + CHECK_TOL) else 0.0
                scenario_cost += data["product_params"][product]["shortage_penalty"] * shortage
                scenario_cost += data["product_params"][product]["holding_station"] * end_inventory

        for product in data["products"]:
            used = emergency_used[product]
            cap = data["emergency_cap"][(scenario, product)]
            if used > cap + CHECK_TOL:
                record_violation(
                    "emergency_cap",
                    f"Emergency cap {scenario, product} exceeded: {used:.6f} > {cap:.6f}",
                    used - cap,
                    cap,
                )

        scenario_costs[scenario] = scenario_cost

    worst_cost = max(scenario_costs.values()) if scenario_costs else 0.0
    expected_cost = sum(data["scenario_prob"][scenario] * scenario_costs[scenario] for scenario in data["scenarios"])
    total_objective = inbound_cost + (1.0 - data["config"]["ambiguity_worst_case_weight"]) * expected_cost + data["config"]["ambiguity_worst_case_weight"] * worst_cost

    return {
        "feasible": not violated_groups,
        "max_theta": max_theta,
        "violated_groups": sorted(violated_groups),
        "messages": messages[:50],
        "num_messages": len(messages),
        "num_rows": sum(1 for _ in path.open()) - 1,
        "inbound_cost": inbound_cost,
        "scenario_costs": scenario_costs,
        "worst_cost": worst_cost,
        "expected_cost": expected_cost,
        "objective": total_objective,
        "state": {
            "x": x_values,
            "q": q_values,
            "emergency": emergency_values,
            "y": y_values,
            "depot_inventory": depot_inventory_values,
            "station_inventory": station_inventory_values,
            "shortage": shortage_values,
            "reserve_binding": reserve_binding_values,
            "scenario_cost": scenario_costs,
            "worst_cost": {"worst_cost": worst_cost},
        },
    }


def apply_mip_start(model: Model, vars_by_name: dict[str, dict], state: dict[str, dict]) -> bool:
    sol = model.createPartialSol()
    for family in ("x", "q", "y", "depot_inventory", "emergency", "shortage", "station_inventory", "reserve_binding", "scenario_cost", "worst_cost"):
        values = state.get(family, {})
        for key, var in vars_by_name[family].items():
            if key in values:
                model.setSolVal(sol, var, float(values[key]))
    return bool(model.addSol(sol, free=True))


def evaluate_candidate(model: Model, vars_by_name: dict[str, dict], data: dict, candidate_path: Path) -> dict | None:
    if model.getNSols() <= 0 or model.getBestSol() is None:
        return None
    candidate_rows = extract_solution(model, vars_by_name, data)
    write_solution(candidate_rows, candidate_path)
    audit = audit_solution(data, candidate_path)
    return {
        "rows": candidate_rows,
        "audit": audit,
        "objective": audit["objective"],
        "status": str(model.getStatus()),
        "nsols": int(model.getNSols()),
        "dualbound": safe_dualbound(model),
        "gap": safe_gap(model),
        "solver_obj": float(model.getObjVal()),
    }


def append_guarded_log(section_title: str, lines: list[str]) -> None:
    with LOG_PATH.open("a") as handle:
        handle.write(f"\n## {section_title}\n")
        for line in lines:
            handle.write(f"- {line}\n")


def cleanup_temp_artifacts() -> None:
    CANDIDATE_PATH.unlink(missing_ok=True)
    pycache = SUBMISSIONS_DIR / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache)


def run_guarded_attempt(
    data: dict,
    time_limit: float,
    incumbent_state: dict[str, dict],
    *,
    fixed_inbound: dict[tuple[str, str, str, str], float] | None = None,
    inbound_l1_radius: float | None = None,
    inbound_center: dict[tuple[str, str, str, str], float] | None = None,
) -> dict:
    build_started = time.time()
    model, vars_by_name, _ = build_model(
        data,
        time_limit,
        fixed_inbound=fixed_inbound,
        inbound_l1_radius=inbound_l1_radius,
        inbound_center=inbound_center,
    )
    build_seconds = time.time() - build_started
    mip_start_loaded = apply_mip_start(model, vars_by_name, incumbent_state)
    solve_started = time.time()
    model.optimize()
    solve_seconds = time.time() - solve_started
    candidate = evaluate_candidate(model, vars_by_name, data, CANDIDATE_PATH)
    return {
        "build_seconds": build_seconds,
        "solve_seconds": solve_seconds,
        "mip_start_loaded": mip_start_loaded,
        "candidate": candidate,
        "status": str(model.getStatus()),
        "dualbound": safe_dualbound(model),
        "gap": safe_gap(model),
        "nsols": int(model.getNSols()),
    }


def append_log(
    command: str,
    time_limit: float,
    build_seconds: float,
    solve_seconds: float,
    status: str,
    nsols: int,
    incumbent_obj: float | None,
    dualbound: float | None,
    gap: float | None,
    audit: dict | None,
) -> None:
    lines = []
    if not LOG_PATH.exists():
        lines.append("# Solve Log\n")
    lines.append(f"\n## Run {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"- Command: `{command}`\n")
    lines.append(f"- Time limit (s): `{time_limit:.2f}`\n")
    lines.append(f"- Build time (s): `{build_seconds:.2f}`\n")
    lines.append(f"- Solve time (s): `{solve_seconds:.2f}`\n")
    lines.append(f"- SCIP status: `{status}`\n")
    lines.append(f"- Incumbent count: `{nsols}`\n")
    if incumbent_obj is not None:
        lines.append(f"- Solver incumbent objective: `{incumbent_obj:.6f}`\n")
    if dualbound is not None:
        lines.append(f"- Solver dual bound: `{dualbound:.6f}`\n")
    if gap is not None:
        lines.append(f"- Solver relative gap: `{gap:.6f}`\n")
    if audit is None:
        lines.append("- Validation: not run\n")
    else:
        lines.append(f"- Validation feasible: `{audit['feasible']}`\n")
        lines.append(f"- Validation max theta: `{audit['max_theta']:.6e}`\n")
        lines.append(f"- Validation violated groups: `{audit['violated_groups']}`\n")
        lines.append(f"- Validation objective: `{audit['objective']:.6f}`\n")
        lines.append(f"- Validation expected scenario cost: `{audit['expected_cost']:.6f}`\n")
        lines.append(f"- Validation worst scenario cost: `{audit['worst_cost']:.6f}`\n")
        if audit["messages"]:
            lines.append("- Validation notes:\n")
            for message in audit["messages"]:
                lines.append(f"  - {message}\n")
            if audit["num_messages"] > len(audit["messages"]):
                lines.append(f"  - ... {audit['num_messages'] - len(audit['messages'])} more messages omitted\n")

    with LOG_PATH.open("a") as handle:
        handle.writelines(lines)


def main() -> None:
    start = time.time()
    cleanup_temp_artifacts()
    time_limit = get_time_limit_seconds()
    data = load_data()
    command = " ".join(shlex.quote(arg) for arg in sys.argv)

    if SOLUTION_PATH.exists():
        incumbent_rows = read_solution_rows(SOLUTION_PATH)
        incumbent_audit = audit_solution(data, SOLUTION_PATH)
        append_guarded_log(
            f"Guarded Phase {time.strftime('%Y-%m-%d %H:%M:%S')}",
            [
                f"Command: `{command}`",
                "Challenger report: none provided",
                f"Incumbent rows on disk: `{incumbent_audit['num_rows']}`",
                f"Incumbent audit feasible: `{incumbent_audit['feasible']}`",
                f"Incumbent max theta: `{incumbent_audit['max_theta']:.6e}`",
                f"Incumbent violated groups: `{incumbent_audit['violated_groups']}`",
                f"Incumbent objective: `{incumbent_audit['objective']:.6f}`",
                f"Incumbent expected scenario cost: `{incumbent_audit['expected_cost']:.6f}`",
                f"Incumbent worst scenario cost: `{incumbent_audit['worst_cost']:.6f}`",
            ],
        )
        if not incumbent_audit["feasible"]:
            raise RuntimeError(f"Current incumbent failed independent audit: groups={incumbent_audit['violated_groups']}")

        best_rows = incumbent_rows
        best_audit = incumbent_audit
        best_state = incumbent_audit["state"]

        remaining = time_limit
        attempt_specs: list[tuple[str, float, str, float | None]] = []
        fixed_time = min(remaining, 120.0, max(10.0, remaining * 0.35))
        attempt_specs.append(("Fixed-X Reopt", fixed_time, "fixed_x", None))
        remaining -= fixed_time
        if remaining >= 25.0:
            radius_small_time = min(remaining, 90.0, max(20.0, remaining * 0.5))
            attempt_specs.append(("Inbound L1 Neighborhood", radius_small_time, "l1", 20.0))
            remaining -= radius_small_time
        if remaining >= 20.0:
            attempt_specs.append(("Inbound L1 Neighborhood Wide", remaining, "l1", 60.0))

        for label, budget, mode, radius in attempt_specs:
            incumbent_before = best_audit["objective"]
            if mode == "fixed_x":
                result = run_guarded_attempt(
                    data,
                    budget,
                    best_state,
                    fixed_inbound=best_state["x"],
                )
            else:
                result = run_guarded_attempt(
                    data,
                    budget,
                    best_state,
                    inbound_l1_radius=radius,
                    inbound_center=best_state["x"],
                )

            candidate = result["candidate"]
            candidate_obj = None if candidate is None else candidate["objective"]
            candidate_audit = None if candidate is None else candidate["audit"]
            accepted = bool(
                candidate_audit
                and candidate_audit["feasible"]
                and candidate_obj is not None
                and candidate_obj < incumbent_before - IMPROVEMENT_TOL
            )

            append_guarded_log(
                label,
                [
                    f"Budget (s): `{budget:.2f}`",
                    f"MIP start accepted by SCIP: `{result['mip_start_loaded']}`",
                    f"Incumbent objective before attempt: `{incumbent_before:.6f}`",
                    f"Solver status: `{result['status']}`",
                    f"Incumbent count: `{result['nsols']}`",
                    f"Dual bound: `{result['dualbound']}`",
                    f"Relative gap: `{result['gap']}`",
                    f"Build time (s): `{result['build_seconds']:.2f}`",
                    f"Solve time (s): `{result['solve_seconds']:.2f}`",
                    f"Candidate objective: `{candidate_obj:.6f}`" if candidate_obj is not None else "Candidate objective: `None`",
                    f"Candidate audit feasible: `{candidate_audit['feasible']}`" if candidate_audit is not None else "Candidate audit feasible: `False`",
                    f"Candidate violated groups: `{candidate_audit['violated_groups']}`" if candidate_audit is not None else "Candidate violated groups: `['no-incumbent']`",
                    f"Accepted: `{accepted}`",
                ],
            )

            if accepted:
                best_rows = candidate["rows"]
                best_audit = candidate_audit
                best_state = candidate_audit["state"]

        if best_audit["objective"] < incumbent_audit["objective"] - IMPROVEMENT_TOL:
            write_solution(best_rows, SOLUTION_PATH)
            final_audit = audit_solution(data, SOLUTION_PATH)
            if not final_audit["feasible"]:
                raise RuntimeError(f"Accepted candidate failed final audit: groups={final_audit['violated_groups']}")
            append_guarded_log(
                "Guarded Phase Result",
                [
                    f"Published improved artifact: `{final_audit['objective']:.6f}` < `{incumbent_audit['objective']:.6f}`",
                    f"Final audit feasible: `{final_audit['feasible']}`",
                    f"Final violated groups: `{final_audit['violated_groups']}`",
                ],
            )
        else:
            final_audit = audit_solution(data, SOLUTION_PATH)
            append_guarded_log(
                "Guarded Phase Result",
                [
                    f"No strict improvement found; incumbent kept at `{incumbent_audit['objective']:.6f}`",
                    f"Final audit feasible: `{final_audit['feasible']}`",
                    f"Final violated groups: `{final_audit['violated_groups']}`",
                ],
            )

        elapsed = time.time() - start
        print(
            json.dumps(
                {
                    "mode": "guarded-performance",
                    "incumbent_objective": round(incumbent_audit["objective"], 6),
                    "final_objective": round(final_audit["objective"], 6),
                    "improved": final_audit["objective"] < incumbent_audit["objective"] - IMPROVEMENT_TOL,
                    "validation_feasible": final_audit["feasible"],
                    "validation_max_theta": final_audit["max_theta"],
                    "elapsed_seconds": round(elapsed, 3),
                    "solution_path": str(SOLUTION_PATH),
                },
                indent=2,
            )
        )
        cleanup_temp_artifacts()
        return

    build_started = time.time()
    model, vars_by_name, _ = build_model(data, time_limit)
    build_seconds = time.time() - build_started

    solve_started = time.time()
    model.optimize()
    solve_seconds = time.time() - solve_started

    status = str(model.getStatus())
    nsols = int(model.getNSols())
    dualbound = safe_dualbound(model)
    gap = safe_gap(model)
    incumbent_obj = None
    audit = None

    if nsols > 0 and model.getBestSol() is not None:
        incumbent_obj = float(model.getObjVal())
        solution_rows = extract_solution(model, vars_by_name, data)
        write_solution(solution_rows, SOLUTION_PATH)
        audit = audit_solution(data, SOLUTION_PATH)
        if not audit["feasible"]:
            raise RuntimeError(f"Exported solution failed validation: groups={audit['violated_groups']}")
    else:
        raise RuntimeError(f"SCIP ended with status {status} and no incumbent")

    append_log(command, time_limit, build_seconds, solve_seconds, status, nsols, incumbent_obj, dualbound, gap, audit)

    elapsed = time.time() - start
    print(json.dumps(
        {
            "status": status,
            "nsols": nsols,
            "objective": incumbent_obj,
            "dualbound": dualbound,
            "gap": gap,
            "build_seconds": round(build_seconds, 3),
            "solve_seconds": round(solve_seconds, 3),
            "elapsed_seconds": round(elapsed, 3),
            "validation_feasible": audit["feasible"],
            "validation_objective": round(audit["objective"], 6),
            "validation_max_theta": audit["max_theta"],
            "solution_path": str(SOLUTION_PATH),
        },
        indent=2,
    ))
    cleanup_temp_artifacts()


if __name__ == "__main__":
    main()
