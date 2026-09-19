"""Contract tests for the wire models.

These are cheap to write and they pin down the two rules from the old API:
no silent defaults for missing fields, and no unknown fields accepted.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from arducam_ptz.models import (
    AutofocusRequest,
    AutofocusResult,
    AutofocusSample,
    ClampReport,
    FocusState,
    Health,
    JobState,
    JobStatus,
    NudgeRequest,
    Photo,
    PositionRequest,
    Region,
    SystemState,
)


def test_requests_refuse_unknown_fields() -> None:
    """A typo must be a 422, not a quiet move to the default angle."""
    with pytest.raises(ValidationError):
        PositionRequest(pan=90, pang=90)  # typos as the old API would have swallowed
    with pytest.raises(ValidationError):
        NudgeRequest(delta=5)  # the old pipeline used "delta", this one uses pan/tilt


def test_position_request_axes_are_optional() -> None:
    req = PositionRequest(pan=100)
    assert req.pan == 100
    assert req.tilt is None


def test_default_nudge_does_nothing() -> None:
    assert NudgeRequest().pan == 0 and NudgeRequest().tilt == 0


def test_region_must_lie_inside_the_frame() -> None:
    Region(x=0.1, y=0.1, width=0.5, height=0.5)
    with pytest.raises(ValidationError, match="inside the frame"):
        Region(x=0.8, y=0.1, width=0.5, height=0.5)


def test_region_helpers_keep_the_window_inside() -> None:
    centred = Region.centred(0.4)
    assert (centred.x, centred.y) == pytest.approx((0.3, 0.3))
    # A tap at the very corner must still produce a legal window.
    corner = Region.around(0.98, 0.01, 0.25)
    assert corner.x + corner.width <= 1.0
    assert corner.y >= 0.0


def test_clamp_report_is_machine_readable() -> None:
    clamp = ClampReport(axis="pan", requested=150, applied=120, reason="travel_limit")
    assert clamp.reason == "travel_limit"
    with pytest.raises(ValidationError):
        ClampReport(axis="roll", requested=1, applied=1, reason="travel_limit")


def test_job_terminal_states() -> None:
    assert JobStatus.COMPLETED.terminal and JobStatus.FAILED.terminal
    assert JobStatus.CANCELLED.terminal
    assert not JobStatus.RUNNING.terminal and not JobStatus.PENDING.terminal


def test_job_freshness_uses_updated_at() -> None:
    job = JobState(
        id="abc",
        kind="autofocus",
        status=JobStatus.COMPLETED,
        progress=1.0,
        created_at=100.0,
        updated_at=100.0,
        poll_url="/api/v1/focus/autofocus/abc",
    )
    assert job.is_fresh(ttl_s=60, now=120.0)
    assert not job.is_fresh(ttl_s=60, now=200.0)


def test_state_payload_round_trips_as_json() -> None:
    """The UI and CLI both parse this with a plain JSON decoder, so keep it flat."""
    payload = SystemState(
        health=Health(status="simulated", version="0.1.0", uptime_s=1.5, components=[]),
        ptz={
            "available": True,
            "simulated": True,
            "pan": {"angle": 90, "min": 60, "max": 120},
            "tilt": {"angle": 85, "min": 15, "max": 145},
        },
        focus=FocusState(available=True, simulated=True, position=1024, device="/dev/x"),
        stream={
            "running": True,
            "width": 960,
            "height": 540,
            "fps": 27.0,
            "kilobytes_per_s": 300.0,
            "subscribers": 1,
            "sequence": 42,
        },
    )
    decoded = json.loads(payload.model_dump_json())
    assert decoded["ptz"]["pan"]["angle"] == 90
    assert decoded["focus"]["position"] == 1024
    assert decoded["stream"]["sequence"] == 42


def test_autofocus_result_keeps_every_sample() -> None:
    """Samples are what make a bad focus hunt debuggable after the fact."""
    result = AutofocusResult(
        position=1200,
        score=42.5,
        samples=[
            AutofocusSample(phase="coarse", position=0, score=3.0),
            AutofocusSample(phase="fine", position=1200, score=42.5),
        ],
        elapsed_s=3.2,
    )
    assert len(result.samples) == 2
    assert result.samples[-1].phase == "fine"


def test_photo_carries_a_thumbnail_url() -> None:
    """Phone galleries must not download 16 MP stills to draw a grid."""
    photo = Photo(
        filename="photo_20260101-120000.jpg",
        url="/api/v1/captures/photo_20260101-120000.jpg",
        thumb_url="/api/v1/captures/photo_20260101-120000.jpg?max_dim=320",
        bytes=1234,
        created_at="2026-01-01T12:00:00",
    )
    assert "max_dim=320" in photo.thumb_url


def test_autofocus_request_accepts_a_tap_region() -> None:
    request = AutofocusRequest(region=Region.around(0.25, 0.75))
    assert request.region is not None
    assert AutofocusRequest().region is None
