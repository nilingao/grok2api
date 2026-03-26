"""
响应处理器基类和通用工具
"""

import asyncio
import time
from typing import Any, AsyncGenerator, AsyncIterable, List, Optional, TypeVar

import orjson

from app.core.config import get_config
from app.core.exceptions import StreamIdleTimeoutError
from app.core.logger import logger
from app.services.grok.utils.download import DownloadService


T = TypeVar("T")


def _is_http2_error(e: Exception) -> bool:
    """检查是否为 HTTP/2 流错误"""
    err_str = str(e).lower()
    return "http/2" in err_str or "curl: (92)" in err_str or "stream" in err_str


def _normalize_line(line: Any) -> Optional[str]:
    """规范化流式响应行，兼容 SSE data 前缀与空行"""
    if line is None:
        return None
    if isinstance(line, (bytes, bytearray)):
        text = line.decode("utf-8", errors="ignore")
    else:
        text = str(line)
    text = text.strip()
    if not text:
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if text == "[DONE]":
        return None
    return text


def _collect_images(obj: Any) -> List[str]:
    """递归收集响应中的图片 URL"""
    urls: List[str] = []
    seen = set()

    def _to_assets_url(raw_url: Any) -> str:
        raw = str(raw_url or "").strip()
        if not raw:
            return ""
        if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("data:"):
            return raw
        if raw.startswith("/"):
            return f"https://assets.grok.com{raw}"
        return f"https://assets.grok.com/{raw}"

    def add(url: Any, *, normalize_assets: bool = False):
        text = str(url or "").strip()
        if not text:
            return
        if normalize_assets:
            text = _to_assets_url(text)
        if not text or text in seen:
            return
        seen.add(text)
        urls.append(text)

    def _add_image_chunk_urls(payload: Any):
        if not isinstance(payload, dict):
            return
        image_chunk = payload.get("image_chunk")
        entries: List[dict] = []
        if isinstance(image_chunk, dict):
            entries.append(image_chunk)
        elif isinstance(image_chunk, list):
            entries.extend([item for item in image_chunk if isinstance(item, dict)])

        for entry in entries:
            progress = entry.get("progress")
            if progress is not None:
                try:
                    if float(progress) < 100:
                        continue
                except (TypeError, ValueError):
                    if str(progress).strip() != "100":
                        continue
            add(entry.get("imageUrl") or entry.get("url"), normalize_assets=True)

    def _parse_card(raw_card: Any) -> dict | None:
        if isinstance(raw_card, dict):
            return raw_card
        if isinstance(raw_card, str) and raw_card.strip():
            try:
                parsed = orjson.loads(raw_card)
            except orjson.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return parsed
        return None

    def _extract_generated_card_images(card: dict):
        card_type = str(card.get("type") or "").strip()
        card_kind = str(card.get("cardType") or "").strip()
        is_generated = (
            card_type == "render_generated_image"
            or card_type == "render_edited_image"
            or card_kind == "generated_image_card"
        )
        if not is_generated:
            return

        _add_image_chunk_urls(card)

        json_data = card.get("jsonData")
        parsed_json_data: Any = json_data
        if isinstance(json_data, str) and json_data.strip():
            try:
                parsed_json_data = orjson.loads(json_data)
            except orjson.JSONDecodeError:
                parsed_json_data = None
        _add_image_chunk_urls(parsed_json_data)

    def walk(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"generatedImageUrls", "imageUrls", "imageURLs"}:
                    if isinstance(item, list):
                        for url in item:
                            add(url)
                    else:
                        add(item)
                    continue
                if key == "cardAttachment":
                    card = _parse_card(item)
                    if card:
                        _extract_generated_card_images(card)
                    continue
                if key == "cardAttachmentsJson" and isinstance(item, list):
                    for raw_card in item:
                        card = _parse_card(raw_card)
                        if card:
                            _extract_generated_card_images(card)
                    continue
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return urls


async def _with_idle_timeout(
    iterable: AsyncIterable[T],
    idle_timeout: float,
    model: str = "",
    first_item_timeout: Optional[float] = None,
) -> AsyncGenerator[T, None]:
    """
    包装异步迭代器，添加空闲超时检测

    Args:
        iterable: 原始异步迭代器
        idle_timeout: 空闲超时时间(秒), 0 表示禁用
        model: 模型名称(用于日志)
    """
    try:
        idle_timeout = float(idle_timeout or 0)
    except (ValueError, TypeError):
        idle_timeout = 0.0

    try:
        first_item_timeout = float(first_item_timeout or 0) if first_item_timeout is not None else 0.0
    except (ValueError, TypeError):
        first_item_timeout = 0.0

    if idle_timeout <= 0:
        async for item in iterable:
            yield item
        return

    iterator = iterable.__aiter__()

    async def _maybe_aclose(it):
        aclose = getattr(it, "aclose", None)
        if not aclose:
            return
        try:
            await aclose()
        except Exception:
            pass

    got_first_item = False
    while True:
        try:
            current_timeout = idle_timeout
            if (not got_first_item) and first_item_timeout and first_item_timeout > 0:
                current_timeout = first_item_timeout
            item = await asyncio.wait_for(iterator.__anext__(), timeout=current_timeout)
            got_first_item = True
            yield item
        except asyncio.TimeoutError:
            logger.warning(
                f"Stream idle timeout after {current_timeout}s",
                extra={
                    "model": model,
                    "idle_timeout": current_timeout,
                    "first_item_timeout": first_item_timeout,
                    "got_first_item": got_first_item,
                },
            )
            await _maybe_aclose(iterator)
            raise StreamIdleTimeoutError(current_timeout)
        except asyncio.CancelledError:
            await _maybe_aclose(iterator)
            raise
        except StopAsyncIteration:
            break


class BaseProcessor:
    """基础处理器"""

    def __init__(self, model: str, token: str = ""):
        self.model = model
        self.token = token
        self.created = int(time.time())
        self.app_url = get_config("app.app_url")
        self._dl_service: Optional[DownloadService] = None

    def _get_dl(self) -> DownloadService:
        """获取下载服务实例（复用）"""
        if self._dl_service is None:
            self._dl_service = DownloadService()
        return self._dl_service

    async def close(self):
        """释放下载服务资源"""
        if self._dl_service:
            await self._dl_service.close()
            self._dl_service = None

    async def process_url(self, path: str, media_type: str = "image") -> str:
        """处理资产 URL"""
        dl_service = self._get_dl()
        return await dl_service.resolve_url(path, self.token, media_type)


__all__ = [
    "BaseProcessor",
    "_with_idle_timeout",
    "_normalize_line",
    "_collect_images",
    "_is_http2_error",
]

