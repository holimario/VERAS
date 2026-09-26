#!/usr/bin/env python3

import copy
import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
GAP_LIMIT = 0.0005
DEFAULT_TIME_LIMIT = 300.0
MODEL_HIGH_PURITY_THRESHOLD = 99.9
DEFINITE_HIGH_PURITY_THRESHOLD = 99.99


def load_json(path):
    with path.open() as handle:
        return json.load(handle)


def load_csv(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def as_float(value):
    return float(value)


def as_int(value):
    return int(round(float(value)))


def clean_float(value, digits=8):
    if abs(value) < 1e-10:
        value = 0.0
    return float(f"{value:.{digits}f}")


def clean_int(value):
    return int(round(value))


def parse_time_limit():
    raw = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if not raw:
        return DEFAULT_TIME_LIMIT
    try:
        return max(1.0, min(DEFAULT_TIME_LIMIT, float(raw)))
    except ValueError:
        return DEFAULT_TIME_LIMIT


def build_data():
    config = load_json(DATA_DIR / "config.json")
    tol = as_float(config["numerical_tolerance"])
    hours = list(range(as_int(config["planning_horizon_hours"])))

    sites = {}
    for row in load_csv(DATA_DIR / "sites.csv"):
        site_id = row["site_id"]
        sites[site_id] = {
            "site_id": site_id,
            "region": row["region"],
            "max_modules": as_int(row["max_modules"]),
            "module_capacity_kgph": as_float(row["module_capacity_kgph"]),
            "min_load_fraction": as_float(row["min_load_fraction"]),
            "module_daily_cost": as_float(row["module_daily_cost"]),
            "variable_cost_per_kg": as_float(row["variable_cost_per_kg"]),
            "tank_base_kg": as_float(row["tank_base_kg"]),
            "tank_module_capacity_kg": as_float(row["tank_module_capacity_kg"]),
            "max_tank_modules": as_int(row["max_tank_modules"]),
            "tank_daily_cost": as_float(row["tank_daily_cost"]),
            "initial_inventory_kg": as_float(row["initial_inventory_kg"]),
            "final_min_inventory_kg": as_float(row["final_min_inventory_kg"]),
            "water_limit_m3_per_day": as_float(row["water_limit_m3_per_day"]),
            "startup_limit": as_int(row["startup_limit"]),
            "polishing_cost_per_kg": as_float(row["polishing_cost_per_kg"]),
            "polishing_daily_cost": as_float(row["polishing_daily_cost"]),
        }

    customers = {}
    for row in load_csv(DATA_DIR / "customers.csv"):
        customer_id = row["customer_id"]
        customers[customer_id] = {
            "customer_id": customer_id,
            "segment": row["segment"],
            "region": row["region"],
            "purity_required_pct": as_float(row["purity_required_pct"]),
            "service_floor_fraction": as_float(row["service_floor_fraction"]),
            "max_hourly_shortfall_fraction": as_float(row["max_hourly_shortfall_fraction"]),
            "shortfall_cost_per_kg": as_float(row["shortfall_cost_per_kg"]),
            "priority_weight": as_float(row["priority_weight"]),
        }

    lanes = {}
    for row in load_csv(DATA_DIR / "lanes.csv"):
        lane_id = row["lane_id"]
        lanes[lane_id] = {
            "lane_id": lane_id,
            "origin_site": row["origin_site"],
            "customer_id": row["customer_id"],
            "distance_km": as_float(row["distance_km"]),
            "kg_per_trip": as_float(row["kg_per_trip"]),
            "base_max_trips_per_hour": as_int(row["base_max_trips_per_hour"]),
            "driver_hours_per_trip": as_float(row["driver_hours_per_trip"]),
            "cost_per_trip": as_float(row["cost_per_trip"]),
            "hazmat_window_start": as_int(row["hazmat_window_start"]),
            "hazmat_window_end": as_int(row["hazmat_window_end"]),
        }

    scenarios = {}
    for row in load_csv(DATA_DIR / "scenarios.csv"):
        scenario_id = row["scenario_id"]
        scenarios[scenario_id] = {
            "scenario_id": scenario_id,
            "probability": as_float(row["probability"]),
            "description": row["description"],
            "grid_price_multiplier": as_float(row["grid_price_multiplier"]),
            "emission_cap_kg": as_float(row["emission_cap_kg"]),
            "driver_hours_cap": as_float(row["driver_hours_cap"]),
        }

    forecast = {}
    for row in load_csv(DATA_DIR / "hourly_forecast.csv"):
        forecast[(row["site_id"], as_int(row["hour"]))] = {
            "renewable_mwh": as_float(row["renewable_mwh"]),
            "grid_price_per_mwh": as_float(row["grid_price_per_mwh"]),
            "grid_emission_kg_per_mwh": as_float(row["grid_emission_kg_per_mwh"]),
        }

    scenario_site = {}
    water_multiplier = {}
    for row in load_csv(DATA_DIR / "scenario_site_hourly.csv"):
        key = (row["scenario_id"], row["site_id"], as_int(row["hour"]))
        scenario_site[key] = {
            "renewable_multiplier": as_float(row["renewable_multiplier"]),
            "module_capacity_multiplier": as_float(row["module_capacity_multiplier"]),
            "water_limit_multiplier": as_float(row["water_limit_multiplier"]),
            "grid_cap_mwh": as_float(row["grid_cap_mwh"]),
        }
        ws_key = (row["scenario_id"], row["site_id"])
        value = scenario_site[key]["water_limit_multiplier"]
        prior = water_multiplier.get(ws_key)
        if prior is None:
            water_multiplier[ws_key] = value
        elif abs(prior - value) > tol:
            raise ValueError(f"Water multiplier varies within scenario-site {ws_key}")

    scenario_demand = {}
    daily_demand = defaultdict(float)
    for row in load_csv(DATA_DIR / "scenario_customer_hourly_demand.csv"):
        key = (row["scenario_id"], row["customer_id"], as_int(row["hour"]))
        demand = as_float(row["demand_kg"])
        scenario_demand[key] = demand
        daily_demand[(row["scenario_id"], row["customer_id"])] += demand

    scenario_lane = {}
    for row in load_csv(DATA_DIR / "scenario_lane_hourly.csv"):
        scenario_lane[(row["scenario_id"], row["lane_id"], as_int(row["hour"]))] = {
            "max_trips": as_int(row["max_trips"]),
            "travel_time_multiplier": as_float(row["travel_time_multiplier"]),
            "closed": as_int(row["closed"]),
        }

    base_demand = {
        (row["customer_id"], as_int(row["hour"])): as_float(row["base_demand_kg"])
        for row in load_csv(DATA_DIR / "base_customer_hourly_demand.csv")
    }

    site_ids = sorted(sites)
    customer_ids = sorted(customers)
    lane_ids = sorted(lanes)
    scenario_ids = sorted(scenarios)

    probability_sum = sum(scenarios[w]["probability"] for w in scenario_ids)
    if abs(probability_sum - 1.0) > 1e-8:
        raise ValueError(f"Scenario probabilities sum to {probability_sum}, not 1")

    high_purity_customers = {
        c for c in customer_ids if customers[c]["purity_required_pct"] >= MODEL_HIGH_PURITY_THRESHOLD
    }
    definite_high_purity_customers = {
        c for c in customer_ids if customers[c]["purity_required_pct"] >= DEFINITE_HIGH_PURITY_THRESHOLD
    }

    lanes_by_site = defaultdict(list)
    lanes_by_customer = defaultdict(list)
    lanes_by_site_high = defaultdict(list)
    lanes_by_site_low = defaultdict(list)
    for lane_id, lane in lanes.items():
        lanes_by_site[lane["origin_site"]].append(lane_id)
        lanes_by_customer[lane["customer_id"]].append(lane_id)
        if lane["customer_id"] in high_purity_customers:
            lanes_by_site_high[lane["origin_site"]].append(lane_id)
        else:
            lanes_by_site_low[lane["origin_site"]].append(lane_id)

    max_polish = {}
    for site_id, site in sites.items():
        max_polish[site_id] = site["initial_inventory_kg"] + len(hours) * site["max_modules"] * site["module_capacity_kgph"]

    return {
        "config": config,
        "tol": tol,
        "hours": hours,
        "sites": sites,
        "customers": customers,
        "lanes": lanes,
        "scenarios": scenarios,
        "forecast": forecast,
        "scenario_site": scenario_site,
        "scenario_demand": scenario_demand,
        "scenario_lane": scenario_lane,
        "base_demand": base_demand,
        "daily_demand": dict(daily_demand),
        "site_ids": site_ids,
        "customer_ids": customer_ids,
        "lane_ids": lane_ids,
        "scenario_ids": scenario_ids,
        "high_purity_customers": high_purity_customers,
        "definite_high_purity_customers": definite_high_purity_customers,
        "lanes_by_site": dict(lanes_by_site),
        "lanes_by_customer": dict(lanes_by_customer),
        "lanes_by_site_high": dict(lanes_by_site_high),
        "lanes_by_site_low": dict(lanes_by_site_low),
        "water_multiplier": water_multiplier,
        "max_polish": max_polish,
    }


def build_model(data, time_limit):
    config = data["config"]
    hours = data["hours"]
    site_ids = data["site_ids"]
    lane_ids = data["lane_ids"]
    customer_ids = data["customer_ids"]
    scenario_ids = data["scenario_ids"]
    sites = data["sites"]
    lanes = data["lanes"]
    customers = data["customers"]
    scenarios = data["scenarios"]
    forecast = data["forecast"]
    scenario_site = data["scenario_site"]
    scenario_demand = data["scenario_demand"]
    scenario_lane = data["scenario_lane"]

    model = Model("green_hydrogen_hub_resilience")
    model.setParam("limits/gap", GAP_LIMIT)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    active = {}
    modules = {}
    tank_modules = {}
    polishing_active = {}
    for site_id in site_ids:
        site = sites[site_id]
        active[site_id] = model.addVar(vtype="B", name=f"active[{site_id}]")
        modules[site_id] = model.addVar(vtype="I", lb=0, ub=site["max_modules"], name=f"modules[{site_id}]")
        tank_modules[site_id] = model.addVar(vtype="I", lb=0, ub=site["max_tank_modules"], name=f"tank[{site_id}]")
        polishing_active[site_id] = model.addVar(vtype="B", name=f"polishing[{site_id}]")

    online = {}
    startup = {}
    production = {}
    renewable = {}
    grid = {}
    polished = {}
    unpolished_inventory = {}
    polished_inventory = {}
    vent_unpolished = {}
    vent_polished = {}
    trips = {}
    delivered = {}
    shortfall = {}
    excess = {}

    for scenario_id in scenario_ids:
        for site_id in site_ids:
            site = sites[site_id]
            for hour in hours:
                key = (scenario_id, site_id, hour)
                online[key] = model.addVar(vtype="I", lb=0, ub=site["max_modules"], name=f"on[{scenario_id},{site_id},{hour}]")
                startup[key] = model.addVar(vtype="I", lb=0, ub=site["max_modules"], name=f"start[{scenario_id},{site_id},{hour}]")
                production[key] = model.addVar(lb=0.0, name=f"prod[{scenario_id},{site_id},{hour}]")
                renewable[key] = model.addVar(lb=0.0, name=f"ren[{scenario_id},{site_id},{hour}]")
                grid[key] = model.addVar(lb=0.0, name=f"grid[{scenario_id},{site_id},{hour}]")
                polished[key] = model.addVar(lb=0.0, name=f"pol[{scenario_id},{site_id},{hour}]")
                unpolished_inventory[key] = model.addVar(lb=0.0, name=f"uinv[{scenario_id},{site_id},{hour}]")
                polished_inventory[key] = model.addVar(lb=0.0, name=f"pinv[{scenario_id},{site_id},{hour}]")
                vent_unpolished[key] = model.addVar(lb=0.0, name=f"ventu[{scenario_id},{site_id},{hour}]")
                vent_polished[key] = model.addVar(lb=0.0, name=f"ventp[{scenario_id},{site_id},{hour}]")
        for lane_id in lane_ids:
            lane = lanes[lane_id]
            for hour in hours:
                lane_key = (scenario_id, lane_id, hour)
                cap = scenario_lane[lane_key]["max_trips"]
                trips[lane_key] = model.addVar(vtype="I", lb=0, ub=cap, name=f"trip[{scenario_id},{lane_id},{hour}]")
                delivered[lane_key] = model.addVar(lb=0.0, ub=lane["kg_per_trip"] * cap, name=f"del[{scenario_id},{lane_id},{hour}]")
        for customer_id in customer_ids:
            for hour in hours:
                shortfall[(scenario_id, customer_id, hour)] = model.addVar(lb=0.0, name=f"short[{scenario_id},{customer_id},{hour}]")
        excess[scenario_id] = model.addVar(lb=0.0, name=f"excess[{scenario_id}]")

    eta = model.addVar(lb=-model.infinity(), name="eta")

    for site_id in site_ids:
        site = sites[site_id]
        model.addCons(modules[site_id] <= site["max_modules"] * active[site_id], name=f"module_select[{site_id}]")
        model.addCons(
            tank_modules[site_id] <= site["max_tank_modules"] * active[site_id],
            name=f"tank_select[{site_id}]",
        )
        model.addCons(polishing_active[site_id] <= active[site_id], name=f"polish_select[{site_id}]")

    operating_cost_expr = {}
    for scenario_id in scenario_ids:
        scenario = scenarios[scenario_id]
        for site_id in site_ids:
            site = sites[site_id]
            model.addCons(
                quicksum(startup[(scenario_id, site_id, hour)] for hour in hours) <= site["startup_limit"],
                name=f"startup_limit[{scenario_id},{site_id}]",
            )
            model.addCons(
                quicksum(config["water_m3_per_kg_h2"] * production[(scenario_id, site_id, hour)] for hour in hours)
                <= site["water_limit_m3_per_day"] * data["water_multiplier"][(scenario_id, site_id)],
                name=f"water[{scenario_id},{site_id}]",
            )
            for hour in hours:
                key = (scenario_id, site_id, hour)
                sh = scenario_site[key]
                fc = forecast[(site_id, hour)]
                low_delivery = quicksum(
                    delivered[(scenario_id, lane_id, hour)] for lane_id in data["lanes_by_site_low"].get(site_id, [])
                )
                high_delivery = quicksum(
                    delivered[(scenario_id, lane_id, hour)] for lane_id in data["lanes_by_site_high"].get(site_id, [])
                )

                model.addCons(online[key] <= modules[site_id], name=f"online[{scenario_id},{site_id},{hour}]")
                if hour == 0:
                    model.addCons(startup[key] >= online[key], name=f"startup0[{scenario_id},{site_id}]")
                else:
                    prev = (scenario_id, site_id, hour - 1)
                    model.addCons(startup[key] >= online[key] - online[prev], name=f"startup[{scenario_id},{site_id},{hour}]")

                cap = site["module_capacity_kgph"] * sh["module_capacity_multiplier"]
                model.addCons(production[key] <= cap * online[key], name=f"produb[{scenario_id},{site_id},{hour}]")
                model.addCons(
                    production[key] >= site["min_load_fraction"] * cap * online[key],
                    name=f"prodlb[{scenario_id},{site_id},{hour}]",
                )
                model.addCons(
                    config["energy_mwh_per_kg_h2"] * production[key]
                    + config["polishing_mwh_per_kg"] * polished[key]
                    <= renewable[key] + grid[key],
                    name=f"energy[{scenario_id},{site_id},{hour}]",
                )
                model.addCons(
                    renewable[key] <= fc["renewable_mwh"] * sh["renewable_multiplier"],
                    name=f"rencap[{scenario_id},{site_id},{hour}]",
                )
                model.addCons(grid[key] <= sh["grid_cap_mwh"], name=f"gridcap[{scenario_id},{site_id},{hour}]")
                model.addCons(
                    polished[key] <= data["max_polish"][site_id] * polishing_active[site_id],
                    name=f"polcap[{scenario_id},{site_id},{hour}]",
                )

                if hour == 0:
                    model.addCons(
                        unpolished_inventory[key]
                        == site["initial_inventory_kg"] + production[key] - polished[key] - low_delivery - vent_unpolished[key],
                        name=f"uinv0[{scenario_id},{site_id}]",
                    )
                    model.addCons(
                        polished_inventory[key] == polished[key] - high_delivery - vent_polished[key],
                        name=f"pinv0[{scenario_id},{site_id}]",
                    )
                else:
                    prev = (scenario_id, site_id, hour - 1)
                    model.addCons(
                        unpolished_inventory[key]
                        == unpolished_inventory[prev] + production[key] - polished[key] - low_delivery - vent_unpolished[key],
                        name=f"uinv[{scenario_id},{site_id},{hour}]",
                    )
                    model.addCons(
                        polished_inventory[key]
                        == polished_inventory[prev] + polished[key] - high_delivery - vent_polished[key],
                        name=f"pinv[{scenario_id},{site_id},{hour}]",
                    )

                model.addCons(
                    unpolished_inventory[key] + polished_inventory[key]
                    <= site["tank_base_kg"] + site["tank_module_capacity_kg"] * tank_modules[site_id],
                    name=f"storage[{scenario_id},{site_id},{hour}]",
                )

            final_key = (scenario_id, site_id, hours[-1])
            model.addCons(
                unpolished_inventory[final_key] + polished_inventory[final_key] >= site["final_min_inventory_kg"],
                name=f"final[{scenario_id},{site_id}]",
            )

        for lane_id in lane_ids:
            lane = lanes[lane_id]
            origin = lane["origin_site"]
            for hour in hours:
                lane_key = (scenario_id, lane_id, hour)
                model.addCons(
                    trips[lane_key] <= scenario_lane[lane_key]["max_trips"] * active[origin],
                    name=f"tripactive[{scenario_id},{lane_id},{hour}]",
                )
                model.addCons(
                    delivered[lane_key] <= lane["kg_per_trip"] * trips[lane_key],
                    name=f"payload[{scenario_id},{lane_id},{hour}]",
                )

        for hour in hours:
            model.addCons(
                quicksum(
                    lanes[lane_id]["driver_hours_per_trip"]
                    * scenario_lane[(scenario_id, lane_id, hour)]["travel_time_multiplier"]
                    * trips[(scenario_id, lane_id, hour)]
                    for lane_id in lane_ids
                )
                <= scenario["driver_hours_cap"],
                name=f"drivers[{scenario_id},{hour}]",
            )
            model.addCons(
                quicksum(
                    unpolished_inventory[(scenario_id, site_id, hour)] + polished_inventory[(scenario_id, site_id, hour)]
                    for site_id in site_ids
                )
                >= config["system_min_reserve_kg"],
                name=f"reserve[{scenario_id},{hour}]",
            )

        for customer_id in customer_ids:
            customer = customers[customer_id]
            model.addCons(
                quicksum(shortfall[(scenario_id, customer_id, hour)] for hour in hours)
                <= (1.0 - customer["service_floor_fraction"]) * data["daily_demand"][(scenario_id, customer_id)],
                name=f"service[{scenario_id},{customer_id}]",
            )
            for hour in hours:
                demand = scenario_demand[(scenario_id, customer_id, hour)]
                model.addCons(
                    quicksum(delivered[(scenario_id, lane_id, hour)] for lane_id in data["lanes_by_customer"][customer_id])
                    + shortfall[(scenario_id, customer_id, hour)]
                    == demand,
                    name=f"demand[{scenario_id},{customer_id},{hour}]",
                )
                model.addCons(
                    shortfall[(scenario_id, customer_id, hour)]
                    <= customer["max_hourly_shortfall_fraction"] * demand,
                    name=f"shortcap[{scenario_id},{customer_id},{hour}]",
                )

        model.addCons(
            quicksum(
                forecast[(site_id, hour)]["grid_emission_kg_per_mwh"] * grid[(scenario_id, site_id, hour)]
                for site_id in site_ids
                for hour in hours
            )
            <= scenario["emission_cap_kg"],
            name=f"emissions[{scenario_id}]",
        )

        operating_cost_expr[scenario_id] = (
            quicksum(
                sites[site_id]["variable_cost_per_kg"] * production[(scenario_id, site_id, hour)]
                + config["water_cost_per_m3"] * config["water_m3_per_kg_h2"] * production[(scenario_id, site_id, hour)]
                + config["startup_cost_per_module"] * startup[(scenario_id, site_id, hour)]
                + sites[site_id]["polishing_cost_per_kg"] * polished[(scenario_id, site_id, hour)]
                + config["vent_penalty_per_kg"]
                * (vent_unpolished[(scenario_id, site_id, hour)] + vent_polished[(scenario_id, site_id, hour)])
                + scenario["grid_price_multiplier"]
                * forecast[(site_id, hour)]["grid_price_per_mwh"]
                * grid[(scenario_id, site_id, hour)]
                + config["emission_cost_per_kg"]
                * forecast[(site_id, hour)]["grid_emission_kg_per_mwh"]
                * grid[(scenario_id, site_id, hour)]
                for site_id in site_ids
                for hour in hours
            )
            + quicksum(
                lanes[lane_id]["cost_per_trip"] * trips[(scenario_id, lane_id, hour)]
                for lane_id in lane_ids
                for hour in hours
            )
            + quicksum(
                customers[customer_id]["shortfall_cost_per_kg"]
                * customers[customer_id]["priority_weight"]
                * shortfall[(scenario_id, customer_id, hour)]
                for customer_id in customer_ids
                for hour in hours
            )
        )
        model.addCons(excess[scenario_id] >= operating_cost_expr[scenario_id] - eta, name=f"cvar[{scenario_id}]")

    infra_cost = quicksum(
        sites[site_id]["module_daily_cost"] * modules[site_id]
        + sites[site_id]["tank_daily_cost"] * tank_modules[site_id]
        + sites[site_id]["polishing_daily_cost"] * polishing_active[site_id]
        for site_id in site_ids
    )
    expected_cost = quicksum(
        scenarios[scenario_id]["probability"] * operating_cost_expr[scenario_id] for scenario_id in scenario_ids
    )
    cvar_cost = eta + (1.0 / (1.0 - config["tail_risk_alpha"])) * quicksum(
        scenarios[scenario_id]["probability"] * excess[scenario_id] for scenario_id in scenario_ids
    )
    model.setObjective(infra_cost + expected_cost + config["tail_risk_weight"] * cvar_cost, "minimize")

    return model, {
        "active": active,
        "modules": modules,
        "tank_modules": tank_modules,
        "polishing_active": polishing_active,
        "online": online,
        "startup": startup,
        "production": production,
        "renewable": renewable,
        "grid": grid,
        "polished": polished,
        "unpolished_inventory": unpolished_inventory,
        "polished_inventory": polished_inventory,
        "vent_unpolished": vent_unpolished,
        "vent_polished": vent_polished,
        "trips": trips,
        "delivered": delivered,
        "shortfall": shortfall,
        "eta": eta,
        "excess": excess,
    }


def extract_solution(data, model, vars_dict):
    tol = data["tol"]
    hours = data["hours"]
    site_ids = data["site_ids"]
    scenario_ids = data["scenario_ids"]
    lane_ids = data["lane_ids"]
    customer_ids = data["customer_ids"]
    sol = model.getBestSol()

    infrastructure = []
    for site_id in site_ids:
        infrastructure.append(
            {
                "site_id": site_id,
                "active": bool(round(model.getSolVal(sol, vars_dict["active"][site_id]))),
                "electrolyzer_modules": clean_int(model.getSolVal(sol, vars_dict["modules"][site_id])),
                "tank_modules": clean_int(model.getSolVal(sol, vars_dict["tank_modules"][site_id])),
                "polishing_active": bool(round(model.getSolVal(sol, vars_dict["polishing_active"][site_id]))),
            }
        )

    operations = []
    for scenario_id in scenario_ids:
        for hour in hours:
            for site_id in site_ids:
                key = (scenario_id, site_id, hour)
                production_value = model.getSolVal(sol, vars_dict["production"][key])
                storage_value = model.getSolVal(sol, vars_dict["unpolished_inventory"][key]) + model.getSolVal(
                    sol, vars_dict["polished_inventory"][key]
                )
                vent_value = model.getSolVal(sol, vars_dict["vent_unpolished"][key]) + model.getSolVal(
                    sol, vars_dict["vent_polished"][key]
                )
                operations.append(
                    {
                        "scenario_id": scenario_id,
                        "hour": hour,
                        "site_id": site_id,
                        "online_modules": clean_int(model.getSolVal(sol, vars_dict["online"][key])),
                        "startup_modules": clean_int(model.getSolVal(sol, vars_dict["startup"][key])),
                        "production_kg": clean_float(production_value),
                        "renewable_mwh": clean_float(model.getSolVal(sol, vars_dict["renewable"][key])),
                        "grid_mwh": clean_float(model.getSolVal(sol, vars_dict["grid"][key])),
                        "storage_end_kg": clean_float(storage_value),
                        "vent_kg": clean_float(vent_value),
                        "water_m3": clean_float(data["config"]["water_m3_per_kg_h2"] * production_value),
                        "polishing_kg": clean_float(model.getSolVal(sol, vars_dict["polished"][key])),
                    }
                )

    deliveries = []
    for scenario_id in scenario_ids:
        for hour in hours:
            for lane_id in lane_ids:
                key = (scenario_id, lane_id, hour)
                trip_value = model.getSolVal(sol, vars_dict["trips"][key])
                kg_value = model.getSolVal(sol, vars_dict["delivered"][key])
                if abs(trip_value) <= tol and abs(kg_value) <= tol:
                    continue
                deliveries.append(
                    {
                        "scenario_id": scenario_id,
                        "hour": hour,
                        "lane_id": lane_id,
                        "kg": clean_float(kg_value),
                        "trips": clean_int(trip_value),
                    }
                )

    shortfalls = []
    for scenario_id in scenario_ids:
        for hour in hours:
            for customer_id in customer_ids:
                key = (scenario_id, customer_id, hour)
                shortfalls.append(
                    {
                        "scenario_id": scenario_id,
                        "hour": hour,
                        "customer_id": customer_id,
                        "kg": clean_float(model.getSolVal(sol, vars_dict["shortfall"][key])),
                    }
                )

    return {
        "infrastructure": infrastructure,
        "operations": operations,
        "deliveries": deliveries,
        "shortfalls": shortfalls,
    }


def compute_cvar(prob_cost_pairs, alpha):
    best = math.inf
    best_eta = None
    for eta in sorted({cost for _, cost in prob_cost_pairs}):
        value = eta + sum(prob * max(cost - eta, 0.0) for prob, cost in prob_cost_pairs) / (1.0 - alpha)
        if value < best:
            best = value
            best_eta = eta
    return best, best_eta


def load_solution_from_disk():
    if not SOLUTION_PATH.exists():
        return None
    return load_json(SOLUTION_PATH)


def build_solution_maps(data, solution):
    site_ids = data["site_ids"]
    scenario_ids = data["scenario_ids"]
    customer_ids = data["customer_ids"]
    lane_ids = data["lane_ids"]
    hours = data["hours"]

    infra = {row["site_id"]: row for row in solution["infrastructure"]}
    ops = {(row["scenario_id"], row["hour"], row["site_id"]): row for row in solution["operations"]}
    deliveries = {(row["scenario_id"], row["hour"], row["lane_id"]): row for row in solution["deliveries"]}
    shortfalls = {(row["scenario_id"], row["hour"], row["customer_id"]): row for row in solution["shortfalls"]}

    for site_id in site_ids:
        infra.setdefault(
            site_id,
            {
                "site_id": site_id,
                "active": False,
                "electrolyzer_modules": 0,
                "tank_modules": 0,
                "polishing_active": False,
            },
        )
    for scenario_id in scenario_ids:
        for hour in hours:
            for site_id in site_ids:
                ops.setdefault(
                    (scenario_id, hour, site_id),
                    {
                        "scenario_id": scenario_id,
                        "hour": hour,
                        "site_id": site_id,
                        "online_modules": 0,
                        "startup_modules": 0,
                        "production_kg": 0.0,
                        "renewable_mwh": 0.0,
                        "grid_mwh": 0.0,
                        "storage_end_kg": 0.0,
                        "vent_kg": 0.0,
                        "water_m3": 0.0,
                        "polishing_kg": 0.0,
                    },
                )
            for lane_id in lane_ids:
                deliveries.setdefault(
                    (scenario_id, hour, lane_id),
                    {"scenario_id": scenario_id, "hour": hour, "lane_id": lane_id, "kg": 0.0, "trips": 0},
                )
            for customer_id in customer_ids:
                shortfalls.setdefault(
                    (scenario_id, hour, customer_id),
                    {"scenario_id": scenario_id, "hour": hour, "customer_id": customer_id, "kg": 0.0},
                )
    return infra, ops, deliveries, shortfalls


def reconstruct_inventory_split(data, solution):
    sites = data["sites"]
    lanes = data["lanes"]
    scenario_ids = data["scenario_ids"]
    site_ids = data["site_ids"]
    hours = data["hours"]
    tol = data["tol"]
    _, ops, deliveries, _ = build_solution_maps(data, solution)

    polished_inventory = {}
    unpolished_inventory = {}
    vent_polished = {}
    vent_unpolished = {}
    outbound_high = defaultdict(float)
    outbound_low = defaultdict(float)
    for (scenario_id, hour, lane_id), row in deliveries.items():
        lane = lanes[lane_id]
        key = (scenario_id, hour, lane["origin_site"])
        if lane["customer_id"] in data["high_purity_customers"]:
            outbound_high[key] += float(row["kg"])
        else:
            outbound_low[key] += float(row["kg"])

    for scenario_id in scenario_ids:
        for site_id in site_ids:
            prev_polished = 0.0
            for hour in hours:
                row = ops[(scenario_id, hour, site_id)]
                storage = float(row["storage_end_kg"])
                vent = float(row["vent_kg"])
                polished = float(row["polishing_kg"])
                high_delivery = outbound_high[(scenario_id, hour, site_id)]
                if hour == 0:
                    lower = max(0.0, polished - high_delivery - vent)
                    upper = min(storage, polished - high_delivery)
                else:
                    lower = max(0.0, prev_polished + polished - high_delivery - vent)
                    upper = min(storage, prev_polished + polished - high_delivery)
                if lower > upper + tol:
                    return None
                polished_end = max(lower, upper)
                vent_p = (polished - high_delivery - polished_end) if hour == 0 else (
                    prev_polished + polished - high_delivery - polished_end
                )
                vent_u = vent - vent_p
                unpolished_end = storage - polished_end
                if min(polished_end, vent_p, vent_u, unpolished_end) < -tol:
                    return None
                polished_end = max(0.0, polished_end)
                vent_p = max(0.0, vent_p)
                vent_u = max(0.0, vent_u)
                unpolished_end = max(0.0, unpolished_end)
                key = (scenario_id, site_id, hour)
                polished_inventory[key] = polished_end
                unpolished_inventory[key] = unpolished_end
                vent_polished[key] = vent_p
                vent_unpolished[key] = vent_u
                prev_polished = polished_end

    return {
        "polished_inventory": polished_inventory,
        "unpolished_inventory": unpolished_inventory,
        "vent_polished": vent_polished,
        "vent_unpolished": vent_unpolished,
    }


def add_incumbent_start(model, vars_dict, data, solution, incumbent_validation):
    split = reconstruct_inventory_split(data, solution)
    if split is None:
        return False

    infra, ops, deliveries, shortfalls = build_solution_maps(data, solution)
    sol = model.createSol()

    for site_id, row in infra.items():
        model.setSolVal(sol, vars_dict["active"][site_id], 1 if row["active"] else 0)
        model.setSolVal(sol, vars_dict["modules"][site_id], int(row["electrolyzer_modules"]))
        model.setSolVal(sol, vars_dict["tank_modules"][site_id], int(row["tank_modules"]))
        model.setSolVal(sol, vars_dict["polishing_active"][site_id], 1 if row["polishing_active"] else 0)

    for (scenario_id, hour, site_id), row in ops.items():
        key = (scenario_id, site_id, hour)
        model.setSolVal(sol, vars_dict["online"][key], int(row["online_modules"]))
        model.setSolVal(sol, vars_dict["startup"][key], int(row["startup_modules"]))
        model.setSolVal(sol, vars_dict["production"][key], float(row["production_kg"]))
        model.setSolVal(sol, vars_dict["renewable"][key], float(row["renewable_mwh"]))
        model.setSolVal(sol, vars_dict["grid"][key], float(row["grid_mwh"]))
        model.setSolVal(sol, vars_dict["polished"][key], float(row["polishing_kg"]))
        model.setSolVal(sol, vars_dict["polished_inventory"][key], split["polished_inventory"][key])
        model.setSolVal(sol, vars_dict["unpolished_inventory"][key], split["unpolished_inventory"][key])
        model.setSolVal(sol, vars_dict["vent_polished"][key], split["vent_polished"][key])
        model.setSolVal(sol, vars_dict["vent_unpolished"][key], split["vent_unpolished"][key])

    for (scenario_id, hour, lane_id), row in deliveries.items():
        key = (scenario_id, lane_id, hour)
        model.setSolVal(sol, vars_dict["trips"][key], int(row["trips"]))
        model.setSolVal(sol, vars_dict["delivered"][key], float(row["kg"]))

    for (scenario_id, hour, customer_id), row in shortfalls.items():
        key = (scenario_id, customer_id, hour)
        model.setSolVal(sol, vars_dict["shortfall"][key], float(row["kg"]))

    eta = incumbent_validation["cvar_eta"]
    model.setSolVal(sol, vars_dict["eta"], eta)
    for scenario_id, value in incumbent_validation["scenario_operating_costs"].items():
        model.setSolVal(sol, vars_dict["excess"][scenario_id], max(0.0, value - eta))

    return model.addSol(sol, free=True)


def validate_solution(data, solution):
    tol = data["tol"]
    hours = data["hours"]
    site_ids = data["site_ids"]
    customer_ids = data["customer_ids"]
    scenario_ids = data["scenario_ids"]
    sites = data["sites"]
    customers = data["customers"]
    lanes = data["lanes"]
    scenarios = data["scenarios"]
    forecast = data["forecast"]
    scenario_site = data["scenario_site"]
    scenario_demand = data["scenario_demand"]
    scenario_lane = data["scenario_lane"]
    config = data["config"]

    issues = []
    group_residuals = defaultdict(float)
    integrality_residual = 0.0

    def record(group, residual, scale, message):
        normalized = abs(residual) / max(1.0, scale)
        group_residuals[group] = max(group_residuals[group], normalized)
        if normalized > tol:
            issues.append(f"{group}: {message} (residual={residual:.8g}, normalized={normalized:.8g})")

    def parse_bool(value):
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"Expected boolean, got {value!r}")

    if set(solution.keys()) != {"infrastructure", "operations", "deliveries", "shortfalls"}:
        issues.append(f"top-level keys mismatch: {sorted(solution.keys())}")

    infra = {}
    if len(solution.get("infrastructure", [])) != len(site_ids):
        issues.append("infrastructure row count mismatch")
    for row in solution.get("infrastructure", []):
        site_id = row.get("site_id")
        if site_id not in sites or site_id in infra:
            issues.append(f"invalid infrastructure row {site_id}")
            continue
        modules = row.get("electrolyzer_modules")
        tanks = row.get("tank_modules")
        if abs(round(modules) - modules) > 1e-8:
            integrality_residual = max(integrality_residual, abs(round(modules) - modules))
        if abs(round(tanks) - tanks) > 1e-8:
            integrality_residual = max(integrality_residual, abs(round(tanks) - tanks))
        active = parse_bool(row.get("active"))
        polish = parse_bool(row.get("polishing_active"))
        modules = int(round(modules))
        tanks = int(round(tanks))
        infra[site_id] = {
            "active": active,
            "modules": modules,
            "tanks": tanks,
            "polishing_active": polish,
        }
        record("infra_modules", max(0.0, modules - sites[site_id]["max_modules"]), sites[site_id]["max_modules"], site_id)
        record("infra_tanks", max(0.0, tanks - sites[site_id]["max_tank_modules"]), sites[site_id]["max_tank_modules"], site_id)
        if not active:
            record("infra_active", modules + tanks + (1 if polish else 0), 1.0, site_id)

    for site_id in site_ids:
        infra.setdefault(site_id, {"active": False, "modules": 0, "tanks": 0, "polishing_active": False})

    ops = {}
    expected_ops = len(scenario_ids) * len(hours) * len(site_ids)
    if len(solution.get("operations", [])) != expected_ops:
        issues.append("operations row count mismatch")
    for row in solution.get("operations", []):
        key = (row.get("scenario_id"), row.get("hour"), row.get("site_id"))
        if key[0] not in scenarios or key[1] not in hours or key[2] not in sites or key in ops:
            issues.append(f"invalid operations row {key}")
            continue
        online = row.get("online_modules")
        startup = row.get("startup_modules")
        if abs(round(online) - online) > 1e-8:
            integrality_residual = max(integrality_residual, abs(round(online) - online))
        if abs(round(startup) - startup) > 1e-8:
            integrality_residual = max(integrality_residual, abs(round(startup) - startup))
        ops[key] = {
            "online": int(round(online)),
            "startup": int(round(startup)),
            "production": float(row.get("production_kg")),
            "renewable": float(row.get("renewable_mwh")),
            "grid": float(row.get("grid_mwh")),
            "storage": float(row.get("storage_end_kg")),
            "vent": float(row.get("vent_kg")),
            "water": float(row.get("water_m3")),
            "polishing": float(row.get("polishing_kg")),
        }
        record(
            "nonnegative_operations",
            min(
                0.0,
                ops[key]["production"],
                ops[key]["renewable"],
                ops[key]["grid"],
                ops[key]["storage"],
                ops[key]["vent"],
                ops[key]["water"],
                ops[key]["polishing"],
            ),
            1.0,
            str(key),
        )
    for scenario_id in scenario_ids:
        for hour in hours:
            for site_id in site_ids:
                ops.setdefault(
                    (scenario_id, hour, site_id),
                    {"online": 0, "startup": 0, "production": 0.0, "renewable": 0.0, "grid": 0.0, "storage": 0.0, "vent": 0.0, "water": 0.0, "polishing": 0.0},
                )

    deliveries = {}
    inbound = defaultdict(float)
    outbound = defaultdict(float)
    outbound_low = defaultdict(float)
    outbound_high = defaultdict(float)
    driver_hours = defaultdict(float)
    for row in solution.get("deliveries", []):
        key = (row.get("scenario_id"), row.get("hour"), row.get("lane_id"))
        if key[0] not in scenarios or key[1] not in hours or key[2] not in lanes or key in deliveries:
            issues.append(f"invalid delivery row {key}")
            continue
        trips = row.get("trips")
        if abs(round(trips) - trips) > 1e-8:
            integrality_residual = max(integrality_residual, abs(round(trips) - trips))
        trips = int(round(trips))
        kg = float(row.get("kg"))
        deliveries[key] = {"trips": trips, "kg": kg}
        lane = lanes[key[2]]
        inbound[(key[0], key[1], lane["customer_id"])] += kg
        outbound[(key[0], key[1], lane["origin_site"])] += kg
        if lane["customer_id"] in data["high_purity_customers"]:
            outbound_high[(key[0], key[1], lane["origin_site"])] += kg
        else:
            outbound_low[(key[0], key[1], lane["origin_site"])] += kg
        sl = scenario_lane[(key[0], key[2], key[1])]
        driver_hours[(key[0], key[1])] += trips * lane["driver_hours_per_trip"] * sl["travel_time_multiplier"]
        record("nonnegative_deliveries", min(0.0, kg, trips), 1.0, str(key))
        record("trip_cap", max(0.0, trips - sl["max_trips"]), max(1.0, sl["max_trips"]), str(key))
        record("payload", max(0.0, kg - lane["kg_per_trip"] * trips), max(1.0, lane["kg_per_trip"]), str(key))
        if key[1] < lane["hazmat_window_start"] or key[1] > lane["hazmat_window_end"]:
            record("hazmat_window", trips + kg, 1.0, str(key))
        if not infra[lane["origin_site"]]["active"]:
            record("inactive_delivery", trips + kg, 1.0, str(key))
        if (
            lane["customer_id"] in data["definite_high_purity_customers"]
            and not infra[lane["origin_site"]]["polishing_active"]
        ):
            record("purity_origin_9999", kg, max(1.0, kg), str(key))

    shorts = {}
    expected_shorts = len(scenario_ids) * len(hours) * len(customer_ids)
    if len(solution.get("shortfalls", [])) != expected_shorts:
        issues.append("shortfalls row count mismatch")
    for row in solution.get("shortfalls", []):
        key = (row.get("scenario_id"), row.get("hour"), row.get("customer_id"))
        if key[0] not in scenarios or key[1] not in hours or key[2] not in customers or key in shorts:
            issues.append(f"invalid shortfall row {key}")
            continue
        shorts[key] = float(row.get("kg"))
        record("nonnegative_shortfalls", min(0.0, shorts[key]), 1.0, str(key))
    for scenario_id in scenario_ids:
        for hour in hours:
            for customer_id in customer_ids:
                shorts.setdefault((scenario_id, hour, customer_id), 0.0)

    for scenario_id in scenario_ids:
        scenario = scenarios[scenario_id]
        emissions = 0.0
        for hour in hours:
            record(
                "driver_hours",
                max(0.0, driver_hours[(scenario_id, hour)] - scenario["driver_hours_cap"]),
                max(1.0, scenario["driver_hours_cap"]),
                f"{scenario_id},{hour}",
            )
            reserve = sum(ops[(scenario_id, hour, site_id)]["storage"] for site_id in site_ids)
            record(
                "reserve",
                max(0.0, config["system_min_reserve_kg"] - reserve),
                max(1.0, config["system_min_reserve_kg"]),
                f"{scenario_id},{hour}",
            )

        for customer_id in customer_ids:
            customer = customers[customer_id]
            total_short = 0.0
            for hour in hours:
                demand = scenario_demand[(scenario_id, customer_id, hour)]
                delivered = inbound[(scenario_id, hour, customer_id)]
                short = shorts[(scenario_id, hour, customer_id)]
                total_short += short
                record("demand_balance", delivered + short - demand, max(1.0, demand), f"{scenario_id},{hour},{customer_id}")
                record(
                    "hourly_shortfall",
                    max(0.0, short - customer["max_hourly_shortfall_fraction"] * demand),
                    max(1.0, demand),
                    f"{scenario_id},{hour},{customer_id}",
                )
            record(
                "service_floor",
                max(0.0, total_short - (1.0 - customer["service_floor_fraction"]) * data["daily_demand"][(scenario_id, customer_id)]),
                max(1.0, data["daily_demand"][(scenario_id, customer_id)]),
                f"{scenario_id},{customer_id}",
            )

        for site_id in site_ids:
            site = sites[site_id]
            storage_cap = site["tank_base_kg"] + site["tank_module_capacity_kg"] * infra[site_id]["tanks"]
            water_total = 0.0
            startup_total = 0.0
            p_lo = 0.0
            p_hi = 0.0
            for hour in hours:
                row = ops[(scenario_id, hour, site_id)]
                sh = scenario_site[(scenario_id, site_id, hour)]
                fc = forecast[(site_id, hour)]
                cap = site["module_capacity_kgph"] * sh["module_capacity_multiplier"]
                prev_storage = site["initial_inventory_kg"] if hour == 0 else ops[(scenario_id, hour - 1, site_id)]["storage"]
                prev_online = 0 if hour == 0 else ops[(scenario_id, hour - 1, site_id)]["online"]

                record("online", max(0.0, row["online"] - infra[site_id]["modules"]), max(1.0, infra[site_id]["modules"]), f"{scenario_id},{hour},{site_id}")
                if not infra[site_id]["active"]:
                    record(
                        "inactive_ops",
                        row["online"] + row["startup"] + row["production"] + row["renewable"] + row["grid"] + row["vent"] + row["polishing"],
                        1.0,
                        f"{scenario_id},{hour},{site_id}",
                    )
                record("prod_upper", max(0.0, row["production"] - cap * row["online"]), max(1.0, cap), f"{scenario_id},{hour},{site_id}")
                record(
                    "prod_lower",
                    max(0.0, site["min_load_fraction"] * cap * row["online"] - row["production"]),
                    max(1.0, cap),
                    f"{scenario_id},{hour},{site_id}",
                )
                record(
                    "energy",
                    max(0.0, config["energy_mwh_per_kg_h2"] * row["production"] + config["polishing_mwh_per_kg"] * row["polishing"] - row["renewable"] - row["grid"]),
                    max(1.0, row["renewable"] + row["grid"]),
                    f"{scenario_id},{hour},{site_id}",
                )
                record("renewable_cap", max(0.0, row["renewable"] - fc["renewable_mwh"] * sh["renewable_multiplier"]), max(1.0, fc["renewable_mwh"]), f"{scenario_id},{hour},{site_id}")
                record("grid_cap", max(0.0, row["grid"] - sh["grid_cap_mwh"]), max(1.0, sh["grid_cap_mwh"]), f"{scenario_id},{hour},{site_id}")
                record("water_link", row["water"] - config["water_m3_per_kg_h2"] * row["production"], max(1.0, row["water"]), f"{scenario_id},{hour},{site_id}")
                record(
                    "polishing_active",
                    max(0.0, row["polishing"] - data["max_polish"][site_id] * (1 if infra[site_id]["polishing_active"] else 0)),
                    max(1.0, data["max_polish"][site_id]),
                    f"{scenario_id},{hour},{site_id}",
                )
                record("storage_cap", max(0.0, row["storage"] - storage_cap), max(1.0, storage_cap), f"{scenario_id},{hour},{site_id}")
                record(
                    "inventory_balance",
                    row["storage"] - prev_storage - row["production"] + outbound[(scenario_id, hour, site_id)] + row["vent"],
                    max(1.0, prev_storage + row["production"]),
                    f"{scenario_id},{hour},{site_id}",
                )
                record("startup_delta", max(0.0, row["online"] - prev_online - row["startup"]), max(1.0, row["online"]), f"{scenario_id},{hour},{site_id}")

                p_lo = max(0.0, p_lo + row["polishing"] - outbound_high[(scenario_id, hour, site_id)] - row["vent"])
                p_hi = min(row["storage"], p_hi + row["polishing"] - outbound_high[(scenario_id, hour, site_id)])
                if hour == 0:
                    p_lo = max(0.0, row["polishing"] - outbound_high[(scenario_id, hour, site_id)] - row["vent"])
                    p_hi = min(row["storage"], row["polishing"] - outbound_high[(scenario_id, hour, site_id)])
                if p_lo > p_hi + tol:
                    issues.append(f"purity_balance: {scenario_id},{hour},{site_id} has no feasible polished inventory split")
                    group_residuals["purity_balance"] = max(group_residuals["purity_balance"], p_lo - p_hi)

                water_total += row["water"]
                startup_total += row["startup"]
                emissions += fc["grid_emission_kg_per_mwh"] * row["grid"]

            record("startup_limit", max(0.0, startup_total - site["startup_limit"]), max(1.0, site["startup_limit"]), f"{scenario_id},{site_id}")
            record(
                "water_limit",
                max(0.0, water_total - site["water_limit_m3_per_day"] * data["water_multiplier"][(scenario_id, site_id)]),
                max(1.0, site["water_limit_m3_per_day"]),
                f"{scenario_id},{site_id}",
            )
            record(
                "final_inventory",
                max(0.0, site["final_min_inventory_kg"] - ops[(scenario_id, hours[-1], site_id)]["storage"]),
                max(1.0, site["final_min_inventory_kg"]),
                f"{scenario_id},{site_id}",
            )

        record("emission_cap", max(0.0, emissions - scenario["emission_cap_kg"]), max(1.0, scenario["emission_cap_kg"]), scenario_id)

    infra_cost = sum(
        sites[site_id]["module_daily_cost"] * infra[site_id]["modules"]
        + sites[site_id]["tank_daily_cost"] * infra[site_id]["tanks"]
        + sites[site_id]["polishing_daily_cost"] * (1 if infra[site_id]["polishing_active"] else 0)
        for site_id in site_ids
    )
    op_costs = {}
    for scenario_id in scenario_ids:
        total = 0.0
        for site_id in site_ids:
            for hour in hours:
                row = ops[(scenario_id, hour, site_id)]
                fc = forecast[(site_id, hour)]
                total += sites[site_id]["variable_cost_per_kg"] * row["production"]
                total += config["water_cost_per_m3"] * config["water_m3_per_kg_h2"] * row["production"]
                total += config["startup_cost_per_module"] * row["startup"]
                total += sites[site_id]["polishing_cost_per_kg"] * row["polishing"]
                total += config["vent_penalty_per_kg"] * row["vent"]
                total += scenarios[scenario_id]["grid_price_multiplier"] * fc["grid_price_per_mwh"] * row["grid"]
                total += config["emission_cost_per_kg"] * fc["grid_emission_kg_per_mwh"] * row["grid"]
        for (scen, _, lane_id), row in deliveries.items():
            if scen == scenario_id:
                total += lanes[lane_id]["cost_per_trip"] * row["trips"]
        for (scen, _, customer_id), short in shorts.items():
            if scen == scenario_id:
                total += customers[customer_id]["shortfall_cost_per_kg"] * customers[customer_id]["priority_weight"] * short
        op_costs[scenario_id] = total

    expected_cost = sum(scenarios[w]["probability"] * op_costs[w] for w in scenario_ids)
    cvar_value, cvar_eta = compute_cvar([(scenarios[w]["probability"], op_costs[w]) for w in scenario_ids], config["tail_risk_alpha"])
    objective = infra_cost + expected_cost + config["tail_risk_weight"] * cvar_value

    return {
        "valid": not issues,
        "issues": issues,
        "max_normalized_residual": max(group_residuals.values(), default=0.0),
        "violated_constraint_groups": sum(1 for value in group_residuals.values() if value > tol),
        "integrality_residual": integrality_residual,
        "group_residuals": dict(sorted(group_residuals.items())),
        "coverage": {
            "infrastructure_rows": len(solution.get("infrastructure", [])),
            "operations_rows": len(solution.get("operations", [])),
            "shortfall_rows": len(solution.get("shortfalls", [])),
            "delivery_rows": len(solution.get("deliveries", [])),
        },
        "infrastructure_cost": infra_cost,
        "scenario_operating_costs": op_costs,
        "expected_operating_cost": expected_cost,
        "cvar_value": cvar_value,
        "cvar_eta": cvar_eta,
        "objective": objective,
    }


