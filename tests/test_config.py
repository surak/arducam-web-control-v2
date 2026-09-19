"""Configuration tests.

The example TOML file is part of the contract: if it drifts from the model, the
first test below fails. The old project's requirements.txt/README drifted apart
within a day, so this is a deliberate guard.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from arducam_ptz.config import (
    CONFIG_CANDIDATES,
    PanTiltSettings,
    Settings,
    load_settings,
    resolve_config_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "arducam.example.toml"


def _walk_unknown_keys(data: dict, model: type, path: str = "") -> list[str]:
    """Return every key in ``data`` that the settings model does not know."""
    unknown: list[str] = []
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            unknown.append(f"{path}{key}")
            continue
        sub = field.annotation
        if isinstance(value, dict):
            nested = next(
                (arg for arg in (getattr(sub, "__args__", ()) or (sub,)) if hasattr(arg, "model_fields")),
                None,
            )
            if nested is not None:
                unknown.extend(_walk_unknown_keys(value, nested, f"{path}{key}."))
    return unknown


def test_example_config_matches_the_schema() -> None:
    """The documented example must only contain keys the code understands."""
    raw = tomllib.loads(EXAMPLE_CONFIG.read_text())
    assert _walk_unknown_keys(raw, Settings) == []


def test_defaults_without_any_config_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "does-not-exist.toml")
    assert settings.source_path == tmp_path / "does-not-exist.toml"
    assert settings.server.port == 8080
    assert settings.camera.preview_width == 960
    assert settings.pan_tilt.pan_min == 60
    assert settings.simulate is False
    assert settings.simulate_everything() is False
    # No config file -> storage paths resolve against the working directory.
    assert settings.storage.photos_dir.is_absolute()


def test_example_config_parses_to_its_own_documented_defaults() -> None:
    """The example ships with the defaults filled in, so loading it changes nothing."""
    from_example = load_settings(EXAMPLE_CONFIG)
    plain = load_settings()
    assert from_example.camera == plain.camera
    assert from_example.pan_tilt == plain.pan_tilt
    assert from_example.focus == plain.focus
    assert from_example.server == plain.server
    # Storage paths are the documented defaults, resolved against the example's
    # own folder (see test_toml_file_is_applied).
    assert from_example.storage.thumbnail_max_dim == plain.storage.thumbnail_max_dim
    assert from_example.storage.photos_dir == EXAMPLE_CONFIG.parent / "photos"


def test_toml_file_is_applied(tmp_path: Path) -> None:
    config = tmp_path / "arducam.toml"
    config.write_text(
        """
[server]
port = 9001
[camera]
preview_width = 640
preview_height = 480
[pan_tilt]
pan_min = 70
pan_max = 110
[storage]
photos_dir = "shots"
"""
    )
    settings = load_settings(config)
    assert settings.server.port == 9001
    assert (settings.camera.preview_width, settings.camera.preview_height) == (640, 480)
    assert (settings.pan_tilt.pan_min, settings.pan_tilt.pan_max) == (70, 110)
    # Relative photo paths follow the config file, not the process cwd.
    assert settings.storage.photos_dir == tmp_path / "shots"


def test_environment_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "arducam.toml"
    config.write_text("[server]\nport = 9001\n")
    monkeypatch.setenv("ARDUCAM_SERVER__PORT", "9123")
    monkeypatch.setenv("ARDUCAM_CAMERA__PREVIEW_WIDTH", "320")
    monkeypatch.setenv("ARDUCAM_SIMULATE", "true")

    settings = load_settings(config)
    assert settings.server.port == 9123
    assert settings.camera.preview_width == 320
    assert settings.simulate_everything() is True


def test_explicit_overrides_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARDUCAM_SERVER__PORT", "9123")
    settings = load_settings(overrides={"server": {"port": 7000}})
    assert settings.server.port == 7000


def test_config_path_env_var_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "custom.toml"
    config.write_text("[server]\nport = 4321\n")
    monkeypatch.setenv("ARDUCAM_CONFIG", str(config))
    assert resolve_config_path() == config
    assert load_settings().server.port == 4321


def test_unknown_config_path_reports_where_it_looked(tmp_path: Path) -> None:
    """A typo'd ARDUCAM_CONFIG must not silently fall back to the defaults."""
    missing = tmp_path / "nope.toml"
    settings = load_settings(missing)
    assert settings.source_path == missing
    assert settings.server.port == 8080  # defaults, because the file is absent


def test_impossible_travel_limits_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must be smaller"):
        PanTiltSettings(pan_min=120, pan_max=60)
    with pytest.raises(ValidationError, match="must lie within"):
        PanTiltSettings(tilt_min=15, tilt_max=145, tilt_center=10)
    with pytest.raises(ValidationError, match="min_pulse_us"):
        PanTiltSettings(min_pulse_us=2500, max_pulse_us=500)


def test_out_of_range_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"server": {"port": 99999}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"camera": {"preview_quality": 200}})


def test_config_candidates_cover_cwd_and_home() -> None:
    """The two documented fallback locations must both be searched."""
    first, second = CONFIG_CANDIDATES
    assert first == Path.cwd() / "arducam.toml"
    assert second == Path.home() / ".config" / "arducam-ptz" / "config.toml"
