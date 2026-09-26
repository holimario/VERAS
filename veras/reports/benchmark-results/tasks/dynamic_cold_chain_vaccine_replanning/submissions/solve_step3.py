#!/usr/bin/env python3
import csv
import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
STATE_PATH = ROOT / "state" / "week4_state.json"
SUBMISSIONS_DIR = ROOT / "submissions"
BASELINE_PLAN_PATH = SUBMISSIONS_DIR / "step2_replan.csv"
PLAN_PATH = SUBMISSIONS_DIR / "step3_replan.csv"
CANDIDATE_PLAN_PATH = SUBMISSIONS_DIR / "_step3_replan_candidate.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log_step3.md"
MODEL_PATH = SUBMISSIONS_DIR / "model_step3.md"

GAP_LIMIT = 0.0005
DEFAULT_TIME_LIMIT = 180.0
VALUE_EPS = 1e-8
FEAS_TOL = 1e-6
PRIORITY_PRODUCTS = {"V_MRNA", "V_PED"}

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
    if not env_limit:
        return DEFAULT_TIME_LIMIT
    return min(DEFAULT_TIME_LIMIT, float(env_limit))


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


def parse_plan(path):
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames
    return fieldnames, rows


def customer_matches_filter(data, customer_id, customer_filter):
    if customer_filter in ("", "all"):
        return True
    if customer_id == customer_filter:
        return True
    attrs = data["customers"].get(customer_id, {})
    return attrs.get("segment") == customer_filter or attrs.get("region") == customer_filter


def shortage_penalty(data, customer_id, product_id):
    attrs = data["customers"][customer_id]
    product = data["products"][product_id]
    if attrs["segment"] == "public_health":
        return product["strategic_shortage_penalty"]
    if attrs["segment"] == "hospital" and product_id in PRIORITY_PRODUCTS:
        return product["strategic_shortage_penalty"]
    return product["shortage_penalty"]


def load_plan_values(plan_path, active_weeks):
    _, rows = parse_plan(plan_path)
    active_weeks = set(active_weeks)
    values = {
        "setup": defaultdict(float),
        "prod_reg": defaultdict(float),
        "prod_ot": defaultdict(float),
        "prod_total": defaultdict(float),
        "ship_pd": defaultdict(float),
        "ship_dc": defaultdict(float),
        "transfer": defaultdict(float),
        "inventory": defaultdict(float),
        "shortage": defaultdict(float),
        "emergency": defaultdict(float),
        "rows": [],
    }
    for row in rows:
        week = int(row["week"])
        if week not in active_weeks:
            continue
        values["rows"].append(row)
        item = row["item"]
        from_node = row["from"]
        to_node = row["to"]
        quantity = float(row["quantity"])
        if row["variable"] == "setup":
            values["setup"][(week, from_node, item)] = 1.0 if quantity >= 0.5 else 0.0
        elif row["variable"] == "produce_regular":
            values["prod_reg"][(week, from_node, item)] = quantity
            values["prod_total"][(week, from_node, item)] += quantity
        elif row["variable"] == "produce_overtime":
            values["prod_ot"][(week, from_node, item)] = quantity
            values["prod_total"][(week, from_node, item)] += quantity
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


def load_baseline(active_weeks):
    return load_plan_values(BASELINE_PLAN_PATH, active_weeks)


def build_frozen_entries(data):
    entries = []
    for rule in data["frozen_policies"]:
        variable = rule["variable"]
        start_week = rule["start_week"]
        end_week = rule["end_week"]
        if variable == "ship_plant_dc":
            for key in data["ship_pd_keys"]:
                if start_week <= key[0] <= end_week:
                    baseline_qty = data["baseline"]["ship_pd"].get(key, 0.0)
                    entries.append(
                        {
                            "variable": variable,
                            "key": key,
                            "minimum_fraction": rule["minimum_fraction"],
                            "maximum_fraction": rule["maximum_fraction"],
                            "baseline_qty": baseline_qty,
                        }
                    )
        elif variable == "ship_dc_customer":
            for key in data["ship_dc_keys"]:
                if start_week <= key[0] <= end_week and customer_matches_filter(data, key[2], rule["customer_filter"]):
                    baseline_qty = data["baseline"]["ship_dc"].get(key, 0.0)
                    entries.append(
                        {
                            "variable": variable,
                            "key": key,
                            "minimum_fraction": rule["minimum_fraction"],
                            "maximum_fraction": rule["maximum_fraction"],
                            "baseline_qty": baseline_qty,
                        }
                    )
        elif variable == "transfer_dc":
            for key in data["transfer_keys"]:
                if start_week <= key[0] <= end_week:
                    baseline_qty = data["baseline"]["transfer"].get(key, 0.0)
                    entries.append(
                        {
                            "variable": variable,
                            "key": key,
                            "minimum_fraction": rule["minimum_fraction"],
                            "maximum_fraction": rule["maximum_fraction"],
                            "baseline_qty": baseline_qty,
                        }
                    )
        else:
            raise ValueError(f"Unsupported frozen variable {variable}")
    return entries


