#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from pyscipopt import Model, quicksum


DATA_DIR = "/app/data"
SUBMISSIONS_DIR = "/app/submissions"
OUTPUT_CSV = os.path.join(SUBMISSIONS_DIR, "initial_plan.csv")
LOG_PATH = os.path.join(SUBMISSIONS_DIR, "solve_log_initial.md")
MODEL_NOTE_PATH = os.path.join(SUBMISSIONS_DIR, "model_initial.md")

PLAN_FIELDS = [
    "exchange_id",
    "exchange_type",
    "position",
    "day_id",
    "altruist_id",
    "pair_id",
]
PANEL_TYPES = ("single_antigen", "flow_cytometry", "desensitization")


def read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: str, rows: List[Dict[str, str]], fieldnames: Sequence[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def format_float(value: float) -> str:
    return f"{value:.8f}"


def freeze_map(mapping: Dict[Tuple[str, ...], int]) -> Tuple[Tuple[Tuple[str, ...], int], ...]:
    return tuple(sorted(mapping.items()))


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    exchange_type: str
    altruist_id: str
    pairs: Tuple[str, ...]
    days: Tuple[str, ...]
    score: float
    components: Tuple[Tuple[str, float], ...]
    recipient_usage: Tuple[Tuple[Tuple[str, str], int], ...]
    retrieval_usage: Tuple[Tuple[Tuple[str, str], int], ...]
    lab_usage: Tuple[Tuple[Tuple[str, str], int], ...]
    panel_usage: Tuple[Tuple[Tuple[str, str, str], int], ...]
    panel_high_usage: Tuple[Tuple[Tuple[str, str, str], int], ...]
    bridge_usage: Tuple[Tuple[Tuple[str, str], int], ...]
    remote_usage: Tuple[Tuple[Tuple[str, str], int], ...]
    region_recipients: Tuple[Tuple[str, int], ...]
    region_sensitized: Tuple[Tuple[str, int], ...]
    region_exports: Tuple[Tuple[str, int], ...]
    region_imports: Tuple[Tuple[str, int], ...]

    def usage_map(self, attribute: str):
        return dict(getattr(self, attribute))

    @property
    def signature(self) -> Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]:
        return (self.exchange_type, self.altruist_id, self.pairs, self.days)


