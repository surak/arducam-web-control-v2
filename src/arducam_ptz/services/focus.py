"""Focus: manual positioning and contrast-detect autofocus.

**Why a sweep instead of the camera's autofocus.** On this stack libcamera exposes
only ``focus_absolute`` for the IMX519's AK7375 motor: there is no ``AfMode``, no
``AfTrigger`` and no autofocus algorithm in the tuning file. The old project hit
exactly the same wall and its sweep was the right answer; what this version fixes
is *how* it runs:

* it executes as a background job, so the HTTP request returns in milliseconds and
  the phone UI shows progress instead of hanging;
* it samples frames from the :class:`FrameHub` and never takes the camera lock, so
  the preview keeps streaming while the lens moves (the old sweep stopped the
  stream, froze every viewer for 5-8 s, and made the whole page look broken);
* it waits for a frame that is *newer than the lens move* before scoring, so it can
  never score a stale image and send the sweep the wrong way;
* every measurement is kept in the result, so a hunt that ends somewhere silly can
  be diagnosed afterwards instead of guessed at;
* it aborts with a reason ("camera busy", "no frames") instead of leaving the lens
  parked at a random place with no explanation.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np

from arducam_ptz.config import FocusSettings
from arducam_ptz.hardware.base import CameraHardware, FocusMotor
from arducam_ptz.models import (
    AutofocusResult,
    AutofocusSample,
    FocusState,
    JobState,
    JobStatus,
    Region,
)
from arducam_ptz.services.frames import FrameHub
from arducam_ptz.services.jobs import Job, JobRegistry

logger = logging.getLogger(__name__)

try:  # pragma: no cover - Pillow is a hard dependency, this is belt and braces
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment,misc]

#: Crops smaller than this are meaningless to score (a few pixels of noise).
_MIN_CROP_PX = 16


class FocusBusyError(RuntimeError):
    """A sweep is already running. Carries its job id so the client can attach."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"autofocus job {job_id} is already running")
        self.job_id = job_id


