import csv
import json
import os
import time
from collections import defaultdict

from pyscipopt import Model, quicksum


DATA_DIR = "/app/data"
SUBMISSIONS_DIR = "/app/submissions"
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")

HEADER = [
    "row_type",
    "scenario_id",
    "period_id",
    "origin_id",
    "destination_id",
    "clinic_id",
    "item_id",
    "fleet_type",
    "quantity",
    "decision",
]

TOL = 1e-6
PRINT_TOL = 5e-9


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_float(value):
    return float(value)


def bool_flag(value):
    return str(value).strip() == "1"


def fmt_qty(value):
    if abs(value) < PRINT_TOL:
        value = 0.0
    return f"{value:.8f}"


def norm_violation(lhs, sense, rhs):
    if sense == "<=":
        violation = max(0.0, lhs - rhs)
    elif sense == ">=":
        violation = max(0.0, rhs - lhs)
    elif sense == "=":
        violation = abs(lhs - rhs)
    else:
        raise ValueError(f"Unknown sense {sense}")
    return violation / max(1.0, abs(rhs))


def load_data():
    with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)

    items_rows = read_csv(os.path.join(DATA_DIR, "items.csv"))
    warehouses_rows = read_csv(os.path.join(DATA_DIR, "warehouses.csv"))
    suppliers_rows = read_csv(os.path.join(DATA_DIR, "suppliers.csv"))
    clinics_rows = read_csv(os.path.join(DATA_DIR, "clinics.csv"))
    periods_rows = read_csv(os.path.join(DATA_DIR, "periods.csv"))
    scenarios_rows = read_csv(os.path.join(DATA_DIR, "scenarios.csv"))
    sup_item_rows = read_csv(os.path.join(DATA_DIR, "supplier_item_capacity.csv"))
    sup_wh_rows = read_csv(os.path.join(DATA_DIR, "supplier_warehouse_lanes.csv"))
    wh_clinic_rows = read_csv(os.path.join(DATA_DIR, "warehouse_clinic_lanes.csv"))
    wh_fleet_rows = read_csv(os.path.join(DATA_DIR, "warehouse_fleet.csv"))
    fleet_rows = read_csv(os.path.join(DATA_DIR, "fleet_types.csv"))
    service_rows = read_csv(os.path.join(DATA_DIR, "service_targets.csv"))
    expcap_rows = read_csv(os.path.join(DATA_DIR, "scenario_expedite_caps.csv"))
    demand_rows = read_csv(os.path.join(DATA_DIR, "scenario_period_demands.csv"))
    sup_avail_rows = read_csv(
        os.path.join(DATA_DIR, "scenario_period_supplier_availability.csv")
    )
    sup_lane_rows = read_csv(
        os.path.join(DATA_DIR, "scenario_period_supplier_lane_impacts.csv")
    )
    wh_avail_rows = read_csv(
        os.path.join(DATA_DIR, "scenario_period_warehouse_availability.csv")
    )
    road_rows = read_csv(os.path.join(DATA_DIR, "scenario_period_road_impacts.csv"))

    items = {}
    item_ids = []
    for row in items_rows:
        item = row["item_id"]
        item_ids.append(item)
        lag_raw = row["quality_release_lag_periods"].strip()
        items[item] = {
            "unit_volume": as_float(row["unit_volume"]),
            "load_factor": as_float(row["load_factor"]),
            "preposition_cost": as_float(row["preposition_cost"]),
            "production_cost": as_float(row["production_cost"]),
            "capacity_reserve_cost": as_float(row["capacity_reserve_cost"]),
            "expedite_cost": as_float(row["expedite_cost"]),
            "shortage_penalty": as_float(row["shortage_penalty"]),
            "holding_cost": as_float(row["holding_cost"]),
            "cold_chain_required": bool_flag(row["cold_chain_required"]),
            "returnable_container": bool_flag(row["returnable_container"]),
            "release_lag": (
                int(lag_raw)
                if lag_raw
                else int(config["quality_release_default_lag_periods"])
            ),
            "max_stock_per_warehouse": as_float(row["max_stock_per_warehouse"]),
        }

    warehouses = {}
    warehouse_ids = []
    for row in warehouses_rows:
        w = row["warehouse_id"]
        warehouse_ids.append(w)
        warehouses[w] = {
            "region": row["region"],
            "open_cost": as_float(row["open_cost"]),
            "storage_capacity": as_float(row["storage_capacity"]),
            "outbound_capacity": as_float(row["outbound_capacity"]),
            "cold_chain": bool_flag(row["cold_chain"]),
            "flood_hardened": bool_flag(row["flood_hardened"]),
            "airport_access": bool_flag(row["airport_access"]),
        }

    suppliers = {}
    supplier_ids = []
    for row in suppliers_rows:
        s = row["supplier_id"]
        supplier_ids.append(s)
        suppliers[s] = {
            "region": row["region"],
            "activation_cost": as_float(row["activation_cost"]),
            "base_period_capacity": as_float(row["base_period_capacity"]),
            "line_hours": as_float(row["line_hours"]),
            "cold_chain_certified": bool_flag(row["cold_chain_certified"]),
            "max_daily_item_setups": int(float(row["max_daily_item_setups"])),
        }

    clinics = {}
    clinic_ids = []
    for row in clinics_rows:
        c = row["clinic_id"]
        clinic_ids.append(c)
        clinics[c] = {
            "district": row["district"],
            "priority_class": row["priority_class"],
            "population": as_float(row["population"]),
            "receiving_capacity": as_float(row["receiving_capacity"]),
            "cold_storage_capacity": as_float(row["cold_storage_capacity"]),
            "local_procure_fraction": as_float(row["local_procure_fraction"]),
            "target_response_hours": as_float(row["target_response_hours"]),
            "floors": {
                item: as_float(row[f"{item}_floor"])
                for item in ["oxygen", "antiviral", "ppe", "dialysis"]
            },
        }

    periods_sorted = sorted(periods_rows, key=lambda row: as_float(row["start_hour"]))
    period_ids = [row["period_id"] for row in periods_sorted]
    period_index = {period: idx for idx, period in enumerate(period_ids)}
    prev_period = {
        period_ids[idx]: (period_ids[idx - 1] if idx > 0 else None)
        for idx in range(len(period_ids))
    }
    periods = {
        row["period_id"]: {
            "start_hour": as_float(row["start_hour"]),
            "end_hour": as_float(row["end_hour"]),
            "label": row["label"],
            "delay_multiplier": as_float(row["delay_multiplier"]),
        }
        for row in periods_sorted
    }

    scenarios = {}
    scenario_ids = []
    for row in scenarios_rows:
        omega = row["scenario_id"]
        scenario_ids.append(omega)
        scenarios[omega] = {
            "probability": as_float(row["probability"]),
            "family": row["family"],
        }

    supplier_item = {}
    for row in sup_item_rows:
        supplier_item[(row["supplier_id"], row["item_id"])] = {
            "max_reserved_capacity": as_float(row["max_reserved_capacity"]),
            "labor_hours_per_unit": as_float(row["labor_hours_per_unit"]),
            "min_batch_quantity": as_float(row["min_batch_quantity"]),
            "setup_hours": as_float(row["setup_hours"]),
            "setup_cost": as_float(row["setup_cost"]),
        }

    supplier_wh = {}
    supplier_wh_by_supplier = defaultdict(list)
    for row in sup_wh_rows:
        key = (row["supplier_id"], row["warehouse_id"])
        supplier_wh[key] = {
            "base_capacity": as_float(row["base_capacity"]),
            "transport_cost_per_load": as_float(row["transport_cost_per_load"]),
            "cold_chain_supported": bool_flag(row["cold_chain_supported"]),
        }
        supplier_wh_by_supplier[row["supplier_id"]].append(row["warehouse_id"])

    wh_clinic = {}
    wh_clinic_by_warehouse = defaultdict(list)
    for row in wh_clinic_rows:
        key = (row["warehouse_id"], row["clinic_id"])
        wh_clinic[key] = {
            "distance_km": as_float(row["distance_km"]),
            "travel_time_hours": as_float(row["travel_time_hours"]),
            "base_capacity": as_float(row["base_capacity"]),
            "transport_cost_per_load": as_float(row["transport_cost_per_load"]),
            "road_class": row["road_class"],
        }
        wh_clinic_by_warehouse[row["warehouse_id"]].append(row["clinic_id"])

    fleets = {}
    fleet_ids = []
    for row in fleet_rows:
        f = row["fleet_type"]
        fleet_ids.append(f)
        fleets[f] = {
            "capacity_load": as_float(row["capacity_load"]),
            "cold_chain_capable": bool_flag(row["cold_chain_capable"]),
            "rough_road_capable": bool_flag(row["rough_road_capable"]),
            "airlift_capable": bool_flag(row["airlift_capable"]),
            "fixed_trip_cost": as_float(row["fixed_trip_cost"]),
            "max_travel_time_hours": as_float(row["max_travel_time_hours"]),
        }

    warehouse_fleet = {}
    for row in wh_fleet_rows:
        warehouse_fleet[(row["warehouse_id"], row["period_id"], row["fleet_type"])] = (
            as_float(row["available_trips"])
        )

    service_targets = {}
    for row in service_rows:
        service_targets[(row["priority_class"], row["item_id"], row["period_id"])] = (
            as_float(row["cumulative_fraction_of_final_floor"])
        )

    expedite_caps = {}
    for row in expcap_rows:
        expedite_caps[(row["scenario_id"], row["item_id"])] = as_float(row["max_expedite"])

    demand = {}
    demand_by_sci = defaultdict(float)
    for row in demand_rows:
        key = (
            row["scenario_id"],
            row["period_id"],
            row["clinic_id"],
            row["item_id"],
        )
        value = as_float(row["demand"])
        demand[key] = value
        demand_by_sci[(row["scenario_id"], row["clinic_id"], row["item_id"])] += value

    supplier_availability = {}
    supplier_avail_max = defaultdict(float)
    for row in sup_avail_rows:
        key = (row["scenario_id"], row["period_id"], row["supplier_id"])
        value = as_float(row["capacity_multiplier"])
        supplier_availability[key] = value
        supplier_avail_max[row["supplier_id"]] = max(
            supplier_avail_max[row["supplier_id"]], value
        )

    supplier_lane_impact = {}
    supplier_lane_cap_max = defaultdict(float)
    for row in sup_lane_rows:
        key = (row["scenario_id"], row["period_id"], row["supplier_id"], row["warehouse_id"])
        cap_mult = as_float(row["capacity_multiplier"])
        cost_mult = as_float(row["cost_multiplier"])
        supplier_lane_impact[key] = {
            "capacity_multiplier": cap_mult,
            "cost_multiplier": cost_mult,
        }
        supplier_lane_cap_max[(row["supplier_id"], row["warehouse_id"])] = max(
            supplier_lane_cap_max[(row["supplier_id"], row["warehouse_id"])], cap_mult
        )

    warehouse_availability = {}
    for row in wh_avail_rows:
        warehouse_availability[(row["scenario_id"], row["period_id"], row["warehouse_id"])] = {
            "storage_multiplier": as_float(row["storage_multiplier"]),
            "outbound_multiplier": as_float(row["outbound_multiplier"]),
        }

    road_impact = {}
    affected_lane = {}
    for row in road_rows:
        key = (row["scenario_id"], row["period_id"], row["warehouse_id"], row["clinic_id"])
        road_impact[key] = {
            "capacity_multiplier": as_float(row["capacity_multiplier"]),
            "extra_transport_cost_per_load": as_float(row["extra_transport_cost_per_load"]),
            "travel_time_hours": as_float(row["travel_time_hours"]),
        }
        base = wh_clinic[(row["warehouse_id"], row["clinic_id"])]
        affected_lane[key] = (
            abs(road_impact[key]["capacity_multiplier"] - 1.0) > 1e-9
            or abs(road_impact[key]["extra_transport_cost_per_load"]) > 1e-9
            or abs(road_impact[key]["travel_time_hours"] - base["travel_time_hours"]) > 1e-9
        )

    final_floor_req = {}
    cumulative_floor_req = {}
    for omega in scenario_ids:
        for c in clinic_ids:
            priority = clinics[c]["priority_class"]
            for item in item_ids:
                total = demand_by_sci[(omega, c, item)]
                final_floor_req[(omega, c, item)] = clinics[c]["floors"][item] * total
                running = 0.0
                for period in period_ids:
                    running += demand[(omega, period, c, item)]
                    cumulative_floor_req[(omega, period, c, item)] = (
                        service_targets[(priority, item, period)]
                        * clinics[c]["floors"][item]
                        * running
                    )

    cold_items = [item for item in item_ids if items[item]["cold_chain_required"]]
    critical_clinics = [
        c for c in clinic_ids if clinics[c]["priority_class"] == "critical"
    ]
    noncritical_clinics = [
        c for c in clinic_ids if clinics[c]["priority_class"] != "critical"
    ]

    reservable_pairs = []
    for s in supplier_ids:
        for item in item_ids:
            pair = (s, item)
            feasible = supplier_item[pair]["max_reserved_capacity"] > 0.0
            if items[item]["cold_chain_required"] and not suppliers[s]["cold_chain_certified"]:
                feasible = False
            if feasible:
                reservable_pairs.append(pair)

    prod_keys = []
    inbound_keys = []
    inbound_by_prod = defaultdict(list)
    inbound_by_lane = defaultdict(list)
    inbound_to_wh_item = defaultdict(list)
    for omega in scenario_ids:
        for period in period_ids:
            for s, item in reservable_pairs:
                prod_key = (omega, period, s, item)
                prod_keys.append(prod_key)
                for w in supplier_wh_by_supplier[s]:
                    lane = supplier_wh[(s, w)]
                    if items[item]["cold_chain_required"]:
                        if not lane["cold_chain_supported"] or not warehouses[w]["cold_chain"]:
                            continue
                    in_key = (omega, period, s, w, item)
                    inbound_keys.append(in_key)
                    inbound_by_prod[prod_key].append(in_key)
                    inbound_by_lane[(omega, period, s, w)].append(in_key)
                    inbound_to_wh_item[(omega, period, w, item)].append(in_key)

    dispatch_keys = []
    dispatch_by_demand = defaultdict(list)
    dispatch_by_lane = defaultdict(list)
    dispatch_by_wh_fleet = defaultdict(list)
    dispatch_by_wh_item = defaultdict(list)
    for omega in scenario_ids:
        for period in period_ids:
            for w in warehouse_ids:
                for c in wh_clinic_by_warehouse[w]:
                    lane = wh_clinic[(w, c)]
                    impact = road_impact[(omega, period, w, c)]
                    affected = affected_lane[(omega, period, w, c)]
                    for item in item_ids:
                        if demand[(omega, period, c, item)] <= TOL:
                            continue
                        if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                            continue
                        for fleet in fleet_ids:
                            fleet_data = fleets[fleet]
                            if items[item]["cold_chain_required"] and not fleet_data["cold_chain_capable"]:
                                continue
                            if (
                                affected
                                and lane["road_class"] != "urban"
                                and not fleet_data["rough_road_capable"]
                            ):
                                continue
                            if (
                                not fleet_data["airlift_capable"]
                                and impact["travel_time_hours"]
                                > fleet_data["max_travel_time_hours"] + 1e-9
                            ):
                                continue
                            key = (omega, period, w, c, item, fleet)
                            dispatch_keys.append(key)
                            dispatch_by_demand[(omega, period, c, item)].append(key)
                            dispatch_by_lane[(omega, period, w, c)].append(key)
                            dispatch_by_wh_fleet[(omega, period, w, fleet)].append(key)
                            dispatch_by_wh_item[(omega, period, w, item)].append(key)

    return {
        "config": config,
        "items": items,
        "item_ids": item_ids,
        "warehouses": warehouses,
        "warehouse_ids": warehouse_ids,
        "suppliers": suppliers,
        "supplier_ids": supplier_ids,
        "clinics": clinics,
        "clinic_ids": clinic_ids,
        "periods": periods,
        "period_ids": period_ids,
        "period_index": period_index,
        "prev_period": prev_period,
        "scenarios": scenarios,
        "scenario_ids": scenario_ids,
        "supplier_item": supplier_item,
        "supplier_wh": supplier_wh,
        "supplier_wh_by_supplier": supplier_wh_by_supplier,
        "wh_clinic": wh_clinic,
        "wh_clinic_by_warehouse": wh_clinic_by_warehouse,
        "fleets": fleets,
        "fleet_ids": fleet_ids,
        "warehouse_fleet": warehouse_fleet,
        "service_targets": service_targets,
        "expedite_caps": expedite_caps,
        "demand": demand,
        "demand_by_sci": demand_by_sci,
        "supplier_availability": supplier_availability,
        "supplier_avail_max": supplier_avail_max,
        "supplier_lane_impact": supplier_lane_impact,
        "supplier_lane_cap_max": supplier_lane_cap_max,
        "warehouse_availability": warehouse_availability,
        "road_impact": road_impact,
        "affected_lane": affected_lane,
        "final_floor_req": final_floor_req,
        "cumulative_floor_req": cumulative_floor_req,
        "cold_items": cold_items,
        "critical_clinics": critical_clinics,
        "noncritical_clinics": noncritical_clinics,
        "reservable_pairs": reservable_pairs,
        "prod_keys": prod_keys,
        "inbound_keys": inbound_keys,
        "inbound_by_prod": inbound_by_prod,
        "inbound_by_lane": inbound_by_lane,
        "inbound_to_wh_item": inbound_to_wh_item,
        "dispatch_keys": dispatch_keys,
        "dispatch_by_demand": dispatch_by_demand,
        "dispatch_by_lane": dispatch_by_lane,
        "dispatch_by_wh_fleet": dispatch_by_wh_fleet,
        "dispatch_by_wh_item": dispatch_by_wh_item,
    }


