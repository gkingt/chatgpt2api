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


class SentinelOfficialSdkTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()