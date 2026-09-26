import csv
import json
import math
import os
import time
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
CANDIDATE_PATH = SUBMISSIONS_DIR / "candidate_solution.csv"

SCHEMA = [
    "row_type",
    "scenario_id",
    "zone_id",
    "base_id",
    "backup_base_id",
    "drones",
    "kits",
    "mode",
    "priority",
]

EPS = 1e-8


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def parse_int(value):
    return int(str(value).strip())


def parse_float(value):
    return float(str(value).strip())


def format_float(value):
    return f"{value:.8f}"


def choose_tail_scenarios(scenarios, tail_count):
    ordered = sorted(
        scenarios,
        key=lambda s: (
            -s["weather_severity"],
            -s["call_multiplier"],
            -s["ground_time_multiplier"],
            s["drone_speed_multiplier"],
            s["scenario_id"],
        ),
    )
    return [s["scenario_id"] for s in ordered[:tail_count]]


def build_data():
    with (DATA_DIR / "config.json").open() as f:
        config = json.load(f)

    bases_raw = read_csv(DATA_DIR / "candidate_bases.csv")
    zones_raw = read_csv(DATA_DIR / "demand_zones.csv")
    scenarios_raw = read_csv(DATA_DIR / "scenarios.csv")
    equity_raw = read_csv(DATA_DIR / "equity_requirements.csv")
    ground_raw = read_csv(DATA_DIR / "district_ground_capacity.csv")
    corridor_raw = read_csv(DATA_DIR / "air_corridor_rules.csv")
    exception_raw = read_csv(DATA_DIR / "base_zone_exceptions.csv")
    staffing_raw = read_csv(DATA_DIR / "base_region_staffing.csv")

    bases = []
    for row in bases_raw:
        bases.append(
            {
                "base_id": row["base_id"],
                "name": row["name"],
                "region": row["region"],
                "x_km": parse_float(row["x_km"]),
                "y_km": parse_float(row["y_km"]),
                "fixed_cost": parse_int(row["fixed_cost"]),
                "max_drones": parse_int(row["max_drones"]),
                "kit_capacity": parse_int(row["kit_capacity"]),
                "launch_delay_min": parse_float(row["launch_delay_min"]),
                "range_km": parse_float(row["range_km"]),
                "weather_hardened": parse_int(row["weather_hardened"]),
            }
        )

    zones = []
    for row in zones_raw:
        zones.append(
            {
                "zone_id": row["zone_id"],
                "district": row["district"],
                "equity_group": row["equity_group"],
                "x_km": parse_float(row["x_km"]),
                "y_km": parse_float(row["y_km"]),
                "base_calls_per_day": parse_float(row["base_calls_per_day"]),
                "risk_weight": parse_float(row["risk_weight"]),
                "baseline_ems_min": parse_float(row["baseline_ems_min"]),
                "priority_zone": parse_int(row["priority_zone"]),
            }
        )

    scenarios = []
    for row in scenarios_raw:
        scenarios.append(
            {
                "scenario_id": row["scenario_id"],
                "description": row["description"],
                "probability": parse_float(row["probability"]),
                "call_multiplier": parse_float(row["call_multiplier"]),
                "drone_speed_multiplier": parse_float(row["drone_speed_multiplier"]),
                "ground_time_multiplier": parse_float(row["ground_time_multiplier"]),
                "kit_reserve_factor": parse_float(row["kit_reserve_factor"]),
                "no_fly": parse_int(row["no_fly"]),
                "weather_severity": parse_int(row["weather_severity"]),
            }
        )

    equity = {}
    for row in equity_raw:
        equity[row["equity_group"]] = {
            "response_target_min": parse_float(row["response_target_min"]),
            "min_fast_share": parse_float(row["min_fast_share"]),
            "tail_min_fast_share": parse_float(row["tail_min_fast_share"]),
        }

    ground_capacity = {}
    for row in ground_raw:
        ground_capacity[(row["scenario_id"], row["district"])] = parse_float(
            row["max_ground_calls_per_day"]
        )

    corridor_rows = {}
    for row in corridor_raw:
        corridor_rows[(row["scenario_id"], row["origin_region"], row["destination_district"])] = {
            "max_calls_per_day": parse_float(row["max_calls_per_day"]),
            "extra_minutes": parse_float(row["extra_minutes"]),
            "requires_weather_hardened": parse_int(row["requires_weather_hardened"]),
        }

    exceptions = {}
    for row in exception_raw:
        exceptions[(row["scenario_id"], row["base_id"], row["zone_id"])] = parse_int(
            row["allowed"]
        )

    staffing = {}
    for row in staffing_raw:
        staffing[row["region"]] = {
            "max_open_bases": parse_int(row["max_open_bases"]),
            "max_drones": parse_int(row["max_drones"]),
        }

    base_ids = [b["base_id"] for b in bases]
    zone_ids = [z["zone_id"] for z in zones]
    scenario_ids = [s["scenario_id"] for s in scenarios]
    districts = sorted({z["district"] for z in zones})
    regions = sorted({b["region"] for b in bases})
    groups = sorted(equity)

    base_by_id = {b["base_id"]: b for b in bases}
    zone_by_id = {z["zone_id"]: z for z in zones}
    scenario_by_id = {s["scenario_id"]: s for s in scenarios}

    tail_scenarios = choose_tail_scenarios(scenarios, config["tail_scenario_count"])

    corridor = {}
    for s in scenario_ids:
        for r in regions:
            for d in districts:
                key = (s, r, d)
                if key in corridor_rows:
                    corridor[key] = corridor_rows[key]
                else:
                    corridor[key] = corridor_rows[("ALL", r, d)]

    ground_response = {}
    ground_fast = {}
    ground_risk = {}
    demand = {}
    total_demand = {}
    group_demand = {}

    for s in scenarios:
        sid = s["scenario_id"]
        total = 0.0
        for z in zones:
            zid = z["zone_id"]
            dem = z["base_calls_per_day"] * s["call_multiplier"]
            demand[(sid, zid)] = dem
            total += dem
            resp = z["baseline_ems_min"] * s["ground_time_multiplier"]
            ground_response[(sid, zid)] = resp
            target = equity[z["equity_group"]]["response_target_min"]
            ground_fast[(sid, zid)] = 1 if resp <= target + EPS else 0
            ground_risk[(sid, zid)] = z["risk_weight"] * (
                1.0 - math.exp(-config["death_risk_decay_per_minute"] * resp)
            )
            group_demand[(sid, z["equity_group"])] = group_demand.get(
                (sid, z["equity_group"]), 0.0
            ) + dem
        total_demand[sid] = total

    drone_metrics = {}
    eligible_bases = {}

    for s in scenarios:
        sid = s["scenario_id"]
        speed = config["base_drone_speed_kmph"] * s["drone_speed_multiplier"]
        for z in zones:
            zid = z["zone_id"]
            eligible = []
            for b in bases:
                bid = b["base_id"]
                allowed = math.hypot(b["x_km"] - z["x_km"], b["y_km"] - z["y_km"]) <= b[
                    "range_km"
                ] + EPS
                if ("ALL", bid, zid) in exceptions:
                    allowed = bool(exceptions[("ALL", bid, zid)])
                if (sid, bid, zid) in exceptions:
                    allowed = bool(exceptions[(sid, bid, zid)])
                if s["no_fly"]:
                    allowed = False
                rule = corridor[(sid, b["region"], z["district"])]
                if rule["requires_weather_hardened"] and not b["weather_hardened"]:
                    allowed = False
                if not allowed:
                    continue

                dist = math.hypot(b["x_km"] - z["x_km"], b["y_km"] - z["y_km"])
                travel = 60.0 * dist / speed
                response = b["launch_delay_min"] + travel + rule["extra_minutes"]
                busy = (
                    b["launch_delay_min"]
                    + 2.0 * travel
                    + 2.0 * rule["extra_minutes"]
                    + config["service_minutes_at_scene"]
                    + config["reload_minutes"]
                )
                target = equity[z["equity_group"]]["response_target_min"]
                drone_metrics[(sid, zid, bid)] = {
                    "distance": dist,
                    "response": response,
                    "busy": busy,
                    "fast": 1 if response <= target + EPS else 0,
                    "risk": z["risk_weight"]
                    * (
                        1.0
                        - math.exp(
                            -config["death_risk_decay_per_minute"] * response
                        )
                    ),
                    "kit_need": demand[(sid, zid)] * s["kit_reserve_factor"],
                }
                eligible.append(bid)
            eligible_bases[(sid, zid)] = eligible

    backup_candidates = {}
    primary_candidates = {}
    for s in scenarios:
        sid = s["scenario_id"]
        for z in zones:
            zid = z["zone_id"]
            priority = z["priority_zone"] == 1 and s["no_fly"] == 0
            primaries = []
            for bid in eligible_bases[(sid, zid)]:
                if priority:
                    compatible = []
                    primary_resp = drone_metrics[(sid, zid, bid)]["response"]
                    for b2 in eligible_bases[(sid, zid)]:
                        if b2 == bid:
                            continue
                        if (
                            drone_metrics[(sid, zid, b2)]["response"]
                            <= primary_resp + config["high_priority_backup_extra_minutes"] + EPS
                        ):
                            compatible.append(b2)
                    if compatible:
                        backup_candidates[(sid, zid, bid)] = compatible
                        primaries.append(bid)
                else:
                    primaries.append(bid)
            primary_candidates[(sid, zid)] = primaries

    zone_priority_value = {}
    for z in zones:
        if z["priority_zone"] == 1:
            zone_priority_value[z["zone_id"]] = 1
        elif z["baseline_ems_min"] >= 12.0 or z["risk_weight"] >= 1.15:
            zone_priority_value[z["zone_id"]] = 2
        else:
            zone_priority_value[z["zone_id"]] = 3

    return {
        "config": config,
        "bases": bases,
        "zones": zones,
        "scenarios": scenarios,
        "equity": equity,
        "staffing": staffing,
        "ground_capacity": ground_capacity,
        "corridor": corridor,
        "demand": demand,
        "total_demand": total_demand,
        "group_demand": group_demand,
        "ground_response": ground_response,
        "ground_fast": ground_fast,
        "ground_risk": ground_risk,
        "drone_metrics": drone_metrics,
        "eligible_bases": eligible_bases,
        "primary_candidates": primary_candidates,
        "backup_candidates": backup_candidates,
        "tail_scenarios": tail_scenarios,
        "base_ids": base_ids,
        "zone_ids": zone_ids,
        "scenario_ids": scenario_ids,
        "districts": districts,
        "regions": regions,
        "groups": groups,
        "base_by_id": base_by_id,
        "zone_by_id": zone_by_id,
        "scenario_by_id": scenario_by_id,
        "zone_priority_value": zone_priority_value,
    }


