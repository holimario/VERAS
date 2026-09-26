from __future__ import annotations

import csv
import json
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from pyscipopt import Model, quicksum


DATA_DIR = Path("/app/data")
SUBMISSIONS_DIR = Path("/app/submissions")
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"


@dataclass(frozen=True)
class Edge:
    edge_id: str
    donor_id: str
    donor_hospital_id: str
    donor_region: str
    recipient_pair_id: str
    recipient_hospital_id: str
    recipient_region: str
    travel_hours: float
    virtual_crossmatch_score: float
    failure_probability: float
    transplant_value: float
    same_region: int
    late_courier_required: int


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    probability: float
    failure_multiplier: float
    long_courier_additive: float
    value_multiplier: float


@dataclass(frozen=True)
class ScheduledLink:
    edge: Edge
    surgery_day_id: str


@dataclass(frozen=True)
class CandidateDay:
    uid: str
    kind: str
    pair_sequence: Tuple[str, ...]
    altruistic_donor_id: Optional[str]
    day_id: str
    scheduled_links: Tuple[ScheduledLink, ...]
    hs_count: int
    ped_count: int
    region_counts: Tuple[Tuple[str, int], ...]
    net_counts: Tuple[Tuple[str, int], ...]
    scenario_scores: Dict[str, float]
    ranking_score: float


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def load_data() -> Dict[str, object]:
    config = json.loads((DATA_DIR / "config.json").read_text())

    planning_days = sorted(
        read_csv(DATA_DIR / "planning_days.csv"),
        key=lambda row: int(row["day_index"]),
    )
    day_ids = [row["day_id"] for row in planning_days]
    day_index = {day_id: idx for idx, day_id in enumerate(day_ids)}
    slot_factor = {row["day_id"]: float(row["regional_slot_factor"]) for row in planning_days}

    hospitals = {
        row["hospital_id"]: {
            "region": row["region"],
            "retrieval_capacity_per_day": int(row["retrieval_capacity_per_day"]),
            "transplant_capacity_per_day": int(row["transplant_capacity_per_day"]),
            "lab_capacity_per_day": int(row["lab_capacity_per_day"]),
            "can_ship_late": int(row["can_ship_late"]),
            "coordination_cost": float(row["coordination_cost"]),
        }
        for row in read_csv(DATA_DIR / "hospitals.csv")
    }
    lab_base_by_region = defaultdict(int)
    for hospital in hospitals.values():
        lab_base_by_region[hospital["region"]] += int(hospital["lab_capacity_per_day"])
    teams = {
        (row["hospital_id"], row["day_id"]): {
            "senior": int(row["senior_team_capacity"]),
            "pediatric": int(row["pediatric_team_capacity"]),
        }
        for row in read_csv(DATA_DIR / "surgical_teams.csv")
    }
    lanes = {
        (row["donor_region"], row["recipient_region"], row["day_id"]): {
            "cap": int(row["max_shipments"]),
            "risk": float(row["cold_ischemia_risk_multiplier"]),
        }
        for row in read_csv(DATA_DIR / "courier_lanes.csv")
    }
    region_policy = {
        row["region"]: {
            "min_transplants": int(row["min_transplants"]),
            "max_net_export_deficit": int(row["max_net_export_deficit"]),
            "credit_weight": float(row["credit_weight"]),
        }
        for row in read_csv(DATA_DIR / "region_policy.csv")
    }
    pairs = {
        row["pair_id"]: {
            "hospital_id": row["hospital_id"],
            "region": row["region"],
            "patient_blood": row["patient_blood"],
            "donor_blood": row["donor_blood"],
            "cpra": int(row["cpra"]),
            "pediatric": int(row["pediatric"]),
            "dialysis_months": int(row["dialysis_months"]),
            "priority_tier": row["priority_tier"],
            "max_travel_hours": float(row["max_travel_hours"]),
            "arrival_rounds_waited": int(row["arrival_rounds_waited"]),
        }
        for row in read_csv(DATA_DIR / "patient_pairs.csv")
    }
    altruists = {
        row["donor_id"]: {
            "hospital_id": row["hospital_id"],
            "region": row["region"],
            "donor_blood": row["donor_blood"],
            "available_day": row["available_day"],
            "chain_start_bonus": float(row["chain_start_bonus"]),
        }
        for row in read_csv(DATA_DIR / "altruistic_donors.csv")
    }
    scenarios = [
        Scenario(
            scenario_id=row["scenario_id"],
            probability=float(row["probability"]),
            failure_multiplier=float(row["failure_multiplier"]),
            long_courier_additive=float(row["long_courier_additive"]),
            value_multiplier=float(row["value_multiplier"]),
        )
        for row in read_csv(DATA_DIR / "stress_scenarios.csv")
    ]
    raw_edges = [
        Edge(
            edge_id=row["edge_id"],
            donor_id=row["donor_id"],
            donor_hospital_id=row["donor_hospital_id"],
            donor_region=row["donor_region"],
            recipient_pair_id=row["recipient_pair_id"],
            recipient_hospital_id=row["recipient_hospital_id"],
            recipient_region=row["recipient_region"],
            travel_hours=float(row["travel_hours"]),
            virtual_crossmatch_score=float(row["virtual_crossmatch_score"]),
            failure_probability=float(row["failure_probability"]),
            transplant_value=float(row["transplant_value"]),
            same_region=int(row["same_region"]),
            late_courier_required=int(row["late_courier_required"]),
        )
        for row in read_csv(DATA_DIR / "donor_edges.csv")
    ]

    return {
        "config": config,
        "planning_days": planning_days,
        "day_ids": day_ids,
        "day_index": day_index,
        "slot_factor": slot_factor,
        "hospitals": hospitals,
        "lab_base_by_region": dict(lab_base_by_region),
        "teams": teams,
        "lanes": lanes,
        "region_policy": region_policy,
        "pairs": pairs,
        "altruists": altruists,
        "scenarios": scenarios,
        "raw_edges": raw_edges,
    }


def effective_capacity(base_capacity: int, factor: float) -> int:
    return int(math.floor(base_capacity * factor + 1e-9))


def get_time_limit(config: Dict[str, object]) -> float:
    default_limit = float(config["time_limit_seconds"])
    env_value = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_value is None:
        return max(1.0, default_limit - 5.0)
    try:
        env_limit = float(env_value)
    except ValueError:
        return max(1.0, default_limit - 5.0)
    return max(1.0, min(default_limit, env_limit) - 5.0)


def average_lane_risk(data: Dict[str, object]) -> Dict[Tuple[str, str], float]:
    values: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for (donor_region, recipient_region, _day_id), lane in data["lanes"].items():
        values[(donor_region, recipient_region)].append(float(lane["risk"]))
    return {key: sum(items) / len(items) for key, items in values.items()}


def link_ranking_score(edge: Edge, data: Dict[str, object], avg_lane: Dict[Tuple[str, str], float]) -> float:
    scenarios: List[Scenario] = data["scenarios"]
    credit = float(data["region_policy"][edge.recipient_region]["credit_weight"])
    lane_mult = 1.0 if edge.same_region else avg_lane[(edge.donor_region, edge.recipient_region)]
    long_indicator = 1.0 if edge.travel_hours > 2.5 else 0.0
    travel_penalty = float(data["config"]["travel_cost_per_hour"]) * edge.travel_hours

    total = 0.0
    for scenario in scenarios:
        fail = min(
            0.999999,
            edge.failure_probability * scenario.failure_multiplier * lane_mult
            + scenario.long_courier_additive * long_indicator,
        )
        success = 1.0 - fail
        value = edge.transplant_value * scenario.value_multiplier * credit
        total += scenario.probability * (value * success - travel_penalty)
    return total


def build_feasible_edges(
    data: Dict[str, object]
) -> Tuple[Dict[Tuple[str, str], Edge], Dict[str, List[Edge]], int]:
    hospitals = data["hospitals"]
    pairs = data["pairs"]
    avg_lane = average_lane_risk(data)

    best_edge: Dict[Tuple[str, str], Tuple[float, Edge]] = {}
    filtered_count = 0
    for edge in data["raw_edges"]:
        if edge.donor_id == edge.recipient_pair_id:
            continue
        if edge.travel_hours > float(pairs[edge.recipient_pair_id]["max_travel_hours"]) + 1e-9:
            continue
        if edge.late_courier_required and not hospitals[edge.donor_hospital_id]["can_ship_late"]:
            continue
        filtered_count += 1
        key = (edge.donor_id, edge.recipient_pair_id)
        score = link_ranking_score(edge, data, avg_lane)
        if key not in best_edge or score > best_edge[key][0] + 1e-9:
            best_edge[key] = (score, edge)

    outgoing_all: Dict[str, List[Tuple[float, Edge]]] = defaultdict(list)
    full_edge_map: Dict[Tuple[str, str], Edge] = {}
    for key, (score, edge) in best_edge.items():
        full_edge_map[key] = edge
        outgoing_all[edge.donor_id].append((score, edge))

    succ_limit = int(data["config"]["candidate_successor_limit"])
    edge_map: Dict[Tuple[str, str], Edge] = {}
    outgoing_pruned: Dict[str, List[Edge]] = {}
    for donor_id, items in outgoing_all.items():
        items.sort(
            key=lambda item: (item[0], item[1].transplant_value, -item[1].failure_probability),
            reverse=True,
        )
        chosen = [edge for _score, edge in items[:succ_limit]]
        outgoing_pruned[donor_id] = chosen
        for edge in chosen:
            edge_map[(edge.donor_id, edge.recipient_pair_id)] = edge
    return edge_map, outgoing_pruned, filtered_count


