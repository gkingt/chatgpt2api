import io
import json
import queue
import unittest
from unittest.mock import patch

from utils import sentinel


class FakeStdout:
    def __init__(self):
        self.lines = queue.Queue()

    def put(self, payload):
        self.lines.put(json.dumps(payload) + "\n")

    def close(self):
        self.lines.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get(timeout=5)
        if line is None:
            raise StopIteration
        return line


class FakeStdin:
    def __init__(self, stdout):
        self.stdout = stdout
        self.response_count = 0

    def write(self, text):
        message = json.loads(text)
        if message["type"] == "start":
            self.stdout.put({"type": "sentinel_req", "requestId": "req-1", "flow": message["flow"], "p": "p1"})
            return
        if message["type"] == "sentinel_req_result":
            self.response_count += 1
            if self.response_count == 1:
                self.stdout.put({"type": "sentinel_req", "requestId": "req-2", "flow": "oauth_create_account", "p": "p2"})
            else:
                self.stdout.put({"type": "result", "token": '{"c":"cookie-token"}', "soToken": "so-token"})
                self.stdout.close()

    def flush(self):
        pass


class FakeProc:
    def __init__(self):
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self.stdout)
        self.stderr = io.StringIO("")
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0

    def kill(self):
        self.terminated = True


class BrokenPipeStdin:
    def write(self, text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


class BrokenPipeProc(FakeProc):
    def __init__(self):
        super().__init__()
        self.stdin = BrokenPipeStdin()
        self.stderr = io.StringIO("runner exploded")


class CleanClosedOnSecondResponseStdin:
    def __init__(self, proc):
        self.proc = proc
        self.response_count = 0

    def write(self, text):
        message = json.loads(text)
        if message["type"] == "start":
            self.proc.stdout.put({"type": "sentinel_req", "requestId": "req-1", "flow": message["flow"], "p": "p1"})
            return
        if message["type"] == "sentinel_req_result":
            self.response_count += 1
            if self.response_count == 1:
                self.proc.stdout.put({"type": "sentinel_req", "requestId": "req-2", "flow": "oauth_create_account", "p": "p2"})
                self.proc.stdout.put({"type": "result", "token": '{"c":"cookie-token"}', "soToken": "so-token"})
                self.proc.stdout.close()
                self.proc.terminated = True
                return
            raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


class CleanClosedOnSecondResponseProc(FakeProc):
    def __init__(self):
        super().__init__()
        self.stdin = CleanClosedOnSecondResponseStdin(self)


class SentinelOfficialSdkTests(unittest.TestCase):
    def test_load_current_sdk_falls_back_to_urllib_when_session_is_forbidden(self):
        class ForbiddenResponse:
            status_code = 403
            text = "forbidden"

        class ForbiddenSession:
            def get(self, *args, **kwargs):
                return ForbiddenResponse()

        fallback_calls = []

        def fake_urllib_fetch(url, headers, timeout=20):
            fallback_calls.append(url)
            if url == sentinel.SENTINEL_SDK_BOOTSTRAP_URL:
                return 200, "script.src='https://sentinel.openai.com/sentinel/current/sdk.js'"
            return 200, "window.SentinelSDK = {};"

        with patch.object(sentinel, "_fetch_sdk_text_with_urllib", side_effect=fake_urllib_fetch):
            source, sdk_url, sdk_version = sentinel._load_current_sdk(
                ForbiddenSession(),
                "ua",
                '"Chromium";v="145"',
            )

        self.assertEqual(source, "window.SentinelSDK = {};")
        self.assertEqual(sdk_url, "https://sentinel.openai.com/sentinel/current/sdk.js")
        self.assertEqual(sdk_version, "current")
        self.assertEqual(fallback_calls, [sentinel.SENTINEL_SDK_BOOTSTRAP_URL, sdk_url])

    def test_post_sentinel_req_falls_back_to_urllib_when_session_is_forbidden(self):
        class ForbiddenResponse:
            status_code = 403
            text = "forbidden"

            def json(self):
                return {}

        class ForbiddenSession:
            def post(self, *args, **kwargs):
                return ForbiddenResponse()

        with patch.object(
            sentinel,
            "_post_sentinel_req_with_urllib",
            return_value=(200, {"token": "sentinel-token", "so": {"required": True}}),
        ) as fallback:
            data = sentinel._post_sentinel_req(
                ForbiddenSession(),
                sdk_url="https://sentinel.openai.com/sentinel/current/sdk.js",
                sdk_version="current",
                flow="oauth_create_account",
                p_value="payload",
                device_id="device-id",
                user_agent="ua",
                sec_ch_ua='"Chromium";v="145"',
            )

        self.assertEqual(data["token"], "sentinel-token")
        fallback.assert_called_once()

    def test_official_sdk_replies_to_multiple_sentinel_reqs(self):
        post_calls = []

        def fake_post_req(session, **kwargs):
            post_calls.append(kwargs["p_value"])
            return {"token": f"token-{len(post_calls)}", "so": {"required": True}}

        with patch.object(sentinel.shutil, "which", return_value="node"), patch.object(
            sentinel,
            "_load_current_sdk",
            return_value=("sdk-source", "https://sentinel.openai.com/sentinel/test/sdk.js", "test"),
        ), patch.object(sentinel.subprocess, "Popen", return_value=FakeProc()), patch.object(
            sentinel,
            "_post_sentinel_req",
            side_effect=fake_post_req,
        ):
            bundle = sentinel._run_official_sdk(
                object(),
                "device-id",
                "oauth_create_account",
                user_agent="ua",
                sec_ch_ua='"Chromium";v="145"',
                include_so=True,
                observer_wait_ms=0,
            )

        self.assertEqual(post_calls, ["p1", "p2"])
        self.assertEqual(bundle.so_token, "so-token")
        self.assertEqual(bundle.requirements_token_length, 2)
        self.assertTrue(bundle.sentinel_req_so_required)

    def test_official_sdk_wraps_broken_pipe_with_runner_detail(self):
        with patch.object(sentinel.shutil, "which", return_value="node"), patch.object(
            sentinel,
            "_load_current_sdk",
            return_value=("sdk-source", "https://sentinel.openai.com/sentinel/test/sdk.js", "test"),
        ), patch.object(sentinel.subprocess, "Popen", return_value=BrokenPipeProc()):
            with self.assertRaisesRegex(RuntimeError, "sentinel_sdk_pipe_write_failed") as ctx:
                sentinel._run_official_sdk(
                    object(),
                    "device-id",
                    "oauth_create_account",
                    user_agent="ua",
                    sec_ch_ua='"Chromium";v="145"',
                    include_so=True,
                    observer_wait_ms=0,
                )

        self.assertIn("context=start", str(ctx.exception))
        self.assertIn("runner exploded", str(ctx.exception))

    def test_official_sdk_ignores_late_second_req_after_clean_exit(self):
        post_calls = []

        def fake_post_req(session, **kwargs):
            post_calls.append(kwargs["p_value"])
            return {"token": f"token-{len(post_calls)}", "so": {"required": True}}

        with patch.object(sentinel.shutil, "which", return_value="node"), patch.object(
            sentinel,
            "_load_current_sdk",
            return_value=("sdk-source", "https://sentinel.openai.com/sentinel/test/sdk.js", "test"),
        ), patch.object(sentinel.subprocess, "Popen", return_value=CleanClosedOnSecondResponseProc()), patch.object(
            sentinel,
            "_post_sentinel_req",
            side_effect=fake_post_req,
        ):
            bundle = sentinel._run_official_sdk(
                object(),
                "device-id",
                "oauth_create_account",
                user_agent="ua",
                sec_ch_ua='"Chromium";v="145"',
                include_so=True,
                observer_wait_ms=0,
            )

        self.assertEqual(post_calls, ["p1", "p2"])
        self.assertEqual(bundle.so_token, "so-token")


if __name__ == "__main__":
    unittest.main()