def create_model(data, time_limit):
    config = data["config"]
    bases = data["bases"]
    zones = data["zones"]
    scenarios = data["scenarios"]
    staffing = data["staffing"]
    corridor = data["corridor"]
    demand = data["demand"]
    drone_metrics = data["drone_metrics"]
    primary_candidates = data["primary_candidates"]
    base_by_id = data["base_by_id"]
    tail_scenarios = set(data["tail_scenarios"])

    model = Model("naloxone_drone_response")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("randomization/randomseedshift", 7)
    model.setParam("presolving/maxrounds", 0 if time_limit < 20 else -1)

    y = {
        b["base_id"]: model.addVar(vtype="B", name=f"open[{b['base_id']}]")
        for b in bases
    }
    d = {
        b["base_id"]: model.addVar(
            vtype="I", lb=0, ub=b["max_drones"], name=f"drones[{b['base_id']}]"
        )
        for b in bases
    }
    k = {
        b["base_id"]: model.addVar(
            vtype="I", lb=0, ub=b["kit_capacity"], name=f"kits[{b['base_id']}]"
        )
        for b in bases
    }
    g = {}
    x = {}
    for s in scenarios:
        sid = s["scenario_id"]
        for z in zones:
            zid = z["zone_id"]
            g[(sid, zid)] = model.addVar(vtype="B", name=f"ground[{sid},{zid}]")
            for bid in primary_candidates[(sid, zid)]:
                x[(sid, zid, bid)] = model.addVar(
                    vtype="B", name=f"drone[{sid},{zid},{bid}]"
                )

    model.addCons(
        quicksum(y[bid] for bid in data["base_ids"]) >= config["min_open_bases"],
        name="min_open_bases",
    )
    model.addCons(
        quicksum(y[bid] for bid in data["base_ids"]) <= config["max_open_bases"],
        name="max_open_bases",
    )

    weekly_cost_expr = (
        quicksum(base_by_id[bid]["fixed_cost"] * y[bid] for bid in data["base_ids"])
        + config["drone_weekly_cost"] * quicksum(d[bid] for bid in data["base_ids"])
        + config["kit_unit_cost"] * quicksum(k[bid] for bid in data["base_ids"])
    )
    model.addCons(weekly_cost_expr <= config["weekly_budget"], name="budget")

    model.addCons(
        quicksum(d[bid] for bid in data["base_ids"]) <= config["total_drones_available"],
        name="fleet_limit",
    )

    for b in bases:
        bid = b["base_id"]
        model.addCons(d[bid] >= y[bid], name=f"drone_lb[{bid}]")
        model.addCons(d[bid] <= b["max_drones"] * y[bid], name=f"drone_ub[{bid}]")
        model.addCons(k[bid] >= y[bid], name=f"kit_lb[{bid}]")
        model.addCons(k[bid] <= b["kit_capacity"] * y[bid], name=f"kit_ub[{bid}]")

    for region in data["regions"]:
        members = [b["base_id"] for b in bases if b["region"] == region]
        model.addCons(
            quicksum(y[bid] for bid in members) <= staffing[region]["max_open_bases"],
            name=f"region_open[{region}]",
        )
        model.addCons(
            quicksum(d[bid] for bid in members) <= staffing[region]["max_drones"],
            name=f"region_drones[{region}]",
        )

    scenario_response_expr = {}
    scenario_risk_expr = {}
    scenario_ground_expr = {}

    for s in scenarios:
        sid = s["scenario_id"]
        response_terms = []
        risk_terms = []
        ground_terms = []
        for z in zones:
            zid = z["zone_id"]
            choices = [x[(sid, zid, bid)] for bid in primary_candidates[(sid, zid)]]
            model.addCons(
                g[(sid, zid)] + quicksum(choices) == 1,
                name=f"dispatch[{sid},{zid}]",
            )

            dem = demand[(sid, zid)]
            response_terms.append(data["ground_response"][(sid, zid)] * dem * g[(sid, zid)])
            risk_terms.append(data["ground_risk"][(sid, zid)] * dem * g[(sid, zid)])
            ground_terms.append(dem * g[(sid, zid)])

            for bid in primary_candidates[(sid, zid)]:
                var = x[(sid, zid, bid)]
                model.addCons(var <= y[bid], name=f"use_open[{sid},{zid},{bid}]")
                model.addCons(var <= d[bid], name=f"use_drone[{sid},{zid},{bid}]")
                response_terms.append(
                    drone_metrics[(sid, zid, bid)]["response"] * dem * var
                )
                risk_terms.append(drone_metrics[(sid, zid, bid)]["risk"] * dem * var)
                if z["priority_zone"] == 1 and s["no_fly"] == 0:
                    model.addCons(
                        var
                        <= quicksum(
                            y[b2] for b2 in data["backup_candidates"][(sid, zid, bid)]
                        ),
                        name=f"backup_exists[{sid},{zid},{bid}]",
                    )

        scenario_response_expr[sid] = quicksum(response_terms)
        scenario_risk_expr[sid] = quicksum(risk_terms)
        scenario_ground_expr[sid] = quicksum(ground_terms)

    service_minutes = config["daily_service_hours"] * 60.0
    for s in scenarios:
        sid = s["scenario_id"]
        for b in bases:
            bid = b["base_id"]
            lhs = quicksum(
                demand[(sid, z["zone_id"])]
                * drone_metrics[(sid, z["zone_id"], bid)]["busy"]
                * x[(sid, z["zone_id"], bid)]
                for z in zones
                if (sid, z["zone_id"], bid) in x
            )
            model.addCons(
                lhs <= service_minutes * config["max_queue_utilization"] * d[bid],
                name=f"queue[{sid},{bid}]",
            )

            kit_lhs = quicksum(
                drone_metrics[(sid, z["zone_id"], bid)]["kit_need"]
                * x[(sid, z["zone_id"], bid)]
                for z in zones
                if (sid, z["zone_id"], bid) in x
            )
            model.addCons(kit_lhs <= k[bid], name=f"kits[{sid},{bid}]")

    for s in scenarios:
        sid = s["scenario_id"]
        for region in data["regions"]:
            region_bases = [b["base_id"] for b in bases if b["region"] == region]
            for district in data["districts"]:
                cap = corridor[(sid, region, district)]["max_calls_per_day"]
                lhs = quicksum(
                    demand[(sid, z["zone_id"])] * x[(sid, z["zone_id"], bid)]
                    for z in zones
                    if z["district"] == district
                    for bid in region_bases
                    if (sid, z["zone_id"], bid) in x
                )
                model.addCons(
                    lhs <= cap, name=f"corridor[{sid},{region},{district}]"
                )

        for district in data["districts"]:
            lhs = quicksum(
                demand[(sid, z["zone_id"])] * g[(sid, z["zone_id"])]
                for z in zones
                if z["district"] == district
            )
            model.addCons(
                lhs <= data["ground_capacity"][(sid, district)],
                name=f"ground_cap[{sid},{district}]",
            )

        if s["no_fly"] == 0:
            model.addCons(
                scenario_ground_expr[sid]
                <= config["max_ground_share_flyable"] * data["total_demand"][sid],
                name=f"ground_share[{sid}]",
            )

        for group in data["groups"]:
            threshold = (
                data["equity"][group]["tail_min_fast_share"]
                if sid in tail_scenarios
                else data["equity"][group]["min_fast_share"]
            )
            lhs_terms = []
            for z in zones:
                if z["equity_group"] != group:
                    continue
                zid = z["zone_id"]
                dem = demand[(sid, zid)]
                lhs_terms.append(data["ground_fast"][(sid, zid)] * dem * g[(sid, zid)])
                for bid in primary_candidates[(sid, zid)]:
                    lhs_terms.append(
                        drone_metrics[(sid, zid, bid)]["fast"]
                        * dem
                        * x[(sid, zid, bid)]
                    )
            model.addCons(
                quicksum(lhs_terms) >= threshold * data["group_demand"][(sid, group)],
                name=f"equity[{sid},{group}]",
            )

    expected_response = quicksum(
        data["scenario_by_id"][sid]["probability"] * scenario_response_expr[sid]
        for sid in data["scenario_ids"]
    )
    tail_response = (
        quicksum(scenario_response_expr[sid] for sid in data["tail_scenarios"])
        / len(data["tail_scenarios"])
    )
    expected_risk = quicksum(
        data["scenario_by_id"][sid]["probability"] * scenario_risk_expr[sid]
        for sid in data["scenario_ids"]
    )
    expected_ground = quicksum(
        data["scenario_by_id"][sid]["probability"] * scenario_ground_expr[sid]
        for sid in data["scenario_ids"]
    )

    weights = config["weights"]
    objective = (
        weights["weekly_cost"] * weekly_cost_expr
        + weights["expected_response_minutes"] * expected_response
        + weights["tail_response_minutes"] * tail_response
        + weights["mortality_risk"] * expected_risk
        + weights["ground_fallback"] * expected_ground
    )
    model.setObjective(objective, "minimize")

    return model, {"open": y, "drones": d, "kits": k, "ground": g, "drone": x}