def run_negative_audit(data, solution):
    corrupted = copy.deepcopy(solution)
    if corrupted["shortfalls"]:
        corrupted["shortfalls"][0]["kg"] = clean_float(corrupted["shortfalls"][0]["kg"] + 1.0)
    elif corrupted["operations"]:
        corrupted["operations"][0]["storage_end_kg"] = clean_float(corrupted["operations"][0]["storage_end_kg"] + 1.0)
    audit = validate_solution(data, corrupted)
    return not audit["valid"]


def write_solution(solution):
    SOLUTION_PATH.write_text(json.dumps(solution, indent=2) + "\n")


def safe_gap(model):
    try:
        return model.getGap()
    except Exception:
        return None


def safe_obj(model):
    try:
        return model.getObjVal()
    except Exception:
        return None


def safe_bound(model):
    try:
        return model.getDualbound()
    except Exception:
        return None


def write_log(
    time_limit,
    elapsed,
    model,
    incumbent_validation,
    candidate_validation,
    final_validation,
    negative_audit_ok,
    published,
    warm_start_added,
):
    status = str(model.getStatus()) if model is not None else "not_run"
    nsols = model.getNSols() if model is not None else 0
    obj = safe_obj(model) if model is not None else None
    bound = safe_bound(model) if model is not None else None
    gap = safe_gap(model) if model is not None else None
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        f"- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Solver",
        "",
        f"- Status: `{status}`",
        f"- Solutions found: `{nsols}`",
        f"- Time limit used: `{time_limit}` seconds",
        f"- Elapsed solve+audit time: `{elapsed:.3f}` seconds",
        f"- Warm start added: `{warm_start_added}`",
    ]
    if obj is not None:
        lines.append(f"- Solver objective: `{obj:.8f}`")
    if bound is not None:
        lines.append(f"- Best bound: `{bound:.8f}`")
    if gap is not None:
        lines.append(f"- Relative gap: `{gap:.8f}`")

    lines.extend(
        [
            "",
            "## Incumbent",
            "",
            f"- Validation passed: `{incumbent_validation['valid']}`",
            f"- Objective: `{incumbent_validation['objective']:.8f}`",
            f"- Max normalized residual: `{incumbent_validation['max_normalized_residual']:.8g}`",
            f"- Violated constraint groups: `{incumbent_validation['violated_constraint_groups']}`",
            "",
            "## Candidate",
            "",
            f"- Candidate available: `{candidate_validation is not None}`",
            f"- Candidate validation passed: `{candidate_validation['valid'] if candidate_validation is not None else None}`",
            f"- Candidate objective: `{candidate_validation['objective']:.8f}`" if candidate_validation is not None else "- Candidate objective: `None`",
            f"- Published: `{published}`",
            "",
            "## Final Audit",
            "",
            f"- Final validation passed: `{final_validation['valid']}`",
            f"- Final objective recomputation: `{final_validation['objective']:.8f}`",
            f"- Max normalized residual: `{final_validation['max_normalized_residual']:.8g}`",
            f"- Violated constraint groups: `{final_validation['violated_constraint_groups']}`",
            f"- Integrality residual: `{final_validation['integrality_residual']:.8g}`",
            f"- Negative audit rejected corrupted copy: `{negative_audit_ok}`",
            f"- Coverage: `{final_validation['coverage']}`",
            f"- Infrastructure cost: `{final_validation['infrastructure_cost']:.8f}`",
            f"- Expected operating cost: `{final_validation['expected_operating_cost']:.8f}`",
            f"- CVaR value: `{final_validation['cvar_value']:.8f}`",
        ]
    )

    if final_validation["scenario_operating_costs"]:
        lines.extend(["", "## Scenario Operating Costs", ""])
        for scenario_id, value in sorted(final_validation["scenario_operating_costs"].items()):
            lines.append(f"- `{scenario_id}`: `{value:.8f}`")

    lines.extend(["", "## Residuals By Group", ""])
    for group, value in final_validation["group_residuals"].items():
        lines.append(f"- `{group}`: `{value:.8g}`")

    lines.extend(["", "## Validation Notes", ""])
    if final_validation["issues"]:
        for issue in final_validation["issues"][:50]:
            lines.append(f"- {issue}")
    else:
        lines.append("- No validation issues detected.")

    LOG_PATH.write_text("\n".join(lines) + "\n")