def build_full_edge_map(data: Dict[str, object]) -> Dict[Tuple[str, str], Edge]:
    hospitals = data["hospitals"]
    pairs = data["pairs"]
    avg_lane = average_lane_risk(data)
    best_edge: Dict[Tuple[str, str], Tuple[float, Edge]] = {}
    for edge in data["raw_edges"]:
        if edge.donor_id == edge.recipient_pair_id:
            continue
        if edge.travel_hours > float(pairs[edge.recipient_pair_id]["max_travel_hours"]) + 1e-9:
            continue
        if edge.late_courier_required and not hospitals[edge.donor_hospital_id]["can_ship_late"]:
            continue
        key = (edge.donor_id, edge.recipient_pair_id)
        score = link_ranking_score(edge, data, avg_lane)
        if key not in best_edge or score > best_edge[key][0] + 1e-9:
            best_edge[key] = (score, edge)
    return {key: edge for key, (_score, edge) in best_edge.items()}


def structure_proxy_score(
    kind: str,
    altruist_id: Optional[str],
    pair_sequence: Sequence[str],
    edge_map: Dict[Tuple[str, str], Edge],
    data: Dict[str, object],
    avg_lane: Dict[Tuple[str, str], float],
) -> float:
    links: List[Edge] = []
    if kind == "cycle":
        for idx, donor_pair in enumerate(pair_sequence):
            recipient_pair = pair_sequence[(idx + 1) % len(pair_sequence)]
            links.append(edge_map[(donor_pair, recipient_pair)])
    else:
        assert altruist_id is not None
        links.append(edge_map[(altruist_id, pair_sequence[0])])
        for idx in range(len(pair_sequence) - 1):
            links.append(edge_map[(pair_sequence[idx], pair_sequence[idx + 1])])

    scenario_scores: Dict[str, float] = {}
    for scenario in data["scenarios"]:
        successes = []
        values = []
        travel_sum = 0.0
        for edge in links:
            lane_mult = 1.0 if edge.same_region else avg_lane[(edge.donor_region, edge.recipient_region)]
            fail = min(
                0.999999,
                edge.failure_probability * scenario.failure_multiplier * lane_mult
                + scenario.long_courier_additive * (1.0 if edge.travel_hours > 2.5 else 0.0),
            )
            successes.append(1.0 - fail)
            values.append(
                edge.transplant_value
                * scenario.value_multiplier
                * float(data["region_policy"][edge.recipient_region]["credit_weight"])
            )
            travel_sum += float(data["config"]["travel_cost_per_hour"]) * edge.travel_hours

        if kind == "cycle":
            joint = 1.0
            for success in successes:
                joint *= success
            score = (
                joint * sum(values)
                - travel_sum
                - float(data["config"]["cycle_cancel_penalty"]) * (1.0 - joint)
            )
            if len(pair_sequence) == 2:
                score += float(data["config"]["embedded_two_cycle_bonus"])
        else:
            score = 0.0
            prefixes = []
            prefix = 1.0
            for success, value in zip(successes, values):
                prefix *= success
                prefixes.append(prefix)
                score += prefix * value
            score -= travel_sum
            score -= float(data["config"]["chain_break_penalty"]) * sum(
                prefixes[idx] - prefixes[idx + 1] for idx in range(len(prefixes) - 1)
            )
            score += float(data["altruists"][altruist_id]["chain_start_bonus"])
        scenario_scores[scenario.scenario_id] = score

    expected = sum(
        scenario.probability * scenario_scores[scenario.scenario_id] for scenario in data["scenarios"]
    )
    stressed = [
        scenario_scores[scenario.scenario_id]
        for scenario in data["scenarios"]
        if scenario.scenario_id != "BASELINE"
    ]
    tail = min(stressed) if stressed else expected
    weight = float(data["config"]["tail_risk_weight"])
    return (1.0 - weight) * expected + weight * tail


