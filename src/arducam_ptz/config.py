"""Configuration: one TOML file + environment overrides, validated by pydantic.

Why this module exists at all: the old app hard-coded the PCA9685 I2C address,
the servo channels, pulse widths, angle limits, camera resolutions and the photo
directory in the middle of a 1000-line file, so every deployment detail was a
code change and every tweak was a fork. Here all of it is data that is validated
once at start-up, and ``config/arducam.example.toml`` doubles as documentation.

Precedence (highest first):  function arguments  >  environment  >  TOML file  >  defaults.

Environment variables use the prefix ``ARDUCAM_`` and ``__`` for nesting, e.g.::

    ARDUCAM_SERVER__PORT=9000
    ARDUCAM_CAMERA__PREVIEW_WIDTH=1280
    ARDUCAM_SIMULATE=true            # force every backend into simulation
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

ENV_PREFIX = "ARDUCAM_"

#: Config file used when nothing is specified, in order of preference.
CONFIG_CANDIDATES = (
    Path.cwd() / "arducam.toml",
    Path.home() / ".config" / "arducam-ptz" / "config.toml",
)

#: Set by :func:`load_settings` so the TOML source knows which file to read.
#: pydantic-settings' declarative ``toml_file`` cannot be used because the path
#: is resolved at runtime (env var -> ./arducam.toml -> ~/.config/...).
_config_path: ContextVar[Path | None] = ContextVar("arducam_config_path", default=None)


def resolve_config_path(explicit: Path | None = None) -> Path | None:
    """Return the config file to read, or ``None`` when there is none.

    ``ARDUCAM_CONFIG`` wins so a systemd unit or a test can point somewhere else
    without touching the working directory.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    for candidate in CONFIG_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


class ServerSettings(BaseModel):
    """HTTP listener and access control."""

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    #: Shared secret. None = open (the default: this is a LAN camera, and the web UI
    #: loads the MJPEG stream from an <img> tag, which cannot send headers - a
    #: mandatory token would have to travel in the URL anyway). Set it when the
    #: camera is reachable from an untrusted network; the CLI sends it as
    #: X-API-Token, and stream endpoints additionally accept ?token=.
    token: str | None = None
    #: Extra origins allowed to call the API from a browser. Empty = same-origin
    #: only, which is what the bundled UI uses. A future iPhone app does not need
    #: this (it is not a browser); add origins only for a separate web front-end.
    cors_origins: list[str] = Field(default_factory=list)
    #: Serve the bundled web UI at /. Turn off when this process is only a backend.
    web_ui: bool = True
    #: Seconds a client may hold the MJPEG stream open without reading before it is
    #: considered gone. Guards against half-dead TCP connections on mobile networks,
    #: which - in the old app - kept a slot busy forever.
    stream_idle_timeout_s: float = Field(default=15.0, ge=1.0, le=120.0)


class CameraSettings(BaseModel):
    """IMX519 preview stream and still capture.

    Defaults are the ones measured on the real rig (see docs/HARDWARE.md): a
    960x540 preview at quality 75 costs ~13% of one core, ~2.6 Mbit/s, 27 fps -
    a compromise that is comfortable on a phone over WiFi. Raising it to
    1280x720 doubles the bandwidth for a modest sharpness gain.
    """

    simulate: bool = False
    preview_width: int = Field(default=960, ge=160, le=4656)
    preview_height: int = Field(default=540, ge=120, le=3496)
    #: JPEG quality of preview frames (1-95). Higher = clearer picture, more bits.
    preview_quality: int = Field(default=75, ge=1, le=95)
    #: Kernel buffers held by libcamera. 4 is the smallest value that did not
    #: stutter while a still capture switched sensor modes.
    buffer_count: int = Field(default=4, ge=2, le=16)
    #: Still capture resolution. 0 x 0 means "native sensor mode" (4656x3496 here).
    capture_width: int = Field(default=0, ge=0, le=4656)
    capture_height: int = Field(default=0, ge=0, le=3496)
    #: Restart the preview when no frame has arrived for this long (cable glitch,
    #: camera wedged). 0 disables the supervisor.
    stall_timeout_s: float = Field(default=6.0, ge=0, le=120)
    max_restarts: int = Field(default=5, ge=0, le=100)