def load_inputs():
    current = json.loads((DATA_DIR / "current_week.json").read_text())
    if current["stage"] != "week4":
        raise ValueError(f"Expected week4 stage, found {current['stage']}")
    event_week = int(current["week"])

    weeks = [int(row["week"]) for row in read_csv(DATA_DIR / "weeks.csv") if int(row["week"]) >= event_week]
    products = {
        row["product_id"]: {
            "hold_cost": to_float(row["hold_cost_per_ton_week"]),
            "shortage_penalty": to_float(row["shortage_penalty_per_ton"]),
            "strategic_shortage_penalty": to_float(row["strategic_shortage_penalty_per_ton"]),
        }
        for row in read_csv(DATA_DIR / "products.csv")
    }
    materials = {row["material_id"]: {"emergency_cost": to_float(row["emergency_unit_cost"])} for row in read_csv(DATA_DIR / "materials.csv")}
    bom = {
        (row["product_id"], row["material_id"]): to_float(row["tons_material_per_ton_product"])
        for row in read_csv(DATA_DIR / "bom.csv")
    }
    plants = {
        row["plant_id"]: {
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
    state = json.loads(STATE_PATH.read_text())
    state_inventory = defaultdict(float)
    for key, qty in state["inventory"].items():
        dc_id, product_id = key.split("|")
        state_inventory[(dc_id, product_id)] = float(qty)
    public_initial_inventory = defaultdict(float)
    for row in read_csv(DATA_DIR / "initial_inventory.csv"):
        public_initial_inventory[(row["dc_id"], row["product_id"])] = to_float(row["initial_inventory_tons"])
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
    policy_row = next(row for row in read_csv(DATA_DIR / "planning_policies.csv") if row["stage"] == "week4")
    policy = {
        "reserve_multiplier": to_float(policy_row["reserve_multiplier_next_week_demand"]),
        "material_discount": to_float(policy_row["material_robust_discount"]),
        "replan_change_penalty": to_float(policy_row["replan_change_penalty_per_ton"]),
    }
    material_supply = {}
    for row in read_csv(DATA_DIR / "material_supply.csv"):
        material_supply[(int(row["week"]), row["plant_id"], row["material_id"])] = {
            "regular_available": to_float(row["regular_available_tons"]),
            "emergency_cap": to_float(row["emergency_cap_tons"]),
        }
    pipeline_product = defaultdict(float)
    pipeline_material = defaultdict(float)
    for row in read_csv(DATA_DIR / "pipeline_receipts.csv"):
        quantity = to_float(row["quantity"])
        if row["receipt_type"] == "product":
            pipeline_product[(int(row["week"]), row["to"], row["item"])] += quantity
        elif row["receipt_type"] == "material":
            pipeline_material[(int(row["week"]), row["to"], row["item"])] += quantity
        else:
            raise ValueError(f"Unsupported pipeline receipt type {row['receipt_type']}")

    customers_by_region = defaultdict(list)
    for customer_id, attrs in customers.items():
        customers_by_region[attrs["region"]].append(customer_id)
    reserve_target = defaultdict(float)
    for idx, week in enumerate(weeks):
        next_week = weeks[idx + 1] if idx + 1 < len(weeks) else None
        for dc_id, dc_attrs in warehouses.items():
            for product_id in products:
                reserve_target[(week, dc_id, product_id)] = 0.0 if next_week is None else policy["reserve_multiplier"] * sum(
                    demand[(next_week, customer_id, product_id)] for customer_id in customers_by_region[dc_attrs["region"]]
                )

    ship_pd_keys = [
        (week, plant_id, dc_id, product_id)
        for week in weeks
        for (plant_id, dc_id) in plant_to_dc
        for product_id in products
        if (plant_id, product_id) in plant_products
    ]
    ship_dc_keys = [
        (week, dc_id, customer_id, product_id)
        for week in weeks
        for (dc_id, customer_id) in dc_to_customer
        for product_id in products
        if demand[(week, customer_id, product_id)] > 0.0
    ]
    transfer_keys = [
        (week, from_dc, to_dc, product_id)
        for week in weeks
        for (from_dc, to_dc) in dc_transfer
        for product_id in products
    ]
    demand_keys = [
        (week, customer_id, product_id)
        for week in weeks
        for customer_id in customers
        for product_id in products
        if demand[(week, customer_id, product_id)] > 0.0
    ]
    emergency_keys = [
        (week, plant_id, material_id)
        for week in weeks
        for plant_id in plants
        for material_id in materials
    ]
    frozen_policies = [
        {
            "variable": row["variable"],
            "start_week": int(row["start_week"]),
            "end_week": int(row["end_week"]),
            "minimum_fraction": to_float(row["minimum_fraction"]),
            "maximum_fraction": to_float(row["maximum_fraction"]),
            "customer_filter": row.get("customer_filter", "all"),
        }
        for row in read_csv(DATA_DIR / "frozen_commitment_policy.csv")
        if row["stage"] == "week4"
    ]

    data = {
        "event_week": event_week,
        "weeks": weeks,
        "products": products,
        "materials": materials,
        "bom": bom,
        "plants": plants,
        "plant_products": plant_products,
        "warehouses": warehouses,
        "initial_inventory": state_inventory,
        "state_inventory": state_inventory,
        "public_initial_inventory": public_initial_inventory,
        "safety_stock": safety_stock,
        "customers": customers,
        "demand": demand,
        "plant_to_dc": plant_to_dc,
        "dc_to_customer": dc_to_customer,
        "dc_transfer": dc_transfer,
        "policy": policy,
        "material_supply": material_supply,
        "pipeline_product": pipeline_product,
        "pipeline_material": pipeline_material,
        "reserve_target": reserve_target,
        "ship_pd_keys": ship_pd_keys,
        "ship_dc_keys": ship_dc_keys,
        "transfer_keys": transfer_keys,
        "demand_keys": demand_keys,
        "emergency_keys": emergency_keys,
        "baseline": load_baseline(weeks),
        "frozen_policies": frozen_policies,
    }
    data["frozen_entries"] = build_frozen_entries(data)
    return data


def build_model(data, time_limit, gap_limit):
    model = Model("cold_chain_week4_replan")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", gap_limit)
    model.setParam("limits/time", time_limit)
    model.setParam("parallel/maxnthreads", 1)

    weeks = data["weeks"]
    products = list(data["products"].keys())
    materials = list(data["materials"].keys())
    plants = list(data["plants"].keys())
    dcs = list(data["warehouses"].keys())
    customers = list(data["customers"].keys())
    feasible_pp = list(data["plant_products"].keys())

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
    ship_pd = {key: model.addVar(lb=0.0, name=f"ship_pd[{','.join(map(str, key))}]") for key in data["ship_pd_keys"]}
    ship_dc = {key: model.addVar(lb=0.0, name=f"ship_dc[{','.join(map(str, key))}]") for key in data["ship_dc_keys"]}
    transfer = {key: model.addVar(lb=0.0, name=f"transfer[{','.join(map(str, key))}]") for key in data["transfer_keys"]}
    inventory = {
        (week, dc_id, product_id): model.addVar(lb=0.0, name=f"inventory[{week},{dc_id},{product_id}]")
        for week in weeks
        for dc_id in dcs
        for product_id in products
    }
    shortage = {key: model.addVar(lb=0.0, name=f"shortage[{','.join(map(str, key))}]") for key in data["demand_keys"]}
    emergency = {
        key: model.addVar(lb=0.0, ub=data["material_supply"][key]["emergency_cap"], name=f"emergency[{','.join(map(str, key))}]")
        for key in data["emergency_keys"]
    }
    prod_change = {
        (week, plant_id, product_id): model.addVar(lb=0.0, name=f"prod_change[{week},{plant_id},{product_id}]")
        for week in weeks
        for (plant_id, product_id) in feasible_pp
    }
    ship_pd_change = {
        key: model.addVar(lb=0.0, name=f"ship_pd_change[{','.join(map(str, key))}]") for key in data["ship_pd_keys"]
    }
    ship_dc_change = {
        key: model.addVar(lb=0.0, name=f"ship_dc_change[{','.join(map(str, key))}]") for key in data["ship_dc_keys"]
    }
    transfer_change = {
        key: model.addVar(lb=0.0, name=f"transfer_change[{','.join(map(str, key))}]") for key in data["transfer_keys"]
    }

    week_index = {week: idx for idx, week in enumerate(weeks)}
    for week in weeks:
        for plant_id, product_id in feasible_pp:
            attrs = data["plant_products"][(plant_id, product_id)]
            total_prod = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            model.addCons(total_prod <= attrs["max_weekly"] * setup[(week, plant_id, product_id)], name=f"max_run[{week},{plant_id},{product_id}]")
            model.addCons(total_prod >= attrs["min_run"] * setup[(week, plant_id, product_id)], name=f"min_run[{week},{plant_id},{product_id}]")

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
                    <= data["policy"]["material_discount"] * supply["regular_available"]
                    + data["pipeline_material"][(week, plant_id, material_id)]
                    + emergency[(week, plant_id, material_id)],
                    name=f"material[{week},{plant_id},{material_id}]",
                )

        for plant_id, product_id in feasible_pp:
            model.addCons(
                quicksum(ship_pd[(week, plant_id, dc_id, product_id)] for dc_id in dcs if (week, plant_id, dc_id, product_id) in ship_pd)
                == prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)],
                name=f"plant_ship_balance[{week},{plant_id},{product_id}]",
            )

        for plant_id, dc_id in data["plant_to_dc"]:
            model.addCons(
                quicksum(ship_pd[(week, plant_id, dc_id, product_id)] for product_id in products if (week, plant_id, dc_id, product_id) in ship_pd)
                <= data["plant_to_dc"][(plant_id, dc_id)]["capacity"],
                name=f"plant_dc_cap[{week},{plant_id},{dc_id}]",
            )
        for dc_id, customer_id in data["dc_to_customer"]:
            model.addCons(
                quicksum(ship_dc[(week, dc_id, customer_id, product_id)] for product_id in products if (week, dc_id, customer_id, product_id) in ship_dc)
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
                model.addCons(shipped + shortage[(week, customer_id, product_id)] == demand_value, name=f"demand_balance[{week},{customer_id},{product_id}]")
                model.addCons(shipped >= data["customers"][customer_id]["min_service_rate"] * demand_value, name=f"service[{week},{customer_id},{product_id}]")

        for dc_id in dcs:
            model.addCons(
                quicksum(inventory[(week, dc_id, product_id)] for product_id in products) <= data["warehouses"][dc_id]["capacity"],
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
                prev_inventory = data["initial_inventory"][(dc_id, product_id)] if week_index[week] == 0 else inventory[(weeks[week_index[week] - 1], dc_id, product_id)]
                model.addCons(
                    inventory[(week, dc_id, product_id)]
                    == prev_inventory
                    + inbound_from_plants
                    + inbound_transfer
                    + data["pipeline_product"][(week, dc_id, product_id)]
                    - outbound_customers
                    - outbound_transfer,
                    name=f"inventory_balance[{week},{dc_id},{product_id}]",
                )
                model.addCons(
                    inventory[(week, dc_id, product_id)] >= data["safety_stock"][(dc_id, product_id)] + data["reserve_target"][(week, dc_id, product_id)],
                    name=f"inventory_floor[{week},{dc_id},{product_id}]",
                )

        for plant_id, product_id in feasible_pp:
            baseline_qty = data["baseline"]["prod_total"].get((week, plant_id, product_id), 0.0)
            total_prod = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            model.addCons(prod_change[(week, plant_id, product_id)] >= total_prod - baseline_qty)
            model.addCons(prod_change[(week, plant_id, product_id)] >= baseline_qty - total_prod)

    for key in data["ship_pd_keys"]:
        baseline_qty = data["baseline"]["ship_pd"].get(key, 0.0)
        model.addCons(ship_pd_change[key] >= ship_pd[key] - baseline_qty)
        model.addCons(ship_pd_change[key] >= baseline_qty - ship_pd[key])
    for key in data["ship_dc_keys"]:
        baseline_qty = data["baseline"]["ship_dc"].get(key, 0.0)
        model.addCons(ship_dc_change[key] >= ship_dc[key] - baseline_qty)
        model.addCons(ship_dc_change[key] >= baseline_qty - ship_dc[key])
    for key in data["transfer_keys"]:
        baseline_qty = data["baseline"]["transfer"].get(key, 0.0)
        model.addCons(transfer_change[key] >= transfer[key] - baseline_qty)
        model.addCons(transfer_change[key] >= baseline_qty - transfer[key])

    for entry in data["frozen_entries"]:
        key = entry["key"]
        baseline_qty = entry["baseline_qty"]
        lower = entry["minimum_fraction"] * baseline_qty
        upper = entry["maximum_fraction"] * baseline_qty
        if entry["variable"] == "ship_plant_dc":
            model.addCons(ship_pd[key] >= lower)
            model.addCons(ship_pd[key] <= upper)
        elif entry["variable"] == "ship_dc_customer":
            model.addCons(ship_dc[key] >= lower)
            model.addCons(ship_dc[key] <= upper)
        elif entry["variable"] == "transfer_dc":
            model.addCons(transfer[key] >= lower)
            model.addCons(transfer[key] <= upper)
        else:
            raise ValueError(f"Unsupported frozen variable {entry['variable']}")

    objective_terms = []
    objective_terms.extend(data["plant_products"][(plant_id, product_id)]["setup_cost"] * setup[(week, plant_id, product_id)] for week in weeks for (plant_id, product_id) in feasible_pp)
    objective_terms.extend(data["plant_products"][(plant_id, product_id)]["regular_cost"] * prod_reg[(week, plant_id, product_id)] for week in weeks for (plant_id, product_id) in feasible_pp)
    objective_terms.extend(data["plant_products"][(plant_id, product_id)]["overtime_cost"] * prod_ot[(week, plant_id, product_id)] for week in weeks for (plant_id, product_id) in feasible_pp)
    objective_terms.extend(data["plant_to_dc"][(plant_id, dc_id)]["cost"] * ship_pd[(week, plant_id, dc_id, product_id)] for (week, plant_id, dc_id, product_id) in data["ship_pd_keys"])
    objective_terms.extend((data["dc_to_customer"][(dc_id, customer_id)]["cost"] + data["warehouses"][dc_id]["handling_cost"]) * ship_dc[(week, dc_id, customer_id, product_id)] for (week, dc_id, customer_id, product_id) in data["ship_dc_keys"])
    objective_terms.extend(data["dc_transfer"][(from_dc, to_dc)]["cost"] * transfer[(week, from_dc, to_dc, product_id)] for (week, from_dc, to_dc, product_id) in data["transfer_keys"])
    objective_terms.extend(data["products"][product_id]["hold_cost"] * inventory[(week, dc_id, product_id)] for week in weeks for dc_id in dcs for product_id in products)
    objective_terms.extend(
        shortage_penalty(data, customer_id, product_id) * shortage[(week, customer_id, product_id)]
        for (week, customer_id, product_id) in data["demand_keys"]
    )
    objective_terms.extend(data["materials"][material_id]["emergency_cost"] * emergency[(week, plant_id, material_id)] for (week, plant_id, material_id) in data["emergency_keys"])
    penalty = data["policy"]["replan_change_penalty"]
    objective_terms.extend(penalty * prod_change[key] for key in prod_change)
    objective_terms.extend(penalty * ship_pd_change[key] for key in ship_pd_change)
    objective_terms.extend(penalty * ship_dc_change[key] for key in ship_dc_change)
    objective_terms.extend(penalty * transfer_change[key] for key in transfer_change)
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


def export_solution(data, model, variables, solution, plan_path):
    rows = []
    products = list(data["products"].keys())
    materials = list(data["materials"].keys())
    for week in data["weeks"]:
        for plant_id, product_id in data["plant_products"]:
            if get_solution_value(model, solution, variables["setup"][(week, plant_id, product_id)]) > 0.5:
                rows.append(("setup", week, product_id, plant_id, "", 1.0))
            reg_value = get_solution_value(model, solution, variables["prod_reg"][(week, plant_id, product_id)])
            if positive(reg_value):
                rows.append(("produce_regular", week, product_id, plant_id, "", reg_value))
            ot_value = get_solution_value(model, solution, variables["prod_ot"][(week, plant_id, product_id)])
            if positive(ot_value):
                rows.append(("produce_overtime", week, product_id, plant_id, "", ot_value))
        for key in data["ship_pd_keys"]:
            if key[0] != week:
                continue
            value = get_solution_value(model, solution, variables["ship_pd"][key])
            if positive(value):
                rows.append(("ship_plant_dc", week, key[3], key[1], key[2], value))
        for key in data["transfer_keys"]:
            if key[0] != week:
                continue
            value = get_solution_value(model, solution, variables["transfer"][key])
            if positive(value):
                rows.append(("transfer_dc", week, key[3], key[1], key[2], value))
        for key in data["ship_dc_keys"]:
            if key[0] != week:
                continue
            value = get_solution_value(model, solution, variables["ship_dc"][key])
            if positive(value):
                rows.append(("ship_dc_customer", week, key[3], key[1], key[2], value))
        for dc_id in data["warehouses"]:
            for product_id in products:
                value = get_solution_value(model, solution, variables["inventory"][(week, dc_id, product_id)])
                if positive(value):
                    rows.append(("inventory", week, product_id, dc_id, "", value))
        for customer_id in data["customers"]:
            for product_id in products:
                key = (week, customer_id, product_id)
                if key not in variables["shortage"]:
                    continue
                value = get_solution_value(model, solution, variables["shortage"][key])
                if positive(value):
                    rows.append(("shortage", week, product_id, customer_id, "", value))
        for plant_id in data["plants"]:
            for material_id in materials:
                value = get_solution_value(model, solution, variables["emergency"][(week, plant_id, material_id)])
                if positive(value):
                    rows.append(("emergency_material", week, material_id, "external", plant_id, value))
    rows.sort(key=lambda row: (row[1], VARIABLE_ORDER[row[0]], row[2], row[3], row[4]))
    export_rows(rows, plan_path)
    return rows


def add_plan_mip_start(model, variables, plan_values):
    solution = model.createSol()
    for key, var in variables["setup"].items():
        model.setSolVal(solution, var, plan_values["setup"].get(key, 0.0))
    for key, var in variables["prod_reg"].items():
        model.setSolVal(solution, var, plan_values["prod_reg"].get(key, 0.0))
    for key, var in variables["prod_ot"].items():
        model.setSolVal(solution, var, plan_values["prod_ot"].get(key, 0.0))
    for key, var in variables["ship_pd"].items():
        model.setSolVal(solution, var, plan_values["ship_pd"].get(key, 0.0))
    for key, var in variables["ship_dc"].items():
        model.setSolVal(solution, var, plan_values["ship_dc"].get(key, 0.0))
    for key, var in variables["transfer"].items():
        model.setSolVal(solution, var, plan_values["transfer"].get(key, 0.0))
    for key, var in variables["inventory"].items():
        model.setSolVal(solution, var, plan_values["inventory"].get(key, 0.0))
    for key, var in variables["shortage"].items():
        model.setSolVal(solution, var, plan_values["shortage"].get(key, 0.0))
    for key, var in variables["emergency"].items():
        model.setSolVal(solution, var, plan_values["emergency"].get(key, 0.0))
    return model.addSol(solution, free=True)


def derive_public_week4_inventory(data):
    prior_week = data["event_week"] - 1
    prior_rows = [row for row in parse_plan(BASELINE_PLAN_PATH)[1] if int(row["week"]) == prior_week]
    if not prior_rows:
        raise RuntimeError("Accepted step-2 replan does not contain the executed week needed to derive public state")

    inventory = defaultdict(float)
    for row in prior_rows:
        if row["variable"] == "inventory":
            inventory[(row["from"], row["item"])] += float(row["quantity"])

    missing_inventory = []
    state_match_max = 0.0
    state_mismatches = []
    for dc_id in data["warehouses"]:
        for product_id in data["products"]:
            key = (dc_id, product_id)
            if key not in inventory:
                missing_inventory.append(key)
            raw = abs(inventory[key] - data["state_inventory"][key])
            state_match_max = max(state_match_max, raw)
            if raw > FEAS_TOL:
                state_mismatches.append((dc_id, product_id, inventory[key], data["state_inventory"][key]))

    return {
        "inventory": inventory,
        "prior_week": prior_week,
        "prior_inventory_ok": not missing_inventory,
        "prior_inventory_missing": missing_inventory,
        "state_match_max_raw": state_match_max,
        "state_match_ok": state_match_max <= FEAS_TOL,
        "state_mismatches": state_mismatches,
    }


def audit_solution(data, plan_path, initial_inventory_override=None):
    fieldnames, rows = parse_plan(plan_path)
    errors = []
    if fieldnames != ["variable", "week", "item", "from", "to", "quantity"]:
        errors.append(f"Invalid header: {fieldnames}")

    setup = defaultdict(float)
    prod_reg = defaultdict(float)
    prod_ot = defaultdict(float)
    ship_pd = defaultdict(float)
    ship_dc = defaultdict(float)
    transfer = defaultdict(float)
    inventory = defaultdict(float)
    shortage = defaultdict(float)
    emergency = defaultdict(float)
    variable_counts = Counter()
    integrality_residual = 0.0
    seen_keys = set()

    product_ids = set(data["products"].keys())
    material_ids = set(data["materials"].keys())
    plant_ids = set(data["plants"].keys())
    dc_ids = set(data["warehouses"].keys())
    customer_ids = set(data["customers"].keys())

    for row in rows:
        variable = row["variable"]
        week = int(row["week"])
        item = row["item"]
        from_node = row["from"]
        to_node = row["to"]
        quantity = float(row["quantity"])
        key = (variable, week, item, from_node, to_node)
        if key in seen_keys:
            errors.append(f"Duplicate row key {key}")
        seen_keys.add(key)
        if week not in set(data["weeks"]):
            errors.append(f"Unexpected week in row {key}")
        if quantity < -VALUE_EPS:
            errors.append(f"Negative quantity in row {key}")
        if quantity <= VALUE_EPS:
            errors.append(f"Zero row should be omitted {key}")
        variable_counts[variable] += 1
        if variable == "setup":
            if item not in product_ids or from_node not in plant_ids or to_node != "":
                errors.append(f"Invalid setup row {key}")
            setup[(week, from_node, item)] = quantity
            integrality_residual = max(integrality_residual, abs(quantity - round(quantity)))
        elif variable == "produce_regular":
            if item not in product_ids or from_node not in plant_ids or to_node != "":
                errors.append(f"Invalid produce_regular row {key}")
            prod_reg[(week, from_node, item)] = quantity
        elif variable == "produce_overtime":
            if item not in product_ids or from_node not in plant_ids or to_node != "":
                errors.append(f"Invalid produce_overtime row {key}")
            prod_ot[(week, from_node, item)] = quantity
        elif variable == "ship_plant_dc":
            if item not in product_ids or from_node not in plant_ids or to_node not in dc_ids:
                errors.append(f"Invalid ship_plant_dc row {key}")
            if (from_node, to_node) not in data["plant_to_dc"]:
                errors.append(f"Unsupported plant-to-DC lane {key}")
            ship_pd[(week, from_node, to_node, item)] = quantity
        elif variable == "ship_dc_customer":
            if item not in product_ids or from_node not in dc_ids or to_node not in customer_ids:
                errors.append(f"Invalid ship_dc_customer row {key}")
            if (from_node, to_node) not in data["dc_to_customer"]:
                errors.append(f"Unsupported DC-to-customer lane {key}")
            ship_dc[(week, from_node, to_node, item)] = quantity
        elif variable == "transfer_dc":
            if item not in product_ids or from_node not in dc_ids or to_node not in dc_ids or from_node == to_node:
                errors.append(f"Invalid transfer_dc row {key}")
            if (from_node, to_node) not in data["dc_transfer"]:
                errors.append(f"Unsupported DC transfer lane {key}")
            transfer[(week, from_node, to_node, item)] = quantity
        elif variable == "inventory":
            if item not in product_ids or from_node not in dc_ids or to_node != "":
                errors.append(f"Invalid inventory row {key}")
            inventory[(week, from_node, item)] = quantity
        elif variable == "shortage":
            if item not in product_ids or from_node not in customer_ids or to_node != "":
                errors.append(f"Invalid shortage row {key}")
            shortage[(week, from_node, item)] = quantity
        elif variable == "emergency_material":
            if item not in material_ids or from_node != "external" or to_node not in plant_ids:
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

    weeks = data["weeks"]
    week_index = {week: idx for idx, week in enumerate(weeks)}
    initial_inventory = data["initial_inventory"] if initial_inventory_override is None else initial_inventory_override
    products = list(data["products"].keys())
    materials = list(data["materials"].keys())
    plants = list(data["plants"].keys())
    dcs = list(data["warehouses"].keys())
    customers = list(data["customers"].keys())

    for week in weeks:
        for plant_id, product_id in data["plant_products"]:
            attrs = data["plant_products"][(plant_id, product_id)]
            total_prod = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            record("setup_upper", total_prod, attrs["max_weekly"] * setup[(week, plant_id, product_id)], "le")
            record("setup_lower", total_prod, attrs["min_run"] * setup[(week, plant_id, product_id)], "ge")
            baseline_total = data["baseline"]["prod_total"].get((week, plant_id, product_id), 0.0)
            record("prod_replan_abs", abs(total_prod - baseline_total), abs(total_prod - baseline_total), "eq")
        for plant_id in plants:
            record(
                "regular_hours",
                sum(
                    prod_reg[(week, plant_id, product_id)] / data["plant_products"][(plant_id, product_id)]["tons_per_hour"]
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                ),
                data["plants"][plant_id]["regular_hours"],
                "le",
            )
            record(
                "overtime_hours",
                sum(
                    prod_ot[(week, plant_id, product_id)] / data["plant_products"][(plant_id, product_id)]["tons_per_hour"]
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                ),
                data["plants"][plant_id]["overtime_hours"],
                "le",
            )
            record(
                "setup_hours",
                sum(
                    data["plant_products"][(plant_id, product_id)]["setup_hours"] * setup[(week, plant_id, product_id)]
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                ),
                data["plants"][plant_id]["setup_team_hours"],
                "le",
            )
            for material_id in materials:
                material_use = sum(
                    data["bom"].get((product_id, material_id), 0.0) * (prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)])
                    for product_id in products
                    if (plant_id, product_id) in data["plant_products"]
                )
                supply = data["material_supply"][(week, plant_id, material_id)]
                record(
                    "material_balance",
                    material_use,
                    data["policy"]["material_discount"] * supply["regular_available"] + data["pipeline_material"][(week, plant_id, material_id)] + emergency[(week, plant_id, material_id)],
                    "le",
                )
                record("emergency_cap", emergency[(week, plant_id, material_id)], supply["emergency_cap"], "le")
        for plant_id, dc_id in data["plant_to_dc"]:
            record("plant_dc_lane", sum(ship_pd[(week, plant_id, dc_id, product_id)] for product_id in products), data["plant_to_dc"][(plant_id, dc_id)]["capacity"], "le")
        for dc_id, customer_id in data["dc_to_customer"]:
            record("dc_customer_lane", sum(ship_dc[(week, dc_id, customer_id, product_id)] for product_id in products), data["dc_to_customer"][(dc_id, customer_id)]["capacity"], "le")
        for from_dc, to_dc in data["dc_transfer"]:
            record("dc_transfer_lane", sum(transfer[(week, from_dc, to_dc, product_id)] for product_id in products), data["dc_transfer"][(from_dc, to_dc)]["capacity"], "le")
        for customer_id in customers:
            for product_id in products:
                demand_value = data["demand"][(week, customer_id, product_id)]
                if demand_value <= 0.0:
                    continue
                shipped = sum(ship_dc[(week, dc_id, customer_id, product_id)] for dc_id in dcs if (dc_id, customer_id) in data["dc_to_customer"])
                record("demand_balance", shipped + shortage[(week, customer_id, product_id)], demand_value, "eq")
                record("service_floor", shipped, data["customers"][customer_id]["min_service_rate"] * demand_value, "ge")
        for plant_id, product_id in data["plant_products"]:
            produced = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
            shipped = sum(ship_pd[(week, plant_id, dc_id, product_id)] for dc_id in dcs if (plant_id, dc_id) in data["plant_to_dc"])
            record("plant_ship_balance", shipped, produced, "eq")
        for dc_id in dcs:
            record("warehouse_capacity", sum(inventory[(week, dc_id, product_id)] for product_id in products), data["warehouses"][dc_id]["capacity"], "le")
            for product_id in products:
                prev_inventory = initial_inventory[(dc_id, product_id)] if week_index[week] == 0 else inventory[(weeks[week_index[week] - 1], dc_id, product_id)]
                inbound = sum(ship_pd[(week, plant_id, dc_id, product_id)] for plant_id in plants if (plant_id, dc_id) in data["plant_to_dc"])
                inbound += sum(transfer[(week, from_dc, dc_id, product_id)] for from_dc in dcs if (from_dc, dc_id) in data["dc_transfer"])
                outbound = sum(ship_dc[(week, dc_id, customer_id, product_id)] for customer_id in customers if (dc_id, customer_id) in data["dc_to_customer"])
                outbound += sum(transfer[(week, dc_id, to_dc, product_id)] for to_dc in dcs if (dc_id, to_dc) in data["dc_transfer"])
                rhs = prev_inventory + inbound + data["pipeline_product"][(week, dc_id, product_id)] - outbound
                record("inventory_balance", inventory[(week, dc_id, product_id)], rhs, "eq")
                record("inventory_floor", inventory[(week, dc_id, product_id)], data["safety_stock"][(dc_id, product_id)] + data["reserve_target"][(week, dc_id, product_id)], "ge")

        for entry in data["frozen_entries"]:
            key = entry["key"]
            if key[0] != week:
                continue
            lower = entry["minimum_fraction"] * entry["baseline_qty"]
            upper = entry["maximum_fraction"] * entry["baseline_qty"]
            if entry["variable"] == "ship_plant_dc":
                record("frozen_ship_pd_lb", ship_pd[key], lower, "ge")
                record("frozen_ship_pd_ub", ship_pd[key], upper, "le")
            elif entry["variable"] == "ship_dc_customer":
                record("frozen_ship_dc_lb", ship_dc[key], lower, "ge")
                record("frozen_ship_dc_ub", ship_dc[key], upper, "le")
            elif entry["variable"] == "transfer_dc":
                record("frozen_transfer_lb", transfer[key], lower, "ge")
                record("frozen_transfer_ub", transfer[key], upper, "le")

    cost_breakdown = defaultdict(float)
    for (week, plant_id, product_id), value in setup.items():
        cost_breakdown["setup_cost"] += data["plant_products"][(plant_id, product_id)]["setup_cost"] * value
    for (week, plant_id, product_id), value in prod_reg.items():
        cost_breakdown["regular_production_cost"] += data["plant_products"][(plant_id, product_id)]["regular_cost"] * value
    for (week, plant_id, product_id), value in prod_ot.items():
        cost_breakdown["overtime_production_cost"] += data["plant_products"][(plant_id, product_id)]["overtime_cost"] * value
    for key in data["ship_pd_keys"]:
        cost_breakdown["plant_to_dc_cost"] += data["plant_to_dc"][(key[1], key[2])]["cost"] * ship_pd[key]
        cost_breakdown["replan_change_cost"] += data["policy"]["replan_change_penalty"] * abs(ship_pd[key] - data["baseline"]["ship_pd"].get(key, 0.0))
    for key in data["ship_dc_keys"]:
        cost_breakdown["dc_to_customer_cost"] += data["dc_to_customer"][(key[1], key[2])]["cost"] * ship_dc[key]
        cost_breakdown["handling_cost"] += data["warehouses"][key[1]]["handling_cost"] * ship_dc[key]
        cost_breakdown["replan_change_cost"] += data["policy"]["replan_change_penalty"] * abs(ship_dc[key] - data["baseline"]["ship_dc"].get(key, 0.0))
    for key in data["transfer_keys"]:
        cost_breakdown["transfer_cost"] += data["dc_transfer"][(key[1], key[2])]["cost"] * transfer[key]
        cost_breakdown["replan_change_cost"] += data["policy"]["replan_change_penalty"] * abs(transfer[key] - data["baseline"]["transfer"].get(key, 0.0))
    for week, plant_id, product_id in [(w, p, k) for w in weeks for (p, k) in data["plant_products"]]:
        total_prod = prod_reg[(week, plant_id, product_id)] + prod_ot[(week, plant_id, product_id)]
        cost_breakdown["replan_change_cost"] += data["policy"]["replan_change_penalty"] * abs(total_prod - data["baseline"]["prod_total"].get((week, plant_id, product_id), 0.0))
    for (week, dc_id, product_id), value in inventory.items():
        cost_breakdown["holding_cost"] += data["products"][product_id]["hold_cost"] * value
    for (week, customer_id, product_id), value in shortage.items():
        cost_breakdown["shortage_cost"] += shortage_penalty(data, customer_id, product_id) * value
    for (week, plant_id, material_id), value in emergency.items():
        cost_breakdown["emergency_material_cost"] += data["materials"][material_id]["emergency_cost"] * value

    theta = max(residuals.values()) if residuals else 0.0
    violated_groups = [group for group, residual in residuals.items() if residual > FEAS_TOL]
    if integrality_residual > FEAS_TOL:
        errors.append(f"Integrality residual {integrality_residual}")
    return {
        "rows": len(rows),
        "errors": errors,
        "theta": theta,
        "residuals": dict(sorted(residuals.items())),
        "raw_residuals": dict(sorted(raw_residuals.items())),
        "violated_groups": violated_groups,
        "integrality_residual": integrality_residual,
        "cost_breakdown": dict(cost_breakdown),
        "variable_counts": dict(sorted(variable_counts.items())),
    }


