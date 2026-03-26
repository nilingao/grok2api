import unittest
from pathlib import Path
import sys

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.grok.services.image_edit import ImageCollectProcessor, ImageStreamProcessor


class _FakeDownloadService:
    def __init__(self):
        self.resolve_calls = 0

    async def parse_b64(self, *_args, **_kwargs):
        raise RuntimeError("boom")

    async def resolve_url(self, _url: str, _token: str, _media_type: str = "image") -> str:
        self.resolve_calls += 1
        return "https://example.com/image.jpg"

    async def close(self):
        return None


async def _one_line_stream(url: str):
    payload = {
        "result": {
            "response": {
                "modelResponse": {
                    "generatedImageUrls": [url],
                }
            }
        }
    }
    yield orjson.dumps(payload)


class ImageCollectNoUrlFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_base64_parse_fail_does_not_fallback_to_url(self):
        processor = ImageCollectProcessor("test-model", "token", response_format="b64_json")
        fake_dl = _FakeDownloadService()
        processor._dl_service = fake_dl

        images = await processor.process(_one_line_stream("users/u1/generated/p1/image.jpg"))

        self.assertEqual(images, [])
        self.assertEqual(fake_dl.resolve_calls, 0)


class ImageStreamNoUrlFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_base64_parse_fail_does_not_fallback_to_url(self):
        processor = ImageStreamProcessor(
            "test-model", "token", n=1, response_format="base64"
        )
        fake_dl = _FakeDownloadService()
        processor._dl_service = fake_dl

        chunks = []
        async for chunk in processor.process(
            _one_line_stream("users/u1/generated/p1/image.jpg")
        ):
            chunks.append(chunk)

        self.assertEqual(chunks, [])
        self.assertEqual(fake_dl.resolve_calls, 0)
