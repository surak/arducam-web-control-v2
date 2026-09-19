"""Build the hardware bundle, real or simulated, from :class:`Settings`.

The factory is the only place that decides between real and fake backends, so
tests and ``--simulate`` cannot half-simulate: either every backend is simulated
or none is.

Start-up is deliberately forgiving: a missing focus motor or a PCA9685 that does
not answer logs a warning and marks that component unavailable, and the server
still comes up. The old app refused to start without hardware, which is why a
loose servo wire meant "the whole website is down" instead of "pan/tilt is
unavailable" - a much worse failure mode on a remote rig.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from arducam_ptz.config import Settings
from arducam_ptz.hardware.base import (
    Availability,
    CameraHardware,
    FocusMotor,
    FrameSink,
    ServoHardware,
)
from arducam_ptz.hardware.camera import PicameraCamera, SimulatedCamera
from arducam_ptz.hardware.focus import SimulatedFocusMotor, V4L2FocusMotor
from arducam_ptz.hardware.servo import ServoRig, SimulatedServoRig

__all__ = [
    "Availability",
    "CameraHardware",
    "FocusMotor",
    "FrameSink",
    "HardwareBundle",
    "ServoHardware",
    "build_hardware",
]

logger = logging.getLogger(__name__)


def build_pan_tilt(settings: Settings, log: logging.Logger | None = None) -> ServoHardware:
    if settings.simulate or settings.pan_tilt.simulate:
        return SimulatedServoRig(settings.pan_tilt, log)
    return ServoRig(settings.pan_tilt, log)


def build_focus(settings: Settings, log: logging.Logger | None = None) -> FocusMotor:
    if settings.simulate or settings.focus.simulate:
        return SimulatedFocusMotor(settings.focus, logger=log)
    return V4L2FocusMotor(settings.focus, log)


def build_camera(
    settings: Settings,
    sink: FrameSink,
    focus: FocusMotor | None = None,
    log: logging.Logger | None = None,
) -> CameraHardware:
    if settings.simulate or settings.camera.simulate:
        # The simulated camera borrows the simulated motor's position so the
        # picture is sharpest at the motor's "in focus" spot. That makes the whole
        # autofocus loop measurable in tests instead of mocked.
        blur_source = focus if isinstance(focus, SimulatedFocusMotor) else None
        return SimulatedCamera(settings.camera, sink, blur_source=blur_source, logger=log)
    return PicameraCamera(settings.camera, sink, log)


@dataclass
class HardwareBundle:
    """The three backends plus the lifecycle calls the server needs."""

    camera: CameraHardware
    pan_tilt: ServoHardware
    focus: FocusMotor

    def start(self) -> None:
        """Bring everything up, logging what worked and what did not.

        Never raises: a rig that boots half-alive is more useful than a server
        that refuses to run.
        """
        # reopen() only means something for real hardware; simulated backends
        # report availability instead.
        servo_ok = self.pan_tilt.reopen() or self.pan_tilt.availability().available
        focus_ok = self.focus.reopen() or self.focus.availability().available
        logger.info("pan/tilt: %s", self.pan_tilt.availability().detail)
        logger.info("focus:    %s", self.focus.availability().detail)
        # The camera is started last: it is the only component whose absence
        # still lets the rest of the UI work (you can move a camera you cannot see
        # - mildly useful - but you cannot see one you cannot move, so know first).
        try:
            self.camera.start()
            logger.info("camera:   %s", self.camera.availability().detail)
        except RuntimeError as exc:
            logger.error("camera failed to start: %s", exc)
        if not servo_ok:
            logger.warning("pan/tilt unavailable - movement endpoints will report 503")
        if not focus_ok:
            logger.warning("focus unavailable - focus endpoints will report 503")

    def close(self) -> None:
        """Shut everything down; each step is individually guarded."""
        for name, closer in (
            ("camera", self.camera.stop),
            ("focus", self.focus.close),
            ("pan/tilt", self.pan_tilt.close),
        ):
            try:
                closer()
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.exception("error closing %s", name)

    def availabilities(self) -> dict[str, Availability]:
        return {
            "camera": self.camera.availability(),
            "focus": self.focus.availability(),
            "pan_tilt": self.pan_tilt.availability(),
        }


def build_hardware(
    settings: Settings,
    sink: FrameSink,
    log: logging.Logger | None = None,
) -> HardwareBundle:
    """Assemble camera, pan/tilt and focus from ``settings``."""
    focus = build_focus(settings, log)
    return HardwareBundle(
        camera=build_camera(settings, sink, focus=focus, log=log),
        pan_tilt=build_pan_tilt(settings, log),
        focus=focus,
    )
