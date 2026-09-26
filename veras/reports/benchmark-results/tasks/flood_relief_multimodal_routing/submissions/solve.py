import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from heapq import heappop, heappush
from itertools import combinations
from pathlib import Path

from pyscipopt import Model, quicksum


BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
SUBMISSIONS_DIR = BASE_DIR / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def load_instance():
    with open(DATA_DIR / "config.json") as handle:
        config = json.load(handle)

    staging_rows = read_csv(DATA_DIR / "staging_points.csv")
    road_rows = read_csv(DATA_DIR / "road_edges.csv")
    shelter_rows = read_csv(DATA_DIR / "shelters.csv")
    drone_rows = read_csv(DATA_DIR / "drone_options.csv")
    boat_rows = read_csv(DATA_DIR / "boat_travel.csv")
    priority_rows = read_csv(DATA_DIR / "priority_weights.csv")
    weather_rows = read_csv(DATA_DIR / "weather_windows.csv")
    access_rows = read_csv(DATA_DIR / "access_notes.csv")

    staging = {}
    for row in staging_rows:
        staging[row["node_id"]] = {
            "node_id": row["node_id"],
            "launch_clearance": row["launch_clearance"],
            "boat_ramp": row["boat_ramp"] == "1",
            "handling_minutes": float(row["handling_minutes"]),
        }

    priority_weights = {
        row["priority"]: float(row["weight"]) for row in priority_rows
    }

    shelters = {}
    dry_by_node = defaultdict(list)
    flooded = []
    for row in shelter_rows:
        shelters[row["shelter_id"]] = {
            "shelter_id": row["shelter_id"],
            "area_type": row["area_type"],
            "nearest_dry_node": row["nearest_dry_node"],
            "demand_kg": float(row["demand_kg"]),
            "priority": row["priority"],
            "deadline": float(row["deadline_minutes"]),
            "weight": priority_weights[row["priority"]],
        }
        if row["area_type"] == "dry":
            dry_by_node[row["nearest_dry_node"]].append(row["shelter_id"])
        else:
            flooded.append(row["shelter_id"])

    road_adj = defaultdict(dict)
    road_edges = set()
    for row in road_rows:
        a = row["from_node"]
        b = row["to_node"]
        t = float(row["travel_minutes"])
        road_adj[a][b] = t
        road_adj[b][a] = t
        road_edges.add(frozenset((a, b)))

    boat_adj = defaultdict(dict)
    boat_edges = set()
    for row in boat_rows:
        a = row["from_id"]
        b = row["to_id"]
        t = float(row["minutes"])
        boat_adj[a][b] = t
        boat_adj[b][a] = t
        boat_edges.add(frozenset((a, b)))

    periods = []
    for idx, row in enumerate(sorted(weather_rows, key=lambda r: float(r["start_minute"]))):
        periods.append(
            {
                "id": row["period_id"],
                "start": float(row["start_minute"]),
                "end": float(row["end_minute"]),
                "drone_mult": float(row["drone_multiplier"]),
                "boat_mult": float(row["boat_multiplier"]),
                "is_last": idx == len(weather_rows) - 1,
            }
        )

    drone_options_by_node = defaultdict(dict)
    for row in drone_rows:
        node = row["launch_node"]
        shelter = row["shelter_id"]
        if row["payload_feasible"] != "1":
            continue
        if staging[node]["launch_clearance"] == "poor":
            continue
        flight_minutes = float(row["flight_minutes"])
        if flight_minutes > float(config["drone_endurance_minutes"]) + 1e-9:
            continue
        drone_options_by_node[node][shelter] = {
            "flight_minutes": flight_minutes,
            "landing_risk": int(row["landing_risk"]),
        }

    ramp_nodes = sorted(
        node_id for node_id, row in staging.items() if row["boat_ramp"]
    )
    relevant_nodes = sorted(
        set(dry_by_node)
        | set(drone_options_by_node)
        | set(ramp_nodes)
    )

    return {
        "config": config,
        "staging": staging,
        "shelters": shelters,
        "dry_by_node": {k: sorted(v) for k, v in dry_by_node.items()},
        "flooded": sorted(flooded),
        "road_adj": road_adj,
        "road_edges": road_edges,
        "boat_adj": boat_adj,
        "boat_edges": boat_edges,
        "drone_options_by_node": {
            k: dict(sorted(v.items())) for k, v in drone_options_by_node.items()
        },
        "periods": periods,
        "ramp_nodes": ramp_nodes,
        "relevant_nodes": relevant_nodes,
        "access_notes": access_rows,
    }


def period_for_time(periods, t):
    for period in periods:
        if period["is_last"]:
            if t + 1e-9 >= period["start"]:
                return period
        elif period["start"] - 1e-9 <= t < period["end"] - 1e-9:
            return period
    return periods[-1]


def shortest_path_pair(source, target, road_adj, relevant_set):
    heap = [(0.0, 0, source)]
    best = {source: (0.0, 0)}
    prev = {}
    while heap:
        travel, internal_hits, node = heappop(heap)
        if (travel, internal_hits) != best[node]:
            continue
        if node == target:
            break
        for nxt, edge_time in sorted(road_adj[node].items()):
            new_travel = travel + edge_time
            add_internal = 1 if nxt in relevant_set and nxt not in (source, target) else 0
            new_internal = internal_hits + add_internal
            cand = (new_travel, new_internal)
            if nxt not in best or cand < best[nxt]:
                best[nxt] = cand
                prev[nxt] = node
                heappush(heap, (new_travel, new_internal, nxt))
    if target not in best:
        raise RuntimeError(f"No road path between {source} and {target}")
    path = [target]
    cur = target
    while cur != source:
        cur = prev[cur]
        path.append(cur)
    path.reverse()
    return best[target][0], path


def build_shortest_paths(instance):
    relevant_plus_depot = [instance["config"]["depot_id"]] + instance["relevant_nodes"]
    relevant_set = set(instance["relevant_nodes"])
    travel = {}
    path = {}
    for i in relevant_plus_depot:
        for j in relevant_plus_depot:
            if i == j:
                continue
            dist, route = shortest_path_pair(i, j, instance["road_adj"], relevant_set)
            travel[(i, j)] = dist
            path[(i, j)] = route
    return travel, path


def generate_boat_trips(instance):
    config = instance["config"]
    shelters = instance["shelters"]
    boat_adj = instance["boat_adj"]
    max_boat_mult = max(period["boat_mult"] for period in instance["periods"])
    trips_by_node = defaultdict(list)

    for ramp in instance["ramp_nodes"]:
        trip_index = 0

        def dfs(path, load, water_without_return):
            nonlocal trip_index
            last = path[-1] if path else ramp
            if path and ramp in boat_adj[last]:
                total_water = water_without_return + boat_adj[last][ramp]
                worst_duration = (
                    float(config["boat_setup_minutes"])
                    + max_boat_mult * total_water
                    + float(config["boat_unload_minutes_per_shelter"]) * len(path)
                )
                if (
                    load <= float(config["boat_capacity_kg"]) + 1e-9
                    and worst_duration <= float(config["boat_trip_limit_minutes"]) + 1e-9
                ):
                    prefix_water = []
                    running = 0.0
                    prev_node = ramp
                    for shelter_id in path:
                        running += boat_adj[prev_node][shelter_id]
                        prefix_water.append(running)
                        prev_node = shelter_id
                    trips_by_node[ramp].append(
                        {
                            "trip_key": f"{ramp}_trip_{trip_index}",
                            "launch_node": ramp,
                            "shelters": tuple(path),
                            "load": load,
                            "total_water": total_water,
                            "prefix_water": tuple(prefix_water),
                        }
                    )
                    trip_index += 1

            for nxt, edge_time in sorted(boat_adj[last].items()):
                if not nxt.startswith("H"):
                    continue
                if nxt in path:
                    continue
                new_load = load + shelters[nxt]["demand_kg"]
                if new_load > float(config["boat_capacity_kg"]) + 1e-9:
                    continue
                if ramp not in boat_adj[nxt]:
                    continue
                new_water = water_without_return + edge_time
                optimistic = (
                    float(config["boat_setup_minutes"])
                    + max_boat_mult * (new_water + boat_adj[nxt][ramp])
                    + float(config["boat_unload_minutes_per_shelter"]) * (len(path) + 1)
                )
                if optimistic > float(config["boat_trip_limit_minutes"]) + 1e-9:
                    continue
                dfs(path + [nxt], new_load, new_water)

        dfs([], 0.0, 0.0)

    return dict(trips_by_node)


