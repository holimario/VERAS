"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .aerospace import observe_aerospace_autoclave
from .common import report, violation
from .flood_relief import observe_flood_relief
from .liner import observe_online_liner
from .medical_waste import observe_medical_waste
from .petroleum import observe_petroleum_inventory
from .specialized import (
    observe_evtol_timeline,
    observe_microgrid_generator_reserve,
    observe_online_bus_schema,
    observe_sterile_nonnegative_times,
)

OBSERVERS: dict[str, Callable[[Path, Path], dict[str, Any]]] = {
    "aerospace_composite_autoclave_scheduling": observe_aerospace_autoclave,
    "online_liner_slot_empty_repositioning": observe_online_liner,
    "petroleum_terminal_dro_inventory": observe_petroleum_inventory,
    "electric_medical_waste_lrp": observe_medical_waste,
    "flood_relief_multimodal_routing": observe_flood_relief,
    "online_electric_bus_depot_replanning": observe_online_bus_schema,
    "robust_microgrid_islanding_dispatch": observe_microgrid_generator_reserve,
    "evtol_vertiport_recovery": observe_evtol_timeline,
    "sterile_processing_robust_schedule": observe_sterile_nonnegative_times,
}


def observe_submission(task: str, app: Path, artifact: Path | None) -> dict[str, Any]:
    observer = OBSERVERS.get(task)
    if observer is None:
        return {
            "supported": False,
            "observer": None,
            "coverage_complete": False,
            "covered_constraint_groups": [],
            "safe": None,
            "violation_count": 0,
            "max_normalized_residual": None,
            "violations": [],
            "metrics": {},
        }
    if artifact is None or not artifact.is_file():
        return report(
            observer.__name__,
            [],
            [violation("artifact", "Observer cannot inspect a missing artifact")],
        )
    try:
        return observer(app, artifact)
    except Exception as error:
        return report(
            observer.__name__,
            [],
            [
                violation(
                    "observer-error",
                    f"Independent observer failed: {type(error).__name__}: {error}",
                )
            ],
        )
