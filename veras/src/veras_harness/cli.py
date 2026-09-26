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
from .constants import (
    DEFAULT_MIN_RECOVERY_SECONDS,
    DEFAULT_RESERVE_FRACTION,
    HARNESS_NAME,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run VERAS on ORAgentBench")
    value.add_argument("--tasks", nargs="*")
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--benchmark-root", type=Path)
    value.add_argument("--model", default=os.environ.get("VERAS_MODEL"))
    value.add_argument("--base-url", default=os.environ.get("VERAS_BASE_URL"))
    value.add_argument(
        "--api-key-env",
        default=os.environ.get("VERAS_API_KEY_ENV", DEFAULT_API_KEY_ENV),
    )
    value.add_argument("--wire-api", choices=("responses", "chat"), default="responses")
    value.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh"),
        default="high",
    )
    value.add_argument("--context-window", type=int, default=272000)
    value.add_argument("--auto-compact-token-limit", type=int, default=240000)
    value.add_argument("--concurrency", type=int, default=1)
    value.add_argument("--timeout-scale", type=float, default=1.0)
    value.add_argument(
        "--reserve-fraction", type=float, default=DEFAULT_RESERVE_FRACTION
    )
    value.add_argument(
        "--min-recovery-seconds", type=float, default=DEFAULT_MIN_RECOVERY_SECONDS
    )
    value.add_argument("--local-solver-cap-seconds", type=float, default=180.0)
    value.add_argument("--max-transport-retries", type=int, default=4)
    value.add_argument("--force", action="store_true")
    value.add_argument("--dry-run", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    if args.benchmark_root:
        os.environ["ORAGENTBENCH_ROOT"] = str(args.benchmark_root.resolve())
    if not args.model:
        raise ValueError("--model or VERAS_MODEL is required")
    if not args.base_url:
        raise ValueError("--base-url or VERAS_BASE_URL is required")
    if not 0.0 <= args.reserve_fraction <= 0.5:
        raise ValueError("reserve-fraction must be between 0.0 and 0.5")
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
            if name != args.api_key_env and name.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
                environment.pop(name)
        environment[args.api_key_env] = api_key
        return environment

    def write_json(path: Path, value: Any) -> None:
        value = public_result_metadata(value)
        if isinstance(value, dict) and "credential_persisted" in value:
            value["credential_persisted"] = False
            value["reasoning_effort"] = args.reasoning_effort
            value["veras_harness"] = HARNESS_NAME
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
