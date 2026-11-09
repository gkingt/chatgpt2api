from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation, openai_v1_models
from services.protocol.conversation import ConversationRequest, ImageGenerationError
from services.image_task_service import ImageTaskService
from test.test_image_task_service import OWNER, wait_for_task
from utils.helper import UpstreamHTTPError, is_codex_image_model, split_image_model
from utils.image_formats import image_format, normalize_image_options, prepare_image_output, validate_svg


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" fill="red"/></svg>'
MODEL = "codex-gpt-image-2.6-sunburst"


def raster(fmt="PNG", animated=False):
    output = io.BytesIO()
    image = Image.new("RGB", (32, 32), "red")
    kwargs = {"save_all": True, "append_images": [Image.new("RGB", (32, 32), "blue")], "duration": 200, "loop": 0} if animated else {}
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


class ImageOptionsTests(unittest.TestCase):
    def test_defaults_and_gif_duration_rounding(self):
        self.assertEqual(normalize_image_options({}), {"output_format": "png"})
        self.assertEqual(normalize_image_options({"output_format": "gif"}), {"output_format": "gif", "frame_count": 9, "frame_duration_ms": 200})
        options = normalize_image_options({"output_format": "gif", "frame_count": "16", "frame_duration_ms": "205", "size": "1024x768", "complexity": "high"})
        self.assertEqual(options["frame_count"], 16)
        self.assertEqual(options["frame_duration_ms"], 210)

    def test_invalid_options_rejected_before_upstream(self):
        for fields in [
            {"output_format": "mp4"}, {"complexity": "max"},
            {"frame_count": 9}, {"frame_duration_ms": 200},
            {"output_format": "svg", "n": 2}, {"output_format": "svg", "n": 0},
            {"output_format": "svg", "stream": True},
            {"output_format": "gif", "partial_images": 0},
            {"output_format": "gif", "output_compression": 80},
            {"output_format": "gif", "frame_count": 1},
            {"output_format": "gif", "frame_count": 17},
            {"output_format": "gif", "frame_count": True},
            {"output_format": "gif", "frame_count": 2.5},
            {"output_format": "gif", "frame_duration_ms": 19},
            {"output_format": "gif", "frame_duration_ms": 2001},
            {"output_format": "gif", "size": "1024x769"},
            {"output_format": "gif", "size": "0x16"},
            {"output_format": "jpeg", "output_compression": 80},
        ]:
            with self.subTest(fields=fields), self.assertRaises(HTTPException) as caught:
                normalize_image_options(fields)
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            normalize_image_options({"output_format": "svg"}, edit=True)

    def test_real_animation_and_vector_bytes_are_preserved(self):
        gif = raster("GIF", animated=True)
        self.assertEqual(prepare_image_output(gif, "gif"), (gif, "gif"))
        self.assertEqual(prepare_image_output(SVG, "svg"), (SVG, "svg"))
        self.assertEqual(image_format(b'<?xml version="1.0"?>' + SVG), "svg")

    def test_static_or_mismatched_outputs_are_not_silently_accepted(self):
        for payload, requested in [(raster(), "gif"), (raster(), "svg"), (raster("GIF"), "gif"), (SVG, "png"), (raster("GIF", True), "jpeg")]:
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                prepare_image_output(payload, requested)

    def test_raster_conversion_has_correct_signature(self):
        for fmt in ("jpeg", "webp"):
            payload, actual = prepare_image_output(raster(), fmt)
            self.assertEqual(actual, fmt)
            self.assertEqual(image_format(payload), fmt)

    def test_svg_static_fragment_references_allowed(self):
        validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="a"/></defs><rect fill="url(#a)"/><use href="#a"/></svg>')

    def test_svg_active_and_remote_content_rejected(self):
        for content in [
            '<script>alert(1)</script>', '<foreignObject/>', '<animate/>',
            '<rect onload="alert(1)"/>', '<use href="https://evil.test/a.svg"/>',
            '<use href="data:image/svg+xml,test"/>', '<image href="x.png"/>',
            '<style>@import "//evil.test/style.css";</style>',
            '<rect style="fill:url(//evil.test/a.svg)"/>',
            '<g xml:base="https://evil.test/"><use href="#a"/></g>',
            '<style>rect{fill:u/**/rl(//evil.test/a)}</style>',
            '<g xmlns="http://www.w3.org/1999/xhtml"><style>body{}</style></g>',
            '<style>rect{<g/>fill:url(//evil.test/a)}</style>',
        ]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                validate_svg(f'<svg xmlns="http://www.w3.org/2000/svg">{content}</svg>'.encode())
        for payload in [b'<!DOCTYPE svg [<!ENTITY a "boom">]><svg>&a;</svg>', '<!DOCTYPE svg><svg/>'.encode("utf-16"), b'<?xml-stylesheet href="https://evil.test/x"?><svg/>']:
            with self.assertRaises(ValueError):
                validate_svg(payload)

    def test_svg_reference_input_rejected(self):
        with self.assertRaises(ImageGenerationError):
            conversation.encode_images([(SVG, "image.png", "image/png")])


