"""Health and aggregate state.

Two endpoints because clients have two different needs:

* ``GET /api/v1/health`` - compact verdict for monitoring and for the CLI's
  ``status`` command. Reports every component with its reason when something is
  missing, instead of a single opaque "false".
* ``GET /api/v1/state`` - everything the UI polls (health + pan/tilt + focus +
  stream statistics) in **one** response. The old app needed four separate requests
  to draw its one screen, which on a phone on a slow link meant four chances to time
  out and a UI that flickered between stale states.
"""

from __future__ import annotations

from fastapi import APIRouter

from arducam_ptz.api.deps import AuthDep, ContextDep
from arducam_ptz.models import Health, StreamStats, SystemState

router = APIRouter(tags=["status"], dependencies=[AuthDep])


@router.get("/health", response_model=Health, summary="Component availability summary")
def health(context: ContextDep) -> Health:
    """Never fails: a camera with a dead servo answers 200 with ``degraded``."""
    return context.health()


@router.get("/state", response_model=SystemState, summary="Everything the UI polls for")
def state(context: ContextDep) -> SystemState:
    """One round trip for the whole UI. Poll at 1-2 Hz; the stream carries the video."""
    return context.state()


@router.get("/stream/stats", response_model=StreamStats, summary="Preview stream statistics")
def stream_stats(context: ContextDep) -> StreamStats:
    return StreamStats.model_validate(context.hub.stats())
