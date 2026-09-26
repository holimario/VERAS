import csv
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
OBJECTIVE_IMPROVEMENT_EPS = 1e-7


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def parse_float(value):
    return float(value) if value not in ("", None) else 0.0


def parse_int(value):
    return int(value) if value not in ("", None) else 0


def format_quantity(value):
    if abs(value) <= 5e-10:
        value = 0.0
    text = f"{value:.8f}"
    text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def load_data():
    config = json.loads((DATA_DIR / "config.json").read_text())

    months_raw = read_csv(DATA_DIR / "months.csv")
    months_sorted = sorted(months_raw, key=lambda row: parse_int(row["period_index"]))
    months = [row["month"] for row in months_sorted]
    month_pos = {month: idx for idx, month in enumerate(months)}

    products_raw = read_csv(DATA_DIR / "products.csv")
    products = [row["product_id"] for row in products_raw]
    product_family = {row["product_id"]: row["product_family"] for row in products_raw}
    unit_price = {row["product_id"]: parse_float(row["unit_price"]) for row in products_raw}
    raw_material_cost = {
        row["product_id"]: parse_float(row["raw_material_cost"]) for row in products_raw
    }

    channels_raw = read_csv(DATA_DIR / "channels.csv")
    channels = [row["channel_id"] for row in channels_raw]
    price_multiplier = {
        row["channel_id"]: parse_float(row["price_multiplier"]) for row in channels_raw
    }

    machines_raw = read_csv(DATA_DIR / "machines.csv")
    machines = [row["machine_id"] for row in machines_raw]
    machine_stage = {row["machine_id"]: row["stage"] for row in machines_raw}
    regular_cost = {
        row["machine_id"]: parse_float(row["regular_cost_per_hour"]) for row in machines_raw
    }
    overtime_premium = {
        row["machine_id"]: parse_float(row["overtime_premium_per_hour"]) for row in machines_raw
    }

    routes_raw = read_csv(DATA_DIR / "process_routes.csv")
    prod_keys = []
    hours_per_unit = {}
    a_routes = defaultdict(list)
    b_routes = defaultdict(list)
    for row in routes_raw:
        stage = row["stage"]
        machine = row["machine_id"]
        product = row["product_id"]
        key = (stage, machine, product)
        prod_keys.append(key)
        hours_per_unit[key] = parse_float(row["hours_per_unit"])
        if stage == "A":
            a_routes[product].append(machine)
        elif stage == "B":
            b_routes[product].append(machine)
        else:
            raise ValueError(f"Unknown stage {stage} in process_routes.csv")

    machine_capacity = {}
    for row in read_csv(DATA_DIR / "machine_month_capacity.csv"):
        machine_capacity[(row["month"], row["machine_id"])] = {
            "regular": parse_float(row["regular_hours"]),
            "overtime": parse_float(row["overtime_hours"]),
        }

    materials = [row["material_id"] for row in read_csv(DATA_DIR / "materials.csv")]
    bom = defaultdict(float)
    for row in read_csv(DATA_DIR / "material_bom.csv"):
        bom[(row["product_id"], row["material_id"])] = parse_float(
            row["units_per_finished_unit"]
        )

    material_avail = defaultdict(float)
    for row in read_csv(DATA_DIR / "material_availability.csv"):
        material_avail[(row["month"], row["material_id"])] = parse_float(
            row["available_units"]
        )

    material_reserve = defaultdict(float)
    for row in read_csv(DATA_DIR / "material_reserve_rules.csv"):
        material_reserve[(row["month"], row["material_id"])] = parse_float(
            row["min_unused_units"]
        )

    demand = {}
    for row in read_csv(DATA_DIR / "demand.csv"):
        demand[(row["month"], row["product_id"], row["channel_id"])] = parse_float(
            row["max_demand_units"]
        )

    service_fraction = defaultdict(float)
    for row in read_csv(DATA_DIR / "channel_service_rules.csv"):
        service_fraction[(row["product_id"], row["channel_id"])] = parse_float(
            row["min_service_fraction"]
        )

    setup_cost = {}
    min_campaign = {}
    max_campaign = {}
    for row in read_csv(DATA_DIR / "setup_rules.csv"):
        product = row["product_id"]
        setup_cost[product] = parse_float(row["setup_cost"])
        min_campaign[product] = parse_float(row["min_campaign_units"])
        max_campaign[product] = parse_float(row["max_campaign_units"])

    setup_hours = defaultdict(float)
    for row in read_csv(DATA_DIR / "machine_setup_hours.csv"):
        setup_hours[(row["stage"], row["machine_id"], row["product_id"])] = parse_float(
            row["setup_hours"]
        )

    initial_inventory = {}
    max_inventory = {}
    final_inventory = {}
    holding_cost = {}
    for row in read_csv(DATA_DIR / "finished_goods_inventory_policy.csv"):
        product = row["product_id"]
        initial_inventory[product] = parse_float(row["initial_inventory"])
        max_inventory[product] = parse_float(row["max_ending_inventory"])
        final_inventory[product] = parse_float(row["final_min_inventory"])
        holding_cost[product] = parse_float(row["holding_cost_per_unit_month"])

    release_lag = {}
    max_pipeline = {}
    for row in read_csv(DATA_DIR / "quality_release_rules.csv"):
        product = row["product_id"]
        release_lag[product] = parse_int(row["release_lag_months"])
        max_pipeline[product] = parse_float(row["max_unreleased_good_units"])

    yield_rate = {}
    for row in read_csv(DATA_DIR / "route_yield_rates.csv"):
        yield_rate[(row["machine_id"], row["product_id"])] = parse_float(row["good_yield"])

    initial_active = {}
    cold_crew = {}
    continuation_crew = {}
    restart_penalty = {}
    min_run = {}
    for row in read_csv(DATA_DIR / "campaign_transition_rules.csv"):
        product = row["product_id"]
        initial_active[product] = parse_int(row["initial_active"])
        cold_crew[product] = parse_float(row["cold_start_crew_units"])
        continuation_crew[product] = parse_float(row["continuation_crew_units"])
        restart_penalty[product] = parse_float(row["restart_penalty_cost"])
        min_run[product] = parse_int(row["min_run_months"])

    crew_capacity = {}
    for row in read_csv(DATA_DIR / "setup_crew_capacity.csv"):
        crew_capacity[row["month"]] = parse_float(row["available_units"])

    mix_windows = []
    for row in read_csv(DATA_DIR / "channel_mix_windows.csv"):
        mix_windows.append(
            {
                "mix_id": row["mix_id"],
                "start_idx": month_pos[row["start_month"]],
                "end_idx": month_pos[row["end_month"]],
                "product": row["product_id"],
                "channel": row["channel_id"],
                "min_share": parse_float(row["min_share"]),
                "max_share": parse_float(row["max_share"]),
            }
        )

    family_limit = {}
    families = set()
    for row in read_csv(DATA_DIR / "family_campaign_limits.csv"):
        family_limit[(row["month"], row["product_family"])] = parse_float(
            row["max_active_products"]
        )
        families.add(row["product_family"])

    contracts = []
    for row in read_csv(DATA_DIR / "contract_commitments.csv"):
        contracts.append(
            {
                "contract_id": row["contract_id"],
                "start_idx": month_pos[row["start_month"]],
                "end_idx": month_pos[row["end_month"]],
                "product": row["product_id"],
                "channel": row["channel_id"],
                "min_total": parse_float(row["min_total_units"]),
            }
        )

    conflicts = []
    for row in read_csv(DATA_DIR / "product_conflict_rules.csv"):
        conflicts.append(
            {
                "month": row["month"],
                "product": row["product_id"],
                "other_product": row["conflict_product_id"],
                "max_simultaneous": parse_float(row["max_simultaneous_setups"]),
            }
        )

    return {
        "config": config,
        "months": months,
        "month_pos": month_pos,
        "products": products,
        "channels": channels,
        "machines": machines,
        "families": sorted(families),
        "product_family": product_family,
        "unit_price": unit_price,
        "raw_material_cost": raw_material_cost,
        "price_multiplier": price_multiplier,
        "machine_stage": machine_stage,
        "regular_cost": regular_cost,
        "overtime_premium": overtime_premium,
        "prod_keys": sorted(prod_keys),
        "hours_per_unit": hours_per_unit,
        "a_routes": {p: sorted(a_routes[p]) for p in products},
        "b_routes": {p: sorted(b_routes[p]) for p in products},
        "machine_capacity": machine_capacity,
        "materials": materials,
        "bom": bom,
        "material_avail": material_avail,
        "material_reserve": material_reserve,
        "demand": demand,
        "service_fraction": service_fraction,
        "setup_cost": setup_cost,
        "min_campaign": min_campaign,
        "max_campaign": max_campaign,
        "setup_hours": setup_hours,
        "initial_inventory": initial_inventory,
        "max_inventory": max_inventory,
        "final_inventory": final_inventory,
        "holding_cost": holding_cost,
        "release_lag": release_lag,
        "max_pipeline": max_pipeline,
        "yield_rate": yield_rate,
        "initial_active": initial_active,
        "cold_crew": cold_crew,
        "continuation_crew": continuation_crew,
        "restart_penalty": restart_penalty,
        "min_run": min_run,
        "crew_capacity": crew_capacity,
        "mix_windows": mix_windows,
        "family_limit": family_limit,
        "contracts": contracts,
        "conflicts": conflicts,
    }