def powerset_nonempty(items):
    items = list(items)
    for r in range(1, len(items) + 1):
        for subset in combinations(items, r):
            yield subset


def build_patterns(instance, trips_by_node):
    config = instance["config"]
    staging = instance["staging"]
    shelters = instance["shelters"]
    drone_options_by_node = instance["drone_options_by_node"]
    periods = instance["periods"]
    dry_by_node = instance["dry_by_node"]

    patterns_by_node = defaultdict(list)
    pattern_lookup = {}

    for node in instance["relevant_nodes"]:
        mandatory_dry = tuple(dry_by_node.get(node, []))
        mandatory_dry_demand = sum(shelters[h]["demand_kg"] for h in mandatory_dry)
        direct_time = float(config["truck_service_minutes_per_kg"]) * mandatory_dry_demand
        base_time = float(config["truck_fixed_stop_minutes"]) + staging[node]["handling_minutes"]
        drone_shelters = sorted(drone_options_by_node.get(node, {}))
        boat_trips = trips_by_node.get(node, [])

        subset_iter = [tuple()]
        if drone_shelters:
            if mandatory_dry:
                subset_iter = [tuple()] + list(powerset_nonempty(drone_shelters))
            else:
                subset_iter = list(powerset_nonempty(drone_shelters))

        for subset in subset_iter:
            if not mandatory_dry and not subset:
                continue
            pattern_id = f"{node}_drv_{len(patterns_by_node[node])}"
            period_data = {}
            for period in periods:
                drone_duration = 0.0
                for shelter_id in subset:
                    option = drone_options_by_node[node][shelter_id]
                    drone_duration += (
                        float(config["drone_setup_minutes"])
                        + period["drone_mult"] * option["flight_minutes"]
                        + float(config["risk_penalty_minutes_per_point"]) * option["landing_risk"]
                    )
                period_data[period["id"]] = {
                    "duration": base_time + direct_time + drone_duration,
                    "boat_offsets": {},
                }
            pattern = {
                "pattern_id": pattern_id,
                "node": node,
                "kind": "truck_drone",
                "mandatory_dry": mandatory_dry,
                "drone_shelters": tuple(subset),
                "flooded_covered": tuple(sorted(subset)),
                "boat_trip": None,
                "period_data": period_data,
            }
            patterns_by_node[node].append(pattern)
            pattern_lookup[pattern_id] = pattern

        for trip in boat_trips:
            pattern_id = f"{node}_boat_{trip['trip_key']}"
            period_data = {}
            for period in periods:
                boat_offsets = {}
                for idx, shelter_id in enumerate(trip["shelters"], start=1):
                    boat_offsets[shelter_id] = (
                        base_time
                        + float(config["boat_setup_minutes"])
                        + period["boat_mult"] * trip["prefix_water"][idx - 1]
                        + float(config["boat_unload_minutes_per_shelter"]) * idx
                    )
                period_data[period["id"]] = {
                    "duration": (
                        base_time
                        + float(config["boat_setup_minutes"])
                        + period["boat_mult"] * trip["total_water"]
                        + float(config["boat_unload_minutes_per_shelter"]) * len(trip["shelters"])
                    ),
                    "boat_offsets": boat_offsets,
                }
            pattern = {
                "pattern_id": pattern_id,
                "node": node,
                "kind": "boat",
                "mandatory_dry": mandatory_dry,
                "drone_shelters": tuple(),
                "flooded_covered": trip["shelters"],
                "boat_trip": trip,
                "period_data": period_data,
            }
            patterns_by_node[node].append(pattern)
            pattern_lookup[pattern_id] = pattern

    return dict(patterns_by_node), pattern_lookup


def pattern_mask(pattern, shelter_index):
    mask = 0
    for shelter_id in pattern["flooded_covered"]:
        mask |= 1 << shelter_index[shelter_id]
    return mask


def choose_patterns_dp(instance, patterns_by_node):
    flooded = instance["flooded"]
    shelter_index = {shelter_id: idx for idx, shelter_id in enumerate(flooded)}
    full_mask = (1 << len(flooded)) - 1
    mandatory_nodes = set(instance["dry_by_node"])
    nodes = instance["relevant_nodes"]

    pattern_masks = {}
    pattern_cost = {}
    for node in nodes:
        for pattern in patterns_by_node[node]:
            pattern_masks[(node, pattern["pattern_id"])] = pattern_mask(pattern, shelter_index)
            min_duration = min(
                data["duration"] for data in pattern["period_data"].values()
            )
            optional_penalty = 18.0 if node not in mandatory_nodes else 0.0
            pattern_cost[(node, pattern["pattern_id"])] = optional_penalty + min_duration

    dp = {0: (0.0, {})}
    for node in nodes:
        next_dp = {}
        options = list(patterns_by_node[node])
        if node not in mandatory_nodes:
            options = [None] + options
        for mask, (cost, chosen) in dp.items():
            for pattern in options:
                if pattern is None:
                    new_mask = mask
                    new_cost = cost
                    new_choice = chosen
                else:
                    p_mask = pattern_masks[(node, pattern["pattern_id"])]
                    if mask & p_mask:
                        continue
                    new_mask = mask | p_mask
                    new_cost = cost + pattern_cost[(node, pattern["pattern_id"])]
                    new_choice = dict(chosen)
                    new_choice[node] = pattern
                old = next_dp.get(new_mask)
                if old is None or new_cost < old[0] - 1e-9:
                    next_dp[new_mask] = (new_cost, new_choice)
        dp = next_dp

    if full_mask not in dp:
        raise RuntimeError("Pattern DP could not cover all flooded shelters")
    return dp[full_mask][1]


def pattern_candidates_beam(instance, patterns_by_node, beam_width=5):
    flooded = instance["flooded"]
    shelter_index = {shelter_id: idx for idx, shelter_id in enumerate(flooded)}
    full_mask = (1 << len(flooded)) - 1
    mandatory_nodes = set(instance["dry_by_node"])
    states = {0: [(0.0, {})]}

    for node in instance["relevant_nodes"]:
        new_states = {}
        options = list(patterns_by_node[node])
        if node not in mandatory_nodes:
            options = [None] + options
        for mask, entries in states.items():
            for cost, chosen in entries:
                for pattern in options:
                    if pattern is None:
                        new_mask = mask
                        new_cost = cost
                        new_choice = chosen
                    else:
                        p_mask = pattern_mask(pattern, shelter_index)
                        if mask & p_mask:
                            continue
                        new_mask = mask | p_mask
                        base_cost = min(
                            data["duration"] for data in pattern["period_data"].values()
                        )
                        optional_penalty = 18.0 if node not in mandatory_nodes else 0.0
                        new_cost = cost + base_cost + optional_penalty
                        new_choice = dict(chosen)
                        new_choice[node] = pattern
                    new_states.setdefault(new_mask, []).append((new_cost, new_choice))
        states = {}
        for mask, entries in new_states.items():
            entries.sort(key=lambda item: item[0])
            kept = []
            seen = set()
            for cost, chosen in entries:
                key = tuple(sorted((n, p["pattern_id"]) for n, p in chosen.items()))
                if key in seen:
                    continue
                seen.add(key)
                kept.append((cost, chosen))
                if len(kept) >= beam_width:
                    break
            states[mask] = kept

    if full_mask not in states:
        raise RuntimeError("Beam search could not cover all flooded shelters")
    return [chosen for _, chosen in states[full_mask]]