def build_model(data, time_limit):
    config = data["config"]
    items = data["items"]
    warehouses = data["warehouses"]
    suppliers = data["suppliers"]
    clinics = data["clinics"]
    periods = data["periods"]
    scenarios = data["scenarios"]
    supplier_item = data["supplier_item"]
    supplier_wh = data["supplier_wh"]
    wh_clinic = data["wh_clinic"]
    fleets = data["fleets"]

    model = Model("medical_supply_dro_response")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", float(time_limit))

    y_open = {
        w: model.addVar(vtype="B", name=f"open[{w}]") for w in data["warehouse_ids"]
    }
    y_active = {
        s: model.addVar(vtype="B", name=f"active[{s}]") for s in data["supplier_ids"]
    }

    stock = {}
    for w in data["warehouse_ids"]:
        for item in data["item_ids"]:
            ub = items[item]["max_stock_per_warehouse"]
            if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                ub = 0.0
            stock[(w, item)] = model.addVar(lb=0.0, ub=ub, name=f"stock[{w},{item}]")

    reserve = {}
    for s in data["supplier_ids"]:
        for item in data["item_ids"]:
            ub = supplier_item[(s, item)]["max_reserved_capacity"]
            if items[item]["cold_chain_required"] and not suppliers[s]["cold_chain_certified"]:
                ub = 0.0
            reserve[(s, item)] = model.addVar(
                lb=0.0, ub=ub, name=f"reserve[{s},{item}]"
            )

    run = {}
    produce = {}
    for omega, period, s, item in data["prod_keys"]:
        max_cap = supplier_item[(s, item)]["max_reserved_capacity"]
        max_mult = data["supplier_avail_max"][s]
        run[(omega, period, s, item)] = model.addVar(
            vtype="B", name=f"run[{omega},{period},{s},{item}]"
        )
        produce[(omega, period, s, item)] = model.addVar(
            lb=0.0,
            ub=max_cap * max_mult,
            name=f"prod[{omega},{period},{s},{item}]",
        )

    inbound = {}
    for omega, period, s, w, item in data["inbound_keys"]:
        lane_cap = supplier_wh[(s, w)]["base_capacity"]
        ub = min(
            supplier_item[(s, item)]["max_reserved_capacity"] * data["supplier_avail_max"][s],
            lane_cap
            * data["supplier_lane_cap_max"][(s, w)]
            / max(items[item]["load_factor"], 1e-9),
        )
        inbound[(omega, period, s, w, item)] = model.addVar(
            lb=0.0,
            ub=ub,
            name=f"in[{omega},{period},{s},{w},{item}]",
        )

    dispatch = {}
    for omega, period, w, c, item, fleet in data["dispatch_keys"]:
        dispatch[(omega, period, w, c, item, fleet)] = model.addVar(
            lb=0.0,
            ub=data["demand"][(omega, period, c, item)],
            name=f"disp[{omega},{period},{w},{c},{item},{fleet}]",
        )

    expedite = {}
    unmet = {}
    served_cum = {}
    for omega in data["scenario_ids"]:
        for period in data["period_ids"]:
            for c in data["clinic_ids"]:
                for item in data["item_ids"]:
                    demand_value = data["demand"][(omega, period, c, item)]
                    expedite[(omega, period, c, item)] = model.addVar(
                        lb=0.0,
                        ub=clinics[c]["local_procure_fraction"] * demand_value,
                        name=f"exp[{omega},{period},{c},{item}]",
                    )
                    unmet[(omega, period, c, item)] = model.addVar(
                        lb=0.0,
                        ub=demand_value,
                        name=f"unmet[{omega},{period},{c},{item}]",
                    )
                    served_cum[(omega, period, c, item)] = model.addVar(
                        lb=0.0,
                        ub=data["demand_by_sci"][(omega, c, item)],
                        name=f"served[{omega},{period},{c},{item}]",
                    )

    inventory = {}
    quarantine = {}
    for omega in data["scenario_ids"]:
        for period in data["period_ids"]:
            for w in data["warehouse_ids"]:
                for item in data["item_ids"]:
                    ub = warehouses[w]["storage_capacity"] / max(
                        items[item]["unit_volume"], 1e-9
                    )
                    if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                        ub = 0.0
                    inventory[(omega, period, w, item)] = model.addVar(
                        lb=0.0,
                        ub=ub,
                        name=f"inv[{omega},{period},{w},{item}]",
                    )
                    quarantine[(omega, period, w, item)] = model.addVar(
                        lb=0.0,
                        ub=ub,
                        name=f"quar[{omega},{period},{w},{item}]",
                    )

    op_cost = {
        omega: model.addVar(lb=0.0, name=f"opcost[{omega}]")
        for omega in data["scenario_ids"]
    }
    eta = model.addVar(lb=None, name="eta")
    cvar_excess = {
        omega: model.addVar(lb=0.0, name=f"cvar[{omega}]")
        for omega in data["scenario_ids"]
    }
    amb_lam = model.addVar(lb=None, name="amb_lam")
    amb_gamma = model.addVar(lb=0.0, name="amb_gamma")
    amb_alpha = {
        omega: model.addVar(lb=0.0, name=f"amb_alpha[{omega}]")
        for omega in data["scenario_ids"]
    }
    amb_beta = {
        omega: model.addVar(lb=0.0, name=f"amb_beta[{omega}]")
        for omega in data["scenario_ids"]
    }

    first_stage_cost = quicksum(
        warehouses[w]["open_cost"] * y_open[w] for w in data["warehouse_ids"]
    ) + quicksum(
        suppliers[s]["activation_cost"] * y_active[s] for s in data["supplier_ids"]
    ) + quicksum(
        items[item]["preposition_cost"] * stock[(w, item)]
        for w in data["warehouse_ids"]
        for item in data["item_ids"]
    ) + quicksum(
        items[item]["capacity_reserve_cost"] * reserve[(s, item)]
        for s in data["supplier_ids"]
        for item in data["item_ids"]
    )

    model.addCons(
        quicksum(y_open[w] for w in data["warehouse_ids"])
        <= config["max_open_warehouses"],
        name="max_open_warehouses",
    )
    model.addCons(
        quicksum(
            y_open[w]
            for w in data["warehouse_ids"]
            if warehouses[w]["cold_chain"]
        )
        >= config["min_open_cold_chain_warehouses"],
        name="min_open_cold_chain_warehouses",
    )
    model.addCons(
        quicksum(y_active[s] for s in data["supplier_ids"])
        >= config["min_active_suppliers"],
        name="min_active_suppliers",
    )
    model.addCons(first_stage_cost <= config["planning_budget"], name="planning_budget")

    for w in data["warehouse_ids"]:
        for item in data["item_ids"]:
            model.addCons(
                stock[(w, item)] <= items[item]["max_stock_per_warehouse"] * y_open[w],
                name=f"stock_open[{w},{item}]",
            )
            if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                model.addCons(stock[(w, item)] == 0.0, name=f"stock_cold[{w},{item}]")

    for s in data["supplier_ids"]:
        for item in data["item_ids"]:
            model.addCons(
                reserve[(s, item)]
                <= supplier_item[(s, item)]["max_reserved_capacity"] * y_active[s],
                name=f"reserve_active[{s},{item}]",
            )
            if (
                items[item]["cold_chain_required"]
                and not suppliers[s]["cold_chain_certified"]
            ):
                model.addCons(
                    reserve[(s, item)] == 0.0, name=f"reserve_cold[{s},{item}]"
                )
        model.addCons(
            quicksum(reserve[(s, item)] for item in data["item_ids"])
            <= suppliers[s]["base_period_capacity"] * y_active[s],
            name=f"reserve_basecap[{s}]",
        )
        model.addCons(
            quicksum(
                supplier_item[(s, item)]["labor_hours_per_unit"] * reserve[(s, item)]
                for item in data["item_ids"]
            )
            <= suppliers[s]["line_hours"] * y_active[s],
            name=f"reserve_linehours[{s}]",
        )

    for omega in data["scenario_ids"]:
        for period in data["period_ids"]:
            for s in data["supplier_ids"]:
                run_keys = [
                    (omega, period, s, item)
                    for item in data["item_ids"]
                    if (omega, period, s, item) in run
                ]
                if run_keys:
                    avail = data["supplier_availability"][(omega, period, s)]
                    model.addCons(
                        quicksum(run[key] for key in run_keys)
                        <= suppliers[s]["max_daily_item_setups"],
                        name=f"setup_limit[{omega},{period},{s}]",
                    )
                    model.addCons(
                        quicksum(produce[key] for key in run_keys)
                        <= suppliers[s]["base_period_capacity"] * avail * y_active[s],
                        name=f"supplier_basecap[{omega},{period},{s}]",
                    )
                    model.addCons(
                        quicksum(
                            supplier_item[(s, key[3])]["labor_hours_per_unit"] * produce[key]
                            + supplier_item[(s, key[3])]["setup_hours"] * run[key]
                            for key in run_keys
                        )
                        <= suppliers[s]["line_hours"] * avail * y_active[s],
                        name=f"supplier_linehours[{omega},{period},{s}]",
                    )

                for item in data["item_ids"]:
                    key = (omega, period, s, item)
                    if key not in produce:
                        continue
                    avail = data["supplier_availability"][(omega, period, s)]
                    model.addCons(
                        produce[key] <= avail * reserve[(s, item)],
                        name=f"prod_reserve[{omega},{period},{s},{item}]",
                    )
                    model.addCons(
                        run[key] <= y_active[s],
                        name=f"run_active[{omega},{period},{s},{item}]",
                    )
                    model.addCons(
                        produce[key]
                        >= supplier_item[(s, item)]["min_batch_quantity"] * run[key],
                        name=f"prod_batch[{omega},{period},{s},{item}]",
                    )
                    model.addCons(
                        produce[key]
                        <= avail
                        * supplier_item[(s, item)]["max_reserved_capacity"]
                        * run[key],
                        name=f"prod_runub[{omega},{period},{s},{item}]",
                    )
                    model.addCons(
                        quicksum(
                            inbound[in_key] for in_key in data["inbound_by_prod"][key]
                        )
                        == produce[key],
                        name=f"prod_flow[{omega},{period},{s},{item}]",
                    )

                for w in data["supplier_wh_by_supplier"][s]:
                    lane_keys = data["inbound_by_lane"].get((omega, period, s, w), [])
                    if not lane_keys:
                        continue
                    model.addCons(
                        quicksum(items[key[4]]["load_factor"] * inbound[key] for key in lane_keys)
                        <= supplier_wh[(s, w)]["base_capacity"]
                        * data["supplier_lane_impact"][(omega, period, s, w)][
                            "capacity_multiplier"
                        ],
                        name=f"sw_lane_cap[{omega},{period},{s},{w}]",
                    )

            for w in data["warehouse_ids"]:
                wh_state = data["warehouse_availability"][(omega, period, w)]
                p_idx = data["period_index"][period]
                prev_p = data["prev_period"][period]
                for item in data["item_ids"]:
                    lag = items[item]["release_lag"]
                    if lag == 0:
                        model.addCons(
                            quarantine[(omega, period, w, item)] == 0.0,
                            name=f"quar_zero[{omega},{period},{w},{item}]",
                        )
                        released_expr = quicksum(
                            inbound[key]
                            for key in data["inbound_to_wh_item"].get(
                                (omega, period, w, item), []
                            )
                        )
                    else:
                        start = max(0, p_idx - lag + 1)
                        model.addCons(
                            quarantine[(omega, period, w, item)]
                            == quicksum(
                                inbound[key]
                                for idx in range(start, p_idx + 1)
                                for key in data["inbound_to_wh_item"].get(
                                    (omega, data["period_ids"][idx], w, item), []
                                )
                            ),
                            name=f"quar_def[{omega},{period},{w},{item}]",
                        )
                        rel_idx = p_idx - lag
                        released_expr = (
                            quicksum(
                                inbound[key]
                                for key in data["inbound_to_wh_item"].get(
                                    (omega, data["period_ids"][rel_idx], w, item), []
                                )
                            )
                            if rel_idx >= 0
                            else 0.0
                        )

                    dispatch_expr = quicksum(
                        dispatch[key]
                        for key in data["dispatch_by_wh_item"].get((omega, period, w, item), [])
                    )
                    if item == "oxygen":
                        ret_idx = p_idx - int(config["oxygen_return_lag_periods"])
                        return_expr = (
                            config["oxygen_return_fraction"]
                            * quicksum(
                                dispatch[key]
                                for key in data["dispatch_by_wh_item"].get(
                                    (omega, data["period_ids"][ret_idx], w, item), []
                                )
                            )
                            if ret_idx >= 0
                            else 0.0
                        )
                    else:
                        return_expr = 0.0

                    rhs = stock[(w, item)] + released_expr + return_expr - dispatch_expr
                    if prev_p is not None:
                        rhs = inventory[(omega, prev_p, w, item)] + released_expr + return_expr - dispatch_expr
                    model.addCons(
                        inventory[(omega, period, w, item)] == rhs,
                        name=f"inv_bal[{omega},{period},{w},{item}]",
                    )
                    if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                        model.addCons(
                            inventory[(omega, period, w, item)] == 0.0,
                            name=f"inv_cold[{omega},{period},{w},{item}]",
                        )
                        model.addCons(
                            quarantine[(omega, period, w, item)] == 0.0,
                            name=f"quar_cold[{omega},{period},{w},{item}]",
                        )

                model.addCons(
                    quicksum(
                        items[item]["load_factor"] * dispatch[key]
                        for item in data["item_ids"]
                        for key in data["dispatch_by_wh_item"].get((omega, period, w, item), [])
                    )
                    <= warehouses[w]["outbound_capacity"]
                    * wh_state["outbound_multiplier"]
                    * y_open[w],
                    name=f"wh_outbound[{omega},{period},{w}]",
                )
                model.addCons(
                    quicksum(
                        items[item]["unit_volume"]
                        * (
                            inventory[(omega, period, w, item)]
                            + quarantine[(omega, period, w, item)]
                        )
                        for item in data["item_ids"]
                    )
                    <= warehouses[w]["storage_capacity"]
                    * wh_state["storage_multiplier"]
                    * y_open[w],
                    name=f"wh_storage[{omega},{period},{w}]",
                )

            for c in data["clinic_ids"]:
                for item in data["item_ids"]:
                    delivered = quicksum(
                        dispatch[key]
                        for key in data["dispatch_by_demand"].get((omega, period, c, item), [])
                    )
                    service_today = delivered + expedite[(omega, period, c, item)]
                    model.addCons(
                        service_today + unmet[(omega, period, c, item)]
                        == data["demand"][(omega, period, c, item)],
                        name=f"demand[{omega},{period},{c},{item}]",
                    )
                    if data["prev_period"][period] is None:
                        model.addCons(
                            served_cum[(omega, period, c, item)] == service_today,
                            name=f"served_init[{omega},{period},{c},{item}]",
                        )
                    else:
                        model.addCons(
                            served_cum[(omega, period, c, item)]
                            == served_cum[(omega, data["prev_period"][period], c, item)]
                            + service_today,
                            name=f"served_step[{omega},{period},{c},{item}]",
                        )
                    model.addCons(
                        served_cum[(omega, period, c, item)]
                        >= data["cumulative_floor_req"][(omega, period, c, item)],
                        name=f"service_floor[{omega},{period},{c},{item}]",
                    )

                model.addCons(
                    quicksum(
                        items[item]["unit_volume"]
                        * (
                            quicksum(
                                dispatch[key]
                                for key in data["dispatch_by_demand"].get(
                                    (omega, period, c, item), []
                                )
                            )
                            + expedite[(omega, period, c, item)]
                        )
                        for item in data["item_ids"]
                    )
                    <= clinics[c]["receiving_capacity"],
                    name=f"receive_cap[{omega},{period},{c}]",
                )
                model.addCons(
                    quicksum(
                        items[item]["unit_volume"]
                        * (
                            quicksum(
                                dispatch[key]
                                for key in data["dispatch_by_demand"].get(
                                    (omega, period, c, item), []
                                )
                            )
                            + expedite[(omega, period, c, item)]
                        )
                        for item in data["cold_items"]
                    )
                    <= clinics[c]["cold_storage_capacity"],
                    name=f"cold_cap[{omega},{period},{c}]",
                )

            for w in data["warehouse_ids"]:
                for c in data["wh_clinic_by_warehouse"][w]:
                    lane_keys = data["dispatch_by_lane"].get((omega, period, w, c), [])
                    if lane_keys:
                        model.addCons(
                            quicksum(items[key[4]]["load_factor"] * dispatch[key] for key in lane_keys)
                            <= wh_clinic[(w, c)]["base_capacity"]
                            * data["road_impact"][(omega, period, w, c)]["capacity_multiplier"],
                            name=f"road_cap[{omega},{period},{w},{c}]",
                        )
                for fleet in data["fleet_ids"]:
                    fleet_keys = data["dispatch_by_wh_fleet"].get((omega, period, w, fleet), [])
                    if fleet_keys:
                        model.addCons(
                            quicksum(
                                items[key[4]]["load_factor"]
                                / fleets[fleet]["capacity_load"]
                                * dispatch[key]
                                for key in fleet_keys
                            )
                            <= data["warehouse_fleet"][(w, period, fleet)] * y_open[w],
                            name=f"fleet_cap[{omega},{period},{w},{fleet}]",
                        )

        for item in data["item_ids"]:
            model.addCons(
                quicksum(
                    expedite[(omega, period, c, item)]
                    for period in data["period_ids"]
                    for c in data["clinic_ids"]
                )
                <= data["expedite_caps"][(omega, item)],
                name=f"exp_cap[{omega},{item}]",
            )

        last_period = data["period_ids"][-1]
        for item in data["item_ids"]:
            for c_crit in data["critical_clinics"]:
                for c_other in data["noncritical_clinics"]:
                    total_crit = data["demand_by_sci"][(omega, c_crit, item)]
                    total_other = data["demand_by_sci"][(omega, c_other, item)]
                    model.addCons(
                        total_other * served_cum[(omega, last_period, c_crit, item)]
                        - total_crit * served_cum[(omega, last_period, c_other, item)]
                        >= -config["fairness_gap"] * total_crit * total_other,
                        name=f"fair[{omega},{item},{c_crit},{c_other}]",
                    )

    for omega in data["scenario_ids"]:
        expr = quicksum(
            items[key[3]]["production_cost"] * produce[key]
            + supplier_item[(key[2], key[3])]["setup_cost"] * run[key]
            for key in data["prod_keys"]
            if key[0] == omega
        )
        expr += quicksum(
            supplier_wh[(key[2], key[3])]["transport_cost_per_load"]
            * data["supplier_lane_impact"][(key[0], key[1], key[2], key[3])]["cost_multiplier"]
            * items[key[4]]["load_factor"]
            * inbound[key]
            for key in data["inbound_keys"]
            if key[0] == omega
        )
        expr += quicksum(
            (
                wh_clinic[(key[2], key[3])]["transport_cost_per_load"]
                + data["road_impact"][(key[0], key[1], key[2], key[3])][
                    "extra_transport_cost_per_load"
                ]
            )
            * items[key[4]]["load_factor"]
            * dispatch[key]
            for key in data["dispatch_keys"]
            if key[0] == omega
        )
        expr += quicksum(
            fleets[key[5]]["fixed_trip_cost"]
            * items[key[4]]["load_factor"]
            / fleets[key[5]]["capacity_load"]
            * dispatch[key]
            for key in data["dispatch_keys"]
            if key[0] == omega
        )
        expr += quicksum(
            config["late_response_penalty_per_load_hour"]
            * periods[key[1]]["delay_multiplier"]
            * data["road_impact"][(key[0], key[1], key[2], key[3])]["travel_time_hours"]
            * items[key[4]]["load_factor"]
            * dispatch[key]
            for key in data["dispatch_keys"]
            if key[0] == omega
        )
        expr += quicksum(
            items[key[3]]["expedite_cost"] * expedite[key]
            + items[key[3]]["shortage_penalty"] * unmet[key]
            for key in expedite
            if key[0] == omega
        )
        expr += quicksum(
            items[key[3]]["holding_cost"] * (inventory[key] + quarantine[key])
            for key in inventory
            if key[0] == omega
        )
        model.addCons(op_cost[omega] == expr, name=f"opcost_def[{omega}]")
        model.addCons(cvar_excess[omega] >= op_cost[omega] - eta, name=f"cvar[{omega}]")
        model.addCons(
            amb_lam + amb_alpha[omega] - amb_beta[omega] >= op_cost[omega],
            name=f"amb1[{omega}]",
        )
        model.addCons(
            amb_gamma >= amb_alpha[omega] + amb_beta[omega],
            name=f"amb2[{omega}]",
        )

    worst_case_expected = amb_lam + quicksum(
        scenarios[omega]["probability"] * (amb_alpha[omega] - amb_beta[omega])
        for omega in data["scenario_ids"]
    ) + config["ambiguity_radius"] * amb_gamma
    cvar_term = eta + (1.0 / (1.0 - config["tail_risk_alpha"])) * quicksum(
        scenarios[omega]["probability"] * cvar_excess[omega]
        for omega in data["scenario_ids"]
    )

    model.setObjective(
        first_stage_cost + worst_case_expected + config["tail_risk_weight"] * cvar_term,
        "minimize",
    )

    return model, {
        "y_open": y_open,
        "y_active": y_active,
        "stock": stock,
        "reserve": reserve,
        "run": run,
        "produce": produce,
        "inbound": inbound,
        "dispatch": dispatch,
        "expedite": expedite,
        "unmet": unmet,
        "served_cum": served_cum,
        "inventory": inventory,
        "quarantine": quarantine,
        "op_cost": op_cost,
        "eta": eta,
        "cvar_excess": cvar_excess,
        "amb_lam": amb_lam,
        "amb_gamma": amb_gamma,
        "amb_alpha": amb_alpha,
        "amb_beta": amb_beta,
    }


