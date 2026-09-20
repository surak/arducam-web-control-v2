"""Hardware abstractions.

Everything that touches I2C, V4L2 or libcamera lives under this package and
exposes one of the small ABCs below. Two reasons:

* **Testability.** The old app created its camera and servo objects at import
  time as module-level globals, so importing it on a machine without a camera
  failed and nothing could be unit tested. Here the services depend on these
  interfaces, and ``--simulate`` swaps in fake implementations.
* **One place to be careful.** Bus locking, retries, restarts and the awkward
  corners of the libraries (picamera2's encoder callback signature, V4L2 control
  parsing) are dealt with here and nowhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether a piece of hardware can be used, and why not if it cannot.

    Replaces the old app's habit of catching every hardware exception and
    returning HTTP 200 with a success flag, which made a missing camera look
    like a working camera that produced no pictures.
    """

    available: bool
    simulated: bool = False
    detail: str | None = None

    @classmethod
    def ok(cls, detail: str | None = None) -> Availability:
        return cls(available=True, simulated=False, detail=detail)

    @classmethod
    def fake(cls, detail: str | None = None) -> Availability:
        """Available, but only in simulation - clients must be able to tell."""
        return cls(available=True, simulated=True, detail=detail)

    @classmethod
    def missing(cls, detail: str) -> Availability:
        return cls(available=False, simulated=False, detail=detail)


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``.

    Used everywhere a client-supplied number could leave the mechanical range;
    the caller is responsible for telling the client that it happened.
    """
    return low if value < low else high if value > high else value


@runtime_checkable
class FrameSink(Protocol):
    """Where a camera pushes encoded preview frames.

    Declared here (rather than importing the frame hub) so the hardware layer
    stays below the service layer: the camera knows nothing about subscribers,
    queues or HTTP - it just hands over JPEG bytes.
    """

    def publish(self, jpeg: bytes, *, width: int, height: int, timestamp: float) -> None: ...


def sink_frame_age(sink: FrameSink) -> float | None:
    """Seconds since ``sink`` last received a frame, or None when it cannot say.

    The camera's stall supervisor asks this instead of importing the frame hub,
    which keeps hardware from depending on services.
    """
    getter = getattr(sink, "last_frame_age", None)
    return getter() if callable(getter) else None


@dataclass(frozen=True, slots=True)
class FocusLimits:
    """Range of the V4L2 focus control, as reported by the driver."""

    minimum: int
    maximum: int
    step: int = 1

    def clamp(self, position: int) -> int:
        bounded = int(clamp(position, self.minimum, self.maximum))
        if self.step > 1:  # keep the value on the driver's grid
            bounded = self.minimum + round((bounded - self.minimum) / self.step) * self.step
            bounded = int(clamp(bounded, self.minimum, self.maximum))
        return bounded


class ServoHardware(ABC):
    """A two-axis pan/tilt rig, addressed in degrees (0-180 nominal)."""

    @abstractmethod
    def availability(self) -> Availability: ...

    @abstractmethod
    def set_angles(self, pan: float, tilt: float) -> None:
        """Drive both axes to the given angles.

        Implementations must be safe to call repeatedly with the same values
        (the servo writer ticks at 20 Hz) and must raise on real bus errors so
        the caller can report a degraded rig.
        """

    @abstractmethod
    def release(self) -> None:
        """Stop sending pulses so the servos go limp (no holding torque).

        This is the real emergency control, and it is deliberately not "go to the middle".
        The old app's only stop button ("EMERGENCY STOP", key 'e') posted to
        /api/servo/emergency-reset, whose handler called servo.reset() - so pressing stop
        on a thrashing rig *drove both servos to 90 degrees*, which is the last thing you
        want a shaking machine to do. This one cuts the pulse train instead: the axes lose
        torque immediately and stay wherever they are.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the I2C bus. Must not raise."""

    def open(self) -> bool:
        """Bring the backend up for the first time. Returns True when it is usable.

        Distinct from :meth:`reopen` on purpose: conflating the two made a normal
        startup log "retrying PCA9685 initialisation", so a log reader could not tell a
        clean boot from a rig that had actually fallen over and recovered. Backends with
        nothing to acquire (the simulated ones) inherit this and just report themselves.
        """
        return self.availability().available

    def reopen(self) -> bool:
        """Re-initialise after a failure. Returns True when the rig is usable again."""
        return False


class FocusMotor(ABC):
    """A voice-coil focus actuator exposed through a V4L2 control."""

    @abstractmethod
    def availability(self) -> Availability: ...

    @abstractmethod
    def limits(self) -> FocusLimits: ...

    @abstractmethod
    def set_position(self, position: int) -> int:
        """Move the lens and return the value actually applied (clamped)."""

    @abstractmethod
    def get_position(self) -> int | None:
        """Read the value back from the driver, or None when it cannot be read."""

    def open(self) -> bool:
        """Bring the motor up for the first time. Returns True when it is usable."""
        return self.availability().available

    def reopen(self) -> bool:
        """Re-initialise after a failure. Returns True when usable again."""
        return False

    def close(self) -> None:  # noqa: B027 - intentionally optional, V4L2 holds no resource
        """Nothing to release for V4L2; kept for symmetry with the other backends."""


class CameraHardware(ABC):
    """A camera that publishes a preview stream and can take full-size stills."""

    @abstractmethod
    def availability(self) -> Availability: ...

    @abstractmethod
    def start(self) -> None:
        """Begin publishing preview frames. Idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop publishing and release the device. Idempotent, must not raise."""

    @abstractmethod
    def capture_still(self, path: Path) -> tuple[int, int]:
        """Write a full-resolution JPEG to ``path``; returns (width, height).

        Implementations switch sensor mode, which briefly interrupts the preview
        and must therefore be excluded against other camera operations.
        """

    @abstractmethod
    def sensor_resolution(self) -> tuple[int, int] | None:
        """Native resolution of the sensor, or None when it cannot be queried."""

    @property
    def busy(self) -> bool:
        """True while a still capture owns the camera."""
        return False
