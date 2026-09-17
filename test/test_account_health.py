from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from services.account_health import (
    ERROR_INVALID_CREDENTIALS,
    ERROR_NEEDS_RELOGIN,
    ERROR_NEEDS_VERIFICATION,
    ERROR_TRANSIENT,
    ERROR_UPSTREAM_RATE_LIMITED,
    HEALTH_STATE_HEALTHY,
    HEALTH_STATE_INVALID_CONFIRMED,
    HEALTH_STATE_INVALID_PENDING,
    HEALTH_STATE_NEEDS_RELOGIN,
    HEALTH_STATE_NEEDS_VERIFICATION,
    HEALTH_STATE_RATE_LIMITED,
    HEALTH_STATE_TRANSIENT_ERROR,
    TokenRefreshError,
    classify_account_error,
)
from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend


class AccountHealthClassificationTests(unittest.TestCase):
    def test_refresh_token_errors_are_classified_without_deleting_accounts(self) -> None:
        self.assertEqual(
            classify_account_error(TokenRefreshError(400, "invalid_grant")).kind,
            ERROR_NEEDS_RELOGIN,
        )
        self.assertEqual(
            classify_account_error(TokenRefreshError(429, "rate_limit_exceeded", retry_after_seconds=12)).kind,
            ERROR_UPSTREAM_RATE_LIMITED,
        )
        self.assertEqual(
            classify_account_error(SimpleNamespace(status_code=503, detail="upstream unavailable")).kind,
            ERROR_TRANSIENT,
        )

    def test_health_failure_uses_cooldown_and_preserves_main_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "status": "正常", "quota": 7}])

            account = service._record_account_health_failure(
                "token",
                "test_503",
                SimpleNamespace(status_code=503, detail="temporary upstream failure"),
            )

            self.assertIsNotNone(account)
            self.assertEqual(account["status"], "正常")
            self.assertEqual(account["quota"], 7)
            self.assertEqual(account["health_state"], HEALTH_STATE_TRANSIENT_ERROR)
            self.assertEqual(account["health_failure_count"], 1)
            self.assertIsNotNone(account["health_retry_at"])

            account = service._record_account_health_failure(
                "token",
                "test_429",
                SimpleNamespace(status_code=429, retry_after=12, detail="too many requests"),
            )
            self.assertEqual(account["health_state"], HEALTH_STATE_RATE_LIMITED)
            self.assertEqual(account["health_error_kind"], ERROR_UPSTREAM_RATE_LIMITED)

    def test_invalid_token_requires_two_confirmations_before_auto_remove(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {
                            "access_token": "token",
                            "status": "正常",
                            "quota": 9,
                            "created_at": "2020-01-01 00:00:00",
                        }
                    ]
                )

                self.assertFalse(service.remove_invalid_token("token", "first", error="HTTP 401"))
                pending = service.get_account("token")
                self.assertIsNotNone(pending)
                self.assertEqual(pending["status"], "正常")
                self.assertEqual(pending["quota"], 9)
                self.assertEqual(pending["health_state"], HEALTH_STATE_INVALID_PENDING)

                service.update_account(
                    "token",
                    {
                        "created_at": "2020-01-01 00:00:00",
                        "last_invalid_at": "2020-01-01 00:00:00",
                    },
                    quiet=True,
                )
                self.assertTrue(service.remove_invalid_token("token", "second", error="HTTP 401"))
                self.assertIsNone(service.get_account("token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_successful_check_clears_pending_health_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "status": "正常", "quota": 4}])
            service.remove_invalid_token("token", "first", error="HTTP 401")

            account = service._record_account_health_success(
                "token",
                "manual_check",
                {
                    "status": "正常",
                    "quota": 6,
                    "image_quota_unknown": False,
                    "email": "user@example.com",
                },
            )

            self.assertEqual(account["health_state"], HEALTH_STATE_HEALTHY)
            self.assertEqual(account["invalid_count"], 0)
            self.assertEqual(account["health_failure_count"], 0)
            self.assertEqual(account["quota"], 6)
            self.assertIsNotNone(account["last_successful_check_at"])

    def test_password_login_errors_are_classified_as_relogin_or_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token"])

            classification, _ = service._classify_password_login_result({"error": "invalid_password"})
            self.assertEqual(classification.kind, ERROR_INVALID_CREDENTIALS)
            service._record_account_health_failure("token", "password", SimpleNamespace(detail="invalid password"), classification)
            self.assertEqual(service.get_account("token")["health_state"], HEALTH_STATE_NEEDS_RELOGIN)

            service.update_account("token", {"health_state": HEALTH_STATE_HEALTHY}, quiet=True)
            classification, _ = service._classify_password_login_result({"error": "need_verification_code"})
            self.assertEqual(classification.kind, ERROR_NEEDS_VERIFICATION)
            service._record_account_health_failure("token", "password", SimpleNamespace(detail="need verification code"), classification)
            self.assertEqual(service.get_account("token")["health_state"], HEALTH_STATE_NEEDS_VERIFICATION)

    def test_rapid_reports_do_not_count_or_postpone_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "created_at": "2020-01-01", "quota": 5}])
            service.remove_invalid_token("token", "first")
            first = service.get_account("token")
            for _ in range(5):
                service.remove_invalid_token("token", "duplicate")
            current = service.get_account("token")
            self.assertEqual(current["invalid_count"], 1)
            self.assertEqual(current["health_failure_count"], 1)
            self.assertEqual(current["health_retry_at"], first["health_retry_at"])

    def test_new_account_grace_cannot_be_bypassed_by_failure_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            now = datetime.now(timezone.utc)
            service.add_account_items([{"access_token": "token", "created_at": now.isoformat()}])
            service.remove_invalid_token("token", "first")
            current = service.get_account("token")
            self.assertGreaterEqual(service._parse_time(current["health_retry_at"]), now + timedelta(minutes=10))
            current.update(invalid_count=10, last_invalid_at=(now - timedelta(minutes=1)).isoformat())
            self.assertTrue(service._should_defer_invalid_token(current, now))
            self.assertFalse(service._should_defer_invalid_token(current, now + timedelta(minutes=11)))

    def test_due_pending_is_selected_despite_exclusion_from_normal_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "created_at": "2020-01-01", "quota": 5}])
            service.remove_invalid_token("token", "first")
            self.assertEqual(service.list_due_health_tokens(), [])
            service.update_account("token", {"health_retry_at": "2020-01-01"}, quiet=True)
            self.assertEqual(service.list_normal_tokens(), [])
            self.assertEqual(service.list_due_health_tokens(), ["token"])
            service._record_account_health_success("token", "retry", {"status": "正常", "quota": 5})
            self.assertEqual(service.list_due_health_tokens(), [])
            self.assertEqual(service.list_normal_tokens(), ["token"])

    def test_quota_unknown_retries_without_blocking_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_accounts(["token"])
            account = service._record_account_health_success("token", "retry", {"image_quota_unknown": True})
            self.assertIsNotNone(account["health_retry_at"])
            self.assertTrue(service._is_account_eligible_for_text(account))
            self.assertFalse(service._is_image_account_available(account))

    def test_health_watcher_runs_and_stops(self):
        from api import support
        stop = Event()
        with patch.object(support, "account_service") as service:
            service.list_due_health_tokens.return_value = ["token"]
            service.refresh_accounts.side_effect = lambda tokens: stop.set()
            thread = support.start_account_health_watcher(stop)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            service.refresh_accounts.assert_called_once_with(["token"])

    def test_image_401_is_recorded_once(self):
        from services.openai_backend_api import OpenAIBackendAPI, InvalidAccessTokenError
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "quota": 5}])
            with patch.object(OpenAIBackendAPI, "get_user_info", side_effect=InvalidAccessTokenError()):
                with self.assertRaisesRegex(RuntimeError, "no available image quota"):
                    service.get_available_access_token()
            self.assertEqual(service.get_account("token")["invalid_count"], 1)
            self.assertEqual(service.get_account("token")["health_failure_count"], 1)


    def test_overlapping_remote_check_does_not_call_backend_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_accounts(["token"])
            def check(*args):
                service._apply_refreshed_tokens("token", {"access_token": "rotated"}, "test")
                return service.fetch_remote_info("rotated")
            with patch.object(service, "_fetch_remote_info", side_effect=check) as remote:
                self.assertIsNone(service.fetch_remote_info("token"))
                remote.assert_called_once()
            self.assertEqual(service._health_check_inflight, set())


    def test_non_json_refresh_error_preserves_http_status_and_retry_after(self):
        from unittest.mock import MagicMock
        response = MagicMock(status_code=429, text="<html>busy</html>")
        response.headers = {"Retry-After": "120"}
        response.json.side_effect = ValueError("not json")
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            with patch("curl_cffi.requests.Session") as session:
                session.return_value.post.return_value = response
                with self.assertRaises(TokenRefreshError) as error:
                    service._request_access_token_refresh("refresh")
            classification = classify_account_error(error.exception)
            self.assertEqual(classification.kind, ERROR_UPSTREAM_RATE_LIMITED)
            self.assertEqual(classification.retry_after_seconds, 120)


    def test_nested_refresh_error_is_parsed(self):
        from unittest.mock import MagicMock
        response = MagicMock(status_code=400, text="json", headers={})
        response.json.return_value = {"error": {"message": "Your session has ended. Please log in again.",
                                                "code": "refresh_token_invalidated", "type": "invalid_request_error"}}
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            with patch("curl_cffi.requests.Session") as session:
                session.return_value.post.return_value = response
                with self.assertRaises(TokenRefreshError) as error:
                    service._request_access_token_refresh("refresh")
            self.assertEqual(error.exception.error_code, "refresh_token_invalidated")
            self.assertEqual(classify_account_error(error.exception).kind, ERROR_NEEDS_RELOGIN)
            self.assertNotIn("{'message'", str(error.exception))

    def test_invalid_refresh_is_confirmed_then_deleted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True, "auto_relogin_after_refresh": False}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_token": "refresh", "created_at": "2020-01-01"}])
            with patch.object(service, "_request_access_token_refresh", side_effect=TokenRefreshError(400, "refresh_token_invalidated")):
                service.refresh_access_token("token", force=True)
                current = service.get_account("token")
                self.assertEqual(current["health_state"], HEALTH_STATE_NEEDS_RELOGIN)
                self.assertEqual(current["health_error_code"], "refresh_token_invalidated")
                self.assertIsNotNone(current["health_retry_at"])
                service.update_account("token", {"credential_invalid_at": "2020-01-01", "health_retry_at": "2020-01-01"}, quiet=True)
                self.assertIn("token", service.list_due_health_tokens())
                service.fetch_remote_info("token")
                self.assertIsNone(service.get_account("token"))

    def test_invalid_refresh_attempts_password_recovery_before_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True, "auto_relogin_after_refresh": True}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_token": "refresh", "email": "test@example.com", "password": "test", "created_at": "2020-01-01"}])
            with patch.object(service, "_request_access_token_refresh", side_effect=TokenRefreshError(400, "refresh_token_invalidated")), patch.object(service, "_start_password_relogin_if_possible", return_value=True) as login:
                service.refresh_access_token("token", force=True)
                login.assert_called_once()
            self.assertEqual(service.get_account("token").get("credential_invalid_count", 0), 0)
            with patch.object(service, "_login_with_password", return_value={"ok": True, "access_token": "new", "refresh_token": "new-refresh"}):
                service._password_re_login_thread("token", "test@example.com", "test", "test")
            self.assertFalse(service.get_account("new")["refresh_credentials_invalid"])
            self.assertEqual(service.get_account("new")["health_state"], HEALTH_STATE_HEALTHY)

    def test_cleanup_only_removes_confirmed_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": False}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            for state in [HEALTH_STATE_INVALID_CONFIRMED, HEALTH_STATE_NEEDS_RELOGIN, HEALTH_STATE_NEEDS_VERIFICATION, HEALTH_STATE_TRANSIENT_ERROR]:
                service.add_account_items([{"access_token": state, "health_state": state}])
            service.cleanup_confirmed_invalid_accounts()
            self.assertEqual(len(service.list_accounts()), 4)
            config.data["auto_remove_invalid_accounts"] = True
            service.cleanup_confirmed_invalid_accounts()
            self.assertIsNone(service.get_account(HEALTH_STATE_INVALID_CONFIRMED))
            self.assertEqual(len(service.list_accounts()), 3)

    def test_legacy_relogin_record_is_scheduled_for_live_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_token": "refresh", "health_state": HEALTH_STATE_NEEDS_RELOGIN, "health_updated_at": "2020-01-01", "health_error_code": "{'message': 'old error'}"}])
            self.assertEqual(service.list_due_health_tokens(), ["token"])


    def test_password_verification_and_network_errors_never_confirm_deletion(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            for error in ["need_verification_code", "connection timeout"]:
                service.add_account_items([{"access_token": error, "refresh_credentials_invalid": True, "credential_invalid_count": 1, "credential_invalid_at": "2020-01-01", "created_at": "2020-01-01"}])
                with patch.object(service, "_login_with_password", return_value={"ok": False, "error": error}):
                    service._password_re_login_thread(error, "test@example.com", "test", "test")
                self.assertIsNotNone(service.get_account(error))
                self.assertNotEqual(service.get_account(error)["health_state"], HEALTH_STATE_INVALID_CONFIRMED)

    def test_wrong_password_confirms_only_after_explicit_refresh_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_credentials_invalid": True, "created_at": "2020-01-01"}])
            with patch.object(service, "_login_with_password", return_value={"ok": False, "error": "invalid_password"}):
                service._password_re_login_thread("token", "email", "password", "test")
                self.assertIsNotNone(service.get_account("token"))
                service.update_account("token", {"credential_invalid_at": "2020-01-01"}, quiet=True)
                service._password_re_login_thread("token", "email", "password", "test")
                self.assertIsNone(service.get_account("token"))

    def test_refresh_credential_confirmation_obeys_new_account_grace(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_credentials_invalid": True, "credential_invalid_count": 3, "credential_invalid_at": "2020-01-01"}])
            service._confirm_unrecoverable_credentials("token", "test")
            self.assertEqual(service.get_account("token")["health_state"], HEALTH_STATE_NEEDS_RELOGIN)
            self.assertIsNotNone(service.get_account("token")["health_retry_at"])


    def test_generic_oauth_401_is_not_terminal_credential_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True, "auto_relogin_after_refresh": False}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_token": "refresh", "created_at": "2020-01-01"}])
            with patch.object(service, "_request_access_token_refresh", side_effect=TokenRefreshError(401)):
                service.refresh_access_token("token", force=True)
            self.assertFalse(service.get_account("token").get("refresh_credentials_invalid"))

    def test_access_token_error_cannot_bypass_refresh_credential_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.data, {"auto_remove_invalid_accounts": True}):
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_credentials_invalid": True, "invalid_count": 3, "last_invalid_at": "2020-01-01", "created_at": "2020-01-01"}])
            self.assertFalse(service._record_invalid_token_seen("token", "test", "401"))
            self.assertIsNotNone(service.get_account("token"))



if __name__ == "__main__":
    unittest.main()
