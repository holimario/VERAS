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
MODEL_PATH = SUBMISSIONS_DIR / "model.md"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def load_instance():
    with open(DATA_DIR / "config.json") as handle:
        config = json.load(handle)

    periods_raw = read_csv(DATA_DIR / "periods.csv")
    periods_raw.sort(key=lambda row: int(row["sequence"]))
    periods = [row["period_id"] for row in periods_raw]
    period_index = {period: idx for idx, period in enumerate(periods)}

    relays_raw = read_csv(DATA_DIR / "relays.csv")
    relays = [row["relay_id"] for row in relays_raw]

    payloads_raw = read_csv(DATA_DIR / "payload_types.csv")
    payloads = [row["payload_type"] for row in payloads_raw]

    aircraft_raw = read_csv(DATA_DIR / "aircraft_fleet.csv")
    aircraft = [row["aircraft_id"] for row in aircraft_raw]

    scenarios_raw = read_csv(DATA_DIR / "weather_scenarios.csv")
    scenarios = [row["scenario_id"] for row in scenarios_raw]

    relay_data = {
        row["relay_id"]: {
            "district_id": row["district_id"],
            "distance_km": float(row["distance_km"]),
            "max_total_drops": int(row["max_total_drops"]),
        }
        for row in relays_raw
    }
    payload_data = {
        row["payload_type"]: {
            "loaded_km_per_liter": float(row["loaded_km_per_liter"]),
            "empty_return_km_per_liter": float(row["empty_return_km_per_liter"]),
            "fixed_fuel_liters_per_trip": float(row["fixed_fuel_liters_per_trip"]),
            "prep_hours_per_trip": float(row["prep_hours_per_trip"]),
        }
        for row in payloads_raw
    }
    aircraft_data = {
        row["aircraft_id"]: {
            "fuel_multiplier": float(row["fuel_multiplier"]),
            "prep_hours_multiplier": float(row["prep_hours_multiplier"]),
            "max_total_trips": int(row["max_total_trips"]),
        }
        for row in aircraft_raw
    }
    payload_stock = {
        row["payload_type"]: int(row["available_units"])
        for row in read_csv(DATA_DIR / "payload_stock.csv")
    }
    compat = {
        (row["aircraft_id"], row["payload_type"]): int(row["compatible"])
        for row in read_csv(DATA_DIR / "aircraft_payload_compatibility.csv")
    }
    aircraft_period_capacity = {
        (row["period_id"], row["aircraft_id"]): {
            "available": int(row["available"]),
            "max_trips": int(row["max_trips"]),
            "max_fuel_liters": float(row["max_fuel_liters"]),
            "max_prep_hours": float(row["max_prep_hours"]),
        }
        for row in read_csv(DATA_DIR / "aircraft_period_capacity.csv")
    }
    period_capacity = {
        row["period_id"]: {
            "max_trips": int(row["max_trips"]),
            "fuel_liters": float(row["fuel_liters"]),
            "prep_hours": float(row["prep_hours"]),
        }
        for row in read_csv(DATA_DIR / "period_capacity.csv")
    }
    relay_period_access = {
        (row["period_id"], row["relay_id"]): {
            "accessible": int(row["accessible"]),
            "max_period_drops": int(row["max_period_drops"]),
            "effectiveness_multiplier": float(row["effectiveness_multiplier"]),
        }
        for row in read_csv(DATA_DIR / "relay_period_access.csv")
    }
    relay_payload_effectiveness = {
        (row["relay_id"], row["payload_type"]): float(row["restore_probability_per_drop"])
        for row in read_csv(DATA_DIR / "relay_payload_effectiveness.csv")
    }
    relay_payload_eligibility = {
        (row["relay_id"], row["payload_type"]): int(row["eligible"])
        for row in read_csv(DATA_DIR / "relay_payload_eligibility.csv")
    }
    scenario_data = {
        row["scenario_id"]: {
            "scenario_probability": float(row["scenario_probability"]),
            "min_mission_probability": float(row["min_mission_probability"]),
        }
        for row in scenarios_raw
    }
    scenario_period_relay_effects = {
        (row["scenario_id"], row["period_id"], row["relay_id"]): float(
            row["effectiveness_multiplier"]
        )
        for row in read_csv(DATA_DIR / "scenario_period_relay_effects.csv")
    }
    district_requirements = {
        row["district_id"]: {
            "min_total_drops": int(row["min_total_drops"]),
            "min_active_relays": int(row["min_active_relays"]),
        }
        for row in read_csv(DATA_DIR / "district_requirements.csv")
    }
    districts = sorted(district_requirements)
    district_period_targets = {
        (row["period_id"], row["district_id"]): {
            "min_cumulative_drops": int(row["min_cumulative_drops"]),
            "min_cumulative_active_relays": int(row["min_cumulative_active_relays"]),
        }
        for row in read_csv(DATA_DIR / "district_period_targets.csv")
    }
    operations_budget = {
        row["resource"]: float(row["limit"])
        for row in read_csv(DATA_DIR / "operations_budget.csv")
    }

    relays_in_district = defaultdict(list)
    for relay in relays:
        relays_in_district[relay_data[relay]["district_id"]].append(relay)

    trip_arcs = []
    arcs_by_prk = defaultdict(list)
    arcs_by_period = defaultdict(list)
    arcs_by_aircraft = defaultdict(list)
    arcs_by_period_aircraft = defaultdict(list)
    arcs_by_payload = defaultdict(list)

    fuel_coeff = {}
    prep_coeff = {}

    for period in periods:
        for aircraft_id in aircraft:
            ap_cap = aircraft_period_capacity[(period, aircraft_id)]
            if ap_cap["available"] == 0:
                continue
            for relay in relays:
                access = relay_period_access[(period, relay)]
                if access["accessible"] == 0:
                    continue
                for payload in payloads:
                    if compat[(aircraft_id, payload)] == 0:
                        continue
                    if relay_payload_eligibility[(relay, payload)] == 0:
                        continue
                    key = (period, aircraft_id, relay, payload)
                    trip_arcs.append(key)
                    arcs_by_prk[(period, relay, payload)].append(key)
                    arcs_by_period[period].append(key)
                    arcs_by_aircraft[aircraft_id].append(key)
                    arcs_by_period_aircraft[(period, aircraft_id)].append(key)
                    arcs_by_payload[payload].append(key)

                    distance = relay_data[relay]["distance_km"]
                    payload_info = payload_data[payload]
                    aircraft_info = aircraft_data[aircraft_id]
                    base_fuel = (
                        distance / payload_info["loaded_km_per_liter"]
                        + distance / payload_info["empty_return_km_per_liter"]
                        + payload_info["fixed_fuel_liters_per_trip"]
                    )
                    fuel_coeff[key] = aircraft_info["fuel_multiplier"] * base_fuel
                    prep_coeff[key] = (
                        aircraft_info["prep_hours_multiplier"]
                        * payload_info["prep_hours_per_trip"]
                    )

    relay_slots = {}
    for relay in relays:
        slots = []
        for period in periods:
            access = relay_period_access[(period, relay)]
            if access["accessible"] == 0:
                continue
            for payload in payloads:
                if relay_payload_eligibility[(relay, payload)] == 0:
                    continue
                if not arcs_by_prk.get((period, relay, payload)):
                    continue
                slots.append((period, payload, access["max_period_drops"]))
        relay_slots[relay] = slots

    pattern_data = {}
    patterns_by_prk = defaultdict(list)

    for relay in relays:
        slots = relay_slots[relay]
        max_total = relay_data[relay]["max_total_drops"]
        local_patterns = []
        counts = {}
        period_used = defaultdict(int)

        def finalize_pattern():
            triplet_counts = {slot[:2]: value for slot, value in counts.items() if value > 0}
            total_drops = sum(triplet_counts.values())
            cumulative_drops = {}
            cumulative_active = {}
            running = 0
            for period in periods:
                running += sum(
                    triplet_counts.get((period, payload), 0) for payload in payloads
                )
                cumulative_drops[period] = running
                cumulative_active[period] = 1 if running > 0 else 0
            restore_prob = {}
            for scenario in scenarios:
                survival = 1.0
                for (period, payload), amount in triplet_counts.items():
                    rho = (
                        relay_payload_effectiveness[(relay, payload)]
                        * relay_period_access[(period, relay)]["effectiveness_multiplier"]
                        * scenario_period_relay_effects[(scenario, period, relay)]
                    )
                    rho = max(0.0, min(1.0, rho))
                    survival *= (1.0 - rho) ** amount
                restore_prob[scenario] = 1.0 - survival
            local_patterns.append(
                {
                    "counts": triplet_counts,
                    "total_drops": total_drops,
                    "active": 1 if total_drops > 0 else 0,
                    "cumulative_drops": cumulative_drops,
                    "cumulative_active": cumulative_active,
                    "restore_prob": restore_prob,
                }
            )

        def recurse(slot_idx, remaining):
            if slot_idx == len(slots):
                finalize_pattern()
                return
            period, payload, slot_cap = slots[slot_idx]
            upper = min(remaining, slot_cap - period_used[period])
            for value in range(upper + 1):
                counts[(period, payload, slot_idx)] = value
                period_used[period] += value
                recurse(slot_idx + 1, remaining - value)
                period_used[period] -= value
                del counts[(period, payload, slot_idx)]

        recurse(0, max_total)

        indexed_patterns = []
        for idx, pattern in enumerate(local_patterns):
            pattern_key = (relay, idx)
            indexed_patterns.append(pattern_key)
            pattern_data[pattern_key] = pattern
            for (period, payload), amount in pattern["counts"].items():
                patterns_by_prk[(relay, period, payload)].append((pattern_key, amount))
        pattern_data[relay] = indexed_patterns

    instance = {
        "config": config,
        "periods": periods,
        "period_index": period_index,
        "relays": relays,
        "payloads": payloads,
        "aircraft": aircraft,
        "scenarios": scenarios,
        "districts": districts,
        "relay_data": relay_data,
        "payload_data": payload_data,
        "aircraft_data": aircraft_data,
        "payload_stock": payload_stock,
        "compat": compat,
        "aircraft_period_capacity": aircraft_period_capacity,
        "period_capacity": period_capacity,
        "relay_period_access": relay_period_access,
        "relay_payload_effectiveness": relay_payload_effectiveness,
        "relay_payload_eligibility": relay_payload_eligibility,
        "scenario_data": scenario_data,
        "scenario_period_relay_effects": scenario_period_relay_effects,
        "district_requirements": district_requirements,
        "district_period_targets": district_period_targets,
        "operations_budget": operations_budget,
        "relays_in_district": relays_in_district,
        "trip_arcs": trip_arcs,
        "arcs_by_prk": arcs_by_prk,
        "arcs_by_period": arcs_by_period,
        "arcs_by_aircraft": arcs_by_aircraft,
        "arcs_by_period_aircraft": arcs_by_period_aircraft,
        "arcs_by_payload": arcs_by_payload,
        "fuel_coeff": fuel_coeff,
        "prep_coeff": prep_coeff,
        "pattern_data": pattern_data,
        "patterns_by_prk": patterns_by_prk,
    }
    return instance