class PanTiltSettings(BaseModel):
    """PCA9685 servo rig.

    ``pan`` increases to the right of the scene, ``tilt`` increases downwards -
    the convention of the original rig, kept so the two projects agree about
    what "left" means. Flip ``invert_*`` if the servos are re-mounted mirrored.
    """

    simulate: bool = False
    i2c_bus: int = Field(default=1, ge=0, le=99)
    i2c_address: int = Field(default=0x40, ge=0x00, le=0x7F)
    pan_channel: int = Field(default=1, ge=0, le=15)
    tilt_channel: int = Field(default=0, ge=0, le=15)
    min_pulse_us: int = Field(default=500, ge=200, le=3000)
    max_pulse_us: int = Field(default=2500, ge=500, le=5000)
    #: Nominal PWM frequency. 60 Hz is what this rig was tuned with - do not
    #: "correct" it to the 50 Hz hobby standard: this number feeds the angle ->
    #: pulse mapping, so changing it silently rescales every angle by that ratio
    #: (50/60 = 17%). The travel limits below were measured in *this* domain.
    pwm_frequency_hz: float = Field(default=60.0, ge=24, le=1526)
    #: Oscillator correction, applied to the prescale only. 0.8449 is a measured
    #: property of this board, not a preference: it was found by trial on a real
    #: servo in the original project, and it is what makes a commanded 90 deg land
    #: in the middle of the travel instead of near a hard stop. A board that is not
    #: this one starts at 1.0 and should be measured before servo limits are set.
    pwm_frequency_correction: float = Field(default=0.8449, gt=0.1, le=2.0)
    #: Mechanical travel limits, measured on this platform's hard stops.
    pan_min: float = Field(default=60.0, ge=0, le=180)
    pan_max: float = Field(default=120.0, ge=0, le=180)
    tilt_min: float = Field(default=15.0, ge=0, le=180)
    tilt_max: float = Field(default=145.0, ge=0, le=180)
    pan_center: float = Field(default=90.0, ge=0, le=180)
    tilt_center: float = Field(default=90.0, ge=0, le=180)
    invert_pan: bool = False
    invert_tilt: bool = False
    #: Refuse single moves larger than this many degrees (0 = no limit). A slider
    #: that is dragged to the far end is *reported* as clamped rather than being
    #: silently turned into a 1-degree step (which is what the old app did).
    max_step_degrees: float = Field(default=0.0, ge=0, le=180)
    #: How often desired->written, and the minimum spacing between bus writes.
    write_rate_hz: float = Field(default=20.0, ge=1, le=100)
    min_write_interval_ms: float = Field(default=40.0, ge=5, le=1000)
    #: Quiet time after each servo write, while the camera may be streaming.
    #:
    #: Not a guess and not a fudge: the original project traced "servos thrash during
    #: use" to I2C contention between picamera2's frame pipeline and PCA9685 writes on
    #: this Pi, and fixed it with a 5 ms gap after every frame. The same effect is
    #: reachable with no gap at all on an idle bench, which is what makes it nasty -
    #: it only shows up once the camera is streaming. Zero is allowed for a rig with a
    #: separate I2C controller, and it costs at most `write_rate_hz` * this per second
    #: of writer-thread time (20 Hz x 5 ms = 1%).
    i2c_settle_ms: float = Field(default=5.0, ge=0, le=50)

    @model_validator(mode="after")
    def _check_ranges(self) -> PanTiltSettings:
        for axis in ("pan", "tilt"):
            low, high = getattr(self, f"{axis}_min"), getattr(self, f"{axis}_max")
            if low >= high:
                raise ValueError(f"{axis}_min ({low}) must be smaller than {axis}_max ({high})")
            center = getattr(self, f"{axis}_center")
            if not low <= center <= high:
                raise ValueError(f"{axis}_center ({center}) must lie within {low}..{high}")
        if self.min_pulse_us >= self.max_pulse_us:
            raise ValueError("min_pulse_us must be smaller than max_pulse_us")
        return self


