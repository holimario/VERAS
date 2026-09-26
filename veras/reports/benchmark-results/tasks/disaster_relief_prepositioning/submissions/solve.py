import csv
import json
import os
import time
from collections import Counter, defaultdict

from pyscipopt import Model, quicksum


DATA_DIR = "/app/data"
SUBMISSIONS_DIR = "/app/submissions"
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")
TEMP_SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.candidate.csv")
MODEL_PATH = os.path.join(SUBMISSIONS_DIR, "model.md")
EXPORT_DECIMALS = 8
VALUE_TOL = 1e-8
IMPROVEMENT_TOL = 1e-6


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_float(value):
    return float(value)


def parse_bool01(value):
    return int(value) == 1


def load_data():
    with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)

    warehouses_raw = read_csv(os.path.join(DATA_DIR, "warehouses.csv"))
    items_raw = read_csv(os.path.join(DATA_DIR, "items.csv"))
    zones_raw = read_csv(os.path.join(DATA_DIR, "zones.csv"))
    lanes_raw = read_csv(os.path.join(DATA_DIR, "lanes.csv"))
    scenarios_raw = read_csv(os.path.join(DATA_DIR, "scenarios.csv"))
    periods_raw = read_csv(os.path.join(DATA_DIR, "periods.csv"))
    fleet_raw = read_csv(os.path.join(DATA_DIR, "fleet_types.csv"))
    warehouse_fleet_raw = read_csv(os.path.join(DATA_DIR, "warehouse_fleet.csv"))
    demands_raw = read_csv(os.path.join(DATA_DIR, "scenario_demands.csv"))
    availability_raw = read_csv(os.path.join(DATA_DIR, "scenario_warehouse_availability.csv"))
    lane_impacts_raw = read_csv(os.path.join(DATA_DIR, "scenario_period_lane_impacts.csv"))
    service_targets_raw = read_csv(os.path.join(DATA_DIR, "period_service_targets.csv"))

    warehouses = {}
    warehouse_ids = []
    for row in warehouses_raw:
        wid = row["warehouse_id"]
        warehouse_ids.append(wid)
        warehouses[wid] = {
            "region": row["region"],
            "open_cost": parse_float(row["open_cost"]),
            "storage_capacity": parse_float(row["storage_capacity"]),
            "outbound_capacity": parse_float(row["outbound_capacity"]),
            "cold_chain": parse_bool01(row["cold_chain"]),
            "seismic_reinforced": parse_bool01(row["seismic_reinforced"]),
            "staff_team": row["staff_team"],
        }

    items = {}
    item_ids = []
    for row in items_raw:
        iid = row["item_id"]
        item_ids.append(iid)
        items[iid] = {
            "unit_volume": parse_float(row["unit_volume"]),
            "load_factor": parse_float(row["load_factor"]),
            "preposition_cost": parse_float(row["preposition_cost"]),
            "emergency_procure_cost": parse_float(row["emergency_procure_cost"]),
            "shortage_penalty": parse_float(row["shortage_penalty"]),
            "cold_chain_required": parse_bool01(row["cold_chain_required"]),
            "max_stock_per_warehouse": parse_float(row["max_stock_per_warehouse"]),
        }

    zones = {}
    zone_ids = []
    for row in zones_raw:
        zid = row["zone_id"]
        zone_ids.append(zid)
        zones[zid] = {
            "district": row["district"],
            "priority_class": row["priority_class"],
            "population": parse_float(row["population"]),
            "water_floor": parse_float(row["water_floor"]),
            "meal_floor": parse_float(row["meal_floor"]),
            "medical_floor": parse_float(row["medical_floor"]),
            "emergency_procure_limit_fraction": parse_float(row["emergency_procure_limit_fraction"]),
            "target_response_hours": parse_float(row["target_response_hours"]),
        }

    periods = []
    for row in periods_raw:
        periods.append(
            {
                "period_id": row["period_id"],
                "start_hour": parse_float(row["start_hour"]),
                "end_hour": parse_float(row["end_hour"]),
                "label": row["label"],
                "delay_penalty_multiplier": parse_float(row["delay_penalty_multiplier"]),
            }
        )
    periods.sort(key=lambda r: r["start_hour"])
    period_ids = [row["period_id"] for row in periods]
    period_pos = {pid: idx for idx, pid in enumerate(period_ids)}

    scenarios = {}
    scenario_ids = []
    for row in scenarios_raw:
        sid = row["scenario_id"]
        scenario_ids.append(sid)
        scenarios[sid] = {
            "probability": parse_float(row["probability"]),
            "disaster_type": row["disaster_type"],
            "procure_caps": {
                "water": parse_float(row["water_procure_cap"]),
                "meal": parse_float(row["meal_procure_cap"]),
                "medical": parse_float(row["medical_procure_cap"]),
            },
        }

    fleet_types = {}
    fleet_ids = []
    for row in fleet_raw:
        fid = row["fleet_type"]
        fleet_ids.append(fid)
        fleet_types[fid] = {
            "capacity_load": parse_float(row["capacity_load"]),
            "cold_chain_capable": parse_bool01(row["cold_chain_capable"]),
            "rough_road_capable": parse_bool01(row["rough_road_capable"]),
            "airlift_capable": parse_bool01(row["airlift_capable"]),
            "fixed_trip_cost": parse_float(row["fixed_trip_cost"]),
            "max_travel_time_hours": parse_float(row["max_travel_time_hours"]),
        }

    lanes = {}
    lane_keys = []
    for row in lanes_raw:
        key = (row["warehouse_id"], row["zone_id"])
        lane_keys.append(key)
        lanes[key] = {
            "distance_km": parse_float(row["distance_km"]),
            "travel_time_hours": parse_float(row["travel_time_hours"]),
            "base_capacity": parse_float(row["base_capacity"]),
            "transport_cost_per_load": parse_float(row["transport_cost_per_load"]),
            "road_class": row["road_class"],
        }

    warehouse_fleet = {}
    for row in warehouse_fleet_raw:
        warehouse_fleet[(row["warehouse_id"], row["period_id"], row["fleet_type"])] = parse_float(row["available_trips"])

    demands = {}
    for row in demands_raw:
        demands[(row["scenario_id"], row["zone_id"], row["item_id"])] = parse_float(row["demand"])

    availability = {}
    for row in availability_raw:
        availability[(row["scenario_id"], row["warehouse_id"])] = {
            "usable_fraction": parse_float(row["usable_fraction"]),
            "outbound_multiplier": parse_float(row["outbound_multiplier"]),
        }

    lane_impacts = {}
    for row in lane_impacts_raw:
        lane_impacts[(row["scenario_id"], row["period_id"], row["warehouse_id"], row["zone_id"])] = {
            "capacity_multiplier": parse_float(row["capacity_multiplier"]),
            "extra_transport_cost_per_load": parse_float(row["extra_transport_cost_per_load"]),
            "travel_time_hours": parse_float(row["travel_time_hours"]),
        }

    service_targets = {}
    for row in service_targets_raw:
        service_targets[(row["priority_class"], row["item_id"], row["period_id"])] = parse_float(
            row["cumulative_fraction_of_final_floor"]
        )

    critical_zones = [z for z in zone_ids if zones[z]["priority_class"] == "critical"]
    noncritical_zones = [z for z in zone_ids if zones[z]["priority_class"] != "critical"]

    inv_ub = {}
    for wid in warehouse_ids:
        for iid in item_ids:
            ub = min(
                items[iid]["max_stock_per_warehouse"],
                warehouses[wid]["storage_capacity"] / items[iid]["unit_volume"],
            )
            if items[iid]["cold_chain_required"] and not warehouses[wid]["cold_chain"]:
                ub = 0.0
            inv_ub[(wid, iid)] = ub

    rough_classes = {"mountain", "bridge", "coastal"}

    def fleet_allowed(road_class, travel_time, fleet_id):
        fleet = fleet_types[fleet_id]
        if road_class in rough_classes and (fleet["rough_road_capable"] or fleet["airlift_capable"]):
            return True
        return travel_time <= fleet["max_travel_time_hours"] + 1e-9

    ship_keys = []
    ship_unit_cost = {}
    ship_unit_load = {}
    ship_ub = {}
    ship_by_cum = defaultdict(list)
    ship_by_stock = defaultdict(list)
    ship_by_outbound = defaultdict(list)
    ship_by_lane = defaultdict(list)
    ship_by_trip = defaultdict(list)
    ship_by_scenario = defaultdict(list)

    for sid in scenario_ids:
        for pid in period_ids:
            period = next(row for row in periods if row["period_id"] == pid)
            for wid, zid in lane_keys:
                impact = lane_impacts[(sid, pid, wid, zid)]
                lane = lanes[(wid, zid)]
                road_class = lane["road_class"]
                travel_time = impact["travel_time_hours"]
                delay_hours = max(0.0, period["start_hour"] + travel_time - zones[zid]["target_response_hours"])
                delay_cost_per_load = (
                    config["late_response_penalty_per_load_hour"]
                    * period["delay_penalty_multiplier"]
                    * delay_hours
                )
                lane_cap = lane["base_capacity"] * impact["capacity_multiplier"]
                for iid in item_ids:
                    demand_ub = demands[(sid, zid, iid)]
                    load_factor = items[iid]["load_factor"]
                    if lane_cap <= 0.0:
                        continue
                    for fid in fleet_ids:
                        if items[iid]["cold_chain_required"] and not fleet_types[fid]["cold_chain_capable"]:
                            continue
                        if not fleet_allowed(road_class, travel_time, fid):
                            continue
                        trip_avail = warehouse_fleet[(wid, pid, fid)]
                        fleet_cap_load = trip_avail * fleet_types[fid]["capacity_load"]
                        if fleet_cap_load <= 0.0:
                            continue
                        stock_cap_units = availability[(sid, wid)]["usable_fraction"] * inv_ub[(wid, iid)]
                        out_cap_units = (
                            warehouses[wid]["outbound_capacity"]
                            * availability[(sid, wid)]["outbound_multiplier"]
                            / load_factor
                        )
                        lane_cap_units = lane_cap / load_factor
                        fleet_cap_units = fleet_cap_load / load_factor
                        ub = min(demand_ub, stock_cap_units, out_cap_units, lane_cap_units, fleet_cap_units)
                        if ub <= VALUE_TOL:
                            continue
                        key = (sid, pid, wid, zid, iid, fid)
                        ship_keys.append(key)
                        ship_unit_load[key] = load_factor
                        unit_cost = load_factor * (
                            lane["transport_cost_per_load"]
                            + impact["extra_transport_cost_per_load"]
                            + delay_cost_per_load
                        )
                        ship_unit_cost[key] = unit_cost
                        ship_ub[key] = ub
                        ship_by_cum[(sid, pid, zid, iid)].append(key)
                        ship_by_stock[(sid, wid, iid)].append(key)
                        ship_by_outbound[(sid, wid)].append(key)
                        ship_by_lane[(sid, pid, wid, zid)].append(key)
                        ship_by_trip[(sid, pid, wid, fid)].append(key)
                        ship_by_scenario[sid].append(key)

    return {
        "config": config,
        "warehouses": warehouses,
        "items": items,
        "zones": zones,
        "periods": periods,
        "period_ids": period_ids,
        "period_pos": period_pos,
        "scenarios": scenarios,
        "scenario_ids": scenario_ids,
        "fleet_types": fleet_types,
        "fleet_ids": fleet_ids,
        "lanes": lanes,
        "lane_keys": lane_keys,
        "warehouse_ids": warehouse_ids,
        "zone_ids": zone_ids,
        "item_ids": item_ids,
        "warehouse_fleet": warehouse_fleet,
        "demands": demands,
        "availability": availability,
        "lane_impacts": lane_impacts,
        "service_targets": service_targets,
        "critical_zones": critical_zones,
        "noncritical_zones": noncritical_zones,
        "inv_ub": inv_ub,
        "ship_keys": ship_keys,
        "ship_unit_cost": ship_unit_cost,
        "ship_unit_load": ship_unit_load,
        "ship_ub": ship_ub,
        "ship_by_cum": ship_by_cum,
        "ship_by_stock": ship_by_stock,
        "ship_by_outbound": ship_by_outbound,
        "ship_by_lane": ship_by_lane,
        "ship_by_trip": ship_by_trip,
        "ship_by_scenario": ship_by_scenario,
    }