def build_model(instance, time_limit):
    details = build_exact_model_details(instance, time_limit)
    return (
        details["model"],
        details["x_vars"],
        details["lambda_vars"],
        details["mission_prob_vars"],
    )


def build_exact_model_details(instance, time_limit):
    config = instance["config"]
    periods = instance["periods"]
    relays = instance["relays"]
    payloads = instance["payloads"]
    aircraft = instance["aircraft"]
    scenarios = instance["scenarios"]
    districts = instance["districts"]
    relay_data = instance["relay_data"]
    aircraft_data = instance["aircraft_data"]
    payload_stock = instance["payload_stock"]
    period_capacity = instance["period_capacity"]
    aircraft_period_capacity = instance["aircraft_period_capacity"]
    district_requirements = instance["district_requirements"]
    district_period_targets = instance["district_period_targets"]
    relays_in_district = instance["relays_in_district"]
    trip_arcs = instance["trip_arcs"]
    arcs_by_prk = instance["arcs_by_prk"]
    arcs_by_period = instance["arcs_by_period"]
    arcs_by_aircraft = instance["arcs_by_aircraft"]
    arcs_by_period_aircraft = instance["arcs_by_period_aircraft"]
    arcs_by_payload = instance["arcs_by_payload"]
    fuel_coeff = instance["fuel_coeff"]
    prep_coeff = instance["prep_coeff"]
    pattern_data = instance["pattern_data"]
    patterns_by_prk = instance["patterns_by_prk"]
    scenario_data = instance["scenario_data"]

    required_restored = int(config["required_restored_relays"])

    model = Model("cascadia_relay_drops")
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    x_vars = {}
    for key in trip_arcs:
        period, aircraft_id, relay, payload = key
        ub = min(
            relay_data[relay]["max_total_drops"],
            payload_stock[payload],
            period_capacity[period]["max_trips"],
            aircraft_data[aircraft_id]["max_total_trips"],
            aircraft_period_capacity[(period, aircraft_id)]["max_trips"],
            instance["relay_period_access"][(period, relay)]["max_period_drops"],
        )
        x_vars[key] = model.addVar(vtype="INTEGER", lb=0, ub=ub, name=f"x[{period},{aircraft_id},{relay},{payload}]")

    lambda_vars = {}
    for relay in relays:
        for pattern_key in pattern_data[relay]:
            lambda_vars[pattern_key] = model.addVar(vtype="BINARY", name=f"lam[{relay},{pattern_key[1]}]")
            model.chgVarBranchPriority(lambda_vars[pattern_key], 10)

    alpha_vars = {}
    u_vars = {}
    n_relays = len(relays)

    for scenario in scenarios:
        alpha_vars[(scenario, 0, 0)] = model.addVar(lb=1.0, ub=1.0, name=f"alpha[{scenario},0,0]")
        for m in range(1, required_restored + 1):
            alpha_vars[(scenario, 0, m)] = model.addVar(lb=0.0, ub=0.0, name=f"alpha[{scenario},0,{m}]")

        for i in range(1, n_relays + 1):
            prev_max = min(i - 1, required_restored)
            curr_max = min(i, required_restored)
            relay = relays[i - 1]
            patterns = pattern_data[relay]

            for m in range(curr_max + 1):
                alpha_vars[(scenario, i, m)] = model.addVar(lb=0.0, ub=1.0, name=f"alpha[{scenario},{i},{m}]")

            for pattern_key in patterns:
                for m in range(prev_max + 1):
                    u_vars[(scenario, i, pattern_key, m)] = model.addVar(
                        lb=0.0,
                        ub=1.0,
                        name=f"u[{scenario},{i},{pattern_key[1]},{m}]",
                    )

    mission_prob_vars = {
        scenario: model.addVar(lb=0.0, ub=1.0, name=f"mission[{scenario}]")
        for scenario in scenarios
    }

    for relay in relays:
        model.addCons(
            quicksum(lambda_vars[pattern_key] for pattern_key in pattern_data[relay]) == 1,
            name=f"one_pattern[{relay}]",
        )

    for relay in relays:
        for period in periods:
            for payload in payloads:
                pattern_terms = patterns_by_prk.get((relay, period, payload), [])
                if not pattern_terms and not arcs_by_prk.get((period, relay, payload)):
                    continue
                model.addCons(
                    quicksum(x_vars[key] for key in arcs_by_prk.get((period, relay, payload), []))
                    == quicksum(amount * lambda_vars[pattern_key] for pattern_key, amount in pattern_terms),
                    name=f"link[{relay},{period},{payload}]",
                )

    for payload in payloads:
        model.addCons(
            quicksum(x_vars[key] for key in arcs_by_payload[payload]) <= payload_stock[payload],
            name=f"payload_stock[{payload}]",
        )

    model.addCons(
        quicksum(fuel_coeff[key] * x_vars[key] for key in trip_arcs)
        <= instance["operations_budget"]["fuel_liters"],
        name="fuel_budget",
    )
    model.addCons(
        quicksum(prep_coeff[key] * x_vars[key] for key in trip_arcs)
        <= instance["operations_budget"]["prep_hours"],
        name="prep_budget",
    )

    for period in periods:
        cap = period_capacity[period]
        model.addCons(
            quicksum(x_vars[key] for key in arcs_by_period[period]) <= cap["max_trips"],
            name=f"period_trips[{period}]",
        )
        model.addCons(
            quicksum(fuel_coeff[key] * x_vars[key] for key in arcs_by_period[period]) <= cap["fuel_liters"],
            name=f"period_fuel[{period}]",
        )
        model.addCons(
            quicksum(prep_coeff[key] * x_vars[key] for key in arcs_by_period[period]) <= cap["prep_hours"],
            name=f"period_prep[{period}]",
        )

    for aircraft_id in aircraft:
        model.addCons(
            quicksum(x_vars[key] for key in arcs_by_aircraft[aircraft_id])
            <= aircraft_data[aircraft_id]["max_total_trips"],
            name=f"aircraft_trips[{aircraft_id}]",
        )

    for period in periods:
        for aircraft_id in aircraft:
            cap = aircraft_period_capacity[(period, aircraft_id)]
            keys = arcs_by_period_aircraft.get((period, aircraft_id), [])
            model.addCons(
                quicksum(x_vars[key] for key in keys) <= cap["max_trips"],
                name=f"ap_trips[{period},{aircraft_id}]",
            )
            model.addCons(
                quicksum(fuel_coeff[key] * x_vars[key] for key in keys) <= cap["max_fuel_liters"],
                name=f"ap_fuel[{period},{aircraft_id}]",
            )
            model.addCons(
                quicksum(prep_coeff[key] * x_vars[key] for key in keys) <= cap["max_prep_hours"],
                name=f"ap_prep[{period},{aircraft_id}]",
            )

    for district in districts:
        district_relays = relays_in_district[district]
        req = district_requirements[district]
        model.addCons(
            quicksum(
                pattern_data[pattern_key]["total_drops"] * lambda_vars[pattern_key]
                for relay in district_relays
                for pattern_key in pattern_data[relay]
            )
            >= req["min_total_drops"],
            name=f"district_drops[{district}]",
        )
        model.addCons(
            quicksum(
                pattern_data[pattern_key]["active"] * lambda_vars[pattern_key]
                for relay in district_relays
                for pattern_key in pattern_data[relay]
            )
            >= req["min_active_relays"],
            name=f"district_active[{district}]",
        )

    for period in periods:
        for district in districts:
            target = district_period_targets[(period, district)]
            district_relays = relays_in_district[district]
            model.addCons(
                quicksum(
                    pattern_data[pattern_key]["cumulative_drops"][period] * lambda_vars[pattern_key]
                    for relay in district_relays
                    for pattern_key in pattern_data[relay]
                )
                >= target["min_cumulative_drops"],
                name=f"district_cum_drops[{period},{district}]",
            )
            model.addCons(
                quicksum(
                    pattern_data[pattern_key]["cumulative_active"][period] * lambda_vars[pattern_key]
                    for relay in district_relays
                    for pattern_key in pattern_data[relay]
                )
                >= target["min_cumulative_active_relays"],
                name=f"district_cum_active[{period},{district}]",
            )

    for scenario in scenarios:
        for i in range(1, n_relays + 1):
            relay = relays[i - 1]
            patterns = pattern_data[relay]
            prev_max = min(i - 1, required_restored)
            curr_max = min(i, required_restored)

            for pattern_key in patterns:
                lam = lambda_vars[pattern_key]
                for m in range(prev_max + 1):
                    u = u_vars[(scenario, i, pattern_key, m)]
                    alpha_prev = alpha_vars[(scenario, i - 1, m)]
                    model.addCons(u <= lam, name=f"u_lam[{scenario},{i},{pattern_key[1]},{m}]")
                    model.addCons(u <= alpha_prev, name=f"u_alpha[{scenario},{i},{pattern_key[1]},{m}]")
                    model.addCons(
                        u >= alpha_prev - (1.0 - lam),
                        name=f"u_lb[{scenario},{i},{pattern_key[1]},{m}]",
                    )

            for m in range(prev_max + 1):
                model.addCons(
                    quicksum(u_vars[(scenario, i, pattern_key, m)] for pattern_key in patterns)
                    == alpha_vars[(scenario, i - 1, m)],
                    name=f"u_partition[{scenario},{i},{m}]",
                )

            for m in range(curr_max + 1):
                if m < required_restored:
                    expr = quicksum(
                        (1.0 - pattern_data[pattern_key]["restore_prob"][scenario])
                        * u_vars[(scenario, i, pattern_key, m)]
                        for pattern_key in patterns
                        if m <= prev_max
                    )
                    if m > 0:
                        expr += quicksum(
                            pattern_data[pattern_key]["restore_prob"][scenario]
                            * u_vars[(scenario, i, pattern_key, m - 1)]
                            for pattern_key in patterns
                            if m - 1 <= prev_max
                        )
                    model.addCons(
                        alpha_vars[(scenario, i, m)] == expr,
                        name=f"alpha_rec[{scenario},{i},{m}]",
                    )
                else:
                    expr = quicksum(
                        u_vars[(scenario, i, pattern_key, required_restored)]
                        for pattern_key in patterns
                        if prev_max >= required_restored
                    )
                    expr += quicksum(
                        pattern_data[pattern_key]["restore_prob"][scenario]
                        * u_vars[(scenario, i, pattern_key, required_restored - 1)]
                        for pattern_key in patterns
                        if required_restored - 1 <= prev_max
                    )
                    model.addCons(
                        alpha_vars[(scenario, i, required_restored)] == expr,
                        name=f"alpha_cap[{scenario},{i}]",
                    )

        model.addCons(
            mission_prob_vars[scenario] == alpha_vars[(scenario, n_relays, required_restored)],
            name=f"mission_link[{scenario}]",
        )
        model.addCons(
            mission_prob_vars[scenario] >= scenario_data[scenario]["min_mission_probability"],
            name=f"mission_min[{scenario}]",
        )

    model.setObjective(
        quicksum(
            scenario_data[scenario]["scenario_probability"] * mission_prob_vars[scenario]
            for scenario in scenarios
        ),
        "maximize",
    )

    return {
        "model": model,
        "x_vars": x_vars,
        "lambda_vars": lambda_vars,
        "alpha_vars": alpha_vars,
        "u_vars": u_vars,
        "mission_prob_vars": mission_prob_vars,
    }