def get_time_limit():
    default_limit = 295.0
    env_value = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if not env_value:
        return default_limit
    try:
        return min(default_limit, max(1.0, float(env_value)))
    except ValueError:
        return default_limit


def build_model(data, time_limit):
    model = Model("meridian_components")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", float(data["config"]["solver_mip_gap"]))
    model.setParam("limits/time", float(time_limit))

    months = data["months"]
    products = data["products"]
    channels = data["channels"]
    machines = data["machines"]

    prod = {}
    for month in months:
        for stage, machine, product in data["prod_keys"]:
            prod[(month, stage, machine, product)] = model.addVar(
                lb=0.0,
                name=f"prod[{month},{stage},{machine},{product}]",
            )

    ship = {}
    for month in months:
        for product in products:
            for channel in channels:
                ship[(month, product, channel)] = model.addVar(
                    lb=0.0,
                    ub=data["demand"][(month, product, channel)],
                    name=f"ship[{month},{product},{channel}]",
                )

    active = {}
    start = {}
    total_prod = {}
    good_prod = {}
    inventory = {}
    pipeline = {}
    overtime = {}

    for month in months:
        for product in products:
            active[(month, product)] = model.addVar(
                vtype="B", name=f"active[{month},{product}]"
            )
            start[(month, product)] = model.addVar(
                vtype="B", name=f"start[{month},{product}]"
            )
            total_prod[(month, product)] = model.addVar(
                lb=0.0, name=f"total_prod[{month},{product}]"
            )
            good_prod[(month, product)] = model.addVar(
                lb=0.0, name=f"good_prod[{month},{product}]"
            )
            inventory[(month, product)] = model.addVar(
                lb=0.0, name=f"inventory[{month},{product}]"
            )
            pipeline[(month, product)] = model.addVar(
                lb=0.0, name=f"pipeline[{month},{product}]"
            )
        for machine in machines:
            overtime[(month, machine)] = model.addVar(
                lb=0.0,
                ub=data["machine_capacity"][(month, machine)]["overtime"],
                name=f"overtime[{month},{machine}]",
            )

    for month in months:
        for product in products:
            expr_a = quicksum(
                prod[(month, "A", machine, product)] for machine in data["a_routes"][product]
            )
            expr_b = quicksum(
                prod[(month, "B", machine, product)] for machine in data["b_routes"][product]
            )
            model.addCons(
                total_prod[(month, product)] == expr_a,
                name=f"stageA_balance[{month},{product}]",
            )
            model.addCons(
                total_prod[(month, product)] == expr_b,
                name=f"stageB_balance[{month},{product}]",
            )
            model.addCons(
                good_prod[(month, product)]
                == quicksum(
                    data["yield_rate"][(machine, product)]
                    * prod[(month, "B", machine, product)]
                    for machine in data["b_routes"][product]
                ),
                name=f"good_output[{month},{product}]",
            )
            model.addCons(
                total_prod[(month, product)]
                >= data["min_campaign"][product] * active[(month, product)],
                name=f"min_campaign[{month},{product}]",
            )
            model.addCons(
                total_prod[(month, product)]
                <= data["max_campaign"][product] * active[(month, product)],
                name=f"max_campaign[{month},{product}]",
            )

    for month in months:
        for machine in machines:
            stage = data["machine_stage"][machine]
            proc_expr = quicksum(
                data["hours_per_unit"][(stage, machine, product)]
                * prod[(month, stage, machine, product)]
                for product in products
                if (stage, machine, product) in data["hours_per_unit"]
            )
            setup_expr = quicksum(
                data["setup_hours"][(stage, machine, product)] * active[(month, product)]
                for product in products
                if data["setup_hours"][(stage, machine, product)] > 0.0
            )
            model.addCons(
                proc_expr + setup_expr
                <= data["machine_capacity"][(month, machine)]["regular"]
                + overtime[(month, machine)],
                name=f"machine_cap[{month},{machine}]",
            )

    for t_idx, month in enumerate(months):
        for product in products:
            lag = data["release_lag"][product]
            if t_idx - lag >= 0:
                released_expr = good_prod[(months[t_idx - lag], product)]
            else:
                released_expr = 0.0
            prev_inventory = (
                data["initial_inventory"][product]
                if t_idx == 0
                else inventory[(months[t_idx - 1], product)]
            )
            prev_pipeline = 0.0 if t_idx == 0 else pipeline[(months[t_idx - 1], product)]

            model.addCons(
                inventory[(month, product)]
                == prev_inventory
                + released_expr
                - quicksum(ship[(month, product, channel)] for channel in channels),
                name=f"inventory_flow[{month},{product}]",
            )
            model.addCons(
                inventory[(month, product)] <= data["max_inventory"][product],
                name=f"inventory_cap[{month},{product}]",
            )
            model.addCons(
                pipeline[(month, product)]
                == prev_pipeline + good_prod[(month, product)] - released_expr,
                name=f"pipeline_flow[{month},{product}]",
            )
            model.addCons(
                pipeline[(month, product)] <= data["max_pipeline"][product],
                name=f"pipeline_cap[{month},{product}]",
            )

    last_month = months[-1]
    for product in products:
        model.addCons(
            inventory[(last_month, product)] >= data["final_inventory"][product],
            name=f"final_inventory[{product}]",
        )

    for month in months:
        for material in data["materials"]:
            model.addCons(
                quicksum(
                    data["bom"][(product, material)] * total_prod[(month, product)]
                    for product in products
                )
                <= data["material_avail"][(month, material)]
                - data["material_reserve"][(month, material)],
                name=f"material[{month},{material}]",
            )

    for month in months:
        for product in products:
            for channel in channels:
                min_ship = (
                    data["service_fraction"][(product, channel)]
                    * data["demand"][(month, product, channel)]
                )
                model.addCons(
                    ship[(month, product, channel)] >= min_ship,
                    name=f"service[{month},{product},{channel}]",
                )

    for t_idx, month in enumerate(months):
        for product in products:
            if t_idx == 0:
                prev_active = data["initial_active"][product]
                model.addCons(
                    start[(month, product)] >= active[(month, product)] - prev_active,
                    name=f"start_lb[{month},{product}]",
                )
                model.addCons(
                    start[(month, product)] <= active[(month, product)],
                    name=f"start_ub1[{month},{product}]",
                )
                model.addCons(
                    start[(month, product)] <= 1 - prev_active,
                    name=f"start_ub2[{month},{product}]",
                )
            else:
                prev_month = months[t_idx - 1]
                model.addCons(
                    start[(month, product)]
                    >= active[(month, product)] - active[(prev_month, product)],
                    name=f"start_lb[{month},{product}]",
                )
                model.addCons(
                    start[(month, product)] <= active[(month, product)],
                    name=f"start_ub1[{month},{product}]",
                )
                model.addCons(
                    start[(month, product)] <= 1 - active[(prev_month, product)],
                    name=f"start_ub2[{month},{product}]",
                )

    n_months = len(months)
    for product in products:
        run = data["min_run"][product]
        for t_idx, month in enumerate(months):
            if t_idx + run <= n_months:
                model.addCons(
                    quicksum(active[(months[k], product)] for k in range(t_idx, t_idx + run))
                    >= run * start[(month, product)],
                    name=f"min_run[{month},{product}]",
                )
            else:
                model.addCons(
                    start[(month, product)] == 0,
                    name=f"late_start_forbidden[{month},{product}]",
                )

    for month in months:
        model.addCons(
            quicksum(
                data["cold_crew"][product] * start[(month, product)]
                + data["continuation_crew"][product]
                * (active[(month, product)] - start[(month, product)])
                for product in products
            )
            <= data["crew_capacity"][month],
            name=f"crew_cap[{month}]",
        )

    for month in months:
        for family in data["families"]:
            family_products = [
                product
                for product in products
                if data["product_family"][product] == family
            ]
            model.addCons(
                quicksum(active[(month, product)] for product in family_products)
                <= data["family_limit"][(month, family)],
                name=f"family_cap[{month},{family}]",
            )

    for conflict in data["conflicts"]:
        month = conflict["month"]
        model.addCons(
            active[(month, conflict["product"])]
            + active[(month, conflict["other_product"])]
            <= conflict["max_simultaneous"],
            name=f"conflict[{month},{conflict['product']},{conflict['other_product']}]",
        )

    for contract in data["contracts"]:
        model.addCons(
            quicksum(
                ship[(months[t_idx], contract["product"], contract["channel"])]
                for t_idx in range(contract["start_idx"], contract["end_idx"] + 1)
            )
            >= contract["min_total"],
            name=f"contract[{contract['contract_id']}]",
        )

    for window in data["mix_windows"]:
        selected = quicksum(
            ship[(months[t_idx], window["product"], window["channel"])]
            for t_idx in range(window["start_idx"], window["end_idx"] + 1)
        )
        total = quicksum(
            ship[(months[t_idx], window["product"], channel)]
            for t_idx in range(window["start_idx"], window["end_idx"] + 1)
            for channel in channels
        )
        model.addCons(
            selected >= window["min_share"] * total,
            name=f"mix_min[{window['mix_id']}]",
        )
        model.addCons(
            selected <= window["max_share"] * total,
            name=f"mix_max[{window['mix_id']}]",
        )

    revenue = quicksum(
        data["unit_price"][product]
        * data["price_multiplier"][channel]
        * ship[(month, product, channel)]
        for month in months
        for product in products
        for channel in channels
    )
    raw_cost = quicksum(
        data["raw_material_cost"][product] * total_prod[(month, product)]
        for month in months
        for product in products
    )
    process_cost = quicksum(
        data["regular_cost"][machine]
        * data["hours_per_unit"][(stage, machine, product)]
        * prod[(month, stage, machine, product)]
        for month in months
        for stage, machine, product in data["prod_keys"]
    )
    overtime_cost = quicksum(
        data["overtime_premium"][machine] * overtime[(month, machine)]
        for month in months
        for machine in machines
    )
    setup_cost_expr = quicksum(
        data["setup_cost"][product] * active[(month, product)]
        for month in months
        for product in products
    )
    restart_cost = quicksum(
        data["restart_penalty"][product] * start[(month, product)]
        for month in months
        for product in products
    )
    holding_cost = quicksum(
        data["holding_cost"][product] * inventory[(month, product)]
        for month in months
        for product in products
    )

    model.setObjective(
        revenue - raw_cost - process_cost - overtime_cost - setup_cost_expr - restart_cost - holding_cost,
        "maximize",
    )

    return model, {
        "prod": prod,
        "ship": ship,
        "active": active,
        "start": start,
        "total_prod": total_prod,
        "good_prod": good_prod,
        "inventory": inventory,
        "pipeline": pipeline,
        "overtime": overtime,
    }