def build_model(data, time_limit, fixed_open=None):
    config = data["config"]
    warehouses = data["warehouses"]
    items = data["items"]
    zones = data["zones"]
    periods = data["periods"]
    period_ids = data["period_ids"]
    scenario_ids = data["scenario_ids"]
    fleet_types = data["fleet_types"]
    warehouse_ids = data["warehouse_ids"]
    zone_ids = data["zone_ids"]
    item_ids = data["item_ids"]

    model = Model("disaster_relief_prepositioning")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", max(1.0, time_limit))
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("parallel/maxnthreads", 1)

    y = {}
    for wid in warehouse_ids:
        if fixed_open is None:
            y[wid] = model.addVar(vtype="B", name=f"open[{wid}]")
        else:
            value = 1.0 if wid in fixed_open else 0.0
            y[wid] = model.addVar(lb=value, ub=value, vtype="C", name=f"open_fixed[{wid}]")

    inventory = {}
    for wid in warehouse_ids:
        for iid in item_ids:
            inventory[(wid, iid)] = model.addVar(
                lb=0.0,
                ub=data["inv_ub"][(wid, iid)],
                vtype="C",
                name=f"inventory[{wid},{iid}]",
            )

    ship = {}
    for key in data["ship_keys"]:
        ship[key] = model.addVar(lb=0.0, ub=data["ship_ub"][key], vtype="C", name=f"ship[{','.join(key)}]")

    procure = {}
    unmet = {}
    served = {}
    for sid in scenario_ids:
        for pid in period_ids:
            for zid in zone_ids:
                for iid in item_ids:
                    demand = data["demands"][(sid, zid, iid)]
                    zone_cap = zones[zid]["emergency_procure_limit_fraction"] * demand
                    scen_cap = data["scenarios"][sid]["procure_caps"][iid]
                    procure[(sid, pid, zid, iid)] = model.addVar(
                        lb=0.0,
                        ub=min(zone_cap, scen_cap),
                        vtype="C",
                        name=f"procure[{sid},{pid},{zid},{iid}]",
                    )
                    unmet[(sid, pid, zid, iid)] = model.addVar(
                        lb=0.0,
                        ub=demand,
                        vtype="C",
                        name=f"unmet[{sid},{pid},{zid},{iid}]",
                    )
                    served[(sid, pid, zid, iid)] = model.addVar(
                        lb=0.0,
                        ub=demand,
                        vtype="C",
                        name=f"served[{sid},{pid},{zid},{iid}]",
                    )

    trips_used = {}
    for sid in scenario_ids:
        for period in periods:
            pid = period["period_id"]
            for wid in warehouse_ids:
                for fid in data["fleet_ids"]:
                    trips_used[(sid, pid, wid, fid)] = model.addVar(
                        lb=0.0,
                        ub=data["warehouse_fleet"][(wid, pid, fid)],
                        vtype="C",
                        name=f"trips[{sid},{pid},{wid},{fid}]",
                    )

    eta_ub = 0.0
    for sid in scenario_ids:
        scenario_ub = 0.0
        for zid in zone_ids:
            for iid in item_ids:
                scenario_ub += data["demands"][(sid, zid, iid)] * items[iid]["shortage_penalty"]
        eta_ub = max(eta_ub, scenario_ub)

    eta = model.addVar(lb=0.0, ub=max(1.0, eta_ub), vtype="C", name="eta")
    excess = {sid: model.addVar(lb=0.0, vtype="C", name=f"excess[{sid}]") for sid in scenario_ids}

    first_stage_cost = (
        quicksum(warehouses[wid]["open_cost"] * y[wid] for wid in warehouse_ids)
        + quicksum(items[iid]["preposition_cost"] * inventory[(wid, iid)] for wid in warehouse_ids for iid in item_ids)
    )

    scenario_operating_cost = {}
    for sid in scenario_ids:
        ship_cost_expr = quicksum(data["ship_unit_cost"][key] * ship[key] for key in data["ship_by_scenario"][sid])
        trip_cost_expr = quicksum(
            fleet_types[fid]["fixed_trip_cost"] * trips_used[(sid, pid, wid, fid)]
            for pid in period_ids
            for wid in warehouse_ids
            for fid in data["fleet_ids"]
        )
        procure_cost_expr = quicksum(
            items[iid]["emergency_procure_cost"] * procure[(sid, pid, zid, iid)]
            for pid in period_ids
            for zid in zone_ids
            for iid in item_ids
        )
        unmet_cost_expr = quicksum(
            items[iid]["shortage_penalty"] * unmet[(sid, pid, zid, iid)]
            for pid in period_ids
            for zid in zone_ids
            for iid in item_ids
        )
        scenario_operating_cost[sid] = ship_cost_expr + trip_cost_expr + procure_cost_expr + unmet_cost_expr

    expected_cost = quicksum(
        data["scenarios"][sid]["probability"] * scenario_operating_cost[sid] for sid in scenario_ids
    )
    cvar_cost = eta + (1.0 / (1.0 - config["tail_risk_alpha"])) * quicksum(
        data["scenarios"][sid]["probability"] * excess[sid] for sid in scenario_ids
    )
    model.setObjective(first_stage_cost + expected_cost + config["tail_risk_weight"] * cvar_cost, "minimize")

    model.addCons(quicksum(y[wid] for wid in warehouse_ids) <= config["max_open_warehouses"], name="max_open_warehouses")
    model.addCons(
        quicksum((1.0 if warehouses[wid]["cold_chain"] else 0.0) * y[wid] for wid in warehouse_ids)
        >= config["min_open_cold_chain_warehouses"],
        name="min_open_cold_chain_warehouses",
    )
    model.addCons(first_stage_cost <= config["planning_budget"], name="planning_budget")

    for wid in warehouse_ids:
        model.addCons(
            quicksum(items[iid]["unit_volume"] * inventory[(wid, iid)] for iid in item_ids)
            <= warehouses[wid]["storage_capacity"] * y[wid],
            name=f"storage_capacity[{wid}]",
        )
        for iid in item_ids:
            model.addCons(
                inventory[(wid, iid)] <= data["inv_ub"][(wid, iid)] * y[wid],
                name=f"inventory_open_link[{wid},{iid}]",
            )

    first_period = period_ids[0]
    last_period = period_ids[-1]
    for sid in scenario_ids:
        for zid in zone_ids:
            for iid in item_ids:
                first_deliveries = quicksum(ship[key] for key in data["ship_by_cum"][(sid, first_period, zid, iid)])
                model.addCons(
                    served[(sid, first_period, zid, iid)] == procure[(sid, first_period, zid, iid)] + first_deliveries,
                    name=f"served_init[{sid},{zid},{iid}]",
                )
                for idx in range(1, len(period_ids)):
                    pid = period_ids[idx]
                    prev = period_ids[idx - 1]
                    period_deliveries = quicksum(ship[key] for key in data["ship_by_cum"][(sid, pid, zid, iid)])
                    model.addCons(
                        served[(sid, pid, zid, iid)]
                        == served[(sid, prev, zid, iid)] + procure[(sid, pid, zid, iid)] + period_deliveries,
                        name=f"served_flow[{sid},{pid},{zid},{iid}]",
                    )

                demand = data["demands"][(sid, zid, iid)]
                model.addCons(
                    served[(sid, last_period, zid, iid)]
                    + quicksum(unmet[(sid, pid, zid, iid)] for pid in period_ids)
                    == demand,
                    name=f"demand_balance[{sid},{zid},{iid}]",
                )

                floor = zones[zid][f"{iid}_floor"]
                priority = zones[zid]["priority_class"]
                for pid in period_ids:
                    target_frac = data["service_targets"][(priority, iid, pid)]
                    model.addCons(
                        served[(sid, pid, zid, iid)] >= target_frac * floor * demand,
                        name=f"service_target[{sid},{pid},{zid},{iid}]",
                    )

                model.addCons(
                    quicksum(procure[(sid, pid, zid, iid)] for pid in period_ids)
                    <= zones[zid]["emergency_procure_limit_fraction"] * demand,
                    name=f"zone_procure_cap[{sid},{zid},{iid}]",
                )

        for iid in item_ids:
            model.addCons(
                quicksum(procure[(sid, pid, zid, iid)] for pid in period_ids for zid in zone_ids)
                <= data["scenarios"][sid]["procure_caps"][iid],
                name=f"scenario_procure_cap[{sid},{iid}]",
            )

        for zc in data["critical_zones"]:
            for zn in data["noncritical_zones"]:
                for iid in item_ids:
                    demand_c = data["demands"][(sid, zc, iid)]
                    demand_n = data["demands"][(sid, zn, iid)]
                    rhs = -config["fairness_gap"] * demand_c * demand_n
                    model.addCons(
                        demand_n * served[(sid, last_period, zc, iid)] - demand_c * served[(sid, last_period, zn, iid)] >= rhs,
                        name=f"fairness[{sid},{zc},{zn},{iid}]",
                    )

        for wid in warehouse_ids:
            model.addCons(
                quicksum(ship[key] for key in data["ship_by_stock"][(sid, wid, "water")])
                <= data["availability"][(sid, wid)]["usable_fraction"] * inventory[(wid, "water")],
                name=f"usable_stock[{sid},{wid},water]",
            )
            model.addCons(
                quicksum(ship[key] for key in data["ship_by_stock"][(sid, wid, "meal")])
                <= data["availability"][(sid, wid)]["usable_fraction"] * inventory[(wid, "meal")],
                name=f"usable_stock[{sid},{wid},meal]",
            )
            model.addCons(
                quicksum(ship[key] for key in data["ship_by_stock"][(sid, wid, "medical")])
                <= data["availability"][(sid, wid)]["usable_fraction"] * inventory[(wid, "medical")],
                name=f"usable_stock[{sid},{wid},medical]",
            )
            model.addCons(
                quicksum(data["ship_unit_load"][key] * ship[key] for key in data["ship_by_outbound"][(sid, wid)])
                <= warehouses[wid]["outbound_capacity"] * data["availability"][(sid, wid)]["outbound_multiplier"] * y[wid],
                name=f"outbound_capacity[{sid},{wid}]",
            )

        for pid in period_ids:
            for wid, zid in data["lane_keys"]:
                lane_cap = data["lanes"][(wid, zid)]["base_capacity"] * data["lane_impacts"][(sid, pid, wid, zid)]["capacity_multiplier"]
                model.addCons(
                    quicksum(data["ship_unit_load"][key] * ship[key] for key in data["ship_by_lane"][(sid, pid, wid, zid)])
                    <= lane_cap,
                    name=f"lane_capacity[{sid},{pid},{wid},{zid}]",
                )

            for wid in warehouse_ids:
                for fid in data["fleet_ids"]:
                    model.addCons(
                        trips_used[(sid, pid, wid, fid)] <= data["warehouse_fleet"][(wid, pid, fid)] * y[wid],
                        name=f"trip_availability[{sid},{pid},{wid},{fid}]",
                    )
                    model.addCons(
                        quicksum(data["ship_unit_load"][key] * ship[key] for key in data["ship_by_trip"][(sid, pid, wid, fid)])
                        <= fleet_types[fid]["capacity_load"] * trips_used[(sid, pid, wid, fid)],
                        name=f"trip_capacity[{sid},{pid},{wid},{fid}]",
                    )

        model.addCons(excess[sid] >= scenario_operating_cost[sid] - eta, name=f"cvar_excess[{sid}]")

    return model, {
        "y": y,
        "inventory": inventory,
        "ship": ship,
        "procure": procure,
        "unmet": unmet,
        "served": served,
        "trips_used": trips_used,
        "eta": eta,
        "excess": excess,
        "scenario_operating_cost": scenario_operating_cost,
        "first_stage_cost": first_stage_cost,
        "expected_cost": expected_cost,
        "cvar_cost": cvar_cost,
    }


