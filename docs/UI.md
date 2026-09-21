# The browser UI

A control surface that is *only* a client of the HTTP API. It is served from
`src/arducam_ptz/web/static/`, it is plain HTML, CSS and ES modules with no build step and
no framework, and it knows nothing about servos, I2C or picamera2 — every limit, angle,
state and capability it displays it reads from `/api/v1/state`. That is the whole reason
the CLI, an iPhone app and this page can all be clients at once, and why changing the
backend does not mean touching the UI.

## Files

| File | Responsibility |
| --- | --- |
| `index.html` | the whole DOM: header/HUD, stage with the MJPEG `<img>` and its overlay, sidebar cards, photo gallery drawer, help drawer |
| `app.css` | the entire theme, all layout, and the two rule-level traps described below |
| `js/api.js` | every HTTP call in one place: typed-ish wrappers over `/api/v1/*`, error objects, base URL derivation |
| `js/pad.js` | the joystick widget. Pure input: pointer events in, a `{x, y}` vector in unit-disc coordinates out. No knowledge of the API |
| `js/app.js` | wiring: state polling, stream lifecycle, HUD rendering, keyboard, gestures, and the single translation from steering vectors into servo deltas |

## Layout

A desktop layout collapses to a phone layout with no separate template: the stage keeps
its aspect ratio and the sidebar wraps under it. Verified in a real browser at **390x844**
and **1440x900**, where the assertions that matter are that no control is off-screen
(`controlsOffscreen: []`), the page does not scroll horizontally
(`scrollW == viewport.w`), and the pad keeps `touch-action: none` so dragging it does not
scroll or zoom the page on a phone.

## Interaction model

| Input | Effect |
| --- | --- |
| drag the pad | proportional nudge while held; the newest vector wins, intermediate ones are dropped |
| arrow keys | one full pad tick per press — same code path, so keyboard and joystick cannot disagree |
| `-` / `+` | step the focus position |
| `f` | autofocus |
| space | shutter |
| `c` | centre both axes |
| `g` | photo gallery |
| `?` | help drawer |
| `E` | **emergency stop** — cuts servo pulses so the rig goes limp |
| Escape | close whatever drawer is open |
| tap the image | move the autofocus window there, when the AF toggle is on |

A tap posts a normalised window to `/api/v1/focus/autofocus`. The object built by
`regionFromTap()` is sent verbatim, so **its field names are the API's**:
`{x, y, width, height}`, exactly `models.Region`. A shorter local spelling (`w`/`h`) is not
a harmless shorthand — the request fails validation and the sweep never starts, so the tap
appears to do nothing but flash an error. Two guards now exist: `tests/test_api.py` posts
the shape the page builds, and `scripts/ui_probe.py` taps the image for real and then asks
the server whether a sweep ran.

Validation errors read differently from the API's own errors, too: FastAPI reports them as
a list under `detail`, not this project's `error` envelope. `api.js` formats that list into
`region.width: Field required`; without it the page shows the raw JSON body, which is how a
field-name typo comes to look like a network fault.

Arrow keys also work while a button has focus (after clicking *help*, holding an arrow
should still steer). Only the space bar defers to a focused button, because that is how
buttons are pressed from a keyboard — and the handler ignores everything while a text
field or the focus slider has focus, since on a range input the arrows are the browser's
own and finer than anything we could synthesise.

## Direction conventions — the bug that tests cannot see

Steering vectors are screen-space: `x` is pan, and `+y` is **up on the screen** (the pad
paints an upward knob travel for it). Servo deltas are not:

- **pan increases to the right** → `+x` passes through unchanged
- **tilt increases downwards** → `+y` must be **negated**

Both come from the app this hardware was calibrated against: there, ▲ was `tilt-1`, ▼ was
`tilt+1`, and the toast read "Tilt down" for a positive delta. `sendNudge()` in `app.js`
is the single place that translation happens, and it is the only place it should ever
happen.

Why this is worth a comment: with the sign flipped, a test that only asserts *that
something moved* passes happily, the pad feels fine in the hand for small corrections, and
the camera still refuses to go the way you push it. It was caught only by pressing ArrowUp
against a live page and reading the resulting angle (`tilt_before: 92.2, tilt_after: 90`,
i.e. up).

## The photo gallery and the viewer

`g` (or the Photos button) opens the grid. `loadGallery()` fills it from
`GET /api/v1/captures` and keeps that listing in `galleryPhotos`, so the viewer can step
through photos without asking the server again.

Tiles carry nothing on top of the picture. They used to have download and delete buttons
absolutely positioned over them, and at the old column widths - 96 px in the base rule,
120 px in the desktop media query - a tile on a *large* screen held 123x92 px of picture
with two 28 px buttons sitting on it, so the buttons were most of what the gallery showed.
That desktop override is the trap worth remembering: the 420 px panel is narrower than a
phone is wide, so a smaller minimum there produced smaller tiles on the big screen than on
the phone.

Tapping a tile opens `#viewer`: the photo at `?max_dim=1600` (the original is a 16 MP JPEG
of about a megabyte, and it is one tap away under "Original" and "Download"), filling the
space between two bars, with the actions in the bottom bar where they cover nothing.

