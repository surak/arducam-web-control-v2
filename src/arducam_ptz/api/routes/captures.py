"""Capture endpoints: take, list, fetch (optionally downscaled) and delete photos.

Two details that matter on a phone:

* ``GET /captures/{filename}?max_dim=320`` serves a cached thumbnail. The gallery grid
  asks for that; tapping a photo asks for the full resolution. The old gallery put
  16 MP JPEGs in a grid, which is why scrolling it on a phone was painful.
* Filenames are validated by :class:`CaptureService` (bare basename, known extension,
  resolved path inside ``photos_dir``). A bad name is a 404, never a path escape.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, status
from fastapi.responses import FileResponse

from arducam_ptz.api.deps import AuthDep, ContextDep
from arducam_ptz.models import Photo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/captures", tags=["captures"], dependencies=[AuthDep])


@router.get("", response_model=list[Photo], summary="List captured photos (newest first)")
def list_captures(
    context: ContextDep,
    limit: int = Query(default=200, ge=1, le=1000, description="Maximum photos to return."),
) -> list[Photo]:
    return context.captures.list(limit=limit)


@router.post(
    "",
    response_model=Photo,
    status_code=status.HTTP_201_CREATED,
    summary="Take a photo at full sensor resolution",
)
def capture(context: ContextDep) -> Photo:
    """Blocks for ~0.5 s (sensor mode switch) while the preview keeps streaming."""
    return context.captures.capture()


@router.get(
    "/{filename}",
    summary="Fetch a photo, optionally downscaled",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def get_capture(
    filename: str,
    context: ContextDep,
    max_dim: int | None = Query(
        default=None,
        ge=16,
        le=2048,
        description="Longest edge in pixels; serves a cached thumbnail. Omit for the original.",
    ),
) -> FileResponse:
    path = context.captures.resolve(filename, max_dim=max_dim)
    return FileResponse(
        path,
        media_type="image/jpeg",
        filename=path.name,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.delete(
    "/{filename}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a photo and its thumbnails",
)
def delete_capture(filename: str, context: ContextDep) -> None:
    context.captures.delete(filename)