def build_mip_start(model, variables, data, validation):
    sol = model.createSol()

    for month in data["months"]:
        for stage, machine, product in data["prod_keys"]:
            model.setSolVal(
                sol,
                variables["prod"][(month, stage, machine, product)],
                validation["production"][(month, stage, machine, product)],
            )
        for product in data["products"]:
            model.setSolVal(
                sol,
                variables["active"][(month, product)],
                validation["setup"][(month, product)],
            )
            model.setSolVal(
                sol,
                variables["start"][(month, product)],
                validation["starts"][(month, product)],
            )
            model.setSolVal(
                sol,
                variables["total_prod"][(month, product)],
                validation["total_prod"][(month, product)],
            )
            model.setSolVal(
                sol,
                variables["good_prod"][(month, product)],
                validation["good_prod"][(month, product)],
            )
            model.setSolVal(
                sol,
                variables["inventory"][(month, product)],
                validation["inventory"][(month, product)],
            )
            model.setSolVal(
                sol,
                variables["pipeline"][(month, product)],
                validation["pipeline"][(month, product)],
            )
            for channel in data["channels"]:
                model.setSolVal(
                    sol,
                    variables["ship"][(month, product, channel)],
                    validation["shipment"][(month, product, channel)],
                )
        for machine in data["machines"]:
            model.setSolVal(
                sol,
                variables["overtime"][(month, machine)],
                validation["overtime_needed"][(month, machine)],
            )

    accepted = model.addSol(sol, free=True)
    return accepted


