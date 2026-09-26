#!/usr/bin/env python3
import csv
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
PLAN_PATH = SUBMISSIONS_DIR / "initial_plan.csv"
CANDIDATE_PLAN_PATH = SUBMISSIONS_DIR / "_initial_plan_candidate.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log_initial.md"

GAP_LIMIT = 0.0005
DEFAULT_TIME_LIMIT = 60.0
VALUE_EPS = 1e-8
FEAS_TOL = 1e-6

VARIABLE_ORDER = {
    "setup": 0,
    "produce_regular": 1,
    "produce_overtime": 2,
    "ship_plant_dc": 3,
    "transfer_dc": 4,
    "ship_dc_customer": 5,
    "inventory": 6,
    "shortage": 7,
    "emergency_material": 8,
    "validated_shipper": 9,
}


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def to_float(value):
    return float(value) if value not in ("", None) else 0.0


def fmt_qty(value):
    return f"{value:.8f}"


def positive(value):
    return value > VALUE_EPS


def total_cost(audit_summary):
    return sum(audit_summary["cost_breakdown"].values())


def calc_norm_residual(lhs, rhs, kind):
    if kind == "eq":
        raw = abs(lhs - rhs)
    elif kind == "le":
        raw = max(0.0, lhs - rhs)
    elif kind == "ge":
        raw = max(0.0, rhs - lhs)
    else:
        raise ValueError(f"Unknown residual kind {kind}")
    scale = max(1.0, abs(rhs))
    return raw / scale, raw


def get_time_limit():
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is None or env_limit == "":
        return DEFAULT_TIME_LIMIT
    return min(DEFAULT_TIME_LIMIT, float(env_limit))


