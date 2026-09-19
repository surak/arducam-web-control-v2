"""The HTTP wire contract, as pydantic models.

This module imports nothing from ``hardware``/``services`` on purpose: a client
(the bundled CLI, a phone app, a shell script reading ``/docs``) can install the
schema without a camera, an I2C bus or libcamera. `arducam_ptz.cli` uses exactly
these classes, so the CLI and the server cannot disagree about the shape of a
response.

Two contract rules, both learned from the old API:

1. **Never lie about what happened.** The old ``/api/pan`` returned
   ``{"success": true, "angle": <the clamped-to-1-degree value>}`` while the
   client believed it had moved where it asked. Here every command answers with
   the applied value *and* a machine-readable list of clamps.
2. **Never invent a default for a missing field.** The old handlers did
   ``data.get("angle", 90)``, so a client with a typo'd key silently re-centred
   the camera. Request models here forbid unknown fields (HTTP 422 instead).
"""

from __future__ import annotations

import time
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Axis = Literal["pan", "tilt"]


class ApiModel(BaseModel):
    """Base for responses: lenient about extra keys so adding fields is not breaking."""

    model_config = ConfigDict(extra="ignore")


class RequestModel(BaseModel):
    """Base for requests: unknown keys are an error, never a silent default."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# pan / tilt
# --------------------------------------------------------------------------- #
class AxisState(ApiModel):
    """One servo axis, in the angle space clients work in (0-180 nominal)."""

    angle: float = Field(description="Current commanded angle in degrees.")
    min: float = Field(description="Lowest angle this axis may be driven to (hardware limit).")
    max: float = Field(description="Highest angle this axis may be driven to (hardware limit).")
    inverted: bool = Field(
        default=False,
        description="True when the axis is mounted mirrored, i.e. 'increasing' looks reversed.",
    )
    released: bool = Field(
        default=False,
        description="True while the servo is unpowered (no holding torque, axis can be pushed).",
    )


class ClampReport(ApiModel):
    """Why an axis did not end up exactly where the client asked."""

    axis: Axis
    requested: float = Field(description="Angle the client asked for.")
    applied: float = Field(description="Angle actually commanded.")
    reason: Literal["travel_limit", "max_step"] = Field(
        description="travel_limit: outside the mechanical range. max_step: limited by "
        "pan_tilt.max_step_degrees on purpose.",
    )


class PtzState(ApiModel):
    """Pan/tilt state. Returned by GET and by every command, so clients get one shape."""

    available: bool = Field(description="False when no PCA9685 answered on the I2C bus.")
    simulated: bool = Field(default=False, description="True when a simulated backend is in use.")
    pan: AxisState
    tilt: AxisState
    #: Empty when the last command was applied as asked; otherwise says what was clamped.
    clamps: list[ClampReport] = Field(default_factory=list)
    detail: str | None = Field(default=None, description="Human-readable backend status/error.")


class PositionRequest(RequestModel):
    """Absolute move. Give one axis or both; omitted axes do not move."""

    pan: float | None = None
    tilt: float | None = None


class NudgeRequest(RequestModel):
    """Relative move in degrees. Positive pan = right, positive tilt = down."""

    pan: float = 0.0
    tilt: float = 0.0


# --------------------------------------------------------------------------- #
# focus
# --------------------------------------------------------------------------- #
class Region(ApiModel):
    """A rectangular area of the frame in normalised coordinates (0-1, origin top-left).

    Used by tap-to-focus: the autofocus sweep scores only this area, which is how
    you focus on the thing you tapped instead of on whatever is in the middle.
    """

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside_frame(self) -> Region:
        if self.x + self.width > 1.0001 or self.y + self.height > 1.0001:
            raise ValueError("region must lie inside the frame (x+width <= 1, y+height <= 1)")
        return self

    @classmethod
    def centred(cls, size: float = 0.25) -> Region:
        """Default autofocus window: the middle of the frame."""
        half = size / 2
        return cls(x=0.5 - half, y=0.5 - half, width=size, height=size)

    @classmethod
    def around(cls, x: float, y: float, size: float = 0.25) -> Region:
        """A window centred on a tap, pushed back inside the frame if needed."""
        half = size / 2
        return cls(
            x=min(max(x - half, 0.0), 1.0 - size),
            y=min(max(y - half, 0.0), 1.0 - size),
            width=size,
            height=size,
        )


class FocusState(ApiModel):
    """Focus motor state plus the current/last autofocus job."""

    available: bool = Field(description="False when no V4L2 subdevice exposes focus_absolute.")
    simulated: bool = Field(default=False)
    position: int | None = Field(default=None, description="Raw focus_absolute value.")
    min: int = 0
    max: int = 4095
    step: int = 1
    near_is_high: bool = Field(
        default=True,
        description="True when a larger position focuses on something nearer, which is how the "
        "AK7375 on this rig behaves. Clients use it to label the slider honestly.",
    )
    device: str | None = Field(default=None, description="e.g. /dev/v4l-subdev3")
    autofocus: JobState | None = Field(default=None, description="Running or most recent job.")
    detail: str | None = None


class FocusPositionRequest(RequestModel):
    """Absolute focus motor position (clamped to the motor's range)."""

    position: int


class FocusNudgeRequest(RequestModel):
    """Relative focus move in raw motor steps."""

    steps: int = Field(description="Signed number of focus_absolute steps.")


class AutofocusRequest(RequestModel):
    """Start an autofocus sweep. ``region`` defaults to a centred window."""

    region: Region | None = None


# --------------------------------------------------------------------------- #
# long-running jobs (202 + polling)
# --------------------------------------------------------------------------- #
class JobStatus(StrEnum):
    """Terminal states are completed/failed/cancelled; the others are interim."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class AutofocusSample(ApiModel):
    """One position that was measured during a sweep - kept for debugging focus hunts."""

    phase: Literal["coarse", "fine"]
    position: int
    score: float = Field(description="Laplacian variance in the focus window; higher is sharper.")


class AutofocusResult(ApiModel):
    position: int = Field(description="Motor position the sweep settled on.")
    score: float
    samples: list[AutofocusSample] = Field(default_factory=list)
    region: Region | None = None
    elapsed_s: float


class JobState(ApiModel):
    """Progress of a background job. Clients poll this until ``status`` is terminal."""

    id: str
    kind: Literal["autofocus"]
    status: JobStatus
    progress: float = Field(ge=0, le=1, description="0-1, for a progress bar.")
    phase: str | None = Field(default=None, description="e.g. 'coarse sweep', 'settling'.")
    created_at: float
    updated_at: float
    poll_url: str = Field(description="Where to poll. Convention: /api/v1/.../<id>.")
    result: AutofocusResult | None = None
    error: str | None = None

    def is_fresh(self, ttl_s: float, now: float | None = None) -> bool:
        """True while the job is worth keeping in the registry."""
        return (now if now is not None else time.time()) - self.updated_at < ttl_s


# --------------------------------------------------------------------------- #
# captures
# --------------------------------------------------------------------------- #
class Photo(ApiModel):
    filename: str
    url: str = Field(description="Full-resolution image, same origin as the API.")
    thumb_url: str = Field(description="Small preview for gallery grids - phones should use this.")
    bytes: int
    created_at: datetime
    width: int | None = None
    height: int | None = None


# --------------------------------------------------------------------------- #
# stream + health
# --------------------------------------------------------------------------- #
class StreamStats(ApiModel):
    """Live preview health. The UI shows these so a frozen stream is visible, not guessed."""

    running: bool
    width: int
    height: int
    fps: float = Field(description="Frames per second published over the last few seconds.")
    kilobytes_per_s: float
    subscribers: int = Field(description="How many clients are watching right now.")
    sequence: int = Field(description="Monotonic frame counter; use it to detect staleness.")
    last_frame_age_s: float | None = None


class ComponentHealth(ApiModel):
    name: Literal["camera", "focus", "pan_tilt"]
    available: bool
    simulated: bool
    detail: str | None = None


class Health(ApiModel):
    """Everything a monitor (or a human debugging at 2 a.m.) needs in one request."""

    status: Literal["ok", "degraded", "simulated"]
    version: str
    uptime_s: float
    components: list[ComponentHealth]
    config_source: str | None = Field(default=None, description="Config file actually in use.")


class SystemState(ApiModel):
    """One request the polling UI can use instead of three.

    Fewer round trips matters on a phone that is simultaneously pulling an MJPEG
    stream: the old page polled ``/api/status`` every 5 s and got a payload whose
    ``camera.simulated`` field did not exist (so "simulation mode" detection never
    worked). This object is complete and explicit.
    """

    health: Health
    ptz: PtzState
    focus: FocusState
    stream: StreamStats


# FocusState mentions JobState before it is defined (it reads better that way);
# resolve the forward reference explicitly instead of leaving it to the first
# validation call at runtime.
FocusState.model_rebuild()