def safe_sol_value(model, sol, var):
    value = model.getSolVal(sol, var)
    if abs(value) <= VALUE_TOL:
        return 0.0
    return value


def extract_solution(model, vars_dict, data):
    if model.getNSols() <= 0:
        return None

    sol = model.getBestSol()
    warehouse_decisions = {}
    for wid in data["warehouse_ids"]:
        warehouse_decisions[wid] = "open" if safe_sol_value(model, sol, vars_dict["y"][wid]) >= 0.5 else "closed"

    inventory = {}
    for key, var in vars_dict["inventory"].items():
        inventory[key] = max(0.0, safe_sol_value(model, sol, var))

    ship = {}
    for key, var in vars_dict["ship"].items():
        value = max(0.0, safe_sol_value(model, sol, var))
        if value > VALUE_TOL:
            ship[key] = value

    procure = {}
    unmet = {}
    for key, var in vars_dict["procure"].items():
        procure[key] = max(0.0, safe_sol_value(model, sol, var))
    for key, var in vars_dict["unmet"].items():
        unmet[key] = max(0.0, safe_sol_value(model, sol, var))

    stats = {
        "status": str(model.getStatus()),
        "n_solutions": model.getNSols(),
        "objective": model.getObjVal() if model.getNSols() > 0 else None,
    }
    try:
        stats["dualbound"] = model.getDualbound()
    except Exception:
        stats["dualbound"] = None
    try:
        stats["gap"] = model.getGap()
    except Exception:
        stats["gap"] = None

    return {
        "warehouse_decisions": warehouse_decisions,
        "inventory": inventory,
        "ship": ship,
        "procure": procure,
        "unmet": unmet,
        "stats": stats,
    }