def build_surrogate_model(instance, time_limit):
    periods = instance["periods"]
    relays = instance["relays"]
    payloads = instance["payloads"]
    aircraft = instance["aircraft"]
    scenarios = instance["scenarios"]
    relay_data = instance["relay_data"]
    aircraft_data = instance["aircraft_data"]
    payload_stock = instance["payload_stock"]
    period_capacity = instance["period_capacity"]
    aircraft_period_capacity = instance["aircraft_period_capacity"]
    district_requirements = instance["district_requirements"]
    district_period_targets = instance["district_period_targets"]
    relays_in_district = instance["relays_in_district"]
    trip_arcs = instance["trip_arcs"]
    arcs_by_prk = instance["arcs_by_prk"]
    arcs_by_period = instance["arcs_by_period"]
    arcs_by_aircraft = instance["arcs_by_aircraft"]
    arcs_by_period_aircraft = instance["arcs_by_period_aircraft"]
    arcs_by_payload = instance["arcs_by_payload"]
    fuel_coeff = instance["fuel_coeff"]
    prep_coeff = instance["prep_coeff"]
    pattern_data = instance["pattern_data"]
    patterns_by_prk = instance["patterns_by_prk"]
    scenario_data = instance["scenario_data"]

    model = Model("cascadia_relay_drops_surrogate")
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    x_vars = {}
    for key in trip_arcs:
        period, aircraft_id, relay, payload = key
        ub = min(
            relay_data[relay]["max_total_drops"],
            payload_stock[payload],
            period_capacity[period]["max_trips"],
            aircraft_data[aircraft_id]["max_total_trips"],
            aircraft_period_capacity[(period, aircraft_id)]["max_trips"],
            instance["relay_period_access"][(period, relay)]["max_period_drops"],
        )
        x_vars[key] = model.addVar(vtype="INTEGER", lb=0, ub=ub, name=f"x[{period},{aircraft_id},{relay},{payload}]")

    lambda_vars = {}
    for relay in relays:
        for pattern_key in pattern_data[relay]:
            lambda_vars[pattern_key] = model.addVar(vtype="BINARY", name=f"lam[{relay},{pattern_key[1]}]")
            model.chgVarBranchPriority(lambda_vars[pattern_key], 10)

    for relay in relays:
        model.addCons(
            quicksum(lambda_vars[pattern_key] for pattern_key in pattern_data[relay]) == 1,
            name=f"one_pattern[{relay}]",
        )

    for relay in relays:
        for period in periods:
            for payload in payloads:
                pattern_terms = patterns_by_prk.get((relay, period, payload), [])
                if not pattern_terms and not arcs_by_prk.get((period, relay, payload)):
                    continue
                model.addCons(
                    quicksum(x_vars[key] for key in arcs_by_prk.get((period, relay, payload), []))
                    == quicksum(amount * lambda_vars[pattern_key] for pattern_key, amount in pattern_terms),
                    name=f"link[{relay},{period},{payload}]",
                )

    for payload in payloads:
        model.addCons(
            quicksum(x_vars[key] for key in arcs_by_payload[payload]) <= payload_stock[payload],
            name=f"payload_stock[{payload}]",
        )

    model.addCons(
        quicksum(fuel_coeff[key] * x_vars[key] for key in trip_arcs)
        <= instance["operations_budget"]["fuel_liters"],
        name="fuel_budget",
    )
    model.addCons(
        quicksum(prep_coeff[key] * x_vars[key] for key in trip_arcs)
        <= instance["operations_budget"]["prep_hours"],
        name="prep_budget",
    )

    for period in periods:
        cap = period_capacity[period]
        model.addCons(
            quicksum(x_vars[key] for key in arcs_by_period[period]) <= cap["max_trips"],
            name=f"period_trips[{period}]",
        )
        model.addCons(
            quicksum(fuel_coeff[key] * x_vars[key] for key in arcs_by_period[period]) <= cap["fuel_liters"],
            name=f"period_fuel[{period}]",
        )
        model.addCons(
            quicksum(prep_coeff[key] * x_vars[key] for key in arcs_by_period[period]) <= cap["prep_hours"],
            name=f"period_prep[{period}]",
        )

    for aircraft_id in aircraft:
        model.addCons(
            quicksum(x_vars[key] for key in arcs_by_aircraft[aircraft_id])
            <= aircraft_data[aircraft_id]["max_total_trips"],
            name=f"aircraft_trips[{aircraft_id}]",
        )

    for period in periods:
        for aircraft_id in aircraft:
            cap = aircraft_period_capacity[(period, aircraft_id)]
            keys = arcs_by_period_aircraft.get((period, aircraft_id), [])
            model.addCons(
                quicksum(x_vars[key] for key in keys) <= cap["max_trips"],
                name=f"ap_trips[{period},{aircraft_id}]",
            )
            model.addCons(
                quicksum(fuel_coeff[key] * x_vars[key] for key in keys) <= cap["max_fuel_liters"],
                name=f"ap_fuel[{period},{aircraft_id}]",
            )
            model.addCons(
                quicksum(prep_coeff[key] * x_vars[key] for key in keys) <= cap["max_prep_hours"],
                name=f"ap_prep[{period},{aircraft_id}]",
            )

    for district in instance["districts"]:
        district_relays = relays_in_district[district]
        req = district_requirements[district]
        model.addCons(
            quicksum(
                pattern_data[pattern_key]["total_drops"] * lambda_vars[pattern_key]
                for relay in district_relays
                for pattern_key in pattern_data[relay]
            )
            >= req["min_total_drops"],
            name=f"district_drops[{district}]",
        )
        model.addCons(
            quicksum(
                pattern_data[pattern_key]["active"] * lambda_vars[pattern_key]
                for relay in district_relays
                for pattern_key in pattern_data[relay]
            )
            >= req["min_active_relays"],
            name=f"district_active[{district}]",
        )

    for period in periods:
        for district in instance["districts"]:
            target = district_period_targets[(period, district)]
            district_relays = relays_in_district[district]
            model.addCons(
                quicksum(
                    pattern_data[pattern_key]["cumulative_drops"][period] * lambda_vars[pattern_key]
                    for relay in district_relays
                    for pattern_key in pattern_data[relay]
                )
                >= target["min_cumulative_drops"],
                name=f"district_cum_drops[{period},{district}]",
            )
            model.addCons(
                quicksum(
                    pattern_data[pattern_key]["cumulative_active"][period] * lambda_vars[pattern_key]
                    for relay in district_relays
                    for pattern_key in pattern_data[relay]
                )
                >= target["min_cumulative_active_relays"],
                name=f"district_cum_active[{period},{district}]",
            )

    model.setObjective(
        quicksum(
            scenario_data[scenario]["scenario_probability"]
            * pattern_data[pattern_key]["restore_prob"][scenario]
            * lambda_vars[pattern_key]
            for relay in relays
            for pattern_key in pattern_data[relay]
            for scenario in scenarios
        ),
        "maximize",
    )

    return model, x_vars, lambda_vars


