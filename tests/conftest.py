"""Shared pytest fixtures.

The whole point of the simulated hardware backends is that the entire stack -
services, HTTP API, even the CLI client - can be exercised on a laptop or in CI
with no camera, no I2C bus and no servos. That is the difference from the old
app, which only ran on the Pi because the hardware objects were module-level
globals created at import time.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from arducam_ptz.config import Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with every backend simulated and all state inside tmp_path.

    The environment is scrubbed of ARDUCAM_* so a developer's shell cannot leak
    into the test run.
    """
    for name in list(os.environ):
        if name.startswith("ARDUCAM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    loaded = load_settings()
    return loaded.model_copy(
        update={
            "simulate": True,
            "storage": loaded.storage.model_copy(
                update={
                    "photos_dir": tmp_path / "photos",
                    "thumbnail_dir": tmp_path / ".thumbs",
                }
            ),
        }
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """TestClient wired to a fully simulated application."""
    from arducam_ptz.api.app import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@contextmanager
def serve(settings: Settings) -> Iterator[str]:
    """Run the real app under uvicorn on a free loopback port; yield its base URL.

    ``TestClient`` is the fast way to test request/response endpoints, but it cannot
    drive a response that never ends: its transport waits for the first body chunk
    inside the same call that starts the app, so an endless MJPEG stream deadlocks the
    harness (the server is fine - the same endpoint streams happily to curl and to the
    browser). Anything stream-shaped therefore goes over a real socket, on port 0 so
    parallel runs cannot collide.

    This is also the honest test: the stream is read by an actual HTTP client, with the
    same framing a browser would see.
    """
    from arducam_ptz.api.app import create_app

    app = create_app(settings)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - startup failure
        raise RuntimeError("uvicorn did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


@pytest.fixture
def live_url(settings: Settings) -> Iterator[str]:
    """Base URL of a real server running the simulated application."""
    with serve(settings) as url:
        yield url
