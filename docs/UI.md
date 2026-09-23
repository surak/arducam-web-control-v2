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

A desktop layout collapses to a phone layout with no separate template. On a phone the
picture keeps the top and the controls are a full-width stack under it - the same shape
the desktop rail uses. That stack is measured, not stylistic: at the type sizes an older
reader needs (17px base here), a half-wide column cannot hold `◀ near | AF | far ▶`, a
shutter and a `Photos` button - the row wants ~227px and the column has ~152px - and the
buttons then run past the right edge of a viewport whose `overflow: hidden` keeps them
hidden for good. Every row is `flex-wrap`, so a row that outgrows its panel wraps instead
of spilling.

All type is in `rem` (1.0625rem base, nothing under 13px) and the chrome is `min-` sized
rather than fixed: an iPhone's larger-text setting inflates *text* while px-sized boxes
stay put, which is how inflated labels used to be cut off by their own buttons. With rem +
min-sized boxes the whole interface scales with the text setting instead of fighting it,
and `--muted` was lifted to a 7:1 contrast so the small print is actually readable.

Two floors keep it honest at extreme settings. The picture never drops below
`max(140px, 20dvh)` (below that the img element rounds to a 0x0 box - see the tap note),
and the controls pane (`min-height: 0` + `overflow-y: auto`) scrolls inside itself once
the picture hits its floor. So at normal text sizes nothing scrolls at all, and at very
large text every control is one swipe away while the picture stays pinned.

Verified in a real browser at **390x844** and **1440x900**, and - because "it does not
work well on my phone" meant "with my bigger fonts" - at scaled text sizes too
(`font=1.6`, `font=2.5`; see Verifying below). The assertions that matter: no control is
unreachable (`controlsOffscreen: []` - controls below the fold of the scrolling pane are
reported separately as `controlsBelowFold`, reachable by one swipe), no label is cut off
inside its box (`clippedText: []`), the page does not scroll horizontally
(`scrollW == viewport.w`), and the pad keeps `touch-action: none` so dragging it does not
scroll or zoom the page on a phone. Landscape picks up the desktop split (image left,
controls rail right) and is what to recommend for aiming with older eyes: big picture and
big controls at once.

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

The tap math also refuses a degenerate box: when the picture has no size (mid-layout, or a
box the layout squeezed to zero) `regionFromTap()` returns `null` instead of dividing by
`content.width` and posting `region.x: Input should be a valid number` - a NaN serialized
as null - which is exactly what a 2.5x text-size run produced before `.stage` had a size
floor. `tests/test_web.py` pins the floor and the guard.

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

The servo mount on this rig is mirrored relative to the reference rig, and that is fixed one
layer down, in the deployment config (`invert_tilt = true`; docs/HARDWARE.md, "Which way is
up"). The translation above is unchanged by it, and it must stay the only translation in
this file: the mirror applies to the CLI and every other client too, which it would not if
it were done here.

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
    uv run --with websockets python scripts/ui_probe.py 8099 1440 900 shot
    uv run --with websockets python scripts/ui_probe.py 8099 390 844 mobile font=1.6

It clicks the help button and re-reads the drawer, presses ArrowRight/ArrowUp and reads
the resulting angles from `/api/v1/state`, presses `E` and checks the axes are released,
taps the image and asks the server about the sweep, collects console errors, and
optionally writes a PNG screenshot named after the viewport (`shot`). `font=N` scales the
browser's standard font size before loading the page - the same lever an iPhone's
larger-text setting pulls, where text scales and px-sized boxes do not - which makes a
`font=1.6` run the regression test for the readable-at-big-text design rather than a
nicety.

A passing desktop run (1440x900, abridged — this is real output, not an illustration):

```json
{"viewport": "1440x900",
 "probe": {"bodyFontPx": "17px",
           "page":   {"scrollW": 1440, "scrollH": 900},
           "overlay": {"hidden": true, "display": "none"},
           "image":   {"naturalW": 640, "naturalH": 360,
                       "rect": {"x": 240, "y": 301, "w": 640, "h": 360}, "complete": true},
           "imageVisible": "stream",        // elementFromPoint at the image centre IS the image
           "padTouchAction": "none",
           "controlsPane": {"scrollH": 839, "clientH": 839},  // fits: nothing even scrolls
           "controlsOnScreen": 12, "controlsOffscreen": [], "controlsBelowFold": [],
           "clippedText": [],
           "streamMeta": "12.0 fps \u00b7 1 viewer"},
 "help_after_open":  {"hidden": false, "display": "flex"},
 "help_after_close": {"hidden": true,  "display": "none"},
 "console": []}
```

and the same probe at 390x844, which is where layout bugs live:

```json
{"viewport": "390x844", "mobile": true,
 "probe": {"bodyFontPx": "17px",
           "overlay": {"hidden": true, "display": "none"},
           "imageVisible": "stream",
           "stage": {"x": 0, "y": 61, "w": 390, "h": 179},   // picture keeps the top
           "pad": {"x": 117, "y": 296, "w": 156, "h": 156},
           "controlsPane": {"scrollH": 603, "clientH": 603},  // fits exactly: nothing scrolls
           "controlsOffscreen": [], "controlsBelowFold": [], "clippedText": []},
 "arrow_right": {"pan_before": 116.4, "pan_after": 118.6},   // pan+ = right
 "arrow_up":    {"tilt_before": 63.6, "tilt_after": 61.4},   // tilt- = up (up on screen)
 "after_E":     {"released": true},
 "tap_to_focus": {"toast": null, "markerShown": true},
 "console": []}
```

and the same phone with the text scaled 1.6x (`font=1.6`) - the reported condition,
"easier to use with my old-man's sight":

```json
{"viewport": "390x844", "font_scale": 1.6,
 "probe": {"bodyFontPx": "27.625px",                        // what the phone's setting does
           "stage": {"x": 0, "y": 92, "w": 390, "h": 169},  // floor holds a real picture...
           "image": {"rect": {"x": 59, "y": 100, "w": 272, "h": 153}},
           "controlsPane": {"scrollH": 719, "clientH": 582}, // ...so the pane scrolls instead
           "controlsOffscreen": [],                          // nothing unreachable...
           "controlsBelowFold":                              // ...only below the fold, one swipe
            ["btn-shutter:{"x":23,"y":886,"w":72,"h":72}",
             "btn-gallery:{"x":218,"y":898,"w":149,"h":48}"],
           "clippedText": []},
 "console": []}
```

The angles are the ones the *server* reports, so a probe run is evidence about the API
contract and the UI at the same time. `12.0 fps · 1 viewer` is the simulated backend's
number; on hardware that field reads ~30 fps.

Point it at a `--simulate` server for UI work: the interface is backend-agnostic, and the
simulated backend means a UI regression can never move the camera.
