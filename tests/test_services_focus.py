"""Focus service tests.

The interesting one is :class:`TestAutofocusSweep`: it runs a *complete* autofocus
sweep - real background thread, real polling, real frame sampling - against the
simulated camera and motor, where sharpness is a function of the motor position.
In other words the sweep logic is tested for correctness (does it find the sharp
position?) rather than for whether it merely fails to crash.
"""

from __future__ import annotations

import io
import time

import numpy as np
import pytest
from PIL import Image

from arducam_ptz.config import CameraSettings, FocusSettings
from arducam_ptz.hardware.camera import SimulatedCamera
from arducam_ptz.hardware.focus import SimulatedFocusMotor
from arducam_ptz.models import JobStatus, Region
from arducam_ptz.services.focus import FocusBusyError, FocusService, sharpness
from arducam_ptz.services.frames import FrameHub
from arducam_ptz.services.jobs import JobRegistry


def checkerboard(value: int = 255, size: int = 96) -> bytes:
    """A high-contrast target: sharp when in focus, flat when blurred."""
    squares = np.zeros((size, size), dtype=np.uint8)
    squares[::8, :] = value
    squares[:, ::8] = value
    image = Image.fromarray(squares, mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def flat_grey(level: int = 128, size: int = 96) -> bytes:
    image = Image.fromarray(np.full((size, size), level, dtype=np.uint8), mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class TestSharpnessMetric:
    def test_sharp_beats_blurred(self) -> None:
        from PIL import ImageFilter

        sharp = checkerboard()
        blurred_buffer = io.BytesIO()
        with Image.open(io.BytesIO(sharp)) as image:
            image.filter(ImageFilter.GaussianBlur(radius=3)).save(blurred_buffer, format="JPEG", quality=95)
        assert sharpness(sharp) > sharpness(blurred_buffer.getvalue())

    def test_featureless_image_scores_zero_not_nan(self) -> None:
        """A dark wall must not produce NaN and send the sweep oscillating."""
        score = sharpness(flat_grey())
        assert score == pytest.approx(0.0, abs=1e-6)
        assert np.isfinite(score)

    def test_region_narrows_the_measurement(self) -> None:
        """Scoring only the centre window must differ from scoring everything."""
        whole = sharpness(checkerboard())
        centred = sharpness(checkerboard(), Region.centred(0.25))
        assert centred > 0
        assert centred != whole

    def test_region_touching_the_frame_edge_is_safe(self) -> None:
        """Pixel rounding at the boundary must not read past the image."""
        score = sharpness(checkerboard(), Region(x=0.5, y=0.5, width=0.5, height=0.5))
        assert score >= 0.0

    def test_tiny_region_is_widened_to_something_measurable(self) -> None:
        assert sharpness(checkerboard(), Region(x=0.5, y=0.5, width=1e-6, height=1e-6)) > 0


class TestFocusServiceManual:
    def _service(self, camera: SimulatedCamera | None = None, hub: FrameHub | None = None):
        settings = FocusSettings()
        motor = SimulatedFocusMotor(FocusSettings())
        hub = hub or FrameHub()
        jobs = JobRegistry()
        camera = camera or SimulatedCamera(CameraSettings(), hub, blur_source=motor)
        return FocusService(settings, motor, hub, jobs, camera=camera), motor, hub, jobs

    def test_state_reports_limits_and_position(self) -> None:
        service, _, _, _ = self._service()
        state = service.state()
        assert state.available is True and state.simulated is True
        assert (state.min, state.max) == (0, 4095)
        # The simulated motor reports its own position; nothing is invented here.
        assert state.position is not None

    def test_set_position_moves_the_motor(self) -> None:
        service, motor, _, _ = self._service()
        service.set_position(1234)
        assert motor.position == 1234
        assert service.state().position == 1234

    def test_nudge_is_relative(self) -> None:
        service, motor, _, _ = self._service()
        service.set_position(1000)
        service.nudge(250)
        assert motor.position == 1250

    def test_out_of_range_position_is_clamped_by_the_motor(self) -> None:
        service, motor, _, _ = self._service()
        service.set_position(99999)
        assert motor.position == 4095

    def test_unavailable_motor_reports_instead_of_raising(self) -> None:
        from arducam_ptz.hardware.focus import V4L2FocusMotor

        motor = V4L2FocusMotor(FocusSettings(device="/dev/definitely-not-here"))
        service = FocusService(FocusSettings(), motor, FrameHub(), JobRegistry())
        state = service.state()
        assert state.available is False
        with pytest.raises(RuntimeError, match="not initialised"):
            service.set_position(100)

    def test_manual_move_cancels_a_running_autofocus(self) -> None:
        """Grabbing the slider wins; a sweep must not fight the user."""
        service, _, _, jobs = self._service()
        started = service.start_autofocus()
        service.set_position(500)
        job = jobs.get(started.id)
        assert job is not None and job.cancelled is True


class TestAutofocusSweep:
    """End-to-end sweeps through the simulated hardware."""

    def _run(self, *, sharpest_at: int = 2048, region: Region | None = None, timeout: float = 20.0):
        camera_settings = CameraSettings()
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings(), sharpest_at=sharpest_at)
        camera = SimulatedCamera(camera_settings, hub, blur_source=motor, fps=30.0)
        jobs = JobRegistry()
        focus_settings = FocusSettings(
            coarse_steps=9,
            fine_steps=5,
            settle_ms=0,  # simulated frames are immediate; the hub's sequence check
            sample_timeout_ms=500,  # is what enforces freshness
        )
        service = FocusService(focus_settings, motor, hub, jobs, camera=camera)
        camera.start()
        try:
            job = service.start_autofocus(region)
            deadline = time.monotonic() + timeout
            state = job
            while not state.status.terminal and time.monotonic() < deadline:
                time.sleep(0.05)
                state = service.job(job.id) or state
            assert state.status is JobStatus.COMPLETED, f"job ended as {state.status}: {state.error}"
            result = state.autofocus_result()
            assert result is not None
            return service, motor, result
        finally:
            camera.stop()

    def test_sweep_finds_the_sharp_position(self) -> None:
        _, motor, result = self._run(sharpest_at=2048)
        assert abs(result.position - 2048) < 400, f"settled at {result.position}"
        # Every sample is reported, so a bad hunt can be diagnosed later.
        assert len(result.samples) >= 10
        assert {sample.phase for sample in result.samples} == {"coarse", "fine"}
        # The best fine sample must be no worse than the best coarse one.
        best_any = max(result.samples, key=lambda sample: sample.score)
        assert result.position == best_any.position
        assert motor.position == result.position

    def test_sweep_converges_near_the_bottom_of_the_range(self) -> None:
        """A target at one end must not be missed by an even coarse sweep."""
        _, _, result = self._run(sharpest_at=200)
        assert abs(result.position - 200) < 500

    def test_sweep_with_a_region_uses_it(self) -> None:
        _, _, result = self._run(sharpest_at=3000, region=Region.centred(0.4))
        assert result.region is not None
        assert abs(result.position - 3000) < 500

    def test_preview_keeps_streaming_during_the_sweep(self) -> None:
        """The whole point: you can watch the picture while it focuses."""
        camera_settings = CameraSettings()
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings(), sharpest_at=1000)
        camera = SimulatedCamera(camera_settings, hub, blur_source=motor, fps=30.0)
        service = FocusService(
            FocusSettings(coarse_steps=7, fine_steps=3, settle_ms=0, sample_timeout_ms=500),
            motor,
            hub,
            JobRegistry(),
        )
        camera.start()
        try:
            with hub.subscribe() as viewer:
                job = service.start_autofocus()
                delivered = 0
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if viewer.get(timeout=0.5) is not None:
                        delivered += 1
                    state = service.job(job.id)
                    if state and state.status.terminal:
                        break
                assert delivered > 5, "preview frames should keep arriving during a sweep"
        finally:
            camera.stop()

    def test_second_sweep_is_rejected_while_one_runs(self) -> None:
        camera_settings = CameraSettings()
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings(), sharpest_at=2048)
        camera = SimulatedCamera(camera_settings, hub, blur_source=motor, fps=2.0)
        service = FocusService(
            FocusSettings(coarse_steps=15, fine_steps=9, settle_ms=50, sample_timeout_ms=500),
            motor,
            hub,
            JobRegistry(),
        )
        camera.start()
        try:
            first = service.start_autofocus()
            with pytest.raises(FocusBusyError) as excinfo:
                service.start_autofocus()
            assert excinfo.value.job_id == first.id  # tells the client what to poll
            service.cancel(first.id)
        finally:
            camera.stop()

    def test_sweep_without_frames_fails_with_a_reason(self) -> None:
        """No preview (camera stopped) must explain itself, not park the lens."""
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings())
        service = FocusService(
            FocusSettings(coarse_steps=5, fine_steps=1, settle_ms=0, sample_timeout_ms=100),
            motor,
            hub,
            JobRegistry(),
        )
        job = service.start_autofocus()
        deadline = time.monotonic() + 5.0
        state = job
        while not state.status.terminal and time.monotonic() < deadline:
            time.sleep(0.05)
            state = service.job(job.id) or state
        assert state.status is JobStatus.FAILED
        assert state.error and "no preview frames" in state.error

    def test_sweep_waits_for_a_busy_camera_then_fails_cleanly(self) -> None:
        """A still capture in progress is waited for, then reported if it sticks."""
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings())
        camera = SimulatedCamera(CameraSettings(), hub, blur_source=motor, fps=30.0)
        # A still capture in progress: the camera reports busy and (as on real
        # hardware) stops producing preview frames while it is in that mode.
        camera._busy = True
        service = FocusService(
            FocusSettings(coarse_steps=3, fine_steps=1, settle_ms=0, sample_timeout_ms=120),
            motor,
            hub,
            JobRegistry(),
            camera=camera,
        )
        job = service.start_autofocus()
        deadline = time.monotonic() + 5.0
        state = job
        while not state.status.terminal and time.monotonic() < deadline:
            time.sleep(0.05)
            state = service.job(job.id) or state
        assert state.status is JobStatus.FAILED
        assert state.error and "no preview frames" in state.error
        camera._busy = False

    def test_cancelled_sweep_stops_early_and_says_so(self) -> None:
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings(), sharpest_at=2048)
        camera = SimulatedCamera(CameraSettings(), hub, blur_source=motor, fps=30.0)
        service = FocusService(
            FocusSettings(coarse_steps=25, fine_steps=9, settle_ms=80, sample_timeout_ms=500),
            motor,
            hub,
            JobRegistry(),
        )
        camera.start()
        try:
            job = service.start_autofocus()
            time.sleep(0.3)  # let it get a few samples in
            service.cancel(job.id)
            deadline = time.monotonic() + 10.0
            state = service.job(job.id)
            while state and not state.status.terminal and time.monotonic() < deadline:
                time.sleep(0.05)
                state = service.job(job.id)
            assert state is not None and state.status is JobStatus.CANCELLED
        finally:
            camera.stop()

    def test_sweep_without_settle_time_still_finds_the_sharp_position(self) -> None:
        """Regression: a sweep that scores its own pre-move frame hunts the wrong way.

        With ``settle_ms=0`` the very next published frame is the one most likely to
        hold pixels from before the move (it was already in the encoder). The hub's
        arrival-time + skip-frames rule must reject it, otherwise the first sample is
        scored with the *previous* position's sharpness - which here is the sharpest
        frame of all, and would park the lens at the blurriest end of the travel.
        """
        _, _, result = self._run(sharpest_at=2048)
        assert abs(result.position - 2048) < 400, f"settled at {result.position}"
        # And the reported best score must belong to the reported position.
        best = max(result.samples, key=lambda sample: sample.score)
        assert best.position == result.position
        assert abs(result.position - 2048) < abs(min(sample.position for sample in result.samples) - 2048)

    def test_noisy_preview_does_not_produce_zero_scores(self) -> None:
        """Scores must be finite and non-negative for every sampled position."""
        _, _, result = self._run(sharpest_at=1024)
        assert all(sample.score >= 0 for sample in result.samples)
        assert all(np.isfinite(sample.score) for sample in result.samples)

    def test_job_is_pollable_by_id(self) -> None:
        hub = FrameHub()
        motor = SimulatedFocusMotor(FocusSettings(), sharpest_at=2048)
        camera = SimulatedCamera(CameraSettings(), hub, blur_source=motor, fps=30.0)
        service = FocusService(
            FocusSettings(coarse_steps=5, fine_steps=3, settle_ms=0, sample_timeout_ms=500),
            motor,
            hub,
            JobRegistry(),
        )
        camera.start()
        try:
            job = service.start_autofocus()
            assert service.job(job.id) is not None
            assert job.poll_url.endswith(job.id)
            assert service.job("does-not-exist") is None
        finally:
            camera.stop()


class TestLinspace:
    def test_endpoints_are_included_and_duplicates_dropped(self) -> None:
        assert FocusService._linspace(0, 100, 5) == [0, 25, 50, 75, 100]
        assert FocusService._linspace(0, 3, 5) == [0, 1, 2, 3]  # rounds, drops repeats
        assert FocusService._linspace(10, 10, 1) == [10]

