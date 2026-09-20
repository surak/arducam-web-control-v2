"""Tests for the frame hub, the servo writer and the job registry.

These cover the concurrency behaviour that the old app got wrong (per-client
camera ownership, unbounded servo writes, blocking jobs) without any hardware.
"""

from __future__ import annotations

import threading
import time

import pytest

from arducam_ptz.config import PanTiltSettings
from arducam_ptz.hardware.servo import SimulatedServoRig
from arducam_ptz.models import JobStatus
from arducam_ptz.services.frames import FrameHub
from arducam_ptz.services.jobs import JobRegistry
from arducam_ptz.services.ptz import PanTiltService


def publish(hub: FrameHub, payload: bytes = b"\xff\xd8\xff\xdbjpeg", width: int = 960, height: int = 540) -> None:
    hub.publish(payload, width=width, height=height, timestamp=time.monotonic())


class TestFrameHub:
    def test_publish_without_subscribers_is_fine(self) -> None:
        hub = FrameHub()
        publish(hub)
        assert hub.snapshot() is not None
        assert hub.subscriber_count == 0

    def test_subscriber_receives_frames(self) -> None:
        hub = FrameHub()
        with hub.subscribe() as subscriber:
            publish(hub)
            frame = subscriber.get(timeout=1.0)
            assert frame is not None
            assert frame.jpeg.startswith(b"\xff\xd8")
            assert (frame.width, frame.height) == (960, 540)
            assert hub.subscriber_count == 1
        assert hub.subscriber_count == 0  # leaving the context unsubscribes

    def test_two_subscribers_both_get_the_same_frame(self) -> None:
        """The old app could not do this: the second viewer stole the camera."""
        hub = FrameHub()
        with hub.subscribe() as first, hub.subscribe() as second:
            publish(hub, b"\xff\xd8one")
            assert first.get(1.0).jpeg == b"\xff\xd8one"
            assert second.get(1.0).jpeg == b"\xff\xd8one"

    def test_slow_subscriber_drops_frames_instead_of_blocking_the_producer(self) -> None:
        """A phone on a bad link must not stall the camera for everyone else."""
        hub = FrameHub()
        with hub.subscribe() as slow:
            for index in range(5):
                publish(hub, b"\xff\xd8frame" + str(index).encode())
            # The slow subscriber never read anything: it holds the newest frame
            # and has counted the dropped ones.
            assert slow.dropped == 4
            assert slow.get(1.0).jpeg == b"\xff\xd8frame4"

    def test_wait_for_frame_requires_a_newer_sequence(self) -> None:
        hub = FrameHub()
        publish(hub, b"\xff\xd8old")
        before = hub.sequence
        assert hub.wait_for_frame(before, timeout=0.05) is None  # nothing new yet

        publisher = threading.Thread(target=lambda: (time.sleep(0.05), publish(hub, b"\xff\xd8new")))
        publisher.start()
        frame = hub.wait_for_frame(before, timeout=1.0)
        publisher.join()
        assert frame is not None and frame.jpeg == b"\xff\xd8new"

    def test_last_frame_age_tracks_the_clock(self) -> None:
        hub = FrameHub()
        assert hub.last_frame_age() is None
        publish(hub)
        assert hub.last_frame_age() < 0.5
        time.sleep(0.1)
        assert hub.last_frame_age() >= 0.1

    def test_stats_report_rate_and_viewers(self) -> None:
        hub = FrameHub()
        with hub.subscribe():
            for _ in range(10):
                publish(hub, b"\xff\xd8" + b"x" * 1000)
                time.sleep(0.01)
            stats = hub.stats()
            assert stats["subscribers"] == 1
            assert stats["sequence"] == 10
            assert stats["width"] == 960
            assert stats["kilobytes_per_s"] > 0
            assert stats["running"] is True

    def test_stats_are_zero_without_frames(self) -> None:
        stats = FrameHub().stats()
        assert stats["running"] is False
        assert stats["fps"] == 0
        assert stats["sequence"] == 0
        assert stats["last_frame_age_s"] is None


