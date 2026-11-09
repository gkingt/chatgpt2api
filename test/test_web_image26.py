from __future__ import annotations

import unittest
from unittest import mock

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.account_service import AccountService
from services.protocol import conversation, openai_v1_models
from services.protocol.conversation import ConversationRequest, ImageGenerationError, ImageOutput
from test.test_image26 import raster
from utils.helper import IMAGE_26_MODELS, UpstreamHTTPError, is_codex_image_model, is_supported_image_model


WEB_MODEL = "gpt-image-2.6-sunburst"


class WebImage26Tests(unittest.TestCase):
    def test_web_slugs_are_not_mapped_to_legacy_chat_model(self):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        self.assertEqual(backend._image_model_slug("gpt-image-2"), "gpt-5-3")
        for model in IMAGE_26_MODELS:
            self.assertTrue(is_supported_image_model(model))
            self.assertFalse(is_codex_image_model(model))
            self.assertEqual(backend._image_model_slug(model), model)

    def test_prepare_and_generation_both_use_exact_web_slug(self):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.session = mock.Mock()
        backend.session.post.return_value.status_code = 200
        backend.session.post.return_value.json.return_value = {"conduit_token": "conduit"}
        with mock.patch.object(backend, "_image_headers", return_value={}):
            requirements = ChatRequirements(token="requirements")
            self.assertEqual(backend._prepare_image_conversation("animate", requirements, WEB_MODEL), "conduit")
            backend._start_image_generation("animate", requirements, "conduit", WEB_MODEL)
        calls = backend.session.post.call_args_list
        self.assertEqual(calls[0].args[0], "https://chatgpt.com/backend-api/f/conversation/prepare")
        self.assertEqual(calls[1].args[0], "https://chatgpt.com/backend-api/f/conversation")
        for call in calls:
            payload = call.kwargs["json"]
            self.assertEqual(payload["model"], WEB_MODEL)
            self.assertEqual(payload["system_hints"], ["picture_v2"])
            self.assertNotIn("output_format", payload)
            self.assertNotIn("frame_count", payload)

    def test_gif_prompt_timing_reaches_actual_web_conversation(self):
        backend = mock.Mock()
        backend.stream_conversation.return_value = iter([])
        with mock.patch.object(conversation, "prompt_with_global_system", side_effect=lambda value: value):
            list(conversation.conversation_events(backend, model=WEB_MODEL, prompt="a lantern", size="auto", output_format="gif", frame_count=8, frame_duration_ms=205))
        kwargs = backend.stream_conversation.call_args.kwargs
        self.assertEqual(kwargs["model"], WEB_MODEL)
        self.assertEqual(kwargs["system_hints"], ["picture_v2"])
        self.assertIn("8 帧，每帧 210 毫秒", kwargs["prompt"])
        self.assertIn("a lantern", kwargs["prompt"])

    def test_chat_and_responses_preserve_web_gif_options(self):
        from services.protocol import openai_v1_chat_complete as chat, openai_v1_response as responses
        options = {"output_format": "gif", "frame_count": 8, "frame_duration_ms": 300, "size": "auto"}
        def outputs(request):
            self.assertEqual(request.model, WEB_MODEL)
            self.assertEqual(request.output_format, "gif")
            self.assertEqual(request.frame_count, 8)
            self.assertEqual(request.frame_duration_ms, 300)
            self.assertEqual(request.size, "auto")
            return iter([ImageOutput(kind="result", model=WEB_MODEL, index=1, total=1, data=[{"b64_json": "R0lG", "mime_type": "image/gif", "output_format": "gif"}])])
        with mock.patch.object(chat, "stream_image_outputs_with_pool", side_effect=outputs):
            chat.handle({"model": WEB_MODEL, "messages": [{"role": "user", "content": "animate"}], **options})
        with mock.patch.object(responses, "stream_image_outputs_with_pool", side_effect=outputs):
            response = responses.handle({"model": WEB_MODEL, "input": "animate", "tools": [{"type": "image_generation", **options}]})
        self.assertEqual(response["output"][0]["mime_type"], "image/gif")

    def test_legacy_prompt_is_unchanged(self):
        self.assertEqual(conversation.build_image_prompt("cat", "1024x1024", "low"), "cat\n\n输出图片尺寸为 1024x1024。输出图片质量为 low。")

    def test_web_pool_never_selects_codex_account(self):
        output = ImageOutput(kind="result", model=WEB_MODEL, index=1, total=1, data=[{"url": "http://app/a.gif"}])
        with mock.patch.object(conversation.account_service, "get_available_access_token", return_value="web-token") as acquire, mock.patch.object(conversation.account_service, "get_account", return_value={}), mock.patch.object(conversation.account_service, "mark_image_result"), mock.patch.object(conversation, "OpenAIBackendAPI"), mock.patch.object(conversation, "stream_image_outputs", return_value=iter([output])), mock.patch.object(conversation, "stream_codex_image_outputs") as codex:
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(model=WEB_MODEL, output_format="gif")))
        acquire.assert_called_once_with(plan_type=None, source_type="chatgpt_web", plan_types=None)
        codex.assert_not_called()

    def test_web_credentials_include_password_and_oauth_imports(self):
        for source in (None, "web", "password", "oauth_login"):
            self.assertTrue(AccountService._account_matches_source_type({"source_type": source}, "chatgpt_web"))
            with mock.patch.object(openai_v1_models.OpenAIBackendAPI, "list_models", return_value={"data": []}), mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[{"source_type": source}]):
                ids = {item["id"] for item in openai_v1_models.list_models()["data"]}
            self.assertIn(WEB_MODEL, ids)
        self.assertFalse(AccountService._account_matches_source_type({"source_type": "codex"}, "chatgpt_web"))

    def test_native_web_gif_is_preserved_and_mismatch_fails_before_save(self):
        for payload, success in [(raster("GIF", True), True), (raster(), False), (raster("GIF"), False)]:
            with self.subTest(success=success):
                backend = mock.Mock()
                backend.resolve_conversation_image_urls.return_value = ["http://upstream/image"]
                backend.download_image_bytes.return_value = [payload]
                request = ConversationRequest(model=WEB_MODEL, prompt="animate", output_format="gif", frame_count=8)
                event = {"conversation_id": "conv", "file_ids": ["file"], "text": ""}
                with mock.patch.object(conversation, "conversation_events", return_value=iter([event])) as events, mock.patch.object(conversation, "save_image_bytes", return_value="http://app/a.gif") as save, mock.patch.object(conversation, "_remove_image_conversation_later"):
                    if success:
                        outputs = list(conversation.stream_image_outputs(backend, request))
                        self.assertEqual(outputs[0].data[0]["mime_type"], "image/gif")
                        save.assert_called_once_with(payload, None)
                    else:
                        with self.assertRaises(ImageGenerationError) as caught:
                            list(conversation.stream_image_outputs(backend, request))
                        self.assertEqual(caught.exception.code, "upstream_image_format_error")
                        save.assert_not_called()
                    self.assertEqual(events.call_args.kwargs["frame_count"], 8)
                    self.assertEqual(events.call_args.kwargs["output_format"], "gif")

    def test_unsupported_web_options_and_gif_edit_fail_before_account_selection(self):
        for options in [{"output_format": "svg"}, {"output_format": "jpeg"}, {"output_format": "webp"}, {"complexity": "low"}, {"output_format": "gif", "images": ["reference"]}]:
            with self.subTest(options=options), mock.patch.object(conversation.account_service, "get_available_access_token") as acquire, self.assertRaises(ImageGenerationError) as caught:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(model=WEB_MODEL, **options)))
            self.assertEqual(caught.exception.status_code, 400)
            acquire.assert_not_called()

    def test_upstream_rejection_does_not_switch_model_or_account(self):
        with mock.patch.object(conversation.account_service, "get_available_access_token", return_value="web-token") as acquire, mock.patch.object(conversation.account_service, "get_account", return_value={}), mock.patch.object(conversation.account_service, "mark_image_result"), mock.patch.object(conversation, "OpenAIBackendAPI"), mock.patch.object(conversation, "stream_image_outputs", side_effect=UpstreamHTTPError("web", 400, "unsupported model")) as stream:
            with self.assertRaises(ImageGenerationError) as caught:
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(model=WEB_MODEL)))
        self.assertEqual(caught.exception.code, "upstream_image_capability_error")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(stream.call_count, 1)

    def test_web_http400_quota_error_still_rotates_accounts(self):
        output = ImageOutput(kind="result", model=WEB_MODEL, index=1, total=1, data=[{"url": "http://app/a.png"}])
        quota_error = UpstreamHTTPError("web", 400, {"error": "image quota exceeded"})
        with mock.patch.object(conversation.account_service, "get_available_access_token", side_effect=["exhausted", "available"]) as acquire, mock.patch.object(conversation.account_service, "get_account", return_value={}), mock.patch.object(conversation.account_service, "mark_image_result"), mock.patch.object(conversation.account_service, "mark_image_quota_exhausted_token") as mark_quota, mock.patch.object(conversation, "OpenAIBackendAPI"), mock.patch.object(conversation, "stream_image_outputs", side_effect=[quota_error, iter([output])]):
            self.assertEqual(list(conversation.stream_image_outputs_with_pool(ConversationRequest(model=WEB_MODEL))), [output])
        self.assertEqual(acquire.call_count, 2)
        mark_quota.assert_called_once_with("exhausted", "image_stream_error", str(quota_error))

    def test_codex_http400_quota_is_not_replaced_by_capability_error(self):
        backend = mock.Mock()
        quota_error = UpstreamHTTPError("codex", 400, {"error": "image quota exceeded"})
        backend.iter_codex_image_response_events.side_effect = quota_error
        with self.assertRaises(UpstreamHTTPError) as caught:
            list(conversation.stream_codex_image_outputs(backend, ConversationRequest(model="codex-" + WEB_MODEL)))
        self.assertIs(caught.exception, quota_error)

    def test_no_accounts_does_not_advertise_new_models(self):
        with mock.patch.object(openai_v1_models.OpenAIBackendAPI, "list_models", return_value={"data": []}), mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[]):
            self.assertEqual(openai_v1_models.list_models()["data"], [])


if __name__ == "__main__":
    unittest.main()