class KidneyExchangeSolver:
    def __init__(self) -> None:
        with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as handle:
            self.config = json.load(handle)
        with open(os.path.join(DATA_DIR, "current_wave.json"), encoding="utf-8") as handle:
            self.current_wave = int(json.load(handle)["current_wave"])

        self.days = read_csv(os.path.join(DATA_DIR, "planning_days.csv"))
        self.day_index = {row["day_id"]: int(row["day_index"]) for row in self.days}
        self.days_sorted = sorted(self.day_index, key=self.day_index.get)
        self.day_position = {day_id: idx for idx, day_id in enumerate(self.days_sorted)}

        self.hospitals = {
            row["hospital_id"]: row for row in read_csv(os.path.join(DATA_DIR, "hospitals.csv"))
        }
        self.pairs = {
            row["pair_id"]: row
            for row in read_csv(os.path.join(DATA_DIR, "patient_pairs.csv"))
            if int(row["reveal_wave"]) <= self.current_wave <= int(row["expiry_wave"])
        }
        self.altruists = {
            row["altruist_id"]: row
            for row in read_csv(os.path.join(DATA_DIR, "altruistic_donors.csv"))
            if int(row["reveal_wave"]) <= self.current_wave
        }
        self.edges = [
            row
            for row in read_csv(os.path.join(DATA_DIR, "compatibility_edges.csv"))
            if int(row["reveal_wave"]) <= self.current_wave
        ]
        self.edge_map = {(row["donor_id"], row["recipient_pair_id"]): row for row in self.edges}
        self.outgoing = defaultdict(list)
        for edge in self.edges:
            self.outgoing[edge["donor_id"]].append(edge["recipient_pair_id"])

        self.hospital_capacity = {
            (row["day_id"], row["hospital_id"]): row
            for row in read_csv(os.path.join(DATA_DIR, "hospital_day_capacity.csv"))
            if int(row["wave"]) == self.current_wave
        }
        self.panel_capacity = {
            (row["day_id"], row["recipient_hospital_id"], row["crossmatch_panel"]): row
            for row in read_csv(os.path.join(DATA_DIR, "crossmatch_panel_capacity.csv"))
            if int(row["wave"]) == self.current_wave
        }
        self.bridge_capacity = {
            (row["day_id"], row["region"]): row
            for row in read_csv(os.path.join(DATA_DIR, "chain_bridge_capacity.csv"))
            if int(row["wave"]) == self.current_wave
        }
        self.blackouts = {
            (row["day_id"], row["donor_hospital_id"], row["recipient_region"])
            for row in read_csv(os.path.join(DATA_DIR, "courier_blackouts.csv"))
            if row.get("wave") and int(row["wave"]) == self.current_wave
        }
        self.region_policy = {
            row["region"]: row
            for row in read_csv(os.path.join(DATA_DIR, "region_policy.csv"))
            if int(row["wave"]) == self.current_wave
        }
        self.regions = sorted(self.region_policy)
        self.cpra_threshold = float(self.config["sensitized_cpra_threshold"])

    def pair_bonus(self, pair_id: str) -> float:
        pair = self.pairs[pair_id]
        bonus = 0.15 * float(pair["waiting_months"])
        if int(pair["pediatric"]):
            bonus += 12.0
        if pair["priority_tier"] == "urgent":
            bonus += 18.0
        elif pair["priority_tier"] == "high":
            bonus += 10.0
        if float(pair["cpra"]) >= self.cpra_threshold:
            bonus += 6.0
        return bonus

    def link_score(self, donor_id: str, recipient_id: str) -> float:
        edge = self.edge_map[(donor_id, recipient_id)]
        return (
            float(edge["clinical_value"])
            + 12.0 * float(edge["virtual_crossmatch_score"])
            - 38.0 * float(edge["failure_probability"])
            - 1.4 * float(edge["travel_hours"])
            - 5.0 * int(edge["late_courier_required"])
            - 1.75 * int(edge["lab_review_required"])
        )

    def edge_day_feasible(self, donor_id: str, recipient_id: str, day_id: str) -> bool:
        edge = self.edge_map.get((donor_id, recipient_id))
        if edge is None or day_id not in self.day_index:
            return False

        recipient = self.pairs.get(recipient_id)
        if recipient is None:
            return False
        if self.day_index[day_id] < self.day_index[recipient["earliest_surgery_day"]]:
            return False

        if donor_id.startswith("P"):
            donor_pair = self.pairs.get(donor_id)
            if donor_pair is None:
                return False
            if self.day_index[day_id] < self.day_index[donor_pair["earliest_surgery_day"]]:
                return False
        else:
            altruist = self.altruists.get(donor_id)
            if altruist is None:
                return False
            if self.day_index[day_id] < self.day_index[altruist["first_available_day"]]:
                return False

        if float(edge["travel_hours"]) > float(recipient["max_courier_hours"]) + 1e-9:
            return False
        if int(edge["late_courier_required"]) and int(
            self.hospitals[edge["donor_hospital_id"]]["late_courier_ok"]
        ) == 0:
            return False
        if (day_id, edge["donor_hospital_id"], edge["recipient_region"]) in self.blackouts:
            return False
        return True

    def cycle_canonical(self, cycle: Tuple[str, ...]) -> Tuple[str, ...]:
        rotations = [cycle[idx:] + cycle[:idx] for idx in range(len(cycle))]
        return min(rotations)

    def build_candidate(
        self,
        candidate_id: str,
        exchange_type: str,
        pairs: List[str],
        days: List[str],
        strict_bridge: bool,
        altruist_id: str = "",
    ) -> Candidate:
        recipient_usage: Dict[Tuple[str, str], int] = defaultdict(int)
        retrieval_usage: Dict[Tuple[str, str], int] = defaultdict(int)
        lab_usage: Dict[Tuple[str, str], int] = defaultdict(int)
        panel_usage: Dict[Tuple[str, str, str], int] = defaultdict(int)
        panel_high_usage: Dict[Tuple[str, str, str], int] = defaultdict(int)
        bridge_usage: Dict[Tuple[str, str], int] = defaultdict(int)
        remote_usage: Dict[Tuple[str, str], int] = defaultdict(int)
        region_recipients: Dict[str, int] = defaultdict(int)
        region_sensitized: Dict[str, int] = defaultdict(int)
        region_exports: Dict[str, int] = defaultdict(int)
        region_imports: Dict[str, int] = defaultdict(int)
        components: Dict[str, float] = defaultdict(float)

        for pair_id in pairs:
            pair = self.pairs[pair_id]
            region_recipients[pair["region"]] += 1
            if float(pair["cpra"]) >= self.cpra_threshold:
                region_sensitized[pair["region"]] += 1
            components["recipient_bonus"] += self.pair_bonus(pair_id)

        if exchange_type == "cycle":
            day_id = days[0]
            for idx, pair_id in enumerate(pairs):
                donor_id = pairs[idx - 1]
                edge = self.edge_map[(donor_id, pair_id)]
                recipient_usage[(day_id, self.pairs[pair_id]["hospital_id"])] += 1
                retrieval_usage[(day_id, self.pairs[donor_id]["hospital_id"])] += 1
                region_exports[edge["donor_region"]] += 1
                region_imports[edge["recipient_region"]] += 1
                components["link_value"] += self.link_score(donor_id, pair_id)
                if int(edge["lab_review_required"]):
                    hospital_id = self.pairs[pair_id]["hospital_id"]
                    panel = edge["crossmatch_panel"]
                    lab_usage[(day_id, hospital_id)] += 1
                    if panel in PANEL_TYPES:
                        panel_usage[(day_id, hospital_id, panel)] += 1
                        if float(self.pairs[pair_id]["cpra"]) >= self.cpra_threshold:
                            panel_high_usage[(day_id, hospital_id, panel)] += 1
        else:
            components["chain_start_value"] += float(self.altruists[altruist_id]["chain_start_value"])
            for idx, pair_id in enumerate(pairs):
                day_id = days[idx]
                donor_id = altruist_id if idx == 0 else pairs[idx - 1]
                edge = self.edge_map[(donor_id, pair_id)]
                recipient_usage[(day_id, self.pairs[pair_id]["hospital_id"])] += 1
                if idx == 0:
                    retrieval_usage[(day_id, self.altruists[altruist_id]["hospital_id"])] += 1
                else:
                    retrieval_usage[(day_id, self.pairs[pairs[idx - 1]]["hospital_id"])] += 1
                region_exports[edge["donor_region"]] += 1
                region_imports[edge["recipient_region"]] += 1
                components["link_value"] += self.link_score(donor_id, pair_id)
                if int(edge["lab_review_required"]):
                    hospital_id = self.pairs[pair_id]["hospital_id"]
                    panel = edge["crossmatch_panel"]
                    lab_usage[(day_id, hospital_id)] += 1
                    if panel in PANEL_TYPES:
                        panel_usage[(day_id, hospital_id, panel)] += 1
                        if float(self.pairs[pair_id]["cpra"]) >= self.cpra_threshold:
                            panel_high_usage[(day_id, hospital_id, panel)] += 1
                if idx > 0:
                    previous_day = days[idx - 1]
                    use_bridge = strict_bridge or self.day_index[day_id] > self.day_index[previous_day]
                    if use_bridge:
                        region = self.pairs[pair_id]["region"]
                        bridge_usage[(day_id, region)] += 1
                        if self.pairs[pairs[idx - 1]]["region"] != region:
                            remote_usage[(day_id, region)] += 1
                    if self.day_index[day_id] > self.day_index[previous_day]:
                        components["bridge_penalty"] -= float(self.pairs[pairs[idx - 1]]["bridge_penalty"])

        return Candidate(
            candidate_id=candidate_id,
            exchange_type=exchange_type,
            altruist_id=altruist_id,
            pairs=tuple(pairs),
            days=tuple(days),
            score=sum(components.values()),
            components=tuple(sorted(components.items())),
            recipient_usage=freeze_map(recipient_usage),
            retrieval_usage=freeze_map(retrieval_usage),
            lab_usage=freeze_map(lab_usage),
            panel_usage=freeze_map(panel_usage),
            panel_high_usage=freeze_map(panel_high_usage),
            bridge_usage=freeze_map(bridge_usage),
            remote_usage=freeze_map(remote_usage),
            region_recipients=tuple(sorted(region_recipients.items())),
            region_sensitized=tuple(sorted(region_sensitized.items())),
            region_exports=tuple(sorted(region_exports.items())),
            region_imports=tuple(sorted(region_imports.items())),
        )

    def enumerate_candidates(self, strict_bridge: bool) -> List[Candidate]:
        candidates: List[Candidate] = []
        candidate_id = 0

        seen_two_cycles = set()
        for first in sorted(self.pairs):
            for second in self.outgoing.get(first, []):
                if second not in self.pairs or first == second:
                    continue
                cycle_key = tuple(sorted((first, second)))
                if cycle_key in seen_two_cycles or (second, first) not in self.edge_map:
                    continue
                seen_two_cycles.add(cycle_key)
                cycle = [cycle_key[0], cycle_key[1]]
                for day_id in self.days_sorted:
                    if all(self.edge_day_feasible(cycle[idx - 1], pair_id, day_id) for idx, pair_id in enumerate(cycle)):
                        candidate_id += 1
                        candidates.append(
                            self.build_candidate(
                                candidate_id=f"cand_{candidate_id:05d}",
                                exchange_type="cycle",
                                pairs=cycle,
                                days=[day_id, day_id],
                                strict_bridge=strict_bridge,
                            )
                        )

        seen_three_cycles = set()
        for first in sorted(self.pairs):
            for second in self.outgoing.get(first, []):
                if second not in self.pairs or second == first:
                    continue
                for third in self.outgoing.get(second, []):
                    if third not in self.pairs or third in {first, second}:
                        continue
                    if (third, first) not in self.edge_map:
                        continue
                    canonical = self.cycle_canonical((first, second, third))
                    if canonical in seen_three_cycles:
                        continue
                    seen_three_cycles.add(canonical)
                    cycle = list(canonical)
                    for day_id in self.days_sorted:
                        if all(self.edge_day_feasible(cycle[idx - 1], pair_id, day_id) for idx, pair_id in enumerate(cycle)):
                            candidate_id += 1
                            candidates.append(
                                self.build_candidate(
                                    candidate_id=f"cand_{candidate_id:05d}",
                                    exchange_type="cycle",
                                    pairs=cycle,
                                    days=[day_id] * len(cycle),
                                    strict_bridge=strict_bridge,
                                )
                            )

        max_chain_length = int(self.config["max_chain_length"])
        for altruist_id in sorted(self.altruists):
            def dfs(path: List[str]) -> None:
                nonlocal candidate_id
                if path:
                    def recurse(position: int, previous_day: Optional[str], days: List[str]) -> None:
                        nonlocal candidate_id
                        if position == len(path):
                            candidate_id += 1
                            candidates.append(
                                self.build_candidate(
                                    candidate_id=f"cand_{candidate_id:05d}",
                                    exchange_type="chain",
                                    pairs=path,
                                    days=days,
                                    strict_bridge=strict_bridge,
                                    altruist_id=altruist_id,
                                )
                            )
                            return

                        donor_id = altruist_id if position == 0 else path[position - 1]
                        recipient_id = path[position]
                        for day_id in self.days_sorted:
                            if previous_day is not None:
                                if self.day_index[day_id] < self.day_index[previous_day]:
                                    continue
                                if self.day_index[day_id] - self.day_index[previous_day] > 1:
                                    continue
                            if self.edge_day_feasible(donor_id, recipient_id, day_id):
                                recurse(position + 1, day_id, days + [day_id])

                    recurse(0, None, [])

                if len(path) == max_chain_length:
                    return
                tail = altruist_id if not path else path[-1]
                for next_pair in self.outgoing.get(tail, []):
                    if next_pair not in self.pairs or next_pair in path:
                        continue
                    dfs(path + [next_pair])

            dfs([])

        return candidates

    def capacity_limit(self, usage_type: str, key: Tuple[str, ...]) -> int:
        if usage_type == "recipient":
            return int(self.hospital_capacity[(key[0], key[1])]["recipient_transplant_slots"])
        if usage_type == "retrieval":
            return int(self.hospital_capacity[(key[0], key[1])]["donor_retrieval_slots"])
        if usage_type == "lab":
            return int(self.hospital_capacity[(key[0], key[1])]["lab_review_slots"])
        if usage_type == "panel":
            return int(self.panel_capacity[(key[0], key[1], key[2])]["max_reviews"])
        if usage_type == "panel_high":
            return int(self.panel_capacity[(key[0], key[1], key[2])]["high_cpra_review_limit"])
        if usage_type == "bridge":
            return int(self.bridge_capacity[(key[0], key[1])]["max_bridge_positions"])
        if usage_type == "remote":
            return int(self.bridge_capacity[(key[0], key[1])]["max_remote_handoffs"])
        raise KeyError(usage_type)

    def region_limit_ok(self, selected: Sequence[Candidate], trial: Candidate) -> bool:
        exports = defaultdict(int)
        imports = defaultdict(int)
        for candidate in list(selected) + [trial]:
            for region, value in candidate.usage_map("region_exports").items():
                exports[region] += value
            for region, value in candidate.usage_map("region_imports").items():
                imports[region] += value
        for region in self.regions:
            if exports[region] - imports[region] > int(self.region_policy[region]["max_net_export"]):
                return False
        return True

    def candidate_fits(self, selected: Sequence[Candidate], trial: Candidate) -> bool:
        selected_pairs = {pair_id for candidate in selected for pair_id in candidate.pairs}
        if any(pair_id in selected_pairs for pair_id in trial.pairs):
            return False
        if trial.altruist_id and any(candidate.altruist_id == trial.altruist_id for candidate in selected):
            return False
        if trial.exchange_type == "chain":
            active_chains = sum(1 for candidate in selected if candidate.exchange_type == "chain")
            if active_chains + 1 > int(self.config["max_active_chain_starts"]):
                return False

        for usage_type, attribute in (
            ("recipient", "recipient_usage"),
            ("retrieval", "retrieval_usage"),
            ("lab", "lab_usage"),
            ("panel", "panel_usage"),
            ("panel_high", "panel_high_usage"),
            ("bridge", "bridge_usage"),
            ("remote", "remote_usage"),
        ):
            usage = defaultdict(int)
            for candidate in selected:
                for key, value in candidate.usage_map(attribute).items():
                    usage[key] += value
            for key, value in trial.usage_map(attribute).items():
                usage[key] += value
                if usage[key] > self.capacity_limit(usage_type, key):
                    return False

        return self.region_limit_ok(selected, trial)

    def rows_to_exchange_signatures(
        self, rows: Sequence[Dict[str, str]]
    ) -> List[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]]:
        exchanges = defaultdict(list)
        for row in rows:
            exchanges[row["exchange_id"]].append(row)
        signatures = []
        for exchange_rows in exchanges.values():
            ordered = sorted(exchange_rows, key=lambda row: int(row["position"]))
            exchange_type = ordered[0]["exchange_type"]
            altruist_id = ordered[0]["altruist_id"] if exchange_type == "chain" else ""
            pairs = tuple(row["pair_id"] for row in ordered)
            days = tuple(row["day_id"] for row in ordered)
            signatures.append((exchange_type, altruist_id, pairs, days))
        signatures.sort()
        return signatures

    def selected_to_rows(self, selected: Sequence[Candidate]) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        selected_sorted = sorted(
            selected,
            key=lambda candidate: (
                min(self.day_position[day] for day in candidate.days),
                0 if candidate.exchange_type == "chain" else 1,
                candidate.signature,
            ),
        )
        chain_count = 0
        cycle_count = 0
        for candidate in selected_sorted:
            if candidate.exchange_type == "chain":
                chain_count += 1
                exchange_id = f"chain_{chain_count:02d}"
            else:
                cycle_count += 1
                exchange_id = f"cycle_{cycle_count:02d}"
            for position, (pair_id, day_id) in enumerate(zip(candidate.pairs, candidate.days), start=1):
                rows.append(
                    {
                        "exchange_id": exchange_id,
                        "exchange_type": candidate.exchange_type,
                        "position": str(position),
                        "day_id": day_id,
                        "altruist_id": candidate.altruist_id if candidate.exchange_type == "chain" else "",
                        "pair_id": pair_id,
                    }
                )
        return rows

    def audit_rows(
        self,
        rows: Sequence[Dict[str, str]],
        strict_bridge: bool,
        source_label: str,
    ) -> Dict[str, object]:
        metrics: Dict[str, object] = {
            "source": source_label,
            "bridge_mode": "strict" if strict_bridge else "public",
            "artifact_present": int(os.path.exists(OUTPUT_CSV)),
            "schema_ok": 1,
            "theta": 0.0,
            "violated_groups": 0,
            "integrality_residual": 0.0,
            "objective": 0.0,
            "transplants": 0,
            "exchange_count": 0,
        }
        violations: Dict[str, float] = defaultdict(float)

        usage_recipient = defaultdict(int)
        usage_retrieval = defaultdict(int)
        usage_lab = defaultdict(int)
        usage_panel = defaultdict(int)
        usage_panel_high = defaultdict(int)
        usage_bridge = defaultdict(int)
        usage_remote = defaultdict(int)
        region_recipients = defaultdict(int)
        region_sensitized = defaultdict(int)
        region_exports = defaultdict(int)
        region_imports = defaultdict(int)
        pair_seen: Set[str] = set()
        altruist_seen: Set[str] = set()

        def register(group: str, residual: float) -> None:
            if residual > 1e-9:
                violations[group] = max(violations[group], residual)

        exchanges = defaultdict(list)
        for row in rows:
            if list(row.keys()) != PLAN_FIELDS:
                metrics["schema_ok"] = 0
                register("schema_header", 1.0)
            exchanges[row["exchange_id"]].append(row)
            if row["exchange_type"] not in {"cycle", "chain"}:
                register("exchange_type_domain", 1.0)
            if row["day_id"] not in self.day_index:
                register("day_domain", 1.0)
            if row["pair_id"] not in self.pairs:
                register("pair_domain", 1.0)

        metrics["exchange_count"] = len(exchanges)

        for exchange_rows in exchanges.values():
            ordered = sorted(exchange_rows, key=lambda row: int(row["position"]))
            positions = [int(row["position"]) for row in ordered]
            if positions != list(range(1, len(ordered) + 1)):
                register("position_contiguity", 1.0)

            exchange_type = ordered[0]["exchange_type"]
            if any(row["exchange_type"] != exchange_type for row in ordered):
                register("exchange_type_consistency", 1.0)
                continue

            pairs = [row["pair_id"] for row in ordered]
            days = [row["day_id"] for row in ordered]
            if len(set(pairs)) != len(pairs):
                register("pair_uniqueness_within_exchange", 1.0)

            for pair_id in pairs:
                if pair_id not in self.pairs:
                    continue
                if pair_id in pair_seen:
                    register("pair_uniqueness_global", 1.0)
                pair_seen.add(pair_id)
                region_recipients[self.pairs[pair_id]["region"]] += 1
                if float(self.pairs[pair_id]["cpra"]) >= self.cpra_threshold:
                    region_sensitized[self.pairs[pair_id]["region"]] += 1
                metrics["objective"] = float(metrics["objective"]) + self.pair_bonus(pair_id)

            if exchange_type == "cycle":
                if len(ordered) not in (2, 3):
                    register("cycle_length", 1.0)
                if any(row["altruist_id"] for row in ordered):
                    register("cycle_altruist_blank", 1.0)
                if len(set(days)) != 1:
                    register("cycle_day_consistency", 1.0)
                for idx, pair_id in enumerate(pairs):
                    if pair_id not in self.pairs or days[idx] not in self.day_index:
                        continue
                    donor_id = pairs[idx - 1]
                    day_id = days[idx]
                    if not self.edge_day_feasible(donor_id, pair_id, day_id):
                        register("cycle_edge_feasibility", 1.0)
                        continue
                    edge = self.edge_map[(donor_id, pair_id)]
                    usage_recipient[(day_id, self.pairs[pair_id]["hospital_id"])] += 1
                    usage_retrieval[(day_id, self.pairs[donor_id]["hospital_id"])] += 1
                    region_exports[edge["donor_region"]] += 1
                    region_imports[edge["recipient_region"]] += 1
                    metrics["objective"] = float(metrics["objective"]) + self.link_score(donor_id, pair_id)
                    if int(edge["lab_review_required"]):
                        hospital_id = self.pairs[pair_id]["hospital_id"]
                        panel = edge["crossmatch_panel"]
                        usage_lab[(day_id, hospital_id)] += 1
                        if panel in PANEL_TYPES:
                            usage_panel[(day_id, hospital_id, panel)] += 1
                            if float(self.pairs[pair_id]["cpra"]) >= self.cpra_threshold:
                                usage_panel_high[(day_id, hospital_id, panel)] += 1

            elif exchange_type == "chain":
                altruist_ids = {row["altruist_id"] for row in ordered}
                if len(altruist_ids) != 1 or "" in altruist_ids:
                    register("chain_altruist_consistency", 1.0)
                    continue
                altruist_id = ordered[0]["altruist_id"]
                if altruist_id not in self.altruists:
                    register("chain_altruist_domain", 1.0)
                    continue
                if altruist_id in altruist_seen:
                    register("altruist_uniqueness", 1.0)
                altruist_seen.add(altruist_id)
                if len(ordered) > int(self.config["max_chain_length"]):
                    register("chain_length", 1.0)

                metrics["objective"] = float(metrics["objective"]) + float(
                    self.altruists[altruist_id]["chain_start_value"]
                )
                for idx, pair_id in enumerate(pairs):
                    if pair_id not in self.pairs or days[idx] not in self.day_index:
                        continue
                    donor_id = altruist_id if idx == 0 else pairs[idx - 1]
                    day_id = days[idx]
                    if idx > 0:
                        previous_day = days[idx - 1]
                        if self.day_index[day_id] < self.day_index[previous_day]:
                            register("chain_day_order", 1.0)
                        if self.day_index[day_id] - self.day_index[previous_day] > 1:
                            register("chain_day_gap", 1.0)
                    if not self.edge_day_feasible(donor_id, pair_id, day_id):
                        register("chain_edge_feasibility", 1.0)
                        continue
                    edge = self.edge_map[(donor_id, pair_id)]
                    usage_recipient[(day_id, self.pairs[pair_id]["hospital_id"])] += 1
                    if idx == 0:
                        usage_retrieval[(day_id, self.altruists[altruist_id]["hospital_id"])] += 1
                    else:
                        usage_retrieval[(day_id, self.pairs[pairs[idx - 1]]["hospital_id"])] += 1
                    region_exports[edge["donor_region"]] += 1
                    region_imports[edge["recipient_region"]] += 1
                    metrics["objective"] = float(metrics["objective"]) + self.link_score(donor_id, pair_id)
                    if int(edge["lab_review_required"]):
                        hospital_id = self.pairs[pair_id]["hospital_id"]
                        panel = edge["crossmatch_panel"]
                        usage_lab[(day_id, hospital_id)] += 1
                        if panel in PANEL_TYPES:
                            usage_panel[(day_id, hospital_id, panel)] += 1
                            if float(self.pairs[pair_id]["cpra"]) >= self.cpra_threshold:
                                usage_panel_high[(day_id, hospital_id, panel)] += 1
                    if idx > 0:
                        previous_day = days[idx - 1]
                        use_bridge = strict_bridge or self.day_index[day_id] > self.day_index[previous_day]
                        if use_bridge:
                            region = self.pairs[pair_id]["region"]
                            usage_bridge[(day_id, region)] += 1
                            if self.pairs[pairs[idx - 1]]["region"] != region:
                                usage_remote[(day_id, region)] += 1
                        if self.day_index[day_id] > self.day_index[previous_day]:
                            metrics["objective"] = float(metrics["objective"]) - float(
                                self.pairs[pairs[idx - 1]]["bridge_penalty"]
                            )
            else:
                register("exchange_type_domain", 1.0)

        active_chains = sum(
            1 for exchange_rows in exchanges.values() if exchange_rows and exchange_rows[0]["exchange_type"] == "chain"
        )
        if active_chains > int(self.config["max_active_chain_starts"]):
            register(
                "max_active_chain_starts",
                (active_chains - int(self.config["max_active_chain_starts"]))
                / max(1, int(self.config["max_active_chain_starts"])),
            )

        for key, usage in usage_recipient.items():
            limit = int(self.hospital_capacity[key]["recipient_transplant_slots"])
            register("recipient_capacity", max(0.0, usage - limit) / max(1, limit))
        for key, usage in usage_retrieval.items():
            limit = int(self.hospital_capacity[key]["donor_retrieval_slots"])
            register("retrieval_capacity", max(0.0, usage - limit) / max(1, limit))
        for key, usage in usage_lab.items():
            limit = int(self.hospital_capacity[key]["lab_review_slots"])
            register("lab_capacity", max(0.0, usage - limit) / max(1, limit))
        for key, usage in usage_panel.items():
            limit = int(self.panel_capacity[key]["max_reviews"])
            register("panel_capacity", max(0.0, usage - limit) / max(1, limit))
        for key, usage in usage_panel_high.items():
            limit = int(self.panel_capacity[key]["high_cpra_review_limit"])
            register("panel_high_capacity", max(0.0, usage - limit) / max(1, limit))
        for key, usage in usage_bridge.items():
            limit = int(self.bridge_capacity[key]["max_bridge_positions"])
            register("bridge_capacity", max(0.0, usage - limit) / max(1, limit))
        for key, usage in usage_remote.items():
            limit = int(self.bridge_capacity[key]["max_remote_handoffs"])
            register("remote_capacity", max(0.0, usage - limit) / max(1, limit))

        for region in self.regions:
            min_transplants = int(self.region_policy[region]["min_transplants"])
            sensitized_floor = int(self.region_policy[region]["sensitized_floor"])
            max_net_export = int(self.region_policy[region]["max_net_export"])
            register(
                f"region_floor[{region}]",
                max(0.0, min_transplants - region_recipients[region]) / max(1, min_transplants),
            )
            register(
                f"sensitized_floor[{region}]",
                max(0.0, sensitized_floor - region_sensitized[region]) / max(1, sensitized_floor),
            )
            register(
                f"net_export[{region}]",
                max(0.0, region_exports[region] - region_imports[region] - max_net_export)
                / max(1, max_net_export),
            )

        metrics["transplants"] = len(pair_seen)
        metrics["pair_coverage"] = f"{len(pair_seen)}/{len(self.pairs)}"
        metrics["altruist_coverage"] = f"{len(altruist_seen)}/{len(self.altruists)}"
        metrics["theta"] = max(violations.values(), default=0.0)
        metrics["violated_groups"] = len(violations)
        metrics["violations"] = dict(sorted(violations.items()))
        return metrics

    def add_usage_constraints(self, model: Model, candidates: Sequence[Candidate], variables) -> None:
        for day_id in self.days_sorted:
            for hospital_id in self.hospitals:
                for usage_type, attribute, field in (
                    ("recipient", "recipient_usage", "recipient_transplant_slots"),
                    ("retrieval", "retrieval_usage", "donor_retrieval_slots"),
                    ("lab", "lab_usage", "lab_review_slots"),
                ):
                    terms = []
                    key = (day_id, hospital_id)
                    for candidate in candidates:
                        usage_map = candidate.usage_map(attribute)
                        if key in usage_map:
                            terms.append(usage_map[key] * variables[candidate.candidate_id])
                    if terms:
                        model.addCons(
                            quicksum(terms) <= int(self.hospital_capacity[key][field]),
                            name=f"{usage_type}[{day_id},{hospital_id}]",
                        )

                for panel in PANEL_TYPES:
                    panel_key = (day_id, hospital_id, panel)
                    if panel_key not in self.panel_capacity:
                        continue
                    for usage_type, attribute, field in (
                        ("panel", "panel_usage", "max_reviews"),
                        ("panel_high", "panel_high_usage", "high_cpra_review_limit"),
                    ):
                        terms = []
                        for candidate in candidates:
                            usage_map = candidate.usage_map(attribute)
                            if panel_key in usage_map:
                                terms.append(usage_map[panel_key] * variables[candidate.candidate_id])
                        if terms:
                            model.addCons(
                                quicksum(terms) <= int(self.panel_capacity[panel_key][field]),
                                name=f"{usage_type}[{day_id},{hospital_id},{panel}]",
                            )

            for region in self.regions:
                bridge_key = (day_id, region)
                if bridge_key not in self.bridge_capacity:
                    continue
                for usage_type, attribute, field in (
                    ("bridge", "bridge_usage", "max_bridge_positions"),
                    ("remote", "remote_usage", "max_remote_handoffs"),
                ):
                    terms = []
                    for candidate in candidates:
                        usage_map = candidate.usage_map(attribute)
                        if bridge_key in usage_map:
                            terms.append(usage_map[bridge_key] * variables[candidate.candidate_id])
                    if terms:
                        model.addCons(
                            quicksum(terms) <= int(self.bridge_capacity[bridge_key][field]),
                            name=f"{usage_type}[{day_id},{region}]",
                        )

    def add_region_constraints(self, model: Model, candidates: Sequence[Candidate], variables) -> None:
        for region in self.regions:
            recipient_terms = []
            sensitized_terms = []
            export_terms = []
            import_terms = []
            for candidate in candidates:
                region_recipients = candidate.usage_map("region_recipients")
                region_sensitized = candidate.usage_map("region_sensitized")
                region_exports = candidate.usage_map("region_exports")
                region_imports = candidate.usage_map("region_imports")
                if region in region_recipients:
                    recipient_terms.append(region_recipients[region] * variables[candidate.candidate_id])
                if region in region_sensitized:
                    sensitized_terms.append(region_sensitized[region] * variables[candidate.candidate_id])
                if region in region_exports:
                    export_terms.append(region_exports[region] * variables[candidate.candidate_id])
                if region in region_imports:
                    import_terms.append(region_imports[region] * variables[candidate.candidate_id])
            model.addCons(
                quicksum(recipient_terms) >= int(self.region_policy[region]["min_transplants"]),
                name=f"region_floor[{region}]",
            )
            model.addCons(
                quicksum(sensitized_terms) >= int(self.region_policy[region]["sensitized_floor"]),
                name=f"sensitized_floor[{region}]",
            )
            model.addCons(
                quicksum(export_terms) - quicksum(import_terms)
                <= int(self.region_policy[region]["max_net_export"]),
                name=f"net_export[{region}]",
            )

    def add_mip_start(self, model: Model, variables, selected_ids: Set[str]) -> bool:
        if not selected_ids:
            return False
        solution = model.createPartialSol()
        for candidate_id in selected_ids:
            model.setSolVal(solution, variables[candidate_id], 1.0)
        return bool(model.addSol(solution, free=True))

    def greedy_repair_start(self, candidates: Sequence[Candidate], preferred_ids: Set[str]) -> Set[str]:
        selected: List[Candidate] = []
        candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        ordered = []
        for candidate_id in preferred_ids:
            if candidate_id in candidate_by_id:
                ordered.append(candidate_by_id[candidate_id])
        ordered.extend(
            candidate
            for candidate in sorted(candidates, key=lambda candidate: (-candidate.score, candidate.signature))
            if candidate.candidate_id not in preferred_ids
        )
        for candidate in ordered:
            if self.candidate_fits(selected, candidate):
                selected.append(candidate)
        return {candidate.candidate_id for candidate in selected}

    def solve_exact(
        self,
        candidates: Sequence[Candidate],
        time_limit_seconds: float,
        incumbent_ids: Set[str],
        repair_start_ids: Set[str],
    ) -> Tuple[List[Candidate], Dict[str, object]]:
        model = Model("kidney_exchange_repair")
        model.hideOutput(True)
        model.setParam("display/verblevel", 0)
        model.setParam("limits/gap", 0.0005)
        if time_limit_seconds > 0:
            model.setParam("limits/time", float(time_limit_seconds))

        variables = {
            candidate.candidate_id: model.addVar(vtype="B", name=candidate.candidate_id)
            for candidate in candidates
        }

        for pair_id in self.pairs:
            relevant = [
                variables[candidate.candidate_id]
                for candidate in candidates
                if pair_id in candidate.pairs
            ]
            if relevant:
                model.addCons(quicksum(relevant) <= 1, name=f"pair_once[{pair_id}]")

        for altruist_id in self.altruists:
            relevant = [
                variables[candidate.candidate_id]
                for candidate in candidates
                if candidate.exchange_type == "chain" and candidate.altruist_id == altruist_id
            ]
            if relevant:
                model.addCons(quicksum(relevant) <= 1, name=f"altruist_once[{altruist_id}]")

        model.addCons(
            quicksum(
                variables[candidate.candidate_id]
                for candidate in candidates
                if candidate.exchange_type == "chain"
            )
            <= int(self.config["max_active_chain_starts"]),
            name="max_active_chain_starts",
        )

        self.add_usage_constraints(model, candidates, variables)
        self.add_region_constraints(model, candidates, variables)

        incumbent_start_ok = self.add_mip_start(model, variables, incumbent_ids)
        repair_start_ok = self.add_mip_start(model, variables, repair_start_ids)

        model.setObjective(
            quicksum(candidate.score * variables[candidate.candidate_id] for candidate in candidates),
            "maximize",
        )
        model.optimize()

        summary: Dict[str, object] = {
            "status": str(model.getStatus()),
            "incumbent_mip_start_accepted": int(incumbent_start_ok),
            "repair_mip_start_accepted": int(repair_start_ok),
            "incumbent_count": int(model.getNSols()),
        }
        best_solution = model.getBestSol()
        if best_solution is None:
            return [], summary

        selected = [
            candidate
            for candidate in candidates
            if model.getSolVal(best_solution, variables[candidate.candidate_id]) > 0.5
        ]
        summary["objective"] = float(model.getObjVal())
        summary["dual_bound"] = float(model.getDualbound())
        summary["gap"] = float(model.getGap())
        return selected, summary

    def incumbent_rows_from_disk(self) -> List[Dict[str, str]]:
        with open(OUTPUT_CSV, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def time_limit_seconds(self) -> float:
        configured = float(self.config["solve_time_limit_seconds"])
        env_value = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
        if env_value is None:
            return configured
        try:
            return min(configured, float(env_value))
        except ValueError:
            return configured

    def update_model_note(self) -> None:
        lines = [
            "# Initial Kidney Exchange Model",
            "",
            "## Current Solve Mode",
            "",
            "The guarded repair/performance pass keeps the existing CSV as the rollback",
            "incumbent, reparses it from disk, and audits public hard constraints",
            "independently of the optimization model before attempting any improvement.",
            "",
            "To address the challenger's bridge-capacity ambiguity, the repair search uses",
            "the stricter interpretation that every continuing chain position consumes",
            "regional bridge capacity on its surgery day, including same-day continuations.",
            "Publication still requires a zero-violation re-audit from the exported CSV.",
            "",
            "## Objective",
            "",
            "The optimization objective remains the explicit stage scoring function already",
            "used in the baseline model:",
            "",
            "- reward clinical value, altruist chain-start value, waiting time, pediatric",
            "  service, high-priority service, and highly sensitized service;",
            "- penalize failure probability, travel time, late-courier moves, lab-review",
            "  burden, and delayed bridge handoffs across days.",
            "",
            "The public brief does not publish a fuller numeric weighting scheme beyond",
            "those modeled terms.",
            "",
            "## Performance Method",
            "",
            "1. Reparse `/app/submissions/initial_plan.csv` and audit it under both the",
            "   public bridge interpretation and the stricter robustness interpretation.",
            "2. Enumerate all valid cycles and chains with the strict bridge interpretation",
            "   enforced as a hard capacity rule.",
            "3. Build a repair MIP start from the disk incumbent and a strict-feasible",
            "   greedy repair start.",
            "4. Solve the strict exact set-packing model in SCIP with relative gap",
            "   `0.0005` and time limit bounded by `ORCLAW_SOLVE_TIME_LIMIT_SECONDS` when set.",
            "5. Recompute objective and hard constraints independently from the candidate",
            "   CSV rows, and overwrite the published artifact only on strict improvement.",
        ]
        with open(MODEL_NOTE_PATH, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def write_log(
        self,
        candidate_count: int,
        incumbent_public_audit: Dict[str, object],
        incumbent_strict_audit: Dict[str, object],
        candidate_rows: List[Dict[str, str]],
        candidate_public_audit: Dict[str, object],
        candidate_strict_audit: Dict[str, object],
        solve_summary: Dict[str, object],
        published: bool,
        elapsed_seconds: float,
    ) -> None:
        incumbent_objective = float(incumbent_public_audit["objective"])
        candidate_objective = float(candidate_public_audit["objective"])
        dual_bound = solve_summary.get("dual_bound")
        gap = solve_summary.get("gap")
        lines = [
            "# Solve Log",
            "",
            "## Guarded Repair Summary",
            "",
            f"- Current wave: `{self.current_wave}`",
            f"- Candidate exchanges enumerated under strict bridge audit: `{candidate_count}`",
            f"- Disk incumbent objective: `{format_float(incumbent_objective)}`",
            f"- Disk incumbent public-audit theta: `{format_float(float(incumbent_public_audit['theta']))}`",
            f"- Disk incumbent strict-audit theta: `{format_float(float(incumbent_strict_audit['theta']))}`",
            f"- SCIP status: `{solve_summary.get('status', 'not_run')}`",
            f"- SCIP incumbent count: `{solve_summary.get('incumbent_count', 0)}`",
            f"- Incumbent MIP start accepted: `{solve_summary.get('incumbent_mip_start_accepted', 0)}`",
            f"- Repair MIP start accepted: `{solve_summary.get('repair_mip_start_accepted', 0)}`",
            f"- Candidate objective: `{format_float(candidate_objective)}`",
            f"- Candidate public-audit theta: `{format_float(float(candidate_public_audit['theta']))}`",
            f"- Candidate strict-audit theta: `{format_float(float(candidate_strict_audit['theta']))}`",
            f"- Candidate dual bound: `{format_float(float(dual_bound))}`" if dual_bound is not None else "- Candidate dual bound: `n/a`",
            f"- Candidate relative gap: `{format_float(float(gap))}`" if gap is not None else "- Candidate relative gap: `n/a`",
            f"- Published new artifact: `{int(published)}`",
            f"- Elapsed seconds: `{format_float(elapsed_seconds)}`",
            "",
            "## Publication Decision",
            "",
        ]
        if published:
            lines.append("- The candidate passed both independent audits and strictly improved the incumbent objective, so `initial_plan.csv` was replaced.")
        else:
            lines.append("- The rollback incumbent was retained because the strict-feasible candidate did not strictly improve the incumbent objective.")

        lines.extend(
            [
                "",
                "## Independent Audits",
                "",
                f"- Incumbent public audit: `theta={format_float(float(incumbent_public_audit['theta']))}`, `violations={incumbent_public_audit['violated_groups']}`",
                f"- Incumbent strict audit: `theta={format_float(float(incumbent_strict_audit['theta']))}`, `violations={incumbent_strict_audit['violated_groups']}`",
                f"- Candidate public audit: `theta={format_float(float(candidate_public_audit['theta']))}`, `violations={candidate_public_audit['violated_groups']}`",
                f"- Candidate strict audit: `theta={format_float(float(candidate_strict_audit['theta']))}`, `violations={candidate_strict_audit['violated_groups']}`",
            ]
        )

        if incumbent_strict_audit["violations"]:
            lines.extend(["", "## Incumbent Strict Violations", ""])
            for name, residual in incumbent_strict_audit["violations"].items():
                lines.append(f"- `{name}`: `{format_float(float(residual))}`")

        if candidate_strict_audit["violations"]:
            lines.extend(["", "## Candidate Strict Violations", ""])
            for name, residual in candidate_strict_audit["violations"].items():
                lines.append(f"- `{name}`: `{format_float(float(residual))}`")

        lines.extend(["", "## Candidate Exchanges", ""])
        if candidate_rows:
            exchanges = defaultdict(list)
            for row in candidate_rows:
                exchanges[row["exchange_id"]].append(row)
            for exchange_id in sorted(exchanges):
                ordered = sorted(exchanges[exchange_id], key=lambda row: int(row["position"]))
                exchange_type = ordered[0]["exchange_type"]
                altruist_id = ordered[0]["altruist_id"]
                descriptor = f"{exchange_type}:{altruist_id}" if altruist_id else exchange_type
                pair_days = ", ".join(f"{row['pair_id']}@{row['day_id']}" for row in ordered)
                lines.append(f"- `{exchange_id}` `{descriptor}` `{pair_days}`")
        else:
            lines.append("- No candidate solution was returned by SCIP.")

        with open(LOG_PATH, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def main() -> None:
    start_time = time.time()
    solver = KidneyExchangeSolver()
    solver.update_model_note()

    incumbent_rows = solver.incumbent_rows_from_disk()
    incumbent_public_audit = solver.audit_rows(incumbent_rows, strict_bridge=False, source_label="disk_incumbent_public")
    incumbent_strict_audit = solver.audit_rows(incumbent_rows, strict_bridge=True, source_label="disk_incumbent_strict")

    candidates = solver.enumerate_candidates(strict_bridge=True)
    candidate_by_signature = {candidate.signature: candidate for candidate in candidates}
    incumbent_signatures = solver.rows_to_exchange_signatures(incumbent_rows)
    incumbent_ids = {
        candidate_by_signature[signature].candidate_id
        for signature in incumbent_signatures
        if signature in candidate_by_signature
    }
    repair_start_ids = solver.greedy_repair_start(candidates, incumbent_ids)

    selected_candidates, solve_summary = solver.solve_exact(
        candidates=candidates,
        time_limit_seconds=solver.time_limit_seconds(),
        incumbent_ids=incumbent_ids,
        repair_start_ids=repair_start_ids,
    )

    candidate_rows = solver.selected_to_rows(selected_candidates) if selected_candidates else incumbent_rows
    candidate_public_audit = solver.audit_rows(candidate_rows, strict_bridge=False, source_label="candidate_public")
    candidate_strict_audit = solver.audit_rows(candidate_rows, strict_bridge=True, source_label="candidate_strict")

    incumbent_objective = float(incumbent_public_audit["objective"])
    candidate_objective = float(candidate_public_audit["objective"])
    publish = (
        float(candidate_public_audit["theta"]) <= 1e-9
        and float(candidate_strict_audit["theta"]) <= 1e-9
        and candidate_objective > incumbent_objective + 1e-8
    )
    if publish:
        write_csv(OUTPUT_CSV, candidate_rows, PLAN_FIELDS)

    elapsed_seconds = time.time() - start_time
    solver.write_log(
        candidate_count=len(candidates),
        incumbent_public_audit=incumbent_public_audit,
        incumbent_strict_audit=incumbent_strict_audit,
        candidate_rows=candidate_rows,
        candidate_public_audit=candidate_public_audit,
        candidate_strict_audit=candidate_strict_audit,
        solve_summary=solve_summary,
        published=publish,
        elapsed_seconds=elapsed_seconds,
    )


if __name__ == "__main__":
    main()