def audit_passes(audit):
    return not audit["errors"] and audit["theta"] <= FEAS_TOL and not audit["violated_groups"]


def find_frozen_window_violations(data, plan_path):
    plan_values = load_plan_values(plan_path, data["weeks"])
    violations = []
    for entry in data["frozen_entries"]:
        key = entry["key"]
        baseline_qty = entry["baseline_qty"]
        lower = entry["minimum_fraction"] * baseline_qty
        upper = entry["maximum_fraction"] * baseline_qty
        if entry["variable"] == "ship_plant_dc":
            value = plan_values["ship_pd"].get(key, 0.0)
        elif entry["variable"] == "ship_dc_customer":
            value = plan_values["ship_dc"].get(key, 0.0)
        elif entry["variable"] == "transfer_dc":
            value = plan_values["transfer"].get(key, 0.0)
        else:
            continue
        if value < lower - FEAS_TOL or value > upper + FEAS_TOL:
            violations.append(
                {
                    "variable": entry["variable"],
                    "week": key[0],
                    "item": key[3],
                    "from": key[1],
                    "to": key[2],
                    "baseline_qty": baseline_qty,
                    "lower": lower,
                    "upper": upper,
                    "value": value,
                }
            )
    return violations


def write_model_md(data, report, final_audit):
    incumbent_text = "none" if report["incumbent_objective"] is None else f"{report['incumbent_objective']:.8f}"
    lines = [
        "# Week 4 Recovery Model",
        "",
        "## Scope",
        "",
        "This PySCIPOpt mixed-integer model replans weeks 4-10 from the executed week-3 state.",
        "The solve uses `week4_state.json` and independently checks that it matches the accepted `step2_replan.csv` week-3 terminal inventory.",
        "It preserves the approved week-2 baseline where the frozen policy requires it, treats pipeline receipts as mandatory inbound quantities, and prices absolute changes from the approved plan.",
        "",
        "## Decision Variables",
        "",
        "- `setup[t,p,k] in {0,1}` for feasible plant-product runs.",
        "- `produce_regular[t,p,k] >= 0` and `produce_overtime[t,p,k] >= 0`.",
        "- `ship_plant_dc[t,p,d,k] >= 0`, `ship_dc_customer[t,d,c,k] >= 0`, and `transfer_dc[t,d1,d2,k] >= 0` on listed lanes only.",
        "- `inventory[t,d,k] >= 0`, `shortage[t,c,k] >= 0`, and `emergency_material[t,p,m] >= 0`.",
        "- Auxiliary absolute-deviation variables for production, plant-to-DC shipments, DC-to-customer shipments, and DC transfers.",
        "",
        "## Objective",
        "",
        "Minimize operating cost plus replanning disruption cost:",
        "",
        "- setup, regular production, overtime production, emergency material, transportation, handling, transfer, holding, and shortage costs",
        "- strategic shortage penalties for all public-health customers and for hospital demand of `V_MRNA` and `V_PED` during the priority window",
        f"- replan penalty `{data['policy']['replan_change_penalty']}` per unit of absolute deviation from `/app/submissions/step2_replan.csv` for production totals, plant-to-DC shipments, DC-to-customer shipments, and DC transfers",
        "",
        "## Hard Constraints",
        "",
        "- setup activation with min/max run sizes",
        "- plant regular hours, overtime hours, and setup-team hours",
        "- material balance with the week-4 robustness discount, emergency purchases, and committed pipeline material receipts",
        "- same-week plant shipment balance",
        "- plant-to-DC, DC-to-customer, and DC-transfer lane capacities",
        "- exact demand balance with per-row service floors",
        "- DC inventory balance from the executed week-3 state, including committed product pipeline receipts",
        "- warehouse freezer capacity, including the reduced west-freezer capacity, and safety-stock plus next-week regional reserve",
        "- frozen commitment bounds from `frozen_commitment_policy.csv` on the full public decision domain, using approved baseline quantities including zero-baseline keys, and including frozen transfers",
        "",
        "## Assumptions",
        "",
        "- `customer_filter` values such as `hospital` are interpreted as customer-segment filters, not literal customer IDs.",
        "- Replanning cost for production is measured on total weekly plant-product output (`regular + overtime`) versus the approved baseline output.",
        "- Pipeline receipts are committed state transitions, not optional decision rows, and therefore do not appear in the exported CSV.",
        "- The week-4 priority window is modeled by applying the strategic shortage penalty to hospital demand for `V_MRNA` and `V_PED`; `MOBILE_FIELD` already receives the strategic public-health penalty.",
        "",
        "## Final Incumbent",
        "",
        f"- Published artifact result: `{report['publication_result']}`",
        f"- Rollback incumbent objective: `{incumbent_text}`",
        f"- Candidate objective: `{report['candidate_objective'] if report['candidate_objective'] is not None else 'none'}`",
        f"- Rollback incumbent public audit pass: `{'yes' if report['incumbent_audit_pass'] else 'no'}`",
        f"- Candidate public audit pass: `{'yes' if report['candidate_audit_pass'] else 'no'}`",
        f"- Published artifact public audit pass: `{'yes' if report['published_audit_pass'] else 'no'}`",
        f"- Solve status: `{report['solve_status']}`",
        f"- Solver incumbent count: `{report['n_solutions']}`",
        f"- Audited total objective: `{sum(final_audit['cost_breakdown'].values()):.8f}`",
        f"- Max normalized hard residual theta: `{final_audit['theta']:.8e}`",
        f"- Violated hard groups: `{len(final_audit['violated_groups'])}`",
        f"- Relative gap at stop: `{report['gap']:.8f}`",
    ]
    MODEL_PATH.write_text("\n".join(lines) + "\n")


