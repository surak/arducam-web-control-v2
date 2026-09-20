"""Pan/tilt policy: limits, coalescing, and honest error reporting.

The service owns a *target* and a writer thread. Requests only ever set the target;
the writer drives the servos at ``write_rate_hz``. Three things fall out of that,
each of them a fix for something the old app got wrong:

1. **Coalescing.** Dragging a slider on a phone can fire 100 events per second.
   The old app wrote to I2C for each one, which made the servos buzz and jitter.
   Here the servos see at most 20 writes per second and always the *latest*
   position.
2. **Truthful responses.** Every command returns the applied angle and a
   machine-readable reason for anything that was not applied as asked. The old
   ``/api/pan`` clamped silently to 1-degree steps and still answered
   ``success: true``.
3. **Survivable I2C.** A bus error is recorded, retried, and - if it persists -
   reported as "pan/tilt unavailable" instead of raising into a web handler and
   producing a 500 page.
"""

from __future__ import annotations

import logging
import threading
import time

from arducam_ptz.config import PanTiltSettings
from arducam_ptz.hardware.base import ServoHardware, clamp
from arducam_ptz.models import AxisState, ClampReport, PtzState

logger = logging.getLogger(__name__)

#: After this many consecutive write failures, try to re-initialise the board.
_FAILURES_BEFORE_RETRY = 3
#: Never retry the bus more often than this.
_RETRY_COOLDOWN_S = 5.0


