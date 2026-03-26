import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.grok.services.image_edit import ImageEditService
from app.services.grok.services.image_edit import ImageCollectProcessor, ImageStreamProcessor
from app.services.grok.services.model import ModelService
from app.services.grok.utils.process import _collect_images
from app.services.reverse.app_chat import AppChatReverse


class _FakeDownloadService:
    def __init__(self):
        self.resolve_calls = 0

    async def parse_b64(self, *_args, **_kwargs):
        return ""

    async def resolve_url(self, url: str, _token: str, _media_type: str = "image") -> str:
        self.resolve_calls += 1
        return url

    async def close(self):
        return None


class _FakeDownloadServiceWithB64(_FakeDownloadService):
    async def parse_b64(self, *_args, **_kwargs):
        return "data:image/png;base64,Zm9v"


def _card_attachment_payload(
    image_url: str,
    progress: int = 100,
    card_type: str = "render_generated_image",
) -> dict:
    card = {
        "type": card_type,
        "jsonData": orjson.dumps(
            {
                "image_chunk": [
                    {"progress": progress, "imageUrl": image_url},
                ]
            }
        ).decode(),
    }
    return {
        "result": {
            "response": {
                "cardAttachment": card,
            }
        }
    }


async def _card_attachment_only_stream(
    image_url: str,
    progress: int = 100,
    card_type: str = "render_generated_image",
):
    yield orjson.dumps(
        _card_attachment_payload(image_url, progress=progress, card_type=card_type)
    )


def _parse_sse_data(chunk: str) -> dict:
    for line in str(chunk).splitlines():
        if line.startswith("data:"):
            return orjson.loads(line[5:].strip())
    return {}


class ImageCardAttachmentExtractionTests(unittest.TestCase):
    def test_collect_images_extracts_progress_100_image_chunk_urls(self):
        card = {
            "type": "render_generated_image",
            "jsonData": orjson.dumps(
                {
                    "image_chunk": [
                        {"progress": 20, "imageUrl": "users/u1/generated/p1/incomplete.jpg"},
                        {"progress": 100, "imageUrl": "users/u1/generated/p1/final.jpg"},
                    ]
                }
            ).decode(),
        }
        payload = {
            "cardAttachmentsJson": [orjson.dumps(card).decode()],
        }
        urls = _collect_images(payload)

        self.assertEqual(
            urls,
            ["https://assets.grok.com/users/u1/generated/p1/final.jpg"],
        )

    def test_collect_images_extracts_render_edited_image_chunk_urls(self):
        card = {
            "type": "render_edited_image",
            "jsonData": orjson.dumps(
                {
                    "image_chunk": [
                        {"progress": 100, "imageUrl": "users/u1/generated/p1/edited.jpg"},
                    ]
                }
            ).decode(),
        }
        payload = {
            "cardAttachmentsJson": [orjson.dumps(card).decode()],
        }
        urls = _collect_images(payload)

        self.assertEqual(
            urls,
            ["https://assets.grok.com/users/u1/generated/p1/edited.jpg"],
        )


class ImageCardAttachmentProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_processor_supports_card_attachment_image_chunk(self):
        processor = ImageCollectProcessor("test-model", "token", response_format="url")
        processor._dl_service = _FakeDownloadService()

        images = await processor.process(
            _card_attachment_only_stream("users/u1/generated/p1/image.jpg")
        )

        self.assertEqual(images, ["https://assets.grok.com/users/u1/generated/p1/image.jpg"])

    async def test_stream_processor_supports_card_attachment_image_chunk(self):
        processor = ImageStreamProcessor("test-model", "", n=1, response_format="url")
        processor._dl_service = _FakeDownloadService()

        chunks = []
        async for chunk in processor.process(
            _card_attachment_only_stream("users/u1/generated/p1/image.jpg")
        ):
            chunks.append(chunk)

        completed = [
            _parse_sse_data(chunk)
            for chunk in chunks
            if "image_generation.completed" in str(chunk)
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(
            completed[0].get("url"),
            "https://assets.grok.com/users/u1/generated/p1/image.jpg",
        )

    async def test_stream_processor_supports_render_edited_image_card_attachment(self):
        processor = ImageStreamProcessor("test-model", "", n=1, response_format="url")
        processor._dl_service = _FakeDownloadService()

        chunks = []
        async for chunk in processor.process(
            _card_attachment_only_stream(
                "users/u1/generated/p1/edited.jpg",
                card_type="render_edited_image",
            )
        ):
            chunks.append(chunk)

        completed = [
            _parse_sse_data(chunk)
            for chunk in chunks
            if "image_generation.completed" in str(chunk)
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(
            completed[0].get("url"),
            "https://assets.grok.com/users/u1/generated/p1/edited.jpg",
        )

    async def test_stream_share_link_uses_source_url_instead_of_b64(self):
        processor = ImageStreamProcessor("test-model", "sso=test", n=1, response_format="b64_json")
        processor._dl_service = _FakeDownloadServiceWithB64()
        captured = {}
        image_post_id = "11111111-2222-3333-4444-555555555555"
        image_url = f"users/u1/generated/{image_post_id}/image.jpg"

        async def _fake_share_link(token, post_id, *, local_url=""):
            captured["token"] = token
            captured["post_id"] = post_id
            captured["local_url"] = local_url

        with patch(
            "app.services.grok.services.image_edit._try_log_image_share_link",
            new=_fake_share_link,
        ):
            chunks = []
            async for chunk in processor.process(
                _card_attachment_only_stream(image_url)
            ):
                chunks.append(chunk)

        completed = [
            _parse_sse_data(chunk)
            for chunk in chunks
            if "image_generation.completed" in str(chunk)
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(captured.get("token"), "sso=test")
        self.assertEqual(captured.get("post_id"), image_post_id)
        self.assertEqual(
            captured.get("local_url"),
            f"https://assets.grok.com/users/u1/generated/{image_post_id}/image.jpg",
        )


class ImageModelRoutingTests(unittest.TestCase):
    def test_image_generation_model_uses_auto_mode(self):
        model = ModelService.get("grok-imagine-1.0")
        self.assertIsNotNone(model)
        self.assertEqual(model.grok_model, "grok-420")
        self.assertEqual(model.model_mode, "auto")

    def test_image_generation_payload_uses_mode_id_auto(self):
        model = ModelService.get("grok-imagine-1.0")
        payload = AppChatReverse.build_payload(
            message="draw a cat",
            model=model.grok_model,
            mode=model.model_mode,
            tool_overrides={"imageGen": True},
        )

        self.assertEqual(payload.get("modeId"), "auto")
        self.assertNotIn("modelMode", payload)

    def test_image_edit_model_uses_auto_mode(self):
        model = ModelService.get("grok-imagine-1.0-edit")
        self.assertIsNotNone(model)
        self.assertEqual(model.grok_model, "grok-420")
        self.assertEqual(model.model_mode, "auto")

    def test_image_edit_payload_uses_mode_id_auto(self):
        model = ModelService.get("grok-imagine-1.0-edit")
        payload = AppChatReverse.build_payload(
            message="edit image",
            model=model.grok_model,
            mode=model.model_mode,
            tool_overrides={"imageGen": True},
            model_config_override={
                "modelMap": {
                    "imageEditModel": "imagine",
                    "imageEditModelConfig": {
                        "imageReferences": [
                            "https://assets.grok.com/users/u1/generated/p1/image.jpg"
                        ],
                    },
                }
            },
        )

        self.assertEqual(payload.get("modeId"), "auto")
        self.assertNotIn("modelMode", payload)


class ImageEditModePropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_edit_collect_path_passes_auto_mode(self):
        service = ImageEditService()
        model_info = SimpleNamespace(
            model_id="grok-imagine-1.0-edit",
            grok_model="grok-420",
            model_mode="auto",
        )
        captured = {}

        async def _empty_stream():
            if False:
                yield b""

        async def _fake_chat(_self, **kwargs):
            captured.update(kwargs)
            return _empty_stream()

        async def _fake_process(_self, _response):
            return ["https://assets.grok.com/users/u1/generated/p1/image.jpg"]

        with patch(
            "app.services.grok.services.image_edit.GrokChatService.chat",
            new=_fake_chat,
        ), patch(
            "app.services.grok.services.image_edit.ImageCollectProcessor.process",
            new=_fake_process,
        ):
            images = await service._collect_images(
                token="",
                prompt="edit this image",
                model_info=model_info,
                response_format="url",
                tool_overrides={"imageGen": True, "webSearch": False},
                model_config_override={
                    "modelMap": {
                        "imageEditModel": "imagine",
                        "imageEditModelConfig": {
                            "imageReferences": [
                                "https://assets.grok.com/users/u1/generated/p1/image.jpg"
                            ]
                        },
                    }
                },
                return_all_images=True,
            )

        self.assertEqual(images, ["https://assets.grok.com/users/u1/generated/p1/image.jpg"])
        self.assertEqual(captured.get("model"), "grok-420")
        self.assertEqual(captured.get("mode"), "auto")
        self.assertEqual(
            captured.get("request_overrides"),
            {"disableMemory": False, "temporary": False},
        )
        self.assertEqual(
            captured.get("tool_overrides"),
            {"imageGen": True, "webSearch": False},
        )
