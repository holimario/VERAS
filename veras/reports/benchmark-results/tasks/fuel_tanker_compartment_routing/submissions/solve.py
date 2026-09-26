#!/usr/bin/env python3
import csv
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
SUBMISSIONS_DIR = BASE_DIR / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"

EPS = 1e-8
FEAS_EPS = 1e-6
CLASS_RANK = {"S": 0, "M": 1, "H": 2}
GROUP_RANK = {"M": 0, "R": 1, "F": 2}
GROUP_NAME = {0: "M", 1: "R", 2: "F"}
DEPOT_ID = "DEPOT"
RESTORATION_BUNDLES = [
    ("S01", "S03"),
    ("S02", "S31"),
    ("S04", "S32"),
    ("S05", "S06"),
    ("S07", "S08"),
    ("S09", "S10"),
    ("S27", "S28"),
    ("S11", "S12"),
    ("S13",),
    ("S14", "S15"),
    ("S16", "S17"),
    ("S18",),
    ("S29", "S30"),
    ("S19", "S20"),
    ("S21", "S22"),
    ("S23", "S24"),
    ("S25", "S26"),
]


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def round8(value):
    return float(f"{value:.8f}")


def powerset_masks(length):
    for mask in range(1, 1 << length):
        yield mask


def haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371.0
    p = math.pi / 180.0
    lat1 *= p
    lon1 *= p
    lat2 *= p
    lon2 *= p
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * radius_km * math.asin(math.sqrt(a))


def bitmask_subset(a, b):
    return (a | b) == b


def station_sort_key(station_id, stations):
    row = stations[station_id]
    return (row["district"], station_id)


def choose_single_pattern(patterns):
    return min(
        patterns,
        key=lambda p: (len(p["used_compartments"]), p["gmax"], -p["gmin"], p["used_mask"]),
    )


def choose_pair_patterns(patterns1, patterns2):
    best = None
    best_score = None
    for p1 in patterns1:
        for p2 in patterns2:
            if p1["used_mask"] & p2["used_mask"]:
                continue
            if p2["gmin"] < p1["gmax"]:
                continue
            score = (
                len(p1["used_compartments"]) + len(p2["used_compartments"]),
                p1["gmax"],
                -p2["gmin"],
                p1["used_mask"],
                p2["used_mask"],
            )
            if best is None or score < best_score:
                best = (p1, p2)
                best_score = score
    return best


def enumerate_station_patterns_for_vehicle_station(compartments, orders):
    patterns = []
    raw_assignments = {}
    order_count = len(orders)

    def recurse(comp_index, comp_to_order):
        if comp_index == len(compartments):
            capacity_by_order = [0.0] * order_count
            for local_idx, order_idx in comp_to_order.items():
                capacity_by_order[order_idx] += compartments[local_idx]["capacity_liters"]
            for order_idx, order in enumerate(orders):
                if capacity_by_order[order_idx] + FEAS_EPS < order["demand_liters"]:
                    return

            assignment = {}
            used_compartments = []
            for order_idx, order in enumerate(orders):
                assigned = [
                    compartments[local_idx]
                    for local_idx, mapped_order_idx in comp_to_order.items()
                    if mapped_order_idx == order_idx
                ]
                assigned.sort(
                    key=lambda comp: (-comp["capacity_liters"], comp["group_rank"], comp["compartment_id"])
                )
                remaining = order["demand_liters"]
                comp_map = {}
                for comp in assigned:
                    quantity = min(comp["capacity_liters"], remaining)
                    if quantity > FEAS_EPS:
                        comp_map[comp["compartment_id"]] = quantity
                        used_compartments.append(comp)
                        remaining -= quantity
                    if remaining <= FEAS_EPS:
                        break
                if remaining > FEAS_EPS:
                    return
                assignment[order["order_id"]] = comp_map

            if not used_compartments:
                return
            used_mask = 0
            for local_idx, comp in enumerate(compartments):
                if any(comp["compartment_id"] in assignment[order["order_id"]] for order in orders):
                    used_mask |= 1 << local_idx
            gmin = min(comp["group_rank"] for comp in used_compartments)
            gmax = max(comp["group_rank"] for comp in used_compartments)
            assignment_key = tuple(
                sorted(
                    (order_id, compartment_id, round(quantity, 8))
                    for order_id, comp_map in assignment.items()
                    for compartment_id, quantity in comp_map.items()
                )
            )
            raw_assignments[(used_mask, assignment_key)] = {
                "used_mask": used_mask,
                "used_compartments": tuple(
                    comp["compartment_id"]
                    for local_idx, comp in enumerate(compartments)
                    if used_mask & (1 << local_idx)
                ),
                "gmin": gmin,
                "gmax": gmax,
                "assignment": assignment,
            }
            return

        recurse(comp_index + 1, comp_to_order)
        comp = compartments[comp_index]
        for order_idx, order in enumerate(orders):
            if order["product_id"] not in comp["compatible_products"]:
                continue
            comp_to_order[comp_index] = order_idx
            recurse(comp_index + 1, comp_to_order)
            del comp_to_order[comp_index]

    recurse(0, {})
    patterns.extend(raw_assignments.values())
    return patterns


