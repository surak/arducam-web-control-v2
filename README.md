# arducam-ptz

Responsive web + CLI control for an **Arducam 16 MP autofocus camera (IMX519)** on an
**Arducam pan-tilt platform (PCA9685)** driven by a **Raspberry Pi 5**.

This is a ground-up rewrite of `../arducam-web-control` (kept in place, untouched).
The old app worked, but its web UI was unusable on a phone and its hardware code was
welded to that one HTML page. This one separates the two:

```
        ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
        │  web UI      │   │  arducam CLI │   │ iPhone app   │   any client
        │ (no build    │   │ (typer)      │   │ (future)     │
        │  step)       │   └──────┬───────┘   └──────┬───────┘
        └──────┬───────┘          │                  │
               └────────── HTTP + JSON + MJPEG ──────┘
                                  │
                        ┌─────────▼──────────┐
                        │  FastAPI service   │  /api/v1/...  +  /docs (OpenAPI)
                        └─────────┬──────────┘
                                  │
                        ┌─────────▼──────────┐
                        │  services          │  pan/tilt state, focus + autofocus
                        │                    │  jobs, frame fan-out, photo store
                        └─────────┬──────────┘
                                  │
                        ┌─────────▼──────────┐
                        │  hardware backends │  camera / focus motor / servos
                        │  (+ simulated set) │  -- or nothing at all in tests
                        └────────────────────┘
```

## What it gives you

* **Camera image and movement controls on screen at the same time**, on a phone and on
  a desktop. A translucent joystick sits *on* the video, so you aim while you watch;
  everything else lives below it and never covers the picture.
* **Focus without giving up the picture**: manual focus position, nudge buttons and a
  contrast-detect autofocus sweep that runs in the background (HTTP `202` + polling)
  while the preview keeps streaming.
* **Tap-to-focus**: tap a spot in the video and the autofocus sweep optimises that area.
* **16 MP stills** captured *while* the preview keeps running, plus a thumbnail gallery
  that does not try to pull twelve 16 MP JPEGs down a phone connection.
* **A real API**: `/docs` is the generated OpenAPI page; `arducam --help` is the CLI that
  uses it. Nothing in the web page is special-cased in the backend.

## Quick start (Raspberry Pi 5, Raspberry Pi OS)

```bash
# libcamera/picamera2 come from Debian, not PyPI - see docs/HARDWARE.md
sudo apt-get install -y python3-picamera2 v4l-utils

cd ~/Devel/arducam-web-control-v2
uv venv --system-site-packages   # --system-site-packages: picamera2 lives in /usr/lib
uv sync
cp config/arducam.example.toml arducam.toml   # optional: your site settings

uv run arducam-server            # http://<pi>:8080/  (UI)   /docs (API)
uv run arducam status            # same machine, from the terminal
```

Open `http://<pi-address>:8080/` on your phone or laptop.

## Documentation

| document | what is in it |
| --- | --- |
| `docs/ARCHITECTURE.md` | layers, threading model, why each decision was made |
| `docs/API.md` | every endpoint with curl examples |
| `docs/CONFIGURATION.md` | all config keys, environment overrides |
| `docs/HARDWARE.md` | wiring, pin/channel map, measured camera performance |
| `docs/LESSONS-LEARNED.md` | every concrete bug/design fault of the old app and how this one avoids it |

## Development

```bash
uv run pytest                     # fast: simulated hardware, no camera needed
uv run pytest -m hardware         # talks to the real camera/motor/servos
uv run ruff check .
uv run arducam-server --reload --simulate   # develop the UI with no hardware
```

## Status / known limits

* Autofocus is *contrast-detect* (sharpness sweep over `focus_absolute`) because the
  IMX519 tuning file in this libcamera build ships no autofocus algorithm - the same
  limitation the old app hit; see `docs/HARDWARE.md`.
* The API has no authentication. Keep it on a trusted network or put it behind a tunnel
  that authenticates; see `docs/ARCHITECTURE.md` ("Exposing it to the internet").
* Photo capture briefly switches sensor mode, so the preview freezes for roughly
  0.5 s per shot (measured).