def exact_tsp_travel_order(nodes, depot, travel_time):
    if not nodes:
        return []
    indexed = list(nodes)
    n = len(indexed)
    dp = {}
    parent = {}
    for j in range(n):
        mask = 1 << j
        dp[(mask, j)] = travel_time[(depot, indexed[j])]
        parent[(mask, j)] = None
    for mask in range(1, 1 << n):
        for j in range(n):
            if not (mask & (1 << j)):
                continue
            state = (mask, j)
            if state not in dp:
                continue
            for k in range(n):
                if mask & (1 << k):
                    continue
                next_mask = mask | (1 << k)
                cand = dp[state] + travel_time[(indexed[j], indexed[k])]
                next_state = (next_mask, k)
                if next_state not in dp or cand < dp[next_state] - 1e-9:
                    dp[next_state] = cand
                    parent[next_state] = j
    full = (1 << n) - 1
    best_last = min(
        range(n),
        key=lambda j: dp[(full, j)] + travel_time[(indexed[j], depot)],
    )
    order = []
    mask = full
    last = best_last
    while last is not None:
        order.append(indexed[last])
        prev = parent[(mask, last)]
        mask ^= 1 << last
        last = prev
    order.reverse()
    return order


def simulate_closure_plan(instance, travel_time, selected_patterns, order):
    config = instance["config"]
    periods = instance["periods"]
    shelters = instance["shelters"]
    depot = config["depot_id"]
    current = depot
    current_time = 0.0
    arrival = {}
    departure = {}
    period_by_node = {}
    service_time = {}

    for node in order:
        current_time += travel_time[(current, node)]
        arr = current_time
        arrival[node] = arr
        period = period_for_time(periods, arr)
        period_by_node[node] = period["id"]
        pattern = selected_patterns[node]
        pdata = pattern["period_data"][period["id"]]
        dep = arr + pdata["duration"]
        departure[node] = dep
        for shelter_id in instance["dry_by_node"].get(node, []):
            service_time[shelter_id] = dep
        for shelter_id in pattern["drone_shelters"]:
            service_time[shelter_id] = dep
        if pattern["kind"] == "boat":
            for shelter_id, offset in pdata["boat_offsets"].items():
                service_time[shelter_id] = arr + offset
        current_time = dep
        current = node

    completion = current_time + travel_time[(current, depot)] if order else 0.0
    weighted_lateness = 0.0
    lateness = {}
    for shelter_id, shelter in shelters.items():
        late = max(0.0, service_time[shelter_id] - shelter["deadline"])
        lateness[shelter_id] = late
        weighted_lateness += shelter["weight"] * late
    objective = completion + float(config["late_penalty_per_weighted_minute"]) * weighted_lateness
    return {
        "arrival": arrival,
        "departure": departure,
        "period_by_node": period_by_node,
        "service_time": service_time,
        "lateness": lateness,
        "completion": completion,
        "objective": objective,
    }


def best_route_for_patterns(instance, travel_time, selected_patterns):
    nodes = sorted(selected_patterns)
    if not nodes:
        return []

    def node_deadline(node):
        deadlines = [
            instance["shelters"][h]["deadline"]
            for h in instance["dry_by_node"].get(node, [])
        ]
        deadlines.extend(
            instance["shelters"][h]["deadline"]
            for h in selected_patterns[node]["flooded_covered"]
        )
        return min(deadlines) if deadlines else 10**9

    seeds = []
    tsp_seed = exact_tsp_travel_order(nodes, instance["config"]["depot_id"], travel_time)
    seeds.append(tsp_seed)
    seeds.append(sorted(nodes, key=node_deadline))

    depot = instance["config"]["depot_id"]
    greedy = []
    remaining = set(nodes)
    current = depot
    while remaining:
        nxt = min(
            remaining,
            key=lambda node: (
                node_deadline(node),
                travel_time[(current, node)],
            ),
        )
        greedy.append(nxt)
        remaining.remove(nxt)
        current = nxt
    seeds.append(greedy)

    best_order = None
    best_eval = None
    for seed in seeds:
        eval_seed = simulate_closure_plan(instance, travel_time, selected_patterns, seed)
        if best_eval is None or eval_seed["objective"] < best_eval["objective"] - 1e-9:
            best_order = list(seed)
            best_eval = eval_seed

    improved = True
    while improved:
        improved = False
        current_order = list(best_order)
        n = len(current_order)
        for i in range(n):
            for j in range(i + 1, n):
                cand = current_order[:]
                cand[i], cand[j] = cand[j], cand[i]
                eval_cand = simulate_closure_plan(instance, travel_time, selected_patterns, cand)
                if eval_cand["objective"] < best_eval["objective"] - 1e-9:
                    best_order = cand
                    best_eval = eval_cand
                    improved = True
                    break
            if improved:
                break
        if improved:
            continue
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                cand = current_order[:]
                node = cand.pop(i)
                cand.insert(j, node)
                eval_cand = simulate_closure_plan(instance, travel_time, selected_patterns, cand)
                if eval_cand["objective"] < best_eval["objective"] - 1e-9:
                    best_order = cand
                    best_eval = eval_cand
                    improved = True
                    break
            if improved:
                break

    return best_order, best_eval


def plan_to_rows(instance, selected_patterns, closure_order, travel_path):
    depot = instance["config"]["depot_id"]
    closure_route = [depot] + list(closure_order) + [depot]
    expanded_route = [depot]
    for a, b in zip(closure_route, closure_route[1:]):
        expanded_route.extend(travel_path[(a, b)][1:])

    first_index = {}
    for idx, node in enumerate(expanded_route):
        first_index.setdefault(node, idx)

    activity_nodes = sorted(selected_patterns, key=lambda node: first_index.get(node, 10**9))

    truck_rows = []
    for seq, node in enumerate(expanded_route, start=1):
        truck_rows.append(
            {
                "row_type": "truck_stop",
                "sequence": str(seq),
                "node_id": node,
                "trip_id": "",
                "mode": "",
                "shelter_id": "",
                "launch_node": "",
            }
        )

    delivery_rows = []
    drone_trip_counter = 1
    boat_trip_counter = 1
    for node in activity_nodes:
        pattern = selected_patterns[node]
        for shelter_id in instance["dry_by_node"].get(node, []):
            delivery_rows.append(
                {
                    "row_type": "delivery",
                    "sequence": "",
                    "node_id": node,
                    "trip_id": "",
                    "mode": "truck",
                    "shelter_id": shelter_id,
                    "launch_node": "",
                }
            )
        for shelter_id in sorted(pattern["drone_shelters"]):
            delivery_rows.append(
                {
                    "row_type": "delivery",
                    "sequence": "",
                    "node_id": "",
                    "trip_id": f"DR{drone_trip_counter:02d}",
                    "mode": "drone",
                    "shelter_id": shelter_id,
                    "launch_node": node,
                }
            )
            drone_trip_counter += 1
        if pattern["kind"] == "boat":
            trip_id = f"BT{boat_trip_counter:02d}"
            for seq, shelter_id in enumerate(pattern["boat_trip"]["shelters"], start=1):
                delivery_rows.append(
                    {
                        "row_type": "delivery",
                        "sequence": str(seq),
                        "node_id": "",
                        "trip_id": trip_id,
                        "mode": "boat",
                        "shelter_id": shelter_id,
                        "launch_node": node,
                    }
                )
            boat_trip_counter += 1

    return {
        "closure_route": closure_route,
        "expanded_route": expanded_route,
        "rows": truck_rows + delivery_rows,
    }