def integral_value(value):
    rounded = int(round(value))
    if abs(value - rounded) > 1e-5:
        raise ValueError(f"Expected near-integer value, found {value}")
    return rounded


def write_solution_rows(rows, path):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def export_solution(data, vars_dict, model, path):
    sol = model.getBestSol()
    if sol is None:
        raise RuntimeError("SCIP reported no incumbent solution.")

    y = vars_dict["open"]
    d = vars_dict["drones"]
    k = vars_dict["kits"]
    g = vars_dict["ground"]
    x = vars_dict["drone"]

    rows = []
    open_bases = {}
    drones = {}
    kits = {}
    for bid in data["base_ids"]:
        open_flag = integral_value(model.getSolVal(sol, y[bid]))
        open_bases[bid] = open_flag
        drones[bid] = integral_value(model.getSolVal(sol, d[bid]))
        kits[bid] = integral_value(model.getSolVal(sol, k[bid]))
        rows.append(
            {
                "row_type": "base",
                "scenario_id": "",
                "zone_id": "",
                "base_id": bid,
                "backup_base_id": "",
                "drones": str(drones[bid]),
                "kits": str(kits[bid]),
                "mode": "open" if open_flag else "closed",
                "priority": "",
            }
        )

    for sid in data["scenario_ids"]:
        for zid in data["zone_ids"]:
            ground_val = model.getSolVal(sol, g[(sid, zid)])
            if ground_val >= 0.5:
                primary = ""
                backup = ""
                mode = "ground"
            else:
                chosen = [
                    bid
                    for bid in data["primary_candidates"][(sid, zid)]
                    if model.getSolVal(sol, x[(sid, zid, bid)]) >= 0.5
                ]
                if len(chosen) != 1:
                    raise RuntimeError(
                        f"Expected one primary base for {(sid, zid)}, found {chosen}"
                    )
                primary = chosen[0]
                mode = "drone"
                if (
                    data["zone_by_id"][zid]["priority_zone"] == 1
                    and data["scenario_by_id"][sid]["no_fly"] == 0
                ):
                    candidates = [
                        bid
                        for bid in data["backup_candidates"][(sid, zid, primary)]
                        if open_bases[bid] == 1
                    ]
                    if not candidates:
                        raise RuntimeError(
                            f"No open backup base for priority zone {(sid, zid, primary)}"
                        )
                    backup = min(
                        candidates,
                        key=lambda bid: (
                            data["drone_metrics"][(sid, zid, bid)]["response"],
                            bid,
                        ),
                    )
                else:
                    backup = ""

            rows.append(
                {
                    "row_type": "dispatch",
                    "scenario_id": sid,
                    "zone_id": zid,
                    "base_id": primary,
                    "backup_base_id": backup,
                    "drones": "",
                    "kits": "",
                    "mode": mode,
                    "priority": str(data["zone_priority_value"][zid]),
                }
            )

    write_solution_rows(rows, path)


