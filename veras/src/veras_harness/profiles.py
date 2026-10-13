"""Pinned, public-input case-study profiles of the evaluated VERAS controller.

The benchmark profile stays untouched. The case profiles supply only the actual
performance text and numeric parameters of the reported runs; routing and
model credentials are supplied by the authorized user at launch time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RunProfile:
    name: str
    task: str | None
    model: str | None
    reasoning_effort: str
    timeout_scale: float
    reserve_fraction: float
    challenger_cap_seconds: float
    concurrency: int
    performance_addendum: str = ""
    input_sha256: Mapping[str, str] = field(default_factory=dict)
    runner_sha256: Mapping[str, str] = field(default_factory=dict)


def verify_case_inputs(root: Path, profile: RunProfile) -> None:
    """Refuse an *exact* case replay if public task/runner bytes have drifted."""
    if profile.task is None:
        return
    task_root = root / "harbor_tasks" / profile.task
    for relative, expected in profile.input_sha256.items():
        path = task_root / relative
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        )
        if actual != expected:
            raise ValueError(
                f"{profile.name}: public input mismatch: {relative} "
                f"(expected {expected}, got {actual})"
            )
    extra = sorted(
        str(path.relative_to(task_root))
        for path in task_root.rglob("*")
        if path.is_file()
        and str(path.relative_to(task_root)) not in profile.input_sha256
    )
    if extra:
        raise ValueError(f"{profile.name}: unexpected task files: {extra}")
    for relative, expected in profile.runner_sha256.items():
        path = root / "local_runner" / relative
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        )
        if actual != expected:
            raise ValueError(
                f"{profile.name}: ORAgentBench runner mismatch: {relative} "
                f"(expected {expected}, got {actual})"
            )


# The following two strings are verbatim additions from the run-time forks.
# Trailing two newlines are intentional: the next token is <public_challenger_report>.
DATACENTER_PERFORMANCE = """For this temporal two-region placement task, a visible worst-day peak at its
public structural floor is not the end of performance work: another visible
day or opposite direction may remain needlessly high. Build an independent
all-Zhongwei sparse-capacity-repair portfolio from public inputs only; preserve
strict positive resource slack and exact table-writer/fallback semantics. Once
worst peak is within 0.050 Gbps of the public floor, continue coordinated
writer-group and reversible task-neighborhood search to lower the *other*
visible day's full directional peak and the two-day mean, not only the maximum.
Use an explicit balance diagnostic: when attainable without worsening the worst
peak or hard constraints, keep the nonbinding daily maximum below 85 percent
of the binding day's peak. Retain a Pareto set of exact two-day candidates with
low intervention counts and resource utilization at most 0.990; do not discard
an otherwise robust near-tie candidate merely because it has a few more moves.
For final selection, transparently report the public near-tie intervention,
mean/spread, and slack tradeoff and validate the exported Parquet itself.
Never access August 10, previous mappings, historical scores, or any reference
implementation. Regenerate only from public inputs in at most 120 seconds.

"""

POWER_GRID_PERFORMANCE = """For this power-grid task, use the available agent wall time for substantive guarded
performance work instead of ending at the first feasible incumbent when a material
public-objective gap remains. Compare genuinely different investment/topology
neighborhoods across `uL`, `uNS`, `uEX`, `zESS`, `zPVG`, and `zWTG` by their complete
independently recomputed objective. A fixed-planning screen is only a diagnostic
internal start; acceptance requires an unrestricted full free-planning solve.
The larger agent wall budget does NOT relax the separate <=900-second total solver
sequence (including presolve) and <=4-thread contract for a single reproducible
from-scratch generator. Before finalizing, run the exact final `solve.py` from a
fresh directory containing only public workbook/spec and source, no previous NPZ
or development checkpoint; time the solver sequence and independently audit the
newly generated artifact. If this clean invocation fails, fix reproducibility
within the remaining wall time or clearly state that a checkpoint-only score is
not an eligible final result. Do not import any outside solution or private data.

