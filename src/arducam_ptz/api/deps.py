"""Shared FastAPI dependencies: the app context and optional token auth.

The context lives on ``app.state`` (set by the lifespan in :mod:`arducam_ptz.api.app`),
so handlers receive it through a dependency instead of importing a global. That is
what makes the test client able to run two independent applications - one simulated,
one with different settings - in the same process.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request, status

from arducam_ptz.config import Settings
from arducam_ptz.services.context import AppContext

logger = logging.getLogger(__name__)


def get_context(request: Request) -> AppContext:
    """The running application's context (hardware + services)."""
    context = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - only if the lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application is not initialised",
        )
    return context


def get_settings(request: Request) -> Settings:
    return get_context(request).settings


ContextDep = Annotated[AppContext, Depends(get_context)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_token(
    request: Request,
    x_api_token: Annotated[str | None, Header(alias="X-API-Token")] = None,
    token: Annotated[str | None, Query(description="Token in the query string.")] = None,
) -> None:
    """Shared-token gate, only when a token is configured.

    Two ways in, deliberately:

    * ``X-API-Token: <token>`` for the CLI, scripts and apps;
    * ``?token=<token>`` for ``<img>``/``EventSource``/MJPEG viewers, which cannot set
      request headers at all. The trade-off is real (a token in a URL can end up in
      logs or browser history), which is why the default configuration has no token:
      on a LAN-bound personal camera, a mandatory secret that then has to leak into
      image URLs is worse than no secret. Set one when the camera is reachable from
      anywhere you do not control, and prefer the header.

    Comparison is constant time so the token cannot be probed byte by byte.
    """
    settings: Settings = request.app.state.context.settings
    expected = settings.server.token
    if not expected:
        return
    supplied = x_api_token or token
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "X-API-Token"},
        )


AuthDep = Depends(require_token)