def parse_solution_artifact(data, solution_path):
    rows = read_csv(solution_path)
    if not rows:
        raise ValueError("Solution file is empty.")
    if list(rows[0].keys()) != SCHEMA:
        raise ValueError("Solution schema does not match required columns.")

    for row in rows:
        if row["row_type"] not in {"base", "dispatch"}:
            raise ValueError(f"Invalid row_type {row['row_type']}")

    base_rows = [r for r in rows if r["row_type"] == "base"]
    dispatch_rows = [r for r in rows if r["row_type"] == "dispatch"]
    if len(base_rows) != len(data["base_ids"]):
        raise ValueError("Incorrect number of base rows.")
    if len(dispatch_rows) != len(data["scenario_ids"]) * len(data["zone_ids"]):
        raise ValueError("Incorrect number of dispatch rows.")

    base_data = {}
    for row in base_rows:
        bid = row["base_id"]
        if bid not in data["base_by_id"]:
            raise ValueError(f"Unknown base in base row: {bid}")
        if row["scenario_id"] or row["zone_id"]:
            raise ValueError(f"Base row must leave scenario_id and zone_id blank: {bid}")
        if row["backup_base_id"] or row["priority"]:
            raise ValueError(f"Base row must leave backup_base_id and priority blank: {bid}")
        if row["mode"] not in {"open", "closed"}:
            raise ValueError(f"Invalid base mode for {bid}")
        if row["drones"] == "" or row["kits"] == "":
            raise ValueError(f"Base row must include drones and kits for {bid}")
        drones = parse_int(row["drones"])
        kits = parse_int(row["kits"])
        if drones < 0 or kits < 0:
            raise ValueError(f"Negative assets at base {bid}")
        if bid in base_data:
            raise ValueError(f"Duplicate base row for {bid}")
        base_data[bid] = {
            "open": 1 if row["mode"] == "open" else 0,
            "drones": drones,
            "kits": kits,
        }

    if set(base_data) != set(data["base_ids"]):
        raise ValueError("Base rows do not cover all candidate bases.")

    dispatch_data = {}
    for row in dispatch_rows:
        sid = row["scenario_id"]
        zid = row["zone_id"]
        if sid not in data["scenario_by_id"] or zid not in data["zone_by_id"]:
            raise ValueError(f"Unknown dispatch key {(sid, zid)}")
        key = (sid, zid)
        if key in dispatch_data:
            raise ValueError(f"Duplicate dispatch row for {key}")
        if row["mode"] not in {"drone", "ground"}:
            raise ValueError(f"Invalid dispatch mode for {key}")
        if row["drones"] or row["kits"]:
            raise ValueError(f"Dispatch row must leave drones and kits blank for {key}")
        priority = parse_int(row["priority"])
        if priority not in {1, 2, 3}:
            raise ValueError(f"Invalid priority for {key}")
        dispatch_data[key] = row

    if len(dispatch_data) != len(data["scenario_ids"]) * len(data["zone_ids"]):
        raise ValueError("Dispatch rows do not cover all scenario-zone pairs.")

    return rows, base_data, dispatch_data