def fmt_qty(value):
    clipped = 0.0 if abs(value) <= VALUE_TOL else max(0.0, value)
    return f"{clipped:.{EXPORT_DECIMALS}f}"


def write_solution_csv(solution, data, path):
    fieldnames = [
        "row_type",
        "scenario_id",
        "period_id",
        "warehouse_id",
        "zone_id",
        "item_id",
        "fleet_type",
        "quantity",
        "decision",
    ]
    rows = []

    for wid in data["warehouse_ids"]:
        rows.append(
            {
                "row_type": "warehouse",
                "scenario_id": "",
                "period_id": "",
                "warehouse_id": wid,
                "zone_id": "",
                "item_id": "",
                "fleet_type": "",
                "quantity": "",
                "decision": solution["warehouse_decisions"][wid],
            }
        )

    for wid in data["warehouse_ids"]:
        for iid in data["item_ids"]:
            rows.append(
                {
                    "row_type": "inventory",
                    "scenario_id": "",
                    "period_id": "",
                    "warehouse_id": wid,
                    "zone_id": "",
                    "item_id": iid,
                    "fleet_type": "",
                    "quantity": fmt_qty(solution["inventory"][(wid, iid)]),
                    "decision": "stock",
                }
            )

    for key in sorted(solution["ship"]):
        sid, pid, wid, zid, iid, fid = key
        qty = solution["ship"][key]
        if qty <= VALUE_TOL:
            continue
        rows.append(
            {
                "row_type": "ship",
                "scenario_id": sid,
                "period_id": pid,
                "warehouse_id": wid,
                "zone_id": zid,
                "item_id": iid,
                "fleet_type": fid,
                "quantity": fmt_qty(qty),
                "decision": "ship",
            }
        )

    for sid in data["scenario_ids"]:
        for pid in data["period_ids"]:
            for zid in data["zone_ids"]:
                for iid in data["item_ids"]:
                    rows.append(
                        {
                            "row_type": "procure",
                            "scenario_id": sid,
                            "period_id": pid,
                            "warehouse_id": "",
                            "zone_id": zid,
                            "item_id": iid,
                            "fleet_type": "",
                            "quantity": fmt_qty(solution["procure"][(sid, pid, zid, iid)]),
                            "decision": "procure",
                        }
                    )
                    rows.append(
                        {
                            "row_type": "unmet",
                            "scenario_id": sid,
                            "period_id": pid,
                            "warehouse_id": "",
                            "zone_id": zid,
                            "item_id": iid,
                            "fleet_type": "",
                            "quantity": fmt_qty(solution["unmet"][(sid, pid, zid, iid)]),
                            "decision": "unmet",
                        }
                    )

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_solution_csv(path, data):
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        expected_header = [
            "row_type",
            "scenario_id",
            "period_id",
            "warehouse_id",
            "zone_id",
            "item_id",
            "fleet_type",
            "quantity",
            "decision",
        ]
        if header != expected_header:
            raise ValueError(f"Unexpected solution header: {header}")
        rows = list(reader)

    valid_scenarios = set(data["scenario_ids"])
    valid_periods = set(data["period_ids"])
    valid_warehouses = set(data["warehouse_ids"])
    valid_zones = set(data["zone_ids"])
    valid_items = set(data["item_ids"])
    valid_fleets = set(data["fleet_ids"])

    diagnostics = []
    row_counts = Counter()
    warehouse_rows = {}
    inventory_rows = {}
    procure_rows = {}
    unmet_rows = {}
    ship = defaultdict(float)

    seen_warehouse = set()
    seen_inventory = set()
    seen_procure = set()
    seen_unmet = set()

    def expect_blank(row, fields, row_type):
        for field in fields:
            if row[field] != "":
                diagnostics.append(f"{row_type} row must leave {field} blank.")

    def expect_nonblank(row, fields, row_type):
        for field in fields:
            if row[field] == "":
                diagnostics.append(f"{row_type} row must provide {field}.")

    for row in rows:
        row_type = row["row_type"]
        row_counts[row_type] += 1
        decision = row["decision"]
        quantity_str = row["quantity"]

        if row_type == "warehouse":
            expect_blank(row, ["scenario_id", "period_id", "zone_id", "item_id", "fleet_type", "quantity"], row_type)
            expect_nonblank(row, ["warehouse_id"], row_type)
            wid = row["warehouse_id"]
            if wid not in valid_warehouses:
                diagnostics.append(f"Invalid warehouse_id in warehouse row: {wid}")
            if decision not in {"open", "closed"}:
                diagnostics.append(f"Invalid warehouse decision for {wid}: {decision}")
            if wid in seen_warehouse:
                diagnostics.append(f"Duplicate warehouse row for {wid}")
            seen_warehouse.add(wid)
            warehouse_rows[wid] = decision
            continue

        if quantity_str == "":
            diagnostics.append(f"Non-warehouse row has blank quantity: {row_type}")
            quantity = 0.0
        else:
            quantity = float(quantity_str)
            if quantity < -data["config"]["quantity_tolerance"]:
                diagnostics.append(f"Negative quantity in row type {row_type}")

        if row_type == "inventory":
            expect_blank(row, ["scenario_id", "period_id", "zone_id", "fleet_type"], row_type)
            expect_nonblank(row, ["warehouse_id", "item_id"], row_type)
            if decision != "stock":
                diagnostics.append(f"Inventory row has invalid decision {decision}")
            wid = row["warehouse_id"]
            iid = row["item_id"]
            if wid not in valid_warehouses:
                diagnostics.append(f"Invalid warehouse_id in inventory row: {wid}")
            if iid not in valid_items:
                diagnostics.append(f"Invalid item_id in inventory row: {iid}")
            key = (wid, iid)
            if key in seen_inventory:
                diagnostics.append(f"Duplicate inventory row for {key}")
            seen_inventory.add(key)
            inventory_rows[key] = quantity
        elif row_type == "procure":
            expect_blank(row, ["warehouse_id", "fleet_type"], row_type)
            expect_nonblank(row, ["scenario_id", "period_id", "zone_id", "item_id"], row_type)
            if decision != "procure":
                diagnostics.append(f"Procure row has invalid decision {decision}")
            key = (row["scenario_id"], row["period_id"], row["zone_id"], row["item_id"])
            if row["scenario_id"] not in valid_scenarios:
                diagnostics.append(f"Invalid scenario_id in procure row: {row['scenario_id']}")
            if row["period_id"] not in valid_periods:
                diagnostics.append(f"Invalid period_id in procure row: {row['period_id']}")
            if row["zone_id"] not in valid_zones:
                diagnostics.append(f"Invalid zone_id in procure row: {row['zone_id']}")
            if row["item_id"] not in valid_items:
                diagnostics.append(f"Invalid item_id in procure row: {row['item_id']}")
            if key in seen_procure:
                diagnostics.append(f"Duplicate procure row for {key}")
            seen_procure.add(key)
            procure_rows[key] = quantity
        elif row_type == "unmet":
            expect_blank(row, ["warehouse_id", "fleet_type"], row_type)
            expect_nonblank(row, ["scenario_id", "period_id", "zone_id", "item_id"], row_type)
            if decision != "unmet":
                diagnostics.append(f"Unmet row has invalid decision {decision}")
            key = (row["scenario_id"], row["period_id"], row["zone_id"], row["item_id"])
            if row["scenario_id"] not in valid_scenarios:
                diagnostics.append(f"Invalid scenario_id in unmet row: {row['scenario_id']}")
            if row["period_id"] not in valid_periods:
                diagnostics.append(f"Invalid period_id in unmet row: {row['period_id']}")
            if row["zone_id"] not in valid_zones:
                diagnostics.append(f"Invalid zone_id in unmet row: {row['zone_id']}")
            if row["item_id"] not in valid_items:
                diagnostics.append(f"Invalid item_id in unmet row: {row['item_id']}")
            if key in seen_unmet:
                diagnostics.append(f"Duplicate unmet row for {key}")
            seen_unmet.add(key)
            unmet_rows[key] = quantity
        elif row_type == "ship":
            expect_nonblank(row, ["scenario_id", "period_id", "warehouse_id", "zone_id", "item_id", "fleet_type"], row_type)
            if decision != "ship":
                diagnostics.append(f"Ship row has invalid decision {decision}")
            if row["scenario_id"] not in valid_scenarios:
                diagnostics.append(f"Invalid scenario_id in ship row: {row['scenario_id']}")
            if row["period_id"] not in valid_periods:
                diagnostics.append(f"Invalid period_id in ship row: {row['period_id']}")
            if row["warehouse_id"] not in valid_warehouses:
                diagnostics.append(f"Invalid warehouse_id in ship row: {row['warehouse_id']}")
            if row["zone_id"] not in valid_zones:
                diagnostics.append(f"Invalid zone_id in ship row: {row['zone_id']}")
            if row["item_id"] not in valid_items:
                diagnostics.append(f"Invalid item_id in ship row: {row['item_id']}")
            if row["fleet_type"] not in valid_fleets:
                diagnostics.append(f"Invalid fleet_type in ship row: {row['fleet_type']}")
            key = (
                row["scenario_id"],
                row["period_id"],
                row["warehouse_id"],
                row["zone_id"],
                row["item_id"],
                row["fleet_type"],
            )
            ship[key] += quantity
            if quantity <= data["config"]["quantity_tolerance"]:
                diagnostics.append("Ship rows must have strictly positive quantities.")
        else:
            diagnostics.append(f"Unknown row_type: {row_type}")

    parsed = {
        "warehouse_decisions": {wid: warehouse_rows.get(wid, "closed") for wid in data["warehouse_ids"]},
        "inventory": {(wid, iid): max(0.0, inventory_rows.get((wid, iid), 0.0)) for wid in data["warehouse_ids"] for iid in data["item_ids"]},
        "ship": {key: qty for key, qty in ship.items() if qty > VALUE_TOL},
        "procure": {
            (sid, pid, zid, iid): max(0.0, procure_rows.get((sid, pid, zid, iid), 0.0))
            for sid in data["scenario_ids"]
            for pid in data["period_ids"]
            for zid in data["zone_ids"]
            for iid in data["item_ids"]
        },
        "unmet": {
            (sid, pid, zid, iid): max(0.0, unmet_rows.get((sid, pid, zid, iid), 0.0))
            for sid in data["scenario_ids"]
            for pid in data["period_ids"]
            for zid in data["zone_ids"]
            for iid in data["item_ids"]
        },
        "row_counts": dict(row_counts),
        "rows": rows,
    }

    return parsed, diagnostics