def audit_rejects_corruption(data, plan_path):
    fieldnames, rows = parse_plan(plan_path)
    corrupted_path = SUBMISSIONS_DIR / "_step3_replan_corrupted.csv"
    with open(corrupted_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows[1:] if len(rows) > 1 else rows)
    audit = audit_solution(data, corrupted_path, initial_inventory_override=data["public_state"]["inventory"])
    corrupted_path.unlink(missing_ok=True)
    return not audit_passes(audit)


def write_log(data, report, baseline_audit, incumbent_audit, final_audit):
    total = sum(final_audit["cost_breakdown"].values())
    incumbent_audit_text = "n/a" if incumbent_audit is None else ("yes" if audit_passes(incumbent_audit) else "no")
    incumbent_objective_text = "none" if report["incumbent_objective"] is None else f"{report['incumbent_objective']:.8f}"
    lines = [
        "# Week 4 Replan Solve Log",
        "",
        "## Restoration Audit",
        "",
        f"- Baseline suffix theta under week-4 data: `{baseline_audit['theta']:.8e}`",
        f"- Baseline suffix violated groups: `{len(baseline_audit['violated_groups'])}`",
        f"- Baseline violated families: `{', '.join(baseline_audit['violated_groups']) if baseline_audit['violated_groups'] else 'none'}`",
        f"- Baseline suffix audit pass: `{'yes' if audit_passes(baseline_audit) else 'no'}`",
        "",
        "## Repair Findings",
        "",
        f"- Rollback incumbent frozen-window violations reproduced: `{len(report['incumbent_frozen_violations'])}`",
    ]
    for violation in report["incumbent_frozen_violations"]:
        lines.append(
            f"- `{violation['variable']}` week `{violation['week']}` `{violation['item']}` `{violation['from']}` -> `{violation['to']}` "
            f"baseline=`{violation['baseline_qty']:.8f}` allowed=`[{violation['lower']:.8f}, {violation['upper']:.8f}]` actual=`{violation['value']:.8f}`"
        )
    lines.extend([
        "",
        "## Public State Audit",
        "",
        f"- Accepted step-2 prior-week inventory rows complete: `{'yes' if data['public_state']['prior_inventory_ok'] else 'no'}`",
        f"- Prior-week used for state bridge: `{data['public_state']['prior_week']}`",
        f"- Accepted/prior-inventory vs provided-state max raw diff: `{data['public_state']['state_match_max_raw']:.8e}`",
        f"- Current rollback incumbent audit pass: `{incumbent_audit_text}`",
        f"- Rollback incumbent objective: `{incumbent_objective_text}`",
        "",
        "## Performance Run",
        "",
        f"- MIP start accepted: `{'yes' if report['mip_start_accepted'] else 'no'}`",
        f"- Solve status: `{report['solve_status']}`",
        f"- Time limit seconds: `{report['time_limit']}`",
        f"- Solve time seconds: `{report['solve_time']:.4f}`",
        f"- Solver incumbent count: `{report['n_solutions']}`",
        f"- Best bound: `{report['best_bound']:.8f}`",
        f"- Relative gap: `{report['gap']:.8f}`",
        f"- Candidate objective: `{report['candidate_objective'] if report['candidate_objective'] is not None else 'none'}`",
        f"- Candidate audit pass: `{'yes' if report['candidate_audit_pass'] else 'no'}`",
        f"- Publication result: `{report['publication_result']}`",
        "",
        "## Final Audit",
        "",
        f"- Published artifact audit pass: `{'yes' if report['published_audit_pass'] else 'no'}`",
        f"- Exported rows: `{final_audit['rows']}`",
        f"- Theta: `{final_audit['theta']:.8e}`",
        f"- Violated groups: `{len(final_audit['violated_groups'])}`",
        f"- Integrality residual: `{final_audit['integrality_residual']:.8e}`",
        f"- Decision coverage: `weeks={len(data['weeks'])}` `ship_pd_keys={len(data['ship_pd_keys'])}` `ship_dc_keys={len(data['ship_dc_keys'])}` `transfer_keys={len(data['transfer_keys'])}`",
        f"- Corrupted-copy rejection test: `{'passed' if report['corruption_rejected'] else 'failed'}`",
        "",
        "## Objective Breakdown",
        "",
    ])
    for name, value in sorted(final_audit["cost_breakdown"].items()):
        lines.append(f"- `{name}`: `{value:.8f}`")
    lines.extend([
        f"- `recomputed_total`: `{total:.8f}`",
        "",
        "## Variable Counts",
        "",
    ])
    for name, value in final_audit["variable_counts"].items():
        lines.append(f"- `{name}`: `{value}`")
    lines.extend([
        "",
        "## Constraint Residuals",
        "",
    ])
    for name, value in final_audit["residuals"].items():
        lines.append(f"- `{name}`: normalized=`{value:.8e}` raw=`{final_audit['raw_residuals'][name]:.8e}`")
    LOG_PATH.write_text("\n".join(lines) + "\n")