def validate_solution(data, solution_path):
    _, base_data, dispatch_data = parse_solution_artifact(data, solution_path)

    config = data["config"]
    weekly_cost = 0.0
    open_count = 0
    total_drones = 0
    region_open = {r: 0 for r in data["regions"]}
    region_drones = {r: 0 for r in data["regions"]}

    for bid in data["base_ids"]:
        entry = base_data[bid]
        base = data["base_by_id"][bid]
        if entry["open"]:
            open_count += 1
            weekly_cost += base["fixed_cost"]
        weekly_cost += config["drone_weekly_cost"] * entry["drones"]
        weekly_cost += config["kit_unit_cost"] * entry["kits"]
        total_drones += entry["drones"]
        region_open[base["region"]] += entry["open"]
        region_drones[base["region"]] += entry["drones"]
        if entry["drones"] > base["max_drones"] or entry["kits"] > base["kit_capacity"]:
            raise ValueError(f"Base capacity exceeded at {bid}")
        if entry["open"]:
            if entry["drones"] < 1 or entry["kits"] < 1:
                raise ValueError(f"Open base {bid} must stage at least one drone and kit")
        else:
            if entry["drones"] != 0 or entry["kits"] != 0:
                raise ValueError(f"Closed base {bid} cannot stage assets")

    if not (config["min_open_bases"] <= open_count <= config["max_open_bases"]):
        raise ValueError("Open-base count out of bounds")
    if weekly_cost > config["weekly_budget"] + 1e-6:
        raise ValueError("Budget exceeded")
    if total_drones > config["total_drones_available"]:
        raise ValueError("Fleet limit exceeded")
    for region in data["regions"]:
        if region_open[region] > data["staffing"][region]["max_open_bases"]:
            raise ValueError(f"Regional open-base staffing limit exceeded in {region}")
        if region_drones[region] > data["staffing"][region]["max_drones"]:
            raise ValueError(f"Regional drone staffing limit exceeded in {region}")

    scenario_response = {sid: 0.0 for sid in data["scenario_ids"]}
    scenario_risk = {sid: 0.0 for sid in data["scenario_ids"]}
    scenario_ground = {sid: 0.0 for sid in data["scenario_ids"]}

    for sid in data["scenario_ids"]:
        base_queue = {bid: 0.0 for bid in data["base_ids"]}
        base_kits = {bid: 0.0 for bid in data["base_ids"]}
        corridor_load = {
            (region, district): 0.0
            for region in data["regions"]
            for district in data["districts"]
        }
        ground_load = {district: 0.0 for district in data["districts"]}
        equity_fast = {group: 0.0 for group in data["groups"]}

        for zid in data["zone_ids"]:
            row = dispatch_data[(sid, zid)]
            zone = data["zone_by_id"][zid]
            dem = data["demand"][(sid, zid)]
            fast_credit = 0.0
            if row["mode"] == "ground":
                if row["base_id"] or row["backup_base_id"]:
                    raise ValueError(f"Ground row must leave base fields blank for {(sid, zid)}")
                scenario_response[sid] += dem * data["ground_response"][(sid, zid)]
                scenario_risk[sid] += dem * data["ground_risk"][(sid, zid)]
                scenario_ground[sid] += dem
                ground_load[zone["district"]] += dem
                if data["ground_fast"][(sid, zid)]:
                    fast_credit = dem
            else:
                bid = row["base_id"]
                if bid not in data["base_by_id"]:
                    raise ValueError(f"Unknown base {bid} for {(sid, zid)}")
                if base_data[bid]["open"] != 1:
                    raise ValueError(f"Dispatch uses closed base {bid} for {(sid, zid)}")
                if (sid, zid, bid) not in data["drone_metrics"]:
                    raise ValueError(f"Ineligible drone dispatch {(sid, zid, bid)}")
                scenario_response[sid] += dem * data["drone_metrics"][(sid, zid, bid)]["response"]
                scenario_risk[sid] += dem * data["drone_metrics"][(sid, zid, bid)]["risk"]
                base_queue[bid] += dem * data["drone_metrics"][(sid, zid, bid)]["busy"]
                base_kits[bid] += data["drone_metrics"][(sid, zid, bid)]["kit_need"]
                corridor_load[(data["base_by_id"][bid]["region"], zone["district"])] += dem
                if data["drone_metrics"][(sid, zid, bid)]["fast"]:
                    fast_credit = dem

                if zone["priority_zone"] == 1 and data["scenario_by_id"][sid]["no_fly"] == 0:
                    backup = row["backup_base_id"]
                    if backup not in data["base_by_id"]:
                        raise ValueError(f"Missing or unknown backup base for {(sid, zid)}")
                    if backup == bid:
                        raise ValueError(f"Backup base must differ from primary for {(sid, zid)}")
                    if base_data[backup]["open"] != 1:
                        raise ValueError(f"Backup base {backup} is not open for {(sid, zid)}")
                    valid = backup in data["backup_candidates"].get((sid, zid, bid), [])
                    if not valid:
                        raise ValueError(f"Invalid backup base {backup} for {(sid, zid, bid)}")
                else:
                    if row["backup_base_id"]:
                        raise ValueError(f"Unexpected backup base for {(sid, zid)}")

            equity_fast[zone["equity_group"]] += fast_credit

        service_minutes = config["daily_service_hours"] * 60.0
        for bid in data["base_ids"]:
            if base_queue[bid] > service_minutes * config["max_queue_utilization"] * base_data[bid]["drones"] + 1e-6:
                raise ValueError(f"Queue capacity exceeded at {(sid, bid)}")
            if base_kits[bid] > base_data[bid]["kits"] + 1e-6:
                raise ValueError(f"Kit capacity exceeded at {(sid, bid)}")

        for region in data["regions"]:
            for district in data["districts"]:
                cap = data["corridor"][(sid, region, district)]["max_calls_per_day"]
                if corridor_load[(region, district)] > cap + 1e-6:
                    raise ValueError(f"Corridor capacity exceeded at {(sid, region, district)}")

        for district in data["districts"]:
            if ground_load[district] > data["ground_capacity"][(sid, district)] + 1e-6:
                raise ValueError(f"Ground district capacity exceeded at {(sid, district)}")

        if data["scenario_by_id"][sid]["no_fly"] == 0:
            if (
                scenario_ground[sid]
                > config["max_ground_share_flyable"] * data["total_demand"][sid] + 1e-6
            ):
                raise ValueError(f"Ground share exceeded in scenario {sid}")

        for group in data["groups"]:
            threshold = (
                data["equity"][group]["tail_min_fast_share"]
                if sid in data["tail_scenarios"]
                else data["equity"][group]["min_fast_share"]
            )
            required = threshold * data["group_demand"][(sid, group)]
            if equity_fast[group] + 1e-6 < required:
                raise ValueError(f"Equity fast-share violated for {(sid, group)}")

    expected_response = sum(
        data["scenario_by_id"][sid]["probability"] * scenario_response[sid]
        for sid in data["scenario_ids"]
    )
    tail_response = sum(scenario_response[sid] for sid in data["tail_scenarios"]) / len(
        data["tail_scenarios"]
    )
    expected_risk = sum(
        data["scenario_by_id"][sid]["probability"] * scenario_risk[sid]
        for sid in data["scenario_ids"]
    )
    expected_ground = sum(
        data["scenario_by_id"][sid]["probability"] * scenario_ground[sid]
        for sid in data["scenario_ids"]
    )

    weights = config["weights"]
    score = (
        weights["weekly_cost"] * weekly_cost
        + weights["expected_response_minutes"] * expected_response
        + weights["tail_response_minutes"] * tail_response
        + weights["mortality_risk"] * expected_risk
        + weights["ground_fallback"] * expected_ground
    )

    return {
        "audit_ok": True,
        "violation_count": 0,
        "max_normalized_residual": 0.0,
        "weekly_cost": weekly_cost,
        "expected_response": expected_response,
        "tail_response": tail_response,
        "expected_risk": expected_risk,
        "expected_ground": expected_ground,
        "score": score,
        "scenario_response": scenario_response,
        "scenario_ground": scenario_ground,
    }


