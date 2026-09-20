#!/usr/bin/env python3
"""Take the pan/tilt rig limp without a running server.

Why this exists: a PCA9685 keeps emitting whatever pulse was last written to it, with or
without a controller. The chip does not know the process that configured it has died, so
a crash, a `kill -9`, a power blip on the *Pi* while the servo supply stays up, or an
unplugged USB cable all leave the servos holding torque - hot, buzzing, and (with a
continuous-rotation servo or a wrong pulse) apparently frantic.

`arducam release` cannot help there, because the CLI talks HTTP and needs a live server -
which is precisely what you do not have in that situation. This script writes the
channels off directly over I2C, so it works with the server dead, absent, or refusing to
start because the camera is wedged.

What it does: writes LED_ON = 0 and LED_OFF_H bit 4 (full off) for every channel, the
same state the driver's `release()` produces. It is therefore safe to run repeatedly, and
safe to run while a server is up - a later move command will simply re-arm the channels.

Usage:
    uv run python scripts/limp.py
    uv run python scripts/limp.py --bus 1 --address 0x40 --channels 0,1

Run `scripts/check_servos.py` afterwards to confirm every channel reads as off.
"""

from __future__ import annotations

import argparse
import time

try:
    from smbus2 import SMBus
except ImportError:  # pragma: no cover - only on a machine without the runtime deps
    print("smbus2 is not installed: run this with `uv run python scripts/limp.py`")
    raise SystemExit(2) from None

#: PCA9685 register map, only what this script needs.
MODE1 = 0x00
LED0_ON_L = 0x06

#: Bit 4 of LEDn_OFF_H means "off from the start of the period", i.e. no pulse at all.
OFF_FULL = 0x10


def limp(bus_number: int, address: int, channels: list[int], settle_ms: float) -> None:
    """Write every requested channel to the chip's *no pulse* state.

    A short pause between channels keeps a single write burst from being lost when the
    bus is long or noisy - the same reason the driver spaces out its own writes.
    """
    with SMBus(bus_number) as bus:
        # MODE2 = 0x04 selects "totem pole" outputs (datasheet 7.3.7), which is what the
        # driver configures and what most breakout boards are wired for. Matching the
        # driver avoids leaving the board in a state the next `serve` has to fix.
        bus.write_byte_data(address, 0x01, 0x04)
        for channel in channels:
            base = LED0_ON_L + 4 * channel
            bus.write_byte_data(address, base + 0, 0x00)  # ON_L
            bus.write_byte_data(address, base + 1, 0x00)  # ON_H
            bus.write_byte_data(address, base + 2, 0x00)  # OFF_L
            bus.write_byte_data(address, base + 3, OFF_FULL)  # OFF_H: full off
            time.sleep(settle_ms / 1000.0)


def parse_channels(text: str) -> list[int]:
    """`0,1` or `all` -> a list of channel numbers, with the obvious mistakes rejected."""
    if text.strip().lower() == "all":
        return list(range(16))
    try:
        channels = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected channel numbers, got {text!r}") from None
    if not channels:
        raise argparse.ArgumentTypeError("no channels given")
    for channel in channels:
        if not 0 <= channel <= 15:
            raise argparse.ArgumentTypeError(f"channel {channel} is outside 0-15")
    return channels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument("--address", type=lambda text: int(text, 0), default=0x40,
                        help="PCA9685 address (default 0x40)")
    parser.add_argument("--channels", type=parse_channels, default=list(range(16)),
                        help="channels to release: 'all', a list, or a single number")
    parser.add_argument("--settle-ms", type=float, default=5.0,
                        help="pause after each channel write (default 5)")
    args = parser.parse_args()

    try:
        limp(args.bus, args.address, args.channels, args.settle_ms)
    except OSError as error:
        # Bus errors are the common failure here (wrong bus, no board, I2C disabled) and
        # a traceback helps nobody at 3am.
        print(f"could not talk to the PCA9685 on bus {args.bus}: {error}")
        return 2

    print(f"released {len(args.channels)} channel(s) on bus {args.bus} at 0x{args.address:02x}")
    print("verify with: uv run python scripts/check_servos.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