def write_active_baseline_file(data):
    temp_path = SUBMISSIONS_DIR / "_baseline_week4_suffix.csv"
    rows = [
        (row["variable"], int(row["week"]), row["item"], row["from"], row["to"], float(row["quantity"]))
        for row in data["baseline"]["rows"]
    ]
    rows.sort(key=lambda row: (row[1], VARIABLE_ORDER[row[0]], row[2], row[3], row[4]))
    export_rows(rows, temp_path)
    return temp_path


def main():
    data = load_inputs()
    data["public_state"] = derive_public_week4_inventory(data)
    baseline_path = write_active_baseline_file(data)
    baseline_audit = audit_solution(data, baseline_path, initial_inventory_override=data["public_state"]["inventory"])
    baseline_path.unlink(missing_ok=True)

    if not data["public_state"]["prior_inventory_ok"]:
        raise RuntimeError("Accepted step-2 artifact does not provide complete week-3 terminal inventory rows")
    if not data["public_state"]["state_match_ok"]:
        raise RuntimeError("Provided week-4 state does not match the accepted step-2 terminal inventory")

    incumbent_plan_values = None
    incumbent_audit = None
    incumbent_objective = None
    incumbent_frozen_violations = []
    if PLAN_PATH.exists():
        incumbent_plan_values = load_plan_values(PLAN_PATH, data["weeks"])
        incumbent_audit = audit_solution(data, PLAN_PATH, initial_inventory_override=data["public_state"]["inventory"])
        incumbent_objective = sum(incumbent_audit["cost_breakdown"].values())
        incumbent_frozen_violations = find_frozen_window_violations(data, PLAN_PATH)
    if incumbent_plan_values is None and audit_passes(baseline_audit):
        rows = [
            (row["variable"], int(row["week"]), row["item"], row["from"], row["to"], float(row["quantity"]))
            for row in data["baseline"]["rows"]
        ]
        rows.sort(key=lambda row: (row[1], VARIABLE_ORDER[row[0]], row[2], row[3], row[4]))
        export_rows(rows, PLAN_PATH)
        incumbent_plan_values = load_plan_values(PLAN_PATH, data["weeks"])
        incumbent_audit = audit_solution(data, PLAN_PATH, initial_inventory_override=data["public_state"]["inventory"])
        incumbent_objective = sum(incumbent_audit["cost_breakdown"].values())

    model, variables = build_model(data, get_time_limit(), gap_limit=0.0)
    mip_start_accepted = add_plan_mip_start(model, variables, incumbent_plan_values or data["baseline"])

    start_time = time.time()
    model.optimize()
    solve_time = time.time() - start_time
    status = str(model.getStatus())
    n_solutions = model.getNSols()
    candidate_objective = None
    candidate_audit_pass = False
    publication_result = "rollback incumbent kept"
    if n_solutions > 0 and model.getBestSol() is not None:
        export_solution(data, model, variables, model.getBestSol(), CANDIDATE_PLAN_PATH)
        candidate_audit = audit_solution(data, CANDIDATE_PLAN_PATH, initial_inventory_override=data["public_state"]["inventory"])
        candidate_objective = sum(candidate_audit["cost_breakdown"].values())
        candidate_audit_pass = audit_passes(candidate_audit)
        if candidate_audit_pass and (incumbent_objective is None or candidate_objective + VALUE_EPS < incumbent_objective):
            CANDIDATE_PLAN_PATH.replace(PLAN_PATH)
            publication_result = "candidate promoted" if incumbent_objective is not None else "candidate published"
        else:
            CANDIDATE_PLAN_PATH.unlink(missing_ok=True)
            if candidate_audit_pass:
                publication_result = "rollback incumbent kept; no strict audited improvement"
            else:
                publication_result = "rollback incumbent kept; candidate failed audit" if incumbent_objective is not None else "no publication; candidate failed audit"
    elif incumbent_objective is not None:
        publication_result = "rollback incumbent kept; solver found no candidate"

    final_audit = audit_solution(data, PLAN_PATH, initial_inventory_override=data["public_state"]["inventory"])
    published_audit_pass = audit_passes(final_audit)
    if publication_result.startswith("candidate") and not published_audit_pass:
        raise RuntimeError("Published artifact failed the public independent audit")
    if incumbent_objective is None and not candidate_audit_pass:
        raise RuntimeError("No feasible incumbent or candidate was available for publication")

    report = {
        "mip_start_accepted": mip_start_accepted,
        "solve_status": status,
        "time_limit": get_time_limit(),
        "solve_time": solve_time,
        "n_solutions": n_solutions,
        "best_bound": model.getDualbound(),
        "gap": model.getGap(),
        "incumbent_objective": incumbent_objective,
        "incumbent_audit_pass": False if incumbent_audit is None else audit_passes(incumbent_audit),
        "incumbent_frozen_violations": incumbent_frozen_violations,
        "candidate_objective": None if candidate_objective is None else f"{candidate_objective:.8f}",
        "candidate_audit_pass": candidate_audit_pass,
        "published_audit_pass": published_audit_pass,
        "publication_result": publication_result,
        "corruption_rejected": audit_rejects_corruption(data, PLAN_PATH),
    }
    write_model_md(data, report, final_audit)
    write_log(data, report, baseline_audit, incumbent_audit, final_audit)


if __name__ == "__main__":
    main()
