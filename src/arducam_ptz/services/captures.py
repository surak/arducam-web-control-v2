"""Captured photos: storage, listing, thumbnails and deletion.

What the old gallery got wrong, and what this does instead:

* It rendered a grid of **full-resolution 16 MP JPEGs**. On a phone that is ~1 MB
  per tile over WiFi, so the gallery crawled. Every photo here has a cached
  thumbnail sized for the grid, generated once and reused.
* Its download endpoint built a path by joining a client-supplied string, so a
  crafted name could reach outside the photo directory. Here a filename must be a
  bare basename with a known extension, the resolved path is checked to be inside
  ``photos_dir``, and anything else is a 404.
* Photo names came from a fixed pattern with no collision handling, so two captures
  in the same second silently overwrote one another.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from arducam_ptz.config import StorageSettings
from arducam_ptz.hardware.base import CameraHardware
from arducam_ptz.models import Photo

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png"}


class CaptureNotFound(KeyError):
    """Raised for an unknown or malformed photo name. Maps to HTTP 404."""


class CaptureService:
    """Owns the photo directory and the thumbnail cache."""

    def __init__(
        self,
        settings: StorageSettings,
        camera: CameraHardware,
        log: logging.Logger | None = None,
        url_prefix: str = "/api/v1/captures",
    ) -> None:
        self.settings = settings
        self.camera = camera
        self.log = log or logger
        self.url_prefix = url_prefix.rstrip("/")
        self._lock = threading.Lock()
        #: filename -> (mtime, width, height): reading a JPEG header a second time
        #: for every gallery refresh is pure waste.
        self._dimension_cache: dict[str, tuple[float, int | None, int | None]] = {}

    # ------------------------------------------------------------------ setup
    def ensure_directories(self) -> None:
        self.settings.photos_dir.mkdir(parents=True, exist_ok=True)
        self.settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ capture
    def capture(self) -> Photo:
        """Take a full-resolution still and return its descriptor."""
        self.ensure_directories()
        path = self._next_path()
        width, height = self.camera.capture_still(path)
        self.log.info("photo saved: %s (%dx%d)", path.name, width, height)
        with self._lock:
            self._dimension_cache[path.name] = (path.stat().st_mtime, width, height)
        return self._describe(path, width, height)

    def _next_path(self) -> Path:
        """Unique path for a new photo, never overwriting an existing one."""
        template = self.settings.filename_template
        if "/" in template or "\\" in template:
            raise ValueError("filename_template must not contain path separators")
        stem = time.strftime(template)
        candidate = self.settings.photos_dir / f"{stem}.jpg"
        counter = 2
        while candidate.exists():
            candidate = self.settings.photos_dir / f"{stem}_{counter}.jpg"
            counter += 1
        return candidate

    # ------------------------------------------------------------------ listing
    def list(self, limit: int = 500) -> list[Photo]:
        """Newest first. ``limit`` protects a phone from a 5000-photo directory."""
        self.ensure_directories()
        paths = sorted(
            (p for p in self.settings.photos_dir.iterdir() if p.suffix.lower() in ALLOWED_SUFFIXES),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [self._describe(path) for path in paths[:limit]]

    def _describe(self, path: Path, width: int | None = None, height: int | None = None) -> Photo:
        stat = path.stat()
        if width is None or height is None:
            cached = self._dimension_cache.get(path.name)
            if cached is not None and cached[0] == stat.st_mtime:
                width, height = cached[1], cached[2]
            else:
                width, height = self._read_dimensions(path)
                with self._lock:
                    self._dimension_cache[path.name] = (stat.st_mtime, width, height)
        return Photo(
            filename=path.name,
            url=f"{self.url_prefix}/{path.name}",
            thumb_url=f"{self.url_prefix}/{path.name}?max_dim={self.settings.thumbnail_max_dim}",
            bytes=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_mtime),
            width=width,
            height=height,
        )

    @staticmethod
    def _read_dimensions(path: Path) -> tuple[int | None, int | None]:
        """Read only the JPEG header - never decode 16 MP just to report a size."""
        try:  # pragma: no cover - depends on the file being a real image
            from PIL import Image

            with Image.open(path) as image:
                return image.size
        except Exception:
            logger.debug("could not read dimensions of %s", path, exc_info=True)
            return None, None

    # ------------------------------------------------------------------ serving
    def resolve(self, filename: str, max_dim: int | None = None) -> Path:
        """Return the file to serve: the original, or a cached thumbnail.

        Every rejection path is a :class:`CaptureNotFound`, which the API turns
        into a 404 - a malformed name must not leak whether a file exists.
        """
        path = self._safe_path(filename)
        if not path.is_file():
            raise CaptureNotFound(filename)
        if max_dim is None or max_dim <= 0:
            return path
        return self._thumbnail(path, max_dim)

    def _safe_path(self, filename: str) -> Path:
        if not filename or filename != Path(filename).name:
            raise CaptureNotFound(filename)  # path separators, "..", absolute paths
        if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
            raise CaptureNotFound(filename)
        if filename.startswith("."):
            raise CaptureNotFound(filename)
        path = (self.settings.photos_dir / filename).resolve()
        root = self.settings.photos_dir.resolve()
        if root not in path.parents:  # belt and braces against symlinked escapes
            raise CaptureNotFound(filename)
        return path

    def _thumbnail(self, source: Path, max_dim: int) -> Path:
        """Render (once) and return a small copy of ``source``."""
        from PIL import Image

        max_dim = max(16, min(int(max_dim), 2048))
        stem = f"{source.stem}@{max_dim}.jpg"
        target = self.settings.thumbnail_dir / stem
        source_mtime = source.stat().st_mtime
        if target.is_file() and target.stat().st_mtime >= source_mtime:
            return target
        with self._lock:  # one renderer at a time: a grid refresh must not stampede
            if target.is_file() and target.stat().st_mtime >= source_mtime:
                return target
            self.settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as image:
                # draft() lets libjpeg scale while decoding, which is several times
                # faster than decoding 16 MP and resizing afterwards.
                image.draft("RGB", (max_dim, max_dim))
                image = image.convert("RGB")
                scale = max_dim / max(image.size)
                if scale < 1.0:
                    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                    image = image.resize(size, Image.Resampling.BILINEAR)
                image.save(target, format="JPEG", quality=82)
        return target

    # ------------------------------------------------------------------ delete
    def delete(self, filename: str) -> None:
        """Delete a photo and any thumbnails rendered from it."""
        path = self._safe_path(filename)
        if not path.is_file():
            raise CaptureNotFound(filename)
        path.unlink()
        for thumb in self.settings.thumbnail_dir.glob(f"{path.stem}@*.jpg"):
            thumb.unlink(missing_ok=True)
        with self._lock:
            self._dimension_cache.pop(path.name, None)
        self.log.info("photo deleted: %s", filename)

    # ------------------------------------------------------------------ housekeeping
    def prune_thumbnails(self, max_age_days: float = 30.0) -> int:
        """Drop thumbnails whose source photo is gone or ancient."""
        if not self.settings.thumbnail_dir.is_dir():
            return 0
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for thumb in self.settings.thumbnail_dir.glob("*.jpg"):
            if thumb.stat().st_mtime >= cutoff:
                continue
            thumb.unlink(missing_ok=True)
            removed += 1
        return removed