def enumerate_structures(
    data: Dict[str, object],
    edge_map: Dict[Tuple[str, str], Edge],
    outgoing_pruned: Dict[str, List[Edge]],
) -> List[Tuple[str, Optional[str], Tuple[str, ...]]]:
    pairs = data["pairs"]
    altruists = data["altruists"]
    pair_ids = sorted(pairs)

    successors: Dict[str, List[str]] = defaultdict(list)
    successor_sets: Dict[str, set[str]] = defaultdict(set)
    for donor_id, edges in outgoing_pruned.items():
        recipients = sorted({edge.recipient_pair_id for edge in edges})
        successors[donor_id] = recipients
        successor_sets[donor_id] = set(recipients)

    structures: List[Tuple[str, Optional[str], Tuple[str, ...]]] = []
    for first in pair_ids:
        for second in successors[first]:
            if second not in pairs or second <= first:
                continue
            if first in successor_sets.get(second, set()):
                structures.append(("cycle", None, (first, second)))

    for first in pair_ids:
        for second in successors[first]:
            if second not in pairs or second == first:
                continue
            for third in successors[second]:
                if third not in pairs or third in (first, second):
                    continue
                if first not in successor_sets.get(third, set()):
                    continue
                if first != min(first, second, third):
                    continue
                structures.append(("cycle", None, (first, second, third)))

    for altruist_id in sorted(altruists):
        for first in successors[altruist_id]:
            if first not in pairs:
                continue
            structures.append(("chain", altruist_id, (first,)))
            for second in successors[first]:
                if second not in pairs or second == first:
                    continue
                structures.append(("chain", altruist_id, (first, second)))
                for third in successors[second]:
                    if third not in pairs or third in (first, second):
                        continue
                    structures.append(("chain", altruist_id, (first, second, third)))

    limit = int(data["config"]["max_candidate_structures"])
    if len(structures) <= limit:
        return structures

    avg_lane = average_lane_risk(data)
    ranked = []
    for kind, altruist_id, pair_sequence in structures:
        ranked.append(
            (
                structure_proxy_score(kind, altruist_id, pair_sequence, edge_map, data, avg_lane),
                kind,
                altruist_id,
                pair_sequence,
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(kind, altruist_id, pair_sequence) for _score, kind, altruist_id, pair_sequence in ranked[:limit]]


def chain_link_day(day_ids: Sequence[str], start_index: int, link_index: int, links_per_day: int) -> str:
    return day_ids[start_index + (link_index // links_per_day)]


def feasible_days_for_structure(
    kind: str,
    altruist_id: Optional[str],
    pair_sequence: Sequence[str],
    data: Dict[str, object],
) -> List[str]:
    if kind == "cycle":
        return list(data["day_ids"])

    assert altruist_id is not None
    day_ids = data["day_ids"]
    day_index = data["day_index"]
    links_per_day = int(data["config"]["chain_links_per_day"])
    start_min = day_index[data["altruists"][altruist_id]["available_day"]]
    max_offset = (len(pair_sequence) - 1) // links_per_day
    return [
        day_ids[idx]
        for idx in range(start_min, len(day_ids))
        if idx + max_offset < len(day_ids)
    ]


def scheduled_links_for_candidate(
    kind: str,
    altruist_id: Optional[str],
    pair_sequence: Sequence[str],
    day_id: str,
    edge_map: Dict[Tuple[str, str], Edge],
    data: Dict[str, object],
) -> List[ScheduledLink]:
    if kind == "cycle":
        return [
            ScheduledLink(
                edge=edge_map[(pair_sequence[idx], pair_sequence[(idx + 1) % len(pair_sequence)])],
                surgery_day_id=day_id,
            )
            for idx in range(len(pair_sequence))
        ]

    assert altruist_id is not None
    start_index = data["day_index"][day_id]
    links_per_day = int(data["config"]["chain_links_per_day"])
    scheduled = [
        ScheduledLink(
            edge=edge_map[(altruist_id, pair_sequence[0])],
            surgery_day_id=chain_link_day(data["day_ids"], start_index, 0, links_per_day),
        )
    ]
    for idx in range(len(pair_sequence) - 1):
        scheduled.append(
            ScheduledLink(
                edge=edge_map[(pair_sequence[idx], pair_sequence[idx + 1])],
                surgery_day_id=chain_link_day(data["day_ids"], start_index, idx + 1, links_per_day),
            )
        )
    return scheduled


def score_candidate_day(
    kind: str,
    altruist_id: Optional[str],
    pair_sequence: Sequence[str],
    scheduled_links: Sequence[ScheduledLink],
    data: Dict[str, object],
) -> Dict[str, float]:
    lanes = data["lanes"]
    scores: Dict[str, float] = {}
    for scenario in data["scenarios"]:
        successes = []
        values = []
        travel_sum = 0.0
        for scheduled_link in scheduled_links:
            edge = scheduled_link.edge
            lane_mult = 1.0
            if not edge.same_region:
                lane_mult = float(
                    lanes[(edge.donor_region, edge.recipient_region, scheduled_link.surgery_day_id)]["risk"]
                )
            fail = min(
                0.999999,
                edge.failure_probability * scenario.failure_multiplier * lane_mult
                + scenario.long_courier_additive * (1.0 if edge.travel_hours > 2.5 else 0.0),
            )
            successes.append(1.0 - fail)
            values.append(
                edge.transplant_value
                * scenario.value_multiplier
                * float(data["region_policy"][edge.recipient_region]["credit_weight"])
            )
            travel_sum += float(data["config"]["travel_cost_per_hour"]) * edge.travel_hours

        if kind == "cycle":
            joint = 1.0
            for success in successes:
                joint *= success
            score = (
                joint * sum(values)
                - travel_sum
                - float(data["config"]["cycle_cancel_penalty"]) * (1.0 - joint)
            )
            if len(pair_sequence) == 2:
                score += float(data["config"]["embedded_two_cycle_bonus"])
        else:
            prefixes = []
            prefix = 1.0
            score = 0.0
            for success, value in zip(successes, values):
                prefix *= success
                prefixes.append(prefix)
                score += prefix * value
            score -= travel_sum
            score -= float(data["config"]["chain_break_penalty"]) * sum(
                prefixes[idx] - prefixes[idx + 1] for idx in range(len(prefixes) - 1)
            )
            score += float(data["altruists"][altruist_id]["chain_start_bonus"])
        scores[scenario.scenario_id] = score
    return scores


def build_candidate_days(
    data: Dict[str, object],
    structures: Sequence[Tuple[str, Optional[str], Tuple[str, ...]]],
    edge_map: Dict[Tuple[str, str], Edge],
):
    pairs = data["pairs"]
    config = data["config"]

    candidates: List[CandidateDay] = []
    pair_to_candidates = defaultdict(list)
    altruist_to_candidates = defaultdict(list)
    retrieval_map = defaultdict(list)
    transplant_map = defaultdict(list)
    lab_map = defaultdict(list)
    senior_map = defaultdict(list)
    ped_team_map = defaultdict(list)
    lane_map = defaultdict(list)
    hs_counts: List[int] = []
    ped_counts: List[int] = []
    region_floor_map = defaultdict(list)
    net_export_map = defaultdict(list)

    for structure_index, (kind, altruist_id, pair_sequence) in enumerate(structures, start=1):
        for day_id in feasible_days_for_structure(kind, altruist_id, pair_sequence, data):
            scheduled_links = scheduled_links_for_candidate(
                kind=kind,
                altruist_id=altruist_id,
                pair_sequence=pair_sequence,
                day_id=day_id,
                edge_map=edge_map,
                data=data,
            )
            scenario_scores = score_candidate_day(
                kind=kind,
                altruist_id=altruist_id,
                pair_sequence=pair_sequence,
                scheduled_links=scheduled_links,
                data=data,
            )
            expected = sum(
                scenario.probability * scenario_scores[scenario.scenario_id]
                for scenario in data["scenarios"]
            )
            stressed = [
                scenario_scores[scenario.scenario_id]
                for scenario in data["scenarios"]
                if scenario.scenario_id != "BASELINE"
            ]
            tail = min(stressed) if stressed else expected
            weight = float(config["tail_risk_weight"])
            ranking_score = (1.0 - weight) * expected + weight * tail

            retrieval_counts = Counter()
            transplant_counts = Counter()
            lab_counts = Counter()
            senior_counts = Counter()
            ped_team_counts = Counter()
            lane_counts = Counter()
            region_counts = Counter()
            net_counts = Counter()
            hs_count = 0
            ped_count = 0

            for pair_id in pair_sequence:
                pair = pairs[pair_id]
                region_counts[pair["region"]] += 1
                if pair["cpra"] >= int(config["highly_sensitized_cpra"]):
                    hs_count += 1
                if pair["pediatric"]:
                    ped_count += 1

            for scheduled_link in scheduled_links:
                edge = scheduled_link.edge
                surgery_day = scheduled_link.surgery_day_id
                recipient_pair = pairs[edge.recipient_pair_id]
                weak = edge.virtual_crossmatch_score < float(config["risky_crossmatch_threshold"])
                needs_senior = (
                    recipient_pair["priority_tier"] == "critical"
                    or recipient_pair["cpra"] >= int(config["highly_sensitized_cpra"])
                    or weak
                )

                retrieval_counts[(edge.donor_hospital_id, surgery_day)] += 1
                transplant_counts[(edge.recipient_hospital_id, surgery_day)] += 1
                if weak:
                    lab_counts[(edge.recipient_region, surgery_day)] += 1
                if needs_senior:
                    senior_counts[(edge.recipient_hospital_id, surgery_day)] += 1
                if recipient_pair["pediatric"]:
                    ped_team_counts[(edge.recipient_hospital_id, surgery_day)] += 1
                if not edge.same_region:
                    lane_counts[(edge.donor_region, edge.recipient_region, surgery_day)] += 1
                    net_counts[edge.donor_region] += 1
                    net_counts[edge.recipient_region] -= 1

            candidate = CandidateDay(
                uid=f"{'C' if kind == 'cycle' else 'K'}_{structure_index:05d}_{day_id}",
                kind=kind,
                pair_sequence=tuple(pair_sequence),
                altruistic_donor_id=altruist_id,
                day_id=day_id,
                scheduled_links=tuple(scheduled_links),
                hs_count=hs_count,
                ped_count=ped_count,
                region_counts=tuple(sorted(region_counts.items())),
                net_counts=tuple(sorted(net_counts.items())),
                scenario_scores=scenario_scores,
                ranking_score=ranking_score,
            )
            candidate_index = len(candidates)
            candidates.append(candidate)

            for pair_id in pair_sequence:
                pair_to_candidates[pair_id].append(candidate_index)
            if altruist_id is not None:
                altruist_to_candidates[altruist_id].append(candidate_index)

            for key, count in retrieval_counts.items():
                retrieval_map[key].append((candidate_index, count))
            for key, count in transplant_counts.items():
                transplant_map[key].append((candidate_index, count))
            for key, count in lab_counts.items():
                lab_map[key].append((candidate_index, count))
            for key, count in senior_counts.items():
                senior_map[key].append((candidate_index, count))
            for key, count in ped_team_counts.items():
                ped_team_map[key].append((candidate_index, count))
            for key, count in lane_counts.items():
                lane_map[key].append((candidate_index, count))
            for key, count in region_counts.items():
                region_floor_map[key].append((candidate_index, count))
            for key, count in net_counts.items():
                net_export_map[key].append((candidate_index, count))
            hs_counts.append(hs_count)
            ped_counts.append(ped_count)

    return (
        candidates,
        pair_to_candidates,
        altruist_to_candidates,
        retrieval_map,
        transplant_map,
        lab_map,
        senior_map,
        ped_team_map,
        lane_map,
        hs_counts,
        ped_counts,
        region_floor_map,
        net_export_map,
    )


def can_add_candidate(
    candidate: CandidateDay,
    data: Dict[str, object],
    used_pairs: set[str],
    used_altruists: set[str],
    chain_count: int,
    retrieval_usage: Counter,
    transplant_usage: Counter,
    lab_usage: Counter,
    senior_usage: Counter,
    ped_usage: Counter,
    lane_usage: Counter,
    net_export: Counter,
) -> bool:
    if any(pair_id in used_pairs for pair_id in candidate.pair_sequence):
        return False
    if candidate.altruistic_donor_id is not None:
        if candidate.altruistic_donor_id in used_altruists:
            return False
        if chain_count >= int(data["config"]["max_active_chains"]):
            return False

    hospitals = data["hospitals"]
    slot_factor = data["slot_factor"]
    teams = data["teams"]
    add_net = Counter(dict(candidate.net_counts))
    for region, delta in add_net.items():
        if net_export[region] + delta > int(data["region_policy"][region]["max_net_export_deficit"]):
            return False

    local_retrieval = Counter()
    local_transplant = Counter()
    local_lab = Counter()
    local_senior = Counter()
    local_ped = Counter()
    local_lane = Counter()

    for scheduled_link in candidate.scheduled_links:
        edge = scheduled_link.edge
        surgery_day = scheduled_link.surgery_day_id
        recipient_pair = data["pairs"][edge.recipient_pair_id]
        weak = edge.virtual_crossmatch_score < float(data["config"]["risky_crossmatch_threshold"])

        ret_key = (edge.donor_hospital_id, surgery_day)
        tx_key = (edge.recipient_hospital_id, surgery_day)
        local_retrieval[ret_key] += 1
        local_transplant[tx_key] += 1
        if weak:
            local_lab[(edge.recipient_region, surgery_day)] += 1
        if (
            recipient_pair["priority_tier"] == "critical"
            or recipient_pair["cpra"] >= int(data["config"]["highly_sensitized_cpra"])
            or weak
        ):
            local_senior[(edge.recipient_hospital_id, surgery_day)] += 1
        if recipient_pair["pediatric"]:
            local_ped[(edge.recipient_hospital_id, surgery_day)] += 1
        if not edge.same_region:
            local_lane[(edge.donor_region, edge.recipient_region, surgery_day)] += 1

    for key, add in local_retrieval.items():
        hospital_id, surgery_day = key
        if retrieval_usage[key] + add > effective_capacity(
            int(hospitals[hospital_id]["retrieval_capacity_per_day"]),
            float(slot_factor[surgery_day]),
        ):
            return False
    for key, add in local_transplant.items():
        hospital_id, surgery_day = key
        if transplant_usage[key] + add > effective_capacity(
            int(hospitals[hospital_id]["transplant_capacity_per_day"]),
            float(slot_factor[surgery_day]),
        ):
            return False
    for key, add in local_lab.items():
        region, surgery_day = key
        if lab_usage[key] + add > effective_capacity(
                int(data["lab_base_by_region"][region]),
                float(slot_factor[surgery_day]),
        ):
            return False
    for key, add in local_senior.items():
        if senior_usage[key] + add > int(teams[key]["senior"]):
            return False
    for key, add in local_ped.items():
        if ped_usage[key] + add > int(teams[key]["pediatric"]):
            return False
    for key, add in local_lane.items():
        if lane_usage[key] + add > int(data["lanes"][key]["cap"]):
            return False
    return True


def add_candidate_usage(
    candidate: CandidateDay,
    data: Dict[str, object],
    used_pairs: set[str],
    used_altruists: set[str],
    retrieval_usage: Counter,
    transplant_usage: Counter,
    lab_usage: Counter,
    senior_usage: Counter,
    ped_usage: Counter,
    lane_usage: Counter,
    region_tx: Counter,
    net_export: Counter,
) -> None:
    for pair_id in candidate.pair_sequence:
        used_pairs.add(pair_id)
        region_tx[data["pairs"][pair_id]["region"]] += 1
    if candidate.altruistic_donor_id is not None:
        used_altruists.add(candidate.altruistic_donor_id)

    for region, delta in candidate.net_counts:
        net_export[region] += delta

    for scheduled_link in candidate.scheduled_links:
        edge = scheduled_link.edge
        surgery_day = scheduled_link.surgery_day_id
        recipient_pair = data["pairs"][edge.recipient_pair_id]
        weak = edge.virtual_crossmatch_score < float(data["config"]["risky_crossmatch_threshold"])

        retrieval_usage[(edge.donor_hospital_id, surgery_day)] += 1
        transplant_usage[(edge.recipient_hospital_id, surgery_day)] += 1
        if weak:
            lab_usage[(edge.recipient_region, surgery_day)] += 1
        if (
            recipient_pair["priority_tier"] == "critical"
            or recipient_pair["cpra"] >= int(data["config"]["highly_sensitized_cpra"])
            or weak
        ):
            senior_usage[(edge.recipient_hospital_id, surgery_day)] += 1
        if recipient_pair["pediatric"]:
            ped_usage[(edge.recipient_hospital_id, surgery_day)] += 1
        if not edge.same_region:
            lane_usage[(edge.donor_region, edge.recipient_region, surgery_day)] += 1


def build_greedy_incumbent(candidates: Sequence[CandidateDay], data: Dict[str, object]) -> List[int]:
    sorted_indices = sorted(
        range(len(candidates)),
        key=lambda idx: (
            candidates[idx].ranking_score,
            candidates[idx].hs_count + candidates[idx].ped_count,
            len(candidates[idx].pair_sequence),
        ),
        reverse=True,
    )

    selected: List[int] = []
    used_pairs: set[str] = set()
    used_altruists: set[str] = set()
    retrieval_usage = Counter()
    transplant_usage = Counter()
    lab_usage = Counter()
    senior_usage = Counter()
    ped_usage = Counter()
    lane_usage = Counter()
    region_tx = Counter()
    net_export = Counter()
    hs_total = 0
    ped_total = 0
    chain_count = 0

    def deficits():
        region_deficits = {
            region: max(0, int(policy["min_transplants"]) - region_tx[region])
            for region, policy in data["region_policy"].items()
        }
        return (
            max(0, int(data["config"]["min_highly_sensitized_transplants"]) - hs_total),
            max(0, int(data["config"]["min_pediatric_transplants"]) - ped_total),
            region_deficits,
        )

    while True:
        hs_deficit, ped_deficit, region_deficits = deficits()
        if hs_deficit == 0 and ped_deficit == 0 and all(v == 0 for v in region_deficits.values()):
            break

        best_idx = None
        best_score = None
        for idx in sorted_indices:
            candidate = candidates[idx]
            if idx in selected:
                continue
            if not can_add_candidate(
                candidate,
                data,
                used_pairs,
                used_altruists,
                chain_count,
                retrieval_usage,
                transplant_usage,
                lab_usage,
                senior_usage,
                ped_usage,
                lane_usage,
                net_export,
            ):
                continue
            coverage = 3000 * min(candidate.hs_count, hs_deficit)
            coverage += 2600 * min(candidate.ped_count, ped_deficit)
            coverage += 1800 * sum(min(count, region_deficits[region]) for region, count in candidate.region_counts)
            coverage += 0.01 * candidate.ranking_score
            if coverage <= 0:
                continue
            if best_score is None or coverage > best_score:
                best_score = coverage
                best_idx = idx

        if best_idx is None:
            return []

        candidate = candidates[best_idx]
        selected.append(best_idx)
        hs_total += candidate.hs_count
        ped_total += candidate.ped_count
        if candidate.kind == "chain":
            chain_count += 1
        add_candidate_usage(
            candidate,
            data,
            used_pairs,
            used_altruists,
            retrieval_usage,
            transplant_usage,
            lab_usage,
            senior_usage,
            ped_usage,
            lane_usage,
            region_tx,
            net_export,
        )

    for idx in sorted_indices:
        candidate = candidates[idx]
        if idx in selected or candidate.ranking_score <= 1e-6:
            continue
        if not can_add_candidate(
            candidate,
            data,
            used_pairs,
            used_altruists,
            chain_count,
            retrieval_usage,
            transplant_usage,
            lab_usage,
            senior_usage,
            ped_usage,
            lane_usage,
            net_export,
        ):
            continue
        selected.append(idx)
        hs_total += candidate.hs_count
        ped_total += candidate.ped_count
        if candidate.kind == "chain":
            chain_count += 1
        add_candidate_usage(
            candidate,
            data,
            used_pairs,
            used_altruists,
            retrieval_usage,
            transplant_usage,
            lab_usage,
            senior_usage,
            ped_usage,
            lane_usage,
            region_tx,
            net_export,
        )

    hs_deficit, ped_deficit, region_deficits = deficits()
    if hs_deficit or ped_deficit or any(v > 0 for v in region_deficits.values()):
        return []
    return selected


def candidate_key(candidate: CandidateDay) -> Tuple[str, Optional[str], str, Tuple[str, ...]]:
    return (candidate.kind, candidate.altruistic_donor_id, candidate.day_id, candidate.pair_sequence)


def solution_to_candidate_indices(candidates: Sequence[CandidateDay], solution: Dict[str, object]) -> List[int]:
    key_to_index = {candidate_key(candidate): idx for idx, candidate in enumerate(candidates)}
    indices: List[int] = []
    for cycle in solution.get("cycles", []):
        key = ("cycle", None, cycle["day_id"], tuple(cycle["pair_sequence"]))
        if key not in key_to_index:
            return []
        indices.append(key_to_index[key])
    for chain in solution.get("chains", []):
        key = (
            "chain",
            chain["altruistic_donor_id"],
            chain["start_day_id"],
            tuple(chain["pair_sequence"]),
        )
        if key not in key_to_index:
            return []
        indices.append(key_to_index[key])
    return sorted(indices)


def choose_neighborhood(
    candidates: Sequence[CandidateDay],
    incumbent_indices: Sequence[int],
    max_candidates: int = 3500,
    top_global: int = 1200,
) -> List[int]:
    incumbent_set = set(incumbent_indices)
    incumbent_pairs = set()
    incumbent_altruists = set()
    incumbent_days = set()
    for idx in incumbent_indices:
        candidate = candidates[idx]
        incumbent_pairs.update(candidate.pair_sequence)
        incumbent_days.add(candidate.day_id)
        if candidate.altruistic_donor_id is not None:
            incumbent_altruists.add(candidate.altruistic_donor_id)

    chosen = set(incumbent_indices)

    ranked = sorted(range(len(candidates)), key=lambda idx: candidates[idx].ranking_score, reverse=True)
    for idx in ranked[:top_global]:
        chosen.add(idx)

    for idx, candidate in enumerate(candidates):
        if len(chosen) >= max_candidates:
            break
        if candidate.altruistic_donor_id in incumbent_altruists:
            chosen.add(idx)
            continue
        if candidate.day_id in incumbent_days and any(pair in incumbent_pairs for pair in candidate.pair_sequence):
            chosen.add(idx)
            continue
        if candidate.hs_count or candidate.ped_count:
            chosen.add(idx)

    if len(chosen) < max_candidates:
        for idx in ranked:
            chosen.add(idx)
            if len(chosen) >= max_candidates:
                break

    return sorted(chosen)


def solve_restricted_neighborhood(
    data: Dict[str, object],
    candidates: Sequence[CandidateDay],
    incumbent_indices: Sequence[int],
    incumbent_objective: float,
    neighborhood_indices: Sequence[int],
    radius: int = 6,
    time_limit: Optional[float] = None,
) -> Dict[str, object]:
    subset = [candidates[idx] for idx in neighborhood_indices]
    global_to_local = {global_idx: local_idx for local_idx, global_idx in enumerate(neighborhood_indices)}
    incumbent_local = [global_to_local[idx] for idx in incumbent_indices if idx in global_to_local]
    incumbent_local_set = set(incumbent_local)

    model = Model("robust_kidney_exchange_neighborhood")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", float(data["config"]["mip_gap"]))
    model.setParam("limits/time", time_limit if time_limit is not None else get_time_limit(data["config"]))

    variables = [model.addVar(vtype="B", name=subset[idx].uid) for idx in range(len(subset))]
    tail_floor = model.addVar(lb=-model.infinity(), name="tail_floor")

    scenario_exprs = {}
    for scenario in data["scenarios"]:
        scenario_exprs[scenario.scenario_id] = quicksum(
            subset[idx].scenario_scores[scenario.scenario_id] * variables[idx]
            for idx in range(len(subset))
            if abs(subset[idx].scenario_scores[scenario.scenario_id]) > 1e-12
        )
        if scenario.scenario_id != "BASELINE":
            model.addCons(tail_floor <= scenario_exprs[scenario.scenario_id])

    objective_expr = (
        (1.0 - float(data["config"]["tail_risk_weight"]))
        * quicksum(
            scenario.probability * scenario_exprs[scenario.scenario_id]
            for scenario in data["scenarios"]
        )
        + float(data["config"]["tail_risk_weight"]) * tail_floor
    )
    model.setObjective(objective_expr, "maximize")
    model.addCons(objective_expr >= incumbent_objective + 1e-8)

    pair_to_terms = defaultdict(list)
    altruist_to_terms = defaultdict(list)
    retrieval_terms = defaultdict(list)
    transplant_terms = defaultdict(list)
    lab_terms = defaultdict(list)
    senior_terms = defaultdict(list)
    ped_terms = defaultdict(list)
    lane_terms = defaultdict(list)
    region_floor_terms = defaultdict(list)
    net_export_terms = defaultdict(list)
    chain_indices = []

    for idx, candidate in enumerate(subset):
        if candidate.kind == "chain":
            chain_indices.append(idx)
        for pair_id in candidate.pair_sequence:
            pair_to_terms[pair_id].append(idx)
        if candidate.altruistic_donor_id is not None:
            altruist_to_terms[candidate.altruistic_donor_id].append(idx)
        for region, count in candidate.region_counts:
            region_floor_terms[region].append((idx, count))
        for region, count in candidate.net_counts:
            net_export_terms[region].append((idx, count))
        for scheduled_link in candidate.scheduled_links:
            edge = scheduled_link.edge
            day_id = scheduled_link.surgery_day_id
            recipient_pair = data["pairs"][edge.recipient_pair_id]
            weak = edge.virtual_crossmatch_score < float(data["config"]["risky_crossmatch_threshold"])
            retrieval_terms[(edge.donor_hospital_id, day_id)].append(idx)
            transplant_terms[(edge.recipient_hospital_id, day_id)].append(idx)
            if weak:
                lab_terms[(edge.recipient_region, day_id)].append(idx)
            if (
                recipient_pair["priority_tier"] == "critical"
                or recipient_pair["cpra"] >= int(data["config"]["highly_sensitized_cpra"])
                or weak
            ):
                senior_terms[(edge.recipient_hospital_id, day_id)].append(idx)
            if recipient_pair["pediatric"]:
                ped_terms[(edge.recipient_hospital_id, day_id)].append(idx)
            if not edge.same_region:
                lane_terms[(edge.donor_region, edge.recipient_region, day_id)].append(idx)

    for indices in pair_to_terms.values():
        model.addCons(quicksum(variables[idx] for idx in indices) <= 1)
    for indices in altruist_to_terms.values():
        model.addCons(quicksum(variables[idx] for idx in indices) <= 1)
    model.addCons(quicksum(variables[idx] for idx in chain_indices) <= int(data["config"]["max_active_chains"]))

    for (hospital_id, day_id), indices in retrieval_terms.items():
        rhs = effective_capacity(
            int(data["hospitals"][hospital_id]["retrieval_capacity_per_day"]),
            float(data["slot_factor"][day_id]),
        )
        model.addCons(quicksum(variables[idx] for idx in indices) <= rhs)
    for (hospital_id, day_id), indices in transplant_terms.items():
        rhs = effective_capacity(
            int(data["hospitals"][hospital_id]["transplant_capacity_per_day"]),
            float(data["slot_factor"][day_id]),
        )
        model.addCons(quicksum(variables[idx] for idx in indices) <= rhs)
    for region in data["region_policy"]:
        for day_id in data["day_ids"]:
            rhs = effective_capacity(int(data["lab_base_by_region"][region]), float(data["slot_factor"][day_id]))
            indices = lab_terms.get((region, day_id), [])
            model.addCons(quicksum(variables[idx] for idx in indices) <= rhs)
    for (hospital_id, day_id), caps in data["teams"].items():
        model.addCons(quicksum(variables[idx] for idx in senior_terms.get((hospital_id, day_id), [])) <= int(caps["senior"]))
        model.addCons(quicksum(variables[idx] for idx in ped_terms.get((hospital_id, day_id), [])) <= int(caps["pediatric"]))
    for key, lane in data["lanes"].items():
        model.addCons(quicksum(variables[idx] for idx in lane_terms.get(key, [])) <= int(lane["cap"]))

    model.addCons(
        quicksum(subset[idx].hs_count * variables[idx] for idx in range(len(subset))) >= int(data["config"]["min_highly_sensitized_transplants"])
    )
    model.addCons(
        quicksum(subset[idx].ped_count * variables[idx] for idx in range(len(subset))) >= int(data["config"]["min_pediatric_transplants"])
    )
    for region, policy in data["region_policy"].items():
        model.addCons(
            quicksum(coeff * variables[idx] for idx, coeff in region_floor_terms.get(region, [])) >= int(policy["min_transplants"])
        )
        model.addCons(
            quicksum(coeff * variables[idx] for idx, coeff in net_export_terms.get(region, [])) <= int(policy["max_net_export_deficit"])
        )

    if incumbent_local:
        model.addCons(
            quicksum(1 - variables[idx] for idx in incumbent_local_set)
            + quicksum(variables[idx] for idx in range(len(subset)) if idx not in incumbent_local_set)
            <= radius
        )
        start_sol = model.createSol()
        for idx in incumbent_local:
            model.setSolVal(start_sol, variables[idx], 1.0)
        model.addSol(start_sol, free=True)

    start = time.time()
    model.optimize()
    solve_seconds = time.time() - start

    has_solution = model.getNSols() > 0 and model.getBestSol() is not None
    selected_global: List[int] = []
    objective_value = None
    best_bound = None
    gap = None
    if has_solution:
        sol = model.getBestSol()
        selected_global = [
            neighborhood_indices[idx]
            for idx, variable in enumerate(variables)
            if model.getSolVal(sol, variable) > 0.5
        ]
        objective_value = float(model.getObjVal())
        best_bound = float(model.getDualbound())
        gap = float(model.getGap())
    else:
        try:
            best_bound = float(model.getDualbound())
        except Exception:
            best_bound = None

    return {
        "status": str(model.getStatus()),
        "has_solution": has_solution,
        "selected_indices": selected_global,
        "objective_value": objective_value,
        "best_bound": best_bound,
        "gap": gap,
        "solve_seconds": solve_seconds,
        "subset_size": len(subset),
        "radius": radius,
    }


def build_and_solve(data: Dict[str, object]) -> Dict[str, object]:
    edge_map, outgoing_pruned, filtered_edge_count = build_feasible_edges(data)
    structures = enumerate_structures(data, edge_map, outgoing_pruned)
    (
        candidates,
        pair_to_candidates,
        altruist_to_candidates,
        retrieval_map,
        transplant_map,
        lab_map,
        senior_map,
        ped_team_map,
        lane_map,
        hs_counts,
        ped_counts,
        region_floor_map,
        net_export_map,
    ) = build_candidate_days(data, structures, edge_map)

    model = Model("robust_kidney_exchange")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", float(data["config"]["mip_gap"]))
    model.setParam("limits/time", get_time_limit(data["config"]))

    variables = [model.addVar(vtype="B", name=candidate.uid) for candidate in candidates]
    tail_floor = model.addVar(lb=-model.infinity(), name="tail_floor")

    scenario_exprs = {}
    for scenario in data["scenarios"]:
        scenario_exprs[scenario.scenario_id] = quicksum(
            candidate.scenario_scores[scenario.scenario_id] * variables[idx]
            for idx, candidate in enumerate(candidates)
            if abs(candidate.scenario_scores[scenario.scenario_id]) > 1e-12
        )
        if scenario.scenario_id != "BASELINE":
            model.addCons(
                tail_floor <= scenario_exprs[scenario.scenario_id],
                name=f"tail_floor[{scenario.scenario_id}]",
            )

    expected_expr = quicksum(
        scenario.probability * scenario_exprs[scenario.scenario_id]
        for scenario in data["scenarios"]
    )
    model.setObjective(
        (1.0 - float(data["config"]["tail_risk_weight"])) * expected_expr
        + float(data["config"]["tail_risk_weight"]) * tail_floor,
        "maximize",
    )

    for pair_id, indices in pair_to_candidates.items():
        model.addCons(quicksum(variables[idx] for idx in indices) <= 1, name=f"pair[{pair_id}]")

    for altruist_id, indices in altruist_to_candidates.items():
        model.addCons(quicksum(variables[idx] for idx in indices) <= 1, name=f"altruist[{altruist_id}]")

    chain_indices = [idx for idx, candidate in enumerate(candidates) if candidate.kind == "chain"]
    model.addCons(
        quicksum(variables[idx] for idx in chain_indices) <= int(data["config"]["max_active_chains"]),
        name="max_active_chains",
    )

    hospitals = data["hospitals"]
    slot_factor = data["slot_factor"]
    for (hospital_id, day_id), terms in retrieval_map.items():
        rhs = effective_capacity(int(hospitals[hospital_id]["retrieval_capacity_per_day"]), float(slot_factor[day_id]))
        model.addCons(quicksum(coeff * variables[idx] for idx, coeff in terms) <= rhs)

    for (hospital_id, day_id), terms in transplant_map.items():
        rhs = effective_capacity(int(hospitals[hospital_id]["transplant_capacity_per_day"]), float(slot_factor[day_id]))
        model.addCons(quicksum(coeff * variables[idx] for idx, coeff in terms) <= rhs)

    lab_base_by_region = defaultdict(int)
    for hospital in hospitals.values():
        lab_base_by_region[hospital["region"]] += int(hospital["lab_capacity_per_day"])
    for region in sorted(data["region_policy"]):
        for day_id in data["day_ids"]:
            rhs = effective_capacity(lab_base_by_region[region], float(slot_factor[day_id]))
            terms = lab_map.get((region, day_id), [])
            model.addCons(quicksum(coeff * variables[idx] for idx, coeff in terms) <= rhs)

    for (hospital_id, day_id), caps in data["teams"].items():
        senior_terms = senior_map.get((hospital_id, day_id), [])
        ped_terms = ped_team_map.get((hospital_id, day_id), [])
        model.addCons(quicksum(coeff * variables[idx] for idx, coeff in senior_terms) <= int(caps["senior"]))
        model.addCons(quicksum(coeff * variables[idx] for idx, coeff in ped_terms) <= int(caps["pediatric"]))

    for (donor_region, recipient_region, day_id), lane in data["lanes"].items():
        terms = lane_map.get((donor_region, recipient_region, day_id), [])
        model.addCons(quicksum(coeff * variables[idx] for idx, coeff in terms) <= int(lane["cap"]))

    model.addCons(
        quicksum(hs_counts[idx] * variables[idx] for idx in range(len(candidates)) if hs_counts[idx])
        >= int(data["config"]["min_highly_sensitized_transplants"])
    )
    model.addCons(
        quicksum(ped_counts[idx] * variables[idx] for idx in range(len(candidates)) if ped_counts[idx])
        >= int(data["config"]["min_pediatric_transplants"])
    )

    for region, policy in data["region_policy"].items():
        floor_terms = region_floor_map.get(region, [])
        net_terms = net_export_map.get(region, [])
        model.addCons(quicksum(coeff * variables[idx] for idx, coeff in floor_terms) >= int(policy["min_transplants"]))
        model.addCons(
            quicksum(coeff * variables[idx] for idx, coeff in net_terms) <= int(policy["max_net_export_deficit"])
        )

    greedy_indices = build_greedy_incumbent(candidates, data)
    if greedy_indices:
        sol = model.createSol()
        for idx in greedy_indices:
            model.setSolVal(sol, variables[idx], 1.0)
        model.trySol(sol, printreason=False, completely=True)

    start = time.time()
    model.optimize()
    solve_seconds = time.time() - start

    has_solution = model.getNSols() > 0 and model.getBestSol() is not None
    result = {
        "edge_count_filtered": filtered_edge_count,
        "structure_count": len(structures),
        "candidate_day_count": len(candidates),
        "status": str(model.getStatus()),
        "solve_seconds": solve_seconds,
        "has_solution": has_solution,
        "model": model,
        "variables": variables,
        "candidates": candidates,
        "objective_value": None,
        "best_bound": None,
        "gap": None,
    }
    if has_solution:
        result["objective_value"] = float(model.getObjVal())
        result["best_bound"] = float(model.getDualbound())
        result["gap"] = float(model.getGap())
    else:
        try:
            result["best_bound"] = float(model.getDualbound())
        except Exception:
            result["best_bound"] = None
    return result


def export_solution(candidates: Sequence[CandidateDay], selected_indices: Sequence[int]) -> Dict[str, object]:
    cycles = sorted(
        [candidates[idx] for idx in selected_indices if candidates[idx].kind == "cycle"],
        key=lambda candidate: (candidate.day_id, candidate.pair_sequence),
    )
    chains = sorted(
        [candidates[idx] for idx in selected_indices if candidates[idx].kind == "chain"],
        key=lambda candidate: (candidate.day_id, candidate.altruistic_donor_id, candidate.pair_sequence),
    )

    solution = {
        "cycles": [
            {
                "cycle_id": f"C{idx:03d}",
                "day_id": candidate.day_id,
                "pair_sequence": list(candidate.pair_sequence),
            }
            for idx, candidate in enumerate(cycles, start=1)
        ],
        "chains": [
            {
                "chain_id": f"{candidate.altruistic_donor_id}_CHAIN{idx}",
                "start_day_id": candidate.day_id,
                "altruistic_donor_id": candidate.altruistic_donor_id,
                "pair_sequence": list(candidate.pair_sequence),
            }
            for idx, candidate in enumerate(chains, start=1)
        ],
    }
    SOLUTION_PATH.write_text(json.dumps(solution, indent=2))
    return solution


def audit_solution(data: Dict[str, object], solution: Dict[str, object]) -> Dict[str, object]:
    config = data["config"]
    pairs = data["pairs"]
    altruists = data["altruists"]
    day_ids = data["day_ids"]
    day_index = data["day_index"]
    hospitals = data["hospitals"]
    teams = data["teams"]
    scenarios: List[Scenario] = data["scenarios"]

    edge_map = build_full_edge_map(data)

    errors: List[str] = []
    used_pairs: set[str] = set()
    used_altruists: set[str] = set()
    retrieval_usage = Counter()
    transplant_usage = Counter()
    lab_usage = Counter()
    senior_usage = Counter()
    ped_usage = Counter()
    lane_usage = Counter()
    hs_total = 0
    ped_total = 0
    region_tx = Counter()
    net_export = Counter()
    scenario_totals = {scenario.scenario_id: 0.0 for scenario in scenarios}

    def add_structure(
        kind: str,
        altruist_id: Optional[str],
        pair_sequence: Sequence[str],
        day_id: str,
        structure_name: str,
    ) -> None:
        nonlocal hs_total, ped_total
        for pair_id in pair_sequence:
            if pair_id not in pairs:
                errors.append(f"{structure_name} uses unknown pair {pair_id}")
                return
            if pair_id in used_pairs:
                errors.append(f"Pair {pair_id} used more than once")
            used_pairs.add(pair_id)
            pair = pairs[pair_id]
            if pair["cpra"] >= int(config["highly_sensitized_cpra"]):
                hs_total += 1
            if pair["pediatric"]:
                ped_total += 1
            region_tx[pair["region"]] += 1

        scheduled_links = scheduled_links_for_candidate(
            kind=kind,
            altruist_id=altruist_id,
            pair_sequence=pair_sequence,
            day_id=day_id,
            edge_map=edge_map,
            data=data,
        )
        for scheduled_link in scheduled_links:
            edge = scheduled_link.edge
            surgery_day = scheduled_link.surgery_day_id
            weak = edge.virtual_crossmatch_score < float(config["risky_crossmatch_threshold"])
            recipient_pair = pairs[edge.recipient_pair_id]
            retrieval_usage[(edge.donor_hospital_id, surgery_day)] += 1
            transplant_usage[(edge.recipient_hospital_id, surgery_day)] += 1
            if weak:
                lab_usage[(edge.recipient_region, surgery_day)] += 1
            if (
                recipient_pair["priority_tier"] == "critical"
                or recipient_pair["cpra"] >= int(config["highly_sensitized_cpra"])
                or weak
            ):
                senior_usage[(edge.recipient_hospital_id, surgery_day)] += 1
            if recipient_pair["pediatric"]:
                ped_usage[(edge.recipient_hospital_id, surgery_day)] += 1
            if not edge.same_region:
                lane_usage[(edge.donor_region, edge.recipient_region, surgery_day)] += 1
                net_export[edge.donor_region] += 1
                net_export[edge.recipient_region] -= 1

        scores = score_candidate_day(kind, altruist_id, pair_sequence, scheduled_links, data)
        for scenario_id, value in scores.items():
            scenario_totals[scenario_id] += value

    for cycle in solution.get("cycles", []):
        if set(cycle) != {"cycle_id", "day_id", "pair_sequence"}:
            errors.append(f"Invalid cycle schema: {cycle}")
            continue
        pair_sequence = cycle["pair_sequence"]
        day_id = cycle["day_id"]
        if day_id not in day_ids:
            errors.append(f"Unknown cycle day {day_id}")
            continue
        if not isinstance(pair_sequence, list) or len(pair_sequence) not in (2, 3):
            errors.append(f"Invalid cycle length in {cycle['cycle_id']}")
            continue
        if len(set(pair_sequence)) != len(pair_sequence):
            errors.append(f"Repeated pair in cycle {cycle['cycle_id']}")
            continue
        missing = [
            (pair_sequence[idx], pair_sequence[(idx + 1) % len(pair_sequence)])
            for idx in range(len(pair_sequence))
            if (pair_sequence[idx], pair_sequence[(idx + 1) % len(pair_sequence)]) not in edge_map
        ]
        if missing:
            errors.append(f"Cycle {cycle['cycle_id']} uses missing feasible arcs {missing}")
            continue
        add_structure("cycle", None, pair_sequence, day_id, cycle["cycle_id"])

    for chain in solution.get("chains", []):
        if set(chain) != {"chain_id", "start_day_id", "altruistic_donor_id", "pair_sequence"}:
            errors.append(f"Invalid chain schema: {chain}")
            continue
        altruist_id = chain["altruistic_donor_id"]
        day_id = chain["start_day_id"]
        pair_sequence = chain["pair_sequence"]
        if altruist_id not in altruists:
            errors.append(f"Unknown altruist {altruist_id}")
            continue
        if day_id not in day_ids:
            errors.append(f"Unknown chain start day {day_id}")
            continue
        if day_index[day_id] < day_index[altruists[altruist_id]["available_day"]]:
            errors.append(f"Chain {chain['chain_id']} starts before altruist availability")
            continue
        if altruist_id in used_altruists:
            errors.append(f"Altruist {altruist_id} used more than once")
        used_altruists.add(altruist_id)
        if not isinstance(pair_sequence, list) or not (1 <= len(pair_sequence) <= int(config["chain_max_length"])):
            errors.append(f"Invalid chain length in {chain['chain_id']}")
            continue
        if len(set(pair_sequence)) != len(pair_sequence):
            errors.append(f"Repeated pair in chain {chain['chain_id']}")
            continue
        max_offset = (len(pair_sequence) - 1) // int(config["chain_links_per_day"])
        if day_index[day_id] + max_offset >= len(day_ids):
            errors.append(f"Chain {chain['chain_id']} exceeds horizon")
            continue
        if (altruist_id, pair_sequence[0]) not in edge_map:
            errors.append(f"Chain {chain['chain_id']} is missing arc {(altruist_id, pair_sequence[0])}")
            continue
        missing = [
            (pair_sequence[idx], pair_sequence[idx + 1])
            for idx in range(len(pair_sequence) - 1)
            if (pair_sequence[idx], pair_sequence[idx + 1]) not in edge_map
        ]
        if missing:
            errors.append(f"Chain {chain['chain_id']} uses missing feasible arcs {missing}")
            continue
        add_structure("chain", altruist_id, pair_sequence, day_id, chain["chain_id"])

    slot_factor = data["slot_factor"]
    lab_base_by_region = defaultdict(int)
    for hospital in hospitals.values():
        lab_base_by_region[hospital["region"]] += int(hospital["lab_capacity_per_day"])

    for (hospital_id, day_id), usage in retrieval_usage.items():
        rhs = effective_capacity(int(hospitals[hospital_id]["retrieval_capacity_per_day"]), float(slot_factor[day_id]))
        if usage > rhs:
            errors.append(f"Retrieval capacity violated at {hospital_id},{day_id}: {usage}>{rhs}")
    for (hospital_id, day_id), usage in transplant_usage.items():
        rhs = effective_capacity(int(hospitals[hospital_id]["transplant_capacity_per_day"]), float(slot_factor[day_id]))
        if usage > rhs:
            errors.append(f"Transplant capacity violated at {hospital_id},{day_id}: {usage}>{rhs}")
    for (region, day_id), usage in lab_usage.items():
        rhs = effective_capacity(lab_base_by_region[region], float(slot_factor[day_id]))
        if usage > rhs:
            errors.append(f"Lab capacity violated at {region},{day_id}: {usage}>{rhs}")
    for (hospital_id, day_id), usage in senior_usage.items():
        rhs = int(teams[(hospital_id, day_id)]["senior"])
        if usage > rhs:
            errors.append(f"Senior-team capacity violated at {hospital_id},{day_id}: {usage}>{rhs}")
    for (hospital_id, day_id), usage in ped_usage.items():
        rhs = int(teams[(hospital_id, day_id)]["pediatric"])
        if usage > rhs:
            errors.append(f"Pediatric-team capacity violated at {hospital_id},{day_id}: {usage}>{rhs}")
    for key, usage in lane_usage.items():
        rhs = int(data["lanes"][key]["cap"])
        if usage > rhs:
            errors.append(f"Lane capacity violated at {key}: {usage}>{rhs}")

    if hs_total < int(config["min_highly_sensitized_transplants"]):
        errors.append(
            f"Highly sensitized floor violated: {hs_total}<{int(config['min_highly_sensitized_transplants'])}"
        )
    if ped_total < int(config["min_pediatric_transplants"]):
        errors.append(f"Pediatric floor violated: {ped_total}<{int(config['min_pediatric_transplants'])}")

    for region, policy in data["region_policy"].items():
        if region_tx[region] < int(policy["min_transplants"]):
            errors.append(f"Regional floor violated for {region}: {region_tx[region]}<{int(policy['min_transplants'])}")
        if net_export[region] > int(policy["max_net_export_deficit"]):
            errors.append(
                f"Net export violated for {region}: {net_export[region]}>{int(policy['max_net_export_deficit'])}"
            )

    expected_total = sum(
        scenario.probability * scenario_totals[scenario.scenario_id] for scenario in scenarios
    )
    stressed = [
        scenario_totals[scenario.scenario_id]
        for scenario in scenarios
        if scenario.scenario_id != "BASELINE"
    ]
    tail_floor = min(stressed) if stressed else expected_total
    objective = (
        (1.0 - float(config["tail_risk_weight"])) * expected_total
        + float(config["tail_risk_weight"]) * tail_floor
    )

    return {
        "ok": not errors,
        "errors": errors,
        "highly_sensitized_transplants": hs_total,
        "pediatric_transplants": ped_total,
        "region_transplants": dict(region_tx),
        "net_export": dict(net_export),
        "scenario_totals": scenario_totals,
        "objective": objective,
    }


def format_float(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.8f}"


def write_solve_log(lines: List[str]) -> None:
    (SUBMISSIONS_DIR / "solve_log.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    data = load_data()
    edge_map, outgoing_pruned, filtered_edge_count = build_feasible_edges(data)
    structures = enumerate_structures(data, edge_map, outgoing_pruned)
    (
        candidates,
        _pair_to_candidates,
        _altruist_to_candidates,
        _retrieval_map,
        _transplant_map,
        _lab_map,
        _senior_map,
        _ped_team_map,
        _lane_map,
        _hs_counts,
        _ped_counts,
        _region_floor_map,
        _net_export_map,
    ) = build_candidate_days(data, structures, edge_map)

    log_lines = [
        "# Solve Log",
        "",
        "## Commands",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Instance Summary",
        f"- Filtered feasible donor-recipient arcs: `{filtered_edge_count}`",
        f"- Enumerated structures after pruning/cap: `{len(structures)}`",
        f"- Day-specific candidate structures: `{len(candidates)}`",
        "",
        "## Independent Audit",
    ]

    incumbent_solution = None
    incumbent_audit = None
    incumbent_indices: List[int] = []
    if SOLUTION_PATH.exists():
        incumbent_solution = json.loads(SOLUTION_PATH.read_text())
        incumbent_audit = audit_solution(data, incumbent_solution)
        log_lines.append(f"- On-disk `solution.json` parsed successfully: `{incumbent_audit['ok']}`")
        log_lines.append(f"- Incumbent objective from independent audit: `{format_float(incumbent_audit['objective'])}`")
        log_lines.append(
            f"- Incumbent hard-check errors: `{0 if incumbent_audit['ok'] else len(incumbent_audit['errors'])}`"
        )
        if incumbent_audit["ok"]:
            incumbent_indices = solution_to_candidate_indices(candidates, incumbent_solution)
    else:
        log_lines.append("- No incumbent artifact was present on disk.")

    if incumbent_solution is None or incumbent_audit is None or not incumbent_audit["ok"] or not incumbent_indices:
        greedy_indices = build_greedy_incumbent(candidates, data)
        if not greedy_indices:
            write_solve_log(log_lines + ["", "## Result", "- No feasible incumbent could be constructed."])
            raise RuntimeError("No feasible incumbent available.")
        greedy_solution = export_solution(candidates, greedy_indices)
        greedy_audit = audit_solution(data, greedy_solution)
        if not greedy_audit["ok"]:
            write_solve_log(log_lines + ["", "## Result", "- Greedy incumbent failed independent audit."])
            raise RuntimeError("Greedy incumbent failed independent audit.")
        incumbent_solution = greedy_solution
        incumbent_audit = greedy_audit
        incumbent_indices = greedy_indices
        log_lines.append("- Rebuilt incumbent from greedy constructive heuristic because no valid on-disk warm start was usable.")

    portfolio = [
        {"max_candidates": 3500, "top_global": 1200, "radius": 6},
        {"max_candidates": 5500, "top_global": 2000, "radius": 8},
        {"max_candidates": 7000, "top_global": 2800, "radius": 10},
        {"max_candidates": 9000, "top_global": 3600, "radius": 12},
    ]
    published = False
    candidate_audit = None
    incumbent_objective = float(incumbent_audit["objective"])
    initial_incumbent_objective = incumbent_objective
    total_time_budget = min(180.0, get_time_limit(data["config"]))
    phase_start = time.time()
    neighborhood_result = {
        "status": "not_run",
        "best_bound": None,
        "gap": None,
        "solve_seconds": 0.0,
    }

    log_lines.extend(["", "## Performance Phase"])
    for attempt_no, settings in enumerate(portfolio, start=1):
        elapsed = time.time() - phase_start
        remaining = total_time_budget - elapsed
        if remaining <= 5.0:
            log_lines.append(f"- Stopped before attempt {attempt_no}: insufficient remaining solve budget.")
            break

        neighborhood_indices = choose_neighborhood(
            candidates,
            incumbent_indices,
            max_candidates=settings["max_candidates"],
            top_global=settings["top_global"],
        )
        time_limit = min(90.0, max(5.0, remaining))
        neighborhood_result = solve_restricted_neighborhood(
            data=data,
            candidates=candidates,
            incumbent_indices=incumbent_indices,
            incumbent_objective=incumbent_objective,
            neighborhood_indices=neighborhood_indices,
            radius=settings["radius"],
            time_limit=time_limit,
        )
        log_lines.extend(
            [
                f"- Attempt {attempt_no}: neighborhood `{settings['max_candidates']}` / radius `{settings['radius']}` / time limit `{format_float(time_limit)}`",
                f"- Attempt {attempt_no} status: `{neighborhood_result['status']}`",
                f"- Attempt {attempt_no} candidate objective: `{format_float(neighborhood_result['objective_value'])}`",
                f"- Attempt {attempt_no} best bound: `{format_float(neighborhood_result['best_bound'])}`",
                f"- Attempt {attempt_no} gap: `{format_float(neighborhood_result['gap'])}`",
                f"- Attempt {attempt_no} solve seconds: `{format_float(neighborhood_result['solve_seconds'])}`",
            ]
        )

        if neighborhood_result["has_solution"] and neighborhood_result["objective_value"] is not None:
            candidate_solution = export_solution(candidates, neighborhood_result["selected_indices"])
            candidate_audit = audit_solution(data, candidate_solution)
            log_lines.append(f"- Attempt {attempt_no} candidate audit ok: `{candidate_audit['ok']}`")
            log_lines.append(
                f"- Attempt {attempt_no} candidate audited objective: `{format_float(candidate_audit['objective'])}`"
            )
            improved = candidate_audit["ok"] and candidate_audit["objective"] > incumbent_objective + 1e-8
            if improved:
                published = True
                incumbent_solution = candidate_solution
                incumbent_audit = candidate_audit
                incumbent_indices = neighborhood_result["selected_indices"]
                incumbent_objective = float(candidate_audit["objective"])
                log_lines.append(f"- Attempt {attempt_no} accepted as new incumbent.")
                continue

        SOLUTION_PATH.write_text(json.dumps(incumbent_solution, indent=2))
        log_lines.append(f"- Attempt {attempt_no} rejected; rollback incumbent restored.")

    final_audit = audit_solution(data, json.loads(SOLUTION_PATH.read_text()))
    log_lines.extend(
        [
            "",
            "## Final Audit",
            f"- Final artifact objective: `{format_float(final_audit['objective'])}`",
            f"- Final audit ok: `{final_audit['ok']}`",
            f"- Final audit errors: `{0 if final_audit['ok'] else len(final_audit['errors'])}`",
            f"- Published changed artifact: `{published}`",
        ]
    )
    if not final_audit["ok"]:
        log_lines.append("- Final audit error list:")
        for error in final_audit["errors"]:
            log_lines.append(f"  - {error}")
        write_solve_log(log_lines)
        raise RuntimeError("Final artifact failed independent audit.")

    write_solve_log(log_lines)
    summary = {
        "objective": final_audit["objective"],
        "published": published,
        "incumbent_objective": initial_incumbent_objective,
        "candidate_objective": None if candidate_audit is None else candidate_audit["objective"],
        "status": neighborhood_result["status"],
        "best_bound": neighborhood_result["best_bound"],
        "gap": neighborhood_result["gap"],
        "solve_seconds": neighborhood_result["solve_seconds"],
        "candidate_day_count": len(candidates),
        "structure_count": len(structures),
    }
    (SUBMISSIONS_DIR / "last_run_summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
