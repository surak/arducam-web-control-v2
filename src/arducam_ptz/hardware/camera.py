"""Camera backends: the real IMX519 through picamera2, and a simulated one.

Measured facts behind the choices here (see docs/HARDWARE.md for the numbers):

* ``JpegEncoder`` (libjpeg via simplejpeg) produces JFIF frames - ``ff d8 ff e0`` -
  which every browser, including iOS Safari, decodes. The hardware
  ``MJPEGEncoder`` produces ``ff d8 ff fe`` frames; they are cheaper for the Pi
  but less portable, so the software encoder is the default and the difference is
  documented rather than discovered on a phone.
* Taking a 16 MP still with ``switch_mode_and_capture_file`` *while the preview
  keeps running* costs ~0.55 s and the preview resumes by itself. Stopping the
  recording, capturing and restarting (what the old app did) costs 2.7 s and
  occasionally left the stream dead. Hence: never stop the stream to take a photo.
* The encoder callback signature has changed between picamera2 versions (4 and 6
  arguments have both been seen). The sink below accepts ``*extra, **kwargs`` so a
  library upgrade cannot break the stream with a TypeError mid-recording.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from arducam_ptz.config import CameraSettings
from arducam_ptz.hardware.base import Availability, CameraHardware, FrameSink, sink_frame_age

try:  # pragma: no cover - import guard so the rest of the app runs on a laptop
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder, Quality
    from picamera2.outputs import Output
except ImportError:  # pragma: no cover
    Picamera2 = None  # type: ignore[assignment,misc]
    JpegEncoder = Quality = Output = None  # type: ignore[assignment,misc]

try:  # pragma: no cover
    from PIL import Image, ImageFilter
except ImportError:  # pragma: no cover
    Image = ImageFilter = None  # type: ignore[assignment,misc]


class _HubOutput(Output if Output is not None else object):  # type: ignore[misc]
    """Adapter from picamera2's encoder callback to our :class:`FrameSink`.

    picamera2 calls ``outputframe`` with the encoded JPEG plus a few positional
    arguments whose count has changed across releases, which is why they are
    swallowed in ``*extra``. Only the JPEG matters to us.
    """

    def __init__(self, sink: FrameSink, width: int, height: int, logger: logging.Logger) -> None:
        if Output is not None:
            super().__init__()
        self.sink = sink
        self.width = width
        self.height = height
        self.log = logger

    def outputframe(
        self,
        frame: Any,
        keyframe: bool = True,
        timestamp: float | None = None,
        *extra: Any,
        **kwargs: Any,
    ) -> None:
        # Signature is tolerant on purpose: different picamera2 versions call an
        # output object with (frame), (frame, keyframe, timestamp) or
        # (array, config, timestamps, file, fname, camera). *extra/**kwargs absorb the
        # difference instead of pinning one version. Unused args of a third-party
        # callback are expected here, hence the ARG002 exemption for this file.
        #
        # The published timestamp is the *arrival* time from time.monotonic(), not the
        # sensor timestamp passed in: the sweep compares frame times against local
        # clock readings ("did this frame arrive after the lens moved?"), and sensor
        # timestamps are in a different domain entirely.
        del keyframe, timestamp  # accepted for compatibility with picamera2
        try:
            self.sink.publish(
                bytes(frame),
                width=self.width,
                height=self.height,
                timestamp=time.monotonic(),
            )
        except Exception:  # pragma: no cover - never let a client error kill the encoder thread
            self.log.exception("frame sink rejected a frame")


class PicameraCamera(CameraHardware):
    """The single owner of the camera.

    One instance, one recording, N viewers: the fan-out to browsers happens in the
    frame hub. The old app started a *new* recording per MJPEG client and called
    ``camera.stop()`` when a client disconnected - with two tabs open, the second
    tab took the camera away from the first, and the log filled with "Camera must
    be stopped" errors.
    """

    def __init__(
        self,
        settings: CameraSettings,
        sink: FrameSink,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self._sink = sink
        self._camera: Picamera2 | None = None
        self._lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._recording = False
        self._busy = False
        self._available = False
        self._detail: str | None = None
        self._sensor_resolution: tuple[int, int] | None = None
        self._restarts = 0
        self._stopping = threading.Event()
        self._supervisor: threading.Thread | None = None

    # ------------------------------------------------------------------ life
    def _require_library(self) -> None:
        if Picamera2 is None:
            raise RuntimeError(
                "picamera2 is not importable - on a Raspberry Pi install it with apt "
                "(python3-picamera2) and run the server from a venv created with "
                "`uv venv --system-site-packages`"
            )

    def open(self) -> bool:
        """Create the camera object and read the sensor's native resolution."""
        with self._lock:
            try:
                self._require_library()
                self._camera = Picamera2()
                info = self._camera.camera_properties
                self._sensor_resolution = tuple(info["PixelArraySize"])  # type: ignore[assignment]
                self._available = True
                self._detail = None
                self.log.info(
                    "camera ready: %s, sensor %dx%d",
                    info.get("Model", "unknown"),
                    self._sensor_resolution[0],
                    self._sensor_resolution[1],
                )
                return True
            except Exception as exc:  # libcamera raises a variety of types
                self._available = False
                self._detail = f"{type(exc).__name__}: {exc}"
                self.log.error("camera unavailable: %s", self._detail)
                self._camera = None
                return False

    def availability(self) -> Availability:
        if self._available:
            return Availability.ok(self._detail or "camera connected")
        return Availability.missing(self._detail or "camera not initialised")

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def restarts(self) -> int:
        return self._restarts

    def sensor_resolution(self) -> tuple[int, int] | None:
        return self._sensor_resolution

    # ------------------------------------------------------------------ preview
    def start(self) -> None:
        """Configure and start the preview, plus the stall supervisor."""
        with self._lock:
            if self._recording:
                return
            if self._camera is None and not self.open():
                raise RuntimeError(self._detail or "camera not initialised")
            assert self._camera is not None
            try:
                config = self._camera.create_video_configuration(
                    main={
                        "size": (self.settings.preview_width, self.settings.preview_height),
                        "format": "RGB888",
                    },
                    buffer_count=self.settings.buffer_count,
                )
                self._camera.configure(config)
                # quality=None keeps the explicit q= set on the encoder; passing a
                # Quality enum here would silently override it.
                self._camera.start_recording(
                    JpegEncoder(q=self.settings.preview_quality),
                    _HubOutput(
                        self._sink,
                        self.settings.preview_width,
                        self.settings.preview_height,
                        self.log,
                    ),
                    quality=None,
                )
                self._recording = True
                self._available = True
                self._detail = None
                self.log.info(
                    "preview streaming at %dx%d q%d",
                    self.settings.preview_width,
                    self.settings.preview_height,
                    self.settings.preview_quality,
                )
            except Exception as exc:
                self._recording = False
                self._available = False
                self._detail = f"preview failed to start: {type(exc).__name__}: {exc}"
                self.log.error(self._detail)
                raise RuntimeError(self._detail) from exc
        self._start_supervisor()

    def stop(self) -> None:
        """Stop the preview and release the camera. Never raises.

        ``stop_recording`` can block if the encoder thread has died (a known
        picamera2 failure mode), so stopping happens in a helper thread with a
        deadline: an unreachable camera must not wedge the shutdown.
        """
        self._stopping.set()
        supervisor = self._supervisor
        if supervisor is not None and supervisor.is_alive():
            supervisor.join(timeout=2.0)
        self._supervisor = None
        with self._lock:
            camera, self._camera = self._camera, None
            # Read the flag *before* clearing it: _stop_camera needs to know
            # whether there is a recording to stop.
            was_recording = self._recording
            self._recording = False
            self._available = False
        if camera is not None:
            self._stop_camera(camera, was_recording=was_recording)
        self._stopping.clear()

    def _stop_camera(self, camera: Any, *, was_recording: bool, timeout_s: float = 5.0) -> None:
        """Stop recording and close, in a thread we are willing to abandon."""
        done = threading.Event()

        def shutdown() -> None:
            try:
                if was_recording:
                    camera.stop_recording()
            except Exception as exc:  # pragma: no cover
                self.log.warning("stop_recording failed: %s", exc)
            try:
                camera.close()
            except Exception as exc:  # pragma: no cover
                self.log.warning("camera close failed: %s", exc)
            finally:
                done.set()

        worker = threading.Thread(target=shutdown, name="camera-shutdown", daemon=True)
        worker.start()
        if not done.wait(timeout_s):
            self.log.error("camera shutdown did not finish in %.0fs; abandoning it", timeout_s)

    # ------------------------------------------------------------------ stills
    def capture_still(self, path: Path) -> tuple[int, int]:
        """Capture a full-resolution JPEG *without* interrupting the preview."""
        with self._capture_lock:  # one capture at a time
            with self._lock:
                if self._camera is None or not self._recording:
                    raise RuntimeError("preview is not running, cannot capture")
                camera = self._camera
                width, height = self._capture_dimensions()
                still_config = camera.create_still_configuration(main={"size": (width, height)})
                self._busy = True
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                started = time.monotonic()
                # switch_mode_and_capture_file temporarily changes sensor mode and
                # returns the camera to the preview configuration afterwards.
                camera.switch_mode_and_capture_file(still_config, str(path))
                self.log.info(
                    "captured %s (%dx%d) in %.2fs",
                    path.name,
                    width,
                    height,
                    time.monotonic() - started,
                )
                return width, height
            finally:
                self._busy = False

    def _capture_dimensions(self) -> tuple[int, int]:
        """Requested still size, clamped to the sensor, or the native mode."""
        native = self._sensor_resolution or (self.settings.preview_width, self.settings.preview_height)
        width = self.settings.capture_width or native[0]
        height = self.settings.capture_height or native[1]
        # Keep the sensor's aspect ratio if a client asks for something odd; a
        # mismatched aspect is silently cropped or stretched by libcamera.
        if (width, height) != native:
            scale = min(width / native[0], height / native[1])
            width = max(16, int(native[0] * scale) // 2 * 2)
            height = max(16, int(native[1] * scale) // 2 * 2)
        # Never exceed what the sensor can do.
        width = min(width, native[0])
        height = min(height, native[1])
        return width, height

    # ------------------------------------------------------------------ supervisor
    def _start_supervisor(self) -> None:
        """Watch for a stalled stream and restart it (cable glitches happen).

        The old app needed a manual restart after such a stall; on a rig mounted
        on a wall, nobody is going to do that.
        """
        if not self.settings.stall_timeout_s or self._supervisor is not None:
            return
        self._supervisor = threading.Thread(
            target=self._supervise,
            name="camera-supervisor",
            daemon=True,
        )
        self._supervisor.start()

    def _supervise(self) -> None:
        while not self._stopping.wait(1.0):
            if not self._recording:
                continue
            age = sink_frame_age(self._sink)
            if age is None or age < self.settings.stall_timeout_s:
                continue
            if self._restarts >= self.settings.max_restarts:
                self._detail = f"preview stalled and gave up after {self._restarts} restarts"
                self.log.error(self._detail)
                self._available = False
                return
            self._restarts += 1
            delay = min(30.0, 2.0**self._restarts)
            self.log.warning(
                "no preview frame for %.1fs - restarting camera (attempt %d, next delay %.0fs)",
                age,
                self._restarts,
                delay,
            )
            self._restart_preview(delay)

    def _restart_preview(self, delay_s: float) -> None:
        """Tear the recording down and bring it back, backing off between tries."""
        with self._lock:
            camera = self._camera
            was_recording = self._recording
            self._camera = None
            self._recording = False
        if camera is not None:
            self._stop_camera(camera, was_recording=was_recording)
        self._stopping.wait(delay_s)
        if self._stopping.is_set():
            return
        try:
            self._stopping.clear()
            if self.open():
                self.start()
        except RuntimeError as exc:
            self.log.error("preview restart failed: %s", exc)


class SimulatedCamera(CameraHardware):
    """Generates preview frames without a camera - and makes autofocus testable.

    The picture is a synthetic target blurred according to the *simulated focus
    motor's* position, so the autofocus sweep in ``services/focus.py`` is exercised
    for real in the test suite: it measures generated JPEGs, finds the sharp
    position, and the assertion is about the position it chose - not about a
    mocked return value.
    """

    def __init__(
        self,
        settings: CameraSettings,
        sink: FrameSink,
        blur_source: Any | None = None,
        logger: logging.Logger | None = None,
        fps: float = 12.0,
    ) -> None:
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self._sink = sink
        self._blur_source = blur_source  # anything with .blur_radius() -> float
        self._fps = fps
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._busy = False
        self._lock = threading.Lock()
        self._base: Any | None = None
        self._size = (min(settings.preview_width, 640), min(settings.preview_height, 360))

    def availability(self) -> Availability:
        return Availability.fake("simulated camera")

    def sensor_resolution(self) -> tuple[int, int] | None:
        return (4656, 3496)

    @property
    def busy(self) -> bool:
        return self._busy

    def _render(self) -> bytes:
        """Render one frame: a high-contrast target, blurred by focus error."""
        if Image is None:  # pragma: no cover - Pillow is a hard dependency
            raise RuntimeError("Pillow is required for the simulated camera")
        if self._base is None:
            self._base = self._build_target()
        image = self._base
        radius = float(self._blur_source.blur_radius()) if self._blur_source is not None else 0.0
        if radius > 0.05:
            image = image.filter(ImageFilter.GaussianBlur(radius=radius))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=max(40, min(95, self.settings.preview_quality)))
        return buffer.getvalue()

    def _build_target(self) -> Any:
        """A Siemens-star-ish target: lots of edges, so blur has a clear cost."""
        width, height = self._size
        ys, xs = np.mgrid[0:height, 0:width]
        cx, cy = width / 2.0, height / 2.0
        angle = np.arctan2(ys - cy, xs - cx)
        bars = ((angle * 16 / np.pi) % 2.0 < 1.0).astype(np.uint8)
        radius = np.hypot(xs - cx, ys - cy)
        # Keep the outermost ring flat so the star has a frame around it.
        bars[radius > min(cx, cy) * 0.9] = 0
        image = (bars * 255).astype(np.uint8)
        return Image.fromarray(image, mode="L").convert("RGB")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="simulated-camera", daemon=True)
        self._thread.start()
        self.log.info("simulated camera streaming at %dx%d", *self._size)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        period = 1.0 / self._fps
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                jpeg = self._render()
                self._sink.publish(
                    jpeg,
                    width=self._size[0],
                    height=self._size[1],
                    timestamp=time.monotonic(),
                )
            except Exception:  # pragma: no cover
                self.log.exception("simulated camera failed to render a frame")
            self._stop.wait(max(0.0, period - (time.monotonic() - started)))

    def capture_still(self, path: Path) -> tuple[int, int]:
        """Writes a real JPEG (of the synthetic target) so gallery code is exercised."""
        with self._lock:
            self._busy = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            width = self.settings.capture_width or 4656
            height = self.settings.capture_height or 3496
            # Small file, honest dimensions: the gallery/thumbnail pipeline cares
            # about the size, and a real 16 MP render would only slow the tests.
            image = self._build_target().resize((640, 480))
            image.save(path, format="JPEG", quality=85)
            return width, height
        finally:
            with self._lock:
                self._busy = False
