"""Error mapping: hardware and validation problems become meaningful HTTP answers.

Why this is a module and not a few try/excepts in the handlers: the old app answered
almost everything with ``{"success": false}`` and HTTP 200, so a phone UI could not
tell "the servo is unplugged" from "you asked for 500 degrees" - and the user got a
button that appeared to work but did nothing. Every failure mode here has a distinct
status code, and the body says which component failed and why.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from arducam_ptz.models import ApiModel
from arducam_ptz.services.captures import CaptureNotFound
from arducam_ptz.services.focus import FocusBusyError

logger = logging.getLogger(__name__)


class ErrorBody(ApiModel):
    """Machine-readable error payload. ``error`` is a stable code, not prose."""

    error: str
    detail: str
    component: str | None = None
    #: Set on a 409 so the client can poll the job that blocked it.
    job_id: str | None = None


class HardwareUnavailableError(RuntimeError):
    """A hardware call failed in a way the client cannot fix by retrying."""

    def __init__(self, component: str, detail: str) -> None:
        super().__init__(detail)
        self.component = component
        self.detail = detail


def _json(
    status_code: int,
    error: str,
    detail: str,
    component: str | None = None,
    job_id: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorBody(error=error, detail=detail, component=component, job_id=job_id).model_dump(),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Attach the handlers. Called once from :func:`arducam_ptz.api.app.create_app`."""

    @app.exception_handler(CaptureNotFound)
    async def _capture_not_found(_request: Request, exc: CaptureNotFound) -> JSONResponse:
        # 404 rather than 403: a malformed name must not reveal whether a file exists.
        return _json(status.HTTP_404_NOT_FOUND, "capture_not_found", f"no such photo: {exc}")

    @app.exception_handler(FocusBusyError)
    async def _focus_busy(_request: Request, exc: FocusBusyError) -> JSONResponse:
        # 409 + the running job id: the client attaches to it instead of firing a
        # second sweep (which would fight over the motor).
        return _json(
            status.HTTP_409_CONFLICT,
            "autofocus_busy",
            str(exc),
            component="focus",
            job_id=exc.job_id,
        )

    @app.exception_handler(HardwareUnavailableError)
    async def _hardware(_request: Request, exc: HardwareUnavailableError) -> JSONResponse:
        return _json(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "hardware_unavailable",
            exc.detail,
            component=exc.component,
        )

    @app.exception_handler(ValueError)
    async def _bad_value(_request: Request, exc: ValueError) -> JSONResponse:
        return _json(status.HTTP_400_BAD_REQUEST, "invalid_request", str(exc))

    @app.exception_handler(RuntimeError)
    async def _runtime(_request: Request, exc: RuntimeError) -> JSONResponse:
        # A RuntimeError that reached the API layer is a hardware/motor problem: the
        # focus motor raises it when it is not initialised. Never a 500 with a
        # traceback, because that tells a phone nothing.
        logger.warning("runtime error while serving a request: %s", exc)
        return _json(status.HTTP_503_SERVICE_UNAVAILABLE, "not_available", str(exc))
