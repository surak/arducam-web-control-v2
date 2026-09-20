"""API tests: every endpoint, through the real application, with simulated hardware.

The app is built by :func:`arducam_ptz.api.app.create_app` from simulated settings, so
these run on any machine and in CI. The ``TestClient`` context manager runs the
lifespan, which means the frame hub, the servo writer thread and the camera publisher
are all genuinely alive - the streaming test therefore consumes real frames instead of
a mock.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from arducam_ptz.api.app import API_PREFIX, create_app

PREFIX = API_PREFIX


@pytest.fixture
def client(settings):
    """A TestClient over the real app.

    ``settings`` comes from conftest and is fully simulated, with the photo directory
    pointed at a tmp_path.
    """
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def running_client(client):
    """A client whose camera is already producing frames."""
    _wait_for_frames(client, minimum=1)
    return client


def _poll_until_done(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    """Poll a job until it reaches a terminal state, or fail with the last body."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"{PREFIX}/focus/autofocus/{job_id}").json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.2)
    pytest.fail(f"job {job_id} still {body.get('status')} after {timeout}s")


def _read_frame(chunks) -> bytes:
    """Read one whole MJPEG part from a streaming response."""
    data = b""
    while b"\r\n\r\n" not in data:
        data += next(chunks)
    return data


@contextmanager
def _open_stream(url: str, timeout: float = 20) -> Iterator[tuple[httpx.Response, Iterator[bytes]]]:
    """Open an MJPEG stream and yield ``(response, chunk iterator)``.

    The iterator has to stay referenced for as long as the stream is read, which is why
    this is a context manager rather than something the tests open inline.
    ``response.iter_bytes()`` is a *generator*: passing it straight into ``_read_frame``
    drops the last reference as soon as that call returns, CPython finalises it, httpx
    closes the connection, and the server correctly reports a viewer disconnect. The
    stream then dies after exactly one frame - which looks like a server bug (and cost
    an afternoon of debugging here), so this helper keeps the reference obvious.
    """
    with httpx.stream("GET", url, timeout=timeout) as response:
        yield response, response.iter_bytes()


def _subscribers(url: str) -> int:
    return httpx.get(f"{url}{PREFIX}/stream/stats", timeout=5).json()["subscribers"]


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true (or the deadline passes)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _wait_for_frames(client: TestClient, minimum: int = 3, timeout: float = 5.0) -> int:
    """Wait until the hub reports at least ``minimum`` frames."""
    deadline = time.monotonic() + timeout
    sequence = 0
    while time.monotonic() < deadline:
        stats = client.get(f"{PREFIX}/stream/stats").json()
        sequence = stats["sequence"]
        if sequence >= minimum:
            return sequence
        time.sleep(0.05)
    pytest.fail(f"only {sequence} frames arrived within {timeout}s")


class TestStatus:
    def test_health_lists_every_component(self, client: TestClient) -> None:
        body = client.get(f"{PREFIX}/health").json()
        assert body["status"] == "simulated"  # nothing real is connected in tests
        assert [component["name"] for component in body["components"]] == ["camera", "pan_tilt", "focus"]
        assert all(component["available"] for component in body["components"])

    def test_state_is_one_round_trip_for_the_whole_ui(self, running_client: TestClient) -> None:
        body = running_client.get(f"{PREFIX}/state").json()
        assert set(body) == {"health", "ptz", "focus", "stream"}
        assert body["ptz"]["pan"]["min"] <= body["ptz"]["pan"]["angle"] <= body["ptz"]["pan"]["max"]
        assert body["focus"]["min"] <= body["focus"]["max"]
        assert body["stream"]["width"] > 0
        assert body["stream"]["sequence"] >= 1

    def test_version_is_reachable_without_a_token(self, client: TestClient) -> None:
        body = client.get("/api/version").json()
        assert body["name"] == "arducam-ptz"
        assert body["api_prefix"] == PREFIX


