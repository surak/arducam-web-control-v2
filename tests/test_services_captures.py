"""Capture service tests.

The security-relevant part (can a client-supplied name escape the photo directory?)
and the phone-relevant part (are thumbnails small?) are both covered here.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from arducam_ptz.config import Settings, StorageSettings
from arducam_ptz.hardware.base import Availability, CameraHardware
from arducam_ptz.services.captures import CaptureNotFound, CaptureService


class FakeCamera(CameraHardware):
    """Writes a real JPEG of the requested size, like the real camera does."""

    def __init__(self, size: tuple[int, int] = (640, 480)) -> None:
        self.size = size
        self.captures = 0

    def availability(self) -> Availability:
        return Availability.ok("fake camera")

    def start(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def busy(self) -> bool:
        return False

    def sensor_resolution(self) -> tuple[int, int] | None:
        return self.size

    def capture_still(self, path: Path) -> tuple[int, int]:
        self.captures += 1
        width, height = self.size
        pixels = np.random.default_rng(0).integers(0, 255, (height, width, 3), dtype=np.uint8)
        Image.fromarray(pixels).save(path, format="JPEG", quality=90)
        return width, height


@pytest.fixture
def service(tmp_path: Path) -> CaptureService:
    settings = StorageSettings(photos_dir=tmp_path / "photos", thumbnail_dir=tmp_path / "thumbs")
    return CaptureService(settings, FakeCamera())


class TestCapture:
    def test_capture_creates_a_file_and_describes_it(self, service: CaptureService) -> None:
        photo = service.capture()
        assert photo.filename.endswith(".jpg")
        assert photo.bytes > 0
        assert (photo.width, photo.height) == (640, 480)
        assert photo.url.endswith(photo.filename)
        assert "max_dim" in photo.thumb_url
        assert (service.settings.photos_dir / photo.filename).is_file()

    def test_directories_are_created_on_demand(self, service: CaptureService) -> None:
        assert not service.settings.photos_dir.exists()
        service.capture()
        assert service.settings.photos_dir.is_dir()
        assert service.settings.thumbnail_dir.is_dir()

    def test_two_captures_in_the_same_second_do_not_overwrite(self, service: CaptureService) -> None:
        """The old app's fixed filename silently replaced the previous photo."""
        first = service.capture()
        second = service.capture()
        assert first.filename != second.filename
        assert len(service.list()) == 2

    def test_list_is_newest_first(self, service: CaptureService) -> None:
        first = service.capture()
        time.sleep(0.01)
        second = service.capture()
        listed = service.list()
        assert [photo.filename for photo in listed] == [second.filename, first.filename]

    def test_list_ignores_non_photo_files(self, service: CaptureService) -> None:
        service.capture()
        (service.settings.photos_dir / "notes.txt").write_text("not a photo")
        (service.settings.photos_dir / ".hidden.jpg").write_text("hidden")
        assert [photo.filename for photo in service.list()] == [
            photo.filename for photo in service.list() if photo.filename.endswith(".jpg")
        ]
        assert all(photo.filename.endswith(".jpg") for photo in service.list())

    def test_list_limit_is_respected(self, service: CaptureService) -> None:
        for _ in range(5):
            service.capture()
        assert len(service.list(limit=2)) == 2

    def test_template_with_a_path_separator_is_rejected(self, tmp_path: Path) -> None:
        settings = StorageSettings(
            photos_dir=tmp_path / "photos",
            thumbnail_dir=tmp_path / "thumbs",
            filename_template="sub/capture",
        )
        service = CaptureService(settings, FakeCamera())
        with pytest.raises(ValueError, match="path separators"):
            service.capture()


