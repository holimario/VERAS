#!/usr/bin/env python3
"""Offline prompt/forwarding checks, optionally against original runner sources.

With no arguments, compare public outputs to golden hashes from the evaluated
sources. Pass all three runner paths for a stronger full-source differential.
No operational task data or model credential is needed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "veras" / "src"))

from veras_harness.cli import parser as cli_parser, resolve_profile  # noqa: E402
from veras_harness.profiles import PROFILES  # noqa: E402
from veras_harness.prompts import performance_prompt  # noqa: E402

FROZEN_SHA256 = "47e8a7a68f59f558daf254299b208041c3bd15a60d53543ebb42c211d9edfc69"
FORK_SHA256 = {
    "datacenter": "2bb70f5d7fca26c17e7fb13614e7d77dce96ff3c54991f5bd2eef13a7c908360",
    "power-grid": "809bf5629dc1bb578dffbbea4db888ec1d0742b0a32fed74a6a4a4d07fc061d4",
}
# Computed by invoking the three pinned original performance_prompt functions
# with the SAMPLE_ARGS below. They are safe to redistribute (no task data).
GOLDEN_PROMPT_SHA256 = {
    "benchmark": (
        "5269c9894cab07ef9bb12d9739f10e1c1245bd6938fe83532c43c7f2b7dfd24d",
        "03dba8a5f330f09cf8f63228b6557df5c045447e02059b93062cc926d4ee7af7",
    ),
    "datacenter": (
        "c06f34a4e03ef0a968fec3f0fdd9069989445902295bd150aef5e82f0f105585",
        "7b57193e94031a272638025501b6de6aa07bc6be1e52697ac48210899d557449",
    ),
    "power-grid": (
        "9ca8e8d3c87e694026229ba83602aa9e91cc6236672359a11499d27d54cd75d5",
        "cbdb2092947b663a3b446f19aca5c5cf8e3558f60fc6db3b34178509bae73eb6",
    ),
}
SAMPLE_ARGS = (
    ("example-task", {}, {}, ""),
    ("example-task", {"valid": True, "note": "数据"}, {"safe": False}, " X " * 9000),
)


def read_pinned(path: Path, expected: str) -> str:
    content = path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise AssertionError(f"runner source hash mismatch: {path} ({actual})")
    return content.decode("utf-8")


def isolated_prompt(source: str):
    """Execute only the runner's prompt function, never its top-level imports."""
    functions = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "performance_prompt"
    ]
    assert len(functions) == 1
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    namespace: dict[str, Any] = {"json": json, "Any": Any}
    exec(compile(module, "<isolated-prompt>", "exec"), namespace)
    return namespace["performance_prompt"]


def assert_public_forwarding() -> None:
    """Check that the two profile-only values reach the existing control points."""
    source = (ROOT / "veras" / "src" / "veras_harness" / "controller.py").read_text()
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "make_run_task"
    ]
    assert len(functions) == 1
    function = functions[0]
    assert {arg.arg for arg in function.args.kwonlyargs} >= {
        "challenger_cap_seconds", "performance_addendum"
    }
    budgets = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "challenge_budget"
            for target in node.targets
        )
    ]
    assert len(budgets) == 1
    assert isinstance(budgets[0].value, ast.Call)
    assert isinstance(budgets[0].value.args[0], ast.Name)
    assert budgets[0].value.args[0].id == "challenger_cap_seconds"
    prompts = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "performance_prompt"
    ]
    assert len(prompts) == 1
    assert any(
        key.arg == "performance_addendum"
        and isinstance(key.value, ast.Name)
        and key.value.id == "performance_addendum"
        for key in prompts[0].keywords
    )

    cli = ast.parse((ROOT / "veras" / "src" / "veras_harness" / "cli.py").read_text())
    launches = [
        node
        for node in ast.walk(cli)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "make_run_task"
    ]
    assert len(launches) == 1
    for name in ("challenger_cap_seconds", "performance_addendum"):
        assert any(
            key.arg == name
            and isinstance(key.value, ast.Attribute)
            and isinstance(key.value.value, ast.Name)
            and key.value.value.id == "profile"
            and key.value.attr == name
            for key in launches[0].keywords
        )