class PanTiltService:
    """Target state plus a coalescing writer for the pan/tilt rig."""

    def __init__(
        self,
        settings: PanTiltSettings,
        rig: ServoHardware,
        log: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.rig = rig
        self.log = log or logger
        self._lock = threading.RLock()
        self._target: tuple[float, float] = (
            self._bounded("pan", settings.pan_center),
            self._bounded("tilt", settings.tilt_center),
        )
        self._written: tuple[float, float] | None = None
        self._released = False
        self._clamps: list[ClampReport] = []
        self._last_write: float | None = None
        self._failures = 0
        self._last_retry = 0.0
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ ranges
    def limits(self, axis: str) -> tuple[float, float]:
        return (
            (self.settings.pan_min, self.settings.pan_max)
            if axis == "pan"
            else (
                self.settings.tilt_min,
                self.settings.tilt_max,
            )
        )

    def _bounded(self, axis: str, value: float) -> float:
        low, high = self.limits(axis)
        return clamp(value, low, high)

    def _raw(self, axis: str, angle: float) -> float:
        """Convert a client-facing angle into the angle to write to the servo.

        Only differs when the axis is mounted mirrored (``invert_*``).
        """
        inverted = self.settings.invert_pan if axis == "pan" else self.settings.invert_tilt
        return 180.0 - angle if inverted else angle

    # ------------------------------------------------------------------ writer
    def start(self) -> None:
        """Start the writer thread that turns targets into I2C writes."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="servo-writer", daemon=True)
        self._thread.start()
        self.log.debug("servo writer started (%.0f Hz)", self.settings.write_rate_hz)

    def stop(self) -> None:
        """Stop the writer and release the servos."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            self.rig.release()
        except Exception:  # pragma: no cover - shutdown path
            self.log.debug("releasing servos during shutdown failed", exc_info=True)

    def _run(self) -> None:
        period = 1.0 / max(1.0, self.settings.write_rate_hz)
        while not self._stop.wait(period):
            try:
                self._tick()
            except Exception:  # pragma: no cover - the writer must never die
                self.log.exception("servo writer tick failed")

    def _tick(self, now: float | None = None) -> bool:
        """Perform at most one write if one is due. Returns True when it wrote.

        Kept public and deterministic on purpose: the tests drive this directly
        instead of sleeping, so rate limiting and coalescing are verified without
        timing flakiness.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._released or self._written == self._target:
                return False
            if self._last_write is not None:
                gap = self.settings.min_write_interval_ms / 1000.0
                if now - self._last_write < gap:
                    return False
            target = self._target
        raw = (self._raw("pan", target[0]), self._raw("tilt", target[1]))
        try:
            self.rig.set_angles(*raw)
        except Exception as exc:
            self._handle_failure(exc, now)
            return False
        with self._lock:
            self._written = target
            self._last_write = now
            self._failures = 0
            self._last_error = None
        return True

    def _handle_failure(self, exc: Exception, now: float) -> None:
        with self._lock:
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            failures = self._failures
            may_retry = now - self._last_retry > _RETRY_COOLDOWN_S
        self.log.warning("servo write failed (%d in a row): %s", failures, exc)
        if failures >= _FAILURES_BEFORE_RETRY and may_retry:
            with self._lock:
                self._last_retry = now
                self._failures = 0  # do not retry on every tick
            self.log.info("re-initialising the pan/tilt rig after repeated failures")
            try:
                if self.rig.reopen():
                    self.log.info("pan/tilt rig recovered")
                    self._last_error = None
            except Exception:  # pragma: no cover
                self.log.exception("reopen() itself failed")

    # ------------------------------------------------------------------ commands
    def state(self) -> PtzState:
        """Current state, including the clamps from the most recent command."""
        with self._lock:
            target = self._target
            clamps = list(self._clamps)
            released = self._released
            error = self._last_error
        availability = self.rig.availability()
        detail = availability.detail
        if error:
            detail = f"{detail or 'pan/tilt'}; last write failed: {error}" if detail else error
        return PtzState(
            available=availability.available,
            simulated=availability.simulated,
            pan=AxisState(
                angle=target[0],
                min=self.settings.pan_min,
                max=self.settings.pan_max,
                inverted=self.settings.invert_pan,
                released=released,
            ),
            tilt=AxisState(
                angle=target[1],
                min=self.settings.tilt_min,
                max=self.settings.tilt_max,
                inverted=self.settings.invert_tilt,
                released=released,
            ),
            clamps=clamps,
            detail=detail,
        )

    def goto(self, pan: float | None = None, tilt: float | None = None) -> PtzState:
        """Move to absolute angles. Omitted axes keep their current target."""
        with self._lock:
            requested = {
                "pan": self._target[0] if pan is None else float(pan),
                "tilt": self._target[1] if tilt is None else float(tilt),
            }
        return self._apply(requested)

    def nudge(self, pan: float = 0.0, tilt: float = 0.0) -> PtzState:
        """Move relative to the current target.

        The joystick in the web UI sends these at ~12 Hz while it is held; because
        the writer coalesces, a fast repeat rate costs nothing.
        """
        with self._lock:
            requested = {"pan": self._target[0] + float(pan), "tilt": self._target[1] + float(tilt)}
        return self._apply(requested)

    def center(self) -> PtzState:
        """Go to the configured centre position."""
        return self._apply({"pan": self.settings.pan_center, "tilt": self.settings.tilt_center})

    def release(self) -> PtzState:
        """Cut the servo pulses so both axes go limp."""
        with self._lock:
            self._released = True
            self._written = None
            self._clamps = []
        try:
            self.rig.release()
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            self.log.warning("releasing the servos failed: %s", exc)
        return self.state()

    def _apply(self, requested: dict[str, float]) -> PtzState:
        """Clamp a request into the mechanical range and adopt it as the target."""
        clamps: list[ClampReport] = []
        applied: dict[str, float] = {}
        for axis in ("pan", "tilt"):
            wanted = requested[axis]
            low, high = self.limits(axis)
            bounded = clamp(wanted, low, high)
            reason = "travel_limit" if bounded != wanted else None
            max_step = self.settings.max_step_degrees
            if reason is None and max_step > 0:
                with self._lock:
                    current = self._target[0] if axis == "pan" else self._target[1]
                if abs(bounded - current) > max_step:
                    bounded = clamp(current + max_step * (1 if bounded > current else -1), low, high)
                    reason = "max_step"
            if reason is not None:
                clamps.append(
                    ClampReport(
                        axis=axis,  # type: ignore[arg-type]
                        requested=round(wanted, 3),
                        applied=round(bounded, 3),
                        reason=reason,  # type: ignore[arg-type]
                    )
                )
            applied[axis] = bounded
        with self._lock:
            self._target = (applied["pan"], applied["tilt"])
            self._clamps = clamps
            # A move re-arms the servos if they had been released.
            self._released = False
        self.log.debug(
            "pan=%.1f tilt=%.1f%s",
            applied["pan"],
            applied["tilt"],
            f" (clamped: {clamps})" if clamps else "",
        )
        # Write immediately rather than waiting for the next tick: a single tap on
        # a phone should feel instant, and the min_write_interval still protects the
        # bus from a flood of requests.
        self._tick()
        return self.state()
