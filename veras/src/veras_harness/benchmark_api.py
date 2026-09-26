"""Lazy bridge to an installed ORAgentBench local runner."""

from __future__ import annotations

import sys

from .constants import benchmark_root


OR_BENCH = benchmark_root()
runner = OR_BENCH / "local_runner"
if str(runner) not in sys.path:
    sys.path.insert(0, str(runner))

try:
    import harness_benchmark as benchmark
except ModuleNotFoundError as error:
    raise ModuleNotFoundError(
        "ORAgentBench is required for controller execution. Set "
        "ORAGENTBENCH_ROOT to its checkout directory."
    ) from error