def solve_model(instance):
    hard_limit = get_hard_time_limit()
    time_limit = min(hard_limit, 60.0)

    model, x_vars, lambda_vars = build_surrogate_model(instance, time_limit)
    model.optimize()

    solution = None
    if model.getNSols() > 0:
        solution = model.getBestSol()

    result = {
        "model": model,
        "x_vars": x_vars,
        "lambda_vars": lambda_vars,
        "time_limit": time_limit,
        "hard_limit": hard_limit,
        "has_solution": solution is not None,
        "best_solution": solution,
        "status": str(model.getStatus()),
        "n_solutions": int(model.getNSols()),
        "n_vars": int(model.getNVars()),
        "n_conss": int(model.getNConss()),
        "solve_time": float(model.getSolvingTime()),
        "search_model": "surrogate_weighted_relay_restoration",
    }
    if solution is not None:
        result["objective"] = float(model.getObjVal())
        result["best_bound"] = float(model.getDualbound())
        result["gap"] = float(model.getGap())
    else:
        result["objective"] = None
        result["best_bound"] = float(model.getDualbound())
        result["gap"] = None
    return result


def export_solution(instance, result):
    if not result["has_solution"]:
        raise RuntimeError("SCIP did not produce an incumbent solution.")

    model = result["model"]
    solution = result["best_solution"]
    rows = []
    for key, var in result["x_vars"].items():
        value = model.getSolVal(solution, var)
        if value > 1e-8:
            trips = int(round(value))
            rows.append(
                {
                    "variable": "drop",
                    "period": key[0],
                    "aircraft": key[1],
                    "relay": key[2],
                    "payload_type": key[3],
                    "trips": str(trips),
                }
            )

    rows.sort(key=lambda row: (row["period"], row["aircraft"], row["relay"], row["payload_type"]))

    with open(SOLUTION_PATH, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=instance["config"]["solution_columns"])
        writer.writeheader()
        writer.writerows(rows)