def compute_cvar_details(scenario_costs, probabilities, alpha):
    costs = [(sid, probabilities[sid], scenario_costs[sid]) for sid in scenario_costs]
    if not costs:
        return {"eta": 0.0, "cvar": 0.0, "excess": {}}

    candidate_etas = sorted({cost for _, _, cost in costs})
    best_eta = candidate_etas[0]
    best_value = None
    for eta in candidate_etas:
        value = eta + (1.0 / (1.0 - alpha)) * sum(prob * max(cost - eta, 0.0) for _, prob, cost in costs)
        if best_value is None or value < best_value - 1e-12:
            best_value = value
            best_eta = eta
    excess = {sid: max(cost - best_eta, 0.0) for sid, _, cost in costs}
    return {"eta": best_eta, "cvar": best_value, "excess": excess}


def normalize_residual(lhs, rhs):
    return max(0.0, lhs - rhs) / max(1.0, abs(rhs))


def normalize_eq_residual(lhs, rhs):
    return abs(lhs - rhs) / max(1.0, abs(rhs))


def normalize_lower_bound(target, value):
    return max(0.0, target - value) / max(1.0, abs(target))


def audit_solution_csv(path, data):
    config = data["config"]
    warehouses = data["warehouses"]
    items = data["items"]
    zones = data["zones"]
    fleet_types = data["fleet_types"]
    periods = data["period_ids"]
    scenarios = data["scenario_ids"]
    lane_keys = set(data["lane_keys"])
    ship_keys_allowed = set(data["ship_keys"])

    residuals = []
    parsed, diagnostics = read_solution_csv(path, data)

    for wid in data["warehouse_ids"]:
        if wid not in parsed["warehouse_decisions"]:
            diagnostics.append(f"Missing warehouse row for {wid}")
    for wid in data["warehouse_ids"]:
        for iid in data["item_ids"]:
            if (wid, iid) not in parsed["inventory"]:
                diagnostics.append(f"Missing inventory row for {(wid, iid)}")
    for sid in scenarios:
        for pid in periods:
            for zid in data["zone_ids"]:
                for iid in data["item_ids"]:
                    if (sid, pid, zid, iid) not in parsed["procure"]:
                        diagnostics.append(f"Missing procure row for {(sid, pid, zid, iid)}")
                    if (sid, pid, zid, iid) not in parsed["unmet"]:
                        diagnostics.append(f"Missing unmet row for {(sid, pid, zid, iid)}")

    warehouse_open = {
        wid: 1.0 if parsed["warehouse_decisions"].get(wid) == "open" else 0.0 for wid in data["warehouse_ids"]
    }
    inventory = parsed["inventory"]

    first_stage_cost = sum(warehouses[wid]["open_cost"] * warehouse_open[wid] for wid in data["warehouse_ids"])
    first_stage_cost += sum(items[iid]["preposition_cost"] * inventory[(wid, iid)] for wid in data["warehouse_ids"] for iid in data["item_ids"])

    residuals.append(("max_open_warehouses", normalize_residual(sum(warehouse_open.values()), config["max_open_warehouses"])))
    residuals.append(
        (
            "min_open_cold_chain_warehouses",
            max(
                0.0,
                config["min_open_cold_chain_warehouses"]
                - sum((1.0 if warehouses[wid]["cold_chain"] else 0.0) * warehouse_open[wid] for wid in data["warehouse_ids"]),
            )
            / max(1.0, config["min_open_cold_chain_warehouses"]),
        )
    )
    residuals.append(("planning_budget", normalize_residual(first_stage_cost, config["planning_budget"])))

    for wid in data["warehouse_ids"]:
        storage_used = sum(items[iid]["unit_volume"] * inventory[(wid, iid)] for iid in data["item_ids"])
        residuals.append(
            (f"storage_capacity[{wid}]", normalize_residual(storage_used, warehouses[wid]["storage_capacity"] * warehouse_open[wid]))
        )
        for iid in data["item_ids"]:
            residuals.append(
                (
                    f"inventory_open_link[{wid},{iid}]",
                    normalize_residual(inventory[(wid, iid)], data["inv_ub"][(wid, iid)] * warehouse_open[wid]),
                )
            )

    ship_load_by_lane = defaultdict(float)
    ship_load_by_trip = defaultdict(float)
    ship_qty_by_stock = defaultdict(float)
    ship_load_by_outbound = defaultdict(float)
    ship_qty_by_cum = defaultdict(float)
    scenario_costs = defaultdict(float)

    for key, qty in parsed["ship"].items():
        sid, pid, wid, zid, iid, fid = key
        if key not in ship_keys_allowed:
            diagnostics.append(f"Ship row uses an ineligible arc {key}")
            continue
        if (wid, zid) not in lane_keys:
            diagnostics.append(f"Ship row uses an invalid lane {(wid, zid)}")
            continue
        load = items[iid]["load_factor"] * qty
        ship_load_by_lane[(sid, pid, wid, zid)] += load
        ship_load_by_trip[(sid, pid, wid, fid)] += load
        ship_qty_by_stock[(sid, wid, iid)] += qty
        ship_load_by_outbound[(sid, wid)] += load
        ship_qty_by_cum[(sid, pid, zid, iid)] += qty
        scenario_costs[sid] += data["ship_unit_cost"][key] * qty

    trips_needed = defaultdict(float)
    for sid in scenarios:
        for pid in periods:
            for wid in data["warehouse_ids"]:
                for fid in data["fleet_ids"]:
                    trips_needed[(sid, pid, wid, fid)] = ship_load_by_trip[(sid, pid, wid, fid)] / max(
                        1.0, fleet_types[fid]["capacity_load"]
                    )
                    scenario_costs[sid] += fleet_types[fid]["fixed_trip_cost"] * trips_needed[(sid, pid, wid, fid)]

    served = {}
    for sid in scenarios:
        for zid in data["zone_ids"]:
            for iid in data["item_ids"]:
                cum = 0.0
                for pid in periods:
                    cum += ship_qty_by_cum[(sid, pid, zid, iid)] + parsed["procure"][(sid, pid, zid, iid)]
                    served[(sid, pid, zid, iid)] = cum
                    scenario_costs[sid] += items[iid]["emergency_procure_cost"] * parsed["procure"][(sid, pid, zid, iid)]
                    scenario_costs[sid] += items[iid]["shortage_penalty"] * parsed["unmet"][(sid, pid, zid, iid)]

    for sid in scenarios:
        for wid in data["warehouse_ids"]:
            for iid in data["item_ids"]:
                usable = data["availability"][(sid, wid)]["usable_fraction"] * inventory[(wid, iid)]
                residuals.append(
                    (
                        f"usable_stock[{sid},{wid},{iid}]",
                        normalize_residual(ship_qty_by_stock[(sid, wid, iid)], usable),
                    )
                )
            outbound = warehouses[wid]["outbound_capacity"] * data["availability"][(sid, wid)]["outbound_multiplier"] * warehouse_open[wid]
            residuals.append(
                (f"outbound_capacity[{sid},{wid}]", normalize_residual(ship_load_by_outbound[(sid, wid)], outbound))
            )

        for pid in periods:
            for wid, zid in data["lane_keys"]:
                lane_cap = data["lanes"][(wid, zid)]["base_capacity"] * data["lane_impacts"][(sid, pid, wid, zid)]["capacity_multiplier"]
                residuals.append(
                    (
                        f"lane_capacity[{sid},{pid},{wid},{zid}]",
                        normalize_residual(ship_load_by_lane[(sid, pid, wid, zid)], lane_cap),
                    )
                )
            for wid in data["warehouse_ids"]:
                for fid in data["fleet_ids"]:
                    avail = data["warehouse_fleet"][(wid, pid, fid)] * warehouse_open[wid]
                    residuals.append(
                        (
                            f"trip_availability[{sid},{pid},{wid},{fid}]",
                            normalize_residual(trips_needed[(sid, pid, wid, fid)], avail),
                        )
                    )

        for zid in data["zone_ids"]:
            for iid in data["item_ids"]:
                demand = data["demands"][(sid, zid, iid)]
                final_served = served[(sid, periods[-1], zid, iid)]
                unmet_total = sum(parsed["unmet"][(sid, pid, zid, iid)] for pid in periods)
                residuals.append(
                    (
                        f"demand_balance[{sid},{zid},{iid}]",
                        normalize_eq_residual(final_served + unmet_total, demand),
                    )
                )
                priority = zones[zid]["priority_class"]
                floor = zones[zid][f"{iid}_floor"]
                for pid in periods:
                    target = data["service_targets"][(priority, iid, pid)] * floor * demand
                    residuals.append(
                        (
                            f"service_target[{sid},{pid},{zid},{iid}]",
                            normalize_lower_bound(target, served[(sid, pid, zid, iid)]),
                        )
                    )
                zone_proc_cap = zones[zid]["emergency_procure_limit_fraction"] * demand
                proc_total = sum(parsed["procure"][(sid, pid, zid, iid)] for pid in periods)
                residuals.append(
                    (
                        f"zone_procure_cap[{sid},{zid},{iid}]",
                        normalize_residual(proc_total, zone_proc_cap),
                    )
                )

        for iid in data["item_ids"]:
            scen_proc = sum(parsed["procure"][(sid, pid, zid, iid)] for pid in periods for zid in data["zone_ids"])
            residuals.append(
                (
                    f"scenario_procure_cap[{sid},{iid}]",
                    normalize_residual(scen_proc, data["scenarios"][sid]["procure_caps"][iid]),
                )
            )

        last_period = periods[-1]
        for zc in data["critical_zones"]:
            for zn in data["noncritical_zones"]:
                for iid in data["item_ids"]:
                    demand_c = data["demands"][(sid, zc, iid)]
                    demand_n = data["demands"][(sid, zn, iid)]
                    lhs = demand_n * served[(sid, last_period, zc, iid)] - demand_c * served[(sid, last_period, zn, iid)]
                    rhs = -config["fairness_gap"] * demand_c * demand_n
                    residuals.append((f"fairness[{sid},{zc},{zn},{iid}]", normalize_lower_bound(rhs, lhs)))

    expected_cost = sum(data["scenarios"][sid]["probability"] * scenario_costs[sid] for sid in scenarios)
    cvar_details = compute_cvar_details(
        {sid: scenario_costs[sid] for sid in scenarios},
        {sid: data["scenarios"][sid]["probability"] for sid in scenarios},
        config["tail_risk_alpha"],
    )
    cvar = cvar_details["cvar"]
    total_objective = first_stage_cost + expected_cost + config["tail_risk_weight"] * cvar

    theta = max((value for _, value in residuals), default=0.0)
    violated_groups = sum(1 for _, value in residuals if value > config["quantity_tolerance"])

    return {
        "parsed": parsed,
        "diagnostics": diagnostics,
        "theta": theta,
        "violated_groups": violated_groups,
        "max_integrality_residual": 0.0,
        "artifact_present": os.path.exists(path),
        "row_count": len(parsed["rows"]),
        "row_counts": parsed["row_counts"],
        "warehouse_open_count": int(sum(warehouse_open.values())),
        "first_stage_cost": first_stage_cost,
        "scenario_costs": dict(scenario_costs),
        "served": served,
        "trips_needed": dict(trips_needed),
        "expected_cost": expected_cost,
        "eta": cvar_details["eta"],
        "cvar": cvar,
        "excess": cvar_details["excess"],
        "objective": total_objective,
        "residuals": residuals,
    }


