"""Frame fan-out: one camera, many viewers, no blocking.

The old app served MJPEG by starting a *generator per browser tab* that drove the
camera itself. Two tabs therefore fought over the device, a slow phone link stalled
the producer for everyone, and every client disconnect logged a camera error. Here
the camera publishes into a hub and subscribers only ever read the newest JPEG.

Design rules:

* ``publish`` runs on the encoder thread and must never block or raise.
* Each subscriber owns a queue of length 1: if a client is too slow to consume a
  frame, the *old* frame is dropped. Video is a "latest value" problem, not a
  queue problem.
* ``wait_for_frame`` lets the autofocus sweep demand a frame captured *after* the
  lens moved, rather than scoring whatever happened to be in the buffer.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import TracebackType

logger = logging.getLogger(__name__)

#: How many recent frames to remember when computing fps/bandwidth.
_STATS_WINDOW = 64


@dataclass(frozen=True, slots=True)
class Frame:
    """One encoded preview frame.

    ``timestamp`` is the moment the frame *arrived* on this host, on the same
    ``time.monotonic()`` clock the rest of the process uses. It is deliberately not
    the sensor/encoder timestamp: those live in a different clock domain, so
    comparing them with local clocks (which is what "was this frame taken after the
    lens moved?" needs) silently answers the wrong question.
    """

    jpeg: bytes
    width: int
    height: int
    timestamp: float
    sequence: int

    @property
    def kilobyte_size(self) -> float:
        return len(self.jpeg) / 1024.0


class Subscriber:
    """A viewer of the stream. Use as a context manager, or call close()."""

    def __init__(self, hub: FrameHub) -> None:
        self._hub = hub
        self._queue: queue.Queue[Frame] = queue.Queue(maxsize=1)
        self._closed = False
        self.dropped = 0
        self.delivered = 0

    # ---------------------------------------------------------------- feeding
    def offer(self, frame: Frame) -> None:
        """Hand a frame to this subscriber, dropping the previous one if unread."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # The viewer is behind. Replace what it has not read yet: for a live
            # view, the newest frame is the only interesting one.
            try:
                self._queue.get_nowait()
                self.dropped += 1
                self._queue.put_nowait(frame)
            except (queue.Empty, queue.Full):  # pragma: no cover - race with the reader
                pass

    def get(self, timeout: float = 1.0) -> Frame | None:
        """Return the next frame, or None on timeout (caller then re-checks liveness)."""
        try:
            frame = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        self.delivered += 1
        return frame

    def close(self) -> None:
        self._closed = True
        self._hub.unsubscribe(self)

    # ------------------------------------------------------------ context mgr
    def __enter__(self) -> Subscriber:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class FrameHub:
    """Publishes the newest frame to any number of subscribers."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self.log = log or logger
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._subscribers: set[Subscriber] = set()
        self._sequence = 0
        self._history: deque[tuple[float, int]] = deque(maxlen=_STATS_WINDOW)

    # ---------------------------------------------------------------- producer
    def publish(self, jpeg: bytes, *, width: int, height: int, timestamp: float) -> None:
        """Called by the camera for every encoded frame. Never blocks, never raises."""
        with self._lock:
            self._sequence += 1
            frame = Frame(
                jpeg=jpeg,
                width=width,
                height=height,
                timestamp=timestamp,
                sequence=self._sequence,
            )
            self._latest = frame
            self._history.append((time.monotonic(), len(jpeg)))
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.offer(frame)

    # ---------------------------------------------------------------- consumer
    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(self)
        with self._lock:
            self._subscribers.add(subscriber)
        self.log.debug("subscriber joined (%d watching)", len(self._subscribers))
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
        self.log.debug("subscriber left (%d watching)", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def snapshot(self) -> Frame | None:
        """The most recent frame, without subscribing. Used for one-shot snapshots."""
        with self._lock:
            return self._latest

    def last_frame_age(self) -> float | None:
        """Seconds since the newest frame arrived (None when none ever did)."""
        with self._lock:
            latest = self._latest
        return None if latest is None else max(0.0, time.monotonic() - latest.timestamp)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def wait_for_frame(self, after_sequence: int | None = None, timeout: float = 1.0) -> Frame | None:
        """Block until a frame newer than ``after_sequence`` exists.

        The autofocus sweep uses this: after moving the lens it must score a frame
        that was *taken after* the move. A stale frame would score the previous
        focus position and send the sweep the wrong way - which is exactly the
        class of bug that makes a focus hunt oscillate.

        Polls rather than using a condition variable: sampling happens at most a
        few dozen times per sweep, and polling keeps the producer's hot path free
        of extra locking.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                latest = self._latest
            if latest is not None and (after_sequence is None or latest.sequence > after_sequence):
                return latest
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def sample_after(self, since: float, timeout: float = 1.0, skip: int = 0) -> Frame | None:
        """Newest frame that arrived after ``since``, ignoring the first ``skip``.

        ``since`` is a ``time.monotonic()`` value (typically the moment the lens was
        commanded). Written for the autofocus sweep, which must never score a frame
        whose pixels predate its own move.

        Why the ``skip`` parameter exists: a sequence number is assigned when a frame
        is *published*, not when it is *exposed*. A frame already in the encoder when
        the lens moves is published right afterwards - newer sequence, older pixels.
        Skipping the first ``skip`` frames that arrive after the move lets the
        pipeline flush. Without this, a sweep can score the pre-move picture as if it
        belonged to the new position, which is how a focus hunt ends up at the wrong
        end of its travel.
        """
        deadline = time.monotonic() + timeout
        accepted = 0
        last_sequence: int | None = None
        while True:
            with self._lock:
                frame = self._latest
            if frame is not None and frame.timestamp > since and frame.sequence != last_sequence:
                last_sequence = frame.sequence
                accepted += 1
                if accepted > skip:
                    return frame
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    # ---------------------------------------------------------------- reporting
    def stats(self) -> dict[str, float | int | bool | None]:
        """Frame rate, bandwidth and viewer count for the health endpoint."""
        with self._lock:
            history = tuple(self._history)
            latest = self._latest
            subscribers = len(self._subscribers)
        now = time.monotonic()
        # Only the last 3 seconds count, so the numbers react to a stall instead of
        # averaging it away over the whole session.
        window = [(t, size) for t, size in history if now - t <= 3.0]
        span = (window[-1][0] - window[0][0]) if len(window) > 1 else 0.0
        fps = (len(window) - 1) / span if span > 0 else 0.0
        kbps = (sum(size for _, size in window) / 1024.0 / span) if span > 0 else 0.0
        age = None if latest is None else max(0.0, now - latest.timestamp)
        return {
            "running": latest is not None and (age or 0) < 5.0,
            "width": latest.width if latest else 0,
            "height": latest.height if latest else 0,
            "fps": round(fps, 1),
            "kilobytes_per_s": round(kbps, 1),
            "subscribers": subscribers,
            "sequence": latest.sequence if latest else 0,
            "last_frame_age_s": None if age is None else round(age, 2),
        }