def main():
    start = time.time()
    time_limit = parse_time_limit()
    data = build_data()
    incumbent_solution = load_solution_from_disk()
    if incumbent_solution is None:
        raise RuntimeError("No rollback incumbent found at /app/submissions/solution.json")

    incumbent_validation = validate_solution(data, incumbent_solution)
    if not incumbent_validation["valid"]:
        raise RuntimeError("Existing disk incumbent failed independent validation")

    negative_audit_ok = run_negative_audit(data, incumbent_solution)

    model, vars_dict = build_model(data, time_limit)
    warm_start_added = add_incumbent_start(model, vars_dict, data, incumbent_solution, incumbent_validation)
    model.optimize()

    candidate_validation = None
    published = False
    if model.getNSols() > 0:
        candidate_solution = extract_solution(data, model, vars_dict)
        candidate_validation = validate_solution(data, candidate_solution)
        if candidate_validation["valid"] and candidate_validation["objective"] + 1e-8 < incumbent_validation["objective"]:
            write_solution(candidate_solution)
            published = True

    final_solution = load_solution_from_disk()
    final_validation = validate_solution(data, final_solution)
    if not final_validation["valid"]:
        raise RuntimeError("Final disk artifact failed independent validation")

    elapsed = time.time() - start
    write_log(
        time_limit,
        elapsed,
        model,
        incumbent_validation,
        candidate_validation,
        final_validation,
        negative_audit_ok,
        published,
        warm_start_added,
    )


if __name__ == "__main__":
    main()
