"""Hardware-layer tests.

Nothing here touches a camera, an I2C bus or a servo. The parsing and mapping
functions are pure, and the simulated backends exist precisely so the rest can be
tested without hardware - which the old project could not do at all, because it
imported picamera2 and smbus2 at module level and created its devices at import
time.
"""

from __future__ import annotations

import logging

import pytest

from arducam_ptz.config import CameraSettings, FocusSettings, PanTiltSettings, Settings
from arducam_ptz.hardware import build_hardware, build_pan_tilt
from arducam_ptz.hardware.base import FocusLimits, clamp, sink_frame_age
from arducam_ptz.hardware.focus import (
    SimulatedFocusMotor,
    V4L2FocusMotor,
    parse_controls,
)
from arducam_ptz.hardware.pca9685 import Pca9685
from arducam_ptz.hardware.servo import ServoRig, SimulatedServoRig

# --------------------------------------------------------------------------- #
# real device output, captured from this rig:
#     v4l2-ctl -d /dev/v4l-subdev3 --list-ctrls
# --------------------------------------------------------------------------- #
REAL_V4L2_OUTPUT = """
                 focus_absolute 0x009a090a (int)    : min=0 max=4095 step=1 default=0 value=0 flags=has-min-max
                 exposure 0x00980911 (int)    : min=1 max=66711 step=1 default=66711 value=3992 flags=inactive, volatile
             horizontal_flip 0x00980914 (bool)   : default=0 value=0
"""

#: Some drivers print a signed value, which the old regex (\\s+ between numbers)
#: could not cope with. Kept as a regression fixture.
NEGATIVE_VALUE_OUTPUT = """
                 focus_absolute 0x009a090a (int) : min=-100 max=4095 step=5 default=-100 value=-50 flags=has-min-max
"""


class TestControlParsing:
    def test_parses_the_real_rigs_focus_control(self) -> None:
        controls = parse_controls(REAL_V4L2_OUTPUT)
        focus = controls["focus_absolute"]
        assert (focus.minimum, focus.maximum, focus.step) == (0, 4095, 1)
        assert focus.value == 0
        assert focus.limits == FocusLimits(0, 4095, 1)

    def test_boolean_controls_are_skipped(self) -> None:
        """A control without min/max is not a ranged control; do not crash on it."""
        controls = parse_controls(REAL_V4L2_OUTPUT)
        assert "horizontal_flip" not in controls
        assert "exposure" in controls

    def test_negative_values_and_coarse_steps(self) -> None:
        focus = parse_controls(NEGATIVE_VALUE_OUTPUT)["focus_absolute"]
        assert focus.minimum == -100
        assert focus.step == 5
        assert focus.value == -50


class TestFocusLimits:
    def test_clamps_to_the_motors_range(self) -> None:
        limits = FocusLimits(0, 4095, 1)
        assert limits.clamp(-10) == 0
        assert limits.clamp(99999) == 4095
        assert limits.clamp(2048) == 2048

    def test_stays_on_the_drivers_step_grid(self) -> None:
        """Writing an off-grid value makes v4l2-ctl round it - so round it first."""
        limits = FocusLimits(0, 1000, 100)
        assert limits.clamp(149) == 100
        assert limits.clamp(151) == 200


class TestV4L2FocusMotor:
    def test_reports_unavailable_instead_of_raising(self, tmp_path) -> None:
        motor = V4L2FocusMotor(FocusSettings(device="/dev/definitely-not-here"))
        assert motor.open() is False
        availability = motor.availability()
        assert availability.available is False
        # The reason must name the device so the operator knows what to look at.
        assert "/dev/definitely-not-here" in (availability.detail or "")

    def test_set_position_refuses_when_unavailable(self) -> None:
        motor = V4L2FocusMotor(FocusSettings(device="/dev/definitely-not-here"))
        motor.open()
        with pytest.raises(RuntimeError, match="not initialised"):
            motor.set_position(100)


class TestSimulatedFocusMotor:
    def test_clamps_and_records_writes(self) -> None:
        motor = SimulatedFocusMotor(FocusSettings(settle_ms=0), sharpest_at=2000)
        assert motor.set_position(-5) == 0
        assert motor.set_position(99999) == 4095
        assert [w[0] for w in motor.writes] == [0, 4095]

    def test_blur_is_lowest_at_the_sharp_position(self) -> None:
        """This curve is what the autofocus sweep climbs; it must be unimodal."""
        motor = SimulatedFocusMotor(FocusSettings(settle_ms=0), sharpest_at=2000)
        motor.set_position(2000)
        sharp = motor.blur_radius()
        motor.set_position(0)
        far = motor.blur_radius()
        assert sharp < far
        assert sharp == pytest.approx(0.0)