class TestPtzEndpoints:
    def test_move_returns_the_resulting_state(self, client: TestClient) -> None:
        body = client.post(f"{PREFIX}/ptz/move", json={"pan": 100, "tilt": 70}).json()
        assert body["pan"]["angle"] == pytest.approx(100)
        assert body["tilt"]["angle"] == pytest.approx(70)

    def test_move_outside_travel_is_clamped_and_reported(self, client: TestClient) -> None:
        body = client.post(f"{PREFIX}/ptz/move", json={"pan": 400, "tilt": 0}).json()
        assert body["pan"]["angle"] == pytest.approx(body["pan"]["max"])
        clamp = next(c for c in body["clamps"] if c["axis"] == "pan")
        assert clamp["requested"] == pytest.approx(400)
        assert clamp["reason"] == "travel_limit"

    def test_nudge_is_relative(self, client: TestClient) -> None:
        start = client.post(f"{PREFIX}/ptz/center").json()["pan"]["angle"]
        body = client.post(f"{PREFIX}/ptz/nudge", json={"pan": 5, "tilt": 0}).json()
        assert body["pan"]["angle"] == pytest.approx(start + 5, abs=0.6)

    def test_center_and_release(self, client: TestClient) -> None:
        centre = client.post(f"{PREFIX}/ptz/center").json()
        assert centre["pan"]["angle"] == pytest.approx(90, abs=1)
        released = client.post(f"{PREFIX}/ptz/release").json()
        assert released["pan"]["released"] is True
        # A move re-arms the servos: the UI must not need a second "wake up" call.
        assert client.post(f"{PREFIX}/ptz/move", json={"pan": 95}).json()["pan"]["released"] is False

    def test_unknown_field_is_rejected_not_ignored(self, client: TestClient) -> None:
        response = client.post(f"{PREFIX}/ptz/move", json={"pan": 10, "pan_angle": 20})
        assert response.status_code == 422  # extra="forbid" on request models

    def test_missing_axis_is_a_no_op(self, client: TestClient) -> None:
        before = client.get(f"{PREFIX}/ptz").json()
        after = client.post(f"{PREFIX}/ptz/move", json={"tilt": before["tilt"]["angle"]}).json()
        assert after["pan"]["angle"] == pytest.approx(before["pan"]["angle"], abs=0.6)


class TestFocusEndpoints:
    def test_get_and_set_position(self, client: TestClient) -> None:
        body = client.post(f"{PREFIX}/focus", json={"position": 900}).json()
        assert body["position"] == 900
        assert client.get(f"{PREFIX}/focus").json()["position"] == 900

    def test_position_is_clamped_to_the_motor_range(self, client: TestClient) -> None:
        body = client.post(f"{PREFIX}/focus", json={"position": 999999}).json()
        assert body["position"] == body["max"]

    def test_nudge_steps_are_clamped_at_the_ends(self, client: TestClient) -> None:
        client.post(f"{PREFIX}/focus", json={"position": 10})
        body = client.post(f"{PREFIX}/focus/nudge", json={"steps": -500}).json()
        assert body["position"] == body["min"]

    def test_autofocus_returns_202_and_a_pollable_job(self, running_client: TestClient) -> None:
        started = running_client.post(f"{PREFIX}/focus/autofocus", json={})
        assert started.status_code == 202
        job = started.json()
        assert job["status"] in {"queued", "running"}
        assert job["poll_url"] == f"{PREFIX}/focus/autofocus/{job['id']}"

        polled = _poll_until_done(running_client, job["id"])
        assert polled["status"] == "completed", polled.get("error")
        result = polled["result"]
        assert result["position"] is not None
        # The sweep reports its samples so a caller can see *why* it chose a position.
        assert len(result["samples"]) >= 8
        # The sweep must leave the motor at the position it reports as best.
        assert running_client.get(f"{PREFIX}/focus").json()["position"] == result["position"]

    def test_tap_to_focus_region_is_accepted_in_the_shape_the_ui_sends(
        self, running_client: TestClient
    ) -> None:
        """The payload the web UI posts when you tap the image must validate.

        `regionFromTap()` in app.js builds this object and sends it verbatim, so its
        field names are part of the API contract. They drifted once - the UI sent
        {x, y, w, h} while `Region` wants width/height - and a tap came back as a 422
        that the page rendered as raw JSON. Pinned here so the contract is tested from
        the Python side too, not only by the browser probe.
        """
        region = {"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2}
        started = running_client.post(f"{PREFIX}/focus/autofocus", json={"region": region})
        assert started.status_code == 202, started.text

        polled = _poll_until_done(running_client, started.json()["id"])
        assert polled["status"] == "completed", polled.get("error")
        # The sweep must report which window it scored, otherwise a caller cannot tell
        # an ignored region from an honoured one.
        assert polled["result"]["region"] == region

    def test_short_region_field_names_are_rejected(self, client: TestClient) -> None:
        """`w`/`h` must fail loudly rather than being quietly ignored.

        If the model silently accepted unknown keys, a region would lose its size and
        tap-to-focus would quietly focus the whole frame instead of the tapped spot -
        the same user-visible bug, minus the error message that exposes it.
        """
        response = client.post(
            f"{PREFIX}/focus/autofocus", json={"region": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}}
        )
        assert response.status_code == 422
        assert "width" in response.text and "height" in response.text

    def test_second_sweep_answers_409_with_the_running_job(self, running_client: TestClient) -> None:
        first = running_client.post(f"{PREFIX}/focus/autofocus", json={}).json()
        second = running_client.post(f"{PREFIX}/focus/autofocus", json={})
        if second.status_code == 409:
            body = second.json()
            assert body["error"] == "autofocus_busy"
            assert body["job_id"] == first["id"]
            assert body["component"] == "focus"
        else:  # pragma: no cover - only if the first sweep finished unusually fast
            pytest.skip("first sweep completed before the second request")

    def test_unknown_job_is_404(self, client: TestClient) -> None:
        assert client.get(f"{PREFIX}/focus/autofocus/nope").status_code == 404
        assert client.post(f"{PREFIX}/focus/autofocus/nope/cancel").status_code == 404

    def test_focus_state_reports_the_last_sweep(self, running_client: TestClient) -> None:
        running_client.post(f"{PREFIX}/focus/autofocus", json={})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if running_client.get(f"{PREFIX}/focus").json().get("autofocus"):
                break
            time.sleep(0.2)
        assert running_client.get(f"{PREFIX}/focus").json()["autofocus"] is not None


