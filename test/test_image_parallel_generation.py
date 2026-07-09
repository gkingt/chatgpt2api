from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageOutput, is_tls_connection_error


class ImageParallelGenerationTests(unittest.TestCase):
    def test_image_stream_connection_abort_errors_are_retryable(self):
        self.assertTrue(is_tls_connection_error("curl: (56) Connection closed abruptly"))
        self.assertTrue(is_tls_connection_error("curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR"))

    def test_parallel_generation_yields_fast_result_and_keeps_waiting_for_slow_peer(self):
        calls: list[int] = []

        def fake_generate(request: ConversationRequest, index: int, total: int):
            calls.append(index)
            if index == 1:
                return [
                    ImageOutput(
                        kind="result",
                        model=request.model,
                        index=index,
                        total=total,
                        data=[{"b64_json": "ok"}],
                    )
                ]
            time.sleep(2.0)
            return [
                ImageOutput(
                    kind="result",
                    model=request.model,
                    index=index,
                    total=total,
                    data=[{"b64_json": "slow"}],
                )
            ]

        request = ConversationRequest(prompt="test", model="gpt-image-2", n=1)
        request.n = 2
        started = time.time()
        original_data = dict(conversation.config.data)
        try:
            conversation.config.data["image_parallel_generation"] = True
            with patch.object(
                conversation,
                "_generate_single_image",
                side_effect=fake_generate,
            ):
                outputs = list(conversation.stream_image_outputs_with_pool(request))
        finally:
            conversation.config.data.clear()
            conversation.config.data.update(original_data)

        self.assertGreaterEqual(time.time() - started, 2.0)
        self.assertEqual([output.data for output in outputs], [[{"b64_json": "ok"}], [{"b64_json": "slow"}]])
        self.assertEqual(outputs[0].data, [{"b64_json": "ok"}])
        self.assertCountEqual(calls, [1, 2])


if __name__ == "__main__":
    unittest.main()