def parse_rows_to_pattern_solution(instance, patterns_by_node, rows):
    truck_rows = sorted(
        [row for row in rows if row["row_type"] == "truck_stop"],
        key=lambda r: int(r["sequence"]),
    )
    route = [row["node_id"] for row in truck_rows]
    first_index = {}
    for idx, node in enumerate(route):
        first_index.setdefault(node, idx)

    truck_deliveries = defaultdict(list)
    drone_deliveries = defaultdict(list)
    boat_by_launch = {}

    for row in rows:
        if row["row_type"] != "delivery":
            continue
        if row["mode"] == "truck":
            truck_deliveries[row["node_id"]].append(row["shelter_id"])
        elif row["mode"] == "drone":
            drone_deliveries[row["launch_node"]].append(row["shelter_id"])
        elif row["mode"] == "boat":
            boat_by_launch.setdefault(row["launch_node"], []).append(row)

    selected_patterns = {}
    activity_nodes = set(truck_deliveries) | set(drone_deliveries) | set(boat_by_launch)
    for node in activity_nodes:
        truck_set = tuple(sorted(truck_deliveries.get(node, [])))
        if truck_set != tuple(instance["dry_by_node"].get(node, [])):
            raise RuntimeError(f"Rows at {node} do not match mandatory dry deliveries")

        boat_rows = boat_by_launch.get(node, [])
        if boat_rows:
            boat_rows.sort(key=lambda r: int(r["sequence"]))
            ordered = tuple(row["shelter_id"] for row in boat_rows)
            match = None
            for pattern in patterns_by_node[node]:
                if pattern["kind"] == "boat" and pattern["boat_trip"]["shelters"] == ordered:
                    match = pattern
                    break
            if match is None:
                raise RuntimeError(f"Could not map boat trip at {node} back to a pattern")
            selected_patterns[node] = match
        else:
            drone_set = tuple(sorted(drone_deliveries.get(node, [])))
            match = None
            for pattern in patterns_by_node[node]:
                if pattern["kind"] == "truck_drone" and tuple(sorted(pattern["drone_shelters"])) == drone_set:
                    match = pattern
                    break
            if match is None:
                raise RuntimeError(f"Could not map truck/drone activity at {node} back to a pattern")
            selected_patterns[node] = match

    closure_order = sorted(activity_nodes, key=lambda node: first_index[node])
    return selected_patterns, closure_order


def best_no_repeat_route_for_patterns(instance, selected_patterns):
    active_nodes = sorted(selected_patterns)
    active_set = set(active_nodes)
    depot = instance["config"]["depot_id"]

    if not active_nodes:
        return [], {"objective": 0.0, "route": [depot, depot]}

    cache = {}

    def restricted_path(source, target):
        key = (source, target)
        if key in cache:
            return cache[key]
        banned = active_set - {source, target}
        heap = [(0.0, source)]
        best = {source: 0.0}
        prev = {}
        while heap:
            dist, node = heappop(heap)
            if dist != best[node]:
                continue
            if node == target:
                break
            for nxt, edge_time in sorted(instance["road_adj"][node].items()):
                if nxt in banned:
                    continue
                new_dist = dist + edge_time
                if nxt not in best or new_dist < best[nxt] - 1e-9:
                    best[nxt] = new_dist
                    prev[nxt] = node
                    heappush(heap, (new_dist, nxt))
        if target not in best:
            cache[key] = (None, None)
            return cache[key]
        path = [target]
        cur = target
        while cur != source:
            cur = prev[cur]
            path.append(cur)
        path.reverse()
        cache[key] = (best[target], path)
        return cache[key]

    n = len(active_nodes)
    dp = {}
    parent = {}
    for j, node in enumerate(active_nodes):
        dist, _ = restricted_path(depot, node)
        if dist is not None:
            dp[(1 << j, j)] = dist
            parent[(1 << j, j)] = None

    for mask in range(1, 1 << n):
        for j in range(n):
            if (mask, j) not in dp:
                continue
            for k in range(n):
                if mask & (1 << k):
                    continue
                dist, _ = restricted_path(active_nodes[j], active_nodes[k])
                if dist is None:
                    continue
                new_mask = mask | (1 << k)
                cand = dp[(mask, j)] + dist
                if (new_mask, k) not in dp or cand < dp[(new_mask, k)] - 1e-9:
                    dp[(new_mask, k)] = cand
                    parent[(new_mask, k)] = j

    full = (1 << n) - 1
    best_total = None
    best_last = None
    for j, node in enumerate(active_nodes):
        if (full, j) not in dp:
            continue
        back, _ = restricted_path(node, depot)
        if back is None:
            continue
        total = dp[(full, j)] + back
        if best_total is None or total < best_total - 1e-9:
            best_total = total
            best_last = j

    if best_last is None:
        return None, None

    order = []
    mask = full
    cur = best_last
    while cur is not None:
        order.append(active_nodes[cur])
        prev = parent[(mask, cur)]
        mask ^= 1 << cur
        cur = prev
    order.reverse()

    route = [depot]
    for a, b in zip([depot] + order, order + [depot]):
        _, path = restricted_path(a, b)
        route.extend(path[1:])

    rows = []
    for seq, node in enumerate(route, start=1):
        rows.append(
            {
                "row_type": "truck_stop",
                "sequence": str(seq),
                "node_id": node,
                "trip_id": "",
                "mode": "",
                "shelter_id": "",
                "launch_node": "",
            }
        )

    first_index = {}
    for idx, node in enumerate(route):
        first_index.setdefault(node, idx)
    activity_nodes_by_route = sorted(active_nodes, key=lambda node: first_index[node])
    drone_counter = 1
    boat_counter = 1
    for node in activity_nodes_by_route:
        pattern = selected_patterns[node]
        for shelter_id in instance["dry_by_node"].get(node, []):
            rows.append(
                {
                    "row_type": "delivery",
                    "sequence": "",
                    "node_id": node,
                    "trip_id": "",
                    "mode": "truck",
                    "shelter_id": shelter_id,
                    "launch_node": "",
                }
            )
        for shelter_id in sorted(pattern["drone_shelters"]):
            rows.append(
                {
                    "row_type": "delivery",
                    "sequence": "",
                    "node_id": "",
                    "trip_id": f"DR{drone_counter:02d}",
                    "mode": "drone",
                    "shelter_id": shelter_id,
                    "launch_node": node,
                }
            )
            drone_counter += 1
        if pattern["kind"] == "boat":
            for seq, shelter_id in enumerate(pattern["boat_trip"]["shelters"], start=1):
                rows.append(
                    {
                        "row_type": "delivery",
                        "sequence": str(seq),
                        "node_id": "",
                        "trip_id": f"BT{boat_counter:02d}",
                        "mode": "boat",
                        "shelter_id": shelter_id,
                        "launch_node": node,
                    }
                )
            boat_counter += 1

    audit = validate_export(instance, rows)
    return order, audit


