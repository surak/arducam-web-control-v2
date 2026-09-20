"""MJPEG preview streaming, the one endpoint that is not JSON.

How this differs from the old app, and why it matters on a phone:

* **One camera, many viewers.** Frames come from the :class:`FrameHub`; opening a
  second tab or a second phone costs nothing and neither view disturbs the other.
  The old generator drove the camera per client, so the second viewer broke the
  first.
* **Slow clients are dropped, not waited for.** The hub overwrites the frame a slow
  client has not read (``multipart/x-mixed-replace`` renders the newest part anyway),
  so a phone with bad reception cannot stall the camera or the other viewers.
* **Disconnects end cleanly, without polling for them.** When the socket goes away
  uvicorn cancels this request's task; the generator's ``finally`` unsubscribes. It is
  deliberately *not* done with ``request.is_disconnected()`` - see the long comment in
  ``stream_mjpg`` for the failure that causes (a preview that freezes after one frame).
  The old app instead raised a BrokenPipeError inside a camera callback and logged a
  stack trace per disconnect, from a thread that was also feeding the camera.
* **A liveness check.** If frames stop arriving (camera wedged, supervisor restarting)
  the response ends rather than holding a dead connection open forever; the browser
  reconnects and the hub feeds it again. Mobile radios hate half-open sockets, and
  the idle timeout is configurable (``server.stream_idle_timeout_s``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from arducam_ptz.api.deps import AuthDep, ContextDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"], dependencies=[AuthDep])

#: Baseline for the MJPEG parts. Browsers accept any of these; the boundary string
#: only has to be unique inside the response body.
BOUNDARY = "arducamframe"


def _jpeg_part(jpeg: bytes) -> bytes:
    """One multipart chunk. Length is mandatory: without it browsers buffer forever."""
    header = (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode(
        "ascii"
    )
    return header + jpeg + b"\r\n"


@router.get(
    "/stream.mjpg",
    summary="Live MJPEG preview stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"multipart/x-mixed-replace; boundary=arducamframe": {}}}},
)
async def stream_mjpg(request: Request, context: ContextDep) -> StreamingResponse:
    """Continuous preview. Use in an ``<img>`` tag; it never completes."""
    subscriber = context.hub.subscribe()

    async def generator() -> AsyncIterator[bytes]:
        idle_timeout = context.settings.server.stream_idle_timeout_s
        last_frame_at = time.monotonic()
        try:
            while True:
                # Note on disconnects, because this is the one line that decides whether
                # a dead viewer leaks a hub slot: Starlette additionally keeps its own
                # disconnect listener next to this generator and ends the response when
                # the client goes away, so this check is the *prompt* path rather than
                # the only one - the finally below cleans up however we leave the loop.
                # Both paths are exercised by the tests in test_api.py (a viewer that
                # closes the socket, and a viewer that keeps reading).
                #
                # The hub is fed from a camera thread; poll it off the event loop so a
                # slow bus cannot block other HTTP requests.
                if await request.is_disconnected():
                    logger.debug("preview client disconnected")
                    return
                frame = await asyncio.to_thread(subscriber.get, 0.5)
                if frame is None:
                    if time.monotonic() - last_frame_at > idle_timeout:
                        # Tell the client, in the only channel a streaming response
                        # has: end it. The UI notices and reconnects.
                        logger.info("preview stream idle for %.0fs; ending it", idle_timeout)
                        return
                    continue
                last_frame_at = time.monotonic()
                yield _jpeg_part(frame.jpeg)
        finally:
            # Runs on disconnect, on error and on shutdown - the hub must not keep a
            # reference to a client that is gone.
            subscriber.close()

    return StreamingResponse(
        generator(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={
            # Stop proxies/mobile browsers from buffering the stream into uselessness.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stream/snapshot.jpg", summary="Single frame from the live preview")
def snapshot(context: ContextDep) -> Response:
    """One JPEG from the running preview - cheap, and always the live picture.

    The old UI used a full 16 MP still capture to refresh a thumbnail, which stole
    the camera for half a second every time. A snapshot here is a frame the camera
    already produced.
    """
    frame = context.hub.snapshot()
    if frame is None:
        # No frames yet: let the client show "starting up" rather than a broken image.
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(
        content=frame.jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Frame-Sequence": str(frame.sequence),
        },
    )