def extract_solution(model, variables, data):
    sol = model.getBestSol()
    if sol is None:
        raise RuntimeError("SCIP did not produce an incumbent solution.")

    production = {}
    shipment = {}
    setup = {}

    for month in data["months"]:
        for stage, machine, product in data["prod_keys"]:
            value = model.getSolVal(sol, variables["prod"][(month, stage, machine, product)])
            production[(month, stage, machine, product)] = max(0.0, value)
        for product in data["products"]:
            value = model.getSolVal(sol, variables["active"][(month, product)])
            setup[(month, product)] = int(round(value))
            for channel in data["channels"]:
                ship_value = model.getSolVal(sol, variables["ship"][(month, product, channel)])
                shipment[(month, product, channel)] = max(0.0, ship_value)

    rows = []
    for month in data["months"]:
        for stage, machine, product in data["prod_keys"]:
            rows.append(
                {
                    "variable": "production",
                    "month": month,
                    "stage": stage,
                    "machine": machine,
                    "product": product,
                    "channel": "",
                    "quantity": format_quantity(production[(month, stage, machine, product)]),
                }
            )
    for month in data["months"]:
        for product in data["products"]:
            for channel in data["channels"]:
                rows.append(
                    {
                        "variable": "shipment",
                        "month": month,
                        "stage": "",
                        "machine": "",
                        "product": product,
                        "channel": channel,
                        "quantity": format_quantity(shipment[(month, product, channel)]),
                    }
                )
    for month in data["months"]:
        for product in data["products"]:
            rows.append(
                {
                    "variable": "setup",
                    "month": month,
                    "stage": "",
                    "machine": "",
                    "product": product,
                    "channel": "",
                    "quantity": str(setup[(month, product)]),
                }
            )
    return rows