class TestServoMapping:
    def test_angle_to_pulse_spans_the_configured_range(self) -> None:
        rig = ServoRig(PanTiltSettings())  # not opened: pure mapping only
        assert rig.angle_to_pulse(0) == pytest.approx(500)
        assert rig.angle_to_pulse(180) == pytest.approx(2500)
        assert rig.angle_to_pulse(90) == pytest.approx(1500)

    def test_channels_and_frequency_from_settings(self) -> None:
        settings = PanTiltSettings(pan_channel=7, tilt_channel=9)
        rig = ServoRig(settings)
        assert (settings.pan_channel, settings.tilt_channel) == (7, 9)
        assert rig.settings.pwm_frequency_hz == 60.0

    def test_prescale_matches_the_rig_the_hardware_was_calibrated_on(self) -> None:
        """60 Hz with the original project's 0.8449 gives prescale 119, and 101 without it.

        This is the single most load-bearing number in the servo path. The 0.8449 is
        inherited verbatim - the original source records neither where it was measured nor
        what against (docs/HARDWARE.md sets out both readings of it) - but the servos in
        this bracket are quiet at the prescale it produces. The test pins the known-good
        value *and* the datasheet-nominal one, so the difference is visible to whoever
        next wonders why it is not 101.
        """
        from arducam_ptz.hardware.pca9685 import prescale_for

        assert prescale_for(60.0, 0.8449) == 119
        assert prescale_for(60.0) == 101  # what the nominal datasheet clock would give
        assert PanTiltSettings().pwm_frequency_correction == 0.8449

    def test_unavailable_rig_does_not_pretend_to_move(self) -> None:
        rig = ServoRig(PanTiltSettings())  # never opened: no bus
        assert rig.availability().available is False
        with pytest.raises(Exception, match="not initialised"):
            rig.set_angles(90, 90)


class TestSimulatedServoRig:
    def test_records_writes_and_release(self) -> None:
        rig = SimulatedServoRig(PanTiltSettings())
        rig.set_angles(100, 80)
        rig.set_angles(105, 80)
        rig.release()
        assert rig.last == (105, 80)
        assert rig.released is True
        assert len(rig.writes) == 2

    def test_fault_injection(self) -> None:
        rig = SimulatedServoRig(PanTiltSettings())
        rig.fail_next = 1
        with pytest.raises(Exception, match="simulated I2C failure"):
            rig.set_angles(90, 90)
        rig.set_angles(90, 90)  # recovers
        assert rig.last == (90, 90)


class TestPca9685Timing:
    def test_pulse_to_ticks_at_50hz(self) -> None:
        """20 ms period, 4096 ticks per period -> 500 us is ~102 ticks."""
        chip = Pca9685(frequency_hz=50.0)
        assert chip.ticks_for_pulse(500) == pytest.approx(102, abs=1)
        assert chip.ticks_for_pulse(1500) == pytest.approx(307, abs=1)
        assert chip.ticks_for_pulse(2500) == pytest.approx(512, abs=1)

    def test_ticks_are_bounded(self) -> None:
        chip = Pca9685(frequency_hz=50.0)
        assert chip.ticks_for_pulse(0) == 0
        assert chip.ticks_for_pulse(10_000) <= 4095  # a long pulse cannot wrap the counter

    def test_write_before_open_is_an_error_not_a_crash(self) -> None:
        chip = Pca9685()
        with pytest.raises(AssertionError):
            chip.set_pulse_us(0, 1500)


class TestHardwareFactory:
    def test_simulation_uses_fake_backends_everywhere(self) -> None:
        settings = Settings(simulate=True)
        hardware = build_hardware(settings, sink=_NullSink(), log=logging.getLogger("test"))
        assert isinstance(hardware.pan_tilt, SimulatedServoRig)
        assert isinstance(hardware.focus, SimulatedFocusMotor)
        assert hardware.camera.availability().simulated is True
        assert all(a.simulated for a in hardware.availabilities().values())

    def test_per_section_simulation_flag(self) -> None:
        settings = Settings(pan_tilt={"simulate": True})
        assert isinstance(build_pan_tilt(settings), SimulatedServoRig)
        assert settings.simulate_everything() is True

    def test_bundle_start_is_survivable_without_any_hardware(self) -> None:
        """Boot half-alive rather than not at all: the server must come up."""
        settings = Settings(
            camera=CameraSettings(simulate=False),
            pan_tilt=PanTiltSettings(simulate=False),
            focus=FocusSettings(simulate=False),
            simulate=True,  # keep it offline in this test; start() must still work
        )
        hardware = build_hardware(settings, sink=_NullSink(), log=logging.getLogger("test"))
        hardware.start()
        try:
            availabilities = hardware.availabilities()
            assert set(availabilities) == {"camera", "focus", "pan_tilt"}
        finally:
            hardware.close()


class TestSinkHelpers:
    def test_frame_age_is_none_for_a_sink_that_cannot_tell(self) -> None:
        assert sink_frame_age(_NullSink()) is None


class _NullSink:
    """Minimal FrameSink for factory tests."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def publish(self, jpeg: bytes, *, width: int, height: int, timestamp: float) -> None:
        self.frames.append(jpeg)


def test_clamp_helper() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(-5, 0, 10) == 0
    assert clamp(50, 0, 10) == 10
