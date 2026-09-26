"""Compatibility facade for the VERAS controller API."""

from .audits import *  # noqa: F401,F403
from .constants import *  # noqa: F401,F403
from .controller import make_run_task
from .protocols import *  # noqa: F401,F403
from .runtime import *  # noqa: F401,F403


def main() -> None:
    from .cli import main as cli_main

    cli_main()