class Image26RoutingTests(unittest.TestCase):
    def test_aliases_and_plan_routing(self):
        for name in ("sunburst", "flare"):
            model = f"codex-gpt-image-2.6-{name}"
            self.assertTrue(is_codex_image_model(model))
            self.assertEqual(split_image_model(f"team-{model}"), ("team", model))
        self.assertEqual(split_image_model("gpt-image-2.6"), (None, None))

    def test_experimental_models_follow_account_source(self):
        for source in ("web", "codex"):
            with mock.patch.object(openai_v1_models.OpenAIBackendAPI, "list_models", return_value={"data": []}), mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[{"source_type": source, "type": "Team"}]):
                result = openai_v1_models.list_models()
            experimental = [item for item in result["data"] if "2.6" in item["id"]]
            self.assertEqual(len(experimental), 4 if source == "codex" else 2)
            self.assertTrue(all(item["experimental"] for item in experimental))
            self.assertTrue(all(item["upstream"] == ("codex" if source == "codex" else "chatgpt_web") for item in experimental))

    def test_exact_tool_payload_uses_existing_codex_endpoint(self):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.access_token, backend.base_url = "fake-pool-token", "https://chatgpt.com"
        with mock.patch.object(backend, "_ensure_codex_source_account"), mock.patch.object(backend, "_iter_codex_response_events", return_value=iter([])), mock.patch("services.openai_backend_api.urllib.request.urlopen") as urlopen, mock.patch("services.openai_backend_api.account_service.get_account", return_value={}), mock.patch("services.openai_backend_api.account_service._decode_jwt_payload", return_value={}):
            list(backend.iter_codex_image_response_events("animate", image_model="gpt-image-2.6-flare", output_format="gif", size="auto", frame_count=8, frame_duration_ms=300, complexity="medium"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-pool-token")
        tool = json.loads(request.data)["tools"][0]
        self.assertEqual(tool, {"type": "image_generation", "model": "gpt-image-2.6-flare", "action": "generate", "size": "auto", "quality": "auto", "output_format": "gif", "frame_count": 8, "frame_duration_ms": 300, "complexity": "medium"})

    def test_reference_mime_type_is_preserved_in_codex_payload(self):
        encoded = base64.b64encode(raster("JPEG")).decode()
        item = OpenAIBackendAPI._codex_image_input("edit", [encoded])[0]["content"][1]
        self.assertTrue(item["image_url"].startswith("data:image/jpeg;base64,"))

    def test_old_codex_defaults_unchanged(self):
        backend = mock.Mock()
        backend.iter_codex_image_response_events.return_value = iter([])
        with self.assertRaises(ImageGenerationError):
            list(conversation.stream_codex_image_outputs(backend, ConversationRequest(model="codex-gpt-image-2")))
        self.assertNotIn("image_model", backend.iter_codex_image_response_events.call_args.kwargs)
        self.assertNotIn("output_format", backend.iter_codex_image_response_events.call_args.kwargs)

    def test_stream_deduplicates_completion_events_and_preserves_svg(self):
        encoded = base64.b64encode(SVG).decode()
        item = {"type": "image_generation_call", "result": encoded}
        backend = mock.Mock()
        backend.iter_codex_image_response_events.return_value = iter([{"item": item}, {"response": {"output": [item]}}])
        request = ConversationRequest(model=f"team-{MODEL}", output_format="svg", prompt="icon")
        with mock.patch.object(conversation, "save_image_bytes", return_value="http://app/image.svg") as save:
            result = list(conversation.stream_codex_image_outputs(backend, request))
        self.assertEqual(len(result[0].data), 1)
        self.assertEqual(result[0].data[0]["mime_type"], "image/svg+xml")
        self.assertEqual(backend.iter_codex_image_response_events.call_args.kwargs["image_model"], "gpt-image-2.6-sunburst")
        save.assert_called_once_with(SVG, None)

    def test_unsupported_model_or_wrong_format_never_falls_back(self):
        backend = mock.Mock()
        for error in [UpstreamHTTPError("codex", 400, "unsupported"), None]:
            with self.subTest(error=error):
                backend.iter_codex_image_response_events.side_effect = error
                backend.iter_codex_image_response_events.return_value = iter([{"type": "image_generation_call", "result": base64.b64encode(raster()).decode()}])
                with mock.patch.object(conversation, "save_image_bytes") as save, self.assertRaises(ImageGenerationError):
                    list(conversation.stream_codex_image_outputs(backend, ConversationRequest(model=MODEL, output_format="gif")))
                save.assert_not_called()

    def test_pool_uses_codex_source_and_paid_plan_filter(self):
        with mock.patch.object(conversation.account_service, "get_available_access_token", return_value="token") as acquire, mock.patch.object(conversation.account_service, "get_account", return_value={}), mock.patch.object(conversation.account_service, "mark_image_result"), mock.patch.object(conversation, "OpenAIBackendAPI"), mock.patch.object(conversation, "stream_codex_image_outputs", return_value=iter([conversation.ImageOutput(kind="result", model=MODEL, index=1, total=1, data=[{"url": "x.svg"}])])):
            list(conversation.stream_image_outputs_with_pool(ConversationRequest(model=MODEL, output_format="svg")))
        acquire.assert_called_once_with(plan_type=None, source_type="codex", plan_types=("plus", "team", "pro"))

    def test_web_and_legacy_models_reject_unverified_options(self):
        for model in ("gpt-image-2", "codex-gpt-image-2"):
            with self.subTest(model=model), mock.patch.object(conversation, "_generate_single_image") as generate, self.assertRaises(ImageGenerationError):
                list(conversation.stream_image_outputs_with_pool(ConversationRequest(model=model, output_format="gif")))
            generate.assert_not_called()

    def test_task_options_and_result_metadata_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            seen = []
            def handler(payload):
                seen.append(payload)
                return {"data": [{"url": "http://app/a.gif", "mime_type": "image/gif", "output_format": "gif"}]}
            service = ImageTaskService(path, generation_handler=handler, retention_days_getter=lambda: 30)
            with mock.patch.object(service, "_log_call"):
                service.submit_generation(OWNER, client_task_id="gif", prompt="animation", model=MODEL, size="auto", output_format="gif", frame_count=8, frame_duration_ms=205, complexity="low")
                wait_for_task(service, OWNER, "gif", "success")
            reloaded = ImageTaskService(path, retention_days_getter=lambda: 30)
            task = reloaded.list_tasks(OWNER, ["gif"])["items"][0]
            self.assertEqual(task["output_format"], "gif")
            self.assertEqual(task["frame_duration_ms"], 210)
            self.assertEqual(task["frame_count"], 8)
            self.assertEqual(task["complexity"], "low")
            self.assertEqual(task["data"][0]["mime_type"], "image/gif")
            self.assertEqual(seen[0]["output_format"], "gif")

    def test_resumed_poll_enforces_original_output_format(self):
        for payload, status in [(SVG, "success"), (raster(), "error")]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                service = ImageTaskService(Path(directory) / "tasks.json", retention_days_getter=lambda: 30)
                key = "owner-1:resume"
                service._tasks[key] = {"id": "resume", "owner_id": "owner-1", "status": "running", "output_format": "svg"}
                with mock.patch("services.openai_backend_api.OpenAIBackendAPI") as backend, mock.patch.object(service, "_log_call"), mock.patch.object(conversation, "save_image_bytes", return_value="http://app/a.svg") as save:
                    backend.return_value._poll_image_results.return_value = (["file"], [])
                    backend.return_value.resolve_conversation_image_urls.return_value = ["url"]
                    backend.return_value.download_image_bytes.return_value = [payload]
                    service._run_resume_poll(key, "conversation", 30, OWNER, "generate", MODEL)
                    backend.assert_called_once_with()
                    self.assertEqual(service._tasks[key]["status"], status)
                    self.assertEqual(save.call_count, 1 if status == "success" else 0)


class Image26ApiTests(unittest.TestCase):
    def setUp(self):
        import api.ai as ai
        self.ai = ai
        app = FastAPI()
        app.include_router(ai.create_router())
        self.client = TestClient(app)
        for patcher in [mock.patch.object(ai, "require_identity", return_value=OWNER), mock.patch.object(ai, "filter_or_log", mock.AsyncMock()), mock.patch("services.log_service.log_service.add")]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_generation_json_options_reach_protocol(self):
        from services.protocol import openai_v1_image_generations as protocol
        def fake_outputs(request):
            self.assertEqual(request.output_format, "gif")
            self.assertEqual(request.frame_count, 8)
            self.assertEqual(request.frame_duration_ms, 210)
            self.assertEqual(request.complexity, "medium")
            return iter([conversation.ImageOutput(kind="result", model=MODEL, index=1, total=1, data=[{"url": "http://app/a.gif", "mime_type": "image/gif", "output_format": "gif"}])])
        with mock.patch.object(protocol, "stream_image_outputs_with_pool", side_effect=fake_outputs):
            response = self.client.post("/v1/images/generations", json={"model": MODEL, "prompt": "animate", "output_format": "gif", "frame_count": 8, "frame_duration_ms": 205, "complexity": "medium", "size": "auto"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["mime_type"], "image/gif")

    def test_invalid_gif_stream_is_400_without_account_lookup(self):
        with mock.patch.object(conversation.account_service, "get_available_access_token") as acquire:
            response = self.client.post("/v1/images/generations", json={"model": MODEL, "prompt": "animate", "output_format": "gif", "stream": True})
        self.assertEqual(response.status_code, 400, response.text)
        acquire.assert_not_called()

    def test_svg_json_edit_rejected(self):
        response = self.client.post("/v1/images/edits", json={"model": MODEL, "prompt": "icon", "output_format": "svg", "image_url": "data:image/png;base64," + base64.b64encode(raster()).decode()})
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
