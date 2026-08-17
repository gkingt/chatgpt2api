from unittest import TestCase, mock

from services import register_service as register_service_module
from services.register import mail_provider


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload or {"address": "user@example.test", "jwt": "mail-token"}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse()

    def close(self):
        pass


class CloudflareTempMailConfigTests(TestCase):
    def test_normalize_enables_manual_level_suffix_by_default(self):
        config = register_service_module._normalize(
            {
                "mail": {
                    "providers": [
                        {
                            "type": "cloudflare_temp_email",
                            "enable": True,
                            "subdomain_levels": ["sfsfe", "grtwrwe"],
                        }
                    ]
                }
            }
        )

        provider = config["mail"]["providers"][0]
        self.assertIs(provider["append_random_suffix"], True)
        self.assertEqual(provider["subdomain_levels"], ["sfsfe", "grtwrwe"])
        self.assertEqual(provider["random_subdomain_depth"], 1)

        config["mail"]["providers"][0]["append_random_suffix"] = False
        normalized = register_service_module._normalize(config)
        self.assertIs(normalized["mail"]["providers"][0]["append_random_suffix"], False)

    def test_manual_levels_are_composed_from_root_outward_with_suffixes(self):
        session = FakeSession()
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        entry = {
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain_levels": ["sfsfe", "grtwrwe"],
        }

        with (
            mock.patch.object(mail_provider, "_create_session", return_value=session),
            mock.patch.object(mail_provider, "_random_subdomain_suffix", side_effect=["a1b2c", "d3e4f"]),
        ):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "grtwrwed3e4f.sfsfea1b2c.example.test")

    def test_random_subdomain_depth_controls_generated_levels(self):
        session = FakeSession()
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        entry = {
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "random_subdomain_depth": 2,
        }

        with (
            mock.patch.object(mail_provider, "_create_session", return_value=session),
            mock.patch.object(mail_provider, "_random_subdomain_label", side_effect=["one", "two"]),
        ):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "one.two.example.test")

    def test_root_domain_is_selected_randomly_from_all_configured_domains(self):
        session = FakeSession()
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        entry = {
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["one.example", "two.example", "three.example"],
            "random_subdomain_depth": 1,
        }

        with (
            mock.patch.object(mail_provider, "_create_session", return_value=session),
            mock.patch.object(mail_provider, "_random_subdomain_label", return_value="box"),
            mock.patch.object(mail_provider.random, "choice", return_value="two.example") as choose_domain,
        ):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "box.two.example")
        choose_domain.assert_any_call(["one.example", "two.example", "three.example"])

    def test_mailnest_provider_buys_temporary_email_and_reads_code_match(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "code": "00000",
                        "msg": "",
                        "type": "",
                        "data": [
                            {
                                "id": "order-1",
                                "email": "chatgpt@example.test",
                                "sale_mode": "temporary",
                                "project_code": "ChatGPT0001",
                            }
                        ],
                    }
                ),
                FakeResponse(
                    {
                        "code": "00000",
                        "msg": "",
                        "type": "",
                        "data": [
                            {
                                "id": "message-1",
                                "subject": "Your verification code",
                                "from_email": "no-reply@openai.com",
                                "body_preview": "Use this code",
                                "body": "<p>Code</p>",
                                "body_type": "html",
                                "code_match": "123456",
                                "received_at": "2026-06-10T12:03:00+08:00",
                            }
                        ],
                    }
                ),
            ]
        )
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        entry = {"api_base": "https://mailnest.example.test", "api_key": "ak-test", "project_code": "ChatGPT0001"}

        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.MailNestProvider(entry, conf)
            mailbox = provider.create_mailbox("user")
            message = provider.fetch_latest_message(mailbox)

        self.assertEqual(mailbox["address"], "chatgpt@example.test")
        self.assertEqual(session.calls[0]["url"], "https://mailnest.example.test/api/v1/email/temporary/buy")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer ak-test")
        self.assertEqual(session.calls[0]["json"], {"count": 1, "project_code": "ChatGPT0001"})
        self.assertEqual(session.calls[1]["url"], "https://mailnest.example.test/api/v1/email/receive")
        self.assertEqual(session.calls[1]["json"], {"email": "chatgpt@example.test"})
        self.assertEqual(mail_provider._extract_code(message or {}), "123456")