def write_solution(rows):
    columns = ["variable", "month", "stage", "machine", "product", "channel", "quantity"]
    with SOLUTION_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_solution_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def residual_le(lhs, rhs):
    return max(0.0, lhs - rhs) / max(1.0, abs(rhs))


def residual_ge(lhs, rhs):
    return max(0.0, rhs - lhs) / max(1.0, abs(rhs))


def residual_eq(lhs, rhs):
    return abs(lhs - rhs) / max(1.0, abs(rhs))


def validate_solution(data, rows):
    tol = float(data["config"]["feasibility_tolerance"])
    bin_tol = float(data["config"]["binary_tolerance"])
    expected_columns = data["config"]["solution_columns"]

    errors = []
    production = {}
    shipment = {}
    setup = {}

    expected_prod = {
        ("production", month, stage, machine, product, "")
        for month in data["months"]
        for stage, machine, product in data["prod_keys"]
    }
    expected_ship = {
        ("shipment", month, "", "", product, channel)
        for month in data["months"]
        for product in data["products"]
        for channel in data["channels"]
    }
    expected_setup = {
        ("setup", month, "", "", product, "")
        for month in data["months"]
        for product in data["products"]
    }
    expected_keys = expected_prod | expected_ship | expected_setup
    seen_keys = set()

    max_integrality_residual = 0.0

    for row in rows:
        if list(row.keys()) != expected_columns:
            errors.append("Schema columns do not match config.json")
            break
        variable = row["variable"]
        month = row["month"]
        stage = row["stage"]
        machine = row["machine"]
        product = row["product"]
        channel = row["channel"]
        key = (variable, month, stage, machine, product, channel)
        if key in seen_keys:
            errors.append(f"Duplicate row {key}")
            continue
        seen_keys.add(key)
        if key not in expected_keys:
            errors.append(f"Unexpected row {key}")
            continue
        try:
            quantity = float(row["quantity"])
        except ValueError:
            errors.append(f"Non-numeric quantity for {key}")
            continue
        if variable == "production":
            production[(month, stage, machine, product)] = quantity
        elif variable == "shipment":
            shipment[(month, product, channel)] = quantity
        elif variable == "setup":
            max_integrality_residual = max(
                max_integrality_residual, abs(quantity - round(quantity))
            )
            setup[(month, product)] = quantity

    missing = expected_keys - seen_keys
    if missing:
        errors.append(f"Missing {len(missing)} required rows")

    residuals = {}

    def note(name, value):
        residuals[name] = max(residuals.get(name, 0.0), value)

    stage_totals_a = defaultdict(float)
    stage_totals_b = defaultdict(float)
    total_prod = defaultdict(float)
    good_prod = defaultdict(float)
    overtime_needed = defaultdict(float)
    starts = {}
    inventory = {}
    pipeline = {}

    for key, qty in production.items():
        month, stage, machine, product = key
        note("nonnegative_production", residual_ge(qty, 0.0))
        if stage == "A":
            stage_totals_a[(month, product)] += qty
        elif stage == "B":
            stage_totals_b[(month, product)] += qty
            good_prod[(month, product)] += data["yield_rate"][(machine, product)] * qty

    for month in data["months"]:
        for product in data["products"]:
            total_prod[(month, product)] = 0.5 * (
                stage_totals_a[(month, product)] + stage_totals_b[(month, product)]
            )

    for key, qty in shipment.items():
        month, product, channel = key
        demand = data["demand"][(month, product, channel)]
        min_ship = data["service_fraction"][(product, channel)] * demand
        note("nonnegative_shipment", residual_ge(qty, 0.0))
        note("demand_cap", residual_le(qty, demand))
        note("service_min", residual_ge(qty, min_ship))

    for month in data["months"]:
        for product in data["products"]:
            setup_val = setup.get((month, product), 0.0)
            note("setup_binary", abs(setup_val - round(setup_val)))
            note(
                "campaign_min",
                residual_ge(
                    total_prod[(month, product)],
                    data["min_campaign"][product] * setup_val,
                ),
            )
            note(
                "campaign_max",
                residual_le(
                    total_prod[(month, product)],
                    data["max_campaign"][product] * setup_val,
                ),
            )
            note(
                "stage_balance",
                residual_eq(stage_totals_a[(month, product)], stage_totals_b[(month, product)]),
            )

    for month in data["months"]:
        for machine in data["machines"]:
            stage = data["machine_stage"][machine]
            proc_hours = 0.0
            for product in data["products"]:
                if (month, stage, machine, product) in production:
                    proc_hours += (
                        data["hours_per_unit"][(stage, machine, product)]
                        * production[(month, stage, machine, product)]
                    )
            setup_load = 0.0
            for product in data["products"]:
                setup_load += (
                    data["setup_hours"][(stage, machine, product)]
                    * setup.get((month, product), 0.0)
                )
            total_hours = proc_hours + setup_load
            reg = data["machine_capacity"][(month, machine)]["regular"]
            ot_cap = data["machine_capacity"][(month, machine)]["overtime"]
            overtime_needed[(month, machine)] = max(0.0, total_hours - reg)
            note("machine_overtime_cap", residual_le(overtime_needed[(month, machine)], ot_cap))

    for month in data["months"]:
        for material in data["materials"]:
            use = sum(
                data["bom"][(product, material)] * total_prod[(month, product)]
                for product in data["products"]
            )
            rhs = data["material_avail"][(month, material)] - data["material_reserve"][
                (month, material)
            ]
            note("material", residual_le(use, rhs))

    months = data["months"]
    for t_idx, month in enumerate(months):
        for product in data["products"]:
            prev_active = (
                data["initial_active"][product]
                if t_idx == 0
                else setup[(months[t_idx - 1], product)]
            )
            active = setup[(month, product)]
            starts[(month, product)] = 1.0 if active > 0.5 and prev_active < 0.5 else 0.0

            if t_idx == 0:
                prev_inventory = data["initial_inventory"][product]
                prev_pipeline = 0.0
            else:
                prev_inventory = inventory[(months[t_idx - 1], product)]
                prev_pipeline = pipeline[(months[t_idx - 1], product)]

            lag = data["release_lag"][product]
            if t_idx - lag >= 0:
                released = good_prod[(months[t_idx - lag], product)]
            else:
                released = 0.0

            shipped_total = sum(
                shipment[(month, product, channel)] for channel in data["channels"]
            )
            inventory[(month, product)] = prev_inventory + released - shipped_total
            pipeline[(month, product)] = prev_pipeline + good_prod[(month, product)] - released

            note("inventory_nonnegative", residual_ge(inventory[(month, product)], 0.0))
            note(
                "inventory_cap",
                residual_le(inventory[(month, product)], data["max_inventory"][product]),
            )
            note(
                "pipeline_nonnegative",
                residual_ge(pipeline[(month, product)], 0.0),
            )
            note(
                "pipeline_cap",
                residual_le(pipeline[(month, product)], data["max_pipeline"][product]),
            )

    last_month = months[-1]
    for product in data["products"]:
        note(
            "final_inventory",
            residual_ge(inventory[(last_month, product)], data["final_inventory"][product]),
        )

    for month in data["months"]:
        crew_use = sum(
            data["cold_crew"][product] * starts[(month, product)]
            + data["continuation_crew"][product]
            * (setup[(month, product)] - starts[(month, product)])
            for product in data["products"]
        )
        note("crew_cap", residual_le(crew_use, data["crew_capacity"][month]))

        for family in data["families"]:
            active_count = sum(
                setup[(month, product)]
                for product in data["products"]
                if data["product_family"][product] == family
            )
            note(
                "family_cap",
                residual_le(active_count, data["family_limit"][(month, family)]),
            )

    for conflict in data["conflicts"]:
        lhs = setup[(conflict["month"], conflict["product"])] + setup[
            (conflict["month"], conflict["other_product"])
        ]
        note("product_conflict", residual_le(lhs, conflict["max_simultaneous"]))

    for product in data["products"]:
        run = data["min_run"][product]
        for t_idx, month in enumerate(months):
            if starts[(month, product)] > 0.5:
                if t_idx + run > len(months):
                    note("min_run", 1.0)
                else:
                    active_span = sum(
                        setup[(months[k], product)] for k in range(t_idx, t_idx + run)
                    )
                    note("min_run", residual_ge(active_span, float(run)))

    for contract in data["contracts"]:
        total_ship = sum(
            shipment[(months[t_idx], contract["product"], contract["channel"])]
            for t_idx in range(contract["start_idx"], contract["end_idx"] + 1)
        )
        note("contract", residual_ge(total_ship, contract["min_total"]))

    for window in data["mix_windows"]:
        selected = sum(
            shipment[(months[t_idx], window["product"], window["channel"])]
            for t_idx in range(window["start_idx"], window["end_idx"] + 1)
        )
        total = sum(
            shipment[(months[t_idx], window["product"], channel)]
            for t_idx in range(window["start_idx"], window["end_idx"] + 1)
            for channel in data["channels"]
        )
        note("mix_min", residual_ge(selected, window["min_share"] * total))
        note("mix_max", residual_le(selected, window["max_share"] * total))

    objective = 0.0
    objective += sum(
        data["unit_price"][product]
        * data["price_multiplier"][channel]
        * shipment[(month, product, channel)]
        for month in data["months"]
        for product in data["products"]
        for channel in data["channels"]
    )
    objective -= sum(
        data["raw_material_cost"][product] * total_prod[(month, product)]
        for month in data["months"]
        for product in data["products"]
    )
    objective -= sum(
        data["regular_cost"][machine]
        * data["hours_per_unit"][(stage, machine, product)]
        * production[(month, stage, machine, product)]
        for month in data["months"]
        for stage, machine, product in data["prod_keys"]
    )
    objective -= sum(
        data["overtime_premium"][machine] * overtime_needed[(month, machine)]
        for month in data["months"]
        for machine in data["machines"]
    )
    objective -= sum(
        data["setup_cost"][product] * setup[(month, product)]
        for month in data["months"]
        for product in data["products"]
    )
    objective -= sum(
        data["restart_penalty"][product] * starts[(month, product)]
        for month in data["months"]
        for product in data["products"]
    )
    objective -= sum(
        data["holding_cost"][product] * inventory[(month, product)]
        for month in data["months"]
        for product in data["products"]
    )

    theta = max(residuals.values()) if residuals else 0.0
    violated_groups = sum(1 for value in residuals.values() if value > tol)

    return {
        "errors": errors,
        "theta": theta,
        "violated_groups": violated_groups,
        "integrality_residual": max_integrality_residual,
        "residuals": dict(sorted(residuals.items())),
        "objective": objective,
        "inventory": inventory,
        "pipeline": pipeline,
        "starts": starts,
        "overtime_needed": overtime_needed,
        "total_prod": total_prod,
        "good_prod": good_prod,
        "production": production,
        "shipment": shipment,
        "setup": setup,
        "is_feasible": not errors and theta <= tol and max_integrality_residual <= bin_tol,
    }


