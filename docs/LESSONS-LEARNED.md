# Lessons learned

Written the way it was learned: a fault, the evidence, what this code does instead, and
how you can check that it still does. Faults found in *this* codebase during testing are
here too — that seemed more useful than pretending the rewrite arrived perfect.

The reference implementation is `../arducam-web-control` (kept untouched on purpose, as
the specification of what the hardware actually does).

---

## 1. Start-up garbage: the camera shook itself apart

**Evidence.** Two independent records in the old project. Its `_init_servo()` comment says
it holds all outputs off briefly "so the PCA9685 does not emit stray startup pulses that
make the pan servo thrash side to side" and logs "pan/tilt will engage on first command";
`PAN_TILT_FIX_SUMMARY.md` documents the delay order it settled on (reset the prescale
first, 200 ms, disconnect every servo, 200 ms, then connect the axes gradually). The
PCA9685 comes out of reset with a ~200 Hz prescale and undefined LED output registers, so
a servo addressed in those first milliseconds gets a pulse train its control loop cannot
interpret.

**This code.** `Pca9685._initialise` switches every channel off *first*, waits
`QUIET_WINDOW_S` (200 ms), then programs the frequency (which requires the chip asleep,
hence the order), waits again, and leaves the chip alone. `PanTiltService` additionally
starts with `_released = True`, so nothing is driven until the first move command — a
restart cannot set the rig moving, and it still remembers where it was pointing.

**Verify.** `uv run python scripts/check_servos.py` right after a restart, or
`grep prescale` the boot log for `prescale 119`. All 16 channels read `full off`.

## 2. The magic number that decides whether it shakes: prescale 119

**Evidence.** The old app's `set_frequency()` multiplied the requested frequency by
`0.8449` before computing the prescale, landing on 119; the datasheet arithmetic on a
nominal 25 MHz oscillator gives 101 for the same 60 Hz request. The constant appears once,
uncommented, in the whole original project — so the honest position is that it is a
correction for *something* on this board and nobody recorded what. Treat it as a property
of this rig, not a truth about PCA9685s, and measure any other board before copying it.

**This code.** Reproduces the old arithmetic exactly — `pwm_frequency_hz = 60.0`,
`pwm_frequency_correction = 0.8449`, correction applied to the *prescale only* while the
pulse arithmetic stays in the nominal domain the travel limits were measured in — and
pins the result in a test so it cannot be "corrected" to the datasheet value later.

**Verify.** `tests/test_hardware.py::test_prescale_matches_the_rig_the_hardware_was_calibrated_on`
asserts 119 *and* 101, so the test explains the difference rather than just freezing a
number.

## 3. The rig thrashed *during use*, not just at start-up

**Evidence.** The old project traced servo misbehaviour while streaming to I2C contention
between picamera2's frame pipeline and PCA9685 writes, and fixed it with a gap after every
frame. Reproducing it on an idle bench shows nothing, which is what makes it nasty: it only
appears once the camera is running.

**This code.** `PanTiltSettings.i2c_settle_ms` (default 5 ms) is that gap, applied by
`ServoRig.set_angles` after each write. 20 Hz x 5 ms = 1% of the writer thread.

**Verify.** `arducam status` while a preview is open: the angles should follow commands with
no oscillation, and the log should stay free of `retrying PCA9685 initialisation`.

## 4. "Emergency stop" that moved the machine

**Evidence.** The old UI's 🚨 EMERGENCY STOP button (and the `e` key) posted to
`/api/servo/emergency-reset`, whose handler called `servo.reset()` — centre both axes. On a
rig that is already shaking, the panic button *drove the servos*.

**This code.** `POST /api/v1/ptz/release` (`arducam release`, and the `E` key) cuts the
pulse train: torque goes to zero immediately and the camera stays where it is.

**Verify.** `scripts/check_servos.py` after pressing `E` says `RESULT: LIMP`.

