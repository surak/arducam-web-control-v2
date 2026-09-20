"""Pan/tilt servo rigs: the real PCA9685 one and a simulated one.

Angle conventions (kept from the original rig so both projects agree):

* ``pan``  0..180, increasing turns the camera **right**
* ``tilt`` 0..180, increasing tilts the camera **down**

Both are the angles of a standard hobby servo, i.e. the pulse is interpolated
between ``min_pulse_us`` (0 deg) and ``max_pulse_us`` (180 deg).
"""

from __future__ import annotations

import logging
import threading
import time

from arducam_ptz.config import PanTiltSettings
from arducam_ptz.hardware.base import Availability, ServoHardware, clamp
from arducam_ptz.hardware.pca9685 import Pca9685, Pca9685Error


class ServoRig(ServoHardware):
    """Two servos on a PCA9685 (pan on one channel, tilt on another).

    Deliberately dumb: it maps an angle to a pulse and writes it. Limits,
    coalescing and motion policy live in the service layer, so the hardware stays
    replaceable (a different board, or a simulated rig, behaves identically).
    """

    def __init__(self, settings: PanTiltSettings, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self._chip = Pca9685(
            bus_number=settings.i2c_bus,
            address=settings.i2c_address,
            frequency_hz=settings.pwm_frequency_hz,
            frequency_correction=settings.pwm_frequency_correction,
            logger=self.log,
        )
        self._available = False
        self._detail: str | None = None
        self._released = False

    # ------------------------------------------------------------------ life
    def open(self) -> bool:
        """Initialise the board. Returns False (and remembers why) on failure."""
        try:
            self._chip.open()
            self._available = True
            self._detail = None
            return True
        except Pca9685Error as exc:
            self._available = False
            self._detail = str(exc)
            self.log.warning("pan/tilt unavailable: %s", exc)
            return False

    def reopen(self) -> bool:
        """Re-initialise after a bus error, as the old app retried mid-request."""
        self.close()
        self.log.info("retrying PCA9685 initialisation")
        return self.open()

    def close(self) -> None:
        self._chip.close()
        self._available = False

    def availability(self) -> Availability:
        if self._available:
            return Availability.ok("PCA9685 connected")
        return Availability.missing(self._detail or "pan/tilt rig not initialised")

    # ------------------------------------------------------------------ motion
    def angle_to_pulse(self, angle: float) -> float:
        """Interpolate 0..180 degrees into the configured pulse range."""
        span = self.settings.max_pulse_us - self.settings.min_pulse_us
        return self.settings.min_pulse_us + (clamp(angle, 0.0, 180.0) / 180.0) * span

    def set_angles(self, pan: float, tilt: float) -> None:
        """Write both axes. Raises :class:`Pca9685Error` on a bus failure."""
        if not self._available:
            raise Pca9685Error("pan/tilt rig is not initialised")
        self._chip.set_pulse_us(self.settings.pan_channel, self.angle_to_pulse(pan))
        self._chip.set_pulse_us(self.settings.tilt_channel, self.angle_to_pulse(tilt))
        self._released = False
        # Leave the bus alone for a moment: the camera pipeline shares this Pi, and a
        # servo write landing inside a frame transfer is how a correct angle turns
        # into a corrupted pulse (and a twitching camera). See i2c_settle_ms.
        settle_ms = self.settings.i2c_settle_ms
        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

    def release(self) -> None:
        """Cut the pulses so both servos go limp and stop drawing current."""
        if not self._available:
            raise Pca9685Error("pan/tilt rig is not initialised")
        self._chip.release_channel(self.settings.pan_channel)
        self._chip.release_channel(self.settings.tilt_channel)
        self._released = True


class SimulatedServoRig(ServoHardware):
    """Records what would have been written.

    ``writes`` is a list of ``(pan, tilt, monotonic_time)`` tuples: the servo
    writer's coalescing and rate limiting are tested against it, which is how the
    20 Hz-while-dragging behaviour is verified without a servo on the bench.
    """

    def __init__(self, settings: PanTiltSettings, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self.writes: list[tuple[float, float, float]] = []
        self.released = False
        self.fail_next: int = 0  # set >0 to make the next writes raise (fault injection)
        self._lock = threading.Lock()

    def availability(self) -> Availability:
        return Availability.fake("simulated pan/tilt rig")

    def set_angles(self, pan: float, tilt: float) -> None:
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise Pca9685Error("simulated I2C failure")
            self.writes.append((pan, tilt, time.monotonic()))
            self.released = False

    def release(self) -> None:
        with self._lock:
            self.released = True

    def close(self) -> None:
        self.log.debug("simulated servo rig closed")

    @property
    def last(self) -> tuple[float, float] | None:
        with self._lock:
            return self.writes[-1][:2] if self.writes else None
