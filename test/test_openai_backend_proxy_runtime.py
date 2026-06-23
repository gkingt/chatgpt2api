import unittest
from unittest.mock import patch

from services import openai_backend_api


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}


class FakeProxySettings:
    def __init__(self):
        self.session_kwargs_calls = []
        self.build_headers_calls = []

    def build_session_kwargs(self, **kwargs):
        self.session_kwargs_calls.append(kwargs)
        return dict(kwargs, proxy="http://runtime.example:8118")

    def build_headers(self, headers=None, **kwargs):
        self.build_headers_calls.append({"headers": dict(headers or {}), **kwargs})
        merged = dict(headers or {})
        merged["Cookie"] = "cf_clearance=runtime-token"
        return merged


class OpenAIBackendProxyRuntimeTests(unittest.TestCase):
    def test_backend_session_uses_upstream_runtime_proxy_scope(self):
        fake_proxy = FakeProxySettings()
        created = []

        def fake_session_factory(**kwargs):
            session = FakeSession(**kwargs)
            created.append(session)
            return session

        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api.requests,
            "Session",
            side_effect=fake_session_factory,
        ):
            api = openai_backend_api.OpenAIBackendAPI()

        self.assertIs(api.session, created[0])
        self.assertTrue(fake_proxy.session_kwargs_calls[0]["upstream"])
        self.assertEqual(api.session.kwargs["proxy"], "http://runtime.example:8118")

    def test_backend_headers_merge_runtime_clearance(self):
        fake_proxy = FakeProxySettings()
        with patch.object(openai_backend_api, "proxy_settings", fake_proxy), patch.object(
            openai_backend_api.requests,
            "Session",
            side_effect=lambda **kwargs: FakeSession(**kwargs),
        ):
            api = openai_backend_api.OpenAIBackendAPI()
            headers = api._headers("/backend-api/f/conversation", {"Accept": "text/event-stream"})

        self.assertEqual(headers["Cookie"], "cf_clearance=runtime-token")
        self.assertEqual(fake_proxy.build_headers_calls[0]["target_url"], "https://chatgpt.com/backend-api/f/conversation")
        self.assertTrue(fake_proxy.build_headers_calls[0]["upstream"])


if __name__ == "__main__":
    unittest.main()