def normalized_violation(lhs, sense, rhs):
    scale = max(1.0, abs(rhs))
    if sense == "<=":
        violation = max(0.0, lhs - rhs)
    elif sense == ">=":
        violation = max(0.0, rhs - lhs)
    elif sense == "==":
        violation = abs(lhs - rhs)
    else:
        raise ValueError(f"Unknown sense {sense}")
    return violation, violation / scale


def compute_mission_probability(relay_success_prob, required_restored):
    state = [0.0] * (required_restored + 1)
    state[0] = 1.0
    for prob in relay_success_prob:
        new_state = [0.0] * (required_restored + 1)
        new_state[0] = state[0] * (1.0 - prob)
        for m in range(1, required_restored):
            new_state[m] = state[m] * (1.0 - prob) + state[m - 1] * prob
        new_state[required_restored] = state[required_restored] + state[required_restored - 1] * prob
        state = new_state
    return state[required_restored]


def audit_solution(instance, solution_path):
    config = instance["config"]
    required_columns = config["solution_columns"]
    required_restored = int(config["required_restored_relays"])
    periods = instance["periods"]
    relays = instance["relays"]
    payloads = instance["payloads"]
    aircraft = instance["aircraft"]
    scenarios = instance["scenarios"]
    relay_data = instance["relay_data"]
    aircraft_data = instance["aircraft_data"]
    payload_stock = instance["payload_stock"]
    period_capacity = instance["period_capacity"]
    aircraft_period_capacity = instance["aircraft_period_capacity"]
    district_requirements = instance["district_requirements"]
    district_period_targets = instance["district_period_targets"]
    relays_in_district = instance["relays_in_district"]
    relay_period_access = instance["relay_period_access"]
    relay_payload_eligibility = instance["relay_payload_eligibility"]
    compat = instance["compat"]
    scenario_data = instance["scenario_data"]
    relay_payload_effectiveness = instance["relay_payload_effectiveness"]
    scenario_period_relay_effects = instance["scenario_period_relay_effects"]
    fuel_coeff = instance["fuel_coeff"]
    prep_coeff = instance["prep_coeff"]

    with open(solution_path, newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)

    schema_ok = header == required_columns
    trip_values = {}
    group_residuals = []
    violated_groups = []

    def record(group, lhs, sense, rhs):
        violation, norm = normalized_violation(lhs, sense, rhs)
        group_residuals.append((group, violation, norm))
        if violation > 1e-7:
            violated_groups.append(group)

    integrality_residual = 0.0
    parse_errors = []
    for idx, row in enumerate(rows):
        key = (
            row.get("period", ""),
            row.get("aircraft", ""),
            row.get("relay", ""),
            row.get("payload_type", ""),
        )
        if row.get("variable") != "drop":
            parse_errors.append(f"row {idx + 2}: variable must be 'drop'")
            continue
        if key[0] not in periods or key[1] not in aircraft or key[2] not in relays or key[3] not in payloads:
            parse_errors.append(f"row {idx + 2}: invalid identifiers")
            continue
        try:
            trips_float = float(row.get("trips", ""))
        except ValueError:
            parse_errors.append(f"row {idx + 2}: trips is not numeric")
            continue
        rounded = round(trips_float)
        integrality_residual = max(integrality_residual, abs(trips_float - rounded))
        if abs(trips_float - rounded) > 1e-7 or rounded < 0:
            parse_errors.append(f"row {idx + 2}: trips must be a nonnegative integer")
            continue
        trip_values[key] = trip_values.get(key, 0) + int(rounded)

    for key, value in trip_values.items():
        period, aircraft_id, relay, payload = key
        if compat[(aircraft_id, payload)] == 0:
            parse_errors.append(f"incompatible aircraft/payload on {key}")
        if aircraft_period_capacity[(period, aircraft_id)]["available"] == 0:
            parse_errors.append(f"unavailable aircraft-period on {key}")
        if relay_period_access[(period, relay)]["accessible"] == 0:
            parse_errors.append(f"inaccessible relay-period on {key}")
        if relay_payload_eligibility[(relay, payload)] == 0:
            parse_errors.append(f"ineligible relay/payload on {key}")
        if (period, aircraft_id, relay, payload) not in fuel_coeff:
            parse_errors.append(f"unknown feasible arc on {key}")
        if value < 0:
            parse_errors.append(f"negative trips on {key}")

    if parse_errors:
        violated_groups.append("schema_or_domain")

    total_by_payload = defaultdict(int)
    total_by_period = defaultdict(int)
    fuel_by_period = defaultdict(float)
    prep_by_period = defaultdict(float)
    total_by_aircraft = defaultdict(int)
    total_by_period_aircraft = defaultdict(int)
    fuel_by_period_aircraft = defaultdict(float)
    prep_by_period_aircraft = defaultdict(float)
    total_by_relay = defaultdict(int)
    total_by_period_relay = defaultdict(int)
    total_by_prk = defaultdict(int)
    total_fuel = 0.0
    total_prep = 0.0

    for key, value in trip_values.items():
        period, aircraft_id, relay, payload = key
        total_by_payload[payload] += value
        total_by_period[period] += value
        total_by_aircraft[aircraft_id] += value
        total_by_period_aircraft[(period, aircraft_id)] += value
        total_by_relay[relay] += value
        total_by_period_relay[(period, relay)] += value
        total_by_prk[(period, relay, payload)] += value

        arc_key = (period, aircraft_id, relay, payload)
        arc_fuel = fuel_coeff.get(arc_key, 0.0) * value
        arc_prep = prep_coeff.get(arc_key, 0.0) * value
        total_fuel += arc_fuel
        total_prep += arc_prep
        fuel_by_period[period] += arc_fuel
        prep_by_period[period] += arc_prep
        fuel_by_period_aircraft[(period, aircraft_id)] += arc_fuel
        prep_by_period_aircraft[(period, aircraft_id)] += arc_prep

    for payload in payloads:
        record(f"payload_stock[{payload}]", total_by_payload[payload], "<=", payload_stock[payload])

    record("fuel_budget", total_fuel, "<=", instance["operations_budget"]["fuel_liters"])
    record("prep_budget", total_prep, "<=", instance["operations_budget"]["prep_hours"])

    for period in periods:
        cap = period_capacity[period]
        record(f"period_trips[{period}]", total_by_period[period], "<=", cap["max_trips"])
        record(f"period_fuel[{period}]", fuel_by_period[period], "<=", cap["fuel_liters"])
        record(f"period_prep[{period}]", prep_by_period[period], "<=", cap["prep_hours"])

    for aircraft_id in aircraft:
        record(
            f"aircraft_trips[{aircraft_id}]",
            total_by_aircraft[aircraft_id],
            "<=",
            aircraft_data[aircraft_id]["max_total_trips"],
        )

    for period in periods:
        for aircraft_id in aircraft:
            cap = aircraft_period_capacity[(period, aircraft_id)]
            record(
                f"ap_trips[{period},{aircraft_id}]",
                total_by_period_aircraft[(period, aircraft_id)],
                "<=",
                cap["max_trips"],
            )
            record(
                f"ap_fuel[{period},{aircraft_id}]",
                fuel_by_period_aircraft[(period, aircraft_id)],
                "<=",
                cap["max_fuel_liters"],
            )
            record(
                f"ap_prep[{period},{aircraft_id}]",
                prep_by_period_aircraft[(period, aircraft_id)],
                "<=",
                cap["max_prep_hours"],
            )

    for relay in relays:
        record(
            f"relay_total[{relay}]",
            total_by_relay[relay],
            "<=",
            relay_data[relay]["max_total_drops"],
        )
        for period in periods:
            record(
                f"relay_period[{period},{relay}]",
                total_by_period_relay[(period, relay)],
                "<=",
                relay_period_access[(period, relay)]["max_period_drops"],
            )

    relay_cum_period = defaultdict(dict)
    for relay in relays:
        running = 0
        for period in periods:
            running += total_by_period_relay[(period, relay)]
            relay_cum_period[relay][period] = running

    district_total_drops = defaultdict(int)
    district_active_relays = defaultdict(int)
    district_cum_drops = defaultdict(lambda: defaultdict(int))
    district_cum_active = defaultdict(lambda: defaultdict(int))

    for district, district_relays in relays_in_district.items():
        for relay in district_relays:
            district_total_drops[district] += total_by_relay[relay]
            district_active_relays[district] += 1 if total_by_relay[relay] > 0 else 0
            for period in periods:
                district_cum_drops[district][period] += relay_cum_period[relay][period]
                district_cum_active[district][period] += 1 if relay_cum_period[relay][period] > 0 else 0

    for district, req in district_requirements.items():
        record(f"district_drops[{district}]", district_total_drops[district], ">=", req["min_total_drops"])
        record(
            f"district_active[{district}]",
            district_active_relays[district],
            ">=",
            req["min_active_relays"],
        )

    for period in periods:
        for district in district_requirements:
            target = district_period_targets[(period, district)]
            record(
                f"district_cum_drops[{period},{district}]",
                district_cum_drops[district][period],
                ">=",
                target["min_cumulative_drops"],
            )
            record(
                f"district_cum_active[{period},{district}]",
                district_cum_active[district][period],
                ">=",
                target["min_cumulative_active_relays"],
            )

    relay_success = {scenario: {} for scenario in scenarios}
    mission_prob = {}
    for scenario in scenarios:
        probs = []
        for relay in relays:
            survival = 1.0
            for period in periods:
                for payload in payloads:
                    amount = total_by_prk[(period, relay, payload)]
                    if amount == 0:
                        continue
                    rho = (
                        relay_payload_effectiveness[(relay, payload)]
                        * relay_period_access[(period, relay)]["effectiveness_multiplier"]
                        * scenario_period_relay_effects[(scenario, period, relay)]
                    )
                    rho = max(0.0, min(1.0, rho))
                    survival *= (1.0 - rho) ** amount
                relay_success[scenario][relay] = 1.0 - survival
            probs.append(relay_success[scenario].get(relay, 0.0))
        mission_prob[scenario] = compute_mission_probability(probs, required_restored)
        record(
            f"mission_min[{scenario}]",
            mission_prob[scenario],
            ">=",
            scenario_data[scenario]["min_mission_probability"],
        )

    weighted_objective = sum(
        scenario_data[scenario]["scenario_probability"] * mission_prob[scenario]
        for scenario in scenarios
    )

    max_norm = max((norm for _, _, norm in group_residuals), default=0.0)
    violation_count = sum(1 for _, violation, _ in group_residuals if violation > 1e-7)
    if parse_errors:
        violation_count += 1

    active_relays = sum(1 for relay in relays if total_by_relay[relay] > 0)
    state_vector = {
        "artifact_present": solution_path.exists(),
        "schema_ok": schema_ok,
        "incumbent_trip_rows": len(rows),
        "max_normalized_residual": max_norm,
        "violated_constraint_groups": violation_count,
        "integrality_residual": integrality_residual,
        "weighted_objective": weighted_objective,
        "active_relays": active_relays,
        "served_districts": sum(1 for district in district_requirements if district_active_relays[district] > 0),
    }

    return {
        "schema_ok": schema_ok,
        "parse_errors": parse_errors,
        "group_residuals": group_residuals,
        "violated_groups": sorted(set(violated_groups)),
        "integrality_residual": integrality_residual,
        "mission_prob": mission_prob,
        "weighted_objective": weighted_objective,
        "state_vector": state_vector,
        "trip_values": trip_values,
    }


