"""Pan/tilt endpoints.

The contract, unchanged from the old app in *intent* but honest in *answers*: every
command returns the full :class:`PtzState`, including any clamps the server applied.
A client therefore never has to guess whether its requested angle was reached, and
the UI can show "you asked for 175°, the rig stops at 120°" instead of silently
moving somewhere else.

``POST /ptz/nudge`` exists for the on-screen joystick. It is deliberately cheap: the
joystick can fire a dozen requests per second, the service coalesces them, and the
servos see a smooth motion rather than a burst.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from arducam_ptz.api.deps import AuthDep, ContextDep
from arducam_ptz.models import NudgeRequest, PositionRequest, PtzState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ptz", tags=["pan-tilt"], dependencies=[AuthDep])


@router.get("", response_model=PtzState, summary="Current pan/tilt target state")
def get_ptz(context: ContextDep) -> PtzState:
    return context.ptz.state()


@router.post("/move", response_model=PtzState, summary="Absolute move (degrees)")
def move(request: PositionRequest, context: ContextDep) -> PtzState:
    """Move to absolute angles. Omitted axes stay where they are."""
    return context.ptz.goto(pan=request.pan, tilt=request.tilt)


@router.post("/nudge", response_model=PtzState, summary="Relative move (degrees)")
def nudge(request: NudgeRequest, context: ContextDep) -> PtzState:
    """Move by a delta. Positive pan turns right, positive tilt turns down."""
    return context.ptz.nudge(pan=request.pan, tilt=request.tilt)


@router.post("/center", response_model=PtzState, summary="Move to the configured centre")
def center(context: ContextDep) -> PtzState:
    return context.ptz.center()


@router.post("/release", response_model=PtzState, summary="Cut servo pulses (axes go limp)")
def release(context: ContextDep) -> PtzState:
    """Stop holding torque. The next move re-arms the servos automatically."""
    return context.ptz.release()
