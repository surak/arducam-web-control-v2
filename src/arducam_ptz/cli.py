"""Terminal client: ``arducam ...``.

**This client only speaks HTTP.** It does not import the hardware layer, does not know
what I2C or V4L2 are, and works from a laptop against a Pi on the network. That is the
practical proof that the functionality is detached from the web front-end: the same
endpoints serve the browser UI, this CLI, ``curl`` and a future iPhone app.

Examples
--------
    arducam status                       # health + state, one round trip
    arducam move --pan 110 --tilt 80     # absolute move
    arducam nudge --pan -5               # joystick-style relative move
    arducam focus 1200                   # set the lens position
    arducam autofocus --wait             # sweep, reporting progress
    arducam capture --download photo.jpg # take a photo and fetch it
    arducam photos --limit 5             # list what is on the device
    arducam stream --seconds 5 --output clip.mjpg

Machine-readable output: every command accepts ``--json``, which prints the response
body verbatim (the same schema the web UI receives). Human-readable output is a
convenience layer on top and never invents information that is not in that body.
"""

from __future__ import annotations

import json as json_module
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import typer

from arducam_ptz import __version__

DEFAULT_URL = "http://localhost:8080"

app = typer.Typer(
    add_completion=False,
    # no_args_is_help is off so that a bare `arducam` (and `arducam --version`) reach
    # the callback below instead of Typer's usage error: printing help with exit code 2
    # for `--version` is a papercut every CLI user hits once.
    no_args_is_help=False,
    help="Control an arducam-ptz server over HTTP. See `arducam <command> --help`.",
)

#: Global options live in a small context object so every command can use them
#: without repeating the flags in the signature.
_state: dict[str, Any] = {"url": DEFAULT_URL, "token": None, "json": False, "timeout": 10.0}


def _client() -> httpx.Client:
    """Build the HTTP client. Tests replace this to inject an in-process transport."""
    headers = {"X-API-Token": _state["token"]} if _state["token"] else {}
    return httpx.Client(base_url=_state["url"], headers=headers, timeout=_state["timeout"])


def _fail(message: str, code: int = 1) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _api(method: str, path: str, payload: dict[str, Any] | None = None, *, expect: int = 200) -> Any:
    """One API call, with errors turned into readable messages.

    Every non-2xx response carries a machine-readable body (see api/errors.py), so the
    CLI reports the server's own explanation - never a generic "request failed".
    """
    with _client() as client:
        try:
            response = client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            _fail(f"cannot reach {_state['url']} ({exc.__class__.__name__}: {exc})")
            raise typer.Exit(1) from exc
    if response.status_code >= 300:
        try:
            body = response.json()
            detail = body.get("detail") or body.get("error") or response.text
            component = body.get("component")
            if component:
                detail = f"{detail} [{component}]"
            job_id = body.get("job_id")
            if job_id:
                detail = f"{detail} (job {job_id})"
        except ValueError:
            detail = response.text or response.reason_phrase
        _fail(f"HTTP {response.status_code}: {detail}")
    if expect == 204 or not response.content:
        return None
    return response.json()


def _out(data: Any, render: Any | None = None) -> None:
    """Print either raw JSON or a human rendering of the same data."""
    if _state["json"]:
        typer.echo(json_module.dumps(data, indent=2, default=str))
    elif render is not None:
        render(data)


def _clamp_note(state: dict[str, Any]) -> str:
    """Turn clamp reports into one line a human can act on."""
    if not state.get("clamps"):
        return ""
    parts = []
    for clamp in state["clamps"]:
        reason = "outside the rig's travel" if clamp["reason"] == "travel_limit" else "too large a jump"
        parts.append(
            f"{clamp['axis']}: asked {clamp['requested']:g}, applied {clamp['applied']:g} ({reason})"
        )
    return "  [clamped: " + "; ".join(parts) + "]"