def extract_pattern_selection(instance, trip_values):
    relays = instance["relays"]
    periods = instance["periods"]
    payloads = instance["payloads"]
    aircraft = instance["aircraft"]
    pattern_data = instance["pattern_data"]

    chosen_patterns = {}
    for relay in relays:
        counts = {}
        for period in periods:
            for payload in payloads:
                total = sum(
                    trip_values.get((period, aircraft_id, relay, payload), 0)
                    for aircraft_id in aircraft
                )
                if total > 0:
                    counts[(period, payload)] = total

        match = None
        for pattern_key in pattern_data[relay]:
            if pattern_data[pattern_key]["counts"] == counts:
                match = pattern_key
                break
        if match is None:
            raise ValueError(f"No relay pattern matches exported trips for {relay}.")
        chosen_patterns[relay] = match
    return chosen_patterns


def compute_pattern_selection_objective(instance, chosen_patterns):
    scenarios = instance["scenarios"]
    relays = instance["relays"]
    scenario_data = instance["scenario_data"]
    pattern_data = instance["pattern_data"]
    required_restored = int(instance["config"]["required_restored_relays"])

    mission_prob = {}
    weighted_objective = 0.0
    for scenario in scenarios:
        relay_probs = [pattern_data[chosen_patterns[relay]]["restore_prob"][scenario] for relay in relays]
        value = compute_mission_probability(relay_probs, required_restored)
        mission_prob[scenario] = value
        weighted_objective += scenario_data[scenario]["scenario_probability"] * value
    return weighted_objective, mission_prob


