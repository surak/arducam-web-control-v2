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
  a desktop. The layout is a single-viewport grid: the picture keeps the top of the
  screen, the controls get the space under it, and neither ever pushes the other off
  screen or under a scroll. The pad uses pointer events with `touch-action: none`, so
  dragging it never scrolls the page, zooms the text or selects the readout - the three
  things the old page did instead of moving the camera.
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

## Command line

`arducam` is not a second implementation of the camera - it is an HTTP client, so it runs
on any machine that can reach the Pi (and it is the proof that the hardware layer is not
welded to the web page). Global options `--url`, `--token` and `--json` go *before* the
subcommand:

```bash
export ARDUCAM_URL=http://<pi-address>:8080

uv run arducam status                    # health, angles, focus, stream statistics
uv run arducam move --pan 20 --tilt -5   # absolute; omitted axes stay put
uv run arducam nudge --pan 3             # relative: a shell loop is a joystick
uv run arducam center
uv run arducam release                   # cut servo pulses, axes go limp
uv run arducam focus 700                 # raw lens position (0-4095)
uv run arducam focus-nudge 40
uv run arducam autofocus --wait          # sweep, then report what it chose
uv run arducam autofocus --region 0.3,0.3,0.4,0.4 --wait   # focus on that patch
uv run arducam capture --download shot.jpg
uv run arducam photos --limit 20
uv run arducam get-photo <name> -o local.jpg --max-dim 320
uv run arducam delete-photo <name> --yes
uv run arducam stream -o preview.mjpg --seconds 10         # also a bandwidth probe
uv run arducam serve --simulate --port 8099                # run a server with fake hardware
```

`--json` prints the raw payload, which is the cheap way to script the camera:

```bash
uv run arducam --json status | jq '.ptz.pan.angle'
```

### If it ever starts shaking

The rig comes up **limp** and is given pulses only when something asks it to move, so a
restart cannot set it going by itself. If it does move on its own, cut the pulses first
and investigate afterwards:

```bash
uv run arducam release                    # cut both servo pulses: the axes go limp
uv run python scripts/check_servos.py     # read the chip directly: LIMP or ARMED, + prescale
```

Pressing **E** in the web page does the same thing from the keyboard. Note that killing
the server does *not* release the servos - the PCA9685 keeps emitting whatever it was last
programmed with, so `release` (or a normal shutdown, which releases) comes first. The
background is in `docs/HARDWARE.md`.

## Documentation

| document | what is in it |
| --- | --- |
| `docs/API.md` | every endpoint with curl examples |
| `docs/CONFIGURATION.md` | all config keys, environment overrides |
| `docs/HARDWARE.md` | wiring, the PWM clock, the start-up and streaming rules that keep the servos quiet, panic procedure |
| `docs/UI.md` | the browser UI: files, interaction model, direction conventions, the CSS traps, how to verify it in a real browser |
| `docs/LESSONS-LEARNED.md` | concrete faults of the old app - and of this one, found by testing it - and what the code does instead |

## Development

```bash
uv run pytest                     # fast: simulated hardware, no camera needed
uv run pytest -m hardware         # talks to the real camera/motor/servos
uv run ruff check .
uv run arducam-server --reload --simulate   # develop the UI with no hardware

# The UI, in a real browser (layout, keys, drawers, does the image actually render).
# Point it at a --simulate server: the page is backend-agnostic, so a UI regression can
# never move the camera. See docs/UI.md for what a passing run looks like.
uv run --with websockets python scripts/ui_probe.py 8099 390 844 mobile
uv run --with websockets python scripts/ui_probe.py 8099 1440 900 desktop
```

## Status / known limits

* Autofocus is *contrast-detect* (sharpness sweep over `focus_absolute`) because the
  IMX519 tuning file in this libcamera build ships no autofocus algorithm - the same
  limitation the old app hit; see `docs/HARDWARE.md`.
* Authentication is a single optional shared token (`server.token`), off by default -
  enough to keep the camera closed on a LAN, not a user system. Keep it on a trusted
  network or behind a TLS reverse proxy otherwise; see `docs/API.md` ("Authentication").
* Photo capture briefly switches sensor mode, so the preview freezes for roughly
  0.5 s per shot (measured).