While the viewer is open it owns the keyboard - Escape closes it, left/right step through
photos - and that branch sits *above* the steering branch in the key handler on purpose:
below it the arrows move the camera, and paging through photos must not drive the rig. The
probe checks exactly that.

Verify: `uv run python scripts/ui_probe.py 8099 390 844 mobile` reports a tile of about
176x132, `buttonsOnTiles: 0`, a viewer image filling ~85% of the viewport height, and an
unchanged pan angle across an ArrowRight press.

## The stream

`GET /api/v1/stream.mjpg` is `multipart/x-mixed-replace; boundary=arducamframe`, each part
carrying a `Content-Length` so the browser does not have to guess at boundaries. Measured
on hardware: 960x540, q75, **150 frames in 5 s (30 fps)**, ~4.1 MB per 5 s window.

Three consequences for anyone touching this code:

- The boundary in the response header and the boundary in the body must match exactly.
  `curl` does not care; a browser does, and when it does not match you get a blank frame
  and no error anywhere.
- An MJPEG `<img>` never "finishes loading", so do not gate the overlay on a `load` event
  that will never be the last one. The UI hides the overlay when it sets the stream `src`
  and shows it again on `error`.
- Liveness is decided by the **server**: `/api/v1/state` reports the age of the newest
  published frame, and the UI's watchdog trips on that number, not on anything it counts
  itself. One source of truth, so a stalled camera and a stalled network look the same to
  the client — which is what the user actually needs to know.

## Two traps in the CSS, both of which cost a live image

**1. `hidden` loses to any author `display` rule.** The `hidden` attribute is implemented
in the *user-agent* stylesheet as `[hidden] { display: none }` — the weakest origin in the
cascade. This UI toggles panels by setting `.hidden`, and `.stream-overlay` is
`display: grid` while `.drawer` is `display: flex`, so both overrode it. The symptoms were
maddening to read about but trivial once seen: a **live, perfectly working camera image
stayed covered by the "stream unavailable / Reconnect" overlay**, and the help drawer
opened but would not close, because the ✕ really was setting `hidden = true` — to no
effect. The fix is one rule near the top of `app.css`:

```css
[hidden] {
  display: none !important;
}
```

**2. Never diagnose this class of bug from the source.** The overlay, the drawer, the
scrim, the gallery and the autofocus progress bar are all toggled the same way, so the one
rule fixes all of them at once; and `getComputedStyle(el).display` plus
`document.elementFromPoint(centre of the image)` answer "is something covering the camera?"
in one call, where reading the JS answers "that should work".

## Verifying it — the only way that counts

The project's test suite can check that assets are served and that the keys it lists are
handled, but it cannot tell you whether the image renders, whether a control is off the
bottom of a phone screen, or whether a drawer closes. Those need a layout engine, so
`scripts/ui_probe.py` drives headless Chromium over the DevTools protocol and prints one
JSON object of measurements:

    uv run arducam serve --simulate --port 8099 &      # no hardware needed for a UI check
    uv run --with websockets python scripts/ui_probe.py 8099 390 844 mobile shot
    uv run --with websockets python scripts/ui_probe.py 8099 1440 900 desktop shot

It clicks the help button and re-reads the drawer, presses ArrowRight/ArrowUp and reads
the resulting angles from `/api/v1/state`, presses `E` and checks the axes are released,
collects console errors, and optionally writes a PNG screenshot named after the viewport.

A passing desktop run (1440x900, abridged — this is real output, not an illustration):

```json
{"viewport": "1440x900",
 "probe": {"page":   {"scrollW": 1440, "scrollH": 900},
           "overlay": {"hidden": true, "display": "none"},
           "image":   {"naturalW": 640, "naturalH": 360, "complete": true},
           "imageVisible": "stream",        // elementFromPoint at the image centre IS the image
           "padTouchAction": "none",
           "controlsOnScreen": 12, "controlsOffscreen": [],
           "streamMeta": "12.0 fps \u00b7 1 viewer"},
 "help_after_open":  {"hidden": false, "display": "flex"},
 "help_after_close": {"hidden": true,  "display": "none"},
 "console": []}
```

and the same probe at 390x844, which is where layout bugs live:

```json
{"viewport": "390x844", "mobile": true,
 "probe": {"overlay": {"hidden": true, "display": "none"},
           "imageVisible": "stream",
           "stage": {"h": 510},            // picture keeps the top, nothing scrolls off
           "pad": {"x": 21, "y": 607, "w": 156, "h": 156},
           "controlsOffscreen": []},
 "arrow_right": {"pan_before": 90.0, "pan_after": 92.2},   // pan+ = right
 "arrow_up":    {"tilt_before": 90.0, "tilt_after": 87.8}, // tilt- = up (up on screen)
 "after_E":     {"released": true},
 "screenshot": "/…/ui_shot_390x844.png",
 "console": []}
```

The angles are the ones the *server* reports, so a probe run is evidence about the API
contract and the UI at the same time. `12.0 fps · 1 viewer` is the simulated backend's
number; on hardware that field reads ~30 fps.

Point it at a `--simulate` server for UI work: the interface is backend-agnostic, and the
simulated backend means a UI regression can never move the camera.