def choose_fallback_open_set(data):
    preferred = ["W_CENTRAL", "W_AIRBASE", "W_PORT", "W_NORTH", "W_EAST", "W_SOUTH", "W_WEST"]
    return [wid for wid in preferred if wid in data["warehouse_ids"]]


def apply_mip_start(model, vars_dict, data, audited):
    parsed = audited["parsed"]
    sol = model.createPartialSol()

    for wid in data["warehouse_ids"]:
        value = 1.0 if parsed["warehouse_decisions"][wid] == "open" else 0.0
        model.setSolVal(sol, vars_dict["y"][wid], value)

    for key, var in vars_dict["inventory"].items():
        value = parsed["inventory"][key]
        if value > VALUE_TOL:
            model.setSolVal(sol, var, value)

    for key, value in parsed["ship"].items():
        model.setSolVal(sol, vars_dict["ship"][key], value)

    for key, var in vars_dict["procure"].items():
        value = parsed["procure"][key]
        if value > VALUE_TOL:
            model.setSolVal(sol, var, value)

    for key, var in vars_dict["unmet"].items():
        value = parsed["unmet"][key]
        if value > VALUE_TOL:
            model.setSolVal(sol, var, value)

    for key, var in vars_dict["served"].items():
        value = audited["served"][key]
        if value > VALUE_TOL:
            model.setSolVal(sol, var, value)

    for key, var in vars_dict["trips_used"].items():
        value = audited["trips_needed"].get(key, 0.0)
        if value > VALUE_TOL:
            model.setSolVal(sol, var, value)

    model.setSolVal(sol, vars_dict["eta"], audited["eta"])
    for sid, var in vars_dict["excess"].items():
        value = audited["excess"][sid]
        if value > VALUE_TOL:
            model.setSolVal(sol, var, value)

    return model.addSol(sol, free=True)