"""

RUNNER_SHA256 = {
    'harness_benchmark.py': '3bc549fc0c74d7ee78ff9e78ea5f24f2d88c2c7179ac657b699e63b31538c73b',
    'run_codex_gpt54_benchmark.py': 'f31b8202b64279b46022e1ac31a008519996144f8f524a62f0437fd8159b3e16',
    'run_agent.py': 'd3d6f354d6d52f581d3899ccb063bf02960794655418125baa126470a3cea8ea',
}

PROFILES = {
    "benchmark": RunProfile(
        name="benchmark",
        task=None,
        model=None,
        reasoning_effort="high",
        timeout_scale=1.0,
        reserve_fraction=0.30,
        challenger_cap_seconds=180.0,
        concurrency=1,  # The run_benchmark.sh split launcher explicitly uses two workers.
    ),
    "datacenter": RunProfile(
        name="datacenter",
        task="veras_strict_datacenter_20261006",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        timeout_scale=3.0,
        reserve_fraction=0.40,
        challenger_cap_seconds=180.0,
        concurrency=1,
        performance_addendum=DATACENTER_PERFORMANCE,
        input_sha256={
            'task.toml': '2171eaf6bc36efde8cb62865d4fd138eb99ba45cd3f6f4694d0e89f81451cb88',
            'instruction.md': '64148bda9f6317a3f288e32f479596ec94ec470055b7e28c421cc37ba8f80b68',
            'environment/app/PROBLEM_STATEMENT.md': '25ca58af768272857c6bb2559af62015e227e1fbb9a7f0d35635d3190850ca8a',
            'tests/test.sh': '070dd42323bda7de8df89402e3eb8fdf3b09fe947893bf190625511586049408',
            'environment/app/submissions/solution_template.json': 'dea2c0805a80d612f333e19fff02a4d4be51d10f1e8c29cc53fc9b25835ecac1',
            'environment/app/data/current_table_regions.parquet': '56b98c165072e9c661f45dbade3d696a5789956983a69ae09ec647ec981e86bf',
            'environment/app/data/exec_train_0808_0809.parquet': '56887088b2c7a1be846e670ce238fce2a33d198a6a0350acd2b41a246d12bd4c',
            'environment/app/data/resource_limits.json': 'b9ad7e27e0780c38295aa024dd457dfb7f4ae5b9f17e95367f9b298a01a1526e',
            'environment/app/data/table_size_latest.parquet': '90a374b6ef85ff66e30653a0baf363c758aeae89f745282f7806ab0b5fb698d4',
            'environment/app/data/table_writers_fallback.parquet': 'd8ece7e54b7559099aaffe42627f143e211749dde1b03aeced147410817c1a08',
            'environment/app/data/task_universe.parquet': '13ba1d856b5d8e626327b974fec28ab256639f50e999a717c5a5f331b6f98ae6',
            'environment/app/data/trans_train_0808_0809.parquet': 'a556dd5b2785f7797702e7b1b02c9d1072f0a5d79adc813e430de76f4d270f68',
        },
        runner_sha256=RUNNER_SHA256,
    ),
    "power-grid": RunProfile(
        name="power-grid",
        task="veras_strict_grid_20261006",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        timeout_scale=2.0,
        reserve_fraction=0.40,
        challenger_cap_seconds=480.0,
        concurrency=1,
        performance_addendum=POWER_GRID_PERFORMANCE,
        input_sha256={
            'task.toml': '8ebeabd294836c9edfc9cd32ed5334b2a4e7412337c49c2d719796d1761c8f0b',
            'instruction.md': 'c06cec767514cb6ad6f28911dff661fc7bc1a98c0f8a52388f666c558f66d4f4',
            'environment/app/PROBLEM_STATEMENT.md': 'f8a01bca6d29761363aafd381a131c16c0a31e0c7814a76efc6332cfabdeea0d',
            'tests/test.sh': '070dd42323bda7de8df89402e3eb8fdf3b09fe947893bf190625511586049408',
            'environment/app/submissions/solution_template.json': 'dea2c0805a80d612f333e19fff02a4d4be51d10f1e8c29cc53fc9b25835ecac1',
            'environment/app/data/line_load.xlsx': 'db24994c4bc9c9f3368a9e1369f47f3b35b80c1a3ff1c6305584fd4eca70137c',
            'environment/app/data/model_spec.json': 'bb3200f9f89fbe61e7738e04bd4a90ff9de2e8d41ea32bc870bf1d62bdf458a7',
        },
        runner_sha256=RUNNER_SHA256,
    ),
}