class TestServing:
    def test_resolve_returns_the_original(self, service: CaptureService) -> None:
        photo = service.capture()
        path = service.resolve(photo.filename)
        assert path.is_file() and path.name == photo.filename

    def test_resolve_renders_a_thumbnail(self, service: CaptureService) -> None:
        photo = service.capture()
        thumb = service.resolve(photo.filename, max_dim=160)
        assert thumb.is_file()
        with Image.open(thumb) as image:
            assert max(image.size) == 160
        assert thumb.stat().st_size < (service.settings.photos_dir / photo.filename).stat().st_size

    def test_thumbnail_is_cached_not_re_rendered(self, service: CaptureService) -> None:
        photo = service.capture()
        first = service.resolve(photo.filename, max_dim=160)
        mtime = first.stat().st_mtime
        second = service.resolve(photo.filename, max_dim=160)
        assert second == first
        assert second.stat().st_mtime == mtime

    def test_thumbnail_is_refreshed_when_the_photo_changes(self, service: CaptureService) -> None:
        photo = service.capture()
        thumb = service.resolve(photo.filename, max_dim=160)
        original = thumb.stat().st_mtime
        time.sleep(0.02)
        source = service.settings.photos_dir / photo.filename
        with Image.open(source) as image:
            image.resize((320, 240)).save(source, format="JPEG")  # newer mtime
        service.resolve(photo.filename, max_dim=160)
        assert thumb.stat().st_mtime != original

    @pytest.mark.parametrize(
        "name",
        [
            "../secrets.jpg",
            "sub/photo.jpg",
            "/etc/passwd",
            "..%2fescape.jpg",
            ".hidden.jpg",
            "photo.txt",
            "",
        ],
    )
    def test_malformed_names_are_rejected(self, service: CaptureService, name: str) -> None:
        """A client-supplied name must never reach outside the photo directory."""
        with pytest.raises(CaptureNotFound):
            service.resolve(name)

    def test_unknown_but_well_formed_name_is_a_404(self, service: CaptureService) -> None:
        with pytest.raises(CaptureNotFound):
            service.resolve("nope.jpg")

    def test_symlink_escaping_the_directory_is_rejected(self, service: CaptureService, tmp_path: Path) -> None:
        service.ensure_directories()
        target = tmp_path / "outside.jpg"
        Image.fromarray(np.zeros((10, 10, 3), dtype=np.uint8)).save(target, format="JPEG")
        link = service.settings.photos_dir / "link.jpg"
        link.symlink_to(target)
        with pytest.raises(CaptureNotFound):
            service.resolve("link.jpg")


class TestDelete:
    def test_delete_removes_photo_and_thumbnails(self, service: CaptureService) -> None:
        photo = service.capture()
        thumb = service.resolve(photo.filename, max_dim=160)
        service.delete(photo.filename)
        assert not (service.settings.photos_dir / photo.filename).exists()
        assert not thumb.exists()
        assert service.list() == []

    def test_deleting_an_unknown_photo_is_a_404(self, service: CaptureService) -> None:
        with pytest.raises(CaptureNotFound):
            service.delete("nope.jpg")

    def test_prune_thumbnails_drops_orphans(self, service: CaptureService) -> None:
        service.ensure_directories()
        stale = service.settings.thumbnail_dir / "gone@160.jpg"
        stale.write_bytes(b"x")
        old = time.time() - 40 * 86400
        import os

        os.utime(stale, (old, old))
        assert service.prune_thumbnails(max_age_days=30) == 1
        assert not stale.exists()


class TestCaptureServiceWiring:
    def test_url_prefix_is_configurable(self, tmp_path: Path) -> None:
        settings = StorageSettings(photos_dir=tmp_path / "p", thumbnail_dir=tmp_path / "t")
        service = CaptureService(settings, FakeCamera(), url_prefix="/custom/photos/")
        photo = service.capture()
        assert photo.url.startswith("/custom/photos/")
        assert photo.thumb_url.startswith("/custom/photos/")

    def test_full_settings_can_drive_it(self, tmp_path: Path) -> None:
        settings = Settings()
        settings.storage = StorageSettings(photos_dir=tmp_path / "p", thumbnail_dir=tmp_path / "t")
        service = CaptureService(settings.storage, FakeCamera())
        assert service.capture().bytes > 0

    def test_dimensions_are_cached_between_listings(self, service: CaptureService, monkeypatch) -> None:
        photo = service.capture()
        calls: list[str] = []
        original = CaptureService._read_dimensions

        def counting_read(path):
            calls.append(str(path))
            return original(path)

        monkeypatch.setattr(CaptureService, "_read_dimensions", staticmethod(counting_read))
        service.list()
        assert calls == []  # the cache from capture() is still valid
        assert photo.width == 640