class TestStreamEndpoints:
    def test_stats_reach_the_client(self, running_client: TestClient) -> None:
        stats = running_client.get(f"{PREFIX}/stream/stats").json()
        assert stats["sequence"] >= 1
        assert stats["width"] > 0 and stats["height"] > 0
        assert stats["subscribers"] == 0

    def test_snapshot_is_a_jpeg(self, running_client: TestClient) -> None:
        response = running_client.get(f"{PREFIX}/stream/snapshot.jpg")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content.startswith(b"\xff\xd8")  # JPEG SOI

    def test_snapshot_without_frames_is_204_not_an_error(self, client: TestClient) -> None:
        """A client polling before the camera is up gets "not yet", not a 500."""
        response = client.get(f"{PREFIX}/stream/snapshot.jpg")
        assert response.status_code in {200, 204}

    def test_mjpeg_stream_yields_multipart_frames(self, live_url: str) -> None:
        """The multipart framing a browser needs, read by a real HTTP client.

        Real server (see ``serve`` in conftest) because TestClient's transport waits for
        a body chunk inside the call that starts the app, and an endless stream never
        satisfies that - it deadlocks the harness, not the server.
        """
        with _open_stream(f"{live_url}{PREFIX}/stream.mjpg") as (response, chunks):
            assert response.status_code == 200
            assert "multipart/x-mixed-replace" in response.headers["content-type"]
            assert response.headers.get("x-accel-buffering") == "no"
            first = _read_frame(chunks)
            assert first.startswith(b"--arducamframe")
            # The part's own headers are what make a browser render frame N instead of
            # buffering everything until the connection closes.
            assert b"Content-Type: image/jpeg" in first
            assert b"Content-Length:" in first

    def test_the_preview_keeps_streaming_for_a_watching_client(self, live_url: str) -> None:
        """Frames keep coming, with the viewer still counted as connected.

        This is the test that catches a preview which freezes after one frame: a slow
        but healthy viewer (one frame every 150 ms, with live stats reads in between)
        must keep its slot for the whole run. Reading a burst of frames back to back
        cannot tell the difference - by then the data is already in the socket buffer,
        so a response that has ended looks just like one that is still going.
        """
        with _open_stream(f"{live_url}{PREFIX}/stream.mjpg") as (_response, chunks):
            for index in range(6):
                assert _read_frame(chunks).startswith(b"--arducamframe")
                assert _subscribers(live_url) == 1, f"lost the subscription at frame {index}"
                time.sleep(0.15)

    def test_stream_subscribes_then_releases_the_viewer(self, live_url: str) -> None:
        """A viewer that goes away costs nothing and frees its hub slot.

        Two *simultaneous* viewers are covered at the hub level
        (``test_two_subscribers_both_get_the_same_frame`` and the slow-subscriber test in
        test_services_core.py). What is API-specific - and what the old app got wrong by
        keeping a dead generator alive forever - is that subscribing costs a slot, and
        that a client hanging up frees it again.
        """
        with _open_stream(f"{live_url}{PREFIX}/stream.mjpg") as (_response, chunks):
            assert _read_frame(chunks).startswith(b"--arducamframe")
            assert _wait_for(lambda: _subscribers(live_url) >= 1), "subscriber was never counted"
        # Leaving the ``with`` closes the socket; the server must notice.
        assert _wait_for(lambda: _subscribers(live_url) == 0, timeout=10), (
            "hub kept a subscriber after the client hung up"
        )