def main():
    start_time = time.time()
    data = load_data()
    time_limit = get_time_limit()

    if not SOLUTION_PATH.exists():
        raise RuntimeError("Rollback incumbent /app/submissions/solution.csv is missing.")

    incumbent_rows = load_solution_csv(SOLUTION_PATH)
    incumbent_validation = validate_solution(data, incumbent_rows)
    if not incumbent_validation["is_feasible"]:
        raise RuntimeError(
            "On-disk incumbent failed independent validation before optimization: "
            f"errors={incumbent_validation['errors']}, residuals={incumbent_validation['residuals']}"
        )

    incumbent_objective = incumbent_validation["objective"]

    model, variables = build_model(data, time_limit)
    mip_start_accepted = build_mip_start(model, variables, data, incumbent_validation)
    model.optimize()

    status = str(model.getStatus())
    n_solutions = model.getNSols()

    summary = {
        "status": status,
        "time_limit_seconds": time_limit,
        "n_solutions": int(n_solutions),
        "solve_time_seconds": time.time() - start_time,
        "incumbent_objective": incumbent_objective,
        "incumbent_theta": incumbent_validation["theta"],
        "incumbent_violated_groups": incumbent_validation["violated_groups"],
        "incumbent_integrality_residual": incumbent_validation["integrality_residual"],
        "mip_start_accepted": bool(mip_start_accepted),
    }

    publish_candidate = False
    candidate_validation = None

    if n_solutions > 0 and model.getBestSol() is not None:
        candidate_rows = extract_solution(model, variables, data)
        candidate_validation = validate_solution(data, candidate_rows)
        summary["candidate_objective"] = candidate_validation["objective"]
        summary["candidate_theta"] = candidate_validation["theta"]
        summary["candidate_violated_groups"] = candidate_validation["violated_groups"]
        summary["candidate_integrality_residual"] = candidate_validation[
            "integrality_residual"
        ]
        summary["candidate_feasible"] = candidate_validation["is_feasible"]
        summary["solver_objective"] = model.getObjVal()
        summary["dual_bound"] = model.getDualbound()
        summary["gap"] = model.getGap()

        if candidate_validation["is_feasible"] and (
            candidate_validation["objective"] > incumbent_objective + OBJECTIVE_IMPROVEMENT_EPS
        ):
            publish_candidate = True
            write_solution(candidate_rows)
    else:
        summary["candidate_objective"] = None
        summary["candidate_theta"] = None
        summary["candidate_violated_groups"] = None
        summary["candidate_integrality_residual"] = None
        summary["candidate_feasible"] = False
        summary["solver_objective"] = None
        summary["dual_bound"] = model.getDualbound()
        summary["gap"] = model.getGap()

    final_rows = load_solution_csv(SOLUTION_PATH)
    final_validation = validate_solution(data, final_rows)
    summary["published_candidate"] = publish_candidate
    summary["final_objective"] = final_validation["objective"]
    summary["final_theta"] = final_validation["theta"]
    summary["final_violated_groups"] = final_validation["violated_groups"]
    summary["final_integrality_residual"] = final_validation["integrality_residual"]
    summary["final_feasible"] = final_validation["is_feasible"]

    if not final_validation["is_feasible"]:
        raise RuntimeError(
            "Final on-disk artifact failed independent validation: "
            f"errors={final_validation['errors']}, residuals={final_validation['residuals']}"
        )

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