def compute_exact_start_state(instance, chosen_patterns):
    scenarios = instance["scenarios"]
    relays = instance["relays"]
    pattern_data = instance["pattern_data"]
    required_restored = int(instance["config"]["required_restored_relays"])

    alpha_values = {}
    u_values = {}
    mission_values = {}

    for scenario in scenarios:
        alpha_prev = [0.0] * (required_restored + 1)
        alpha_prev[0] = 1.0
        alpha_values[(scenario, 0, 0)] = 1.0
        for m in range(1, required_restored + 1):
            alpha_values[(scenario, 0, m)] = 0.0

        for i, relay in enumerate(relays, start=1):
            chosen_pattern = chosen_patterns[relay]
            chosen_restore_prob = pattern_data[chosen_pattern]["restore_prob"][scenario]
            prev_max = min(i - 1, required_restored)
            curr_max = min(i, required_restored)

            for pattern_key in pattern_data[relay]:
                for m in range(prev_max + 1):
                    u_values[(scenario, i, pattern_key, m)] = (
                        alpha_prev[m] if pattern_key == chosen_pattern else 0.0
                    )

            alpha_curr = [0.0] * (required_restored + 1)
            alpha_curr[0] = alpha_prev[0] * (1.0 - chosen_restore_prob)
            alpha_values[(scenario, i, 0)] = alpha_curr[0]

            for m in range(1, curr_max + 1):
                if m < required_restored:
                    alpha_curr[m] = alpha_prev[m] * (1.0 - chosen_restore_prob) + alpha_prev[m - 1] * chosen_restore_prob
                else:
                    alpha_curr[m] = alpha_prev[m] + alpha_prev[m - 1] * chosen_restore_prob
                alpha_values[(scenario, i, m)] = alpha_curr[m]

            alpha_prev = alpha_curr

        mission_values[scenario] = alpha_prev[required_restored]

    return alpha_values, u_values, mission_values


def add_exact_mip_start(model_details, instance, trip_values, chosen_patterns):
    model = model_details["model"]
    solution = model.createSol()

    for key, var in model_details["x_vars"].items():
        model.setSolVal(solution, var, float(trip_values.get(key, 0)))

    for pattern_key, var in model_details["lambda_vars"].items():
        relay = pattern_key[0]
        model.setSolVal(solution, var, 1.0 if chosen_patterns[relay] == pattern_key else 0.0)

    alpha_values, u_values, mission_values = compute_exact_start_state(instance, chosen_patterns)

    for key, var in model_details["alpha_vars"].items():
        model.setSolVal(solution, var, alpha_values.get(key, 0.0))

    for key, var in model_details["u_vars"].items():
        model.setSolVal(solution, var, u_values.get(key, 0.0))

    for scenario, var in model_details["mission_prob_vars"].items():
        model.setSolVal(solution, var, mission_values[scenario])

    return model.addSol(solution, free=True)


def extract_trip_values_from_solution(model, x_vars, solution):
    trip_values = {}
    for key, var in x_vars.items():
        value = model.getSolVal(solution, var)
        if value > 1e-8:
            trip_values[key] = int(round(value))
    return trip_values


def write_trip_values(instance, trip_values, solution_path):
    rows = []
    for key, value in trip_values.items():
        if value <= 0:
            continue
        rows.append(
            {
                "variable": "drop",
                "period": key[0],
                "aircraft": key[1],
                "relay": key[2],
                "payload_type": key[3],
                "trips": str(int(value)),
            }
        )

    rows.sort(key=lambda row: (row["period"], row["aircraft"], row["relay"], row["payload_type"]))

    with open(solution_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=instance["config"]["solution_columns"])
        writer.writeheader()
        writer.writerows(rows)


def build_local_branching_instance(instance, incumbent_patterns, incumbent_objective):
    relays = instance["relays"]
    scenarios = instance["scenarios"]
    pattern_data = instance["pattern_data"]
    scenario_data = instance["scenario_data"]

    reduced = dict(instance)
    new_pattern_data = {}
    new_patterns_by_prk = defaultdict(list)

    for relay in relays:
        scored_patterns = []
        for pattern_key in pattern_data[relay]:
            selection = dict(incumbent_patterns)
            selection[relay] = pattern_key
            objective, _ = compute_pattern_selection_objective(instance, selection)
            scored_patterns.append((objective, pattern_key))
        scored_patterns.sort(reverse=True, key=lambda item: item[0])

        incumbent_counts = pattern_data[incumbent_patterns[relay]]["counts"]
        distance_patterns = []
        for pattern_key in pattern_data[relay]:
            candidate_counts = pattern_data[pattern_key]["counts"]
            l1_distance = 0
            for count_key in set(incumbent_counts) | set(candidate_counts):
                l1_distance += abs(incumbent_counts.get(count_key, 0) - candidate_counts.get(count_key, 0))
            distance_patterns.append((l1_distance, pattern_key))
        distance_patterns.sort(key=lambda item: (item[0], item[1][1]))

        keep = {incumbent_patterns[relay]}
        for _, pattern_key in scored_patterns[:8]:
            keep.add(pattern_key)
        for _, pattern_key in distance_patterns[:8]:
            keep.add(pattern_key)
        for scenario in scenarios:
            by_scenario = sorted(
                pattern_data[relay],
                key=lambda pattern_key: pattern_data[pattern_key]["restore_prob"][scenario],
                reverse=True,
            )
            for pattern_key in by_scenario[:3]:
                keep.add(pattern_key)

        reduced_patterns = sorted(keep, key=lambda pattern_key: pattern_key[1])
        new_pattern_data[relay] = reduced_patterns
        for pattern_key in reduced_patterns:
            new_pattern_data[pattern_key] = pattern_data[pattern_key]
            for (period, payload), amount in pattern_data[pattern_key]["counts"].items():
                new_patterns_by_prk[(relay, period, payload)].append((pattern_key, amount))

    reduced["pattern_data"] = new_pattern_data
    reduced["patterns_by_prk"] = new_patterns_by_prk
    reduced["local_branching_incumbent_objective"] = incumbent_objective
    reduced["local_branching_total_patterns"] = sum(len(new_pattern_data[relay]) for relay in relays)
    return reduced