class _SweepAborted(Exception):
    """Internal control flow: stop the sweep, with a reason for the client."""

    def __init__(self, reason: str, *, cancelled: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cancelled = cancelled


@dataclass(frozen=True, slots=True)
class SharpnessMeasurement:
    """One scored lens position."""

    phase: Literal["coarse", "fine"]
    position: int
    score: float


def sharpness(jpeg: bytes, region: Region | None = None) -> float:
    """Laplacian variance of a frame region: higher means sharper.

    This is the classic contrast-detection metric and the same one the old app
    used, so the mouse-ear region in the middle of the frame still behaves the
    same. Two details mattered:

    * decode with Pillow rather than re-deriving an array from the camera, because
      the frame we score must be the frame the *viewer* sees;
    * guard the variance against a featureless crop (a dark wall gives variance 0,
      and a division somewhere downstream of that is how a focus hunt ends up
      oscillating forever).
    """
    if Image is None:  # pragma: no cover
        raise RuntimeError("Pillow is required to score frames")
    with Image.open(io.BytesIO(jpeg)) as image:
        grey = image.convert("L")
        width, height = grey.size
        if region is not None:
            left = int(region.x * width)
            top = int(region.y * height)
            right = int((region.x + region.width) * width)
            bottom = int((region.y + region.height) * height)
            # A tiny window on a low-resolution preview can round to nothing;
            # widen it to something measurable.
            right = max(right, left + _MIN_CROP_PX)
            bottom = max(bottom, top + _MIN_CROP_PX)
            grey = grey.crop((left, top, min(right, width), min(bottom, height)))
        array = np.asarray(grey, dtype=np.float32)
    if array.ndim != 2 or min(array.shape) < 3:
        return 0.0
    laplacian = (
        -4.0 * array[1:-1, 1:-1] + array[:-2, 1:-1] + array[2:, 1:-1] + array[1:-1, :-2] + array[1:-1, 2:]
    )
    variance = float(laplacian.var())
    return 0.0 if not np.isfinite(variance) else variance


class FocusService:
    """Manual focus plus the autofocus job."""

    def __init__(
        self,
        settings: FocusSettings,
        motor: FocusMotor,
        hub: FrameHub,
        jobs: JobRegistry,
        camera: CameraHardware | None = None,
        log: logging.Logger | None = None,
        poll_url: str = "/api/v1/focus/autofocus/{job_id}",
    ) -> None:
        self.settings = settings
        self.motor = motor
        self.hub = hub
        self.jobs = jobs
        self.camera = camera
        self.log = log or logger
        self.poll_url = poll_url
        self._lock = threading.RLock()
        self._last_error: str | None = None

    # ------------------------------------------------------------------ state
    def state(self) -> FocusState:
        availability = self.motor.availability()
        limits = None
        try:
            limits = self.motor.limits()
        except Exception:  # not initialised: report the availability reason instead
            limits = None
        position = None
        try:
            position = self.motor.get_position()
        except Exception as exc:  # pragma: no cover - defensive
            self.log.debug("reading focus position failed: %s", exc)
        detail = availability.detail
        if self._last_error:
            detail = f"{detail}; last error: {self._last_error}" if detail else self._last_error
        job = self.jobs.latest("autofocus")
        return FocusState(
            available=availability.available,
            simulated=availability.simulated,
            position=position,
            min=limits.minimum if limits else 0,
            max=limits.maximum if limits else 4095,
            step=limits.step if limits else 1,
            near_is_high=self.settings.near_is_high,
            device=getattr(self.motor, "device", None),
            autofocus=job.to_model() if job is not None else None,
            detail=detail,
        )

    # ------------------------------------------------------------------ manual
    def set_position(self, position: int) -> FocusState:
        """Move the lens. Cancels a running autofocus: manual input wins."""
        self._cancel_running("manual focus set")
        try:
            self.motor.set_position(int(position))
            self._last_error = None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        return self.state()

    def nudge(self, steps: int) -> FocusState:
        """Move the lens by a relative number of steps."""
        current = self.motor.get_position() or 0
        return self.set_position(current + int(steps))

    def _cancel_running(self, reason: str) -> None:
        for job in self.jobs.active("autofocus"):
            self.log.info("cancelling autofocus job %s: %s", job.id, reason)
            job.cancel()

    # ------------------------------------------------------------------ autofocus
    def start_autofocus(self, region: Region | None = None) -> JobState:
        """Begin a sweep in the background and return its job immediately."""
        existing = self.jobs.active("autofocus")
        if existing:
            # One sweep at a time: two sweeps would fight over the motor and both
            # would produce nonsense. 409 at the API level, with the running job's
            # id so a client can attach to it instead of retrying blindly.
            raise FocusBusyError(existing[0].id)
        window = region or Region.centred()
        job = self.jobs.create("autofocus", lambda job_id: self.poll_url.format(job_id=job_id))
        job.update(status=JobStatus.RUNNING, progress=0.0, phase="starting")
        worker = threading.Thread(
            target=self._run_sweep,
            args=(job, window),
            name=f"autofocus-{job.id}",
            daemon=True,
        )
        worker.start()
        return job.to_model()

    def job(self, job_id: str) -> JobState | None:
        job = self.jobs.get(job_id)
        return None if job is None else job.to_model()

    def cancel(self, job_id: str) -> JobState | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.cancel()
        return job.to_model()

    # ------------------------------------------------------------------ the sweep
    def _run_sweep(self, job: Job, region: Region) -> None:
        """Coarse pass over the whole travel, then a fine pass around the winner."""
        started = time.monotonic()
        samples: list[AutofocusSample] = []
        try:
            limits = self.motor.limits()
            coarse = self._linspace(limits.minimum, limits.maximum, self.settings.coarse_steps)
            total = len(coarse) + self.settings.fine_steps
            done = 0

            for position in coarse:
                self._checkpoint(job)
                measurement = self._measure(position, region, "coarse")
                samples.append(measurement)
                done += 1
                job.update(progress=done / total, phase="coarse sweep")

            best_coarse = max(samples, key=lambda sample: sample.score)
            step = (coarse[1] - coarse[0]) if len(coarse) > 1 else (limits.maximum - limits.minimum)
            fine = [
                p
                for p in self._linspace(
                    best_coarse.position - step / 2,
                    best_coarse.position + step / 2,
                    self.settings.fine_steps,
                )
                if limits.minimum <= p <= limits.maximum
            ]
            job.update(phase="fine sweep")
            for position in fine:
                self._checkpoint(job)
                measurement = self._measure(position, region, "fine")
                samples.append(measurement)
                done += 1
                job.update(progress=done / total)

            best = max(samples, key=lambda sample: sample.score)
            applied = self.motor.set_position(best.position)
            result = AutofocusResult(
                position=applied,
                score=best.score,
                samples=samples,
                region=region,
                elapsed_s=round(time.monotonic() - started, 2),
            )
            self.log.info(
                "autofocus done: position=%d score=%.1f (%d samples, %.1fs)",
                applied,
                best.score,
                len(samples),
                result.elapsed_s,
            )
            job.succeed(result)
        except _SweepAborted as aborted:
            if aborted.cancelled:
                self.log.info("autofocus %s cancelled after %d samples", job.id, len(samples))
                job.cancelled_finally()
            else:
                self.log.warning("autofocus %s aborted: %s", job.id, aborted.reason)
                job.fail(aborted.reason)
            self._last_error = None if aborted.cancelled else aborted.reason
        except Exception as exc:  # pragma: no cover - defensive: never crash the thread
            self.log.exception("autofocus %s failed unexpectedly", job.id)
            job.fail(f"{type(exc).__name__}: {exc}")
            self._last_error = f"{type(exc).__name__}: {exc}"

    def _checkpoint(self, job: Job) -> None:
        """Between samples: honour cancellation."""
        if job.cancelled:
            raise _SweepAborted(f"cancelled by client ({job.id})", cancelled=True)

    def _measure(self, position: int, region: Region, phase: str) -> AutofocusSample:
        """Move to ``position``, wait for it to settle, and score a frame taken after.

        Order matters and is the whole reason this is one method:

        1. note the current time,
        2. move the lens,
        3. wait out the mechanical settle,
        4. score the first frame that arrived *after the move*, skipping the frames
           that were already in the image pipeline when the lens started moving.

        Skipping a frame is not paranoia: a frame published microseconds after the move
        can still carry pre-move pixels. Scoring it attributes the old position's
        sharpness to the new one, and with an even worse coincidence reports "the
        blurriest position is the sharpest" and parks the lens there.
        """
        moved_at = time.monotonic()
        try:
            self.motor.set_position(position)
        except Exception as exc:
            raise _SweepAborted(f"focus motor failed at position {position}: {exc}") from exc
        if self.settings.settle_ms:
            time.sleep(self.settings.settle_ms / 1000.0)
        frame = self._fresh_frame(moved_at)
        score = sharpness(frame, region)
        self.log.debug("focus sample %s %d -> %.1f", phase, position, score)
        return AutofocusSample(phase=phase, position=position, score=score)  # type: ignore[arg-type]

    def _fresh_frame(self, moved_at: float) -> bytes:
        """Wait for a frame exposed after ``moved_at``, or explain why not."""
        timeout = self.settings.sample_timeout_ms / 1000.0
        deadline = time.monotonic() + timeout
        # A still capture briefly changes sensor mode; frames pause for ~0.5 s.
        # Waiting is right; failing instantly would make "tap shutter then focus"
        # fail for no good reason.
        while self.camera is not None and self.camera.busy and time.monotonic() < deadline:
            time.sleep(0.05)
        remaining = max(0.05, deadline - time.monotonic())
        frame = self.hub.sample_after(
            moved_at,
            timeout=remaining,
            skip=self.settings.pipeline_skip_frames,
        )
        if frame is None:
            age = self.hub.last_frame_age()
            if age is None:
                raise _SweepAborted("no preview frames are arriving")
            raise _SweepAborted(f"preview frames stalled (last frame {age:.1f}s ago); nothing to focus on")
        return frame.jpeg

    @staticmethod
    def _linspace(start: float, stop: float, count: int) -> list[int]:
        """Evenly spaced integer positions, inclusive of both ends, deduplicated."""
        if count <= 1:
            return [round((start + stop) / 2)]
        step = (stop - start) / (count - 1)
        seen: list[int] = []
        for index in range(count):
            value = round(start + index * step)
            if not seen or value != seen[-1]:
                seen.append(value)
        return seen
