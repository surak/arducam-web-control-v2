#!/usr/bin/env python3
"""Report whether the pan/tilt rig is armed, by reading the chip directly.

The PCA9685 cannot tell you what it is doing through a status register, but its LED
output registers say it unambiguously: each channel has LED_ON_L/H and LED_OFF_L/H, and
a channel is *fully off* when LED_OFF_H has bit 4 set and the LED_ON registers are zero.
Anything else means the channel is emitting pulses, i.e. that servo is powered and
holding torque, i.e. the rig is armed.

This reads those registers with plain I2C, deliberately not through the project's own
driver: the point is to check the hardware independently of whatever a running server
believes it did. It is the tool to reach for when the camera starts shaking - run it and
you know within a second whether something is driving the motors, and whether killing
the server actually left them limp.

Read-only: it never writes a register, so it is safe to run at any time, including
while a server is up.

Usage:
    uv run python scripts/check_servos.py            # default bus 1, address 0x40
    uv run python scripts/check_servos.py --bus 1 --address 0x40 --pan 1 --tilt 0

Exit code is 0 when every channel is off (rig limp) and 1 when any channel is driving,
so a script or a shell `if` can branch on it.
"""

from __future__ import annotations

import argparse
import sys

try:
    from smbus2 import SMBus
except ImportError:  # pragma: no cover - only on a machine without the runtime deps
    print("smbus2 is not installed: run this with `uv run python scripts/check_servos.py`")
    raise SystemExit(2) from None

#: PCA9685 register map, only what this script needs.
MODE2 = 0x01
LED0_ON_L = 0x06
PRESCALE = 0xFE

#: Bit in LEDn_OFF_H that means "off at the start of the period", i.e. no pulse.
OFF_FULL_BIT = 0x10

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report whether the PCA9685 is driving any channel (is the rig armed?)"
    )
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument("--address", type=lambda v: int(v, 0), default=0x40,
                        help="PCA9685 address (default 0x40)")
    parser.add_argument("--pan", type=int, default=None, help="pan channel, to label it")
    parser.add_argument("--tilt", type=int, default=None, help="tilt channel, to label it")
    args = parser.parse_args()

    labels = {args.pan: "pan", args.tilt: "tilt"}

    with SMBus(args.bus) as bus:
        mode2 = bus.read_byte_data(args.address, MODE2)
        prescale = bus.read_byte_data(args.address, PRESCALE)
        channels = []
        for channel in range(16):
            base = LED0_ON_L + 4 * channel
            on_l = bus.read_byte_data(args.address, base)
            on_h = bus.read_byte_data(args.address, base + 1)
            off_l = bus.read_byte_data(args.address, base + 2)
            off_h = bus.read_byte_data(args.address, base + 3)
            full_off = (off_h & OFF_FULL_BIT) != 0 and on_l == 0 and on_h == 0
            channels.append((channel, full_off, on_l, on_h, off_l, off_h))

    # 119 is the value this rig is known to be quiet and accurate at: it is what the
    # original app produced for a 60 Hz request once its inherited correction factor
    # (0.8449, provenance unknown - see docs/HARDWARE.md) is applied. That constant is
    # not a measurement of this board, so treat 119 as "the usual": any other value means
    # someone changed the frequency (or the correction), and the servos will hum, jitter
    # or run hot. Say so loudly rather than quoting a frequency, since the correction
    # makes this board's true period differ from the nominal 25 MHz the datasheet
    # arithmetic assumes.
    known_good = prescale == 119

    print(f"PCA9685 at 0x{args.address:02x} on i2c-{args.bus}")
    print(f"  MODE2    0x{mode2:02x}")
    print(f"  PRESCALE {prescale}  "
          + ("(known-good setting)" if known_good
             else "*** NOT the known-good 119 - frequency or correction was changed ***"))
    print()

    armed = [c for c in channels if not c[1]]
    for channel, full_off, on_l, on_h, off_l, off_h in channels:
        name = labels.get(channel, "")
        name = f"  <- {name}" if name else ""
        state = "full off (limp)" if full_off else "*** DRIVING ***"
        print(f"  ch{channel:2}  ON=0x{on_h:02x}{on_l:02x} OFF=0x{off_h:02x}{off_l:02x}  "
              f"{state}{name}")
    print()

    if armed:
        names = ", ".join(f"ch{c[0]}" for c in armed)
        print(f"RESULT: ARMED - {len(armed)} channel(s) emitting pulses: {names}")
        if not known_good:
            print(f"        ...and the prescale is wrong too ({prescale}, not 119), which by")
            print("        itself makes a servo buzz, jitter or creep. Check [pan_tilt]")
        print("        A servo holding torque can be commanded to move at any moment.")
        print("        To make the rig limp:  uv run arducam release")
        return 1

    print("RESULT: LIMP - all 16 channels are off, no servo has power or torque")
    return 0


if __name__ == "__main__":
    sys.exit(main())
