"""Command-line entry point for VERAS."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .api import (
    DEFAULT_API_KEY_ENV,
    codex_config,
    model_from_environment,
    public_result_metadata,
)
from .constants import DEFAULT_MIN_RECOVERY_SECONDS, HARNESS_NAME, benchmark_root
from .profiles import PROFILES, verify_case_inputs


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run VERAS on ORAgentBench")
    value.add_argument("--profile", choices=tuple(PROFILES), default="benchmark")
    value.add_argument("--tasks", nargs="*")
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--benchmark-root", type=Path)
    value.add_argument("--model")
    value.add_argument("--base-url", default=os.environ.get("VERAS_BASE_URL"))
    value.add_argument(
        "--api-key-env",
        default=os.environ.get("VERAS_API_KEY_ENV", DEFAULT_API_KEY_ENV),
    )
    value.add_argument("--wire-api", choices=("responses", "chat"))
    value.add_argument(
        "--reasoning-effort", choices=("low", "medium", "high", "xhigh")
    )
    value.add_argument("--context-window", type=int)
    value.add_argument("--auto-compact-token-limit", type=int)
    value.add_argument("--concurrency", type=int)
    value.add_argument("--timeout-scale", type=float)
    value.add_argument("--reserve-fraction", type=float)
    value.add_argument("--min-recovery-seconds", type=float)
    value.add_argument("--local-solver-cap-seconds", type=float)
    value.add_argument("--max-transport-retries", type=int)
    value.add_argument("--force", action="store_true")
    value.add_argument("--dry-run", action="store_true")
    return value


def resolve_profile(args: argparse.Namespace) -> None:
    """Apply defaults; exact case profiles reject controller/model overrides."""
    profile = PROFILES[args.profile]
    model = args.model or os.environ.get("VERAS_MODEL")
    if profile.task:
        if args.tasks is not None and args.tasks != [profile.task]:
            raise ValueError(f"{profile.name} requires exactly --tasks {profile.task}")
        args.tasks = [profile.task]
        if model is not None and model != profile.model:
            raise ValueError(f"{profile.name} requires model {profile.model}")
        model = profile.model
    args.model = model
    defaults = {
        "reasoning_effort": profile.reasoning_effort,
        "timeout_scale": profile.timeout_scale,
        "reserve_fraction": profile.reserve_fraction,
        "concurrency": profile.concurrency,
        "min_recovery_seconds": DEFAULT_MIN_RECOVERY_SECONDS,
        "local_solver_cap_seconds": 180.0,
        "max_transport_retries": 4,
        "context_window": 272000,
        "auto_compact_token_limit": 240000,
        "wire_api": "responses",
    }
    for name, expected in defaults.items():
        supplied = getattr(args, name)
        if profile.task and supplied is not None and supplied != expected:
            raise ValueError(
                f"{profile.name} requires {name}={expected}, got {supplied}"
            )
        if supplied is None:
            setattr(args, name, expected)


def main() -> None:
    args = parser().parse_args()
    if args.benchmark_root:
        os.environ["ORAGENTBENCH_ROOT"] = str(args.benchmark_root.expanduser().resolve())
    resolve_profile(args)
    profile = PROFILES[args.profile]
    if profile.task:
        verify_case_inputs(benchmark_root(), profile)
    if not args.model:
        raise ValueError("--model or VERAS_MODEL is required")
    if not args.base_url:
        raise ValueError("--base-url or VERAS_BASE_URL is required")
    if not 0.0 <= args.reserve_fraction <= 0.5:
        raise ValueError("reserve-fraction must be between 0.0 and 0.5")
    if args.timeout_scale <= 0 or args.concurrency < 1:
        raise ValueError("timeout-scale and concurrency must be positive")
    if args.min_recovery_seconds < 0 or args.local_solver_cap_seconds <= 0:
        raise ValueError("recovery and solver budgets must be nonnegative/positive")
    if args.max_transport_retries < 1:
        raise ValueError("max-transport-retries must be positive")
    if args.auto_compact_token_limit >= args.context_window:
        raise ValueError("auto-compact-token-limit must be smaller than context-window")

    from .audits import redact_tree
    from .benchmark_api import benchmark
    from .controller import make_run_task

    model = model_from_environment(args.model, args.base_url, args.api_key_env)
    original_safe_environment = benchmark.safe_environment
    original_write_json = benchmark.write_json

    def safe_environment(api_key: str, solution_env: dict[str, Any]) -> dict[str, str]:
        environment = original_safe_environment(api_key, solution_env)
        for name in list(environment):
            if name != args.api_key_env and name.endswith(
                ("_API_KEY", "_TOKEN", "_SECRET")
            ):
                environment.pop(name)
        environment[args.api_key_env] = api_key
        return environment

    def write_json(path: Path, value: Any) -> None:
        value = public_result_metadata(value)
        if isinstance(value, dict) and "credential_persisted" in value:
            value["credential_persisted"] = False
            value["reasoning_effort"] = args.reasoning_effort
            value["veras_harness"] = HARNESS_NAME
            if profile.task:
                value["run_profile"] = profile.name
        original_write_json(path, value)

    benchmark.load_model_config = lambda _path: dict(model)
    benchmark.safe_environment = safe_environment
    benchmark.codex_config = lambda value: codex_config(
        value,
        reasoning_effort=args.reasoning_effort,
        context_window=args.context_window,
        auto_compact_token_limit=args.auto_compact_token_limit,
        wire_api=args.wire_api,
    )
    benchmark.write_json = write_json
    benchmark.redact_tree = redact_tree
    benchmark.VERSIONS["codex"] = {
        **benchmark.VERSIONS["codex"],
        "policy": "counterexample-guided-guarded-performance",
        "reasoning_effort": args.reasoning_effort,
        "veras_harness": HARNESS_NAME,
    }
    benchmark.run_task = make_run_task(
        reserve_fraction=args.reserve_fraction,
        min_recovery_seconds=args.min_recovery_seconds,
        local_solver_cap_seconds=args.local_solver_cap_seconds,
        max_transport_retries=args.max_transport_retries,
        challenger_cap_seconds=profile.challenger_cap_seconds,
        performance_addendum=profile.performance_addendum,
    )

    forwarded = [
        "harness_benchmark.py",
        "--harness",
        "codex",
        "--output",
        str(args.output),
        "--concurrency",
        str(args.concurrency),
        "--timeout-scale",
        str(args.timeout_scale),
    ]
    if args.tasks is not None:
        forwarded.extend(["--tasks", *args.tasks])
    if args.force:
        forwarded.append("--force")
    if args.dry_run:
        forwarded.append("--dry-run")
    sys.argv = forwarded
    benchmark.main()


if __name__ == "__main__":
    main()
