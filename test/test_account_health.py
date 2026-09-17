from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
