import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.proxy_service import ClearanceBundle
from services.register import mail_provider, openai_register
from services.register_service import register_service


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url="https://auth.openai.com/test"):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url

    def json(self):
        return {}


class FakeCookieJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain=None):
        self.items.append({"name": name, "value": value, "domain": domain})


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}
        self.cookies = FakeCookieJar()
        self.closed = False

    def close(self):
        self.closed = True


class FakeProxySettings:
    def __init__(self, bundle=None):
        self.bundle = bundle
        self.refreshed = False
        self.session_kwargs_calls = []
        self.build_headers_calls = []
        self.refresh_calls = []

    def build_session_kwargs(self, **kwargs):
        self.session_kwargs_calls.append(kwargs)
        return dict(kwargs, proxy="http://runtime.example:8118")

    def build_headers(self, headers=None, target_url="", proxy="", upstream=True, **kwargs):
        self.build_headers_calls.append({"target_url": target_url, "proxy": proxy, "upstream": upstream})
        merged = dict(headers or {})
        if self.refreshed and self.bundle and self.bundle.cookies:
            merged["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.bundle.cookies.items())
        return merged

    def refresh_clearance(self, target_url="", proxy="", force=False, upstream=True, **kwargs):
        self.refresh_calls.append({"target_url": target_url, "proxy": proxy, "force": force, "upstream": upstream})
        self.refreshed = self.bundle is not None
        return self.bundle


class RegisterProxyRuntimeTests(unittest.TestCase):
    def test_mail_provider_session_uses_mail_proxy(self):
        created = []

        def fake_session_factory(**kwargs):
            session = FakeSession(**kwargs)
            created.append(session)
            return session

        with patch.object(mail_provider.requests, "Session", side_effect=fake_session_factory):
            session = mail_provider._create_session(mail_provider._config({"proxy": "http://mail-proxy.example:8080"}))

        self.assertIs(session, created[0])
        self.assertEqual(session.kwargs["proxy"], "http://mail-proxy.example:8080")

    def test_create_session_uses_proxy_settings_without_breaking_existing_proxy_argument(self):
        fake_proxy = FakeProxySettings()
        created = []

        def fake_session_factory(**kwargs):
            session = FakeSession(**kwargs)
            created.append(session)
            return session

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register.requests,
            "Session",
            side_effect=fake_session_factory,
        ):
            session = openai_register.create_session("http://legacy-register.example:8080")

        self.assertIs(session, created[0])
        self.assertEqual(fake_proxy.session_kwargs_calls[0]["proxy"], "http://legacy-register.example:8080")
        self.assertTrue(fake_proxy.session_kwargs_calls[0]["upstream"])
        self.assertEqual(fake_proxy.session_kwargs_calls[0]["impersonate"], "chrome")
        self.assertFalse(fake_proxy.session_kwargs_calls[0]["verify"])
        self.assertEqual(session.kwargs["proxy"], "http://runtime.example:8118")
        session.close()

    def test_request_cancel_closes_active_sessions_and_blocks_new_sessions(self):
        fake_proxy = FakeProxySettings()
        created = []

        def fake_session_factory(**kwargs):
            session = FakeSession(**kwargs)
            created.append(session)
            return session

        openai_register.reset_cancel()
        try:
            with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
                openai_register.requests,
                "Session",
                side_effect=fake_session_factory,
            ):
                session = openai_register.create_session("http://legacy-register.example:8080")
                openai_register.request_cancel()

                self.assertTrue(session.closed)
                with self.assertRaises(openai_register.RegistrationCancelled):
                    openai_register.create_session("http://legacy-register.example:8080")
        finally:
            openai_register.reset_cancel()

    def test_total_mode_targets_successful_normal_accounts_not_attempts(self):
        cfg = {"mode": "total", "total": 2}

        self.assertFalse(register_service._target_reached(cfg, success=1))
        self.assertTrue(register_service._target_reached(cfg, success=2))
        self.assertFalse(register_service._can_submit_more(cfg, success=1, running=1))
        self.assertTrue(register_service._can_submit_more(cfg, success=1, running=0))

    def test_register_snapshot_cache_keeps_outlook_pool_redaction_stable(self):
        original_config = register_service._config
        original_logs = register_service._logs
        original_snapshot = register_service._last_snapshot
        original_snapshot_payload = register_service._last_snapshot_payload
        original_snapshot_json = register_service._last_snapshot_json
        try:
            register_service._logs = []
            register_service._last_snapshot = ""
            register_service._last_snapshot_payload = None
            register_service._last_snapshot_json = ""
            register_service._config = {
                **original_config,
                "mail": {
                    "providers": [
                        {
                            "type": "outlook_token",
                            "mailboxes": "user@example.com----password----client-id----refresh-token",
                        }
                    ]
                },
            }

            first = register_service.get()
            second = register_service.get()

            self.assertEqual(first["mail"]["providers"][0]["mailboxes"], "")
            self.assertEqual(second["mail"]["providers"][0]["mailboxes"], "")
            self.assertEqual(second["mail"]["providers"][0]["mailboxes_count"], 1)
            snapshot = json.loads(register_service.snapshot_json())
            self.assertEqual(snapshot["mail"]["providers"][0]["mailboxes"], "")
        finally:
            register_service._config = original_config
            register_service._logs = original_logs
            register_service._last_snapshot = original_snapshot
            register_service._last_snapshot_payload = original_snapshot_payload
            register_service._last_snapshot_json = original_snapshot_json

    def test_register_stats_save_is_throttled_while_running(self):
        original_last_save_at = register_service._last_running_save_at
        try:
            register_service._last_running_save_at = 0.0
            with patch.object(register_service, "_save") as mocked_save:
                with patch("services.register_service.time.monotonic", side_effect=[10.0, 10.5, 11.0, 12.1]):
                    register_service._bump(running=1)
                    register_service._bump(running=2)
                    register_service._bump(running=3)
                    register_service._bump(running=4)

            self.assertEqual(mocked_save.call_count, 2)
        finally:
            register_service._last_running_save_at = original_last_save_at

    def test_cloudflare_without_clearance_keeps_clear_register_error(self):
        fake_proxy = FakeProxySettings(bundle=None)
        cf_response = FakeResponse(
            status_code=403,
            text="<html><title>Just a moment...</title></html>",
            headers={"server": "cloudflare", "content-type": "text/html"},
            url="https://auth.openai.com/api/accounts/authorize",
        )

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "request_with_local_retry", return_value=(cf_response, "")):
            registrar = openai_register.PlatformRegistrar(proxy="http://legacy-register.example:8080")
            with self.assertRaisesRegex(RuntimeError, "Cloudflare") as ctx:
                registrar._platform_authorize("user@example.com", 1)

        self.assertEqual(len(fake_proxy.refresh_calls), 1)
        self.assertIn("status=403", str(ctx.exception))
        self.assertIn("Just a moment", str(ctx.exception))

    def test_openai_html_behind_cloudflare_is_not_treated_as_challenge(self):
        response = FakeResponse(
            status_code=200,
            text="""
            <!DOCTYPE html><html lang=\"en-US\"><head>
            <title>Create a password - OpenAI</title>
            </head><body>OpenAI account page</body></html>
            """,
            headers={"server": "cloudflare", "content-type": "text/html; charset=utf-8"},
            url="https://auth.openai.com/create-account/password",
        )

        self.assertFalse(openai_register._is_cloudflare_challenge(response))

    def test_cloudflare_challenge_refreshes_clearance_and_retries_once_with_matching_headers(self):
        bundle = ClearanceBundle(
            target_host="auth.openai.com",
            proxy_url="http://runtime.example:8118",
            cookies={"cf_clearance": "flare-token"},
            user_agent="Flare UA",
        )
        fake_proxy = FakeProxySettings(bundle=bundle)
        responses = [
            FakeResponse(
                status_code=403,
                text="<html><title>Just a moment...</title></html>",
                headers={"server": "cloudflare", "content-type": "text/html"},
                url="https://auth.openai.com/api/accounts/authorize",
            ),
            FakeResponse(status_code=200, text="{}", headers={"content-type": "application/json"}),
        ]
        request_calls = []

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url, "headers": dict(kwargs.get("headers") or {})})
            return responses.pop(0), ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "request_with_local_retry", side_effect=fake_request):
            registrar = openai_register.PlatformRegistrar(proxy="http://legacy-register.example:8080")
            registrar._platform_authorize("user@example.com", 1)

        self.assertEqual(len(request_calls), 2)
        self.assertEqual(len(fake_proxy.refresh_calls), 1)
        retry_headers = {key.lower(): value for key, value in request_calls[1]["headers"].items()}
        self.assertEqual(retry_headers["user-agent"], "Flare UA")
        self.assertEqual(retry_headers["cookie"], "cf_clearance=flare-token")
        self.assertEqual(fake_proxy.refresh_calls[0]["target_url"], openai_register.auth_base)
        self.assertEqual(fake_proxy.refresh_calls[0]["proxy"], "http://legacy-register.example:8080")
        self.assertTrue(fake_proxy.refresh_calls[0]["force"])

    def test_refresh_failure_reports_cloudflare_detail_without_infinite_retry(self):
        fake_proxy = FakeProxySettings(bundle=None)
        cf_response = FakeResponse(
            status_code=403,
            text="<html><title>Just a moment...</title><body>challenge body</body></html>",
            headers={"server": "cloudflare", "content-type": "text/html"},
            url="https://auth.openai.com/api/accounts/authorize",
        )
        request_calls = []

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url})
            return cf_response, ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "request_with_local_retry", side_effect=fake_request):
            registrar = openai_register.PlatformRegistrar(proxy="")
            with self.assertRaisesRegex(RuntimeError, "Cloudflare") as ctx:
                registrar._platform_authorize("user@example.com", 1)

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(len(fake_proxy.refresh_calls), 1)
        message = str(ctx.exception)
        self.assertIn("status=403", message)
        self.assertIn("challenge body", message)

    def test_login_token_exchange_retries_oauth_session_conflict_once(self):
        registrar = openai_register.PlatformRegistrar(proxy="")
        calls = []

        def fake_once(email, password, mailbox, index):
            calls.append((email, password, mailbox, index))
            if len(calls) == 1:
                raise RuntimeError("oauth_token_http_409, status=409, json={\"error\":\"invalid_state\"}")
            return {"access_token": "access", "refresh_token": "refresh", "id_token": "id"}

        try:
            with patch.object(registrar, "_login_and_exchange_tokens_once", side_effect=fake_once):
                result = registrar._login_and_exchange_tokens("user@example.com", "password", {"address": "user@example.com"}, 1)
        finally:
            registrar.close()

        self.assertEqual(result["access_token"], "access")
        self.assertEqual(len(calls), 2)

    def test_create_account_sends_sentinel_and_so_headers_without_logging_token_values(self):
        registrar = openai_register.PlatformRegistrar(proxy="")
        request_calls = []
        log_lines = []

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url, "headers": dict(kwargs.get("headers") or {})})
            return FakeResponse(status_code=200, text='{}', headers={"content-type": "application/json", "Location": "/continue"}), ""

        try:
            with patch.object(
                openai_register,
                "build_sentinel_tokens",
                return_value=openai_register.SentinelTokens("sentinel-secret", "so-secret", "20260124ceb8"),
            ), patch.object(openai_register, "request_with_local_retry", side_effect=fake_request), patch.object(openai_register, "step", side_effect=lambda index, text, color="": log_lines.append(text)):
                continue_url = registrar._create_account("Test User", "2000-01-01", 1)
        finally:
            registrar.close()

        self.assertEqual(continue_url, "/continue")
        headers = request_calls[0]["headers"]
        normalized = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(normalized["openai-sentinel-token"], "sentinel-secret")
        self.assertEqual(normalized["openai-sentinel-so-token"], "so-secret")
        self.assertTrue(any("token_len=15" in line and "so_token=yes" in line and "sdk=20260124ceb8" in line for line in log_lines))
        self.assertFalse(any("sentinel-secret" in line or "so-secret" in line for line in log_lines))

    def test_register_uses_passwordless_signup_after_platform_authorize(self):
        registrar = openai_register.PlatformRegistrar(proxy="")
        calls = []

        try:
            with patch.object(openai_register, "create_mailbox", return_value={"address": "user@example.com"}), patch.object(
                openai_register,
                "wait_for_code",
                return_value="123456",
            ), patch.object(registrar, "_platform_authorize", side_effect=lambda email, index: calls.append("authorize") or "verifier"), patch.object(
                registrar,
                "_submit_email_continue",
                side_effect=lambda email, index, referer="": calls.append("email_continue"),
            ), patch.object(registrar, "_register_user", side_effect=lambda email, password, index: calls.append("password_register")), patch.object(
                registrar,
                "_start_passwordless_signup",
                side_effect=lambda index: calls.append("passwordless_send_otp") or setattr(registrar, "passwordless_signup", True),
            ), patch.object(registrar, "_send_otp", side_effect=lambda index: calls.append("send_otp")), patch.object(
                registrar,
                "_validate_otp",
                side_effect=lambda code, index: calls.append("validate_otp") or "/about-you",
            ), patch.object(
                registrar,
                "_create_account",
                side_effect=lambda name, birthdate, index, referer="": calls.append("create_account") or "/continue",
            ), patch.object(
                registrar,
                "_finish_registration_and_exchange_tokens",
                return_value={"access_token": "access", "refresh_token": "refresh", "id_token": "id"},
            ):
                result = registrar.register(1)
        finally:
            registrar.close()

        self.assertEqual(result["email"], "user@example.com")
        self.assertEqual(calls[:4], ["authorize", "passwordless_send_otp", "validate_otp", "create_account"])
        self.assertEqual(result["password"], "")
        self.assertNotIn("password_register", calls)
        self.assertNotIn("send_otp", calls)
        self.assertNotIn("email_continue", calls)

    def test_register_user_logs_account_creation_failed_diagnostic(self):
        registrar = openai_register.PlatformRegistrar(proxy="")
        response = FakeResponse(status_code=400, headers={"content-type": "application/json"})
        response.json = lambda: {
            "error": {
                "message": "Failed to create account. Please try again.",
                "code": "account_creation_failed",
            }
        }
        lines = []
        try:
            with patch.object(openai_register, "build_sentinel_tokens", return_value=openai_register.SentinelTokens("token")), patch.object(
                openai_register,
                "request_with_local_retry",
                return_value=(response, ""),
            ), patch.object(openai_register, "step", side_effect=lambda index, text, color="": lines.append(text)):
                with self.assertRaisesRegex(RuntimeError, "account_creation_failed"):
                    registrar._register_user("user@example.com", "Password123!", 1)
        finally:
            registrar.close()

        self.assertTrue(any("邮箱域名、IP 或会话风控" in line for line in lines))

    def test_register_user_logs_invalid_auth_step_diagnostic(self):
        registrar = openai_register.PlatformRegistrar(proxy="")
        response = FakeResponse(status_code=400, headers={"content-type": "application/json"})
        response.json = lambda: {
            "error": {
                "message": "Invalid authorization step.",
                "code": "invalid_auth_step",
            }
        }
        lines = []
        try:
            with patch.object(openai_register, "build_sentinel_tokens", return_value=openai_register.SentinelTokens("token")), patch.object(
                openai_register,
                "request_with_local_retry",
                return_value=(response, ""),
            ), patch.object(openai_register, "step", side_effect=lambda index, text, color="": lines.append(text)):
                with self.assertRaisesRegex(RuntimeError, "invalid_auth_step"):
                    registrar._register_user("user@example.com", "Password123!", 1)
        finally:
            registrar.close()

        self.assertTrue(any("会话步骤不匹配" in line for line in lines))

    def test_domain_stats_record_low_success_domain_but_mail_provider_keeps_using_all_domains(self):
        original_file = openai_register.domain_stats_file
        temp_file = Path(__file__).resolve().parent / ".tmp_domain_stats.json"
        try:
            if temp_file.exists():
                temp_file.unlink()
            openai_register.domain_stats_file = temp_file
            mail_provider.set_disabled_domains([])
            for _ in range(openai_register.min_domain_attempts_before_disable):
                openai_register._record_register_domain_result(
                    {"address": "user@bad.example", "provider": "temp", "provider_ref": "temp#1"},
                    False,
                )
            data = openai_register._load_domain_stats()
            self.assertIn("bad.example", data.get("disabled_domains") or [])
            with patch.object(mail_provider.random, "choice", return_value="bad.example") as choose_domain:
                self.assertEqual(mail_provider._next_domain(["bad.example", "good.example"]), "bad.example")
            choose_domain.assert_called_once_with(["bad.example", "good.example"])
        finally:
            openai_register.domain_stats_file = original_file
            mail_provider.set_disabled_domains([])
            if temp_file.exists():
                temp_file.unlink()

    def test_worker_uses_refresh_accounts_after_saving_registered_account(self):
        refreshed_tokens = []
        added_items = []
        fake_result = {
            "email": "user@example.com",
            "password": "secret",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }

        class FakeRegistrar:
            def __init__(self, proxy=""):
                self.proxy = proxy
                self.closed = False

            def register(self, index):
                return dict(fake_result)

            def close(self):
                self.closed = True

        def fake_add(items):
            added_items.extend(items)
            return {"items": items}

        def fake_refresh(tokens):
            refreshed_tokens.extend(tokens)
            return {"refreshed": 1, "errors": [], "items": []}

        original_stats = dict(openai_register.stats)
        try:
            openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 1})
            with patch.object(openai_register, "PlatformRegistrar", FakeRegistrar), patch.object(
                openai_register.account_service,
                "add_account_items",
                side_effect=fake_add,
            ), patch.object(
                openai_register.account_service,
                "refresh_accounts",
                side_effect=fake_refresh,
            ), patch.object(openai_register, "log"):
                result = openai_register.worker(2)
        finally:
            openai_register.stats.update(original_stats)

        self.assertTrue(result["ok"])
        self.assertEqual(refreshed_tokens, ["access-token"])
        self.assertEqual(added_items[0]["source_type"], "web")
        self.assertIn("proxy", added_items[0])


if __name__ == "__main__":
    unittest.main()
