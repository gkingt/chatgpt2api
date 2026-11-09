from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
import zipfile
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from PIL import Image

import services.image_service as images
import services.image_storage_service as storage
import services.image_tags_service as tags


class ImageManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image_root = self.root / "images"
        self.thumbnail_root = self.root / "thumbnails"
        self.image_root.mkdir()
        self.thumbnail_root.mkdir()
        self.settings = {"mode": "local", "public_base_url": ""}
        self.config = SimpleNamespace(
            images_dir=self.image_root,
            image_thumbnails_dir=self.thumbnail_root,
            image_retention_days=30,
            base_url="http://app.test",
            get_image_storage_settings=lambda: dict(self.settings),
        )
        self.service = storage.ImageStorageService(self.root / "index.json")
        for target, value in [
            ("services.image_service.config", self.config),
            ("services.image_storage_service.config", self.config),
            ("services.image_service.image_storage_service", self.service),
            ("services.image_tags_service.TAGS_FILE", self.root / "tags.json"),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def image(self, rel="2026/11/09/image.png", age_days=0):
        path = self.image_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (3, 2), color="red").save(path)
        modified = time.time() - age_days * 86400
        os.utime(path, (modified, modified))
        thumbnail = self.thumbnail_root / f"{rel}.png"
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.write_bytes(b"thumbnail")
        tags.set_tags(rel, ["test"])
        return path

    def test_list_does_not_cleanup_or_read_full_image_payload(self):
        self.image(age_days=40)
        with (
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("full image read")),
            mock.patch.object(images, "cleanup_image_thumbnails", side_effect=AssertionError("cleanup in list")),
        ):
            result = images.list_images("http://app.test")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual((result["items"][0]["width"], result["items"][0]["height"]), (3, 2))
        self.assertEqual(result["items"][0]["tags"], ["test"])

    def test_repeated_list_uses_cached_index_and_throttles_directory_scans(self):
        self.image()
        self.service.list_items("")
        with (
            mock.patch.object(Path, "rglob", side_effect=AssertionError("repeated scan")),
            mock.patch.object(storage, "_read_json_object", side_effect=AssertionError("repeated JSON read")),
        ):
            self.assertEqual(len(self.service.list_items("")), 1)
            self.assertFalse(self.service.exists("missing.png"))
        self.image("external.png")
        self.assertEqual(len(self.service.list_items("", refresh=True)), 2)

    def test_delete_by_date_cleans_files_index_thumbnails_and_tags(self):
        old = "2026/10/01/old.jpg"
        recent = "2026/11/09/recent.png"
        self.image(old)
        self.image(recent)
        self.service.list_items("")
        result = images.delete_images(start_date="2026-10-01", end_date="2026-10-01", all_matching=True)
        self.assertEqual(result, {"removed": 1})
        self.assertFalse((self.image_root / old).exists())
        self.assertFalse((self.thumbnail_root / f"{old}.png").exists())
        self.assertNotIn(old, self.service.index_snapshot())
        self.assertNotIn(old, tags.load_tags())
        self.assertTrue((self.image_root / recent).exists())
        self.assertIn(recent, tags.load_tags())

    def test_delete_rejects_escape_before_deleting_valid_targets(self):
        path = self.image("keep.png")
        with self.assertRaises(HTTPException):
            images.delete_images(["keep.png", "../outside.png"])
        self.assertTrue(path.exists())

    def test_expiration_removes_all_image_formats_but_keeps_non_images(self):
        old = [f"2026/09/01/old{extension}" for extension in (".png", ".jpg", ".webp")]
        for rel in old:
            self.image(rel, age_days=40)
        current = self.image("2026/11/09/current.png")
        unrelated = self.image_root / "notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        os.utime(unrelated, (0, 0))
        self.assertEqual(images.cleanup_expired_images(), 3)
        self.assertTrue(current.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(set(self.service.index_snapshot()), {"2026/11/09/current.png"})
        self.assertEqual(set(tags.load_tags()), {"2026/11/09/current.png"})
        for rel in old:
            self.assertFalse((self.thumbnail_root / f"{rel}.png").exists())

    def test_gif_svg_archive_preview_and_expiration(self):
        from test.test_image26 import SVG, raster
        payloads = {"2026/10/01/a.svg": SVG, "2026/10/01/b.gif": raster("GIF", True)}
        for rel, payload in payloads.items():
            path = self.image_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            os.utime(path, (time.time() - 40 * 86400,) * 2)
            tags.set_tags(rel, ["test"])
        result = images.list_images("http://app.test")
        self.assertEqual(len(result["items"]), 2)
        response = images.get_thumbnail_response("2026/10/01/a.svg")
        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertEqual(images.get_image_download_response("2026/10/01/a.svg").headers["x-content-type-options"], "nosniff")
        thumbnail = images.ensure_thumbnail("2026/10/01/b.gif")
        self.assertEqual((self.image_root / "2026/10/01/b.gif").read_bytes(), payloads["2026/10/01/b.gif"])
        self.assertTrue(thumbnail.exists())
        buffer = images.download_images_zip(list(payloads))
        with zipfile.ZipFile(buffer) as archive:
            for rel, payload in payloads.items():
                self.assertEqual(archive.read(Path(rel).name), payload)
        buffer.close()
        self.assertEqual(images.cleanup_expired_images(), 2)
        self.assertEqual(tags.load_tags(), {})
        self.assertFalse(thumbnail.exists())
        self.assertEqual(self.service.index_snapshot(), {})

    def test_date_cleanup_includes_vector_and_animation(self):
        from test.test_image26 import SVG, raster
        for name, payload in [("old.svg", SVG), ("old.gif", raster("GIF", True))]:
            path = self.image_root / "2026/10/01" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.assertEqual(images.delete_images(end_date="2026-10-01", all_matching=True), {"removed": 2})

    def test_external_unsafe_svg_is_not_served_inline(self):
        (self.image_root / "unsafe.svg").write_bytes(b'<svg><script>alert(1)</script></svg>')
        with self.assertRaises(HTTPException) as caught:
            images.get_image_response("unsafe.svg")
        self.assertEqual(caught.exception.status_code, 422)

    def test_webdav_delete_failure_preserves_local_copy_and_index(self):
        path = self.image()
        self.service.list_items("")
        indexed = self.service.index_snapshot()
        rel = next(iter(indexed))
        indexed[rel].update(webdav=True, storage="both")
        self.service._save_index(indexed)
        with mock.patch.object(storage, "WebDAVClient") as client:
            client.return_value.delete.side_effect = storage.ImageStorageError("unavailable")
            with self.assertRaises(storage.ImageStorageError):
                images.delete_images([rel])
        self.assertTrue(path.exists())
        self.assertTrue(self.service.index_snapshot()[rel]["webdav"])
        self.assertIn(rel, tags.load_tags())
        self.assertTrue((self.thumbnail_root / f"{rel}.png").exists())

    def test_partial_batch_failure_cleans_successful_items_only(self):
        first = self.image("first.png")
        second = self.image("second.png")
        self.service.list_items("")
        indexed = self.service.index_snapshot()
        indexed["second.png"].update(webdav=True, storage="both")
        self.service._save_index(indexed)
        with mock.patch.object(storage, "WebDAVClient") as client:
            client.return_value.delete.side_effect = storage.ImageStorageError("unavailable")
            with self.assertRaises(storage.ImageStorageError):
                images.delete_images(["first.png", "second.png"])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertNotIn("first.png", self.service.index_snapshot())
        self.assertNotIn("first.png", tags.load_tags())
        self.assertFalse((self.thumbnail_root / "first.png.png").exists())
        self.assertIn("second.png", tags.load_tags())

    def test_date_delete_refreshes_externally_added_images(self):
        self.service.list_items("")
        path = self.image("2026/10/01/external.png")
        self.assertEqual(images.delete_images(end_date="2026-10-01", all_matching=True), {"removed": 1})
        self.assertFalse(path.exists())

    def test_webdav_only_expiration_deletes_remote_index_tags_and_thumbnail(self):
        rel = "2026/01/01/remote.png"
        self.service._save_index({rel: {"created_at": "2026-01-01 00:00:00", "webdav": True, "storage": "webdav"}})
        tags.set_tags(rel, ["remote"])
        thumbnail = self.thumbnail_root / f"{rel}.png"
        thumbnail.parent.mkdir(parents=True)
        thumbnail.touch()
        with mock.patch.object(storage, "WebDAVClient") as client:
            client.return_value.delete.return_value = False  # Already absent remotely.
            self.assertEqual(images.cleanup_expired_images(), 1)
            client.return_value.delete.assert_called_once_with(rel)
        self.assertEqual(self.service.index_snapshot(), {})
        self.assertEqual(tags.load_tags(), {})
        self.assertFalse(thumbnail.exists())

    def test_disk_cleanup_supports_jpg_and_preserves_webdav_metadata(self):
        self.image("2026/11/09/both.jpg", age_days=2)
        self.image("2026/11/09/local.webp", age_days=1)
        self.service.list_items("")
        indexed = self.service.index_snapshot()
        indexed["2026/11/09/both.jpg"].update(webdav=True, storage="both")
        self.service._save_index(indexed)
        usage = namedtuple("Usage", "total used free")
        with (
            mock.patch.object(images.shutil, "disk_usage", side_effect=[usage(100, 100, 0), usage(100, 0, 10 * 1024 * 1024)]),
            mock.patch.object(storage, "WebDAVClient") as client,
        ):
            result = images.delete_to_target(1)
            client.assert_not_called()
        self.assertEqual(result["removed"], 2)
        self.assertTrue(result["done"])
        self.assertEqual(result["current_free_mb"], 10)
        self.assertEqual(self.service.index_snapshot()["2026/11/09/both.jpg"]["storage"], "webdav")
        self.assertIn("2026/11/09/both.jpg", tags.load_tags())
        self.assertNotIn("2026/11/09/local.webp", tags.load_tags())
        self.assertFalse((self.thumbnail_root / "2026/11/09/local.webp.png").exists())

    def test_disk_cleanup_dry_run_does_not_modify_files_or_metadata(self):
        path = self.image()
        self.service.list_items("")
        before = self.service.index_file.read_bytes()
        with mock.patch.object(images.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            result = images.delete_to_target(1, dry_run=True)
        self.assertEqual(result["removed"], 1)
        self.assertTrue(path.exists())
        self.assertEqual(self.service.index_file.read_bytes(), before)

    def test_orphan_cleanup_preserves_remote_thumbnail_and_tags(self):
        missing = self.image("missing.png")
        remote = self.image("remote.png")
        self.service.list_items("")
        indexed = self.service.index_snapshot()
        indexed["remote.png"].update(webdav=True)
        self.service._save_index(indexed)
        missing.unlink()
        remote.unlink()
        images.cleanup_image_thumbnails()
        self.assertFalse((self.thumbnail_root / "missing.png.png").exists())
        self.assertTrue((self.thumbnail_root / "remote.png.png").exists())
        self.assertEqual(tags.load_tags(), {"remote.png": ["test"]})

    def test_thumbnail_cleanup_does_not_remove_inflight_temporary_files(self):
        temporary = self.thumbnail_root / "inflight.tmp"
        temporary.write_bytes(b"in progress")
        images.cleanup_image_thumbnails()
        self.assertTrue(temporary.exists())

    def test_zip_includes_remote_images_and_stream_closes_buffer(self):
        self.image("local.png")
        self.service._save_index({"remote.png": {"webdav": True}})
        with mock.patch.object(storage, "WebDAVClient") as client:
            client.return_value.get.return_value = b"remote image"
            buffer = images.download_images_zip(["local.png", "remote.png"])
        payload = b"".join(images.iter_zip_chunks(buffer))
        self.assertTrue(buffer.closed)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()), {"local.png", "remote.png"})
            self.assertEqual(archive.read("remote.png"), b"remote image")

    def test_storage_stats_does_not_count_non_image_files(self):
        path = self.image()
        (self.image_root / "notes.txt").write_bytes(b"not an image")
        result = images.storage_stats()
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["image_size_bytes"], path.stat().st_size)

    def test_concurrent_thumbnail_generation_is_atomic(self):
        self.image("concurrent.png")
        thumbnail = self.thumbnail_root / "concurrent.png.png"
        thumbnail.unlink()
        errors = []

        def generate():
            try:
                images.ensure_thumbnail("concurrent.png")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=generate) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with Image.open(thumbnail) as image:
            image.verify()
        self.assertEqual(list(self.thumbnail_root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
