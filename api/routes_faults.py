"""Fault injection control.

``POST /faults/{name}/start``, ``POST /faults/{name}/stop`` and ``GET /faults``.

Exposed in the demo on purpose -- the simulator is disclosed, not hidden. The
telemetry is synthetic and says so; the investigation that reads it is real.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.runtime import stage_runner
from simulator.faults.base import FAULT_NAMES, build_fault

router = APIRouter(prefix="/faults", tags=["faults"])


class FaultState(BaseModel):
    name: str
    active: bool
    elapsed_s: float
    summary: str
    #: Seconds from start until the evidence the agent needs exists. The UI
    #: shows this so nobody spends a metered model call investigating a fault
    #: that has not finished becoming one.
    maturity_s: float
    matured: bool
    matures_in_s: float


class FaultAction(BaseModel):
    name: str
    active: bool
    message: str


#: Built once so the UI can label the buttons without starting anything.
_SUMMARIES: dict[str, str] = {n: build_fault(n).summary for n in FAULT_NAMES}


def _require_known(name: str) -> None:
    if name not in FAULT_NAMES:
        raise HTTPException(
            status_code=404,
            detail=f"unknown fault '{name}'; known: {', '.join(FAULT_NAMES)}",
        )


@router.get("", response_model=list[FaultState])
def list_faults() -> list[FaultState]:
    """Current state of every fault."""
    state = stage_runner.fault_state()
    return [
        FaultState(
            name=name,
            active=bool(state.get(name, {}).get("active", False)),
            elapsed_s=float(state.get(name, {}).get("elapsed_s", 0.0)),
            summary=_SUMMARIES[name],
            maturity_s=float(state.get(name, {}).get("maturity_s", 0.0)),
            matured=bool(state.get(name, {}).get("matured", False)),
            matures_in_s=float(state.get(name, {}).get("matures_in_s", 0.0)),
        )
        for name in FAULT_NAMES
    ]


@router.post("/{name}/start", response_model=FaultAction)
def start_fault(name: str) -> FaultAction:
    """Inject a fault into the live telemetry."""
    _require_known(name)
    if not stage_runner.running:
        raise HTTPException(status_code=503, detail="simulator is not running")
    stage_runner.start_fault(name)
    return FaultAction(name=name, active=True, message=f"{name} started")


@router.post("/{name}/stop", response_model=FaultAction)
def stop_fault(name: str) -> FaultAction:
    """Stop a fault. Signals revert to baseline on the next tick."""
    _require_known(name)
    if not stage_runner.running:
        raise HTTPException(status_code=503, detail="simulator is not running")
    stage_runner.stop_fault(name)
    return FaultAction(name=name, active=False, message=f"{name} stopped")