def add_warm_start(instance, built, selected_patterns, route_order, route_eval):
    model = built["model"]
    sol = model.createSol()
    depot = instance["config"]["depot_id"]

    selected_y = set()
    for node, pattern in selected_patterns.items():
        period_id = route_eval["period_by_node"][node]
        selected_y.add((node, pattern["pattern_id"], period_id))

    for key, var in built["y"].items():
        model.setSolVal(sol, var, 1.0 if key in selected_y else 0.0)

    active_nodes = set(selected_patterns)
    for node, var in built["v"].items():
        model.setSolVal(sol, var, 1.0 if node in active_nodes else 0.0)

    route = [depot] + list(route_order) + [depot]
    selected_x = set(zip(route, route[1:]))
    for key, var in built["x"].items():
        model.setSolVal(sol, var, 1.0 if key in selected_x else 0.0)

    for node, var in built["A"].items():
        model.setSolVal(sol, var, route_eval["arrival"].get(node, 0.0))
    for node, var in built["D"].items():
        model.setSolVal(sol, var, route_eval["departure"].get(node, 0.0))
    for shelter_id, var in built["T"].items():
        model.setSolVal(sol, var, route_eval["service_time"][shelter_id])
    for shelter_id, var in built["L"].items():
        model.setSolVal(sol, var, route_eval["lateness"][shelter_id])
    model.setSolVal(sol, built["C"], route_eval["completion"])

    return model.addSol(sol, free=True)


def build_model(instance, travel_time, patterns_by_node):
    config = instance["config"]
    depot = config["depot_id"]
    relevant_nodes = instance["relevant_nodes"]
    periods = instance["periods"]
    shelters = instance["shelters"]
    dry_by_node = instance["dry_by_node"]
    flooded = instance["flooded"]

    horizon = 10000.0

    model = Model("flood_relief_multimodal")
    model.setParam("limits/gap", 0.0005)
    time_limit = 60.0
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit:
        try:
            time_limit = min(time_limit, float(env_limit))
        except ValueError:
            pass
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    y = {}
    for node in relevant_nodes:
        for pattern in patterns_by_node[node]:
            for period in periods:
                y[(node, pattern["pattern_id"], period["id"])] = model.addVar(
                    vtype="B",
                    name=f"y[{node},{pattern['pattern_id']},{period['id']}]",
                )

    v = {node: model.addVar(vtype="B", name=f"v[{node}]") for node in relevant_nodes}
    A = {node: model.addVar(lb=0.0, name=f"A[{node}]") for node in relevant_nodes}
    D = {node: model.addVar(lb=0.0, name=f"D[{node}]") for node in relevant_nodes}
    T = {
        shelter_id: model.addVar(lb=0.0, name=f"T[{shelter_id}]")
        for shelter_id in shelters
    }
    L = {
        shelter_id: model.addVar(lb=0.0, name=f"L[{shelter_id}]")
        for shelter_id in shelters
    }
    C = model.addVar(lb=0.0, name="completion_time")

    route_nodes = [depot] + relevant_nodes
    x = {}
    for i in route_nodes:
        for j in route_nodes:
            if i == j:
                continue
            x[(i, j)] = model.addVar(vtype="B", name=f"x[{i},{j}]")

    for node in relevant_nodes:
        expr = quicksum(
            y[(node, pattern["pattern_id"], period["id"])]
            for pattern in patterns_by_node[node]
            for period in periods
        )
        if node in dry_by_node:
            model.addCons(expr == 1, name=f"mandatory_pattern[{node}]")
            model.addCons(v[node] == 1, name=f"mandatory_visit[{node}]")
        else:
            model.addCons(expr == v[node], name=f"optional_pattern[{node}]")

    for shelter_id in flooded:
        expr = quicksum(
            y[(node, pattern["pattern_id"], period["id"])]
            for node in relevant_nodes
            for pattern in patterns_by_node[node]
            if shelter_id in pattern["flooded_covered"]
            for period in periods
        )
        model.addCons(expr == 1, name=f"cover[{shelter_id}]")

    for node in relevant_nodes:
        for pattern in patterns_by_node[node]:
            for period in periods:
                var = y[(node, pattern["pattern_id"], period["id"])]
                model.addCons(
                    A[node] >= period["start"] - horizon * (1 - var),
                    name=f"period_lb[{node},{pattern['pattern_id']},{period['id']}]",
                )
                upper = horizon if period["is_last"] else period["end"]
                model.addCons(
                    A[node] <= upper + horizon * (1 - var),
                    name=f"period_ub[{node},{pattern['pattern_id']},{period['id']}]",
                )

    for node in relevant_nodes:
        duration_expr = quicksum(
            pattern["period_data"][period["id"]]["duration"]
            * y[(node, pattern["pattern_id"], period["id"])]
            for pattern in patterns_by_node[node]
            for period in periods
        )
        model.addCons(D[node] == A[node] + duration_expr, name=f"departure[{node}]")
        if node not in dry_by_node:
            model.addCons(A[node] <= horizon * v[node], name=f"arrival_cap[{node}]")
            model.addCons(D[node] <= horizon * v[node], name=f"departure_cap[{node}]")

    model.addCons(
        quicksum(x[(depot, j)] for j in relevant_nodes) == 1,
        name="depot_out",
    )
    model.addCons(
        quicksum(x[(i, depot)] for i in relevant_nodes) == 1,
        name="depot_in",
    )

    for node in relevant_nodes:
        model.addCons(
            quicksum(x[(node, j)] for j in route_nodes if j != node) == v[node],
            name=f"route_out[{node}]",
        )
        model.addCons(
            quicksum(x[(j, node)] for j in route_nodes if j != node) == v[node],
            name=f"route_in[{node}]",
        )

    for node in relevant_nodes:
        model.addCons(
            A[node] >= travel_time[(depot, node)] - horizon * (1 - x[(depot, node)]),
            name=f"start_time[{node}]",
        )

    for i in relevant_nodes:
        for j in relevant_nodes:
            if i == j:
                continue
            model.addCons(
                A[j] >= D[i] + travel_time[(i, j)] - horizon * (1 - x[(i, j)]),
                name=f"time_link[{i},{j}]",
            )

    for node in relevant_nodes:
        model.addCons(
            C >= D[node] + travel_time[(node, depot)] - horizon * (1 - x[(node, depot)]),
            name=f"completion[{node}]",
        )

    for node, dry_shelters in dry_by_node.items():
        for shelter_id in dry_shelters:
            model.addCons(T[shelter_id] == D[node], name=f"dry_time[{shelter_id}]")

    for shelter_id in flooded:
        for node in relevant_nodes:
            drone_expr = quicksum(
                y[(node, pattern["pattern_id"], period["id"])]
                for pattern in patterns_by_node[node]
                if pattern["kind"] == "truck_drone" and shelter_id in pattern["drone_shelters"]
                for period in periods
            )
            if drone_expr is not None:
                model.addCons(
                    T[shelter_id] >= D[node] - horizon + horizon * drone_expr,
                    name=f"drone_time_lb[{shelter_id},{node}]",
                )
                model.addCons(
                    T[shelter_id] <= D[node] + horizon - horizon * drone_expr,
                    name=f"drone_time_ub[{shelter_id},{node}]",
                )
        for node in relevant_nodes:
            for pattern in patterns_by_node[node]:
                if pattern["kind"] != "boat":
                    continue
                if shelter_id not in pattern["flooded_covered"]:
                    continue
                for period in periods:
                    var = y[(node, pattern["pattern_id"], period["id"])]
                    offset = pattern["period_data"][period["id"]]["boat_offsets"][shelter_id]
                    model.addCons(
                        T[shelter_id] >= A[node] + offset - horizon * (1 - var),
                        name=f"boat_time_lb[{shelter_id},{pattern['pattern_id']},{period['id']}]",
                    )
                    model.addCons(
                        T[shelter_id] <= A[node] + offset + horizon * (1 - var),
                        name=f"boat_time_ub[{shelter_id},{pattern['pattern_id']},{period['id']}]",
                    )

    for shelter_id, shelter in shelters.items():
        model.addCons(
            L[shelter_id] >= T[shelter_id] - shelter["deadline"],
            name=f"lateness_lb[{shelter_id}]",
        )

    objective = C + float(config["late_penalty_per_weighted_minute"]) * quicksum(
        shelters[shelter_id]["weight"] * L[shelter_id]
        for shelter_id in shelters
    )
    model.setObjective(objective, "minimize")

    return {
        "model": model,
        "y": y,
        "v": v,
        "A": A,
        "D": D,
        "T": T,
        "L": L,
        "C": C,
        "x": x,
        "time_limit": time_limit,
        "horizon": horizon,
    }


