"""Focus motor backends: the real AK7375 through V4L2, and a simulated one.

The camera module's voice-coil motor is exposed by libcamera as the V4L2 control
``focus_absolute`` on the sensor's subdevice. There is **no** autofocus algorithm
in the IMX519 tuning file on this stack - ``libcamera`` reports no
``AfMode``/``AfTrigger`` on the subdevice - so the only way to focus is to move
the lens yourself and judge the result. That is what ``services/focus.py`` does;
this module only moves the lens.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from arducam_ptz.config import FocusSettings
from arducam_ptz.hardware.base import Availability, FocusLimits, FocusMotor

#: One line of ``v4l2-ctl -l`` looks like this (captured on the real rig):
#:
#:     focus_absolute 0x009a090a (int)    : min=0 max=4095 step=1 default=0 value=0 flags=has-min-max
#:
#: The columns are whitespace-aligned and the value moves, so match on names
#: rather than on positions. An early version of the old code matched
#: ``\\s+`` between min/max/step and broke as soon as a value was negative.
_CONTROL_RE = re.compile(
    r"^\s*(?P<name>\S+)\s+0x[0-9a-fA-F]+\s+\((?P<type>[a-z]+)\)\s*:\s*(?P<body>.*)$",
    re.MULTILINE,
)
_VALUE_RE = re.compile(r"(min|max|step|default|value)=(-?\d+)")


@dataclass(frozen=True, slots=True)
class ControlDescription:
    """Parsed V4L2 control line."""

    name: str
    kind: str
    minimum: int
    maximum: int
    step: int
    default: int
    value: int | None

    @property
    def limits(self) -> FocusLimits:
        return FocusLimits(minimum=self.minimum, maximum=self.maximum, step=self.step)


def parse_controls(output: str) -> dict[str, ControlDescription]:
    """Parse ``v4l2-ctl --list-ctrls`` output into a dict keyed by control name.

    Pure function, unit tested against real device output (tests/test_focus.py)
    because this parsing is exactly where the old code was brittle.
    """
    descriptions: dict[str, ControlDescription] = {}
    for match in _CONTROL_RE.finditer(output):
        fields = {name: int(number) for name, number in _VALUE_RE.findall(match["body"])}
        if "min" not in fields or "max" not in fields:
            continue  # not a ranged control (menu/toggle/button)
        descriptions[match["name"]] = ControlDescription(
            name=match["name"],
            kind=match["type"],
            minimum=fields["min"],
            maximum=fields["max"],
            step=fields.get("step", 1) or 1,
            default=fields.get("default", fields["min"]),
            value=fields.get("value"),
        )
    return descriptions


def _run_v4l2ctl(args: list[str], timeout_s: float = 5.0) -> str:
    """Run v4l2-ctl and return stdout, or raise RuntimeError with the reason."""
    try:
        completed = subprocess.run(
            ["v4l2-ctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("v4l2-ctl is not installed (apt install v4l-utils)") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"v4l2-ctl {' '.join(args)} timed out after {timeout_s}s") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        raise RuntimeError(f"v4l2-ctl {' '.join(args)} failed: {message[-1] if message else 'no output'}")
    return completed.stdout


def discover_device(control: str = "focus_absolute", pattern: str = "/dev/v4l-subdev*") -> str | None:
    """Find the subdevice exposing ``control``.

    Probed at start-up and then remembered; the old app hard-coded
    ``/dev/v4l-subdev3``, which silently stopped matching after an update moved
    the sensor to a different subdevice number.
    """
    candidates = Path(pattern)
    # Path.glob rather than the glob module: same result, one less import, and it
    # keeps the pattern's directory explicit.
    for device in sorted(candidates.parent.glob(candidates.name)):
        try:
            output = _run_v4l2ctl(["-d", str(device), "--list-ctrls"])
        except RuntimeError:
            continue
        if control in parse_controls(output):
            return str(device)
    return None


class V4L2FocusMotor(FocusMotor):
    """Moves the lens with ``v4l2-ctl --set-ctrl focus_absolute=<n>``.

    ``v4l2-ctl`` costs ~15 ms per invocation; the old code used it too and paid
    that price inside the request handler, which is why a focus sweep blocked the
    whole server. Here it is called from a background job and from the servo-style
    writer, never from a request that a browser is waiting on.
    """

    def __init__(self, settings: FocusSettings, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self.device: str | None = settings.device
        self._limits: FocusLimits | None = None
        self._position: int | None = None
        self._available = False
        self._detail: str | None = None
        self._lock = threading.RLock()
        self._failures = 0

    # ------------------------------------------------------------------ life
    def open(self) -> bool:
        """Resolve the device and read its limits. False when unusable."""
        with self._lock:
            try:
                device = self.device or discover_device(self.settings.control)
                if device is None:
                    raise RuntimeError(
                        f"no V4L2 subdevice exposes {self.settings.control} "
                        "(is the camera detected? try `v4l2-ctl --list-devices`)"
                    )
                self.device = device
                described = parse_controls(_run_v4l2ctl(["-d", device, "--list-ctrls"]))
                control = described.get(self.settings.control)
                if control is None:
                    raise RuntimeError(f"{device} does not expose {self.settings.control}")
                self._limits = control.limits
                self._position = control.value
                self._available = True
                self._detail = None
                self.log.info(
                    "focus motor on %s: %s range %d..%d step %d",
                    device,
                    control.name,
                    control.minimum,
                    control.maximum,
                    control.step,
                )
                return True
            except RuntimeError as exc:
                self._available = False
                self._detail = str(exc)
                self.log.warning("focus motor unavailable: %s", exc)
                return False

    def reopen(self) -> bool:
        with self._lock:
            self.device = self.settings.device
            return self.open()

    def availability(self) -> Availability:
        if self._available and self._limits is not None:
            return Availability.ok(f"{self.device} ({self._limits.minimum}..{self._limits.maximum})")
        return Availability.missing(self._detail or "focus motor not initialised")

    def limits(self) -> FocusLimits:
        with self._lock:
            if self._limits is None:
                raise RuntimeError("focus motor is not initialised")
            return self._limits

    # ------------------------------------------------------------------ motion
    def set_position(self, position: int) -> int:
        """Write ``focus_absolute``. Returns the clamped value that was applied."""
        with self._lock:
            if not self._available or self.device is None:
                raise RuntimeError(f"focus motor is not initialised: {self._detail or 'no device'}")
            applied = self.limits().clamp(position)
            if applied == self._position:
                return applied
            try:
                _run_v4l2ctl(["-d", self.device, "--set-ctrl", f"{self.settings.control}={applied}"])
            except RuntimeError:
                self._failures += 1
                # One retry, because a USB/HAT glitch usually clears immediately;
                # two failures mean the device is gone and we say so.
                if self._failures >= 2:
                    self._available = False
                    self._detail = "focus motor stopped responding; re-initialise required"
                raise
            self._failures = 0
            self._position = applied
            return applied

    def get_position(self) -> int | None:
        """Read the control back; also refreshes availability after a failure."""
        with self._lock:
            if self.device is None:
                return self._position
            try:
                described = parse_controls(_run_v4l2ctl(["-d", self.device, "--list-ctrls"]))
            except RuntimeError as exc:
                self._detail = str(exc)
                return self._position
            control = described.get(self.settings.control)
            if control is None:
                return self._position
            self._limits = control.limits
            if control.value is not None:
                self._position = control.value
            self._available = True
            return self._position


class SimulatedFocusMotor(FocusMotor):
    """A focus motor that behaves like the real one, including soft limits.

    ``sharpest_at`` gives the simulated camera a position where the picture is
    sharp, so the autofocus sweep can be tested end to end: the score curve is a
    real measurement of generated frames, not a mocked number.
    """

    def __init__(
        self,
        settings: FocusSettings,
        limits: FocusLimits | None = None,
        sharpest_at: int = 1400,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self._limits = limits or FocusLimits(minimum=0, maximum=4095, step=1)
        self.position = self._limits.clamp(sharpest_at)
        self.sharpest_at = self.position
        self.writes: list[tuple[int, float]] = []
        self._lock = threading.Lock()

    def availability(self) -> Availability:
        return Availability.fake("simulated focus motor")

    def limits(self) -> FocusLimits:
        return self._limits

    def set_position(self, position: int) -> int:
        with self._lock:
            applied = self._limits.clamp(position)
            # Stand in for the motor's travel time so a sweep in simulation has a
            # realistic shape (and so tests exercise the settle logic).
            time.sleep(self.settings.settle_ms / 1000.0 if self.settings.settle_ms else 0)
            self.position = applied
            self.writes.append((applied, time.monotonic()))
            return applied

    def get_position(self) -> int | None:
        return self.position

    def blur_radius(self, worst: float = 4.0) -> float:
        """How blurry the simulated picture should look at the current position.

        Linear in the distance from ``sharpest_at``, which yields the same
        unimodal sharpness curve a real lens produces near best focus.
        """
        span = max(self._limits.maximum - self._limits.minimum, 1)
        distance = abs(self.position - self.sharpest_at) / span
        return float(min(worst, worst * 2.0 * distance))


__all__ = [
    "ControlDescription",
    "SimulatedFocusMotor",
    "V4L2FocusMotor",
    "discover_device",
    "parse_controls",
]
