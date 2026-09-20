"""AppContext tests: the whole stack wired from settings, in simulation mode.

This is the closest thing to an integration test that runs without hardware and
without a web server: build, start, ask for state, shut down.
"""

from __future__ import annotations

from pathlib import Path

from arducam_ptz.config import Settings
from arducam_ptz.services.context import AppContext


def simulated_settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.camera.simulate = True
    settings.pan_tilt.simulate = True
    settings.focus.simulate = True
    settings.camera.preview_width = 320
    settings.camera.preview_height = 180
    settings.storage.photos_dir = tmp_path / "photos"
    settings.storage.thumbnail_dir = tmp_path / "thumbs"
    return settings


class TestAppContext:
    def test_build_creates_every_service(self, tmp_path: Path) -> None:
        context = AppContext.build(simulated_settings(tmp_path))
        assert context.hub is not None
        assert context.ptz.state().simulated is True
        assert context.focus.state().simulated is True
        assert context.captures.settings.photos_dir.name == "photos"

    def test_health_reports_simulated_when_nothing_is_real(self, tmp_path: Path) -> None:
        context = AppContext.build(simulated_settings(tmp_path))
        health = context.health()
        assert health.status == "simulated"
        assert {component.name for component in health.components} == {"camera", "pan_tilt", "focus"}
        assert all(component.simulated for component in health.components)

    def test_component_order_is_stable(self, tmp_path: Path) -> None:
        """The UI renders these in order; a reshuffle should be a deliberate change."""
        context = AppContext.build(simulated_settings(tmp_path))
        assert [component.name for component in context.component_health()] == [
            "camera",
            "pan_tilt",
            "focus",
        ]

    def test_state_bundles_everything_the_ui_polls_for(self, tmp_path: Path) -> None:
        context = AppContext.build(simulated_settings(tmp_path))
        state = context.state()
        assert state.health.version
        assert state.ptz.pan.angle == 90
        assert state.focus.position is not None
        # Stream stats are always present, even before the first frame.
        assert state.stream.subscribers == 0
        assert state.stream.running is False

    def test_start_and_close_are_safe_and_repeatable(self, tmp_path: Path) -> None:
        context = AppContext.build(simulated_settings(tmp_path))
        context.start()
        context.start()  # idempotent
        try:
            assert context.captures.settings.photos_dir.is_dir()
            assert context.hub.subscriber_count == 0
        finally:
            context.close()
            context.close()

    def test_uptime_is_reported(self, tmp_path: Path) -> None:
        context = AppContext.build(simulated_settings(tmp_path))
        assert context.uptime_s >= 0
        assert context.health().uptime_s >= 0

    def test_mounted_paths_are_listed(self, tmp_path: Path) -> None:
        context = AppContext.build(simulated_settings(tmp_path))
        paths = list(context.mounted_paths())
        assert len(paths) == 2
        assert all(isinstance(path, str) for path in paths)