## 5. Two writers on one PCA9685

**Evidence.** The old Flask app and this server were both running against the same board at
one point. Two processes alternating pulses on one servo produce a shake that no amount of
tuning fixes, and it looks exactly like a software bug.

**This code.** Nothing special — but the check is in `docs/HARDWARE.md`:
`sudo fuser -v /dev/i2c-1` must show exactly one `arducam` process.

## 6. `hidden` loses to `display` (found here, live)

**Evidence.** `.stream-overlay { display: grid }` and `.drawer { display: flex }` are author
rules; the `hidden` attribute is only `display: none` in the *user-agent* stylesheet, so the
JS toggling `.hidden` was a no-op. Result: a perfectly working camera image stayed hidden
under a permanent "stream unavailable / Reconnect" overlay, and the help drawer could be
opened but never closed.

**This code.** One `[hidden] { display: none !important; }` rule near the top of `app.css`,
which fixes the overlay, the drawer, the scrim, the gallery and the autofocus progress bar
at once.

**Verify.** `scripts/ui_probe.py` asserts `overlay.display == "none"` *and* that
`document.elementFromPoint()` at the centre of the image returns the image itself — reading
the CSS would have said "that should work".

## 7. Steering that steers the wrong way (found here, live)

**Evidence.** Pressing ArrowUp on a live page moved tilt from 92.2 to **94.4** — and tilt
increases downwards. The pad had the same inversion, and it is invisible to any test that
merely asserts *that* something moved: the pad feels plausible in the hand, and a camera you
cannot see moving cannot tell you it went the wrong way.

**This code.** The direction convention is taken from the app the hardware was calibrated
against (there, ▲ = `tilt-1`, and the toast read "Tilt down" for a positive delta), and the
translation lives in exactly one place — `sendNudge()` in `app.js`, which negates the tilt
term and passes pan through.

**Verify.** `scripts/ui_probe.py` presses ArrowRight and ArrowUp and prints the resulting
angles; the expected signs are in `docs/UI.md`.

## 8. Keys that were advertised but never handled

**Evidence.** The help panel listed arrow keys; the `keydown` switch handled `-`, `+`, `f`,
space, `c`, `g`, `?` and Escape, and had no `Arrow*` cases at all.

**This code.** Arrows are handled before the switch and share the pad's code path, so the
keyboard and the joystick cannot drift apart. They also keep working while a button has
focus (after clicking *help*), because the guard only skips text fields and the focus
slider — where the arrows are the browser's own and finer than anything we could fake.

**Verify.** `scripts/ui_probe.py` dispatches real key events and reads `/api/v1/state`.

## 9. The hardware layer welded to one HTML page

**Evidence.** The old app's servo, focus and camera code lived in the same Flask file as the
one page that drove it; the page was the only client, so nothing else could use the camera.

**This code.** `hardware/` (backends, all simulatable) → `services/` (state, policy, frame
fan-out, jobs) → `api/` (the `/api/v1` contract) → clients: the browser page, the `arducam`
CLI, or anything else. The UI reads every limit and angle from `/api/v1/state` and knows no
hardware facts; `/docs` is the contract, generated from the code.

**Verify.** `uv run arducam --simulate status` works with no camera and no browser; the CLI
is an HTTP client and does not import `hardware/`.

## 10. A preview that cannot be used on a phone

**Evidence.** The old page had the image and the controls competing for one small screen,
with no touch handling that survived a drag (`touch-action` was never set, so dragging
scrolled or zoomed and selected the readouts instead of moving the camera).

**This code.** A single-viewport grid: the picture keeps the top, the controls take the rest,
nothing scrolls off. The pad is pointer-event driven with `touch-action: none` and
`user-select: none`.

**Verify.** `scripts/ui_probe.py 8099 390 844 mobile` reports `controlsOffscreen: []`,
`scrollW == viewport.w`, and `padTouchAction: "none"`.
