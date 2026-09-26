"""Stable VERAS controller constants."""

from __future__ import annotations

import os
import re
from pathlib import Path

HARNESS_NAME = "VERAS"
DEFAULT_RESERVE_FRACTION = 0.30
DEFAULT_MIN_RECOVERY_SECONDS = 120.0
DEFAULT_CHALLENGER_FRACTION = 0.22
MIN_INTERVENTION_SECONDS = 45.0


def benchmark_root() -> Path:
    configured = os.environ.get("ORAGENTBENCH_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repository = Path(__file__).resolve().parents[3]
    return repository.parent / "ORAgentBench"


OR_BENCH = benchmark_root()

SUBMISSION_REF = re.compile(
    r"(?:/app/)?submissions/([A-Za-z0-9_.-]+\.(?:csv|json|md|py))",
    re.IGNORECASE,
)

RUNTIME_NOTE = """

Runtime note from the benchmark harness:
- PySCIPOpt is provided at `/opt/or-deps`; invoke Python as `PYTHONPATH=/opt/or-deps python`.
- Do not search for or depend on conda. Validate the import with
  `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`.
"""

TRANSIENT_TRANSPORT = re.compile(
    r"(?:503 Service Unavailable|429 Too Many Requests|exceeded rate limit|"
    r"stream disconnected before completion|connection reset|upstream connect error|"
    r"temporarily unavailable|timed out waiting for response)",
    re.IGNORECASE,
)

RATE_LIMIT = re.compile(
    r"(?:429 Too Many Requests|exceeded rate limit|requests? per minute|\bQPM\b)",
    re.IGNORECASE,
)

DAILY_LIMIT = re.compile(
    r"(?:daily limits? exceeded|daily quota|quota exhausted|insufficient quota)",
    re.IGNORECASE,
)