class FocusSettings(BaseModel):
    """AK7375 voice-coil focus motor, reached through the sensor subdevice.

    ``focus_absolute`` is the only focus control libcamera exposes for this
    module; there is no autofocus algorithm in the IMX519 tuning file, which is
    why autofocus in this project is a sharpness sweep (see services/focus.py).
    """

    simulate: bool = False
    #: None = probe /dev/v4l-subdev* for a device exposing ``focus_absolute``.
    device: str | None = None
    control: str = "focus_absolute"
    #: Wait after moving the lens before sampling: the VCM needs to settle or a
    #: sweep measures its own ringing.
    settle_ms: int = Field(default=140, ge=0, le=2000)
    #: How long to wait for the next preview frame while sampling a position.
    sample_timeout_ms: int = Field(default=1500, ge=100, le=10000)
    #: Frames to discard after moving the lens before scoring one. A frame can be
    #: published immediately after a move while still holding pre-move pixels (it was
    #: already in the encoder), so at least one frame must be dropped. Raise this if
    #: a sweep ever lands one coarse step off; 0 is only sensible in simulations with
    #: a hand-synchronised frame source.
    pipeline_skip_frames: int = Field(default=1, ge=0, le=10)
    #: Coarse sweep samples across the whole travel, then fine samples inside the
    #: winning coarse interval (the old app's sweep shape, kept: it worked).
    coarse_steps: int = Field(default=9, ge=3, le=64)
    fine_steps: int = Field(default=9, ge=1, le=64)
    #: Larger focus_absolute means the lens moves closer to the sensor, i.e.
    #: focuses on something nearer. Set true if your module is the other way
    #: round; it only changes how the UI labels the slider.
    near_is_high: bool = True


class StorageSettings(BaseModel):
    """Captured photos and their thumbnails."""

    photos_dir: Path = Path("photos")
    thumbnail_dir: Path = Path(".thumbs")
    thumbnail_max_dim: int = Field(default=320, ge=64, le=2048)
    #: strftime template for new filenames (no extension).
    filename_template: str = "photo_%Y%m%d-%H%M%S"


class Settings(BaseSettings):
    """Root settings object. Build it with :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",
        # Keeping the camel/snake mapping explicit rather than case-insensitive
        # magic: ARDUCAM_PAN_TILT__PAN_MIN maps to pan_tilt.pan_min.
        case_sensitive=False,
        validate_default=True,
    )

    #: Shortcut: force every hardware backend into simulation, whatever the
    #: per-section flags say. Used by --simulate and by the test suite.
    simulate: bool = False

    server: ServerSettings = ServerSettings()
    camera: CameraSettings = CameraSettings()
    pan_tilt: PanTiltSettings = PanTiltSettings()
    focus: FocusSettings = FocusSettings()
    storage: StorageSettings = StorageSettings()

    #: Where the configuration came from (None = built-in defaults only).
    source_path: Path | None = Field(default=None, exclude=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - fixed pydantic-settings signature
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Layer init args > environment > TOML file > secrets.

        Written explicitly (instead of the declarative ``toml_file=`` option)
        because the file location depends on runtime state.
        """
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        path = _config_path.get()
        if path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=path))
        sources.append(file_secret_settings)
        return tuple(sources)

    def with_server(self, **overrides: object) -> Settings:
        """Return a copy with the ``server`` section overridden.

        Used by the CLI/server argument parsing: ``--host``/``--port`` are one-off
        overrides and must not silently persist into the config file.
        """
        return self.model_copy(update={"server": self.server.model_copy(update=overrides)})

    def simulate_everything(self) -> bool:
        """True when no real hardware should be touched at all."""
        return bool(self.simulate or self.camera.simulate or self.pan_tilt.simulate or self.focus.simulate)


def _absolutise(settings: Settings, base: Path) -> Settings:
    """Make storage paths absolute relative to the config file (or cwd).

    Without this, ``photos_dir = "photos"`` means something different depending
    on where the process was started - a classic way to "lose" captured photos.
    """
    storage = settings.storage
    if storage.photos_dir.is_absolute() and storage.thumbnail_dir.is_absolute():
        return settings
    return settings.model_copy(
        update={
            "storage": storage.model_copy(
                update={
                    "photos_dir": (base / storage.photos_dir).resolve(),
                    "thumbnail_dir": (base / storage.thumbnail_dir).resolve(),
                }
            )
        }
    )


def load_settings(
    config_path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Build :class:`Settings` from file + environment (+ explicit overrides).

    ``overrides`` are the same nested dict shape as the TOML file and are applied
    with the highest priority, which is what the CLI's ``--simulate`` /
    ``--port`` flags use.
    """
    path = resolve_config_path(config_path)
    token = _config_path.set(path)
    try:
        settings = Settings(**(overrides or {}))
    finally:
        _config_path.reset(token)

    settings = settings.model_copy(update={"source_path": path})
    # Relative storage paths resolve against the directory holding the config
    # file; with no config file, against the current working directory.
    base = path.parent if path is not None else Path.cwd()
    return _absolutise(settings, base)
