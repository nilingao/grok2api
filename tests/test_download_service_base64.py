import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.grok.utils.download import DownloadService


class DownloadServiceNormalizePathTests(unittest.TestCase):
    def setUp(self):
        self.service = DownloadService()

    def test_normalize_raw_users_path(self):
        path = "users/u1/generated/p1/image.jpg"
        self.assertEqual(
            self.service._normalize_path(path),
            "/users/u1/generated/p1/image.jpg",
        )

    def test_normalize_local_file_url_path(self):
        path = "https://example.com/v1/files/image/users/u1/generated/p1/image.jpg"
        self.assertEqual(
            self.service._normalize_path(path),
            "/users/u1/generated/p1/image.jpg",
        )

    def test_normalize_preserves_query(self):
        path = "https://example.com/v1/files/image/users/u1/generated/p1/image.jpg?x=1"
        self.assertEqual(
            self.service._normalize_path(path),
            "/users/u1/generated/p1/image.jpg?x=1",
        )


class DownloadServiceParseB64Tests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_b64_accepts_users_path(self):
        class _Resp:
            headers = {"content-type": "image/jpeg"}
            content = b"abc"

        service = DownloadService()
        with patch.object(service, "create", AsyncMock(return_value=object())), patch(
            "app.services.grok.utils.download.AssetsDownloadReverse.request",
            new=AsyncMock(return_value=_Resp()),
        ) as mock_request:
            data_uri = await service.parse_b64(
                "users/u1/generated/p1/image.jpg", "token", "image"
            )

        self.assertTrue(data_uri.startswith("data:image/jpeg;base64,"))
        self.assertEqual(mock_request.await_count, 1)
        self.assertEqual(mock_request.await_args.args[2], "/users/u1/generated/p1/image.jpg")
