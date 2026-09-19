"""Shared pytest fixtures.

The whole point of the simulated hardware backends is that the entire stack -
services, HTTP API, even the CLI client - can be exercised on a laptop or in CI
with no camera, no I2C bus and no servos. That is the difference from the old
app, which only ran on the Pi because the hardware objects were module-level
globals created at import time.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
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
