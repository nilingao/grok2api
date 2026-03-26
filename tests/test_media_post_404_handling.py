import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reverse.media_post import MediaPostReverse


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def json(self):
        import json

        return json.loads(self.text or "{}")


class _FakeSession:
    async def post(self, *_args, **_kwargs):
        return _FakeResponse(
            404,
            '{"code":5, "message":"Media post not found", "details":[]}',
        )


class _CapturePayloadSession:
    def __init__(self):
        self.payloads = []

    async def post(self, _url, **kwargs):
        self.payloads.append(kwargs.get("json") or {})
        return _FakeResponse(200, '{"post":{"id":"captured-post-id"}}')


async def _passthrough_retry(func, *args, **kwargs):
    return await func(*args, **kwargs)


class MediaPost404HandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_post_get_404_is_gracefully_downgraded(self):
        cfg = {
            "proxy.base_proxy_url": "",
            "video.timeout": 5,
            "proxy.browser": "chrome",
        }

        with patch(
            "app.services.reverse.media_post.retry_on_status",
            new=_passthrough_retry,
        ), patch(
            "app.services.reverse.media_post.get_config",
            side_effect=lambda key, default=None: cfg.get(key, default),
        ), patch(
            "app.services.reverse.media_post.build_headers",
            return_value={"Content-Type": "application/json"},
        ):
            response = await MediaPostReverse.get(_FakeSession(), "sso=test", "post-id")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})

    async def test_capture_metadata_creates_post_when_probe_not_found(self):
        calls = {"get": [], "request": [], "create_link": []}

        async def _fake_get(_session, _token, post_id):
            calls["get"].append(post_id)
            return _FakeResponse(200, "{}")

        async def _fake_request(_session, _token, media_type, media_url, prompt=""):
            calls["request"].append((media_type, media_url, prompt))
            return _FakeResponse(
                200,
                '{"post":{"id":"new-post-id","mediaUrl":"https://assets.grok.com/users/u1/generated/p1/image.jpg","mediaType":"MEDIA_POST_TYPE_IMAGE"}}',
            )

        async def _fake_create_link(_session, _token, post_id, source="post-page", platform="web"):
            calls["create_link"].append((post_id, source, platform))
            return _FakeResponse(200, '{"shareLink":"https://grok.com/imagine/post/new-post-id"}')

        with patch(
            "app.services.reverse.media_post.MediaPostReverse.get",
            new=_fake_get,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.request",
            new=_fake_request,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.create_link",
            new=_fake_create_link,
        ):
            metadata = await MediaPostReverse.capture_metadata(
                _FakeSession(),
                "sso=test",
                "old-non-post-id",
                media_type="image",
                local_url="https://assets.grok.com/users/u1/generated/p1/image.jpg",
            )

        self.assertEqual(metadata.get("post_id"), "new-post-id")
        self.assertEqual(
            metadata.get("share_link"),
            "https://grok.com/imagine/post/new-post-id",
        )
        self.assertEqual(calls["get"], ["old-non-post-id"])
        self.assertEqual(
            calls["request"],
            [("MEDIA_POST_TYPE_IMAGE", "https://assets.grok.com/users/u1/generated/p1/image.jpg", "")],
        )
        self.assertEqual(calls["create_link"], [("new-post-id", "post-page", "web")])

    async def test_capture_metadata_prefers_create_for_generated_asset_ids(self):
        calls = {"get": [], "request": [], "create_link": []}

        async def _fake_get(_session, _token, post_id):
            calls["get"].append(post_id)
            return _FakeResponse(200, "{}")

        async def _fake_request(_session, _token, media_type, media_url, prompt=""):
            calls["request"].append((media_type, media_url, prompt))
            return _FakeResponse(
                200,
                '{"post":{"id":"created-from-asset","mediaUrl":"https://assets.grok.com/users/u1/generated/abc123-def456-7890-abcd-ef1234567890/image.jpg","mediaType":"MEDIA_POST_TYPE_IMAGE"}}',
            )

        async def _fake_create_link(_session, _token, post_id, source="post-page", platform="web"):
            calls["create_link"].append((post_id, source, platform))
            return _FakeResponse(
                200,
                '{"shareLink":"https://grok.com/imagine/post/created-from-asset"}',
            )

        with patch(
            "app.services.reverse.media_post.MediaPostReverse.get",
            new=_fake_get,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.request",
            new=_fake_request,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.create_link",
            new=_fake_create_link,
        ):
            metadata = await MediaPostReverse.capture_metadata(
                _FakeSession(),
                "sso=test",
                "abc123-def456-7890-abcd-ef1234567890",
                media_type="image",
                local_url="https://assets.grok.com/users/u1/generated/abc123-def456-7890-abcd-ef1234567890/image.jpg",
            )

        self.assertEqual(metadata.get("post_id"), "created-from-asset")
        self.assertEqual(calls["get"], [])
        self.assertEqual(len(calls["request"]), 1)
        self.assertEqual(calls["create_link"], [("created-from-asset", "post-page", "web")])

    async def test_capture_metadata_uses_public_image_url_when_local_url_not_http(self):
        calls = {"get": [], "request": [], "create_link": []}
        post_id = "471e0431-7501-45de-aed1-20ccbcdf7284"

        async def _fake_get(_session, _token, pid):
            calls["get"].append(pid)
            return _FakeResponse(200, "{}")

        async def _fake_request(_session, _token, media_type, media_url, prompt=""):
            calls["request"].append((media_type, media_url, prompt))
            return _FakeResponse(
                200,
                '{"post":{"id":"created-from-public-url","mediaUrl":"https://assets.grok.com/users/u1/generated/created-from-public-url/image.jpg","mediaType":"MEDIA_POST_TYPE_IMAGE"}}',
            )

        async def _fake_create_link(_session, _token, pid, source="post-page", platform="web"):
            calls["create_link"].append((pid, source, platform))
            return _FakeResponse(
                200,
                '{"shareLink":"https://grok.com/imagine/post/created-from-public-url"}',
            )

        with patch(
            "app.services.reverse.media_post.MediaPostReverse.get",
            new=_fake_get,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.request",
            new=_fake_request,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.create_link",
            new=_fake_create_link,
        ):
            metadata = await MediaPostReverse.capture_metadata(
                _FakeSession(),
                "sso=test",
                post_id,
                media_type="image",
                local_url="data:image/png;base64,abc",
            )

        self.assertEqual(metadata.get("post_id"), post_id)
        self.assertEqual(calls["get"], [post_id])
        self.assertEqual(calls["request"], [])
        self.assertEqual(calls["create_link"], [])

    async def test_capture_metadata_create_before_get_failure_is_not_retried(self):
        calls = {"get": [], "request": [], "create_link": []}
        post_id = "abc123-def456-7890-abcd-ef1234567890"
        local_url = f"https://assets.grok.com/users/u1/generated/{post_id}/image.jpg"

        async def _fake_get(_session, _token, pid):
            calls["get"].append(pid)
            return _FakeResponse(200, "{}")

        async def _fake_request(_session, _token, media_type, media_url, prompt=""):
            calls["request"].append((media_type, media_url, prompt))
            raise RuntimeError("create failed once")

        async def _fake_create_link(_session, _token, pid, source="post-page", platform="web"):
            calls["create_link"].append((pid, source, platform))
            return _FakeResponse(200, '{"shareLink":"https://grok.com/imagine/post/should-not-happen"}')

        with patch(
            "app.services.reverse.media_post.MediaPostReverse.get",
            new=_fake_get,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.request",
            new=_fake_request,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.create_link",
            new=_fake_create_link,
        ):
            metadata = await MediaPostReverse.capture_metadata(
                _FakeSession(),
                "sso=test",
                post_id,
                media_type="image",
                local_url=local_url,
            )

        self.assertEqual(metadata.get("post_id"), post_id)
        self.assertEqual(calls["get"], [post_id])
        self.assertEqual(len(calls["request"]), 1)
        self.assertEqual(calls["create_link"], [])

    async def test_capture_metadata_normalizes_proxy_file_url_before_create(self):
        calls = {"get": [], "request": [], "create_link": []}
        post_id = "b68a97dc-6248-41f0-8630-ea3987c0bcea"
        proxy_url = (
            "https://www.nlacloud.shop:2533/v1/files/image/users/"
            "942f2f9a-f734-457a-bc2d-401b5dbf5185/generated/"
            "d60602cd-c5d1-43ec-a674-861f247a8d14/image.jpg"
        )
        expected_assets_url = (
            "https://assets.grok.com/users/"
            "942f2f9a-f734-457a-bc2d-401b5dbf5185/generated/"
            "d60602cd-c5d1-43ec-a674-861f247a8d14/image.jpg"
        )

        async def _fake_get(_session, _token, pid):
            calls["get"].append(pid)
            return _FakeResponse(200, "{}")

        async def _fake_request(_session, _token, media_type, media_url, prompt=""):
            calls["request"].append((media_type, media_url, prompt))
            return _FakeResponse(
                200,
                '{"post":{"id":"created-from-proxy-url","mediaUrl":"https://assets.grok.com/users/u1/generated/created-from-proxy-url/image.jpg","mediaType":"MEDIA_POST_TYPE_IMAGE"}}',
            )

        async def _fake_create_link(_session, _token, pid, source="post-page", platform="web"):
            calls["create_link"].append((pid, source, platform))
            return _FakeResponse(200, '{"shareLink":"https://grok.com/imagine/post/created-from-proxy-url"}')

        with patch(
            "app.services.reverse.media_post.MediaPostReverse.get",
            new=_fake_get,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.request",
            new=_fake_request,
        ), patch(
            "app.services.reverse.media_post.MediaPostReverse.create_link",
            new=_fake_create_link,
        ):
            metadata = await MediaPostReverse.capture_metadata(
                _FakeSession(),
                "sso=test",
                post_id,
                media_type="image",
                local_url=proxy_url,
            )

        self.assertEqual(metadata.get("post_id"), "created-from-proxy-url")
        self.assertEqual(calls["get"], [])
        self.assertEqual(calls["request"], [("MEDIA_POST_TYPE_IMAGE", expected_assets_url, "")])
        self.assertEqual(calls["create_link"], [("created-from-proxy-url", "post-page", "web")])

    async def test_request_normalizes_proxy_media_url_payload(self):
        cfg = {
            "proxy.base_proxy_url": "",
            "video.timeout": 5,
            "proxy.browser": "chrome",
        }
        proxy_url = (
            "https://www.nlacloud.shop:2533/v1/files/image/users/"
            "942f2f9a-f734-457a-bc2d-401b5dbf5185/generated/"
            "d60602cd-c5d1-43ec-a674-861f247a8d14/image.jpg"
        )
        expected_assets_url = (
            "https://assets.grok.com/users/"
            "942f2f9a-f734-457a-bc2d-401b5dbf5185/generated/"
            "d60602cd-c5d1-43ec-a674-861f247a8d14/image.jpg"
        )
        session = _CapturePayloadSession()

        with patch(
            "app.services.reverse.media_post.retry_on_status",
            new=_passthrough_retry,
        ), patch(
            "app.services.reverse.media_post.get_config",
            side_effect=lambda key, default=None: cfg.get(key, default),
        ), patch(
            "app.services.reverse.media_post.build_headers",
            return_value={"Content-Type": "application/json"},
        ):
            response = await MediaPostReverse.request(
                session,
                "sso=test",
                "MEDIA_POST_TYPE_IMAGE",
                proxy_url,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.payloads), 1)
        self.assertEqual(session.payloads[0].get("mediaUrl"), expected_assets_url)
