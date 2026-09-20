"""The application context: hardware and services wired together, once.

One object to build, start and close. The API layer takes it as a dependency
(``app.state.context``), tests build it in simulation mode, and the CLI never sees
it at all - the CLI talks HTTP, which is what makes the backend reusable from an
iPhone app or a shell script.

This is also where the process's uptime, version and overall health verdict live,
so there is exactly one answer to "is this thing okay?".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from arducam_ptz import __version__
from arducam_ptz.config import Settings
from arducam_ptz.hardware import HardwareBundle, build_hardware
from arducam_ptz.hardware.base import Availability
from arducam_ptz.models import ComponentHealth, Health, SystemState
from arducam_ptz.services.captures import CaptureService
from arducam_ptz.services.focus import FocusService
from arducam_ptz.services.frames import FrameHub
from arducam_ptz.services.jobs import JobRegistry
from arducam_ptz.services.ptz import PanTiltService

logger = logging.getLogger(__name__)

#: Order in which components are reported. Fixed here (not inherited from a dict)
#: so the UI can render a stable list and a reordering shows up as a real change.
_COMPONENT_ORDER: tuple[str, ...] = ("camera", "pan_tilt", "focus")


@dataclass
class AppContext:
    """Everything a request might need, plus its lifecycle."""

    settings: Settings
    hub: FrameHub
    hardware: HardwareBundle
    ptz: PanTiltService
    focus: FocusService
    captures: CaptureService
    jobs: JobRegistry
    started_at: float = field(default_factory=time.time)
    log: logging.Logger = field(default_factory=lambda: logger)

    # ------------------------------------------------------------------ factory
    @classmethod
    def build(cls, settings: Settings, log: logging.Logger | None = None) -> AppContext:
        """Assemble the whole stack from settings without touching hardware yet.

        Building and starting are separate so tests (and the ``--simulate`` path)
        can construct an application and only then decide to run it.
        """
        log = log or logger
        hub = FrameHub(log)
        hardware = build_hardware(settings, hub, log)
        jobs = JobRegistry(log=log)
        ptz = PanTiltService(settings.pan_tilt, hardware.pan_tilt, log)
        focus = FocusService(
            settings.focus,
            hardware.focus,
            hub,
            jobs,
            camera=hardware.camera,
            log=log,
        )
        captures = CaptureService(settings.storage, hardware.camera, log)
        return cls(
            settings=settings,
            hub=hub,
            hardware=hardware,
            ptz=ptz,
            focus=focus,
            captures=captures,
            jobs=jobs,
            log=log,
        )

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Bring up hardware, the servo writer and the photo directories."""
        self.captures.ensure_directories()
        self.hardware.start()
        self.ptz.start()
        self.log.info(
            "arducam-ptz %s ready (config: %s)",
            __version__,
            self.settings.source_path or "built-in defaults",
        )

    def close(self) -> None:
        """Shut everything down, in reverse order, without raising."""
        self.ptz.stop()
        self.hardware.close()
        self.log.info("shutdown complete")

    # ------------------------------------------------------------------ reporting
    @property
    def uptime_s(self) -> float:
        return round(time.time() - self.started_at, 2)

    def component_health(self) -> list[ComponentHealth]:
        availabilities: dict[str, Availability] = self.hardware.availabilities()
        return [
            ComponentHealth(
                name=name,  # type: ignore[arg-type]
                available=availabilities[name].available,
                simulated=availabilities[name].simulated,
                detail=availabilities[name].detail,
            )
            for name in _COMPONENT_ORDER
            if name in availabilities
        ]

    def health(self) -> Health:
        """Overall verdict: ok, degraded (something missing) or simulated."""
        components = self.component_health()
        if all(component.simulated for component in components):
            status = "simulated"
        elif all(component.available for component in components):
            status = "ok"
        else:
            status = "degraded"
        return Health(
            status=status,  # type: ignore[arg-type]
            version=__version__,
            uptime_s=self.uptime_s,
            components=components,
            config_source=str(self.settings.source_path) if self.settings.source_path else None,
        )

    def state(self) -> SystemState:
        """Aggregate state for the polling UI: one request instead of three."""
        return SystemState(
            health=self.health(),
            ptz=self.ptz.state(),
            focus=self.focus.state(),
            # The hub returns a plain dict; pydantic coerces it into StreamStats, so
            # there is one definition of the shape instead of two.
            stream=self.hub.stats(),  # type: ignore[arg-type]
        )

    def mounted_paths(self) -> Iterable[str]:
        """Paths this instance writes to - handy in logs and health output."""
        yield str(self.settings.storage.photos_dir)
        yield str(self.settings.storage.thumbnail_dir)