@app.callback(invoke_without_command=True)
def main_options(
    ctx: typer.Context,
    url: str = typer.Option(
        DEFAULT_URL,
        "--url",
        "-u",
        envvar="ARDUCAM_URL",
        help="Base URL of the arducam-ptz server.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="ARDUCAM_TOKEN",
        help="API token, when the server requires one.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses."),
    timeout: float = typer.Option(10.0, "--timeout", help="HTTP timeout in seconds."),
    version: bool = typer.Option(False, "--version", help="Print the client version and exit."),
) -> None:
    """Store global options for the command that runs next."""
    if version:
        typer.echo(f"arducam-ptz {__version__}")
        raise typer.Exit(0)
    _state.update({"url": url.rstrip("/"), "token": token, "json": json_output, "timeout": timeout})
    if ctx.invoked_subcommand is None:
        # Bare invocation: show the help that was asked for, with a success code.
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
@app.command()
def serve(
    config: Path | None = typer.Option(None, "--config", help="TOML config file."),
    host: str | None = typer.Option(None, "--host", help="Override the listen address."),
    port: int | None = typer.Option(None, "--port", help="Override the port."),
    simulate: bool = typer.Option(False, "--simulate", help="No camera, no I2C: fake hardware."),
    log_level: str | None = typer.Option(None, "--log-level", help="debug|info|warning|error"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload (development)."),
) -> None:
    """Run the server on this machine.

    Convenience for ``arducam-server`` with the same flags; on the Pi this is what
    a systemd unit calls.
    """
    from arducam_ptz.server import main as server_main

    argv: list[str] = []
    if config:
        argv += ["--config", str(config)]
    if host:
        argv += ["--host", host]
    if port:
        argv += ["--port", str(port)]
    if simulate:
        argv += ["--simulate"]
    if log_level:
        argv += ["--log-level", log_level]
    if reload:
        argv += ["--reload"]
    raise typer.Exit(server_main(argv))


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
#: Colour per health verdict: green = everything present, yellow = simulated rig,
#: red = something is missing (the UI shows the same three states).
_STATUS_COLOURS = {
    "ok": typer.colors.GREEN,
    "simulated": typer.colors.YELLOW,
    "degraded": typer.colors.RED,
}


def _render_state(state: dict[str, Any]) -> None:
    health = state["health"]
    colour = _STATUS_COLOURS.get(health["status"], typer.colors.WHITE)
    typer.secho(f"{health['status'].upper()}", fg=colour, bold=True, nl=False)
    header = f"  version {health['version']}  up {health['uptime_s']:.0f}s"
    if health.get("config_source"):
        header += f"  config {health['config_source']}"
    typer.echo(header)
    for component in health["components"]:
        mark = "ok" if component["available"] else "MISSING"
        if component["simulated"]:
            mark = "simulated"
        detail = f"  - {component['detail']}" if component.get("detail") else ""
        typer.echo(f"  {component['name']:<9} {mark}{detail}")
    ptz_state = state["ptz"]
    pan, tilt = ptz_state["pan"], ptz_state["tilt"]
    typer.echo(
        f"  pan {pan['angle']:.1f}° ({pan['min']:.0f}-{pan['max']:.0f})"
        f"   tilt {tilt['angle']:.1f}° ({tilt['min']:.0f}-{tilt['max']:.0f})"
        + ("  [released]" if pan["released"] else "")
    )
    focus_state = state["focus"]
    position = "?" if focus_state["position"] is None else f"{focus_state['position']}"
    typer.echo(f"  focus {position} ({focus_state['min']}-{focus_state['max']})")
    if focus_state.get("autofocus"):
        job = focus_state["autofocus"]
        phase = f" ({job['phase']})" if job.get("phase") else ""
        typer.echo(f"  autofocus {job['status']} {job['progress'] * 100:.0f}%{phase}")
    stream = state["stream"]
    typer.echo(
        f"  stream {stream['width']}x{stream['height']} {stream['fps']} fps "
        f"{stream['kilobytes_per_s'] / 128:.1f} Mbit/s, {stream['subscribers']} viewer(s)"
    )


@app.command()
def status() -> None:
    """Show health, pan/tilt, focus and stream statistics."""
    _out(_api("GET", "/api/v1/state"), _render_state)


@app.command()
def health() -> None:
    """Show only the component availability verdict."""
    _out(
        _api("GET", "/api/v1/health"),
        lambda data: typer.echo(
            "\n".join(
                f"{data['status']:<10} {component['name']:<9} "
                f"{'ok' if component['available'] else 'MISSING'}"
                + (f"  {component['detail']}" if component.get("detail") else "")
                for component in data["components"]
            )
        ),
    )


# --------------------------------------------------------------------------- #
# pan/tilt
# --------------------------------------------------------------------------- #
def _render_ptz(state: dict[str, Any]) -> None:
    typer.echo(
        f"pan {state['pan']['angle']:.1f}°  tilt {state['tilt']['angle']:.1f}°"
        + ("  [released]" if state["pan"]["released"] else "")
        + _clamp_note(state)
    )


@app.command()
def move(
    pan: float | None = typer.Option(None, "--pan", help="Absolute pan angle in degrees."),
    tilt: float | None = typer.Option(None, "--tilt", help="Absolute tilt angle in degrees."),
) -> None:
    """Move to absolute angles (omitted axes do not move)."""
    if pan is None and tilt is None:
        _fail("give --pan and/or --tilt")
    _out(_api("POST", "/api/v1/ptz/move", {"pan": pan, "tilt": tilt}), _render_ptz)


@app.command()
def nudge(
    pan: float = typer.Option(0.0, "--pan", help="Relative pan change in degrees."),
    tilt: float = typer.Option(0.0, "--tilt", help="Relative tilt change in degrees."),
) -> None:
    """Move relative to the current position."""
    _out(_api("POST", "/api/v1/ptz/nudge", {"pan": pan, "tilt": tilt}), _render_ptz)


@app.command()
def center() -> None:
    """Return to the configured centre position."""
    _out(_api("POST", "/api/v1/ptz/center"), _render_ptz)


@app.command()
def release() -> None:
    """Cut servo pulses so both axes go limp."""
    _out(_api("POST", "/api/v1/ptz/release"), _render_ptz)


# --------------------------------------------------------------------------- #
# focus
# --------------------------------------------------------------------------- #
def _render_focus(state: dict[str, Any]) -> None:
    detail = f"  {state['detail']}" if state.get("detail") else ""
    typer.echo(f"focus position {state['position']} of {state['min']}-{state['max']}{detail}")


@app.command()
def focus(position: int = typer.Argument(..., help="Absolute motor position.")) -> None:
    """Set the lens position (cancels a running autofocus)."""
    _out(_api("POST", "/api/v1/focus", {"position": position}), _render_focus)


@app.command("focus-nudge")
def focus_nudge(steps: int = typer.Argument(..., help="Signed motor steps.")) -> None:
    """Step the lens by a relative number of motor steps."""
    _out(_api("POST", "/api/v1/focus/nudge", {"steps": steps}), _render_focus)


@app.command()
def autofocus(
    wait: bool = typer.Option(False, "--wait", help="Block until the sweep finishes."),
    region: str | None = typer.Option(
        None,
        "--region",
        help="Focus window as x,y,w,h in 0-1 coordinates (default: a centred window).",
    ),
    cancel: bool = typer.Option(False, "--cancel", help="Cancel a running sweep and exit."),
) -> None:
    """Run a contrast-detect autofocus sweep (background job; poll with --wait)."""
    if cancel:
        _cancel_autofocus()
        return
    payload: dict[str, Any] = {}
    if region:
        try:
            x, y, width, height = (float(part) for part in region.split(","))
        except ValueError:
            _fail("--region expects four comma-separated numbers: x,y,w,h")
            return
        payload["region"] = {"x": x, "y": y, "width": width, "height": height}

    job = _api("POST", "/api/v1/focus/autofocus", payload, expect=202)
    if _state["json"] and not wait:
        typer.echo(json_module.dumps(job, indent=2))
        return
    typer.echo(f"autofocus job {job['id']} started")
    if wait:
        _wait_for_job(job)


def _cancel_autofocus() -> None:
    """Cancel the newest running sweep, if the server reports one."""
    state = _api("GET", "/api/v1/focus")
    job = state.get("autofocus")
    if not job or job["status"] in {"completed", "failed", "cancelled"}:
        typer.echo("no autofocus sweep is running")
        return
    cancelled = _api("POST", f"/api/v1/focus/autofocus/{job['id']}/cancel")
    _out(cancelled, _render_job_line)


def _wait_for_job(job: dict[str, Any]) -> None:
    """Poll a job at 4 Hz, printing progress, until it reaches a terminal state."""
    last_line = ""
    while True:
        current = _api("GET", f"/api/v1/focus/autofocus/{job['id']}")
        if _state["json"]:
            typer.echo(json_module.dumps(current, default=str))
        else:
            line = f"  {current['progress'] * 100:5.0f}%  {current['phase'] or current['status']}"
            if line != last_line:
                typer.echo(line)
                last_line = line
        if current["status"] in {"completed", "failed", "cancelled"}:
            _report_job(current)
            return
        time.sleep(0.25)


def _render_job_line(job: dict[str, Any]) -> None:
    typer.echo(f"job {job['id']}: {job['status']}")


def _report_job(job: dict[str, Any]) -> None:
    if job["status"] == "completed":
        result = job.get("result") or {}
        position = result.get("position")
        score = result.get("score", 0.0)
        elapsed = result.get("elapsed_s")
        typer.secho(
            f"focused at position {position} (score {score:.1f}, {elapsed}s)",
            fg=typer.colors.GREEN,
        )
    elif job["status"] == "failed":
        _fail(f"autofocus failed: {job.get('error')}")
    else:
        typer.secho("autofocus cancelled", fg=typer.colors.YELLOW)


# --------------------------------------------------------------------------- #
# captures
# --------------------------------------------------------------------------- #
def _render_photo(photo: dict[str, Any]) -> None:
    size = photo.get("bytes", 0) / 1024
    dims = f"{photo.get('width')}x{photo.get('height')}" if photo.get("width") else "?"
    typer.echo(f"{photo['filename']}  {dims}  {size:.0f} KiB  {photo.get('created_at')}")


def _render_photos(data: list[dict[str, Any]]) -> None:
    if not data:
        typer.echo("no photos yet")
        return
    for photo in data:
        _render_photo(photo)


@app.command()
def capture(
    download: Path | None = typer.Option(None, "--download", help="Also save the photo to this local path."),
) -> None:
    """Take a full-resolution photo on the device."""
    photo = _api("POST", "/api/v1/captures", expect=201)
    if _state["json"]:
        typer.echo(json_module.dumps(photo, indent=2, default=str))
    else:
        _render_photo(photo)
    if download:
        _fetch_file(photo["url"], download)
        typer.echo(f"saved to {download}")


@app.command()
def photos(limit: int = typer.Option(10, "--limit", help="How many to list.")) -> None:
    """List photos stored on the device, newest first."""
    _out(
        _api("GET", f"/api/v1/captures?limit={limit}"),
        _render_photos,
    )


@app.command("get-photo")
def get_photo(
    filename: str = typer.Argument(..., help="Photo name from `arducam photos`."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Local path (default: the same name)."),
    max_dim: int | None = typer.Option(None, "--max-dim", help="Download a downscaled version."),
) -> None:
    """Download a photo to this machine."""
    query = f"?max_dim={max_dim}" if max_dim else ""
    target = output or Path(filename)
    _fetch_file(f"/api/v1/captures/{filename}{query}", target)
    typer.echo(f"saved to {target}")


@app.command("delete-photo")
def delete_photo(
    filename: str = typer.Argument(..., help="Photo name from `arducam photos`."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Delete a photo from the device."""
    if not yes and not typer.confirm(f"delete {filename} on the device?"):
        raise typer.Exit(1)
    _api("DELETE", f"/api/v1/captures/{filename}", expect=204)
    typer.echo(f"deleted {filename}")


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #
@app.command()
def stream(
    seconds: float = typer.Option(5.0, "--seconds", "-s", help="How long to record (0 = until interrupted)."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write the MJPEG stream here; omit for stdout."
    ),
) -> None:
    """Save or pipe the raw MJPEG preview stream.

    Prints frame statistics at the end instead of the frames themselves when writing
    to a file, so it doubles as a bandwidth probe for a phone on the same network.
    """
    deadline = None if seconds == 0 else time.monotonic() + seconds
    started_at = time.monotonic()
    frames = 0
    total = 0
    sink = output.open("wb") if output else sys.stdout.buffer
    try:
        headers = {"X-API-Token": _state["token"]} if _state["token"] else {}
        client = httpx.Client(base_url=_state["url"], headers=headers, timeout=None)
        with client, client.stream("GET", "/api/v1/stream.mjpg") as response:
            if response.status_code >= 300:
                _fail(f"HTTP {response.status_code} opening the stream")
            buffer = bytearray()
            for chunk in response.iter_bytes():
                buffer.extend(chunk)
                while True:
                    frame = _extract_frame(buffer)
                    if frame is None:
                        break
                    frames += 1
                    total += len(frame)
                    if sink is not sys.stdout.buffer:
                        sink.write(frame)
                    elif frames == 1:  # first frame only when piping
                        sink.write(frame)
                        sink.flush()
                if deadline and time.monotonic() >= deadline:
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    finally:
        if output:
            sink.close()
    elapsed = max(1e-6, time.monotonic() - started_at)
    typer.echo(
        f"{frames} frames, {total / 1024:.0f} KiB in {elapsed:.1f}s "
        f"({frames / elapsed:.1f} fps, {total * 8 / elapsed / 1e6:.2f} Mbit/s)",
        err=True,
    )


def _extract_frame(buffer: bytearray) -> bytes | None:
    """Pull one complete JPEG out of a growing MJPEG buffer, or None to wait for more."""
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        buffer.clear()
        return None
    end = buffer.find(b"\xff\xd9", start + 2)
    if end < 0:
        del buffer[:start]
        return None
    frame = bytes(buffer[start : end + 2])
    del buffer[: end + 2]
    return frame


def _fetch_file(path: str, target: Path) -> None:
    """Download a binary resource (photo) to ``target``."""
    headers = {"X-API-Token": _state["token"]} if _state["token"] else {}
    try:
        with httpx.Client(base_url=_state["url"], headers=headers, timeout=60.0) as client:
            response = client.get(path)
    except httpx.HTTPError as exc:
        _fail(f"cannot reach {_state['url']} ({exc})")
        return
    if response.status_code >= 300:
        _fail(f"HTTP {response.status_code} fetching {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``arducam`` console script."""
    try:
        app(args=argv)
    except typer.Exit as exit_:  # typer already answered the user
        return int(exit_.exit_code or 0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
