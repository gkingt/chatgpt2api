from __future__ import annotations

import asyncio
import io
import threading
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException

import api.system as system_module


class ManagementApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in ("require_admin", "require_identity"):
            patcher = mock.patch.object(system_module, name, return_value={"role": "admin"})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = FastAPI()
        self.app.include_router(system_module.create_router("test"))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_file_operations_run_outside_event_loop_thread(self):
        event_thread = threading.get_ident()
        calls = []

        def worker(result):
            def invoke(*args, **kwargs):
                calls.append((threading.get_ident(), args, kwargs))
                return result
            return invoke

        cases = [
            ("GET", "/api/logs?type=%20call%20&start_date=2026-11-01", "log_service.list", [], None),
            ("POST", "/api/logs/delete", "log_service.delete", {"removed": 1}, {"ids": ["one"]}),
            ("GET", "/api/images", "list_images", {"items": [], "groups": []}, None),
            ("POST", "/api/images/delete", "delete_images", {"removed": 1}, {"paths": ["one.png"]}),
            ("GET", "/api/images/storage", "storage_stats", {}, None),
            ("GET", "/api/images/tags", "get_all_tags", [], None),
            ("POST", "/api/images/tags", "set_tags", ["tag"], {"path": "one.png", "tags": ["tag"]}),
            ("DELETE", "/api/images/tags/tag", "delete_tag", 1, None),
            ("GET", "/images/one.png", "get_image_response", system_module.Response(b"png"), None),
            ("GET", "/image-thumbnails/one.png", "get_thumbnail_response", system_module.Response(b"png"), None),
            ("GET", "/api/images/download/one.png", "get_image_download_response", system_module.Response(b"png"), None),
            ("POST", "/api/images/download", "download_images_zip", io.BytesIO(b"zip"), {"paths": ["one.png"]}),
        ]
        for method, url, target, result, body in cases:
            with self.subTest(url=url), mock.patch(f"api.system.{target}", side_effect=worker(result)):
                response = await self.client.request(method, url, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotEqual(calls[-1][0], event_thread)
        self.assertEqual(calls[0][2], {"type": "call", "start_date": "2026-11-01", "end_date": ""})

    async def test_slow_log_and_image_operations_do_not_block_other_requests(self):
        for path, target, result, body in [
            ("/api/logs", "log_service.list", [], None),
            ("/api/images", "list_images", {"items": [], "groups": []}, None),
            ("/api/logs/delete", "log_service.delete", {"removed": 1}, {"ids": ["one"]}),
            ("/api/images/delete", "delete_images", {"removed": 1}, {"paths": ["one.png"]}),
        ]:
            started = threading.Event()
            release = threading.Event()

            def slow(*args, **kwargs):
                started.set()
                if not release.wait(5):
                    raise AssertionError("event loop was blocked")
                return result

            with self.subTest(path=path), mock.patch(f"api.system.{target}", side_effect=slow):
                pending = asyncio.create_task(self.client.request("POST" if body else "GET", path, json=body))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    response = await asyncio.wait_for(self.client.get("/version"), timeout=1)
                    self.assertEqual(response.status_code, 200)
                    with mock.patch.object(system_module.config, "get_third_party_apps_settings", return_value={}):
                        response = await asyncio.wait_for(self.client.get("/api/third-party-apps"), timeout=1)
                    self.assertEqual(response.status_code, 200)
                finally:
                    release.set()
                    await pending

    async def test_unauthorized_requests_do_not_access_files(self):
        with (
            mock.patch.object(system_module, "require_admin", side_effect=HTTPException(status_code=401)),
            mock.patch.object(system_module.log_service, "list") as logs,
            mock.patch.object(system_module, "list_images") as images,
        ):
            for path in ("/api/logs", "/api/images"):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 401)
            logs.assert_not_called()
            images.assert_not_called()

    async def test_negative_cleanup_target_is_rejected(self):
        response = await self.client.post("/api/images/storage/cleanup-to-target?target_free_mb=-1")
        self.assertEqual(response.status_code, 422)

    async def test_webdav_delete_failure_returns_actionable_error(self):
        with mock.patch.object(system_module, "delete_images", side_effect=system_module.ImageStorageError("WebDAV DELETE failed: HTTP 503")):
            response = await self.client.post("/api/images/delete", json={"paths": ["image.png"]})
        self.assertEqual(response.status_code, 502)
        self.assertIn("WebDAV DELETE failed", response.json()["detail"]["error"])


if __name__ == "__main__":
    unittest.main()