def extract_solution(instance, built, pattern_lookup, travel_path):
    model = built["model"]
    sol = model.getBestSol()
    if sol is None:
        raise RuntimeError("SCIP finished without an incumbent solution")

    selected_by_node = {}
    for (node, pattern_id, period_id), var in built["y"].items():
        if model.getSolVal(sol, var) > 0.5:
            selected_by_node[node] = {
                "pattern_id": pattern_id,
                "period_id": period_id,
                "pattern": pattern_lookup[pattern_id],
                "arrival": model.getSolVal(sol, built["A"][node]),
                "departure": model.getSolVal(sol, built["D"][node]),
            }

    succ = {}
    for (i, j), var in built["x"].items():
        if model.getSolVal(sol, var) > 0.5:
            succ[i] = j

    depot = instance["config"]["depot_id"]
    closure_route = [depot]
    cur = depot
    seen = set()
    while True:
        nxt = succ[cur]
        closure_route.append(nxt)
        if nxt == depot:
            break
        if nxt in seen:
            raise RuntimeError("Route extraction found a repeated closure node")
        seen.add(nxt)
        cur = nxt

    expanded_route = [depot]
    for a, b in zip(closure_route, closure_route[1:]):
        expanded_route.extend(travel_path[(a, b)][1:])

    first_index = {}
    for idx, node in enumerate(expanded_route):
        first_index.setdefault(node, idx)

    activity_nodes = sorted(
        (node for node, info in selected_by_node.items() if info["pattern"]),
        key=lambda n: first_index.get(n, 10**9),
    )

    truck_rows = []
    for seq, node in enumerate(expanded_route, start=1):
        truck_rows.append(
            {
                "row_type": "truck_stop",
                "sequence": str(seq),
                "node_id": node,
                "trip_id": "",
                "mode": "",
                "shelter_id": "",
                "launch_node": "",
            }
        )

    delivery_rows = []
    drone_trip_counter = 1
    boat_trip_counter = 1
    dry_by_node = instance["dry_by_node"]
    shelters = instance["shelters"]

    for node in activity_nodes:
        info = selected_by_node[node]
        pattern = info["pattern"]
        for shelter_id in dry_by_node.get(node, []):
            delivery_rows.append(
                {
                    "row_type": "delivery",
                    "sequence": "",
                    "node_id": node,
                    "trip_id": "",
                    "mode": "truck",
                    "shelter_id": shelter_id,
                    "launch_node": "",
                }
            )
        for shelter_id in sorted(pattern["drone_shelters"]):
            delivery_rows.append(
                {
                    "row_type": "delivery",
                    "sequence": "",
                    "node_id": "",
                    "trip_id": f"DR{drone_trip_counter:02d}",
                    "mode": "drone",
                    "shelter_id": shelter_id,
                    "launch_node": node,
                }
            )
            drone_trip_counter += 1
        if pattern["kind"] == "boat":
            trip = pattern["boat_trip"]
            trip_id = f"BT{boat_trip_counter:02d}"
            for seq, shelter_id in enumerate(trip["shelters"], start=1):
                delivery_rows.append(
                    {
                        "row_type": "delivery",
                        "sequence": str(seq),
                        "node_id": "",
                        "trip_id": trip_id,
                        "mode": "boat",
                        "shelter_id": shelter_id,
                        "launch_node": node,
                    }
                )
            boat_trip_counter += 1

    return {
        "closure_route": closure_route,
        "expanded_route": expanded_route,
        "selected_by_node": selected_by_node,
        "rows": truck_rows + delivery_rows,
        "solver_service_times": {
            shelter_id: model.getSolVal(sol, built["T"][shelter_id])
            for shelter_id in shelters
        },
        "solver_lateness": {
            shelter_id: model.getSolVal(sol, built["L"][shelter_id])
            for shelter_id in shelters
        },
        "solver_objective": model.getObjVal() if model.getNSols() > 0 else None,
        "solver_completion": model.getSolVal(sol, built["C"]),
    }


