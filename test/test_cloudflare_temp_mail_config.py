from unittest import TestCase, mock

from services import register_service as register_service_module
from services.register import mail_provider


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"address": "user@example.test", "jwt": "mail-token"}


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

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