def add_incumbent_start(data, vars_dict, model, solution_path):
    if not solution_path.exists():
        return False
    _, base_data, dispatch_data = parse_solution_artifact(data, solution_path)
    sol = model.createOrigSol()

    for bid in data["base_ids"]:
        model.setSolVal(sol, vars_dict["open"][bid], float(base_data[bid]["open"]))
        model.setSolVal(sol, vars_dict["drones"][bid], float(base_data[bid]["drones"]))
        model.setSolVal(sol, vars_dict["kits"][bid], float(base_data[bid]["kits"]))

    for sid in data["scenario_ids"]:
        for zid in data["zone_ids"]:
            row = dispatch_data[(sid, zid)]
            if row["mode"] == "ground":
                model.setSolVal(sol, vars_dict["ground"][(sid, zid)], 1.0)
                for bid in data["primary_candidates"][(sid, zid)]:
                    model.setSolVal(sol, vars_dict["drone"][(sid, zid, bid)], 0.0)
            else:
                chosen = row["base_id"]
                model.setSolVal(sol, vars_dict["ground"][(sid, zid)], 0.0)
                for bid in data["primary_candidates"][(sid, zid)]:
                    model.setSolVal(
                        sol,
                        vars_dict["drone"][(sid, zid, bid)],
                        1.0 if bid == chosen else 0.0,
                    )

    return model.addSol(sol, free=True)


