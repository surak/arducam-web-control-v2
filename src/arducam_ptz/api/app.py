"""The FastAPI application factory.

``create_app(settings)`` is the only entry point; ``arducam_ptz.server`` builds the
settings and calls it. Everything stateful hangs off ``app.state.context`` (see
:mod:`arducam_ptz.api.deps`), which is why tests can run several independent
applications in one process - each with its own simulated hardware.

Startup and shutdown are deliberately forgiving:

* if the camera is missing, the API still starts and advertises ``degraded``; the UI
  can say "no camera" instead of showing a connection error. The old app refused to
  start without hardware, which meant you could not even see *why* it was unhappy;
* shutdown always tries to release the servos, because a servo left holding torque is
  a hot, buzzing servo.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from arducam_ptz import __version__
from arducam_ptz.api.errors import install_error_handlers
from arducam_ptz.api.routes import captures, focus, ptz, stream
from arducam_ptz.api.routes import status as status_routes
from arducam_ptz.config import Settings
from arducam_ptz.services.context import AppContext

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

#: Where the bundled UI lives. Kept next to the package so it ships with it.
#: Directory holding the static interface (index.html, app.css, js/). Inside the
#: installed package so that `pip install` ships the UI along with the API.
WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

DESCRIPTION = """
Control an Arducam 16 MP autofocus camera on a PCA9685 pan/tilt rig.

* **Pan/tilt** - absolute moves, relative nudges, centre, release.
* **Focus** - manual position, relative steps, and a contrast-detect autofocus sweep
  that runs as a background job (202 + poll) so the preview keeps streaming.
* **Preview** - one MJPEG stream shared by every viewer; extra tabs cost nothing.
* **Captures** - full-resolution stills plus cached thumbnails for gallery grids.

Every command answers with the state that actually resulted, including any clamping
the rig applied, so a client never has to assume its request succeeded.
"""


def create_app(settings: Settings, context: AppContext | None = None) -> FastAPI:
    """Build the application. ``context`` is injectable for tests."""
    logging.basicConfig(
        level=settings.server.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Built and attached here rather than inside the lifespan so that
    # app.state.context exists even when the app is driven without lifespan support
    # (httpx.ASGITransport in the CLI tests, for instance). The lifespan then only
    # controls *lifecycle*: start the threads, stop them on shutdown.
    resolved_context = context or AppContext.build(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.context.start()
        try:
            yield
        finally:
            app.state.context.close()

    app = FastAPI(
        title="arducam-ptz",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        # The API is versioned in the path (/api/v1/...), so the schema URLs stay
        # stable for the CLI and for a future iPhone app.
        docs_url="/docs",
        redoc_url=None,
    )

    app.state.settings = settings
    app.state.context = resolved_context

    if settings.server.cors_origins:
        # Only when explicitly configured: the bundled UI is same-origin, and wide-open
        # CORS on a device that physically moves is a bad default.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.server.cors_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Token"],
            allow_credentials=False,
        )

    install_error_handlers(app)

    app.include_router(status_routes.router, prefix=API_PREFIX)
    app.include_router(stream.router, prefix=API_PREFIX)
    app.include_router(ptz.router, prefix=API_PREFIX)
    app.include_router(focus.router, prefix=API_PREFIX)
    app.include_router(captures.router, prefix=API_PREFIX)

    @app.get("/api/version", tags=["status"], summary="Version and API prefix")
    def version() -> dict[str, str]:
        """Unauthenticated on purpose: clients need to check compatibility first."""
        return {"name": "arducam-ptz", "version": __version__, "api_prefix": API_PREFIX}

    # Mounted last: the static UI is a catch-all on "/", so every API route has to be
    # registered before it or it will be shadowed. (This bit me with /api/version.)
    _mount_web_ui(app, settings)

    return app


def _mount_web_ui(app: FastAPI, settings: Settings) -> None:
    """Serve the static UI at ``/`` when it exists and is enabled."""
    if not settings.server.web_ui:
        logger.info("web UI disabled by configuration")
        return
    if not WEB_DIR.is_dir():  # pragma: no cover - packaging accident
        logger.warning("web UI directory %s is missing; serving API only", WEB_DIR)
        return

    # Mounted last so /api/v1/* routes always win. html=True makes / serve index.html.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> JSONResponse:  # pragma: no cover - cosmetic
        return JSONResponse(status_code=204, content=None)


def index_file() -> FileResponse | None:  # pragma: no cover - convenience for scripts
    """The UI's entry document, if it was packaged. Used by the server's ``/`` route."""
    index = WEB_DIR / "index.html"
    return FileResponse(index) if index.is_file() else None


__all__ = ["API_PREFIX", "WEB_DIR", "create_app", "index_file"]
