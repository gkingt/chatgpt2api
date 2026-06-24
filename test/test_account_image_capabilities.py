from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.auth_service import AuthService
from services.config import config
from services.openai_backend_api import InvalidAccessTokenError
from services.protocol.conversation import ConversationRequest, ImageOutput, _generate_single_image, is_image_quota_exhausted_error
from services.register_service import register_service
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token, split_image_model


class AccountCapabilityTests(unittest.TestCase):
    def test_unknown_quota_accounts_are_available_only_when_not_throttled(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "image_quota_unknown": True, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "image_quota_unknown": True, "quota": 0}
            )
        )

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_does_not_consume_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 0,
                    "image_quota_unknown": True,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "正常")
            self.assertTrue(updated["image_quota_unknown"])

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_image_preflight_failure_records_error_without_disabling_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "stale-token", "status": "正常", "quota": 8, "image_quota_unknown": True}
            ])

            def fail_preflight(access_token: str, event: str = "fetch_remote_info") -> dict:
                raise RuntimeError("upstream auth preflight failed")

            service.fetch_remote_info = fail_preflight

            with self.assertRaisesRegex(RuntimeError, "no available image quota"):
                service.get_available_access_token()

            account = service.get_account("stale-token")
            stats = service.get_stats()
            self.assertIsNotNone(account)
            self.assertEqual(account["status"], "正常")
            self.assertEqual(account["quota"], 8)
            self.assertTrue(account["image_quota_unknown"])
            self.assertIn("preflight failed", account["last_refresh_error"])
            self.assertEqual(stats["active"], 1)
            self.assertEqual(stats["total_quota"], 8)
            self.assertEqual(stats["unlimited_quota_count"], 1)

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=True)

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["status"], "异常")
                self.assertEqual(account["quota"], 0)
                self.assertFalse(account["image_quota_unknown"])
                self.assertEqual(account["invalid_count"], 1)
                stats = service.get_stats()
                self.assertEqual(stats["active"], 0)
                self.assertEqual(stats["total_quota"], 0)
                self.assertEqual(stats["unlimited_quota_count"], 0)
                self.assertEqual(service.list_normal_tokens(), [])
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_stats_ignore_normal_accounts_with_pending_invalid_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "pending-invalid-token",
                        "status": "正常",
                        "quota": 9,
                        "image_quota_unknown": True,
                        "invalid_count": 1,
                    },
                    {"access_token": "good-token", "status": "正常", "quota": 2},
                ]
            )

            stats = service.get_stats()

            self.assertEqual(stats["active"], 1)
            self.assertEqual(stats["total_quota"], 2)
            self.assertEqual(stats["unlimited_quota_count"], 0)
            self.assertEqual(service.list_normal_tokens(), ["good-token"])

    def test_register_pool_metrics_ignore_unavailable_normal_accounts(self) -> None:
        with patch("services.register_service.account_service") as mocked_account_service:
            mocked_account_service.list_accounts.return_value = [
                {"access_token": "pending-invalid-token", "status": "正常", "quota": 9, "invalid_count": 1},
                {"access_token": "unknown-quota-token", "status": "正常", "quota": 0, "image_quota_unknown": True},
                {"access_token": "good-token", "status": "正常", "quota": 2},
                {"access_token": "limited-token", "status": "限流", "quota": 5},
            ]
            mocked_account_service._is_image_account_available.side_effect = AccountService._is_image_account_available

            metrics = register_service._pool_metrics()

            self.assertEqual(metrics["current_available"], 2)
            self.assertEqual(metrics["current_quota"], 2)

    def test_image_quota_error_detector_matches_common_messages(self) -> None:
        self.assertTrue(is_image_quota_exhausted_error("reached your image generation limit"))
        self.assertTrue(is_image_quota_exhausted_error("图片额度不足，请稍后再试"))
        self.assertFalse(is_image_quota_exhausted_error("upstream connection timed out"))
        self.assertFalse(is_image_quota_exhausted_error("rate_limit_exceeded: too many requests for image generation"))

    def test_image_generation_marks_quota_exhausted_token_limited_and_retries(self) -> None:
        class FakeAccountService:
            def __init__(self) -> None:
                self.tokens = ["bad-token", "good-token"]
                self.limited: list[str] = []
                self.marked: list[tuple[str, bool]] = []

            def get_available_access_token(self, **_: object) -> str:
                if not self.tokens:
                    raise RuntimeError("no available image quota")
                return self.tokens.pop(0)

            def get_account(self, token: str) -> dict:
                return {"email": f"{token}@example.com"}

            def mark_image_quota_exhausted_token(self, token: str, event: str, error: str = "") -> dict:
                self.limited.append(token)
                return {"access_token": token, "status": "限流", "quota": 0}

            def mark_image_result(self, token: str, success: bool) -> None:
                self.marked.append((token, success))

        fake_account_service = FakeAccountService()

        def fake_stream(backend: object, request: ConversationRequest, index: int, total: int):
            if getattr(backend, "access_token", "") == "bad-token":
                raise RuntimeError("reached your image generation limit")
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=[{"url": "ok"}])

        with patch("services.protocol.conversation.account_service", fake_account_service), patch(
            "services.protocol.conversation.stream_image_outputs", fake_stream
        ):
            outputs = _generate_single_image(ConversationRequest(model="gpt-image-2", prompt="test"), 1, 1)

        self.assertEqual(fake_account_service.limited, ["bad-token"])
        self.assertEqual(fake_account_service.marked, [("good-token", True)])
        self.assertEqual(outputs[0].data, [{"url": "ok"}])

    def test_refresh_accounts_removes_invalid_token_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_marks_deactivated_account_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "deactivated-token", "status": "正常", "quota": 5}])

            with patch(
                "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                return_value={
                    "email": "disabled@example.com",
                    "type": "Plus",
                    "quota": 0,
                    "image_quota_unknown": False,
                    "status": "禁用",
                    "is_deactivated": True,
                },
            ):
                result = service.refresh_accounts(["deactivated-token"])

            account = service.get_account("deactivated-token")
            self.assertEqual(result["refreshed"], 1)
            self.assertIsNotNone(account)
            self.assertEqual(account["status"], "禁用")
            self.assertEqual(account["quota"], 0)

    def test_text_token_skips_pending_invalid_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "bad-token", "status": "正常", "invalid_count": 1},
                    {"access_token": "good-token", "status": "正常"},
                ]
            )

            self.assertEqual(service.get_text_access_token(), "good-token")


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
