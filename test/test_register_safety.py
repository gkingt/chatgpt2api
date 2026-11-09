from __future__ import annotations

import copy
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.register as register_api
from services.register import mail_provider, openai_register
from services.register_service import RegisterService


class RegisterSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "register.json"
        self.original_runtime = copy.deepcopy(openai_register.config)
        self.original_sink = openai_register.register_log_sink
        self.addCleanup(self.restore_runtime)
        self.path.write_text(json.dumps({"enabled": False, "threads": 1, "total": 1,
            "mail": {"providers": [{"id": "mail", "type": "tempy_email", "enable": True}]}}), encoding="utf-8")
        self.service = RegisterService(self.path)
        patcher = mock.patch.object(self.service, "_pool_metrics", return_value={"current_available": 0, "current_quota": 0})
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore_runtime(self):
        if hasattr(self, "service") and self.service._runner and self.service._runner.is_alive():
            self.service.stop()
            self.service._runner.join(3)
        openai_register.config.clear()
        openai_register.config.update(self.original_runtime)
        openai_register.register_log_sink = self.original_sink
        openai_register.reset_cancel()

    def wait_finished(self, timeout=3):
        self.service._runner.join(timeout)
        self.assertFalse(self.service._runner.is_alive())

    def test_disabled_provider_rejects_start_without_spawning_worker(self):
        self.service.update({"mail": {"providers": [{"type": "tempy_email", "enable": False}]}})
        before = self.path.read_bytes()
        with mock.patch.object(openai_register, "worker") as worker:
            with self.assertRaisesRegex(ValueError, "provider"):
                self.service.start()
        worker.assert_not_called()
        self.assertIsNone(self.service._runner)
        self.assertFalse(self.service.get()["enabled"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_saved_mail_configuration_is_available_without_starting_registration(self):
        self.assertEqual(openai_register.config["mail"]["providers"][0]["id"], "mail")
        self.assertIsNone(self.service._runner)

    def test_read_and_event_snapshots_never_save_configuration(self):
        before = self.path.read_bytes()
        with mock.patch.object(self.service, "_save") as save:
            self.service.get()
            self.service.snapshot_json()
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_background_quota_check_does_not_rewrite_configuration(self):
        before = self.path.read_bytes()
        with (
            mock.patch.object(self.service, "_pool_metrics", return_value={"current_quota": 83, "current_available": 8}),
            mock.patch.object(self.service, "_save") as save,
        ):
            result = self.service.auto_start_if_quota_low(1)
        self.assertEqual(result["reason"], "quota_enough")
        self.assertEqual(self.service.get()["stats"]["current_quota"], 83)
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_background_quota_check_never_persists_defaults_after_load_failure(self):
        self.path.write_text("", encoding="utf-8")
        broken = RegisterService(self.path)
        with (
            mock.patch.object(broken, "_pool_metrics", return_value={"current_quota": 83, "current_available": 8}),
            mock.patch.object(broken, "_save") as save,
        ):
            self.assertEqual(broken.auto_start_if_quota_low(1)["reason"], "quota_enough")
            self.assertEqual(broken.auto_start_if_quota_low(100)["reason"], "invalid_config")
        save.assert_not_called()
        self.assertTrue(broken.get()["stats"]["last_error"])
        self.assertEqual(self.path.read_bytes(), b"")

    def test_background_check_for_running_job_does_not_save_configuration(self):
        runner = mock.Mock()
        runner.is_alive.return_value = True
        with (
            mock.patch.object(self.service, "_runner", runner),
            mock.patch.object(self.service, "_save") as save,
        ):
            self.assertEqual(self.service.auto_start_if_quota_low(100)["reason"], "running")
        save.assert_not_called()

    def test_missing_required_provider_fields_rejected_before_start(self):
        for provider in ({"type": "agentmail", "enable": True}, {"type": "outlook_token", "enable": True}, {"type": "not-supported", "enable": True}):
            with self.subTest(provider=provider):
                self.service.update({"mail": {"providers": [provider]}})
                with self.assertRaises(ValueError):
                    self.service.start()

    def test_fast_failures_are_rate_limited_and_stop_interrupts_backoff(self):
        attempted = threading.Event()

        def fail(_index):
            attempted.set()
            return {"ok": False, "error": "network failure"}

        with mock.patch.object(openai_register, "worker", side_effect=fail) as worker:
            self.service.start()
            self.assertTrue(attempted.wait(1))
            time.sleep(0.15)
            self.assertEqual(worker.call_count, 1)
            started = time.monotonic()
            self.service.stop()
            self.wait_finished()
            self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.service.get()["enabled"])

    def test_fatal_configuration_failure_stops_and_prevents_auto_restart_storm(self):
        with mock.patch.object(openai_register, "worker", return_value={"ok": False, "fatal": True, "error": "bad configuration"}) as worker:
            self.service.start()
            self.wait_finished()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.service.get()["stats"]["fail"], 1)
        self.assertIn("bad configuration", self.service.get()["stats"]["last_error"])
        self.assertEqual(self.service.auto_start_if_quota_low(100)["reason"], "failure_cooldown")

    def test_consecutive_failures_trip_circuit_breaker(self):
        with (
            mock.patch.object(openai_register, "worker", return_value={"ok": False, "error": "failed"}) as worker,
            mock.patch("services.register_service.MAX_CONSECUTIVE_FAILURES", 1),
        ):
            self.service.start()
            self.wait_finished()
        self.assertEqual(worker.call_count, 1)
        self.assertFalse(self.service.get()["enabled"])

    def test_update_and_restart_rejected_until_stopping_workers_finish(self):
        entered = threading.Event()
        release = threading.Event()

        def worker(_index):
            entered.set()
            release.wait(3)
            return {"ok": False, "error": "cancelled"}

        with mock.patch.object(openai_register, "worker", side_effect=worker):
            try:
                self.service.start()
                self.assertTrue(entered.wait(1))
                self.service.stop()
                with self.assertRaises(ValueError):
                    self.service.start()
                with self.assertRaises(ValueError):
                    self.service.update({"threads": 2})
            finally:
                release.set()
                self.wait_finished()

    def test_total_mode_finishes_after_success(self):
        with mock.patch.object(openai_register, "worker", return_value={"ok": True}) as worker:
            self.service.start()
            self.wait_finished()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(self.service.get()["stats"]["success"], 1)
        self.assertFalse(json.loads(self.path.read_text(encoding="utf-8"))["enabled"])

    def test_bad_file_is_not_silently_overwritten_with_defaults(self):
        self.path.write_text('{"mail":', encoding="utf-8")
        broken = RegisterService(self.path)
        self.assertTrue(broken.get()["stats"]["last_error"])
        with self.assertRaises(ValueError):
            broken.update({"total": 5})
        with self.assertRaises(ValueError):
            broken.start()
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"mail":')

    def test_valid_backup_recovers_configuration_without_auto_start(self):
        backup = self.path.with_name("register.json.bak")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        saved["enabled"] = True
        backup.write_text(json.dumps(saved), encoding="utf-8")
        self.path.write_text("broken", encoding="utf-8")
        recovered = RegisterService(self.path)
        self.assertFalse(recovered.get()["enabled"])
        self.assertEqual(recovered.get()["mail"]["providers"][0]["id"], "mail")
        recovered.update({"total": 7})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["total"], 7)

    def test_partial_mail_update_preserves_provider_and_failed_save_rolls_back(self):
        original = self.service.get()["mail"]["providers"]
        self.service.update({"mail": {"request_timeout": 45}})
        self.assertEqual(self.service.get()["mail"]["providers"], original)
        before = self.service.get()
        with mock.patch.object(self.service, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.update({"mail": {"providers": []}})
        self.assertEqual(self.service.get(), before)

    def test_auto_start_invalid_config_returns_error_and_cooldown(self):
        self.service.update({"mail": {"providers": []}})
        self.assertEqual(self.service.auto_start_if_quota_low(10)["reason"], "invalid_config")
        self.assertEqual(self.service.auto_start_if_quota_low(10)["reason"], "failure_cooldown")

    def test_stale_revision_cannot_overwrite_newer_configuration(self):
        self.service.update({"total": 7, "config_revision": 0})
        with self.assertRaisesRegex(ValueError, "其他页面"):
            self.service.update({"mail": {"providers": []}, "config_revision": 0})
        self.assertEqual(self.service.get()["total"], 7)
        self.assertTrue(self.service.get()["mail"]["providers"])

    def test_start_api_reports_bad_config_as_400(self):
        self.service.update({"mail": {"providers": []}})
        with mock.patch.object(register_api, "register_service", self.service), mock.patch.object(register_api, "require_admin"):
            app = FastAPI()
            app.include_router(register_api.create_router())
            response = TestClient(app).post("/api/register/start")
        self.assertEqual(response.status_code, 400)
        self.assertIn("provider", response.json()["detail"]["error"])

    def test_worker_labels_configuration_error_as_fatal(self):
        with mock.patch.object(openai_register, "PlatformRegistrar") as registrar:
            registrar.return_value.register.side_effect = mail_provider.MailConfigurationError("missing mail")
            result = openai_register.worker(1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["fatal"])
        registrar.return_value.close.assert_called_once()


class RegisterApiThreadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_save_does_not_block_stop_or_health_requests(self):
        entered = threading.Event()
        release = threading.Event()
        event_thread = threading.get_ident()
        calls = []

        def slow_save(*args):
            entered.set()
            release.wait(3)
            return {}

        def stop():
            calls.append(threading.get_ident())
            return {"enabled": False}

        with (
            mock.patch.object(register_api, "require_admin"),
            mock.patch.object(register_api.register_service, "update", side_effect=slow_save),
            mock.patch.object(register_api.register_service, "stop", side_effect=stop),
        ):
            app = FastAPI()
            app.include_router(register_api.create_router())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                pending = asyncio.create_task(client.post("/api/register", json={"total": 1}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    response = await asyncio.wait_for(client.post("/api/register/stop"), timeout=1)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotEqual(calls[0], event_thread)
                finally:
                    release.set()
                    await pending


if __name__ == "__main__":
    unittest.main()