def load_instance():
    config = read_json(DATA_DIR / "config.json")

    products = {}
    for row in read_csv(DATA_DIR / "products.csv"):
        products[row["product_id"]] = {
            "product_id": row["product_id"],
            "description": row["description"],
            "density": float(row["density_kg_per_liter"]),
            "hose_family": row["hose_family"],
        }

    vehicles = {}
    for row in read_csv(DATA_DIR / "vehicles.csv"):
        vehicles[row["vehicle_id"]] = {
            "vehicle_id": row["vehicle_id"],
            "vehicle_class": row["vehicle_class"],
            "class_rank": CLASS_RANK[row["vehicle_class"]],
            "max_volume_liters": float(row["max_volume_liters"]),
            "max_payload_kg": float(row["max_payload_kg"]),
            "max_trips": int(row["max_trips"]),
            "work_limit_min": float(row["work_limit_min"]),
            "fixed_trip_cost": float(row["fixed_trip_cost"]),
            "cost_per_km": float(row["cost_per_km"]),
            "allowed_station_classes": set(row["allowed_station_classes"].split("|")),
            "compartments": [],
        }

    for row in read_csv(DATA_DIR / "compartments.csv"):
        vehicles[row["vehicle_id"]]["compartments"].append(
            {
                "compartment_id": row["compartment_id"],
                "group": row["group"],
                "group_rank": GROUP_RANK[row["group"]],
                "capacity_liters": float(row["capacity_liters"]),
                "compatible_products": set(row["compatible_products"].split("|")),
            }
        )

    for vehicle in vehicles.values():
        vehicle["compartments"].sort(
            key=lambda c: (c["group_rank"], -c["capacity_liters"], c["compartment_id"])
        )

    stations = {}
    for row in read_csv(DATA_DIR / "stations.csv"):
        stations[row["station_id"]] = {
            "station_id": row["station_id"],
            "station_name": row["station_name"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "access_class": row["access_class"],
            "earliest_min": float(row["earliest_min"]),
            "preferred_latest_min": float(row["preferred_latest_min"]),
            "latest_min": float(row["latest_min"]),
            "fixed_service_min": float(row["fixed_service_min"]),
            "split_allowed": row["split_allowed"] == "1",
            "risk_zone": row["risk_zone"],
            "district": row["district"],
        }

    orders = {}
    station_orders = defaultdict(list)
    for row in read_csv(DATA_DIR / "orders.csv"):
        order = {
            "order_id": row["order_id"],
            "station_id": row["station_id"],
            "product_id": row["product_id"],
            "demand_liters": float(row["demand_liters"]),
            "min_liters": float(row["min_liters"]),
            "shortfall_penalty_per_liter": float(row["shortfall_penalty_per_liter"]),
        }
        orders[order["order_id"]] = order
        station_orders[order["station_id"]].append(order)

    loading_slots = []
    for row in read_csv(DATA_DIR / "loading_slots.csv"):
        loading_slots.append(
            {
                "slot_id": row["slot_id"],
                "start_min": float(row["start_min"]),
                "rack_capacity": int(row["rack_capacity"]),
                "slot_cost": float(row["slot_cost"]),
            }
        )
    loading_slots.sort(key=lambda s: s["start_min"])

    bays = {}
    for row in read_csv(DATA_DIR / "loading_bays.csv"):
        bays[row["bay_id"]] = {
            "bay_id": row["bay_id"],
            "compatible_hose_families": set(row["compatible_hose_families"].split("|")),
            "max_vehicle_class": row["max_vehicle_class"],
            "max_vehicle_rank": CLASS_RANK[row["max_vehicle_class"]],
            "bay_cost": float(row["bay_cost"]),
        }

    cleaning_rules = {}
    for row in read_csv(DATA_DIR / "terminal_cleaning_rules.csv"):
        cleaning_rules[(row["from_hose_family"], row["to_hose_family"])] = {
            "cleanout_min": float(row["cleanout_min"]),
            "cleanout_cost": float(row["cleanout_cost"]),
        }

    rest_stops = {}
    for row in read_csv(DATA_DIR / "rest_stops.csv"):
        rest_stops[row["node_id"]] = {
            "node_id": row["node_id"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "risk_zone": row["risk_zone"],
            "min_break_min": float(row["min_break_min"]),
        }

    risk_rules = {}
    for row in read_csv(DATA_DIR / "risk_zones.csv"):
        risk_rules[(row["from_zone"], row["to_zone"])] = {
            "time_factor": float(row["time_factor"]),
            "hazmat_factor": float(row["hazmat_surcharge_factor"]),
        }

    road_adjustments = {}
    for row in read_csv(DATA_DIR / "road_adjustments.csv"):
        extra_minutes = float(row["extra_minutes"])
        extra_km = float(row["extra_km"])
        road_adjustments[(row["from_node"], row["to_node"])] = (extra_minutes, extra_km)
        road_adjustments[(row["to_node"], row["from_node"])] = (extra_minutes, extra_km)

    pairing_rules = []
    for row in read_csv(DATA_DIR / "station_product_rules.csv"):
        pairing_rules.append(
            {
                "station_id": row["station_id"],
                "product_a": row["product_a"],
                "product_b": row["product_b"],
                "rule": row["rule"],
            }
        )

    vehicle_station_exceptions = {}
    for row in read_csv(DATA_DIR / "vehicle_station_exceptions.csv"):
        vehicle_station_exceptions[(row["vehicle_id"], row["station_id"])] = row["rule"]

    station_bundle = {}
    for station_id, station in stations.items():
        station_order_list = sorted(station_orders[station_id], key=lambda o: o["order_id"])
        total_liters = sum(order["demand_liters"] for order in station_order_list)
        total_weight = sum(
            order["demand_liters"] * products[order["product_id"]]["density"]
            for order in station_order_list
        )
        families = {
            products[order["product_id"]]["hose_family"] for order in station_order_list
        }
        service_time = station["fixed_service_min"] + (
            total_liters / float(config["service_liters_per_min"])
        )
        station_bundle[station_id] = {
            "station_id": station_id,
            "orders": station_order_list,
            "products": {order["product_id"] for order in station_order_list},
            "families": families,
            "total_liters": total_liters,
            "total_weight": total_weight,
            "service_time": service_time,
        }

    nodes = {
        DEPOT_ID: {
            "node_id": DEPOT_ID,
            "lat": float(config["terminal_lat"]),
            "lon": float(config["terminal_lon"]),
            "risk_zone": "industrial",
        }
    }
    for station in stations.values():
        nodes[station["station_id"]] = {
            "node_id": station["station_id"],
            "lat": station["lat"],
            "lon": station["lon"],
            "risk_zone": station["risk_zone"],
        }
    for rest_stop in rest_stops.values():
        nodes[rest_stop["node_id"]] = {
            "node_id": rest_stop["node_id"],
            "lat": rest_stop["lat"],
            "lon": rest_stop["lon"],
            "risk_zone": rest_stop["risk_zone"],
        }

    travel = {}
    for origin_id, origin in nodes.items():
        for dest_id, dest in nodes.items():
            if origin_id == dest_id:
                travel[(origin_id, dest_id)] = {
                    "time_min": 0.0,
                    "distance_km": 0.0,
                    "haz_factor": 0.0,
                }
                continue
            base_km = haversine_km(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
            adjustment = road_adjustments.get((origin_id, dest_id), (0.0, 0.0))
            factors = risk_rules[(origin["risk_zone"], dest["risk_zone"])]
            travel[(origin_id, dest_id)] = {
                "time_min": (
                    base_km / float(config["base_speed_kmph"]) * 60.0 * factors["time_factor"]
                    + adjustment[0]
                ),
                "distance_km": base_km + adjustment[1],
                "haz_factor": factors["hazmat_factor"],
            }

    return {
        "config": config,
        "products": products,
        "vehicles": vehicles,
        "stations": stations,
        "orders": orders,
        "station_orders": station_orders,
        "loading_slots": loading_slots,
        "bays": bays,
        "cleaning_rules": cleaning_rules,
        "rest_stops": rest_stops,
        "risk_rules": risk_rules,
        "road_adjustments": road_adjustments,
        "pairing_rules": pairing_rules,
        "vehicle_station_exceptions": vehicle_station_exceptions,
        "station_bundle": station_bundle,
        "travel": travel,
    }


def vehicle_can_visit_station(instance, vehicle_id, station_id):
    vehicle = instance["vehicles"][vehicle_id]
    station = instance["stations"][station_id]
    exception = instance["vehicle_station_exceptions"].get((vehicle_id, station_id))
    if exception == "blocked":
        return False
    if exception == "allowed":
        return True
    return station["access_class"] in vehicle["allowed_station_classes"]


def compatible_bays_for_route(instance, vehicle, family_set):
    compatible = []
    for bay in instance["bays"].values():
        if vehicle["class_rank"] > bay["max_vehicle_rank"]:
            continue
        if not family_set.issubset(bay["compatible_hose_families"]):
            continue
        compatible.append(bay["bay_id"])
    compatible.sort()
    return compatible


def clean_transition(instance, families_from, families_to):
    best_time = None
    best_cost = None
    for family_from in families_from:
        for family_to in families_to:
            rule = instance["cleaning_rules"][(family_from, family_to)]
            if best_time is None or rule["cleanout_min"] < best_time - FEAS_EPS or (
                abs(rule["cleanout_min"] - best_time) <= FEAS_EPS
                and rule["cleanout_cost"] < best_cost - FEAS_EPS
            ):
                best_time = rule["cleanout_min"]
                best_cost = rule["cleanout_cost"]
    return best_time, best_cost


def build_station_patterns(instance):
    patterns = defaultdict(list)
    for vehicle_id, vehicle in instance["vehicles"].items():
        compartments = vehicle["compartments"]
        for station_id, bundle in instance["station_bundle"].items():
            if not vehicle_can_visit_station(instance, vehicle_id, station_id):
                continue
            if bundle["total_liters"] > vehicle["max_volume_liters"] + FEAS_EPS:
                continue
            if bundle["total_weight"] > vehicle["max_payload_kg"] + FEAS_EPS:
                continue

            local_patterns = enumerate_station_patterns_for_vehicle_station(
                compartments, bundle["orders"]
            )

            filtered = []
            for pattern in local_patterns:
                dominated = False
                for other in local_patterns:
                    if other is pattern:
                        continue
                    if not bitmask_subset(other["used_mask"], pattern["used_mask"]):
                        continue
                    if other["gmin"] + FEAS_EPS < pattern["gmin"]:
                        continue
                    if other["gmax"] - FEAS_EPS > pattern["gmax"]:
                        continue
                    if (
                        other["used_mask"] != pattern["used_mask"]
                        or other["gmin"] != pattern["gmin"]
                        or other["gmax"] != pattern["gmax"]
                    ):
                        dominated = True
                        break
                if not dominated:
                    filtered.append(
                        {
                            "station_id": station_id,
                            "vehicle_id": vehicle_id,
                            "used_mask": pattern["used_mask"],
                            "used_compartments": pattern["used_compartments"],
                            "gmin": pattern["gmin"],
                            "gmax": pattern["gmax"],
                            "assignment": {
                                order_id: {
                                    comp_id: qty
                                    for comp_id, qty in sorted(comp_map.items())
                                    if qty > FEAS_EPS
                                }
                                for order_id, comp_map in pattern["assignment"].items()
                            },
                        }
                    )

            patterns[(vehicle_id, station_id)] = filtered
    return patterns


def build_route_patterns(instance, station_patterns):
    routes = []
    route_id = 0
    station_ids = sorted(instance["stations"], key=lambda sid: station_sort_key(sid, instance["stations"]))
    for vehicle_id, vehicle in instance["vehicles"].items():
        for station_id in station_ids:
            patterns = station_patterns.get((vehicle_id, station_id), [])
            if not patterns:
                continue
            pattern = choose_single_pattern(patterns)
            families = set(instance["station_bundle"][station_id]["families"])
            compat_bays = compatible_bays_for_route(instance, vehicle, families)
            if not compat_bays:
                continue
            routes.append(
                {
                    "route_id": f"R{route_id:05d}",
                    "vehicle_id": vehicle_id,
                    "stops": [station_id],
                    "patterns": [pattern],
                    "families": tuple(sorted(families)),
                    "compat_bays": tuple(compat_bays),
                }
            )
            route_id += 1

        for station_a in station_ids:
            patterns_a = station_patterns.get((vehicle_id, station_a), [])
            if not patterns_a:
                continue
            for station_b in station_ids:
                if station_b == station_a:
                    continue
                patterns_b = station_patterns.get((vehicle_id, station_b), [])
                if not patterns_b:
                    continue
                bundle_a = instance["station_bundle"][station_a]
                bundle_b = instance["station_bundle"][station_b]
                total_liters = bundle_a["total_liters"] + bundle_b["total_liters"]
                total_weight = bundle_a["total_weight"] + bundle_b["total_weight"]
                if total_liters > vehicle["max_volume_liters"] + FEAS_EPS:
                    continue
                if total_weight > vehicle["max_payload_kg"] + FEAS_EPS:
                    continue
                pair = choose_pair_patterns(patterns_a, patterns_b)
                if pair is None:
                    continue
                families = set(bundle_a["families"]) | set(bundle_b["families"])
                compat_bays = compatible_bays_for_route(instance, vehicle, families)
                if not compat_bays:
                    continue
                routes.append(
                    {
                        "route_id": f"R{route_id:05d}",
                        "vehicle_id": vehicle_id,
                        "stops": [station_a, station_b],
                        "patterns": [pair[0], pair[1]],
                        "families": tuple(sorted(families)),
                        "compat_bays": tuple(compat_bays),
                    }
                )
                route_id += 1
    return routes


def add_route_metrics(instance, routes):
    config = instance["config"]
    for route in routes:
        vehicle = instance["vehicles"][route["vehicle_id"]]
        prev = DEPOT_ID
        total_distance = 0.0
        total_hazard_km = 0.0
        total_service = 0.0
        for stop_id in route["stops"]:
            arc = instance["travel"][(prev, stop_id)]
            total_distance += arc["distance_km"]
            total_hazard_km += arc["distance_km"] * arc["haz_factor"]
            total_service += instance["station_bundle"][stop_id]["service_time"]
            prev = stop_id
        arc = instance["travel"][(prev, DEPOT_ID)]
        total_distance += arc["distance_km"]
        total_hazard_km += arc["distance_km"] * arc["haz_factor"]
        route["total_distance_km"] = total_distance
        route["total_hazard_km"] = total_hazard_km
        route["base_cost"] = (
            vehicle["fixed_trip_cost"]
            + vehicle["cost_per_km"] * total_distance
            + float(config["hazmat_surcharge_per_km"]) * total_hazard_km
        )


def build_candidates(instance, routes):
    config = instance["config"]
    candidates = []
    candidate_id = 0
    slot_lookup = {slot["slot_id"]: slot for slot in instance["loading_slots"]}
    for route in routes:
        min_bay_cost = min(
            instance["bays"][bay_id]["bay_cost"] for bay_id in route["compat_bays"]
        )
        for slot in instance["loading_slots"]:
            current_time = slot["start_min"] + float(config["loading_duration_min"])
            stops = []
            total_lateness = 0.0
            prev = DEPOT_ID
            feasible = True
            for stop_id in route["stops"]:
                station = instance["stations"][stop_id]
                arrival = current_time + instance["travel"][(prev, stop_id)]["time_min"]
                if arrival + FEAS_EPS < station["earliest_min"]:
                    feasible = False
                    break
                if arrival - FEAS_EPS > station["latest_min"]:
                    feasible = False
                    break
                departure = arrival + instance["station_bundle"][stop_id]["service_time"]
                total_lateness += max(0.0, arrival - station["preferred_latest_min"])
                stops.append(
                    {
                        "node_id": stop_id,
                        "arrival_min": arrival,
                        "departure_min": departure,
                    }
                )
                current_time = departure
                prev = stop_id
            if not feasible:
                continue

            return_time = current_time + instance["travel"][(prev, DEPOT_ID)]["time_min"]
            if return_time - FEAS_EPS > float(config["shift_end_min"]):
                continue
            if return_time - slot["start_min"] - FEAS_EPS > float(config["max_trip_elapsed_min"]):
                continue

            candidate_cost = (
                route["base_cost"]
                + float(config["late_penalty_per_min"]) * total_lateness
                + slot["slot_cost"]
                + min_bay_cost
            )
            deliveries = []
            for stop_index, pattern in enumerate(route["patterns"], start=1):
                for order_id, comp_map in pattern["assignment"].items():
                    for compartment_id, quantity in comp_map.items():
                        deliveries.append(
                            {
                                "order_id": order_id,
                                "stop_sequence": stop_index,
                                "compartment_id": compartment_id,
                                "quantity_liters": quantity,
                            }
                        )
            deliveries.sort(
                key=lambda d: (d["stop_sequence"], d["order_id"], d["compartment_id"])
            )
            candidates.append(
                {
                    "candidate_id": f"C{candidate_id:06d}",
                    "route_id": route["route_id"],
                    "vehicle_id": route["vehicle_id"],
                    "slot_id": slot["slot_id"],
                    "load_start_min": slot["start_min"],
                    "return_min": return_time,
                    "stops": stops,
                    "station_ids": tuple(route["stops"]),
                    "deliveries": deliveries,
                    "compat_bays": route["compat_bays"],
                    "compat_bay_mask": sum(
                        1 << idx
                        for idx, bay_id in enumerate(sorted(instance["bays"]))
                        if bay_id in route["compat_bays"]
                    ),
                    "families": route["families"],
                    "objective_cost": candidate_cost,
                    "true_cost_no_cleanout": candidate_cost,
                }
            )
            candidate_id += 1
    return candidates


def build_master_model(instance, candidates, time_limit, no_good_cuts):
    model = Model("route_master")
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)
    positions = (1, 2)
    x = {}
    for idx, candidate in enumerate(candidates):
        for position in positions:
            x[(idx, position)] = model.addVar(
                vtype="B", name=f"{candidate['candidate_id']}[t{position}]"
            )

    model.setObjective(
        quicksum(
            candidates[idx]["objective_cost"] * x[(idx, position)]
            for idx in range(len(candidates))
            for position in positions
        ),
        "minimize",
    )

    station_cover = defaultdict(list)
    by_vehicle = defaultdict(lambda: defaultdict(list))
    by_slot = defaultdict(list)
    for idx, candidate in enumerate(candidates):
        for position in positions:
            for station_id in candidate["station_ids"]:
                station_cover[station_id].append((idx, position))
            by_vehicle[candidate["vehicle_id"]][position].append(idx)
            by_slot[candidate["slot_id"]].append((idx, position))

    for station_id in sorted(instance["stations"]):
        model.addCons(
            quicksum(x[key] for key in station_cover[station_id]) == 1,
            name=f"cover[{station_id}]",
        )

    big_m_time = float(instance["config"]["shift_end_min"]) + float(
        instance["config"]["max_trip_elapsed_min"]
    )
    for vehicle_id, pos_map in sorted(by_vehicle.items()):
        indices_pos1 = pos_map[1]
        indices_pos2 = pos_map[2]
        model.addCons(
            quicksum(x[(idx, 1)] for idx in indices_pos1) <= 1,
            name=f"trip_pos[{vehicle_id},1]",
        )
        model.addCons(
            quicksum(x[(idx, 2)] for idx in indices_pos2) <= 1,
            name=f"trip_pos[{vehicle_id},2]",
        )
        model.addCons(
            quicksum(x[(idx, 2)] for idx in indices_pos2)
            <= quicksum(x[(idx, 1)] for idx in indices_pos1),
            name=f"trip_order[{vehicle_id}]",
        )

        start1 = quicksum(candidates[idx]["load_start_min"] * x[(idx, 1)] for idx in indices_pos1)
        end1 = quicksum(candidates[idx]["return_min"] * x[(idx, 1)] for idx in indices_pos1)
        start2 = quicksum(candidates[idx]["load_start_min"] * x[(idx, 2)] for idx in indices_pos2)
        end2 = quicksum(candidates[idx]["return_min"] * x[(idx, 2)] for idx in indices_pos2)
        active2 = quicksum(x[(idx, 2)] for idx in indices_pos2)

        model.addCons(
            start2
            >= end1 + float(instance["config"]["turnaround_min"]) - big_m_time * (1 - active2),
            name=f"turnaround[{vehicle_id}]",
        )
        model.addCons(
            end2 - start1
            <= instance["vehicles"][vehicle_id]["work_limit_min"] + big_m_time * (1 - active2),
            name=f"work_limit[{vehicle_id}]",
        )

    for slot in instance["loading_slots"]:
        model.addCons(
            quicksum(x[key] for key in by_slot[slot["slot_id"]]) <= slot["rack_capacity"],
            name=f"slot_cap[{slot['slot_id']}]",
        )

    bay_ids = sorted(instance["bays"])
    all_masks = list(range(1, 1 << len(bay_ids)))
    for slot in instance["loading_slots"]:
        slot_indices = by_slot[slot["slot_id"]]
        if not slot_indices:
            continue
        for subset_mask in all_masks:
            indices = [
                key
                for key in slot_indices
                if candidates[key[0]]["compat_bay_mask"] & subset_mask
                == candidates[key[0]]["compat_bay_mask"]
            ]
            subset_size = bin(subset_mask).count("1")
            if len(indices) > subset_size:
                model.addCons(
                    quicksum(x[key] for key in indices) <= subset_size,
                    name=f"hall[{slot['slot_id']}][{subset_mask}]",
                )

    for cut_idx, cut in enumerate(no_good_cuts):
        model.addCons(
            quicksum(x[key] for key in cut) <= len(cut) - 1,
            name=f"nogood[{cut_idx}]",
        )

    return model, x


def extract_selected_master_solution(model, x, candidates):
    if model.getNSols() <= 0:
        return None
    selected = []
    for key, var in x.items():
        if model.getVal(var) > 0.5:
            idx, position = key
            selected.append((position, candidates[idx]))
    selected.sort(key=lambda item: (item[1]["vehicle_id"], item[0], item[1]["load_start_min"]))
    return selected


def solve_bay_assignment(instance, selected_candidates, time_limit):
    bay_ids = sorted(instance["bays"])
    index_of = {cand["candidate_id"]: idx for idx, cand in enumerate(selected_candidates)}
    model = Model("bay_assignment")
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    arcs = {}
    incoming = defaultdict(list)
    outgoing = defaultdict(list)

    for bay_id in bay_ids:
        for j, cand_j in enumerate(selected_candidates):
            if bay_id not in cand_j["compat_bays"]:
                continue
            key = (None, j, bay_id)
            arcs[key] = model.addVar(vtype="B", name=f"y[start,{j},{bay_id}]")
            incoming[j].append(key)
        for i, cand_i in enumerate(selected_candidates):
            if bay_id not in cand_i["compat_bays"]:
                continue
            for j in range(i + 1, len(selected_candidates)):
                cand_j = selected_candidates[j]
                if bay_id not in cand_j["compat_bays"]:
                    continue
                clean_time, _ = clean_transition(
                    instance, cand_i["families"], cand_j["families"]
                )
                if (
                    cand_j["load_start_min"] + FEAS_EPS
                    < cand_i["load_start_min"]
                    + float(instance["config"]["loading_duration_min"])
                    + clean_time
                ):
                    continue
                key = (i, j, bay_id)
                arcs[key] = model.addVar(vtype="B", name=f"y[{i},{j},{bay_id}]")
                incoming[j].append(key)
                outgoing[(i, bay_id)].append(key)

    for j in range(len(selected_candidates)):
        model.addCons(
            quicksum(arcs[key] for key in incoming[j]) == 1,
            name=f"incoming[{j}]",
        )

    for bay_id in bay_ids:
        starts = [key for key in arcs if key[0] is None and key[2] == bay_id]
        model.addCons(
            quicksum(arcs[key] for key in starts) <= 1,
            name=f"start_limit[{bay_id}]",
        )

    for i in range(len(selected_candidates)):
        for bay_id in bay_ids:
            keys_out = outgoing.get((i, bay_id), [])
            if not keys_out:
                continue
            keys_in = [
                key
                for key in incoming[i]
                if key[2] == bay_id
            ]
            model.addCons(
                quicksum(arcs[key] for key in keys_out)
                <= quicksum(arcs[key] for key in keys_in),
                name=f"flow[{i},{bay_id}]",
            )

    objective_terms = []
    for key, var in arcs.items():
        predecessor, successor, bay_id = key
        bay_cost = instance["bays"][bay_id]["bay_cost"]
        if predecessor is None:
            cost = bay_cost
        else:
            _, clean_cost = clean_transition(
                instance,
                selected_candidates[predecessor]["families"],
                selected_candidates[successor]["families"],
            )
            cost = bay_cost + clean_cost
        objective_terms.append(cost * var)
    model.setObjective(quicksum(objective_terms), "minimize")
    model.optimize()

    if model.getNSols() <= 0:
        return None

    assigned_bay = {}
    predecessors = {}
    for key, var in arcs.items():
        if model.getVal(var) > 0.5:
            predecessor, successor, bay_id = key
            assigned_bay[successor] = bay_id
            predecessors[successor] = predecessor

    cleanout_cost = 0.0
    cleanout_checks = []
    by_bay = defaultdict(list)
    for idx, bay_id in assigned_bay.items():
        by_bay[bay_id].append((selected_candidates[idx]["load_start_min"], idx))
    for bay_id, items in by_bay.items():
        items.sort()
        for pos in range(1, len(items)):
            prev_idx = items[pos - 1][1]
            curr_idx = items[pos][1]
            clean_time, clean_cost = clean_transition(
                instance,
                selected_candidates[prev_idx]["families"],
                selected_candidates[curr_idx]["families"],
            )
            gap = (
                selected_candidates[curr_idx]["load_start_min"]
                - selected_candidates[prev_idx]["load_start_min"]
            )
            required_gap = float(instance["config"]["loading_duration_min"]) + clean_time
            if gap + FEAS_EPS < required_gap:
                return None
            cleanout_cost += clean_cost
            cleanout_checks.append(
                {
                    "bay_id": bay_id,
                    "from_trip": selected_candidates[prev_idx]["candidate_id"],
                    "to_trip": selected_candidates[curr_idx]["candidate_id"],
                    "required_gap": required_gap,
                    "actual_gap": gap,
                    "cleanout_cost": clean_cost,
                }
            )

    bay_cost = sum(instance["bays"][bay_id]["bay_cost"] for bay_id in assigned_bay.values())
    return {
        "assigned_bay": assigned_bay,
        "bay_cost": bay_cost,
        "cleanout_cost": cleanout_cost,
        "cleanout_checks": cleanout_checks,
        "status": model.getStatus(),
        "objective": model.getObjVal() if model.getNSols() > 0 else None,
    }


def solve_restoration_assignment(instance, candidates, bundles, time_limit):
    bundle_set = set(bundles)
    restricted_candidates = [
        candidate for candidate in candidates if candidate["station_ids"] in bundle_set
    ]
    if not restricted_candidates:
        return None

    candidates_by_bundle = defaultdict(list)
    for idx, candidate in enumerate(restricted_candidates):
        candidates_by_bundle[candidate["station_ids"]].append(idx)
    for bundle in bundles:
        if not candidates_by_bundle[bundle]:
            return None

    model = Model("restoration_bundle_assignment")
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    x = {idx: model.addVar(vtype="B", name=restricted_candidates[idx]["candidate_id"]) for idx in range(len(restricted_candidates))}

    for bundle in bundles:
        model.addCons(
            quicksum(x[idx] for idx in candidates_by_bundle[bundle]) == 1,
            name=f"bundle[{','.join(bundle)}]",
        )

    for slot in instance["loading_slots"]:
        indices = [
            idx
            for idx, candidate in enumerate(restricted_candidates)
            if candidate["slot_id"] == slot["slot_id"]
        ]
        if indices:
            model.addCons(
                quicksum(x[idx] for idx in indices) <= 1,
                name=f"slot_unique[{slot['slot_id']}]",
            )

    for vehicle_id in sorted(instance["vehicles"]):
        indices = [
            idx
            for idx, candidate in enumerate(restricted_candidates)
            if candidate["vehicle_id"] == vehicle_id
        ]
        if not indices:
            continue
        first_trip = model.addVar(vtype="B", name=f"first[{vehicle_id}]")
        second_trip = model.addVar(vtype="B", name=f"second[{vehicle_id}]")
        y_first = {
            idx: model.addVar(vtype="B", name=f"yfirst[{vehicle_id},{idx}]")
            for idx in indices
        }
        y_second = {
            idx: model.addVar(vtype="B", name=f"ysecond[{vehicle_id},{idx}]")
            for idx in indices
        }
        model.addCons(
            quicksum(x[idx] for idx in indices) == first_trip + second_trip,
            name=f"trip_count[{vehicle_id}]",
        )
        model.addCons(second_trip <= first_trip, name=f"trip_order[{vehicle_id}]")
        for idx in indices:
            model.addCons(
                y_first[idx] + y_second[idx] == x[idx],
                name=f"assign_pos[{vehicle_id},{idx}]",
            )
        model.addCons(
            quicksum(y_first[idx] for idx in indices) == first_trip,
            name=f"one_first[{vehicle_id}]",
        )
        model.addCons(
            quicksum(y_second[idx] for idx in indices) == second_trip,
            name=f"one_second[{vehicle_id}]",
        )
        big_m = 5000.0
        model.addCons(
            quicksum(restricted_candidates[idx]["load_start_min"] * y_second[idx] for idx in indices)
            >= quicksum(restricted_candidates[idx]["return_min"] * y_first[idx] for idx in indices)
            + float(instance["config"]["turnaround_min"])
            - big_m * (1 - second_trip),
            name=f"turnaround[{vehicle_id}]",
        )
        model.addCons(
            quicksum(restricted_candidates[idx]["return_min"] * y_second[idx] for idx in indices)
            - quicksum(restricted_candidates[idx]["load_start_min"] * y_first[idx] for idx in indices)
            <= instance["vehicles"][vehicle_id]["work_limit_min"] + big_m * (1 - second_trip),
            name=f"work_limit[{vehicle_id}]",
        )
        model.addCons(
            quicksum(restricted_candidates[idx]["load_start_min"] * y_first[idx] for idx in indices)
            <= quicksum(restricted_candidates[idx]["load_start_min"] * y_second[idx] for idx in indices)
            + big_m * (1 - second_trip),
            name=f"chronology[{vehicle_id}]",
        )

    model.setObjective(
        quicksum(
            restricted_candidates[idx]["objective_cost"] * x[idx]
            for idx in range(len(restricted_candidates))
        ),
        "minimize",
    )
    model.optimize()
    if model.getNSols() <= 0:
        return {
            "status": str(model.getStatus()),
            "objective": None,
            "dual_bound": model.getDualbound(),
            "gap": model.getGap(),
            "selected_candidates": None,
            "candidate_count": len(restricted_candidates),
        }

    selected_candidates = [
        restricted_candidates[idx]
        for idx in range(len(restricted_candidates))
        if model.getVal(x[idx]) > 0.5
    ]
    selected_candidates.sort(
        key=lambda candidate: (
            candidate["load_start_min"],
            candidate["vehicle_id"],
            candidate["candidate_id"],
        )
    )
    return {
        "status": str(model.getStatus()),
        "objective": model.getObjVal(),
        "dual_bound": model.getDualbound(),
        "gap": model.getGap(),
        "selected_candidates": selected_candidates,
        "candidate_count": len(restricted_candidates),
    }


def attempt_local_branching_improvement(
    instance,
    candidates,
    incumbent_candidates,
    incumbent_objective,
    time_limit,
    radius=4,
):
    if time_limit <= 0:
        return {
            "status": "skipped",
            "objective": None,
            "dual_bound": None,
            "gap": None,
            "selected_candidates": None,
            "bay_solution": None,
            "audit": None,
        }

    model, x = build_master_model(instance, candidates, time_limit, [])
    candidate_index = {
        candidate["candidate_id"]: idx for idx, candidate in enumerate(candidates)
    }
    by_vehicle = defaultdict(list)
    for candidate in incumbent_candidates:
        by_vehicle[candidate["vehicle_id"]].append(candidate)

    incumbent_keys = set()
    for vehicle_id, vehicle_candidates in by_vehicle.items():
        vehicle_candidates.sort(
            key=lambda candidate: (candidate["load_start_min"], candidate["candidate_id"])
        )
        for position, candidate in enumerate(vehicle_candidates, start=1):
            incumbent_keys.add((candidate_index[candidate["candidate_id"]], position))

    objective_expr = quicksum(
        candidates[idx]["objective_cost"] * x[(idx, position)]
        for idx in range(len(candidates))
        for position in (1, 2)
    )
    model.addCons(
        objective_expr <= incumbent_objective - 1e-5,
        name="improve_cut",
    )
    model.addCons(
        quicksum(1 - x[key] for key in incumbent_keys)
        + quicksum(x[key] for key in x if key not in incumbent_keys)
        <= radius,
        name="local_branching",
    )

    start_solution = model.createSol()
    for key, variable in x.items():
        model.setSolVal(start_solution, variable, 1.0 if key in incumbent_keys else 0.0)
    model.addSol(start_solution, False)
    model.optimize()

    result = {
        "status": str(model.getStatus()),
        "objective": model.getObjVal() if model.getNSols() > 0 else None,
        "dual_bound": model.getDualbound(),
        "gap": model.getGap(),
        "selected_candidates": None,
        "bay_solution": None,
        "audit": None,
    }
    if model.getNSols() <= 0:
        return result

    selected = extract_selected_master_solution(model, x, candidates)
    if not selected:
        return result
    selected_candidates = [candidate for _, candidate in selected]
    selected_candidates.sort(
        key=lambda candidate: (
            candidate["load_start_min"],
            candidate["vehicle_id"],
            candidate["candidate_id"],
        )
    )
    bay_solution = solve_bay_assignment(instance, selected_candidates, max(5.0, min(20.0, time_limit)))
    result["selected_candidates"] = selected_candidates
    result["bay_solution"] = bay_solution
    return result


def compose_solution(selected_candidates, bay_solution):
    trips = []
    by_vehicle = defaultdict(list)
    for idx, cand in enumerate(selected_candidates):
        by_vehicle[cand["vehicle_id"]].append((cand["load_start_min"], idx, cand))

    for vehicle_id, items in by_vehicle.items():
        items.sort()
        for trip_number, (_, idx, cand) in enumerate(items, start=1):
            bay_id = bay_solution["assigned_bay"][idx]
            trip = {
                "vehicle_id": vehicle_id,
                "trip_id": f"{vehicle_id}_{trip_number}",
                "bay_id": bay_id,
                "load_start_min": round8(cand["load_start_min"]),
                "stops": [
                    {
                        "node_id": stop["node_id"],
                        "arrival_min": round8(stop["arrival_min"]),
                        "departure_min": round8(stop["departure_min"]),
                    }
                    for stop in cand["stops"]
                ],
                "deliveries": [
                    {
                        "order_id": delivery["order_id"],
                        "stop_sequence": delivery["stop_sequence"],
                        "compartment_id": delivery["compartment_id"],
                        "quantity_liters": round8(delivery["quantity_liters"]),
                    }
                    for delivery in cand["deliveries"]
                ],
            }
            trips.append(trip)

    trips.sort(key=lambda t: (t["load_start_min"], t["vehicle_id"], t["trip_id"]))
    return {"trips": trips}


def validate_solution(instance, solution):
    errors = []
    objective = {
        "fixed_trip_cost": 0.0,
        "distance_cost": 0.0,
        "hazmat_cost": 0.0,
        "lateness_cost": 0.0,
        "slot_cost": 0.0,
        "bay_cost": 0.0,
        "cleanout_cost": 0.0,
        "shortfill_cost": 0.0,
    }

    slot_by_start = {
        round(slot["start_min"], 8): slot for slot in instance["loading_slots"]
    }
    trip_ids = set()
    delivered_by_order = defaultdict(float)
    order_visit_map = defaultdict(set)
    slot_usage = defaultdict(int)
    vehicle_trips = defaultdict(list)
    bay_usage = defaultdict(list)

    for trip in solution.get("trips", []):
        vehicle_id = trip.get("vehicle_id")
        trip_id = trip.get("trip_id")
        bay_id = trip.get("bay_id")
        load_start = float(trip.get("load_start_min"))
        stops = trip.get("stops", [])
        deliveries = trip.get("deliveries", [])

        if trip_id in trip_ids:
            errors.append(f"Duplicate trip_id {trip_id}")
        trip_ids.add(trip_id)

        if vehicle_id not in instance["vehicles"]:
            errors.append(f"Unknown vehicle {vehicle_id}")
            continue
        if bay_id not in instance["bays"]:
            errors.append(f"Unknown bay {bay_id}")
            continue
        if round(load_start, 8) not in slot_by_start:
            errors.append(f"Trip {trip_id} load_start_min is not a listed slot start")
            continue

        slot = slot_by_start[round(load_start, 8)]
        slot_usage[slot["slot_id"]] += 1
        vehicle = instance["vehicles"][vehicle_id]
        bay = instance["bays"][bay_id]
        objective["slot_cost"] += slot["slot_cost"]
        objective["bay_cost"] += bay["bay_cost"]

        if vehicle["class_rank"] > bay["max_vehicle_rank"]:
            errors.append(f"Trip {trip_id} uses incompatible bay class {bay_id}")

        compartment_usage = {}
        compartment_orders = defaultdict(set)
        stop_nodes = {}
        for seq, stop in enumerate(stops, start=1):
            node_id = stop.get("node_id")
            if node_id not in instance["stations"] and node_id not in instance["rest_stops"]:
                errors.append(f"Trip {trip_id} stop {seq} has unknown node {node_id}")
                continue
            if node_id in stop_nodes.values():
                errors.append(f"Trip {trip_id} repeats stop node {node_id}")
            stop_nodes[seq] = node_id

        families_used = set()
        stop_deliveries = defaultdict(list)
        for delivery in deliveries:
            order_id = delivery["order_id"]
            if order_id not in instance["orders"]:
                errors.append(f"Trip {trip_id} references unknown order {order_id}")
                continue
            order = instance["orders"][order_id]
            seq = int(delivery["stop_sequence"])
            compartment_id = delivery["compartment_id"]
            quantity = float(delivery["quantity_liters"])
            if seq not in stop_nodes:
                errors.append(f"Trip {trip_id} delivery {order_id} references invalid stop sequence")
                continue
            if stop_nodes[seq] != order["station_id"]:
                errors.append(
                    f"Trip {trip_id} delivery {order_id} points to {stop_nodes[seq]} not {order['station_id']}"
                )
            compartment_usage[compartment_id] = compartment_usage.get(compartment_id, 0.0) + quantity
            compartment_orders[compartment_id].add(order_id)
            stop_deliveries[seq].append(delivery)
            delivered_by_order[order_id] += quantity
            order_visit_map[order_id].add((trip_id, seq))
            families_used.add(
                instance["products"][order["product_id"]]["hose_family"]
            )

        if not families_used.issubset(bay["compatible_hose_families"]):
            errors.append(f"Trip {trip_id} uses bay {bay_id} with incompatible hose families")

        vehicle_compartments = {
            comp["compartment_id"]: comp for comp in vehicle["compartments"]
        }
        total_liters = 0.0
        total_weight = 0.0
        stop_group_ranges = []
        for seq in range(1, len(stops) + 1):
            groups = []
            for delivery in stop_deliveries.get(seq, []):
                order = instance["orders"][delivery["order_id"]]
                quantity = float(delivery["quantity_liters"])
                compartment_id = delivery["compartment_id"]
                if compartment_id not in vehicle_compartments:
                    errors.append(f"Trip {trip_id} uses foreign compartment {compartment_id}")
                    continue
                comp = vehicle_compartments[compartment_id]
                if order["product_id"] not in comp["compatible_products"]:
                    errors.append(
                        f"Trip {trip_id} compartment {compartment_id} incompatible with product {order['product_id']}"
                    )
                groups.append(comp["group_rank"])
                total_liters += quantity
                total_weight += quantity * instance["products"][order["product_id"]]["density"]
            if groups:
                stop_group_ranges.append((min(groups), max(groups)))

        for compartment_id, quantity in compartment_usage.items():
            if compartment_id not in vehicle_compartments:
                continue
            if len(compartment_orders[compartment_id]) > 1:
                errors.append(
                    f"Trip {trip_id} compartment {compartment_id} serves multiple order items"
                )
            if quantity - FEAS_EPS > vehicle_compartments[compartment_id]["capacity_liters"]:
                errors.append(
                    f"Trip {trip_id} compartment {compartment_id} exceeds capacity"
                )

        if total_liters - FEAS_EPS > vehicle["max_volume_liters"]:
            errors.append(f"Trip {trip_id} exceeds vehicle volume")
        if total_weight - FEAS_EPS > vehicle["max_payload_kg"]:
            errors.append(f"Trip {trip_id} exceeds vehicle payload")

        last_max_group = -1
        for min_group, max_group in stop_group_ranges:
            if min_group + FEAS_EPS < last_max_group:
                errors.append(f"Trip {trip_id} violates compartment precedence")
                break
            last_max_group = max(last_max_group, max_group)

        departure_from_depot = load_start + float(instance["config"]["loading_duration_min"])
        current_time = departure_from_depot
        prev_node = DEPOT_ID
        for seq, stop in enumerate(stops, start=1):
            node_id = stop["node_id"]
            expected_arrival = current_time + instance["travel"][(prev_node, node_id)]["time_min"]
            reported_arrival = float(stop["arrival_min"])
            if abs(reported_arrival - expected_arrival) > 1e-5:
                errors.append(
                    f"Trip {trip_id} stop {seq} arrival does not match propagated travel time"
                )
            service_time = 0.0
            if node_id in instance["stations"]:
                station = instance["stations"][node_id]
                if reported_arrival + FEAS_EPS < station["earliest_min"]:
                    errors.append(f"Trip {trip_id} arrives early at {node_id}")
                if reported_arrival - FEAS_EPS > station["latest_min"]:
                    errors.append(f"Trip {trip_id} arrives late at {node_id}")
                station_orders_here = [
                    instance["orders"][d["order_id"]]
                    for d in stop_deliveries.get(seq, [])
                ]
                liters_here = sum(float(d["quantity_liters"]) for d in stop_deliveries.get(seq, []))
                service_time = station["fixed_service_min"] + (
                    liters_here / float(instance["config"]["service_liters_per_min"])
                )
                objective["lateness_cost"] += float(instance["config"]["late_penalty_per_min"]) * max(
                    0.0, reported_arrival - station["preferred_latest_min"]
                )
                if not vehicle_can_visit_station(instance, vehicle_id, node_id):
                    errors.append(f"Trip {trip_id} vehicle cannot access {node_id}")
            reported_departure = float(stop["departure_min"])
            expected_departure = reported_arrival + service_time
            if abs(reported_departure - expected_departure) > 1e-5:
                errors.append(
                    f"Trip {trip_id} stop {seq} departure does not match service completion"
                )
            current_time = reported_departure
            prev_node = node_id

        return_time = current_time + instance["travel"][(prev_node, DEPOT_ID)]["time_min"]
        if return_time - FEAS_EPS > float(instance["config"]["shift_end_min"]):
            errors.append(f"Trip {trip_id} returns after shift end")
        if return_time - load_start - FEAS_EPS > float(instance["config"]["max_trip_elapsed_min"]):
            errors.append(f"Trip {trip_id} exceeds max trip elapsed")

        total_distance = 0.0
        total_hazard_km = 0.0
        prev_node = DEPOT_ID
        for stop in stops:
            arc = instance["travel"][(prev_node, stop["node_id"])]
            total_distance += arc["distance_km"]
            total_hazard_km += arc["distance_km"] * arc["haz_factor"]
            prev_node = stop["node_id"]
        arc = instance["travel"][(prev_node, DEPOT_ID)]
        total_distance += arc["distance_km"]
        total_hazard_km += arc["distance_km"] * arc["haz_factor"]
        objective["fixed_trip_cost"] += vehicle["fixed_trip_cost"]
        objective["distance_cost"] += vehicle["cost_per_km"] * total_distance
        objective["hazmat_cost"] += float(instance["config"]["hazmat_surcharge_per_km"]) * total_hazard_km

        vehicle_trips[vehicle_id].append((load_start, return_time, trip_id))
        bay_usage[bay_id].append((load_start, trip_id, tuple(sorted(families_used))))

    for slot in instance["loading_slots"]:
        if slot_usage[slot["slot_id"]] > slot["rack_capacity"]:
            errors.append(f"Slot {slot['slot_id']} exceeds rack capacity")

    for vehicle_id, trips in vehicle_trips.items():
        trips.sort()
        if len(trips) > instance["vehicles"][vehicle_id]["max_trips"]:
            errors.append(f"Vehicle {vehicle_id} exceeds max trips")
        if trips:
            span = trips[-1][1] - trips[0][0]
            if span - FEAS_EPS > instance["vehicles"][vehicle_id]["work_limit_min"]:
                errors.append(f"Vehicle {vehicle_id} exceeds work limit")
        for pos in range(1, len(trips)):
            prev = trips[pos - 1]
            curr = trips[pos]
            if curr[0] + FEAS_EPS < prev[1] + float(instance["config"]["turnaround_min"]):
                errors.append(f"Vehicle {vehicle_id} violates turnaround between trips")

    for bay_id, items in bay_usage.items():
        items.sort()
        for pos in range(1, len(items)):
            prev_start, prev_trip_id, prev_families = items[pos - 1]
            curr_start, curr_trip_id, curr_families = items[pos]
            clean_time, clean_cost = clean_transition(
                instance, prev_families, curr_families
            )
            required_gap = float(instance["config"]["loading_duration_min"]) + clean_time
            if curr_start + FEAS_EPS < prev_start + required_gap:
                errors.append(
                    f"Bay {bay_id} violates cleanout between {prev_trip_id} and {curr_trip_id}"
                )
            objective["cleanout_cost"] += clean_cost

    for order_id, order in instance["orders"].items():
        delivered = delivered_by_order[order_id]
        if len(order_visit_map[order_id]) > 1:
            errors.append(f"Order {order_id} is delivered in multiple trips or visits")
        if delivered + FEAS_EPS < order["min_liters"]:
            errors.append(f"Order {order_id} below minimum liters")
        if delivered - FEAS_EPS > order["demand_liters"]:
            errors.append(f"Order {order_id} exceeds demand")
        objective["shortfill_cost"] += (
            max(0.0, order["demand_liters"] - delivered)
        ) * order["shortfall_penalty_per_liter"]

    for station_id, station in instance["stations"].items():
        visits = set()
        for order in instance["station_orders"][station_id]:
            visits.update(order_visit_map[order["order_id"]])
        if not station["split_allowed"] and len(visits) > 1:
            errors.append(f"Station {station_id} is split across multiple trips or visits")

    for rule in instance["pairing_rules"]:
        if rule["rule"] != "same_trip_required":
            continue
        order_a = None
        order_b = None
        for order in instance["station_orders"][rule["station_id"]]:
            if order["product_id"] == rule["product_a"]:
                order_a = order["order_id"]
            if order["product_id"] == rule["product_b"]:
                order_b = order["order_id"]
        if order_a and order_b:
            visits_a = order_visit_map[order_a]
            visits_b = order_visit_map[order_b]
            if len(visits_a) != 1 or len(visits_b) != 1 or visits_a != visits_b:
                errors.append(f"Pairing rule broken at station {rule['station_id']}")

    total_objective = sum(objective.values())
    return {
        "feasible": not errors,
        "errors": errors,
        "objective_components": objective,
        "total_objective": total_objective,
    }


def run_independent_public_audit(solution_path=SOLUTION_PATH):
    audit_code = r"""
import csv, json, math
from collections import defaultdict
from pathlib import Path
base = Path('/app')
data = base / 'data'
solution = json.load(open(Path(__SOLUTION_PATH__)))
config = json.load(open(data / 'config.json'))
products = {r['product_id']: r for r in csv.DictReader(open(data / 'products.csv'))}
vehicles = {r['vehicle_id']: r for r in csv.DictReader(open(data / 'vehicles.csv'))}
stations = {r['station_id']: r for r in csv.DictReader(open(data / 'stations.csv'))}
orders = {r['order_id']: r for r in csv.DictReader(open(data / 'orders.csv'))}
station_orders = defaultdict(list)
for order in orders.values():
    station_orders[order['station_id']].append(order)
compartments = defaultdict(dict)
for row in csv.DictReader(open(data / 'compartments.csv')):
    compartments[row['vehicle_id']][row['compartment_id']] = row
slots = {float(row['start_min']): row for row in csv.DictReader(open(data / 'loading_slots.csv'))}
bays = {row['bay_id']: row for row in csv.DictReader(open(data / 'loading_bays.csv'))}
rest_stops = {row['node_id']: row for row in csv.DictReader(open(data / 'rest_stops.csv'))}
risk = {
    (row['from_zone'], row['to_zone']): (
        float(row['time_factor']),
        float(row['hazmat_surcharge_factor']),
    )
    for row in csv.DictReader(open(data / 'risk_zones.csv'))
}
adjustments = {}
for row in csv.DictReader(open(data / 'road_adjustments.csv')):
    adjustments[(row['from_node'], row['to_node'])] = (
        float(row['extra_minutes']),
        float(row['extra_km']),
    )
    adjustments[(row['to_node'], row['from_node'])] = (
        float(row['extra_minutes']),
        float(row['extra_km']),
    )
exceptions = {
    (row['vehicle_id'], row['station_id']): row['rule']
    for row in csv.DictReader(open(data / 'vehicle_station_exceptions.csv'))
}
pairing_rules = list(csv.DictReader(open(data / 'station_product_rules.csv')))
cleaning = {
    (row['from_hose_family'], row['to_hose_family']): (
        float(row['cleanout_min']),
        float(row['cleanout_cost']),
    )
    for row in csv.DictReader(open(data / 'terminal_cleaning_rules.csv'))
}
group_rank = {'M': 0, 'R': 1, 'F': 2}
radius_km = 6371.0
deg = math.pi / 180.0
nodes = {
    'DEPOT': {
        'lat': float(config['terminal_lat']),
        'lon': float(config['terminal_lon']),
        'risk_zone': 'industrial',
    }
}
for station_id, station in stations.items():
    nodes[station_id] = {
        'lat': float(station['lat']),
        'lon': float(station['lon']),
        'risk_zone': station['risk_zone'],
    }
for node_id, stop in rest_stops.items():
    nodes[node_id] = {
        'lat': float(stop['lat']),
        'lon': float(stop['lon']),
        'risk_zone': stop['risk_zone'],
    }
def haversine(origin_id, dest_id):
    lat1 = nodes[origin_id]['lat'] * deg
    lon1 = nodes[origin_id]['lon'] * deg
    lat2 = nodes[dest_id]['lat'] * deg
    lon2 = nodes[dest_id]['lon'] * deg
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 2.0 * radius_km * math.asin(math.sqrt(a))
def travel(origin_id, dest_id):
    if origin_id == dest_id:
        return (0.0, 0.0, 0.0)
    base_km = haversine(origin_id, dest_id)
    time_factor, haz_factor = risk[(nodes[origin_id]['risk_zone'], nodes[dest_id]['risk_zone'])]
    extra_min, extra_km = adjustments.get((origin_id, dest_id), (0.0, 0.0))
    return (
        base_km / float(config['base_speed_kmph']) * 60.0 * time_factor + extra_min,
        base_km + extra_km,
        haz_factor,
    )
errors = []
delivered = defaultdict(float)
order_visits = defaultdict(set)
slot_usage = defaultdict(int)
vehicle_trips = defaultdict(list)
bay_usage = defaultdict(list)
for trip in solution['trips']:
    vehicle_id = trip['vehicle_id']
    bay_id = trip['bay_id']
    load_start = float(trip['load_start_min'])
    if load_start not in slots:
        errors.append(f'Invalid slot start {load_start}')
    slot_usage[load_start] += 1
    stop_nodes = {idx + 1: stop['node_id'] for idx, stop in enumerate(trip['stops'])}
    stop_deliveries = defaultdict(list)
    compartment_load = defaultdict(float)
    compartment_orders = defaultdict(set)
    families = set()
    for delivery in trip['deliveries']:
        order = orders[delivery['order_id']]
        stop_seq = int(delivery['stop_sequence'])
        qty = float(delivery['quantity_liters'])
        compartment_id = delivery['compartment_id']
        delivered[delivery['order_id']] += qty
        order_visits[delivery['order_id']].add((trip['trip_id'], stop_seq))
        stop_deliveries[stop_seq].append(delivery)
        compartment_load[compartment_id] += qty
        compartment_orders[compartment_id].add(delivery['order_id'])
        families.add(products[order['product_id']]['hose_family'])
        if stop_seq not in stop_nodes or stop_nodes[stop_seq] != order['station_id']:
            errors.append(f"Delivery mapping error {trip['trip_id']} {delivery['order_id']}")
        if compartment_id not in compartments[vehicle_id]:
            errors.append(f"Foreign compartment {trip['trip_id']} {compartment_id}")
        elif order['product_id'] not in compartments[vehicle_id][compartment_id]['compatible_products'].split('|'):
            errors.append(f"Compartment incompatibility {trip['trip_id']} {compartment_id}")
    bay_families = set(bays[bay_id]['compatible_hose_families'].split('|'))
    if not families.issubset(bay_families):
        errors.append(f'Bay family mismatch {trip["trip_id"]}')
    liters = 0.0
    weight = 0.0
    last_group = -1
    current_time = load_start + float(config['loading_duration_min'])
    prev_node = 'DEPOT'
    for seq, stop in enumerate(trip['stops'], start=1):
        node_id = stop['node_id']
        arrival = float(stop['arrival_min'])
        departure = float(stop['departure_min'])
        expected_arrival = current_time + travel(prev_node, node_id)[0]
        if abs(arrival - expected_arrival) > 1e-5:
            errors.append(f'Arrival propagation mismatch {trip["trip_id"]} {seq}')
        station = stations[node_id]
        if arrival < float(station['earliest_min']) - 1e-6:
            errors.append(f'Early arrival {trip["trip_id"]} {node_id}')
        if arrival > float(station['latest_min']) + 1e-6:
            errors.append(f'Late arrival {trip["trip_id"]} {node_id}')
        exception = exceptions.get((vehicle_id, node_id))
        access_ok = exception == 'allowed' or (
            exception != 'blocked'
            and station['access_class'] in vehicles[vehicle_id]['allowed_station_classes'].split('|')
        )
        if not access_ok:
            errors.append(f'Access violation {trip["trip_id"]} {node_id}')
        liters_here = 0.0
        groups_here = []
        for delivery in stop_deliveries.get(seq, []):
            order = orders[delivery['order_id']]
            qty = float(delivery['quantity_liters'])
            liters += qty
            liters_here += qty
            weight += qty * float(products[order['product_id']]['density_kg_per_liter'])
            groups_here.append(group_rank[compartments[vehicle_id][delivery['compartment_id']]['group']])
        if groups_here:
            if min(groups_here) < last_group:
                errors.append(f'Precedence violation {trip["trip_id"]}')
            last_group = max(last_group, max(groups_here))
        service_time = float(station['fixed_service_min']) + liters_here / float(config['service_liters_per_min'])
        if abs(departure - (arrival + service_time)) > 1e-5:
            errors.append(f'Departure propagation mismatch {trip["trip_id"]} {seq}')
        current_time = departure
        prev_node = node_id
    for compartment_id, qty in compartment_load.items():
        if compartment_id in compartments[vehicle_id]:
            if len(compartment_orders[compartment_id]) > 1:
                errors.append('Compartment multi-order violation %s %s' % (trip['trip_id'], compartment_id))
            if qty > float(compartments[vehicle_id][compartment_id]['capacity_liters']) + 1e-6:
                errors.append(f'Compartment capacity violation {trip["trip_id"]} {compartment_id}')
    if liters > float(vehicles[vehicle_id]['max_volume_liters']) + 1e-6:
        errors.append(f'Volume limit {trip["trip_id"]}')
    if weight > float(vehicles[vehicle_id]['max_payload_kg']) + 1e-6:
        errors.append(f'Payload limit {trip["trip_id"]}')
    return_time = current_time + travel(prev_node, 'DEPOT')[0]
    if return_time > float(config['shift_end_min']) + 1e-6:
        errors.append(f'Shift end violation {trip["trip_id"]}')
    if return_time - load_start > float(config['max_trip_elapsed_min']) + 1e-6:
        errors.append(f'Trip elapsed violation {trip["trip_id"]}')
    vehicle_trips[vehicle_id].append((load_start, return_time, trip['trip_id']))
    bay_usage[bay_id].append((load_start, trip['trip_id'], tuple(sorted(families))))
for start_time, slot in slots.items():
    if slot_usage[start_time] > int(slot['rack_capacity']):
        errors.append(f"Slot capacity violation {slot['slot_id']}")
for vehicle_id, trips in vehicle_trips.items():
    trips.sort()
    if len(trips) > int(vehicles[vehicle_id]['max_trips']):
        errors.append(f'Max trips violation {vehicle_id}')
    if trips and trips[-1][1] - trips[0][0] > float(vehicles[vehicle_id]['work_limit_min']) + 1e-6:
        errors.append(f'Work limit violation {vehicle_id}')
    for idx in range(1, len(trips)):
        if trips[idx][0] < trips[idx - 1][1] + float(config['turnaround_min']) - 1e-6:
            errors.append(f'Turnaround violation {vehicle_id}')
for bay_id, items in bay_usage.items():
    items.sort()
    for idx in range(1, len(items)):
        prev_start, prev_trip_id, prev_families = items[idx - 1]
        curr_start, curr_trip_id, curr_families = items[idx]
        clean_time = min(cleaning[(family_a, family_b)][0] for family_a in prev_families for family_b in curr_families)
        if curr_start < prev_start + float(config['loading_duration_min']) + clean_time - 1e-6:
            errors.append(f'Bay cleanout violation {bay_id} {prev_trip_id}->{curr_trip_id}')
for order_id, order in orders.items():
    qty = delivered[order_id]
    if len(order_visits[order_id]) != 1:
        errors.append(f'Order visit count {order_id}')
    if qty < float(order['min_liters']) - 1e-6:
        errors.append(f'Order minimum {order_id}')
    if qty > float(order['demand_liters']) + 1e-6:
        errors.append(f'Order maximum {order_id}')
for station_id, station in stations.items():
    visits = set()
    for order in station_orders[station_id]:
        visits |= order_visits[order['order_id']]
    if station['split_allowed'] == '0' and len(visits) > 1:
        errors.append(f'Station split {station_id}')
for rule in pairing_rules:
    if rule['rule'] != 'same_trip_required':
        continue
    order_a = next(order['order_id'] for order in station_orders[rule['station_id']] if order['product_id'] == rule['product_a'])
    order_b = next(order['order_id'] for order in station_orders[rule['station_id']] if order['product_id'] == rule['product_b'])
    if order_visits[order_a] != order_visits[order_b]:
        errors.append(f'Pairing rule {rule["station_id"]}')
print(json.dumps({
    'feasible': not errors,
    'error_count': len(errors),
    'errors': errors[:20],
    'trip_count': len(solution['trips']),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", audit_code.replace("__SOLUTION_PATH__", repr(str(solution_path)))],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
    )
    return json.loads(result.stdout)


def write_solution(solution, path=SOLUTION_PATH):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(solution, handle, indent=2)
        handle.write("\n")


def write_log(lines):
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def solve():
    start_wall = time.time()
    solve_limit = min(float(os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS", "300")), 300.0)
    instance = load_instance()

    log_lines = [
        "# Solve Log",
        "",
        "## Command",
        "",
        "`PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Preprocessing",
        "",
    ]

    station_patterns = build_station_patterns(instance)
    pattern_count = sum(len(v) for v in station_patterns.values())
    routes = build_route_patterns(instance, station_patterns)
    add_route_metrics(instance, routes)
    candidates = build_candidates(instance, routes)

    log_lines.extend(
        [
            f"- Station patterns: {pattern_count}",
            f"- Route patterns: {len(routes)}",
            f"- Slot-specific candidates: {len(candidates)}",
            f"- Restoration bundles: {len(RESTORATION_BUNDLES)}",
            "- Restoration operator: exact repair MIP on a fixed public-data bundle basis, then exact bay assignment.",
            "",
            "## Solve Attempts",
            "",
        ]
    )

    if not candidates:
        raise RuntimeError("No feasible candidates were generated")

    incumbent_info = None
    if SOLUTION_PATH.exists():
        incumbent_solution = read_json(SOLUTION_PATH)
        incumbent_validation = validate_solution(instance, incumbent_solution)
        incumbent_audit = run_independent_public_audit(SOLUTION_PATH)
        incumbent_info = {
            "solution": incumbent_solution,
            "validation": incumbent_validation,
            "audit": incumbent_audit,
            "objective": incumbent_validation["total_objective"],
            "feasible": incumbent_validation["feasible"] and incumbent_audit["feasible"],
        }
        log_lines.extend(
            [
                "### Incumbent Audit",
                f"- Incumbent objective: {incumbent_info['objective']}",
                f"- Internal validation feasible: `{incumbent_validation['feasible']}`",
                f"- Independent audit feasible: `{incumbent_audit['feasible']}`",
                f"- Independent audit errors: {incumbent_audit['error_count']}",
                "",
            ]
        )

    elapsed = time.time() - start_wall
    remaining = max(10.0, solve_limit - elapsed)
    assignment_time_limit = max(5.0, min(remaining - 2.0, solve_limit))
    assignment_result = solve_restoration_assignment(
        instance,
        candidates,
        RESTORATION_BUNDLES,
        assignment_time_limit,
    )
    if assignment_result is None:
        raise RuntimeError("Restoration bundle candidate set is empty")

    log_lines.extend(
        [
            "### Restricted Exact Repair",
            f"- Candidate universe: {assignment_result['candidate_count']}",
            f"- Assignment status: `{assignment_result['status']}`",
            f"- Assignment objective: {assignment_result['objective']}",
            f"- Assignment dual bound: {assignment_result['dual_bound']}",
            f"- Assignment gap: {assignment_result['gap']}",
        ]
    )

    repaired_selected_candidates = assignment_result["selected_candidates"]
    if not repaired_selected_candidates:
        raise RuntimeError("Restricted repair MIP found no incumbent")

    bay_elapsed = time.time() - start_wall
    bay_remaining = max(5.0, solve_limit - bay_elapsed)
    repaired_bay_solution = solve_bay_assignment(
        instance, repaired_selected_candidates, bay_remaining
    )
    log_lines.extend(
        [
            f"- Selected trips: {len(repaired_selected_candidates)}",
            f"- Bay status: `{None if repaired_bay_solution is None else repaired_bay_solution['status']}`",
            f"- Bay objective: {None if repaired_bay_solution is None else repaired_bay_solution['objective']}",
            "",
        ]
    )
    if repaired_bay_solution is None:
        raise RuntimeError("Restricted repair solution is not bay-feasible")

    repaired_solution = compose_solution(repaired_selected_candidates, repaired_bay_solution)
    repaired_validation = validate_solution(instance, repaired_solution)
    if not repaired_validation["feasible"]:
        raise RuntimeError("Repair validation failed: " + "; ".join(repaired_validation["errors"]))
    repaired_candidate_path = SUBMISSIONS_DIR / "solution.repair_candidate.json"
    write_solution(repaired_solution, repaired_candidate_path)
    repaired_audit = run_independent_public_audit(repaired_candidate_path)
    if not repaired_audit["feasible"]:
        raise RuntimeError(
            "Repair independent audit failed: " + "; ".join(repaired_audit["errors"])
        )

    performance_result = {
        "status": "not_run",
        "objective": None,
        "dual_bound": None,
        "gap": None,
        "selected_candidates": None,
        "bay_solution": None,
        "validation": None,
        "audit": None,
    }
    perf_elapsed = time.time() - start_wall
    perf_remaining = solve_limit - perf_elapsed
    if perf_remaining >= 15.0:
        performance_result = attempt_local_branching_improvement(
            instance,
            candidates,
            repaired_selected_candidates,
            repaired_validation["total_objective"],
            min(30.0, perf_remaining - 5.0),
            radius=4,
        )
        if (
            performance_result["selected_candidates"] is not None
            and performance_result["bay_solution"] is not None
        ):
            performance_solution = compose_solution(
                performance_result["selected_candidates"],
                performance_result["bay_solution"],
            )
            performance_validation = validate_solution(instance, performance_solution)
            performance_result["validation"] = performance_validation
            if performance_validation["feasible"]:
                performance_candidate_path = SUBMISSIONS_DIR / "solution.performance_candidate.json"
                write_solution(performance_solution, performance_candidate_path)
                performance_result["audit"] = run_independent_public_audit(
                    performance_candidate_path
                )

    published_label = "repaired_candidate"
    published_solution = repaired_solution
    published_validation = repaired_validation
    published_audit = repaired_audit
    published_objective = repaired_validation["total_objective"]

    if incumbent_info and incumbent_info["feasible"]:
        published_label = "incumbent"
        published_solution = incumbent_info["solution"]
        published_validation = incumbent_info["validation"]
        published_audit = incumbent_info["audit"]
        published_objective = incumbent_info["objective"]
        if repaired_validation["total_objective"] < published_objective - 1e-6:
            published_label = "repaired_candidate"
            published_solution = repaired_solution
            published_validation = repaired_validation
            published_audit = repaired_audit
            published_objective = repaired_validation["total_objective"]

    if (
        performance_result["validation"] is not None
        and performance_result["audit"] is not None
        and performance_result["validation"]["feasible"]
        and performance_result["audit"]["feasible"]
        and performance_result["validation"]["total_objective"] < published_objective - 1e-6
    ):
        performance_solution = compose_solution(
            performance_result["selected_candidates"],
            performance_result["bay_solution"],
        )
        published_label = "performance_candidate"
        published_solution = performance_solution
        published_validation = performance_result["validation"]
        published_audit = performance_result["audit"]
        published_objective = performance_result["validation"]["total_objective"]

    performance_improved = (
        performance_result["validation"] is not None
        and performance_result["audit"] is not None
        and performance_result["validation"]["feasible"]
        and performance_result["audit"]["feasible"]
        and performance_result["validation"]["total_objective"]
        < repaired_validation["total_objective"] - 1e-6
    )

    write_solution(published_solution, SOLUTION_PATH)

    log_lines.extend(
        [
            "## Repair Audit",
            "",
            f"- Repair objective: {repaired_validation['total_objective']}",
            f"- Repair internal feasible: `{repaired_validation['feasible']}`",
            f"- Repair independent feasible: `{repaired_audit['feasible']}`",
            f"- Repair independent errors: {repaired_audit['error_count']}",
            "",
            "## Performance Attempt",
            "",
            f"- Status: `{performance_result['status']}`",
            f"- Candidate objective: {performance_result['objective']}",
            f"- Dual bound: {performance_result['dual_bound']}",
            f"- Gap: {performance_result['gap']}",
            f"- Bay feasible: `{performance_result['bay_solution'] is not None}`",
            f"- Audit feasible: `{None if performance_result['audit'] is None else performance_result['audit']['feasible']}`",
            f"- Strict improvement over repaired incumbent: `{performance_improved}`",
            "",
            "## Published Artifact",
            "",
            f"- Source: `{published_label}`",
            f"- Published objective: {published_objective}",
            "",
            "## Validation",
            "",
            f"- Feasible: `{published_validation['feasible']}`",
            f"- Total objective: {published_validation['total_objective']}",
            f"- Fixed trip cost: {published_validation['objective_components']['fixed_trip_cost']}",
            f"- Distance cost: {published_validation['objective_components']['distance_cost']}",
            f"- Hazmat cost: {published_validation['objective_components']['hazmat_cost']}",
            f"- Lateness cost: {published_validation['objective_components']['lateness_cost']}",
            f"- Slot cost: {published_validation['objective_components']['slot_cost']}",
            f"- Bay cost: {published_validation['objective_components']['bay_cost']}",
            f"- Cleanout cost: {published_validation['objective_components']['cleanout_cost']}",
            f"- Shortfill cost: {published_validation['objective_components']['shortfill_cost']}",
            "- Checks: schema, slot capacity, vehicle turnaround, work limits, access, compartment single-order rule, compartment capacity, product compatibility, precedence, time propagation, bay cleanout, station split rules, pairing rules, exact order coverage bounds.",
            "",
            "## Independent Audit",
            "",
            "- Command: standalone `PYTHONPATH=/opt/or-deps python` replay over `/app/data/*` and the exported solution artifact.",
            f"- Feasible: `{published_audit['feasible']}`",
            f"- Error count: {published_audit['error_count']}",
            f"- Trip count: {published_audit['trip_count']}",
        ]
    )
    write_log(log_lines)


if __name__ == "__main__":
    solve()