def write_solution(rows):
    with open(SOLUTION_PATH, "w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "row_type",
                "sequence",
                "node_id",
                "trip_id",
                "mode",
                "shelter_id",
                "launch_node",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def validate_export(instance, rows=None):
    if rows is None:
        rows = read_csv(SOLUTION_PATH)
    expected_cols = [
        "row_type",
        "sequence",
        "node_id",
        "trip_id",
        "mode",
        "shelter_id",
        "launch_node",
    ]
    if rows and list(rows[0].keys()) != expected_cols:
        raise RuntimeError("solution.csv has unexpected columns")

    config = instance["config"]
    staging = instance["staging"]
    shelters = instance["shelters"]
    road_adj = instance["road_adj"]
    boat_adj = instance["boat_adj"]
    periods = instance["periods"]
    drone_options = instance["drone_options_by_node"]

    truck_rows = [row for row in rows if row["row_type"] == "truck_stop"]
    delivery_rows = [row for row in rows if row["row_type"] == "delivery"]

    if not truck_rows:
        raise RuntimeError("No truck_stop rows found")

    truck_rows = sorted(truck_rows, key=lambda r: int(r["sequence"]))
    route = [row["node_id"] for row in truck_rows]
    if route[0] != config["depot_id"] or route[-1] != config["depot_id"]:
        raise RuntimeError("Truck route must start and end at the depot")

    for prev, cur in zip(route, route[1:]):
        if cur not in road_adj[prev]:
            raise RuntimeError(f"Truck route uses non-road edge {prev}->{cur}")

    shelter_seen = {}
    truck_deliveries = defaultdict(list)
    drone_deliveries = defaultdict(list)
    boat_rows_by_trip = defaultdict(list)

    for row in delivery_rows:
        shelter_id = row["shelter_id"]
        if shelter_id in shelter_seen:
            raise RuntimeError(f"Shelter {shelter_id} is served more than once")
        shelter_seen[shelter_id] = row

        mode = row["mode"]
        if mode == "truck":
            node = row["node_id"]
            if shelters[shelter_id]["area_type"] != "dry":
                raise RuntimeError(f"Truck delivery assigned to flooded shelter {shelter_id}")
            if node != shelters[shelter_id]["nearest_dry_node"]:
                raise RuntimeError(f"Truck delivery for {shelter_id} uses wrong dry node")
            truck_deliveries[node].append(shelter_id)
        elif mode == "drone":
            launch = row["launch_node"]
            if shelters[shelter_id]["area_type"] != "flooded":
                raise RuntimeError(f"Drone delivery assigned to dry shelter {shelter_id}")
            if launch not in drone_options or shelter_id not in drone_options[launch]:
                raise RuntimeError(f"Drone option {launch}->{shelter_id} is infeasible")
            drone_deliveries[launch].append(shelter_id)
        elif mode == "boat":
            launch = row["launch_node"]
            if not staging[launch]["boat_ramp"]:
                raise RuntimeError(f"Boat trip launches from non-ramp {launch}")
            if shelters[shelter_id]["area_type"] != "flooded":
                raise RuntimeError(f"Boat delivery assigned to dry shelter {shelter_id}")
            boat_rows_by_trip[row["trip_id"]].append(row)
        else:
            raise RuntimeError(f"Unknown delivery mode {mode}")

    if set(shelter_seen) != set(shelters):
        missing = sorted(set(shelters) - set(shelter_seen))
        raise RuntimeError(f"Missing shelter deliveries: {missing}")

    activity_nodes = set(truck_deliveries) | set(drone_deliveries)
    for trip_id, trip_rows in boat_rows_by_trip.items():
        trip_rows.sort(key=lambda r: int(r["sequence"]))
        launch_nodes = {row["launch_node"] for row in trip_rows}
        if len(launch_nodes) != 1:
            raise RuntimeError(f"Boat trip {trip_id} uses multiple launch nodes")
        launch = next(iter(launch_nodes))
        activity_nodes.add(launch)
        ordered_shelters = [row["shelter_id"] for row in trip_rows]
        total_load = sum(shelters[h]["demand_kg"] for h in ordered_shelters)
        if total_load > float(config["boat_capacity_kg"]) + 1e-9:
            raise RuntimeError(f"Boat trip {trip_id} exceeds capacity")
        prev = launch
        for shelter_id in ordered_shelters:
            if shelter_id not in boat_adj[prev]:
                raise RuntimeError(f"Boat trip {trip_id} uses invalid arc {prev}->{shelter_id}")
            prev = shelter_id
        if launch not in boat_adj[prev]:
            raise RuntimeError(f"Boat trip {trip_id} cannot return from {prev} to {launch}")

    for node in activity_nodes:
        if node not in route:
            raise RuntimeError(f"Activity node {node} is not visited by the truck")

    first_service_visit = {}
    current = route[0]
    current_time = 0.0
    service_time = {}
    departure_time = {}

    activity_payload = defaultdict(lambda: {"truck": [], "drone": [], "boat": []})
    for node, items in truck_deliveries.items():
        activity_payload[node]["truck"] = sorted(items)
    for node, items in drone_deliveries.items():
        activity_payload[node]["drone"] = sorted(items)
    for trip_id, trip_rows in boat_rows_by_trip.items():
        trip_rows.sort(key=lambda r: int(r["sequence"]))
        launch = trip_rows[0]["launch_node"]
        activity_payload[launch]["boat"].append(
            {
                "trip_id": trip_id,
                "shelters": [row["shelter_id"] for row in trip_rows],
            }
        )

    for nxt in route[1:]:
        current_time += road_adj[current][nxt]
        current = nxt
        if nxt in activity_payload and nxt not in first_service_visit:
            first_service_visit[nxt] = current_time
            period = period_for_time(periods, current_time)
            payload = activity_payload[nxt]
            if payload["boat"] and payload["drone"]:
                raise RuntimeError(f"Node {nxt} mixes boat and drone activity in export")
            if len(payload["boat"]) > 1:
                raise RuntimeError(f"Node {nxt} exports more than one boat trip")

            base = (
                float(config["truck_fixed_stop_minutes"])
                + staging[nxt]["handling_minutes"]
            )
            direct_time = float(config["truck_service_minutes_per_kg"]) * sum(
                shelters[h]["demand_kg"] for h in payload["truck"]
            )
            drone_time = 0.0
            for shelter_id in payload["drone"]:
                option = drone_options[nxt][shelter_id]
                drone_time += (
                    float(config["drone_setup_minutes"])
                    + period["drone_mult"] * option["flight_minutes"]
                    + float(config["risk_penalty_minutes_per_point"]) * option["landing_risk"]
                )
            boat_time = 0.0
            for trip in payload["boat"]:
                ordered = trip["shelters"]
                water = 0.0
                prev = nxt
                for idx, shelter_id in enumerate(ordered, start=1):
                    water += boat_adj[prev][shelter_id]
                    service_time[shelter_id] = (
                        current_time
                        + base
                        + float(config["boat_setup_minutes"])
                        + period["boat_mult"] * water
                        + float(config["boat_unload_minutes_per_shelter"]) * idx
                    )
                    prev = shelter_id
                water += boat_adj[prev][nxt]
                trip_duration = (
                    float(config["boat_setup_minutes"])
                    + period["boat_mult"] * water
                    + float(config["boat_unload_minutes_per_shelter"]) * len(ordered)
                )
                if trip_duration > float(config["boat_trip_limit_minutes"]) + 1e-9:
                    raise RuntimeError(f"Boat trip at {nxt} exceeds duration limit in audit")
                boat_time += trip_duration

            stop_duration = base + direct_time + drone_time + boat_time
            depart = current_time + stop_duration
            departure_time[nxt] = depart
            for shelter_id in payload["truck"] + payload["drone"]:
                service_time[shelter_id] = depart
            current_time = depart

    missing_activity = sorted(activity_nodes - set(first_service_visit))
    if missing_activity:
        raise RuntimeError(f"Activity nodes were not serviced during simulation: {missing_activity}")

    if set(service_time) != set(shelters):
        missing = sorted(set(shelters) - set(service_time))
        raise RuntimeError(f"Simulation failed to assign service times to {missing}")

    weighted_lateness = 0.0
    lateness = {}
    for shelter_id, shelter in shelters.items():
        late = max(0.0, service_time[shelter_id] - shelter["deadline"])
        lateness[shelter_id] = late
        weighted_lateness += shelter["weight"] * late

    objective = current_time + float(config["late_penalty_per_weighted_minute"]) * weighted_lateness
    activity_visit_counts = {}
    for node in route:
        if node in activity_nodes:
            activity_visit_counts[node] = activity_visit_counts.get(node, 0) + 1
    repeated_activity_nodes = {
        node: count for node, count in activity_visit_counts.items() if count > 1
    }
    return {
        "route": route,
        "completion_time": current_time,
        "service_time": service_time,
        "lateness": lateness,
        "weighted_lateness": weighted_lateness,
        "objective": objective,
        "activity_nodes": sorted(activity_nodes),
        "arrival_time_by_node": first_service_visit,
        "departure_time_by_node": departure_time,
        "activity_visit_counts": activity_visit_counts,
        "repeated_activity_nodes": repeated_activity_nodes,
    }


def write_log(log_lines):
    with open(LOG_PATH, "w") as handle:
        handle.write("\n".join(log_lines).rstrip() + "\n")


def main():
    start = time.time()
    log_lines = [
        "# Solve Log",
        "",
        "## Command",
        "",
        f"- `PYTHONPATH=/opt/or-deps python {SUBMISSIONS_DIR / 'solve.py'}`",
        "",
    ]

    instance = load_instance()
    incumbent_rows = read_csv(SOLUTION_PATH) if SOLUTION_PATH.exists() else None
    if incumbent_rows is None:
        raise RuntimeError("Rollback incumbent solution.csv is missing")
    incumbent_audit = validate_export(instance, incumbent_rows)

    travel_time, travel_path = build_shortest_paths(instance)
    trips_by_node = generate_boat_trips(instance)
    patterns_by_node, pattern_lookup = build_patterns(instance, trips_by_node)

    incumbent_patterns = None
    incumbent_order = None
    incumbent_start_ok = False
    incumbent_no_repeat_audit = None
    try:
        incumbent_patterns, incumbent_order = parse_rows_to_pattern_solution(
            instance, patterns_by_node, incumbent_rows
        )
        _, incumbent_no_repeat_audit = best_no_repeat_route_for_patterns(
            instance, incumbent_patterns
        )
    except Exception as exc:
        log_lines.extend(
            [
                "## Incumbent Parsing",
                "",
                f"- Existing artifact could not be mapped back to pattern variables: `{exc}`",
                "",
            ]
        )

    heuristic_candidates = pattern_candidates_beam(instance, patterns_by_node, beam_width=12)
    best_constructive = None
    for candidate in heuristic_candidates:
        cand_order, _ = best_route_for_patterns(instance, travel_time, candidate)
        cand_rows = plan_to_rows(instance, candidate, cand_order, travel_path)["rows"]
        cand_audit = validate_export(instance, cand_rows)
        if best_constructive is None or cand_audit["objective"] < best_constructive["audit"]["objective"] - 1e-9:
            best_constructive = {
                "patterns": candidate,
                "order": cand_order,
                "rows": cand_rows,
                "audit": cand_audit,
            }

    log_lines.extend(
        [
            "## Incumbent Audit",
            "",
            f"- Incumbent objective from disk audit: {incumbent_audit['objective']:.8f}",
            f"- Incumbent completion time: {incumbent_audit['completion_time']:.8f}",
            f"- Incumbent weighted lateness: {incumbent_audit['weighted_lateness']:.8f}",
            f"- Incumbent repeated activity nodes: {incumbent_audit['repeated_activity_nodes'] if incumbent_audit['repeated_activity_nodes'] else '{}'}",
        ]
    )
    if incumbent_no_repeat_audit is not None:
        log_lines.append(
            f"- Best no-repeat repair objective for incumbent pattern set: {incumbent_no_repeat_audit['objective']:.8f}"
        )
    log_lines.extend(
        [
            "",
            "## Preprocessing",
            "",
            f"- Relevant staging nodes: {len(instance['relevant_nodes'])}",
            f"- Flooded shelters: {len(instance['flooded'])}",
            f"- Boat trips generated: {sum(len(v) for v in trips_by_node.values())}",
            f"- Node patterns generated: {sum(len(v) for v in patterns_by_node.values())}",
            f"- Heuristic candidate covers tested: {len(heuristic_candidates)}",
            f"- Best constructive candidate objective: {best_constructive['audit']['objective']:.8f}",
            f"- Best constructive repeated activity nodes: {best_constructive['audit']['repeated_activity_nodes'] if best_constructive['audit']['repeated_activity_nodes'] else '{}'}",
            "",
        ]
    )

    built = build_model(instance, travel_time, patterns_by_node)
    model = built["model"]
    warm_start_results = []
    if incumbent_patterns is not None and incumbent_order is not None:
        incumbent_eval = simulate_closure_plan(
            instance, travel_time, incumbent_patterns, incumbent_order
        )
        incumbent_start_ok = add_warm_start(
            instance, built, incumbent_patterns, incumbent_order, incumbent_eval
        )
        warm_start_results.append(("incumbent", incumbent_start_ok))
    constructive_eval = simulate_closure_plan(
        instance, travel_time, best_constructive["patterns"], best_constructive["order"]
    )
    constructive_start_ok = add_warm_start(
        instance,
        built,
        best_constructive["patterns"],
        best_constructive["order"],
        constructive_eval,
    )
    warm_start_results.append(("constructive", constructive_start_ok))
    solve_started = time.time()
    model.optimize()
    solve_elapsed = time.time() - solve_started

    status = str(model.getStatus())
    nsols = model.getNSols()
    best_bound = None
    gap = None
    objective = None
    if nsols > 0:
        objective = model.getObjVal()
        try:
            best_bound = model.getDualbound()
        except Exception:
            best_bound = None
        try:
            gap = model.getGap()
        except Exception:
            gap = None

    log_lines.extend(
        [
            "## Solve Attempt",
            "",
            f"- SCIP status: `{status}`",
            f"- Time limit used: {built['time_limit']:.8f} seconds",
            f"- Solve wall time: {solve_elapsed:.8f} seconds",
            f"- Warm starts accepted: `{warm_start_results}`",
            f"- Incumbent count: {nsols}",
        ]
    )
    if objective is not None:
        log_lines.append(f"- Solver objective: {objective:.8f}")
    if best_bound is not None:
        log_lines.append(f"- Best bound: {best_bound:.8f}")
    if gap is not None:
        log_lines.append(f"- Relative gap: {gap:.8f}")
    log_lines.append("")

    candidate_pool = [
        {
            "name": "rollback_incumbent",
            "rows": incumbent_rows,
            "audit": incumbent_audit,
        },
        {
            "name": "constructive_candidate",
            "rows": best_constructive["rows"],
            "audit": best_constructive["audit"],
        },
    ]

    if nsols > 0:
        extracted = extract_solution(instance, built, pattern_lookup, travel_path)
        solver_audit = validate_export(instance, extracted["rows"])
        candidate_pool.append(
            {
                "name": "scip_incumbent",
                "rows": extracted["rows"],
                "audit": solver_audit,
            }
        )
    else:
        solver_audit = None

    best_publish = min(candidate_pool, key=lambda item: item["audit"]["objective"])
    publish_improves = (
        best_publish["audit"]["objective"] < incumbent_audit["objective"] - 1e-9
    )
    if publish_improves:
        write_solution(best_publish["rows"])
        publish_action = f"published `{best_publish['name']}`"
    else:
        publish_action = "kept rollback incumbent unchanged"
    final_rows = read_csv(SOLUTION_PATH)
    final_audit = validate_export(instance, final_rows)

    log_lines.extend(
        [
            "## Candidate Comparison",
            "",
            f"- Rollback incumbent objective: {incumbent_audit['objective']:.8f}",
            f"- Constructive candidate objective: {best_constructive['audit']['objective']:.8f}",
        ]
    )
    if solver_audit is not None:
        log_lines.append(f"- SCIP candidate objective: {solver_audit['objective']:.8f}")
    else:
        log_lines.append("- SCIP candidate objective: unavailable (no incumbent)")
    log_lines.extend(
        [
            f"- Publish decision: {publish_action}",
            "",
            "## Validation",
            "",
            "- Exact artifact reparsed from disk-independent candidate rows and recomputed against public data.",
            f"- Truck route nodes audited: {len(final_audit['route'])}",
            f"- Activity nodes audited: {len(final_audit['activity_nodes'])}",
            f"- Completion time from final audit: {final_audit['completion_time']:.8f}",
            f"- Weighted lateness from final audit: {final_audit['weighted_lateness']:.8f}",
            f"- Objective from final audit: {final_audit['objective']:.8f}",
            f"- Final repeated activity nodes: {final_audit['repeated_activity_nodes'] if final_audit['repeated_activity_nodes'] else '{}'}",
            "- Hard checks passed: shelter coverage, road adjacency, boat adjacency, boat capacity, boat duration, launch eligibility.",
            "",
        ]
    )

    total_elapsed = time.time() - start
    log_lines.extend(
        [
            "## Runtime",
            "",
            f"- End-to-end wall time: {total_elapsed:.8f} seconds",
        ]
    )
    write_log(log_lines)


if __name__ == "__main__":
    main()