def assert_published_hashes() -> None:
    records = {
        "datacenter": ROOT / "real_world" / "data_center" / "comparison.json",
        "power-grid": ROOT / "real_world" / "power_grid" / "comparison.json",
    }
    for name, path in records.items():
        record = json.loads(path.read_text())
        profile = PROFILES[name]
        assert dict(profile.runner_sha256) == record["runner_sha256"]
        assert set(profile.input_sha256) == (
            set(record["public_input_sha256"])
            | {"environment/app/submissions/solution_template.json"}
        )
        assert all(
            profile.input_sha256[key] == digest
            for key, digest in record["public_input_sha256"].items()
        )
        assert profile.input_sha256["environment/app/submissions/solution_template.json"] == (
            "dea2c0805a80d612f333e19fff02a4d4be51d10f1e8c29cc53fc9b25835ecac1"
        )
        assert profile.model == record["model"]
        assert profile.reasoning_effort == record["reasoning_effort"]
        assert profile.timeout_scale == record["timeout_scale"]


def assert_profile_resolution() -> None:
    benchmark_args = cli_parser().parse_args(["--output", "unused"])
    resolve_profile(benchmark_args)
    assert benchmark_args.reasoning_effort == "high"
    assert benchmark_args.timeout_scale == 1.0
    assert benchmark_args.reserve_fraction == 0.30
    assert benchmark_args.concurrency == 1
    assert benchmark_args.tasks is None
    for name in ("datacenter", "power-grid"):
        profile = PROFILES[name]
        args = cli_parser().parse_args(
            ["--profile", name, "--output", "unused", "--model", profile.model]
        )
        resolve_profile(args)
        assert args.tasks == [profile.task]
        for setting in (
            "model", "reasoning_effort", "timeout_scale", "reserve_fraction", "concurrency"
        ):
            assert getattr(args, setting) == getattr(profile, setting)
        for setting, value in (
            ("--tasks", "other-task"),
            ("--reasoning-effort", "low"),
            ("--timeout-scale", "9"),
        ):
            divergent = cli_parser().parse_args(
                [
                    "--profile", name, "--output", "unused", "--model", profile.model,
                    setting, value,
                ]
            )
            try:
                resolve_profile(divergent)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{name}: accepted divergent {setting}")

def assert_golden_prompts() -> None:
    for name, expected_hashes in GOLDEN_PROMPT_SHA256.items():
        addendum = PROFILES[name].performance_addendum
        actual_hashes = tuple(
            hashlib.sha256(
                performance_prompt(*args, performance_addendum=addendum).encode("utf-8")
            ).hexdigest()
            for args in SAMPLE_ARGS
        )
        assert actual_hashes == expected_hashes, f"{name}: prompt output drift"
        print(f"{name}: golden prompt outputs verified")


def assert_fork(frozen: str, fork: str, profile_name: str) -> None:
    profile = PROFILES[profile_name]
    marker = "\n<public_challenger_report>\n"
    assert frozen.count(marker) == 1
    expected = frozen.replace(
        marker, "\n" + profile.performance_addendum + marker[1:], 1
    )
    if profile_name == "power-grid":
        old = "challenge_budget = min(\n                            180.0,"
        assert expected.count(old) == 1
        expected = expected.replace(old, old.replace("180.0", "480.0"), 1)
    assert fork == expected, f"{profile_name}: fork has an unaccounted controller edit"

    original = isolated_prompt(frozen)
    actual = isolated_prompt(fork)
    for _, audit, observation, report in SAMPLE_ARGS:
        args = (profile.task, audit, observation, report)
        assert (
            performance_prompt(*args, performance_addendum=profile.performance_addendum)
            == actual(*args)
        )
        assert performance_prompt(*args) == original(*args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--datacenter", type=Path)
    parser.add_argument("--power-grid", type=Path)
    args = parser.parse_args()
    assert_public_forwarding()
    assert_published_hashes()
    assert_profile_resolution()
    assert_golden_prompts()
    paths = (args.frozen, args.datacenter, args.power_grid)
    if any(paths) and not all(paths):
        parser.error("pass all three runner source paths, or none")
    if not any(paths):
        return
    frozen = read_pinned(args.frozen, FROZEN_SHA256)
    for name in ("datacenter", "power-grid"):
        fork = read_pinned(getattr(args, name.replace("-", "_")), FORK_SHA256[name])
        assert_fork(frozen, fork, name)
        print(f"{name}: exact fork delta and prompt output verified")


if __name__ == "__main__":
    main()