def build_log(attempts):
    lines = ["# Solve Log", ""]
    lines.append("Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`")
    lines.append("")
    for attempt in attempts:
        lines.append(f"## {attempt['name']}")
        lines.append(f"- Status: `{attempt['status']}`")
        if "message" in attempt:
            lines.append(f"- Note: {attempt['message']}")
        if "used_mip_start" in attempt:
            lines.append(f"- Used incumbent MIP start: `{attempt['used_mip_start']}`")
        if "solutions" in attempt:
            lines.append(f"- Solutions: `{attempt['solutions']}`")
        if "objective" in attempt and attempt["objective"] is not None:
            lines.append(f"- Solver objective: `{format_float(attempt['objective'])}`")
        if "dualbound" in attempt and attempt["dualbound"] is not None:
            lines.append(f"- Dual bound: `{format_float(attempt['dualbound'])}`")
        if "gap" in attempt and attempt["gap"] is not None:
            lines.append(f"- Relative gap: `{format_float(attempt['gap'])}`")
        if "elapsed_seconds" in attempt:
            lines.append(f"- Elapsed seconds: `{format_float(attempt['elapsed_seconds'])}`")
        if "audit_ok" in attempt:
            lines.append(f"- Independent audit: `{'pass' if attempt['audit_ok'] else 'fail'}`")
        if "violation_count" in attempt:
            lines.append(f"- Violations: `{attempt['violation_count']}`")
        if attempt.get("max_normalized_residual") is not None:
            lines.append(
                f"- Max normalized residual: `{format_float(attempt['max_normalized_residual'])}`"
            )
        if attempt.get("artifact_score") is not None:
            lines.append(
                f"- Recomputed artifact objective: `{format_float(attempt['artifact_score'])}`"
            )
        if attempt.get("weekly_cost") is not None:
            lines.append(f"- Weekly cost: `{format_float(attempt['weekly_cost'])}`")
        if attempt.get("expected_response") is not None:
            lines.append(f"- Expected response: `{format_float(attempt['expected_response'])}`")
        if attempt.get("tail_response") is not None:
            lines.append(f"- Tail response: `{format_float(attempt['tail_response'])}`")
        if attempt.get("expected_risk") is not None:
            lines.append(
                f"- Expected mortality proxy: `{format_float(attempt['expected_risk'])}`"
            )
        if attempt.get("expected_ground") is not None:
            lines.append(
                f"- Expected ground fallback: `{format_float(attempt['expected_ground'])}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def solve():
    start = time.time()
    data = build_data()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    default_limit = 295.0
    if env_limit is not None:
        time_limit = min(default_limit, float(env_limit))
    else:
        time_limit = default_limit

    attempts = []

    incumbent_audit = None
    if SOLUTION_PATH.exists():
        incumbent_audit = validate_solution(data, SOLUTION_PATH)
        attempts.append(
            {
                "name": "Incumbent audit",
                "status": "feasible",
                "audit_ok": True,
                "violation_count": incumbent_audit["violation_count"],
                "max_normalized_residual": incumbent_audit["max_normalized_residual"],
                "artifact_score": incumbent_audit["score"],
                "weekly_cost": incumbent_audit["weekly_cost"],
                "expected_response": incumbent_audit["expected_response"],
                "tail_response": incumbent_audit["tail_response"],
                "expected_risk": incumbent_audit["expected_risk"],
                "expected_ground": incumbent_audit["expected_ground"],
            }
        )

    model, vars_dict = create_model(data, time_limit)
    used_mip_start = False
    if incumbent_audit is not None:
        used_mip_start = add_incumbent_start(data, vars_dict, model, SOLUTION_PATH)

    model.optimize()
    solve_elapsed = time.time() - start
    status = str(model.getStatus())
    candidate_audit = None
    candidate_summary = {
        "name": "Exact improvement solve",
        "status": status,
        "used_mip_start": used_mip_start,
        "solutions": model.getNSols(),
        "objective": model.getObjVal() if model.getNSols() > 0 else None,
        "dualbound": model.getDualbound(),
        "gap": model.getGap(),
        "elapsed_seconds": solve_elapsed,
    }

    if model.getNSols() > 0 and model.getBestSol() is not None:
        export_solution(data, vars_dict, model, CANDIDATE_PATH)
        candidate_audit = validate_solution(data, CANDIDATE_PATH)
        candidate_summary.update(
            {
                "audit_ok": True,
                "violation_count": candidate_audit["violation_count"],
                "max_normalized_residual": candidate_audit["max_normalized_residual"],
                "artifact_score": candidate_audit["score"],
                "weekly_cost": candidate_audit["weekly_cost"],
                "expected_response": candidate_audit["expected_response"],
                "tail_response": candidate_audit["tail_response"],
                "expected_risk": candidate_audit["expected_risk"],
                "expected_ground": candidate_audit["expected_ground"],
            }
        )
    else:
        candidate_summary.update(
            {
                "audit_ok": False,
                "violation_count": None,
                "max_normalized_residual": None,
                "message": "Solver returned no incumbent candidate beyond the preserved rollback artifact.",
            }
        )
    attempts.append(candidate_summary)

    kept_incumbent = False
    if candidate_audit is not None:
        if incumbent_audit is None or candidate_audit["score"] + 1e-8 < incumbent_audit["score"]:
            CANDIDATE_PATH.replace(SOLUTION_PATH)
        else:
            kept_incumbent = True
            CANDIDATE_PATH.unlink(missing_ok=True)

    final_audit = validate_solution(data, SOLUTION_PATH)
    attempts.append(
        {
            "name": "Published artifact audit",
            "status": "feasible",
            "message": "Incumbent kept" if kept_incumbent else "Candidate published",
            "audit_ok": True,
            "violation_count": final_audit["violation_count"],
            "max_normalized_residual": final_audit["max_normalized_residual"],
            "artifact_score": final_audit["score"],
            "weekly_cost": final_audit["weekly_cost"],
            "expected_response": final_audit["expected_response"],
            "tail_response": final_audit["tail_response"],
            "expected_risk": final_audit["expected_risk"],
            "expected_ground": final_audit["expected_ground"],
        }
    )

    LOG_PATH.write_text(build_log(attempts))

    summary = {
        "incumbent_score": incumbent_audit["score"] if incumbent_audit is not None else None,
        "candidate_score": candidate_audit["score"] if candidate_audit is not None else None,
        "final_score": final_audit["score"],
        "final_status": status,
        "gap": model.getGap(),
        "kept_incumbent": kept_incumbent,
        "published_candidate": not kept_incumbent and candidate_audit is not None,
        "used_mip_start": used_mip_start,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    solve()