class TestPanTiltService:
    def _service(self, **overrides) -> tuple[PanTiltService, SimulatedServoRig]:
        settings = PanTiltSettings(**overrides)
        rig = SimulatedServoRig(settings)
        return PanTiltService(settings, rig), rig

    def test_centre_is_the_default_target(self) -> None:
        service, _ = self._service()
        state = service.state()
        assert (state.pan.angle, state.tilt.angle) == (90, 90)
        assert state.simulated is True
        assert state.available is True

    def test_absolute_move_writes_immediately(self) -> None:
        service, rig = self._service()
        state = service.goto(pan=110, tilt=70)
        assert (state.pan.angle, state.tilt.angle) == (110, 70)
        assert rig.last == (110, 70)
        assert state.clamps == []

    def test_out_of_range_move_is_clamped_and_reported(self) -> None:
        """The old API returned success while quietly moving 1 degree instead."""
        service, rig = self._service()
        state = service.goto(pan=180)
        assert state.pan.angle == 120  # pan_max
        assert rig.last[0] == 120
        assert len(state.clamps) == 1
        clamp = state.clamps[0]
        assert (clamp.axis, clamp.reason) == ("pan", "travel_limit")
        assert (clamp.requested, clamp.applied) == (180, 120)

    def test_max_step_limits_a_jump(self) -> None:
        service, _ = self._service(max_step_degrees=5)
        state = service.goto(pan=110)
        assert state.pan.angle == 95
        assert state.clamps[0].reason == "max_step"

    def test_nudge_is_relative(self) -> None:
        service, _ = self._service()
        service.goto(pan=100, tilt=100)
        state = service.nudge(pan=5, tilt=-10)
        assert (state.pan.angle, state.tilt.angle) == (105, 90)

    def test_rapid_nudges_are_coalesced(self) -> None:
        """100 joystick events must not become 100 I2C writes."""
        service, rig = self._service()
        for _ in range(50):
            service.nudge(pan=0.2)
        # Immediate writes are rate limited by min_write_interval_ms.
        assert len(rig.writes) <= 3
        # The target, however, is exactly where all the nudges add up to.
        assert service.state().pan.angle == pytest.approx(100.0)

    def test_writer_tick_pushes_the_pending_target(self) -> None:
        service, rig = self._service()
        service.nudge(pan=10)  # written immediately
        writes_after_nudge = len(rig.writes)
        service.nudge(pan=1)  # probably rate limited
        service._tick(now=time.monotonic() + 1.0)  # pretend the interval elapsed
        assert len(rig.writes) > writes_after_nudge
        assert rig.last[0] == pytest.approx(101.0)

    def test_identical_target_is_not_rewritten(self) -> None:
        service, rig = self._service()
        service.goto(pan=100, tilt=90)
        count = len(rig.writes)
        service.goto(pan=100, tilt=90)
        service._tick(now=time.monotonic() + 1.0)
        assert len(rig.writes) == count

    def test_release_cuts_the_pulses_and_a_move_re_arms(self) -> None:
        service, rig = self._service()
        state = service.release()
        assert state.pan.released is True and rig.released is True
        state = service.goto(pan=95)
        assert state.pan.released is False
        assert rig.released is False

    def test_inverted_axes_write_mirrored_angles(self) -> None:
        service, rig = self._service(invert_tilt=True)
        service.goto(tilt=60)
        assert rig.last[1] == 120  # 180 - 60

    def test_bus_failure_is_reported_not_raised(self) -> None:
        service, rig = self._service()
        rig.fail_next = 1
        state = service.goto(pan=100)
        assert state.detail and "simulated I2C failure" in state.detail
        # The writer keeps working afterwards.
        service._tick(now=time.monotonic() + 1.0)
        assert rig.last == (100, 90)

    def test_repeated_failures_trigger_a_reopen_attempt(self) -> None:
        service, rig = self._service()
        rig.fail_next = 10
        reopens = []
        rig.reopen = lambda: (reopens.append(1), True)[1]  # type: ignore[method-assign]
        for index in range(4):
            service.goto(pan=100 + index)
            service._tick(now=time.monotonic() + 10.0)
        assert reopens, "the service should try to recover the I2C board"

    def test_writer_thread_keeps_running_after_an_error(self) -> None:
        service, rig = self._service(write_rate_hz=50)
        service.start()
        try:
            rig.fail_next = 1
            service.goto(pan=100)
            deadline = time.monotonic() + 2.0
            while rig.last != (100, 90) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert rig.last == (100, 90)
        finally:
            service.stop()

    def test_stop_releases_the_servos(self) -> None:
        service, rig = self._service()
        service.start()
        service.stop()
        assert rig.released is True