def get_solution_value(model, sol, var):
    value = model.getSolVal(sol, var)
    if abs(value) < PRINT_TOL:
        return 0.0
    return value


def write_solution_csv(data, model, variables, solution_path):
    sol = model.getBestSol()
    if sol is None:
        raise RuntimeError("No SCIP incumbent is available for export.")

    rows = []
    for w in data["warehouse_ids"]:
        rows.append(
            {
                "row_type": "warehouse",
                "scenario_id": "",
                "period_id": "",
                "origin_id": w,
                "destination_id": "",
                "clinic_id": "",
                "item_id": "",
                "fleet_type": "",
                "quantity": "",
                "decision": "open"
                if get_solution_value(model, sol, variables["y_open"][w]) >= 0.5
                else "closed",
            }
        )
    for s in data["supplier_ids"]:
        rows.append(
            {
                "row_type": "supplier",
                "scenario_id": "",
                "period_id": "",
                "origin_id": s,
                "destination_id": "",
                "clinic_id": "",
                "item_id": "",
                "fleet_type": "",
                "quantity": "",
                "decision": "active"
                if get_solution_value(model, sol, variables["y_active"][s]) >= 0.5
                else "inactive",
            }
        )
    for w in data["warehouse_ids"]:
        for item in data["item_ids"]:
            rows.append(
                {
                    "row_type": "stock",
                    "scenario_id": "",
                    "period_id": "",
                    "origin_id": w,
                    "destination_id": "",
                    "clinic_id": "",
                    "item_id": item,
                    "fleet_type": "",
                    "quantity": fmt_qty(
                        get_solution_value(model, sol, variables["stock"][(w, item)])
                    ),
                    "decision": "stock",
                }
            )
    for s in data["supplier_ids"]:
        for item in data["item_ids"]:
            rows.append(
                {
                    "row_type": "reserve",
                    "scenario_id": "",
                    "period_id": "",
                    "origin_id": s,
                    "destination_id": "",
                    "clinic_id": "",
                    "item_id": item,
                    "fleet_type": "",
                    "quantity": fmt_qty(
                        get_solution_value(model, sol, variables["reserve"][(s, item)])
                    ),
                    "decision": "reserve",
                }
            )
    for key in sorted(data["prod_keys"]):
        value = get_solution_value(model, sol, variables["produce"][key])
        if value <= PRINT_TOL:
            continue
        omega, period, s, item = key
        rows.append(
            {
                "row_type": "produce",
                "scenario_id": omega,
                "period_id": period,
                "origin_id": s,
                "destination_id": "",
                "clinic_id": "",
                "item_id": item,
                "fleet_type": "",
                "quantity": fmt_qty(value),
                "decision": "produce",
            }
        )
    for key in sorted(data["inbound_keys"]):
        value = get_solution_value(model, sol, variables["inbound"][key])
        if value <= PRINT_TOL:
            continue
        omega, period, s, w, item = key
        rows.append(
            {
                "row_type": "inbound",
                "scenario_id": omega,
                "period_id": period,
                "origin_id": s,
                "destination_id": w,
                "clinic_id": "",
                "item_id": item,
                "fleet_type": "",
                "quantity": fmt_qty(value),
                "decision": "inbound",
            }
        )
    for key in sorted(data["dispatch_keys"]):
        value = get_solution_value(model, sol, variables["dispatch"][key])
        if value <= PRINT_TOL:
            continue
        omega, period, w, c, item, fleet = key
        rows.append(
            {
                "row_type": "dispatch",
                "scenario_id": omega,
                "period_id": period,
                "origin_id": w,
                "destination_id": "",
                "clinic_id": c,
                "item_id": item,
                "fleet_type": fleet,
                "quantity": fmt_qty(value),
                "decision": "dispatch",
            }
        )
    for omega in data["scenario_ids"]:
        for period in data["period_ids"]:
            for c in data["clinic_ids"]:
                for item in data["item_ids"]:
                    rows.append(
                        {
                            "row_type": "expedite",
                            "scenario_id": omega,
                            "period_id": period,
                            "origin_id": "",
                            "destination_id": "",
                            "clinic_id": c,
                            "item_id": item,
                            "fleet_type": "",
                            "quantity": fmt_qty(
                                get_solution_value(
                                    model,
                                    sol,
                                    variables["expedite"][(omega, period, c, item)],
                                )
                            ),
                            "decision": "expedite",
                        }
                    )
                    rows.append(
                        {
                            "row_type": "unmet",
                            "scenario_id": omega,
                            "period_id": period,
                            "origin_id": "",
                            "destination_id": "",
                            "clinic_id": c,
                            "item_id": item,
                            "fleet_type": "",
                            "quantity": fmt_qty(
                                get_solution_value(
                                    model,
                                    sol,
                                    variables["unmet"][(omega, period, c, item)],
                                )
                            ),
                            "decision": "unmet",
                        }
                    )
    with open(solution_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def parse_solution_file(solution_path):
    with open(solution_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != HEADER:
            raise ValueError(f"Unexpected CSV header: {reader.fieldnames}")
        rows = list(reader)

    parsed = {
        "warehouse": {},
        "supplier": {},
        "stock": {},
        "reserve": {},
        "produce": defaultdict(float),
        "inbound": defaultdict(float),
        "dispatch": defaultdict(float),
        "expedite": {},
        "unmet": {},
        "rows": rows,
    }
    issues = []

    def parse_qty(row):
        return 0.0 if row["quantity"].strip() == "" else float(row["quantity"])

    for row in rows:
        row_type = row["row_type"]
        qty = parse_qty(row)
        if qty < -TOL:
            issues.append(f"Negative quantity in row {row}")
        if row_type == "warehouse":
            key = row["origin_id"]
            if key in parsed["warehouse"]:
                issues.append(f"Duplicate warehouse row {key}")
            parsed["warehouse"][key] = row["decision"]
        elif row_type == "supplier":
            key = row["origin_id"]
            if key in parsed["supplier"]:
                issues.append(f"Duplicate supplier row {key}")
            parsed["supplier"][key] = row["decision"]
        elif row_type == "stock":
            key = (row["origin_id"], row["item_id"])
            if key in parsed["stock"]:
                issues.append(f"Duplicate stock row {key}")
            parsed["stock"][key] = qty
        elif row_type == "reserve":
            key = (row["origin_id"], row["item_id"])
            if key in parsed["reserve"]:
                issues.append(f"Duplicate reserve row {key}")
            parsed["reserve"][key] = qty
        elif row_type == "produce":
            key = (row["scenario_id"], row["period_id"], row["origin_id"], row["item_id"])
            parsed["produce"][key] += qty
        elif row_type == "inbound":
            key = (
                row["scenario_id"],
                row["period_id"],
                row["origin_id"],
                row["destination_id"],
                row["item_id"],
            )
            parsed["inbound"][key] += qty
        elif row_type == "dispatch":
            key = (
                row["scenario_id"],
                row["period_id"],
                row["origin_id"],
                row["clinic_id"],
                row["item_id"],
                row["fleet_type"],
            )
            parsed["dispatch"][key] += qty
        elif row_type == "expedite":
            key = (row["scenario_id"], row["period_id"], row["clinic_id"], row["item_id"])
            if key in parsed["expedite"]:
                issues.append(f"Duplicate expedite row {key}")
            parsed["expedite"][key] = qty
        elif row_type == "unmet":
            key = (row["scenario_id"], row["period_id"], row["clinic_id"], row["item_id"])
            if key in parsed["unmet"]:
                issues.append(f"Duplicate unmet row {key}")
            parsed["unmet"][key] = qty
        else:
            issues.append(f"Unknown row_type {row_type}")

    return parsed, issues


def validate_solution(data, solution_path):
    parsed, issues = parse_solution_file(solution_path)
    config = data["config"]
    items = data["items"]
    warehouses = data["warehouses"]
    suppliers = data["suppliers"]
    clinics = data["clinics"]
    periods = data["periods"]
    scenarios = data["scenarios"]
    supplier_item = data["supplier_item"]
    supplier_wh = data["supplier_wh"]
    wh_clinic = data["wh_clinic"]
    fleets = data["fleets"]

    violations = []

    def check(group, lhs, sense, rhs, detail):
        residual = norm_violation(lhs, sense, rhs)
        if residual > 1e-7:
            violations.append((group, residual, detail, lhs, sense, rhs))

    for w in data["warehouse_ids"]:
        if parsed["warehouse"].get(w) not in {"open", "closed"}:
            issues.append(f"Missing or invalid warehouse decision {w}")
    for s in data["supplier_ids"]:
        if parsed["supplier"].get(s) not in {"active", "inactive"}:
            issues.append(f"Missing or invalid supplier decision {s}")
    for w in data["warehouse_ids"]:
        for item in data["item_ids"]:
            if (w, item) not in parsed["stock"]:
                issues.append(f"Missing stock row {(w, item)}")
    for s in data["supplier_ids"]:
        for item in data["item_ids"]:
            if (s, item) not in parsed["reserve"]:
                issues.append(f"Missing reserve row {(s, item)}")
    for omega in data["scenario_ids"]:
        for period in data["period_ids"]:
            for c in data["clinic_ids"]:
                for item in data["item_ids"]:
                    if (omega, period, c, item) not in parsed["expedite"]:
                        issues.append(f"Missing expedite row {(omega, period, c, item)}")
                    if (omega, period, c, item) not in parsed["unmet"]:
                        issues.append(f"Missing unmet row {(omega, period, c, item)}")

    open_count = sum(1 for w in data["warehouse_ids"] if parsed["warehouse"].get(w) == "open")
    cold_open = sum(
        1
        for w in data["warehouse_ids"]
        if warehouses[w]["cold_chain"] and parsed["warehouse"].get(w) == "open"
    )
    active_count = sum(
        1 for s in data["supplier_ids"] if parsed["supplier"].get(s) == "active"
    )
    check("first_stage", open_count, "<=", config["max_open_warehouses"], "max open")
    check(
        "first_stage",
        cold_open,
        ">=",
        config["min_open_cold_chain_warehouses"],
        "min cold open",
    )
    check(
        "first_stage",
        active_count,
        ">=",
        config["min_active_suppliers"],
        "min active suppliers",
    )

    first_stage_cost = 0.0
    for w in data["warehouse_ids"]:
        is_open = 1.0 if parsed["warehouse"].get(w) == "open" else 0.0
        first_stage_cost += warehouses[w]["open_cost"] * is_open
        for item in data["item_ids"]:
            qty = parsed["stock"].get((w, item), 0.0)
            first_stage_cost += items[item]["preposition_cost"] * qty
            check(
                "stock",
                qty,
                "<=",
                items[item]["max_stock_per_warehouse"] * is_open,
                f"stock/open {(w, item)}",
            )
            if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                check("stock", qty, "=", 0.0, f"cold stock {(w, item)}")

    for s in data["supplier_ids"]:
        is_active = 1.0 if parsed["supplier"].get(s) == "active" else 0.0
        first_stage_cost += suppliers[s]["activation_cost"] * is_active
        reserve_sum = 0.0
        reserve_hours = 0.0
        for item in data["item_ids"]:
            qty = parsed["reserve"].get((s, item), 0.0)
            first_stage_cost += items[item]["capacity_reserve_cost"] * qty
            reserve_sum += qty
            reserve_hours += supplier_item[(s, item)]["labor_hours_per_unit"] * qty
            check(
                "reserve",
                qty,
                "<=",
                supplier_item[(s, item)]["max_reserved_capacity"] * is_active,
                f"reserve/active {(s, item)}",
            )
            if (
                items[item]["cold_chain_required"]
                and not suppliers[s]["cold_chain_certified"]
            ):
                check("reserve", qty, "=", 0.0, f"cold reserve {(s, item)}")
        check(
            "reserve",
            reserve_sum,
            "<=",
            suppliers[s]["base_period_capacity"] * is_active,
            f"reserve basecap {s}",
        )
        check(
            "reserve",
            reserve_hours,
            "<=",
            suppliers[s]["line_hours"] * is_active,
            f"reserve linehours {s}",
        )
    check("budget", first_stage_cost, "<=", config["planning_budget"], "planning budget")

    inventory = {}
    quarantine = {}
    served_cum = {}
    scenario_cost = {}

    for omega in data["scenario_ids"]:
        for item in data["item_ids"]:
            total_exp = sum(
                parsed["expedite"].get((omega, period, c, item), 0.0)
                for period in data["period_ids"]
                for c in data["clinic_ids"]
            )
            check(
                "expedite",
                total_exp,
                "<=",
                data["expedite_caps"][(omega, item)],
                f"scenario expedite {(omega, item)}",
            )

        for period in data["period_ids"]:
            p_idx = data["period_index"][period]
            for s in data["supplier_ids"]:
                is_active = 1.0 if parsed["supplier"].get(s) == "active" else 0.0
                prod_sum = 0.0
                line_hours = 0.0
                setup_count = 0.0
                for item in data["item_ids"]:
                    key = (omega, period, s, item)
                    qty = parsed["produce"].get(key, 0.0)
                    if qty > TOL:
                        setup_count += 1.0
                    prod_sum += qty
                    line_hours += supplier_item[(s, item)]["labor_hours_per_unit"] * qty
                    if qty > TOL:
                        line_hours += supplier_item[(s, item)]["setup_hours"]
                        check(
                            "production",
                            qty,
                            ">=",
                            supplier_item[(s, item)]["min_batch_quantity"],
                            f"min batch {key}",
                        )
                    check(
                        "production",
                        qty,
                        "<=",
                        data["supplier_availability"][(omega, period, s)]
                        * parsed["reserve"].get((s, item), 0.0),
                        f"reserve link {key}",
                    )
                    inbound_total = sum(
                        parsed["inbound"].get((omega, period, s, w, item), 0.0)
                        for w in data["supplier_wh_by_supplier"][s]
                    )
                    check("inbound", inbound_total, "=", qty, f"prod flow {key}")
                check(
                    "production",
                    prod_sum,
                    "<=",
                    suppliers[s]["base_period_capacity"]
                    * data["supplier_availability"][(omega, period, s)]
                    * is_active,
                    f"supplier basecap {(omega, period, s)}",
                )
                check(
                    "production",
                    line_hours,
                    "<=",
                    suppliers[s]["line_hours"]
                    * data["supplier_availability"][(omega, period, s)]
                    * is_active,
                    f"supplier linehours {(omega, period, s)}",
                )
                check(
                    "production",
                    setup_count,
                    "<=",
                    suppliers[s]["max_daily_item_setups"],
                    f"setup count {(omega, period, s)}",
                )

                for w in data["supplier_wh_by_supplier"][s]:
                    lane_load = 0.0
                    for item in data["item_ids"]:
                        qty = parsed["inbound"].get((omega, period, s, w, item), 0.0)
                        if qty > TOL and items[item]["cold_chain_required"]:
                            if (
                                not supplier_wh[(s, w)]["cold_chain_supported"]
                                or not warehouses[w]["cold_chain"]
                            ):
                                check(
                                    "inbound",
                                    qty,
                                    "=",
                                    0.0,
                                    f"cold inbound {(omega, period, s, w, item)}",
                                )
                        lane_load += items[item]["load_factor"] * qty
                    check(
                        "inbound",
                        lane_load,
                        "<=",
                        supplier_wh[(s, w)]["base_capacity"]
                        * data["supplier_lane_impact"][(omega, period, s, w)][
                            "capacity_multiplier"
                        ],
                        f"sw lane {(omega, period, s, w)}",
                    )

            for w in data["warehouse_ids"]:
                is_open = 1.0 if parsed["warehouse"].get(w) == "open" else 0.0
                wh_state = data["warehouse_availability"][(omega, period, w)]
                out_load = 0.0
                storage = 0.0
                for item in data["item_ids"]:
                    lag = items[item]["release_lag"]
                    if lag == 0:
                        quar_qty = 0.0
                        released = sum(
                            parsed["inbound"].get((omega, period, s, w, item), 0.0)
                            for s in data["supplier_ids"]
                        )
                    else:
                        quar_qty = 0.0
                        for idx in range(max(0, p_idx - lag + 1), p_idx + 1):
                            tau = data["period_ids"][idx]
                            quar_qty += sum(
                                parsed["inbound"].get((omega, tau, s, w, item), 0.0)
                                for s in data["supplier_ids"]
                            )
                        rel_idx = p_idx - lag
                        if rel_idx >= 0:
                            rel_period = data["period_ids"][rel_idx]
                            released = sum(
                                parsed["inbound"].get((omega, rel_period, s, w, item), 0.0)
                                for s in data["supplier_ids"]
                            )
                        else:
                            released = 0.0
                    dispatch_qty = sum(
                        parsed["dispatch"].get((omega, period, w, c, item, fleet), 0.0)
                        for c in data["wh_clinic_by_warehouse"][w]
                        for fleet in data["fleet_ids"]
                    )
                    if item == "oxygen":
                        ret_idx = p_idx - int(config["oxygen_return_lag_periods"])
                        returned = 0.0
                        if ret_idx >= 0:
                            ret_period = data["period_ids"][ret_idx]
                            returned = config["oxygen_return_fraction"] * sum(
                                parsed["dispatch"].get(
                                    (omega, ret_period, w, c, item, fleet), 0.0
                                )
                                for c in data["wh_clinic_by_warehouse"][w]
                                for fleet in data["fleet_ids"]
                            )
                    else:
                        returned = 0.0
                    if p_idx == 0:
                        inv_qty = parsed["stock"].get((w, item), 0.0) + released + returned - dispatch_qty
                    else:
                        prev_period = data["period_ids"][p_idx - 1]
                        inv_qty = inventory[(omega, prev_period, w, item)] + released + returned - dispatch_qty
                    inventory[(omega, period, w, item)] = inv_qty
                    quarantine[(omega, period, w, item)] = quar_qty
                    check("inventory", inv_qty, ">=", 0.0, f"inventory {(omega, period, w, item)}")
                    if items[item]["cold_chain_required"] and not warehouses[w]["cold_chain"]:
                        check("inventory", inv_qty, "=", 0.0, f"cold inventory {(omega, period, w, item)}")
                        check("inventory", quar_qty, "=", 0.0, f"cold quarantine {(omega, period, w, item)}")
                    out_load += items[item]["load_factor"] * dispatch_qty
                    storage += items[item]["unit_volume"] * (inv_qty + quar_qty)
                check(
                    "warehouse",
                    out_load,
                    "<=",
                    warehouses[w]["outbound_capacity"] * wh_state["outbound_multiplier"] * is_open,
                    f"warehouse outbound {(omega, period, w)}",
                )
                check(
                    "warehouse",
                    storage,
                    "<=",
                    warehouses[w]["storage_capacity"] * wh_state["storage_multiplier"] * is_open,
                    f"warehouse storage {(omega, period, w)}",
                )

            for c in data["clinic_ids"]:
                receive_vol = 0.0
                cold_vol = 0.0
                for item in data["item_ids"]:
                    delivered = sum(
                        parsed["dispatch"].get((omega, period, w, c, item, fleet), 0.0)
                        for w in data["warehouse_ids"]
                        for fleet in data["fleet_ids"]
                    )
                    exp_qty = parsed["expedite"].get((omega, period, c, item), 0.0)
                    unmet_qty = parsed["unmet"].get((omega, period, c, item), 0.0)
                    check(
                        "demand",
                        delivered + exp_qty + unmet_qty,
                        "=",
                        data["demand"][(omega, period, c, item)],
                        f"demand balance {(omega, period, c, item)}",
                    )
                    check(
                        "expedite",
                        exp_qty,
                        "<=",
                        clinics[c]["local_procure_fraction"] * data["demand"][(omega, period, c, item)],
                        f"local expedite {(omega, period, c, item)}",
                    )
                    service_today = delivered + exp_qty
                    if p_idx == 0:
                        served_cum[(omega, period, c, item)] = service_today
                    else:
                        served_cum[(omega, period, c, item)] = (
                            served_cum[(omega, data["period_ids"][p_idx - 1], c, item)] + service_today
                        )
                    check(
                        "service",
                        served_cum[(omega, period, c, item)],
                        ">=",
                        data["cumulative_floor_req"][(omega, period, c, item)],
                        f"service floor {(omega, period, c, item)}",
                    )
                    receive_vol += items[item]["unit_volume"] * service_today
                    if item in data["cold_items"]:
                        cold_vol += items[item]["unit_volume"] * service_today
                check(
                    "clinic",
                    receive_vol,
                    "<=",
                    clinics[c]["receiving_capacity"],
                    f"receiving cap {(omega, period, c)}",
                )
                check(
                    "clinic",
                    cold_vol,
                    "<=",
                    clinics[c]["cold_storage_capacity"],
                    f"cold cap {(omega, period, c)}",
                )

            for w in data["warehouse_ids"]:
                for c in data["wh_clinic_by_warehouse"][w]:
                    lane = wh_clinic[(w, c)]
                    road = data["road_impact"][(omega, period, w, c)]
                    lane_load = 0.0
                    for item in data["item_ids"]:
                        for fleet in data["fleet_ids"]:
                            qty = parsed["dispatch"].get((omega, period, w, c, item, fleet), 0.0)
                            lane_load += items[item]["load_factor"] * qty
                            if qty <= TOL:
                                continue
                            if items[item]["cold_chain_required"] and not fleets[fleet]["cold_chain_capable"]:
                                check("dispatch", qty, "=", 0.0, f"cold fleet {(omega, period, w, c, item, fleet)}")
                            if (
                                data["affected_lane"][(omega, period, w, c)]
                                and lane["road_class"] != "urban"
                                and not fleets[fleet]["rough_road_capable"]
                            ):
                                check("dispatch", qty, "=", 0.0, f"rough road {(omega, period, w, c, item, fleet)}")
                            if (
                                not fleets[fleet]["airlift_capable"]
                                and road["travel_time_hours"] > fleets[fleet]["max_travel_time_hours"] + 1e-9
                            ):
                                check("dispatch", qty, "=", 0.0, f"travel time {(omega, period, w, c, item, fleet)}")
                    check(
                        "dispatch",
                        lane_load,
                        "<=",
                        lane["base_capacity"] * road["capacity_multiplier"],
                        f"road cap {(omega, period, w, c)}",
                    )
                for fleet in data["fleet_ids"]:
                    trips = sum(
                        items[item]["load_factor"] / fleets[fleet]["capacity_load"]
                        * parsed["dispatch"].get((omega, period, w, c, item, fleet), 0.0)
                        for c in data["wh_clinic_by_warehouse"][w]
                        for item in data["item_ids"]
                    )
                    is_open = 1.0 if parsed["warehouse"].get(w) == "open" else 0.0
                    check(
                        "dispatch",
                        trips,
                        "<=",
                        data["warehouse_fleet"][(w, period, fleet)] * is_open,
                        f"fleet cap {(omega, period, w, fleet)}",
                    )

        last_period = data["period_ids"][-1]
        for item in data["item_ids"]:
            for c_crit in data["critical_clinics"]:
                for c_other in data["noncritical_clinics"]:
                    d_crit = data["demand_by_sci"][(omega, c_crit, item)]
                    d_other = data["demand_by_sci"][(omega, c_other, item)]
                    lhs = (
                        d_other * served_cum[(omega, last_period, c_crit, item)]
                        - d_crit * served_cum[(omega, last_period, c_other, item)]
                    )
                    rhs = -config["fairness_gap"] * d_crit * d_other
                    check("fairness", lhs, ">=", rhs, f"fairness {(omega, item, c_crit, c_other)}")

        cost = 0.0
        for period in data["period_ids"]:
            for s in data["supplier_ids"]:
                for item in data["item_ids"]:
                    qty = parsed["produce"].get((omega, period, s, item), 0.0)
                    cost += items[item]["production_cost"] * qty
                    if qty > TOL:
                        cost += supplier_item[(s, item)]["setup_cost"]
                for w in data["supplier_wh_by_supplier"][s]:
                    for item in data["item_ids"]:
                        qty = parsed["inbound"].get((omega, period, s, w, item), 0.0)
                        cost += (
                            supplier_wh[(s, w)]["transport_cost_per_load"]
                            * data["supplier_lane_impact"][(omega, period, s, w)]["cost_multiplier"]
                            * items[item]["load_factor"]
                            * qty
                        )
            for w in data["warehouse_ids"]:
                for c in data["wh_clinic_by_warehouse"][w]:
                    road = data["road_impact"][(omega, period, w, c)]
                    base_lane = wh_clinic[(w, c)]
                    for item in data["item_ids"]:
                        for fleet in data["fleet_ids"]:
                            qty = parsed["dispatch"].get((omega, period, w, c, item, fleet), 0.0)
                            if qty <= TOL:
                                continue
                            load = items[item]["load_factor"] * qty
                            cost += (base_lane["transport_cost_per_load"] + road["extra_transport_cost_per_load"]) * load
                            cost += fleets[fleet]["fixed_trip_cost"] * load / fleets[fleet]["capacity_load"]
                            cost += (
                                config["late_response_penalty_per_load_hour"]
                                * periods[period]["delay_multiplier"]
                                * road["travel_time_hours"]
                                * load
                            )
            for c in data["clinic_ids"]:
                for item in data["item_ids"]:
                    cost += items[item]["expedite_cost"] * parsed["expedite"].get((omega, period, c, item), 0.0)
                    cost += items[item]["shortage_penalty"] * parsed["unmet"].get((omega, period, c, item), 0.0)
            for w in data["warehouse_ids"]:
                for item in data["item_ids"]:
                    cost += items[item]["holding_cost"] * (
                        inventory[(omega, period, w, item)] + quarantine[(omega, period, w, item)]
                    )
        scenario_cost[omega] = cost

    probs = {omega: scenarios[omega]["probability"] for omega in data["scenario_ids"]}
    alpha = config["tail_risk_alpha"]
    tail_weight = config["tail_risk_weight"]
    best_eta = None
    best_cvar = None
    sorted_costs = sorted(scenario_cost.values())
    for eta in sorted_costs:
        cvar = eta + sum(probs[o] * max(0.0, scenario_cost[o] - eta) for o in data["scenario_ids"]) / (1.0 - alpha)
        if best_cvar is None or cvar < best_cvar:
            best_cvar = cvar
            best_eta = eta

    expected = sum(probs[o] * scenario_cost[o] for o in data["scenario_ids"])
    remaining = config["ambiguity_radius"] / 2.0
    low = [
        [scenario_cost[o], probs[o], o]
        for o in sorted(data["scenario_ids"], key=lambda x: scenario_cost[x])
    ]
    high = [
        [scenario_cost[o], probs[o], o]
        for o in sorted(data["scenario_ids"], key=lambda x: scenario_cost[x], reverse=True)
    ]
    worst_case_expected = expected
    lo = 0
    hi = 0
    while remaining > 1e-12 and lo < len(low) and hi < len(high):
        low_cost, low_mass, low_id = low[lo]
        high_cost, high_q, high_id = high[hi]
        if low_cost >= high_cost - 1e-12:
            break
        room = 1.0 - high_q
        if low_id == high_id:
            room = 0.0
        move = min(remaining, low_mass, room)
        if move <= 1e-12:
            if low_mass <= 1e-12 or low_id == high_id:
                lo += 1
            if room <= 1e-12:
                hi += 1
            continue
        worst_case_expected += move * (high_cost - low_cost)
        low[lo][1] -= move
        high[hi][1] += move
        remaining -= move
        if low[lo][1] <= 1e-12:
            lo += 1
        if 1.0 - high[hi][1] <= 1e-12:
            hi += 1

    total_objective = first_stage_cost + worst_case_expected + tail_weight * best_cvar
    violations_sorted = sorted(violations, key=lambda item: item[1], reverse=True)
    max_residual = violations_sorted[0][1] if violations_sorted else 0.0
    groups = sorted({item[0] for item in violations_sorted})
    return {
        "issues": issues,
        "violations": violations_sorted,
        "max_normalized_residual": max_residual,
        "violated_groups": groups,
        "violated_group_count": len(groups),
        "schema_ok": len(issues) == 0,
        "feasible": len(issues) == 0 and not violations_sorted,
        "first_stage_cost": first_stage_cost,
        "scenario_cost": scenario_cost,
        "cvar_eta": best_eta,
        "cvar_value": best_cvar,
        "worst_case_expected": worst_case_expected,
        "objective": total_objective,
    }


def solve_baseline_no_production(data, time_limit):
    model, variables = build_model(data, time_limit)
    for var in variables["reserve"].values():
        model.chgVarUb(var, 0.0)
    for var in variables["run"].values():
        model.chgVarUb(var, 0.0)
    for var in variables["produce"].values():
        model.chgVarUb(var, 0.0)
    for var in variables["inbound"].values():
        model.chgVarUb(var, 0.0)
    model.optimize()
    return model, variables


def add_warm_start_from_solution(target_model, target_vars, source_model, source_vars):
    source_sol = source_model.getBestSol()
    if source_sol is None:
        return False
    warm = target_model.createSol()
    containers = [
        "y_open",
        "y_active",
        "stock",
        "reserve",
        "run",
        "produce",
        "inbound",
        "dispatch",
        "expedite",
        "unmet",
        "served_cum",
        "inventory",
        "quarantine",
        "op_cost",
        "cvar_excess",
        "amb_alpha",
        "amb_beta",
    ]
    for name in containers:
        for key, var in target_vars[name].items():
            if key in source_vars[name]:
                target_model.setSolVal(
                    warm, var, source_model.getSolVal(source_sol, source_vars[name][key])
                )
    target_model.setSolVal(warm, target_vars["eta"], source_model.getSolVal(source_sol, source_vars["eta"]))
    target_model.setSolVal(
        warm,
        target_vars["amb_lam"],
        source_model.getSolVal(source_sol, source_vars["amb_lam"]),
    )
    target_model.setSolVal(
        warm,
        target_vars["amb_gamma"],
        source_model.getSolVal(source_sol, source_vars["amb_gamma"]),
    )
    return target_model.addSol(warm, free=True)


def main():
    start = time.time()
    os.makedirs(SUBMISSIONS_DIR, exist_ok=True)
    data = load_data()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    time_limit = 300.0
    if env_limit is not None:
        time_limit = min(time_limit, float(env_limit))

    baseline_limit = min(30.0, max(10.0, 0.15 * time_limit))
    baseline_model, baseline_vars = solve_baseline_no_production(data, baseline_limit)
    baseline_status = str(baseline_model.getStatus())
    if baseline_model.getBestSol() is None:
        print(f"Baseline status: {baseline_status}")
        print("Baseline restoration model found no incumbent.")
        raise SystemExit(1)

    remaining_time = max(1.0, time_limit - (time.time() - start))
    model, variables = build_model(data, remaining_time)
    warm_stored = add_warm_start_from_solution(model, variables, baseline_model, baseline_vars)
    model.optimize()
    status = str(model.getStatus())
    nsols = model.getNSols()

    chosen_model = model
    chosen_vars = variables
    chosen_status = status
    if nsols <= 0 or model.getBestSol() is None:
        chosen_model = baseline_model
        chosen_vars = baseline_vars
        chosen_status = f"fallback_to_baseline_after_{status}"

    write_solution_csv(data, chosen_model, chosen_vars, SOLUTION_PATH)
    audit = validate_solution(data, SOLUTION_PATH)

    print(f"Baseline status: {baseline_status}")
    print(f"Warm start stored: {warm_stored}")
    print(f"Chosen status: {chosen_status}")
    print(f"Full-model status: {status}")
    print(f"Full-model incumbents: {nsols}")
    if chosen_model.getBestSol() is not None:
        print(f"Chosen objective (SCIP): {chosen_model.getObjVal():.10f}")
        print(f"Chosen bound: {chosen_model.getDualbound():.10f}")
        print(f"Chosen relative gap: {chosen_model.getGap():.10f}")
    print(f"Audit feasible: {audit['feasible']}")
    print(f"Audit objective: {audit['objective']:.10f}")
    print(f"Max normalized residual: {audit['max_normalized_residual']:.10e}")
    print(f"Violated groups: {audit['violated_group_count']}")
    if audit["issues"]:
        print("Issues:")
        for issue in audit["issues"][:20]:
            print(f"  - {issue}")
    if audit["violations"]:
        print("Top violations:")
        for group, residual, detail, lhs, sense, rhs in audit["violations"][:20]:
            print(
                f"  - {group}: residual={residual:.3e} detail={detail} lhs={lhs:.6f} {sense} rhs={rhs:.6f}"
            )
    print(f"Elapsed seconds: {time.time() - start:.2f}")


if __name__ == "__main__":
    main()
