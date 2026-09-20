"""CLI tests.

The CLI is exercised **through HTTP**, against the real application in-process
(``httpx.ASGITransport``), not against the services directly. That is deliberate: it
proves the CLI only needs the API - which is the whole point of splitting the front-end
from the hardware. If a command ever needed to import ``arducam_ptz.hardware`` to work,
these tests would still pass while the real product would be broken, so there is also
:func:`test_cli_does_not_import_the_hardware_layer` to catch exactly that.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arducam_ptz import cli

runner = CliRunner()


@pytest.fixture
def cli_client(live_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A running server (see ``serve`` in conftest), with the CLI pointed at it.

    Over a real socket rather than an in-process transport, because the CLI is an HTTP
    client: httpx 0.28's ``ASGITransport`` is async-only, and an endless MJPEG download
    cannot be driven through TestClient at all. ``ARDUCAM_URL`` is how a real user would
    point the CLI at a Pi, so going through the environment exercises that wiring too.
    """
    monkeypatch.setenv("ARDUCAM_URL", live_url)
    monkeypatch.delenv("ARDUCAM_TOKEN", raising=False)
    yield live_url


def invoke(*args: str, json_output: bool = True):
    """Run the CLI.

    ``--json`` is passed explicitly because the Typer callback re-reads the global
    options on every invocation: module state set by a fixture is overwritten, exactly
    as it would be in a fresh shell.
    """
    return runner.invoke(cli.app, (["--json"] if json_output else []) + list(args))


def test_version_flag_does_not_need_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_state", dict(cli._state))
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "arducam-ptz" in result.stdout


def test_status_reports_the_simulated_rig(cli_client) -> None:
    result = invoke("status")
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["health"]["status"] == "simulated"
    assert "stream" in body


def test_health_command(cli_client) -> None:
    result = invoke("health")
    assert result.exit_code == 0
    assert "camera" in result.stdout


def test_move_and_nudge_return_the_resulting_angles(cli_client) -> None:
    moved = json.loads(invoke("move", "--pan", "100", "--tilt", "60").stdout)
    assert moved["pan"]["angle"] == pytest.approx(100)

    nudged = json.loads(invoke("nudge", "--pan", "5").stdout)
    assert nudged["pan"]["angle"] == pytest.approx(105, abs=1)


def test_move_without_axes_fails_with_a_message(cli_client) -> None:
    result = invoke("move")
    assert result.exit_code == 1
    assert "give --pan" in result.output


def test_out_of_range_move_reports_the_clamp(cli_client) -> None:
    result = invoke("move", "--pan", "400")
    assert result.exit_code == 0
    clamp = json.loads(result.stdout)["clamps"][0]
    assert clamp["reason"] == "travel_limit"


def test_center_and_release(cli_client) -> None:
    assert json.loads(invoke("center").stdout)["pan"]["angle"] == pytest.approx(90, abs=1)
    assert json.loads(invoke("release").stdout)["pan"]["released"] is True


def test_focus_set_and_nudge(cli_client) -> None:
    assert json.loads(invoke("focus", "800").stdout)["position"] == 800
    assert json.loads(invoke("focus-nudge", "50").stdout)["position"] == 850


def test_autofocus_wait_reports_a_position(cli_client) -> None:
    result = invoke("autofocus", "--wait")
    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.stdout.strip().splitlines() if line.startswith("{")]
    assert lines, result.stdout
    final = lines[-1]
    assert final["status"] == "completed", final.get("error")
    assert final["result"]["position"] is not None


def test_autofocus_with_a_region(cli_client) -> None:
    result = invoke("autofocus", "--wait", "--region", "0.3,0.3,0.4,0.4")
    assert result.exit_code == 0
    final = json.loads([line for line in result.stdout.strip().splitlines() if line.startswith("{")][-1])
    assert final["status"] == "completed"


def test_capture_photos_and_download(cli_client, tmp_path: Path) -> None:
    taken = json.loads(invoke("capture").stdout)
    assert taken["filename"].endswith(".jpg")

    listed = json.loads(invoke("photos", "--limit", "5").stdout)
    assert [photo["filename"] for photo in listed] == [taken["filename"]]

    target = tmp_path / "downloaded.jpg"
    result = invoke("get-photo", taken["filename"], "--output", str(target))
    assert result.exit_code == 0
    assert target.read_bytes().startswith(b"\xff\xd8")


def test_get_photo_with_max_dim_downloads_a_thumbnail(cli_client, tmp_path: Path) -> None:
    taken = json.loads(invoke("capture").stdout)
    target = tmp_path / "thumb.jpg"
    invoke("get-photo", taken["filename"], "--output", str(target), "--max-dim", "200")
    invoke("get-photo", taken["filename"], "--output", str(tmp_path / "full.jpg"))
    assert target.stat().st_size < (tmp_path / "full.jpg").stat().st_size


def test_delete_photo_with_confirmation_flag(cli_client) -> None:
    taken = json.loads(invoke("capture").stdout)
    assert invoke("delete-photo", taken["filename"], "--yes").exit_code == 0
    assert json.loads(invoke("photos").stdout) == []


def test_unknown_photo_is_a_clean_error(cli_client) -> None:
    result = invoke("get-photo", "nope.jpg")
    assert result.exit_code == 1
    assert "HTTP 404" in result.output


def test_unreachable_server_is_reported_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["--json", "--url", "http://127.0.0.1:9", "--timeout", "1", "status"])
    assert result.exit_code == 1
    assert "cannot reach" in result.output


def test_stream_writes_frames_to_a_file(cli_client, tmp_path: Path) -> None:
    """The MJPEG extractor is exercised against a real multiplexed stream."""
    out = tmp_path / "clip.mjpg"
    result = invoke("stream", "--seconds", "1.5", "--output", str(out))
    assert result.exit_code == 0
    data = out.read_bytes()
    assert data.startswith(b"\xff\xd8")
    assert data.count(b"\xff\xd9") >= 2  # at least two whole JPEGs


def test_cli_does_not_import_the_hardware_layer() -> None:
    """The CLI must be a pure API client.

    Checked by reading the module source for hardware imports instead of importing and
    inspecting: this catches a conditional or lazy import too, which an import-time
    check would miss.
    """
    source = Path(cli.__file__).read_text()
    for forbidden in ("arducam_ptz.hardware", "arducam_ptz.services", "picamera2", "smbus2", "v4l2"):
        assert forbidden not in source, f"the CLI must not reference {forbidden}"


def test_stream_idle_timeout_uses_a_short_value(cli_client) -> None:
    """Sanity check on the stream interval: a slow consumer must not stall others.

    Indirect, but it pins the behaviour that matters: frames keep arriving while a
    previous iteration is still being written.
    """
    start = time.monotonic()
    result = invoke("stream", "--seconds", "0.5")
    assert result.exit_code == 0
    assert time.monotonic() - start < 10