class TestJobRegistry:
    def test_create_and_fetch(self) -> None:
        registry = JobRegistry()
        job = registry.create("autofocus", lambda job_id: f"/jobs/{job_id}")
        assert job.status is JobStatus.PENDING
        assert registry.get(job.id) is job
        assert job.poll_url.endswith(job.id)
        assert registry.get("nope") is None

    def test_job_lifecycle_is_monotonic(self) -> None:
        """A finished job must not be resurrected by a late progress update."""
        registry = JobRegistry()
        job = registry.create("autofocus", lambda job_id: job_id)
        job.update(status=JobStatus.RUNNING, progress=0.5, phase="coarse")
        assert (job.to_model().progress, job.to_model().phase) == (0.5, "coarse")
        job.succeed({"position": 1200})
        job.update(status=JobStatus.RUNNING, progress=0.1)
        model = job.to_model()
        assert model.status is JobStatus.COMPLETED
        assert model.progress == 1.0
        assert model.result == {"position": 1200}

    def test_failure_records_the_reason(self) -> None:
        registry = JobRegistry()
        job = registry.create("autofocus", lambda job_id: job_id)
        job.fail("camera busy")
        assert job.to_model().status is JobStatus.FAILED
        assert job.to_model().error == "camera busy"

    def test_cancellation_sets_the_flag_and_marks_terminal(self) -> None:
        registry = JobRegistry()
        job = registry.create("autofocus", lambda job_id: job_id)
        job.update(status=JobStatus.RUNNING)
        job.cancel()
        assert job.cancelled is True
        assert job.status is JobStatus.RUNNING  # the worker decides when to stop
        job.cancelled_finally()
        assert job.status is JobStatus.CANCELLED

    def test_active_and_latest(self) -> None:
        registry = JobRegistry()
        first = registry.create("autofocus", lambda job_id: job_id)
        first.succeed(None)
        second = registry.create("autofocus", lambda job_id: job_id)
        assert [job.id for job in registry.active("autofocus")] == [second.id]
        assert registry.latest("autofocus").id == second.id

    def test_pruning_drops_stale_finished_jobs(self) -> None:
        registry = JobRegistry(ttl_s=60.0)
        old = registry.create("autofocus", lambda job_id: job_id)
        old.succeed(None)
        old.updated_at -= 600.0  # finished ten minutes ago
        fresh = registry.create("autofocus", lambda job_id: job_id)
        fresh.succeed(None)
        registry.prune()
        assert registry.get(old.id) is None
        assert registry.get(fresh.id) is fresh

    def test_pruning_caps_the_number_of_finished_jobs(self) -> None:
        """However recent they are, do not accumulate unbounded job history."""
        registry = JobRegistry(ttl_s=3600.0, keep=2)
        jobs = []
        for _ in range(5):
            job = registry.create("autofocus", lambda job_id: job_id)
            job.succeed(None)
            job.updated_at -= 1.0  # age them apart so "newest" is well defined
            jobs.append(job)
        registry.prune()
        remaining = [job for job in (registry.get(job.id) for job in jobs) if job is not None]
        assert len(remaining) == 2
        assert {job.id for job in remaining} == {jobs[-1].id, jobs[-2].id}

    def test_running_jobs_are_never_pruned(self) -> None:
        registry = JobRegistry(ttl_s=0.0, keep=0)
        job = registry.create("autofocus", lambda job_id: job_id)
        job.update(status=JobStatus.RUNNING)
        registry.prune()
        assert registry.get(job.id) is job