class TestCaptureEndpoints:
    def test_capture_creates_a_listed_photo(self, client: TestClient) -> None:
        created = client.post(f"{PREFIX}/captures")
        assert created.status_code == 201
        photo = created.json()
        assert photo["filename"].endswith(".jpg")
        assert photo["url"] == f"{PREFIX}/captures/{photo['filename']}"
        assert photo["width"] and photo["height"]
        assert [item["filename"] for item in client.get(f"{PREFIX}/captures").json()] == [photo["filename"]]

    def test_photo_can_be_fetched_full_size_and_as_a_thumbnail(self, client: TestClient) -> None:
        """The gallery asks for small versions; the download must still be the original.

        Checked by decoded pixel size rather than byte count: a phone-sized thumbnail of
        a small preview image is not necessarily 4x smaller in bytes, but it must be
        capped at the requested dimension, and the full-size fetch must be untouched.
        """
        photo = client.post(f"{PREFIX}/captures").json()
        full = client.get(f"{PREFIX}/captures/{photo['filename']}")
        assert full.status_code == 200
        assert full.content.startswith(b"\xff\xd8")
        assert (full.headers["content-type"], "content-length" in full.headers) == ("image/jpeg", True)

        thumb = client.get(f"{PREFIX}/captures/{photo['filename']}?max_dim=320")
        assert thumb.status_code == 200
        with Image.open(io.BytesIO(thumb.content)) as scaled, Image.open(io.BytesIO(full.content)) as original:
            assert max(scaled.size) <= 320
            assert max(scaled.size) < max(original.size)
            assert scaled.format == "JPEG"

    def test_thumbnail_is_cached_not_recomputed(self, client: TestClient) -> None:
        photo = client.post(f"{PREFIX}/captures").json()
        first = client.get(f"{PREFIX}/captures/{photo['filename']}?max_dim=200")
        second = client.get(f"{PREFIX}/captures/{photo['filename']}?max_dim=200")
        assert first.content == second.content

    def test_delete_removes_the_photo(self, client: TestClient) -> None:
        photo = client.post(f"{PREFIX}/captures").json()
        assert client.delete(f"{PREFIX}/captures/{photo['filename']}").status_code == 204
        assert client.get(f"{PREFIX}/captures/{photo['filename']}").status_code == 404
        assert client.get(f"{PREFIX}/captures").json() == []

    def test_path_traversal_is_404_not_a_file_leak(self, client: TestClient) -> None:
        """The old download endpoint joined a client string onto a path."""
        for name in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "....//etc/passwd"):
            response = client.get(f"{PREFIX}/captures/{name}")
            assert response.status_code in {404, 400, 405}, name

    def test_unknown_photo_is_404(self, client: TestClient) -> None:
        assert client.get(f"{PREFIX}/captures/nope.jpg").status_code == 404
        assert client.delete(f"{PREFIX}/captures/nope.jpg").status_code == 404


class TestAuth:
    def test_open_by_default(self, client: TestClient) -> None:
        assert client.get(f"{PREFIX}/health").status_code == 200

    def test_token_is_enforced_when_configured(self, settings) -> None:
        locked = settings.model_copy(
            update={"server": settings.server.model_copy(update={"token": "s3cret"})}
        )
        with TestClient(create_app(locked)) as locked_client:
            assert locked_client.get(f"{PREFIX}/health").status_code == 401
            assert locked_client.get(f"{PREFIX}/health", headers={"X-API-Token": "s3cret"}).status_code == 200
            assert locked_client.get(f"{PREFIX}/health", headers={"X-API-Token": "wrong"}).status_code == 401
            # The query-string form exists for <img> tags, which cannot set headers.
            assert locked_client.get(f"{PREFIX}/health?token=s3cret").status_code == 200

    def test_plain_http_error_body_is_machine_readable(self, settings) -> None:
        locked = settings.model_copy(
            update={"server": settings.server.model_copy(update={"token": "s3cret"})}
        )
        with TestClient(create_app(locked)) as locked_client:
            body = locked_client.get(f"{PREFIX}/health").json()
            assert isinstance(body["detail"], str)
