"""Minimal PCA9685 16-channel PWM driver, spoken directly over I2C.

Deliberately not Adafruit's ServoKit/Blinka stack: Blinka pulls in a large
dependency tree, wants to own the board detection, and its failure modes are
opaque on a headless Pi. The chip's register map is a page long, so talking to it
directly keeps the dependency list at ``smbus2`` and makes every failure say
something specific.

Register layout (PCA9685 datasheet, section 7):

    0x00 MODE1     0x01 MODE2     0x06..0x45 LEDn_ON_L/OFF_L (4 bytes per channel)
    0xFE PRESCALE

Pulse timing: the chip counts 4096 ticks per PWM period, so with an internal
25 MHz clock the period is ``4096 * (prescale + 1) / 25e6`` seconds.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from arducam_ptz.hardware.base import Availability, clamp

try:  # pragma: no cover - import guard so unit tests run on a laptop
    from smbus2 import SMBus
except ImportError:  # pragma: no cover
    SMBus = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:  # the real type for annotations, even when smbus2 is absent
    from smbus2 import SMBus

MODE1 = 0x00
MODE2 = 0x01
LED0_ON_L = 0x06
PRESCALE = 0xFE

# MODE1 bits
RESTART = 0x80
AI = 0x20  # auto-increment: lets one write cover ON_L..OFF_H
SLEEP = 0x10  # required while changing PRESCALE

# MODE2 bits
OUTDRV = 0x04  # totem-pole output (what a servo signal wire wants)

#: The PCA9685's own "software reset / all call" address. It is normal to see it
#: on an I2C scan next to the configured address (0x70 next to 0x40 on this rig).
ALLCALL_ADDRESS = 0x70

OSCILLATOR_HZ = 25_000_000
TICKS_PER_PERIOD = 4096

#: How long to leave every output switched off after the board answers, before and
#: after the frequency change. The PCA9685 comes up with the *power-on* prescale
#: (about 200 Hz, not hobby-servo 50 Hz) and with every LED register at zero, which
#: means "switch on at tick 0, switch off at tick 0" - a level that a servo reads as
#: garbage. Writing the register map without this quiet window is what makes a
#: pan-tilt rig thrash: the servo chases the out-of-spec pulses, is yanked to a hard
#: stop, and keeps hunting there for as long as the garbage lasts.
#:
#: The original app hit exactly this and fixed it with a "quiet startup sequence"
#: (all 16 channels off, 0.2 s either side of the initialisation). Those two sleeps
#: are load-bearing, not politeness - the servos are electrically silent meanwhile.
QUIET_WINDOW_S = 0.2

#: Writing the four LED registers is not atomic, so a write that straddles the
#: counter wrapping produces a visible servo glitch. Pausing between the ON and
#: OFF halves avoids it; the delay is far below any servo's reaction time.
GLITCH_GUARD_S = 0.002


def prescale_for(frequency_hz: float, correction: float = 1.0) -> int:
    """Return the PRESCALE register value for a target PWM frequency.

    Datasheet section 7.3.5: ``period = 4096 * (prescale + 1) / oscillator``, with the
    nominal 25 MHz oscillator, so ``prescale = round(osc / (4096 * f)) - 1``.

    ``correction`` scales *only* the frequency, never the pulse arithmetic. It is a
    documented-nowhere constant inherited from the original project: nobody recorded what
    it was measured against, so docs/HARDWARE.md sets out what is known and what is not.
    What is certain is that this rig is quiet at the prescale it produces (119) and that
    the datasheet-nominal value (101) is not it - so this is a separate, pure function,
    assertable in a test with no I2C bus attached.
    """
    raw = int(round(OSCILLATOR_HZ / (TICKS_PER_PERIOD * frequency_hz * correction)) - 1)
    return int(clamp(raw, 3, 255))


class Pca9685Error(RuntimeError):
    """Raised when the chip cannot be reached or configured."""


class Pca9685:
    """A single PCA9685 board on an I2C bus."""

    def __init__(
        self,
        bus_number: int = 1,
        address: int = 0x40,
        frequency_hz: float = 60.0,
        frequency_correction: float = 1.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.bus_number = bus_number
        self.address = address
        self.frequency_hz = frequency_hz
        #: Multiplier applied to the *prescale* calculation only (see
        #: :meth:`set_frequency`). 0.8449 is the original project's constant for this
        #: board, carried over verbatim: its origin is not recorded there (one
        #: uncommented line, no notes), but this is the value the servos in this bracket
        #: are quiet at. A different board starts at 1.0 and gets measured before its
        #: limits are trusted - with the wrong value every pulse is hundreds of
        #: microseconds out, which is of order twenty degrees of servo travel.
        self.frequency_correction = frequency_correction
        self._bus: SMBus | None = None
        self._prescale = 0
        #: Last value written per channel, so the 20 Hz writer does not hammer the
        #: bus with identical pulses (the old app wrote unconditionally).
        self._last_ticks: dict[int, int] = {}

    # ------------------------------------------------------------------ life
    def open(self) -> None:
        """Open the bus and configure the chip. Raises :class:`Pca9685Error`."""
        if SMBus is None:
            raise Pca9685Error("smbus2 is not installed (uv sync should have provided it)")
        try:
            self._bus = SMBus(self.bus_number)
            self._initialise()
        except FileNotFoundError as exc:  # no /dev/i2c-N: I2C not enabled
            raise Pca9685Error(
                f"I2C bus {self.bus_number} not found - enable it with raspi-config "
                f"(Interface Options -> I2C)"
            ) from exc
        except OSError as exc:
            self.close()
            raise Pca9685Error(
                f"no PCA9685 answered at 0x{self.address:02x} on i2c-{self.bus_number} "
                f"(check the wiring and `i2cdetect -y {self.bus_number}`): {exc}"
            ) from exc
        self.log.info(
            "PCA9685 at 0x%02x on i2c-%d configured for %.1f Hz (prescale %d)",
            self.address,
            self.bus_number,
            self.frequency_hz,
            self._prescale,
        )

    def _initialise(self) -> None:
        """Bring the board up without ever emitting a malformed servo pulse.

        Order matters and is the fix for a rig that shakes itself apart on start:

        1. switch *every* output off while the chip still runs on its power-on
           prescale - the servos see no pulses at all, instead of a ~200 Hz train
           they cannot interpret;
        2. wait out the quiet window;
        3. only then program the prescale (which needs the chip asleep);
        4. wait again, and re-assert "all off" in case the wake-up blipped a channel.

        Nothing is driven here. The first pulse is sent the first time a client asks
        for a move - see ``PanTiltService`` (it starts released on purpose).
        """
        assert self._bus is not None
        # Totem-pole outputs (what a servo's signal wire wants), then auto-increment.
        self._bus.write_byte_data(self.address, MODE2, OUTDRV)
        self._bus.write_byte_data(self.address, MODE1, AI)

        self.all_off()
        time.sleep(QUIET_WINDOW_S)
        self.set_frequency(self.frequency_hz)
        time.sleep(QUIET_WINDOW_S)
        self.all_off()
        self.log.debug("PCA9685 initialised quiet: every output off, %.1f Hz", self.frequency_hz)

    def all_off(self) -> None:
        """Switch all 16 channels fully off (no pulses at all).

        Cheap insurance: a channel that was left on by a previous run - or a stray
        startup pulse on any of the fourteen channels this rig does not use - stops
        doing whatever it was doing.
        """
        assert self._bus is not None, "Pca9685.open() has not been called"
        for channel in range(16):
            base = LED0_ON_L + 4 * channel
            self._bus.write_byte_data(self.address, base, 0)
            self._bus.write_byte_data(self.address, base + 1, 0)
            self._bus.write_byte_data(self.address, base + 2, 0)
            self._bus.write_byte_data(self.address, base + 3, 0x10)  # OFF_H bit 4: full off
        self._last_ticks.clear()

    def set_frequency(self, frequency_hz: float) -> None:
        """Program the PWM frequency.

        The prescale register may only be written while the chip is asleep, and
        the oscillator needs ~500 us to come back up, hence the sleeps.
        """
        assert self._bus is not None
        # The correction is applied here and *only* here: it changes the period the chip
        # actually produces, while the tick arithmetic below stays in the nominal domain
        # the servo was calibrated in. The two must not both be corrected - that cancels
        # out into a wrong pulse.
        prescale = prescale_for(frequency_hz, self.frequency_correction)
        old_mode = self._bus.read_byte_data(self.address, MODE1)
        self._bus.write_byte_data(self.address, MODE1, (old_mode & 0x7F) | SLEEP)
        self._bus.write_byte_data(self.address, PRESCALE, prescale)
        self._bus.write_byte_data(self.address, MODE1, old_mode)
        time.sleep(0.005)
        self._bus.write_byte_data(self.address, MODE1, old_mode | RESTART)
        self._prescale = prescale
        self._last_ticks.clear()

    def close(self) -> None:
        """Release the bus. Never raises: it is called from cleanup paths."""
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:  # pragma: no cover - close() failing is not actionable
                self.log.debug("ignoring error while closing i2c-%d", self.bus_number)
            self._bus = None

    # ------------------------------------------------------------------ writes
    def ticks_for_pulse(self, pulse_us: float) -> int:
        """Convert a pulse width in microseconds into PCA9685 off-ticks."""
        period_us = 1e6 / self.frequency_hz
        ticks = round(pulse_us / period_us * TICKS_PER_PERIOD)  # round() already yields an int
        return max(0, min(TICKS_PER_PERIOD - 1, ticks))

    def set_pulse_us(self, channel: int, pulse_us: float) -> None:
        """Set the pulse width of one channel, skipping no-op writes."""
        if not 0 <= channel <= 15:
            raise ValueError(f"PCA9685 channel must be 0..15, got {channel}")
        assert self._bus is not None, "Pca9685.open() has not been called"
        ticks = self.ticks_for_pulse(pulse_us)
        if self._last_ticks.get(channel) == ticks:
            return
        base = LED0_ON_L + 4 * channel
        # ON = 0 (pulse starts at the beginning of the period), OFF = ticks.
        self._bus.write_byte_data(self.address, base, 0)
        self._bus.write_byte_data(self.address, base + 1, 0)
        time.sleep(GLITCH_GUARD_S)
        self._bus.write_byte_data(self.address, base + 2, ticks & 0xFF)
        self._bus.write_byte_data(self.address, base + 3, ticks >> 8)
        self._last_ticks[channel] = ticks

    def release_channel(self, channel: int) -> None:
        """Turn one channel fully off (no pulses at all) - the servo goes limp."""
        assert self._bus is not None, "Pca9685.open() has not been called"
        base = LED0_ON_L + 4 * channel
        self._bus.write_byte_data(self.address, base, 0)
        self._bus.write_byte_data(self.address, base + 1, 0)
        time.sleep(GLITCH_GUARD_S)
        # OFF_H bit 4 = "full off": the channel is disabled regardless of the counter.
        self._bus.write_byte_data(self.address, base + 2, 0)
        self._bus.write_byte_data(self.address, base + 3, 0x10)
        self._last_ticks.pop(channel, None)

    def availability(self) -> Availability:
        return Availability.ok() if self._bus is not None else Availability.missing("bus not open")