def load_inputs():
    current = json.loads((DATA_DIR / "current_week.json").read_text())
    stage = current["stage"]
    current_week = int(current["week"])
    if stage != "initial":
        raise ValueError(f"Expected initial stage, found {stage}")

    weeks = [int(row["week"]) for row in read_csv(DATA_DIR / "weeks.csv") if int(row["week"]) > current_week]
    products = {
        row["product_id"]: {
            "hold_cost": to_float(row["hold_cost_per_ton_week"]),
            "shortage_penalty": to_float(row["shortage_penalty_per_ton"]),
            "strategic_shortage_penalty": to_float(row["strategic_shortage_penalty_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "products.csv")
    }
    materials = {
        row["material_id"]: {
            "emergency_cost": to_float(row["emergency_unit_cost"]),
        }
        for row in read_csv(DATA_DIR / "materials.csv")
    }
    bom = {
        (row["product_id"], row["material_id"]): to_float(row["tons_material_per_ton_product"])
        for row in read_csv(DATA_DIR / "bom.csv")
    }
    plants = {
        row["plant_id"]: {
            "region": row["region"],
            "regular_hours": to_float(row["regular_hours"]),
            "overtime_hours": to_float(row["overtime_hours"]),
            "setup_team_hours": to_float(row["weekly_setup_team_hours"]),
        }
        for row in read_csv(DATA_DIR / "plants.csv")
    }
    plant_products = {
        (row["plant_id"], row["product_id"]): {
            "tons_per_hour": to_float(row["tons_per_hour"]),
            "min_run": to_float(row["min_run_tons"]),
            "max_weekly": to_float(row["max_weekly_tons"]),
            "setup_hours": to_float(row["setup_hours"]),
            "regular_cost": to_float(row["regular_cost_per_ton"]),
            "overtime_cost": to_float(row["overtime_cost_per_ton"]),
            "setup_cost": to_float(row["setup_cost"]),
        }
        for row in read_csv(DATA_DIR / "plant_products.csv")
    }
    warehouses = {
        row["dc_id"]: {
            "region": row["region"],
            "capacity": to_float(row["capacity_tons"]),
            "handling_cost": to_float(row["handling_cost_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "warehouses.csv")
    }
    initial_inventory = defaultdict(float)
    for row in read_csv(DATA_DIR / "initial_inventory.csv"):
        initial_inventory[(row["dc_id"], row["product_id"])] = to_float(row["initial_inventory_tons"])
    safety_stock = defaultdict(float)
    for row in read_csv(DATA_DIR / "safety_stock.csv"):
        safety_stock[(row["dc_id"], row["product_id"])] = to_float(row["safety_stock_tons"])
    customers = {
        row["customer_id"]: {
            "region": row["region"],
            "segment": row["segment"],
            "min_service_rate": to_float(row["min_service_rate"]),
        }
        for row in read_csv(DATA_DIR / "customers.csv")
    }
    demand = defaultdict(float)
    for row in read_csv(DATA_DIR / "demand.csv"):
        demand[(int(row["week"]), row["customer_id"], row["product_id"])] = to_float(row["demand_tons"])

    plant_to_dc = {
        (row["plant_id"], row["dc_id"]): {
            "capacity": to_float(row["weekly_capacity_tons"]),
            "cost": to_float(row["cost_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "plant_to_dc_lanes.csv")
    }
    dc_to_customer = {
        (row["dc_id"], row["customer_id"]): {
            "capacity": to_float(row["weekly_capacity_tons"]),
            "cost": to_float(row["cost_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "dc_to_customer_lanes.csv")
    }
    dc_transfer = {
        (row["from_dc"], row["to_dc"]): {
            "capacity": to_float(row["weekly_capacity_tons"]),
            "cost": to_float(row["cost_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "dc_transfer_lanes.csv")
    }
    policies = {
        row["stage"]: {
            "reserve_multiplier": to_float(row["reserve_multiplier_next_week_demand"]),
            "material_discount": to_float(row["material_robust_discount"]),
            "replan_change_penalty": to_float(row["replan_change_penalty_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "planning_policies.csv")
    }
    policy = policies[stage]

    material_supply = {}
    for row in read_csv(DATA_DIR / "material_supply.csv"):
        material_supply[(int(row["week"]), row["plant_id"], row["material_id"])] = {
            "regular_available": to_float(row["regular_available_tons"]),
            "emergency_cap": to_float(row["emergency_cap_tons"]),
        }

    customers_by_region = defaultdict(list)
    for customer_id, attrs in customers.items():
        customers_by_region[attrs["region"]].append(customer_id)

    next_week_of = {}
    for idx, week in enumerate(weeks):
        next_week_of[week] = weeks[idx + 1] if idx + 1 < len(weeks) else None

    reserve_target = defaultdict(float)
    for week in weeks:
        next_week = next_week_of[week]
        for dc_id, dc_attrs in warehouses.items():
            for product_id in products:
                if next_week is None:
                    reserve_target[(week, dc_id, product_id)] = 0.0
                else:
                    reserve_target[(week, dc_id, product_id)] = policy["reserve_multiplier"] * sum(
                        demand[(next_week, customer_id, product_id)]
                        for customer_id in customers_by_region[dc_attrs["region"]]
                    )

    return {
        "stage": stage,
        "current_week": current_week,
        "weeks": weeks,
        "products": products,
        "materials": materials,
        "bom": bom,
        "plants": plants,
        "plant_products": plant_products,
        "warehouses": warehouses,
        "initial_inventory": initial_inventory,
        "safety_stock": safety_stock,
        "customers": customers,
        "demand": demand,
        "plant_to_dc": plant_to_dc,
        "dc_to_customer": dc_to_customer,
        "dc_transfer": dc_transfer,
        "policy": policy,
        "material_supply": material_supply,
        "reserve_target": reserve_target,
    }


def build_model(data, time_limit):
    model = Model("cold_chain_initial")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", GAP_LIMIT)
    model.setParam("limits/time", time_limit)
    model.setParam("parallel/maxnthreads", 1)

    weeks = data["weeks"]
    products = list(data["products"].keys())
    materials = list(data["materials"].keys())
    plants = list(data["plants"].keys())
    dcs = list(data["warehouses"].keys())
    customers = list(data["customers"].keys())
    feasible_pp = list(data["plant_products"].keys())

    ship_pd_keys = [
        (week, plant_id, dc_id, product_id)
        for week in weeks
        for (plant_id, dc_id) in data["plant_to_dc"]
        for product_id in products
        if (plant_id, product_id) in data["plant_products"]
    ]
    ship_dc_keys = [
        (week, dc_id, customer_id, product_id)
        for week in weeks
        for (dc_id, customer_id) in data["dc_to_customer"]
        for product_id in products
        if data["demand"][(week, customer_id, product_id)] > 0.0
    ]
    transfer_keys = [
        (week, from_dc, to_dc, product_id)
        for week in weeks
        for (from_dc, to_dc) in data["dc_transfer"]
        for product_id in products
    ]
    demand_keys = [
        (week, customer_id, product_id)
        for week in weeks
        for customer_id in customers
        for product_id in products
        if data["demand"][(week, customer_id, product_id)] > 0.0
    ]
    emergency_keys = [
        (week, plant_id, material_id)
        for week in weeks
        for plant_id in plants
        for material_id in materials
    ]

    setup = {
        (week, plant_id, product_id): model.addVar(vtype="B", name=f"setup[{week},{plant_id},{product_id}]")
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    }
    prod_reg = {
        (week, plant_id, product_id): model.addVar(lb=0.0, name=f"prod_reg[{week},{plant_id},{product_id}]")
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    }
    prod_ot = {
        (week, plant_id, product_id): model.addVar(lb=0.0, name=f"prod_ot[{week},{plant_id},{product_id}]")
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    }
    ship_pd = {
        key: model.addVar(lb=0.0, name=f"ship_pd[{key[0]},{key[1]},{key[2]},{key[3]}]")
        for key in ship_pd_keys
    }
    ship_dc = {
        key: model.addVar(lb=0.0, name=f"ship_dc[{key[0]},{key[1]},{key[2]},{key[3]}]")
        for key in ship_dc_keys
    }
    transfer = {
        key: model.addVar(lb=0.0, name=f"transfer[{key[0]},{key[1]},{key[2]},{key[3]}]")
        for key in transfer_keys
    }
    inventory = {
        (week, dc_id, product_id): model.addVar(lb=0.0, name=f"inventory[{week},{dc_id},{product_id}]")
        for week in weeks
        for dc_id in dcs
        for product_id in products
    }
    shortage = {
        key: model.addVar(lb=0.0, name=f"shortage[{key[0]},{key[1]},{key[2]}]")
        for key in demand_keys
    }
    emergency = {}
    for key in emergency_keys:
        cap = data["material_supply"][key]["emergency_cap"]
        emergency[key] = model.addVar(lb=0.0, ub=cap, name=f"emergency[{key[0]},{key[1]},{key[2]}]")

    for week in weeks:
        for plant_id, product_id in feasible_pp:
            attrs = data["plant_products"][(plant_id, product_id)]
            total_prod = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            model.addCons(
                total_prod <= attrs["max_weekly"] * setup[(week, plant_id, product_id)],
                name=f"max_run[{week},{plant_id},{product_id}]",
            )
            model.addCons(
                total_prod >= attrs["min_run"] * setup[(week, plant_id, product_id)],
                name=f"min_run[{week},{plant_id},{product_id}]",
            )

        for plant_id in plants:
            model.addCons(
                quicksum(
                    prod_reg[(week, plant_id, product_id)] / data["plant_products"][(plant_id, product_id)]["tons_per_hour"]
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                )
                <= data["plants"][plant_id]["regular_hours"],
                name=f"regular_hours[{week},{plant_id}]",
            )
            model.addCons(
                quicksum(
                    prod_ot[(week, plant_id, product_id)] / data["plant_products"][(plant_id, product_id)]["tons_per_hour"]
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                )
                <= data["plants"][plant_id]["overtime_hours"],
                name=f"overtime_hours[{week},{plant_id}]",
            )
            model.addCons(
                quicksum(
                    data["plant_products"][(plant_id, product_id)]["setup_hours"] * setup[(week, plant_id, product_id)]
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                )
                <= data["plants"][plant_id]["setup_team_hours"],
                name=f"setup_hours[{week},{plant_id}]",
            )

            for material_id in materials:
                supply = data["material_supply"][(week, plant_id, material_id)]
                model.addCons(
                    quicksum(
                        data["bom"].get((product_id, material_id), 0.0)
                        * (prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)])
                        for product_id in products
                        if (plant_id, product_id) in data["plant_products"]
                    )
                    <= data["policy"]["material_discount"] * supply["regular_available"] + emergency[(week, plant_id, material_id)],
                    name=f"material[{week},{plant_id},{material_id}]",
                )

        for plant_id, product_id in feasible_pp:
            model.addCons(
                quicksum(
                    ship_pd[(week, plant_id, dc_id, product_id)]
                    for dc_id in dcs
                    if (week, plant_id, dc_id, product_id) in ship_pd
                )
                == prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)],
                name=f"plant_ship_balance[{week},{plant_id},{product_id}]",
            )

        for plant_id, dc_id in data["plant_to_dc"]:
            model.addCons(
                quicksum(
                    ship_pd[(week, plant_id, dc_id, product_id)]
                    for product_id in products
                    if (week, plant_id, dc_id, product_id) in ship_pd
                )
                <= data["plant_to_dc"][(plant_id, dc_id)]["capacity"],
                name=f"plant_dc_cap[{week},{plant_id},{dc_id}]",
            )

        for dc_id, customer_id in data["dc_to_customer"]:
            model.addCons(
                quicksum(
                    ship_dc[(week, dc_id, customer_id, product_id)]
                    for product_id in products
                    if (week, dc_id, customer_id, product_id) in ship_dc
                )
                <= data["dc_to_customer"][(dc_id, customer_id)]["capacity"],
                name=f"dc_customer_cap[{week},{dc_id},{customer_id}]",
            )

        for from_dc, to_dc in data["dc_transfer"]:
            model.addCons(
                quicksum(transfer[(week, from_dc, to_dc, product_id)] for product_id in products)
                <= data["dc_transfer"][(from_dc, to_dc)]["capacity"],
                name=f"transfer_cap[{week},{from_dc},{to_dc}]",
            )

        for customer_id in customers:
            for product_id in products:
                demand_value = data["demand"][(week, customer_id, product_id)]
                if demand_value <= 0.0:
                    continue
                shipped = quicksum(
                    ship_dc[(week, dc_id, customer_id, product_id)]
                    for dc_id in dcs
                    if (week, dc_id, customer_id, product_id) in ship_dc
                )
                model.addCons(
                    shipped + shortage[(week, customer_id, product_id)] == demand_value,
                    name=f"demand_balance[{week},{customer_id},{product_id}]",
                )
                model.addCons(
                    shipped >= data["customers"][customer_id]["min_service_rate"] * demand_value,
                    name=f"service[{week},{customer_id},{product_id}]",
                )

        for dc_id in dcs:
            model.addCons(
                quicksum(inventory[(week, dc_id, product_id)] for product_id in products)
                <= data["warehouses"][dc_id]["capacity"],
                name=f"warehouse_cap[{week},{dc_id}]",
            )
            for product_id in products:
                inbound_from_plants = quicksum(
                    ship_pd[(week, plant_id, dc_id, product_id)]
                    for plant_id in plants
                    if (week, plant_id, dc_id, product_id) in ship_pd
                )
                inbound_transfer = quicksum(
                    transfer[(week, from_dc, dc_id, product_id)]
                    for from_dc in dcs
                    if (week, from_dc, dc_id, product_id) in transfer
                )
                outbound_customers = quicksum(
                    ship_dc[(week, dc_id, customer_id, product_id)]
                    for customer_id in customers
                    if (week, dc_id, customer_id, product_id) in ship_dc
                )
                outbound_transfer = quicksum(
                    transfer[(week, dc_id, to_dc, product_id)]
                    for to_dc in dcs
                    if (week, dc_id, to_dc, product_id) in transfer
                )
                previous_inventory = (
                    data["initial_inventory"][(dc_id, product_id)]
                    if week == weeks[0]
                    else inventory[(weeks[weeks.index(week) - 1], dc_id, product_id)]
                )
                model.addCons(
                    inventory[(week, dc_id, product_id)]
                    == previous_inventory + inbound_from_plants + inbound_transfer - outbound_customers - outbound_transfer,
                    name=f"inventory_balance[{week},{dc_id},{product_id}]",
                )
                model.addCons(
                    inventory[(week, dc_id, product_id)]
                    >= data["safety_stock"][(dc_id, product_id)] + data["reserve_target"][(week, dc_id, product_id)],
                    name=f"inventory_floor[{week},{dc_id},{product_id}]",
                )

    objective_terms = []
    objective_terms.extend(
        data["plant_products"][(plant_id, product_id)]["setup_cost"] * setup[(week, plant_id, product_id)]
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    )
    objective_terms.extend(
        data["plant_products"][(plant_id, product_id)]["regular_cost"] * prod_reg[(week, plant_id, product_id)]
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    )
    objective_terms.extend(
        data["plant_products"][(plant_id, product_id)]["overtime_cost"] * prod_ot[(week, plant_id, product_id)]
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    )
    objective_terms.extend(
        data["plant_to_dc"][(plant_id, dc_id)]["cost"] * ship_pd[(week, plant_id, dc_id, product_id)]
        for (week, plant_id, dc_id, product_id) in ship_pd
    )
    objective_terms.extend(
        (
            data["dc_to_customer"][(dc_id, customer_id)]["cost"] + data["warehouses"][dc_id]["handling_cost"]
        )
        * ship_dc[(week, dc_id, customer_id, product_id)]
        for (week, dc_id, customer_id, product_id) in ship_dc
    )
    objective_terms.extend(
        data["dc_transfer"][(from_dc, to_dc)]["cost"] * transfer[(week, from_dc, to_dc, product_id)]
        for (week, from_dc, to_dc, product_id) in transfer
    )
    objective_terms.extend(
        data["products"][product_id]["hold_cost"] * inventory[(week, dc_id, product_id)]
        for week in weeks
        for dc_id in dcs
        for product_id in products
    )
    objective_terms.extend(
        (
            data["products"][product_id]["strategic_shortage_penalty"]
            if data["customers"][customer_id]["segment"] == "public_health"
            else data["products"][product_id]["shortage_penalty"]
        )
        * shortage[(week, customer_id, product_id)]
        for (week, customer_id, product_id) in shortage
    )
    objective_terms.extend(
        data["materials"][material_id]["emergency_cost"] * emergency[(week, plant_id, material_id)]
        for (week, plant_id, material_id) in emergency
    )
    model.setObjective(quicksum(objective_terms), "minimize")

    variables = {
        "setup": setup,
        "prod_reg": prod_reg,
        "prod_ot": prod_ot,
        "ship_pd": ship_pd,
        "ship_dc": ship_dc,
        "transfer": transfer,
        "inventory": inventory,
        "shortage": shortage,
        "emergency": emergency,
    }
    return model, variables


def get_solution_value(model, solution, var):
    value = model.getSolVal(solution, var)
    if abs(value) <= VALUE_EPS:
        return 0.0
    if value < 0.0 and abs(value) <= 1e-7:
        return 0.0
    return value


def export_rows(rows, plan_path):
    with open(plan_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["variable", "week", "item", "from", "to", "quantity"])
        for variable, week, item, from_node, to_node, quantity in rows:
            writer.writerow([variable, week, item, from_node, to_node, fmt_qty(quantity)])


def export_solution(data, model, variables, solution, plan_path=PLAN_PATH):
    rows = []
    weeks = data["weeks"]
    products = list(data["products"].keys())
    plants = list(data["plants"].keys())
    dcs = list(data["warehouses"].keys())
    customers = list(data["customers"].keys())
    materials = list(data["materials"].keys())

    for week in weeks:
        for plant_id, product_id in data["plant_products"]:
            value = get_solution_value(model, solution, variables["setup"][(week, plant_id, product_id)])
            if value > 0.5:
                rows.append(("setup", week, product_id, plant_id, "", 1.0))
            reg_val = get_solution_value(model, solution, variables["prod_reg"][(week, plant_id, product_id)])
            if positive(reg_val):
                rows.append(("produce_regular", week, product_id, plant_id, "", reg_val))
            ot_val = get_solution_value(model, solution, variables["prod_ot"][(week, plant_id, product_id)])
            if positive(ot_val):
                rows.append(("produce_overtime", week, product_id, plant_id, "", ot_val))

        for plant_id, dc_id in data["plant_to_dc"]:
            for product_id in products:
                key = (week, plant_id, dc_id, product_id)
                if key not in variables["ship_pd"]:
                    continue
                value = get_solution_value(model, solution, variables["ship_pd"][key])
                if positive(value):
                    rows.append(("ship_plant_dc", week, product_id, plant_id, dc_id, value))

        for from_dc, to_dc in data["dc_transfer"]:
            for product_id in products:
                value = get_solution_value(model, solution, variables["transfer"][(week, from_dc, to_dc, product_id)])
                if positive(value):
                    rows.append(("transfer_dc", week, product_id, from_dc, to_dc, value))

        for dc_id, customer_id in data["dc_to_customer"]:
            for product_id in products:
                key = (week, dc_id, customer_id, product_id)
                if key not in variables["ship_dc"]:
                    continue
                value = get_solution_value(model, solution, variables["ship_dc"][key])
                if positive(value):
                    rows.append(("ship_dc_customer", week, product_id, dc_id, customer_id, value))

        for dc_id in dcs:
            for product_id in products:
                value = get_solution_value(model, solution, variables["inventory"][(week, dc_id, product_id)])
                if positive(value):
                    rows.append(("inventory", week, product_id, dc_id, "", value))

        for customer_id in customers:
            for product_id in products:
                key = (week, customer_id, product_id)
                if key not in variables["shortage"]:
                    continue
                value = get_solution_value(model, solution, variables["shortage"][key])
                if positive(value):
                    rows.append(("shortage", week, product_id, customer_id, "", value))

        for plant_id in plants:
            for material_id in materials:
                value = get_solution_value(model, solution, variables["emergency"][(week, plant_id, material_id)])
                if positive(value):
                    rows.append(("emergency_material", week, material_id, "external", plant_id, value))

    rows.sort(key=lambda row: (row[1], VARIABLE_ORDER[row[0]], row[2], row[3], row[4]))
    export_rows(rows, plan_path)
    return rows


def parse_plan(path):
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames
    return fieldnames, rows


def load_plan_values(plan_path):
    _, rows = parse_plan(plan_path)
    values = {
        "setup": defaultdict(float),
        "prod_reg": defaultdict(float),
        "prod_ot": defaultdict(float),
        "ship_pd": defaultdict(float),
        "ship_dc": defaultdict(float),
        "transfer": defaultdict(float),
        "inventory": defaultdict(float),
        "shortage": defaultdict(float),
        "emergency": defaultdict(float),
    }
    for row in rows:
        week = int(row["week"])
        item = row["item"]
        from_node = row["from"]
        to_node = row["to"]
        quantity = float(row["quantity"])
        if row["variable"] == "setup":
            values["setup"][(week, from_node, item)] = 1.0 if quantity >= 0.5 else 0.0
        elif row["variable"] == "produce_regular":
            values["prod_reg"][(week, from_node, item)] = quantity
        elif row["variable"] == "produce_overtime":
            values["prod_ot"][(week, from_node, item)] = quantity
        elif row["variable"] == "ship_plant_dc":
            values["ship_pd"][(week, from_node, to_node, item)] = quantity
        elif row["variable"] == "ship_dc_customer":
            values["ship_dc"][(week, from_node, to_node, item)] = quantity
        elif row["variable"] == "transfer_dc":
            values["transfer"][(week, from_node, to_node, item)] = quantity
        elif row["variable"] == "inventory":
            values["inventory"][(week, from_node, item)] = quantity
        elif row["variable"] == "shortage":
            values["shortage"][(week, from_node, item)] = quantity
        elif row["variable"] == "emergency_material":
            values["emergency"][(week, to_node, item)] = quantity
    return values


def add_incumbent_mip_start(model, variables, plan_path):
    values = load_plan_values(plan_path)
    solution = model.createSol()
    for key, var in variables["setup"].items():
        model.setSolVal(solution, var, values["setup"].get(key, 0.0))
    for key, var in variables["prod_reg"].items():
        model.setSolVal(solution, var, values["prod_reg"].get(key, 0.0))
    for key, var in variables["prod_ot"].items():
        model.setSolVal(solution, var, values["prod_ot"].get(key, 0.0))
    for key, var in variables["ship_pd"].items():
        model.setSolVal(solution, var, values["ship_pd"].get(key, 0.0))
    for key, var in variables["ship_dc"].items():
        model.setSolVal(solution, var, values["ship_dc"].get(key, 0.0))
    for key, var in variables["transfer"].items():
        model.setSolVal(solution, var, values["transfer"].get(key, 0.0))
    for key, var in variables["inventory"].items():
        model.setSolVal(solution, var, values["inventory"].get(key, 0.0))
    for key, var in variables["shortage"].items():
        model.setSolVal(solution, var, values["shortage"].get(key, 0.0))
    for key, var in variables["emergency"].items():
        model.setSolVal(solution, var, values["emergency"].get(key, 0.0))
    return model.addSol(solution, free=True)


def audit_solution(data, plan_path):
    fieldnames, rows = parse_plan(plan_path)
    errors = []
    if fieldnames != ["variable", "week", "item", "from", "to", "quantity"]:
        errors.append(f"Invalid header: {fieldnames}")

    weeks = set(data["weeks"])
    products = set(data["products"].keys())
    materials = set(data["materials"].keys())
    plants = set(data["plants"].keys())
    dcs = set(data["warehouses"].keys())
    customers = set(data["customers"].keys())
    feasible_pp = set(data["plant_products"].keys())
    plant_dc_lanes = set(data["plant_to_dc"].keys())
    dc_customer_lanes = set(data["dc_to_customer"].keys())
    transfer_lanes = set(data["dc_transfer"].keys())

    setup = defaultdict(float)
    prod_reg = defaultdict(float)
    prod_ot = defaultdict(float)
    ship_pd = defaultdict(float)
    ship_dc = defaultdict(float)
    transfer = defaultdict(float)
    inventory = defaultdict(float)
    shortage = defaultdict(float)
    emergency = defaultdict(float)

    seen_keys = set()
    integrality_residual = 0.0
    unsupported_lane_rows = []
    unsupported_plant_product_rows = []

    for row in rows:
        variable = row["variable"]
        try:
            week = int(row["week"])
        except ValueError:
            errors.append(f"Invalid week value in row {row}")
            continue
        item = row["item"]
        from_node = row["from"]
        to_node = row["to"]
        try:
            quantity = float(row["quantity"])
        except ValueError:
            errors.append(f"Invalid quantity in row {row}")
            continue
        key = (variable, week, item, from_node, to_node)
        if key in seen_keys:
            errors.append(f"Duplicate row key {key}")
        seen_keys.add(key)
        if week not in weeks:
            errors.append(f"Invalid week in row {key}")
        if quantity < -VALUE_EPS:
            errors.append(f"Negative quantity in row {key}")
        if quantity <= VALUE_EPS:
            errors.append(f"Zero-valued row should be omitted {key}")
        if variable == "setup":
            if item not in products or from_node not in plants or to_node != "":
                errors.append(f"Invalid setup row {key}")
            if (from_node, item) not in feasible_pp:
                unsupported_plant_product_rows.append(key)
                errors.append(f"Unsupported plant-product in setup row {key}")
            setup[(week, from_node, item)] = quantity
            integrality_residual = max(integrality_residual, abs(quantity - round(quantity)))
        elif variable == "produce_regular":
            if item not in products or from_node not in plants or to_node != "":
                errors.append(f"Invalid produce_regular row {key}")
            if (from_node, item) not in feasible_pp:
                unsupported_plant_product_rows.append(key)
                errors.append(f"Unsupported plant-product in produce_regular row {key}")
            prod_reg[(week, from_node, item)] = quantity
        elif variable == "produce_overtime":
            if item not in products or from_node not in plants or to_node != "":
                errors.append(f"Invalid produce_overtime row {key}")
            if (from_node, item) not in feasible_pp:
                unsupported_plant_product_rows.append(key)
                errors.append(f"Unsupported plant-product in produce_overtime row {key}")
            prod_ot[(week, from_node, item)] = quantity
        elif variable == "ship_plant_dc":
            if item not in products or from_node not in plants or to_node not in dcs:
                errors.append(f"Invalid ship_plant_dc row {key}")
            if (from_node, item) not in feasible_pp:
                unsupported_plant_product_rows.append(key)
                errors.append(f"Unsupported plant-product in ship_plant_dc row {key}")
            if (from_node, to_node) not in plant_dc_lanes:
                unsupported_lane_rows.append(key)
                errors.append(f"Unsupported plant-to-DC lane {key}")
            ship_pd[(week, from_node, to_node, item)] = quantity
        elif variable == "ship_dc_customer":
            if item not in products or from_node not in dcs or to_node not in customers:
                errors.append(f"Invalid ship_dc_customer row {key}")
            if (from_node, to_node) not in dc_customer_lanes:
                unsupported_lane_rows.append(key)
                errors.append(f"Unsupported DC-to-customer lane {key}")
            ship_dc[(week, from_node, to_node, item)] = quantity
        elif variable == "transfer_dc":
            if item not in products or from_node not in dcs or to_node not in dcs or from_node == to_node:
                errors.append(f"Invalid transfer_dc row {key}")
            if (from_node, to_node) not in transfer_lanes:
                unsupported_lane_rows.append(key)
                errors.append(f"Unsupported DC transfer lane {key}")
            transfer[(week, from_node, to_node, item)] = quantity
        elif variable == "inventory":
            if item not in products or from_node not in dcs or to_node != "":
                errors.append(f"Invalid inventory row {key}")
            inventory[(week, from_node, item)] = quantity
        elif variable == "shortage":
            if item not in products or from_node not in customers or to_node != "":
                errors.append(f"Invalid shortage row {key}")
            shortage[(week, from_node, item)] = quantity
        elif variable == "emergency_material":
            if item not in materials or from_node != "external" or to_node not in plants:
                errors.append(f"Invalid emergency_material row {key}")
            emergency[(week, to_node, item)] = quantity
        else:
            errors.append(f"Unsupported variable {variable}")

    residuals = defaultdict(float)
    raw_residuals = defaultdict(float)

    def record(group, lhs, rhs, kind):
        norm, raw = calc_norm_residual(lhs, rhs, kind)
        residuals[group] = max(residuals[group], norm)
        raw_residuals[group] = max(raw_residuals[group], raw)

    product_list = list(data["products"].keys())
    material_list = list(data["materials"].keys())
    plant_list = list(data["plants"].keys())
    dc_list = list(data["warehouses"].keys())
    customer_list = list(data["customers"].keys())
    weeks_sorted = data["weeks"]
    previous_week = {weeks_sorted[idx]: (weeks_sorted[idx - 1] if idx > 0 else None) for idx in range(len(weeks_sorted))}

    for week in weeks_sorted:
        for plant_id, product_id in data["plant_products"]:
            attrs = data["plant_products"][(plant_id, product_id)]
            total_prod = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            y = setup[(week, plant_id, product_id)]
            record("setup_upper", total_prod, attrs["max_weekly"] * y, "le")
            record("setup_lower", total_prod, attrs["min_run"] * y, "ge")
        for plant_id in plant_list:
            regular_hours_used = sum(
                prod_reg[(week, plant_id, product_id)] / data["plant_products"][(plant_id, product_id)]["tons_per_hour"]
                for product_id in product_list
                if (plant_id, product_id) in data["plant_products"]
            )
            overtime_hours_used = sum(
                prod_ot[(week, plant_id, product_id)] / data["plant_products"][(plant_id, product_id)]["tons_per_hour"]
                for product_id in product_list
                if (plant_id, product_id) in data["plant_products"]
            )
            setup_hours_used = sum(
                data["plant_products"][(plant_id, product_id)]["setup_hours"] * setup[(week, plant_id, product_id)]
                for product_id in product_list
                if (plant_id, product_id) in data["plant_products"]
            )
            record("regular_hours", regular_hours_used, data["plants"][plant_id]["regular_hours"], "le")
            record("overtime_hours", overtime_hours_used, data["plants"][plant_id]["overtime_hours"], "le")
            record("setup_hours", setup_hours_used, data["plants"][plant_id]["setup_team_hours"], "le")
            for material_id in material_list:
                material_use = sum(
                    data["bom"].get((product_id, material_id), 0.0)
                    * (prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)])
                    for product_id in product_list
                    if (plant_id, product_id) in data["plant_products"]
                )
                supply = data["material_supply"][(week, plant_id, material_id)]
                record(
                    "material_balance",
                    material_use,
                    data["policy"]["material_discount"] * supply["regular_available"] + emergency[(week, plant_id, material_id)],
                    "le",
                )
                record("emergency_cap", emergency[(week, plant_id, material_id)], supply["emergency_cap"], "le")
        for plant_id, dc_id in data["plant_to_dc"]:
            lane_total = sum(ship_pd[(week, plant_id, dc_id, product_id)] for product_id in product_list)
            record("plant_dc_lane", lane_total, data["plant_to_dc"][(plant_id, dc_id)]["capacity"], "le")
        for dc_id, customer_id in data["dc_to_customer"]:
            lane_total = sum(ship_dc[(week, dc_id, customer_id, product_id)] for product_id in product_list)
            record("dc_customer_lane", lane_total, data["dc_to_customer"][(dc_id, customer_id)]["capacity"], "le")
        for from_dc, to_dc in data["dc_transfer"]:
            lane_total = sum(transfer[(week, from_dc, to_dc, product_id)] for product_id in product_list)
            record("dc_transfer_lane", lane_total, data["dc_transfer"][(from_dc, to_dc)]["capacity"], "le")
        for customer_id in customer_list:
            for product_id in product_list:
                demand_value = data["demand"][(week, customer_id, product_id)]
                if demand_value <= 0.0:
                    continue
                shipped = sum(
                    ship_dc[(week, dc_id, customer_id, product_id)]
                    for dc_id in dc_list
                    if (dc_id, customer_id) in data["dc_to_customer"]
                )
                record("demand_balance", shipped + shortage[(week, customer_id, product_id)], demand_value, "eq")
                record(
                    "service_floor",
                    shipped,
                    data["customers"][customer_id]["min_service_rate"] * demand_value,
                    "ge",
                )
        for plant_id, product_id in data["plant_products"]:
            shipped = sum(
                ship_pd[(week, plant_id, dc_id, product_id)]
                for dc_id in dc_list
                if (plant_id, dc_id) in data["plant_to_dc"]
            )
            produced = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            record("plant_ship_balance", shipped, produced, "eq")
        for dc_id in dc_list:
            inv_total = sum(inventory[(week, dc_id, product_id)] for product_id in product_list)
            record("warehouse_capacity", inv_total, data["warehouses"][dc_id]["capacity"], "le")
            for product_id in product_list:
                inbound_from_plants = sum(
                    ship_pd[(week, plant_id, dc_id, product_id)]
                    for plant_id in plant_list
                    if (plant_id, dc_id) in data["plant_to_dc"]
                )
                inbound_transfer = sum(
                    transfer[(week, from_dc, dc_id, product_id)]
                    for from_dc in dc_list
                    if (from_dc, dc_id) in data["dc_transfer"]
                )
                outbound_customers = sum(
                    ship_dc[(week, dc_id, customer_id, product_id)]
                    for customer_id in customer_list
                    if (dc_id, customer_id) in data["dc_to_customer"]
                )
                outbound_transfer = sum(
                    transfer[(week, dc_id, to_dc, product_id)]
                    for to_dc in dc_list
                    if (dc_id, to_dc) in data["dc_transfer"]
                )
                prev_week = previous_week[week]
                prev_inventory = (
                    data["initial_inventory"][(dc_id, product_id)]
                    if prev_week is None
                    else inventory[(prev_week, dc_id, product_id)]
                )
                record(
                    "inventory_balance",
                    inventory[(week, dc_id, product_id)],
                    prev_inventory + inbound_from_plants + inbound_transfer - outbound_customers - outbound_transfer,
                    "eq",
                )
                record(
                    "inventory_floor",
                    inventory[(week, dc_id, product_id)],
                    data["safety_stock"][(dc_id, product_id)] + data["reserve_target"][(week, dc_id, product_id)],
                    "ge",
                )

    cost_breakdown = defaultdict(float)
    for (week, plant_id, product_id), value in setup.items():
        cost_breakdown["setup_cost"] += data["plant_products"][(plant_id, product_id)]["setup_cost"] * value
    for (week, plant_id, product_id), value in prod_reg.items():
        cost_breakdown["regular_production_cost"] += data["plant_products"][(plant_id, product_id)]["regular_cost"] * value
    for (week, plant_id, product_id), value in prod_ot.items():
        cost_breakdown["overtime_production_cost"] += data["plant_products"][(plant_id, product_id)]["overtime_cost"] * value
    for (week, plant_id, dc_id, product_id), value in ship_pd.items():
        cost_breakdown["plant_to_dc_cost"] += data["plant_to_dc"][(plant_id, dc_id)]["cost"] * value
    for (week, dc_id, customer_id, product_id), value in ship_dc.items():
        cost_breakdown["dc_to_customer_cost"] += data["dc_to_customer"][(dc_id, customer_id)]["cost"] * value
        cost_breakdown["handling_cost"] += data["warehouses"][dc_id]["handling_cost"] * value
    for (week, from_dc, to_dc, product_id), value in transfer.items():
        cost_breakdown["transfer_cost"] += data["dc_transfer"][(from_dc, to_dc)]["cost"] * value
    for (week, dc_id, product_id), value in inventory.items():
        cost_breakdown["holding_cost"] += data["products"][product_id]["hold_cost"] * value
    for (week, customer_id, product_id), value in shortage.items():
        penalty = (
            data["products"][product_id]["strategic_shortage_penalty"]
            if data["customers"][customer_id]["segment"] == "public_health"
            else data["products"][product_id]["shortage_penalty"]
        )
        cost_breakdown["shortage_cost"] += penalty * value
    for (week, plant_id, material_id), value in emergency.items():
        cost_breakdown["emergency_material_cost"] += data["materials"][material_id]["emergency_cost"] * value

    theta = max(residuals.values()) if residuals else 0.0
    violated_groups = [group for group, residual in residuals.items() if residual > FEAS_TOL]
    hard_errors = list(errors)
    if integrality_residual > FEAS_TOL:
        hard_errors.append(f"Integrality residual {integrality_residual}")

    return {
        "rows": len(rows),
        "errors": hard_errors,
        "theta": theta,
        "residuals": dict(sorted(residuals.items())),
        "raw_residuals": dict(sorted(raw_residuals.items())),
        "violated_groups": violated_groups,
        "integrality_residual": integrality_residual,
        "cost_breakdown": dict(cost_breakdown),
        "unsupported_lane_rows": unsupported_lane_rows,
        "unsupported_plant_product_rows": unsupported_plant_product_rows,
    }


def audit_rejects_corruption(data, plan_path):
    fieldnames, rows = parse_plan(plan_path)
    corrupted_path = SUBMISSIONS_DIR / "_initial_plan_corrupted.csv"
    corrupted_rows = rows[1:] if len(rows) > 1 else rows
    with open(corrupted_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(corrupted_rows)
    corrupted_audit = audit_solution(data, corrupted_path)
    corrupted_path.unlink(missing_ok=True)
    return corrupted_audit["theta"] > FEAS_TOL or bool(corrupted_audit["errors"]) or bool(corrupted_audit["violated_groups"])


def write_log(data, report):
    lines = []
    lines.append("# Initial Solve Log")
    lines.append("")
    lines.append("## Rollback Incumbent")
    lines.append("")
    lines.append(f"- Objective from disk audit: `{report['incumbent_objective']:.8f}`")
    lines.append(f"- Audit pass: `{'yes' if report['incumbent_audit_pass'] else 'no'}`")
    lines.append(f"- Rows: `{report['incumbent_audit']['rows']}`")
    lines.append(f"- Theta: `{report['incumbent_audit']['theta']:.8e}`")
    lines.append(f"- Unsupported lane rows: `{len(report['incumbent_audit']['unsupported_lane_rows'])}`")
    lines.append(f"- Unsupported plant-product rows: `{len(report['incumbent_audit']['unsupported_plant_product_rows'])}`")
    lines.append("")
    lines.append("## Performance Phase")
    lines.append("")
    lines.append(f"- MIP start accepted: `{'yes' if report['mip_start_accepted'] else 'no'}`")
    lines.append(f"- Solve status: `{report['solve_status']}`")
    lines.append(f"- Time limit seconds: `{report['time_limit']}`")
    lines.append(f"- Solve time seconds: `{report['solve_time']:.4f}`")
    lines.append(f"- Solver incumbent count: `{report['n_solutions']}`")
    lines.append(f"- Best bound: `{report['best_bound']:.8f}`")
    lines.append(f"- Relative gap: `{report['gap']:.8f}`")
    if report["candidate_objective"] is None:
        lines.append("- Candidate objective from disk audit: `none`")
    else:
        lines.append(f"- Candidate objective from disk audit: `{report['candidate_objective']:.8f}`")
        lines.append(f"- Candidate audit pass: `{'yes' if report['candidate_audit_pass'] else 'no'}`")
    lines.append(f"- Published artifact: `{report['publication_result']}`")
    lines.append("")
    lines.append("## Final Artifact Audit")
    lines.append("")
    lines.append(f"- Exported rows: `{report['final_audit']['rows']}`")
    lines.append(f"- Max normalized hard residual theta: `{report['final_audit']['theta']:.8e}`")
    lines.append(f"- Violated hard-constraint groups: `{len(report['final_audit']['violated_groups'])}`")
    lines.append(f"- Integrality residual: `{report['final_audit']['integrality_residual']:.8e}`")
    lines.append(
        f"- Decision coverage: `weeks={len(data['weeks'])}/{len(data['weeks'])}`, "
        f"`demand_keys={len(data['weeks']) * len(data['customers']) * len(data['products'])}` "
        f"reconstructible from sparse export, "
        f"`inventory_keys={len(data['weeks']) * len(data['warehouses']) * len(data['products'])}` reconstructible"
    )
    lines.append(f"- Corrupted-copy rejection test: `{'passed' if report['corruption_rejected'] else 'failed'}`")
    if report["final_audit"]["errors"]:
        lines.append("- Audit errors:")
        for error in report["final_audit"]["errors"]:
            lines.append(f"  - {error}")
    lines.append("")
    lines.append("## Objective Breakdown")
    lines.append("")
    total = 0.0
    for name, value in sorted(report["final_audit"]["cost_breakdown"].items()):
        total += value
        lines.append(f"- `{name}`: `{value:.8f}`")
    lines.append(f"- `recomputed_total`: `{total:.8f}`")
    lines.append("")
    lines.append("## Constraint Residuals")
    lines.append("")
    for name, residual in report["final_audit"]["residuals"].items():
        lines.append(
            f"- `{name}`: normalized=`{residual:.8e}`, raw=`{report['final_audit']['raw_residuals'][name]:.8e}`"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Reserve stock is enforced as safety stock plus 11% of next-week demand for customers in the same DC region.")
    lines.append("- Shortage uses the strategic product penalty only for customers in the `public_health` segment.")
    lines.append("- Production is shipped out of the plant in the same week; no plant finished-goods inventory or disposal is allowed.")
    lines.append("- The public challenger's unsupported-lane concern was replayed directly on the exported CSV; zero unsupported shipment or transfer rows were found.")

    LOG_PATH.write_text("\n".join(lines) + "\n")


def main():
    data = load_inputs()
    incumbent_audit = audit_solution(data, PLAN_PATH)
    incumbent_audit_pass = (
        not incumbent_audit["errors"]
        and incumbent_audit["theta"] <= FEAS_TOL
        and not incumbent_audit["violated_groups"]
    )
    if not incumbent_audit_pass:
        report = {
            "incumbent_objective": total_cost(incumbent_audit),
            "incumbent_audit": incumbent_audit,
            "incumbent_audit_pass": False,
            "mip_start_accepted": False,
            "solve_status": "not_run",
            "time_limit": get_time_limit(),
            "solve_time": 0.0,
            "n_solutions": 0,
            "best_bound": math.nan,
            "gap": math.nan,
            "candidate_objective": None,
            "candidate_audit_pass": False,
            "publication_result": "rollback incumbent kept; incumbent audit failed",
            "final_audit": incumbent_audit,
            "corruption_rejected": audit_rejects_corruption(data, PLAN_PATH),
        }
        write_log(data, report)
        raise RuntimeError("Rollback incumbent failed strengthened independent audit")

    time_limit = get_time_limit()
    start_time = time.time()
    model, variables = build_model(data, time_limit)
    mip_start_accepted = add_incumbent_mip_start(model, variables, PLAN_PATH)

    model.optimize()
    solve_time = time.time() - start_time
    status = str(model.getStatus())
    n_solutions = model.getNSols()
    incumbent_objective = total_cost(incumbent_audit)
    candidate_objective = None
    candidate_audit = None
    candidate_audit_pass = False
    publication_result = "rollback incumbent kept"

    if n_solutions > 0 and model.getBestSol() is not None:
        solution = model.getBestSol()
        export_solution(data, model, variables, solution, CANDIDATE_PLAN_PATH)
        candidate_audit = audit_solution(data, CANDIDATE_PLAN_PATH)
        candidate_objective = total_cost(candidate_audit)
        candidate_audit_pass = (
            not candidate_audit["errors"]
            and candidate_audit["theta"] <= FEAS_TOL
            and not candidate_audit["violated_groups"]
        )
        if candidate_audit_pass and candidate_objective + VALUE_EPS < incumbent_objective:
            CANDIDATE_PLAN_PATH.replace(PLAN_PATH)
            publication_result = "candidate promoted"
        else:
            CANDIDATE_PLAN_PATH.unlink(missing_ok=True)
            if candidate_audit_pass:
                publication_result = "rollback incumbent kept; no strict improvement"
            else:
                publication_result = "rollback incumbent kept; candidate failed audit"
    else:
        publication_result = "rollback incumbent kept; solver returned no candidate"

    final_audit = audit_solution(data, PLAN_PATH)
    final_audit_pass = (
        not final_audit["errors"]
        and final_audit["theta"] <= FEAS_TOL
        and not final_audit["violated_groups"]
    )
    if not final_audit_pass:
        raise RuntimeError("Final artifact failed independent audit")

    report = {
        "incumbent_objective": incumbent_objective,
        "incumbent_audit": incumbent_audit,
        "incumbent_audit_pass": incumbent_audit_pass,
        "mip_start_accepted": mip_start_accepted,
        "solve_status": status,
        "time_limit": time_limit,
        "solve_time": solve_time,
        "n_solutions": n_solutions,
        "best_bound": model.getDualbound() if n_solutions > 0 else math.nan,
        "gap": model.getGap() if n_solutions > 0 else math.nan,
        "candidate_objective": candidate_objective,
        "candidate_audit_pass": candidate_audit_pass,
        "publication_result": publication_result,
        "final_audit": final_audit,
        "corruption_rejected": audit_rejects_corruption(data, PLAN_PATH),
    }
    write_log(data, report)


if __name__ == "__main__":
    main()
