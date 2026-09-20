"""Focus endpoints, including the asynchronous autofocus sweep.

The autofocus contract is the 202 + poll pattern:

    POST /api/v1/focus/autofocus            -> 202 {status: "running", poll_url: ...}
    GET  /api/v1/focus/autofocus/{job_id}   -> progress, then the result
    POST /api/v1/focus/autofocus/{job_id}/cancel

Why not simply block until the sweep finishes? A sweep takes 3-8 seconds. In the old
app that wait happened inside the request, with the preview frozen and no progress
feedback: on a phone the request timed out, the browser gave up, and the camera
carried on turning with no way to know what happened. A job id costs one extra round
trip and makes the operation observable, cancellable and impossible to lose track of.

A second sweep while one is running answers **409** with the running job's id, so a
client that double-taps the focus button attaches to the sweep in flight instead of
starting a fight over the focus motor.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from arducam_ptz.api.deps import AuthDep, ContextDep
from arducam_ptz.models import (
    AutofocusRequest,
    FocusNudgeRequest,
    FocusPositionRequest,
    FocusState,
    JobState,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/focus", tags=["focus"], dependencies=[AuthDep])


@router.get("", response_model=FocusState, summary="Focus motor state")
def get_focus(context: ContextDep) -> FocusState:
    """Position, limits, and the state of the most recent autofocus job (if any)."""
    return context.focus.state()


@router.post("", response_model=FocusState, summary="Set the focus position")
def set_focus(request: FocusPositionRequest, context: ContextDep) -> FocusState:
    """Absolute motor position. Cancels a running autofocus: manual input wins."""
    return context.focus.set_position(request.position)


@router.post("/nudge", response_model=FocusState, summary="Step the focus")
def nudge_focus(request: FocusNudgeRequest, context: ContextDep) -> FocusState:
    """Relative move in motor steps, for a fine-focus rocker in the UI."""
    return context.focus.nudge(request.steps)


@router.post(
    "/autofocus",
    response_model=JobState,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an autofocus sweep (returns a job to poll)",
    responses={409: {"description": "A sweep is already running; body carries its job id"}},
)
def start_autofocus(request: AutofocusRequest, context: ContextDep) -> JobState:
    """Begin a contrast-detect sweep in the background. Body: optional ``region``."""
    return context.focus.start_autofocus(request.region)


@router.get("/autofocus/{job_id}", response_model=JobState, summary="Poll an autofocus job")
def autofocus_job(job_id: str, context: ContextDep) -> JobState:
    """Progress while running; position, score and every sample once completed."""
    job = context.focus.job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown or expired autofocus job",
        )
    return job


@router.post("/autofocus/{job_id}/cancel", response_model=JobState, summary="Cancel a sweep")
def cancel_autofocus(job_id: str, context: ContextDep) -> JobState:
    """Ask the sweep to stop. It stops between samples and reports ``cancelled``."""
    job = context.focus.cancel(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown or expired autofocus job",
        )
    return job