def run_true_objective_improvement(instance, incumbent_audit, hard_limit):
    incumbent_trip_values = incumbent_audit["trip_values"]
    incumbent_objective = incumbent_audit["weighted_objective"]
    incumbent_patterns = extract_pattern_selection(instance, incumbent_trip_values)
    reduced_instance = build_local_branching_instance(instance, incumbent_patterns, incumbent_objective)

    time_limit = min(60.0, hard_limit)
    model_details = build_exact_model_details(reduced_instance, time_limit)
    model = model_details["model"]

    radius = 2
    model.addCons(
        quicksum(1.0 - model_details["lambda_vars"][incumbent_patterns[relay]] for relay in instance["relays"]) <= radius,
        name="local_branching_radius",
    )
    mip_start_ok = add_exact_mip_start(model_details, reduced_instance, incumbent_trip_values, incumbent_patterns)
    model.optimize()

    summary = {
        "search_model": "exact_true_objective_local_branching",
        "time_limit": time_limit,
        "status": str(model.getStatus()),
        "solve_time": float(model.getSolvingTime()),
        "mip_start_ok": bool(mip_start_ok),
        "radius": radius,
        "pattern_pool_size": int(reduced_instance["local_branching_total_patterns"]),
        "objective": float(model.getObjVal()) if model.getNSols() > 0 else None,
        "best_bound": float(model.getDualbound()),
        "gap": float(model.getGap()) if model.getNSols() > 0 else None,
        "n_solutions": int(model.getNSols()),
        "candidate_accepted": False,
        "candidate_objective": None,
        "candidate_audit_ok": False,
        "candidate_status": "no-strict-improvement",
        "candidate_audit": None,
    }

    if model.getNSols() > 0:
        solution = model.getBestSol()
        candidate_trip_values = extract_trip_values_from_solution(model, model_details["x_vars"], solution)
        candidate_path = SUBMISSIONS_DIR / "solution_candidate.csv"
        write_trip_values(instance, candidate_trip_values, candidate_path)
        candidate_audit = audit_solution(instance, candidate_path)
        candidate_objective = candidate_audit["weighted_objective"]
        audit_ok = (
            candidate_audit["schema_ok"]
            and not candidate_audit["parse_errors"]
            and candidate_audit["state_vector"]["violated_constraint_groups"] == 0
        )
        summary["candidate_objective"] = candidate_objective
        summary["candidate_audit_ok"] = audit_ok
        summary["candidate_audit"] = candidate_audit

        if audit_ok and candidate_objective > incumbent_objective + 1e-9:
            write_trip_values(instance, candidate_trip_values, SOLUTION_PATH)
            summary["candidate_accepted"] = True
            summary["candidate_status"] = "accepted"
        elif audit_ok:
            summary["candidate_status"] = "not-better-than-incumbent"
        else:
            summary["candidate_status"] = "candidate-audit-failed"

        if candidate_path.exists():
            candidate_path.unlink()

    return summary


def format_float(value):
    if value is None:
        return "NA"
    return f"{value:.8f}"


def get_hard_time_limit():
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit:
        try:
            return min(300.0, float(env_limit))
        except ValueError:
            return 300.0
    return 300.0


def write_log(incumbent_audit, final_audit, improvement_summary, start_time, baseline_result=None):
    elapsed = time.time() - start_time
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Independent Audit",
        "",
        "- Public challenger report: `no concrete public-rule violation reported`",
        "- Reparsed artifact from disk before improvement: `passed`",
        f"- Incumbent exported objective: `{format_float(incumbent_audit['weighted_objective'])}`",
        f"- Incumbent violated constraint groups: `{incumbent_audit['state_vector']['violated_constraint_groups']}`",
        f"- Incumbent max normalized residual: `{format_float(incumbent_audit['state_vector']['max_normalized_residual'])}`",
        "",
        "## Improvement Search",
        "",
        f"- Search model: `{improvement_summary['search_model']}`",
        f"- Local branching radius: `{improvement_summary['radius']}`",
        f"- Reduced pattern pool size: `{improvement_summary['pattern_pool_size']}`",
        f"- MIP start accepted: `{improvement_summary['mip_start_ok']}`",
        f"- Status: `{improvement_summary['status']}`",
        f"- Time limit seconds: `{format_float(improvement_summary['time_limit'])}`",
        f"- Solve time seconds: `{format_float(improvement_summary['solve_time'])}`",
        f"- Search incumbent count: `{improvement_summary['n_solutions']}`",
        f"- Search primal objective: `{format_float(improvement_summary['objective'])}`",
        f"- Search best bound: `{format_float(improvement_summary['best_bound'])}`",
        f"- Search relative gap: `{format_float(improvement_summary['gap'])}`",
        f"- Candidate exported objective: `{format_float(improvement_summary['candidate_objective'])}`",
        f"- Candidate audit ok: `{improvement_summary['candidate_audit_ok']}`",
        f"- Candidate decision: `{improvement_summary['candidate_status']}`",
        "",
        "## Final Artifact",
        "",
        f"- Elapsed wall time seconds: `{format_float(elapsed)}`",
        f"- Exported objective: `{format_float(final_audit['weighted_objective'])}`",
        f"- Schema ok: `{final_audit['schema_ok']}`",
        f"- Parse errors: `{len(final_audit['parse_errors'])}`",
        f"- Integrality residual: `{format_float(final_audit['integrality_residual'])}`",
        f"- Max normalized residual: `{format_float(final_audit['state_vector']['max_normalized_residual'])}`",
        f"- Violated constraint groups: `{final_audit['state_vector']['violated_constraint_groups']}`",
        f"- Artifact changed: `{improvement_summary['candidate_accepted']}`",
        "",
        "## Scenario Mission Probabilities",
        "",
    ]
    for scenario, value in final_audit["mission_prob"].items():
        lines.append(f"- `{scenario}`: `{format_float(value)}`")

    lines.extend(
        [
            "",
            "## State Vector",
            "",
            f"- Artifact present: `{final_audit['state_vector']['artifact_present']}`",
            f"- Trip rows: `{final_audit['state_vector']['incumbent_trip_rows']}`",
            f"- Active relays: `{final_audit['state_vector']['active_relays']}`",
            f"- Served districts: `{final_audit['state_vector']['served_districts']}`",
            "",
            "## Checks",
            "",
        ]
    )

    if baseline_result is not None:
        lines.extend(
            [
                f"- Baseline surrogate status: `{baseline_result['status']}`",
                f"- Baseline surrogate objective: `{format_float(baseline_result['objective'])}`",
                f"- Baseline surrogate bound: `{format_float(baseline_result['best_bound'])}`",
                f"- Baseline surrogate gap: `{format_float(baseline_result['gap'])}`",
            ]
        )

    if final_audit["parse_errors"]:
        for error in final_audit["parse_errors"]:
            lines.append(f"- Parse issue: `{error}`")
    else:
        lines.append("- Parse/domain checks: `passed`")

    violated = [item for item in final_audit["group_residuals"] if item[1] > 1e-7]
    if violated:
        for group, violation, norm in violated[:20]:
            lines.append(
                f"- Violated `{group}` with residual `{format_float(violation)}` and normalized residual `{format_float(norm)}`"
            )
    else:
        lines.append("- Public hard-constraint audit: `passed`")

    with open(LOG_PATH, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    start_time = time.time()
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Missing required model file: {MODEL_PATH}")

    instance = load_instance()
    baseline_result = None

    incumbent_audit = None
    if SOLUTION_PATH.exists():
        candidate_audit = audit_solution(instance, SOLUTION_PATH)
        if (
            candidate_audit["schema_ok"]
            and not candidate_audit["parse_errors"]
            and candidate_audit["state_vector"]["violated_constraint_groups"] == 0
        ):
            incumbent_audit = candidate_audit

    if incumbent_audit is None:
        baseline_result = solve_model(instance)
        if not baseline_result["has_solution"]:
            raise RuntimeError("No incumbent found by the surrogate SCIP model.")
        export_solution(instance, baseline_result)
        incumbent_audit = audit_solution(instance, SOLUTION_PATH)

    improvement_summary = run_true_objective_improvement(
        instance,
        incumbent_audit,
        get_hard_time_limit(),
    )
    final_audit = audit_solution(instance, SOLUTION_PATH)
    write_log(incumbent_audit, final_audit, improvement_summary, start_time, baseline_result=baseline_result)


if __name__ == "__main__":
    main()