def solve():
    os.makedirs(SUBMISSIONS_DIR, exist_ok=True)
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Required model artifact missing: {MODEL_PATH}")

    start = time.time()
    data = load_data()
    incumbent_audit = None
    incumbent_objective = None
    incumbent_solution = None
    incumbent_solution_stats = None
    incumbent_path_exists = os.path.exists(SOLUTION_PATH)

    if incumbent_path_exists:
        incumbent_audit = audit_solution_csv(SOLUTION_PATH, data)
        incumbent_objective = incumbent_audit["objective"]
        incumbent_solution = incumbent_audit["parsed"]
        incumbent_solution_stats = {
            "status": "disk_incumbent",
            "n_solutions": 1,
            "objective": incumbent_objective,
            "dualbound": None,
            "gap": None,
        }
        if incumbent_audit["diagnostics"] or incumbent_audit["theta"] > data["config"]["quantity_tolerance"]:
            raise RuntimeError(
                f"Existing incumbent on disk failed independent audit: diagnostics={incumbent_audit['diagnostics']}, theta={incumbent_audit['theta']}"
            )

    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is not None:
        total_limit = min(290.0, max(1.0, float(env_limit)))
    else:
        total_limit = 290.0

    reserve = min(40.0, max(20.0, 0.15 * total_limit))
    main_limit = max(10.0, total_limit - reserve)

    model, vars_dict = build_model(data, main_limit)
    mip_start_loaded = False
    if incumbent_audit is not None:
        mip_start_loaded = apply_mip_start(model, vars_dict, data, incumbent_audit)
    model.optimize()
    solution = extract_solution(model, vars_dict, data)
    solve_mode = "main_mip"
    main_stats = {
        "status": str(model.getStatus()),
        "n_solutions": model.getNSols(),
        "objective": solution["stats"]["objective"] if solution is not None else None,
        "dualbound": None,
        "gap": None,
    }
    try:
        main_stats["dualbound"] = model.getDualbound()
    except Exception:
        pass
    try:
        main_stats["gap"] = model.getGap()
    except Exception:
        pass

    polished_solution = None
    if solution is not None:
        elapsed = time.time() - start
        remaining = max(0.0, total_limit - elapsed)
        if remaining >= 10.0:
            incumbent_open = [
                wid for wid in data["warehouse_ids"] if solution["warehouse_decisions"][wid] == "open"
            ]
            polish_model, polish_vars = build_model(data, remaining, fixed_open=incumbent_open)
            polish_model.optimize()
            polished_solution = extract_solution(polish_model, polish_vars, data)
            if polished_solution is not None:
                solve_mode = "main_mip_plus_fixed_open_lp"
                if polished_solution["stats"]["objective"] <= solution["stats"]["objective"] + 1e-7:
                    solution = polished_solution

    if solution is None:
        elapsed = time.time() - start
        remaining = max(1.0, total_limit - elapsed)
        fallback_open = choose_fallback_open_set(data)
        fallback_model, fallback_vars = build_model(data, remaining, fixed_open=fallback_open)
        fallback_model.optimize()
        solution = extract_solution(fallback_model, fallback_vars, data)
        solve_mode = "fallback_fixed_open_lp"
        if solution is None:
            raise RuntimeError("No incumbent solution was found by either the main MIP or the fallback LP.")

    write_solution_csv(solution, data, TEMP_SOLUTION_PATH)
    candidate_audit = audit_solution_csv(TEMP_SOLUTION_PATH, data)
    if candidate_audit["diagnostics"] or candidate_audit["theta"] > data["config"]["quantity_tolerance"]:
        os.remove(TEMP_SOLUTION_PATH)
        raise RuntimeError(
            f"Candidate solution failed independent audit: diagnostics={candidate_audit['diagnostics']}, theta={candidate_audit['theta']}"
        )

    publish_candidate = incumbent_objective is None or (
        candidate_audit["objective"] < incumbent_objective - IMPROVEMENT_TOL
    )
    if publish_candidate:
        os.replace(TEMP_SOLUTION_PATH, SOLUTION_PATH)
        final_audit = candidate_audit
        final_solution_stats = solution["stats"]
    else:
        os.remove(TEMP_SOLUTION_PATH)
        final_audit = incumbent_audit
        final_solution_stats = incumbent_solution_stats

    summary = {
        "incumbent_objective": incumbent_objective,
        "incumbent_audit_theta": incumbent_audit["theta"] if incumbent_audit is not None else None,
        "incumbent_violated_groups": incumbent_audit["violated_groups"] if incumbent_audit is not None else None,
        "mip_start_loaded": mip_start_loaded,
        "solve_mode": solve_mode,
        "status": final_solution_stats["status"],
        "n_solutions": final_solution_stats["n_solutions"],
        "objective": final_solution_stats["objective"],
        "candidate_objective": candidate_audit["objective"],
        "dualbound": main_stats["dualbound"],
        "gap": main_stats["gap"],
        "published_candidate": publish_candidate,
        "audit_theta": final_audit["theta"],
        "audit_violated_groups": final_audit["violated_groups"],
        "audit_objective": final_audit["objective"],
        "candidate_audit_theta": candidate_audit["theta"],
        "candidate_audit_violated_groups": candidate_audit["violated_groups"],
        "elapsed_seconds": time.time() - start,
    }
    with open(os.path.join(SUBMISSIONS_DIR, "solve_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)


if __name__ == "__main__":
    solve()
