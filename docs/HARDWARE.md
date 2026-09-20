# The rig, and how not to make it shake

Everything here was learned on the actual hardware, most of it the hard way — the camera
on this platform has a history of thrashing violently on start-up, and every paragraph
below exists because something shook.

## What is wired to what

| Part | Where | Notes |
| --- | --- | --- |
| Raspberry Pi 5 | — | the whole build runs here, no separate controller |
| Arducam 16 MP (IMX519) | CSI, `i2c@80000` (rp1) | sensor is 4656x3496; preview 960x540 |
| PCA9685 PWM driver | **i2c-1, address 0x40** | 16 channels, only two used |
| pan servo | PCA9685 **channel 1** | `pan_channel` |
| tilt servo | PCA9685 **channel 0** | `tilt_channel` |
| focus motor (VCM) | `/dev/v4l-subdev3` | V4L2 `focus_absolute`, range 0..4095, step 1 |

The two i2c buses in that table are different buses on the same Pi. That matters, because
the camera pipeline and the servo driver both keep the SoC busy, and the original project
found that they interfere with each other (see "The 5 ms gap" below).

Servo mapping is linear in the *nominal* domain: `0 deg -> min_pulse_us`,
`180 deg -> max_pulse_us`, defaults 500 us and 2500 us. Mechanical travel is not the full
range — the bracket hits its hard stops outside `pan 60..120`, `tilt 15..145`, so those
limits are configuration, not suggestions.

## The PWM clock: prescale 119, and why it is not 101

The PCA9685 counts 4096 ticks per period off a nominal 25 MHz internal oscillator, so

    prescale = round(25e6 / (4096 * frequency)) - 1

For a 60 Hz request that gives **101**. This board runs at **119**, and that difference is
the single most load-bearing number in the whole project:

    frequency_requested = 60 Hz
    pwm_frequency_correction = 0.8449        # measured on this board, not a preference
    prescale = round(25e6 / (4096 * 60 * 0.8449)) - 1 = 119

The old app arrived at 119 through `set_frequency()`, which multiplies the requested
frequency by **0.8449** before deriving the prescale. That constant carries no comment in
the original source, so where it came from is lost to history — but it is a measured
property of *this board*, and it is the difference between a rig that sits still and one
that does not. What matters operationally is that 119 is the value this rig has always run
at, and
`tests/test_hardware.py::test_prescale_matches_the_rig_the_hardware_was_calibrated_on`
asserts both 119 and the 101 a datasheet-nominal oscillator would give, so nobody can
"correct" it back by accident. Do not carry 0.8449 to a different board without measuring
it first.

Two rules follow from that, and both are easy to break:

1. **The correction is applied to the prescale, and only to the prescale.** The
   angle-to-tick arithmetic stays in the nominal 60 Hz domain the travel limits were
   measured in. Correcting both cancels out into a wrong pulse.
2. **Do not change `pwm_frequency_hz` to the 50 Hz "hobby standard".** It feeds the
   pulse mapping, so changing it rescales every commanded angle by that ratio (50/60 =
   17%). The old project ran 60 Hz on this rig and the limits were measured there.

Verify it at any time without trusting the app:

    uv run python scripts/check_servos.py      # prints PRESCALE and every channel's state

## Start-up: the rig is limp until you ask it to move

This is the fix for the shake-at-startup. Three things happen in order, and the order is
the point:

1. **Every channel is switched off first** (`LEDn_OFF_H` set), before anything else is
   programmed.
2. **A 200 ms quiet window** follows, so the servos are physically coasting while the
   chip is reconfigured.
3. **The frequency is programmed** (which requires the chip asleep, hence the ordering),
   **then a second quiet window**, and only then is the chip left alone.

The PCA9685 powers up with a ~200 Hz prescale and undefined LED registers, so a servo
addressed in the first milliseconds after reset receives garbage — a pulse train with a
period the servo cannot interpret. The original project found this the hard way, and left
the finding in the comment on its `_init_servo()`: it holds every channel off "so the
PCA9685 does not emit stray startup pulses that make the pan servo thrash side to side",
and logs "pan/tilt will engage on first command" while doing it.

So the service layer also starts in the **released** state: nothing is driven until the
first move command arrives, and the first command re-arms the axes. A restart therefore
leaves the camera where it was pointing, with no torque and no motion — verified on
hardware with `scripts/check_servos.py` immediately after a restart, which reports all 16
channels off.

## The 5 ms gap: the camera and the servos share this Pi

The original project traced "servos thrash *while using the app*" — not on start-up, but
during normal operation — to I2C contention between picamera2's frame pipeline and
PCA9685 writes, and fixed it by leaving a gap after every frame. The same code with no gap
at all is *fine on an idle bench*, which is what makes this failure so unpleasant: it only
appears once the camera is streaming.

`i2c_settle_ms` (default 5.0) is that gap, applied after each servo write. It costs at
most `write_rate_hz * i2c_settle_ms` of the writer thread per second — 20 Hz x 5 ms = 1%
— and it is the difference between a rig that is stable under load and one that twitches
when someone opens the preview page. Set it to 0 only if the servos are on their own
controller.

If the rig ever does shake again, the first thing to check is that the inspected state
matches what the code believes: `scripts/check_servos.py` reads the chip registers
directly, not through this project's driver.

## Panic procedure

Order matters: cut the pulses first, then investigate.

| Step | Command | What it does |
| --- | --- | --- |
| 1 | press **E** in the web UI | `POST /api/v1/ptz/release` — both axes limp, one keystroke |
| 1 | `uv run arducam release` | the same thing from a shell |
| 2 | `uv run python scripts/check_servos.py` | reads the chip directly; prints `RESULT: LIMP` or `RESULT: ARMED` and the prescale |
| 3 | `uv run arducam status` | what the service *thinks*: angles, `released`, health of each component |

**Killing the server does not release the servos.** The PCA9685 keeps emitting whatever
was last programmed into it, so a killed server leaves an armed rig holding position (and
humming). Always `arducam release` before stopping the server, or let it shut down
normally — the shutdown path releases the axes.

## Environment conflicts seen on this machine

- **`wireplumber` holds `/dev/v4l-subdev3`.** Observed with `fuser`. A desktop audio
  session manager has no business on a camera subdevice, and it is a second process
  talking to the same hardware this project drives; it is the first suspect if image
  capture misbehaves in ways the API does not explain. Stopping the PipeWire user unit for
  the session is the fix.
- **Only one process may hold `/dev/i2c-1`.** While the old Flask app and this server were
  both running, they fought over the same PCA9685 channels — two writers alternating
  pulses on one servo is a shake that no amount of tuning will cure. Check with
  `sudo fuser -v /dev/i2c-1` (expect exactly one `arducam` process) before blaming the
  code.

## Boot log: what a healthy start looks like

```
arducam_ptz.server: serving on http://0.0.0.0:8080 (simulated=False)
services.context: PCA9685 at 0x40 on i2c-1 configured for 60.0 Hz (prescale 119)
services.context: focus motor on /dev/v4l-subdev3: focus_absolute range 0..4095 step 1
hardware: pan/tilt: PCA9685 connected
services.context: camera ready: imx519, sensor 4656x3496
services.context: preview streaming at 960x540 q75
services.context: arducam-ptz 0.1.0 ready (config: built-in defaults)
```

Anything saying `retrying PCA9685 initialisation` means the PTZ service hit a bus error
mid-request and re-opened the chip — that message is now reserved for genuine recovery, so
seeing it on a *clean* boot was a bug (fixed: the builder called `reopen()` where it meant
`open()`), and seeing it while running means the bus is being disturbed by something.
