# Configuration

One TOML file plus optional environment variables. Nothing about your hardware is
compiled into the code, which is the main structural difference from the previous
version (where the I2C address, channels, pulse widths, angle limits and camera
resolutions were literals spread through `app.py`).

## Where the file is looked for

In order, first hit wins:

1. `$ARDUCAM_CONFIG` (path from the environment - what systemd and tests use)
2. `./arducam.toml` (next to wherever you started the server)
3. `~/.config/arducam-ptz/config.toml`

Start from the documented example, which lists every key with its default value:

```bash
cp config/arducam.example.toml arducam.toml
```

A file does not have to be complete: anything you leave out keeps its default.
The example file is checked by the test suite, so it cannot drift out of date.

## Environment overrides

Pattern: `ARDUCAM_<SECTION>__<KEY>`, with a double underscore before the key.

```bash
ARDUCAM_SERVER__PORT=9000
ARDUCAM_CAMERA__PREVIEW_WIDTH=1280
ARDUCAM_CAMERA__PREVIEW_QUALITY=70
ARDUCAM_PAN_TILT__PAN_MIN=70
ARDUCAM_FOCUS__SETTLE_MS=200
ARDUCAM_STORAGE__PHOTOS_DIR=/mnt/usb/photos
ARDUCAM_SIMULATE=true            # force every backend into simulation
```

Precedence: **explicit function arguments > environment > TOML file > defaults.**
So a value in the environment always beats the same value in the file, which is
what you want for deploy-time tweaks (`systemctl edit` / docker env) while the
file holds the site's standing settings.

## Sections

### `[server]`

| key | default | meaning |
| --- | --- | --- |
| `host` | `0.0.0.0` | bind address; `127.0.0.1` to keep it local-only |
| `port` | `8080` | HTTP port |
| `log_level` | `info` | `debug` adds per-frame and per-servo-write logging |

### `[camera]`

| key | default | meaning |
| --- | --- | --- |
| `preview_width` / `preview_height` | `960` / `540` | size of the MJPEG preview |
| `preview_quality` | `75` | JPEG quality, 1-95 |
| `buffer_count` | `4` | libcamera buffers |
| `capture_width` / `capture_height` | `0` / `0` | still capture size; `0` = native sensor mode (16 MP) |
| `stall_timeout_s` | `6.0` | restart the preview after this long with no frame; `0` disables the supervisor |
| `max_restarts` | `5` | give up after this many consecutive restart attempts |

Measured cost of the preview on the Pi 5 (27 fps, JPEG quality 75/70) is in
`docs/HARDWARE.md`; the short version is 960x540 = ~0.3 MB/s and 13% of one core.

### `[pan_tilt]`

| key | default | meaning |
| --- | --- | --- |
| `i2c_bus` / `i2c_address` | `1` / `0x40` | where the PCA9685 lives |
| `pan_channel` / `tilt_channel` | `1` / `0` | PCA9685 outputs as wired on this rig |
| `min_pulse_us` / `max_pulse_us` | `500` / `2500` | servo pulse range for 0-180 degrees |
| `pwm_frequency_hz` | `50.0` | hobby-servo standard |
| `pan_min` / `pan_max` | `60` / `120` | mechanical travel limits |
| `tilt_min` / `tilt_max` | `15` / `145` | mechanical travel limits |
| `pan_center` / `tilt_center` | `90` / `90` | where "centre" puts the camera |
| `invert_pan` / `invert_tilt` | `false` | flip the direction of an axis |
| `max_step_degrees` | `0` (off) | limit a single requested move; the reply reports the clamp |
| `write_rate_hz` | `20` | servo update rate |
| `min_write_interval_ms` | `40` | minimum spacing between I2C writes |

Direction convention (unchanged from the original rig, so the two projects agree):
**increasing `pan` turns right, increasing `tilt` tilts down.**

### `[focus]`

| key | default | meaning |
| --- | --- | --- |
| `device` | unset | explicit `/dev/v4l-subdevN`; unset = probe for `focus_absolute` |
| `control` | `focus_absolute` | V4L2 control name |
| `settle_ms` | `140` | wait after moving the lens before scoring a frame |
| `sample_timeout_ms` | `1500` | how long to wait for a fresh preview frame |
| `coarse_steps` / `fine_steps` | `9` / `9` | autofocus sweep resolution |
| `near_is_high` | `true` | whether larger `focus_absolute` means a nearer subject |

### `[storage]`

| key | default | meaning |
| --- | --- | --- |
| `photos_dir` | `photos` | where stills are written |
| `thumbnail_dir` | `.thumbs` | cache for gallery thumbnails |
| `thumbnail_max_dim` | `320` | longest edge of a thumbnail, in pixels |
| `filename_template` | `photo_%Y%m%d-%H%M%S` | strftime template, no extension |

Relative paths resolve against **the folder containing the config file**, not the
process working directory, so photos do not "disappear" depending on where you
started the server. With no config file at all they resolve against the current
working directory.

## Checking the effective configuration

```bash
curl -s localhost:8080/api/v1/health | python3 -m json.tool   # resolved settings + hardware health
uv run arducam config                                          # same thing, human-readable
```
