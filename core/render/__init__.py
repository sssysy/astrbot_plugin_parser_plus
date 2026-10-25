from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiofiles
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from playwright.async_api import Browser, Playwright, async_playwright

from astrbot.api import logger

from ..config import PluginConfig
from ..data import (
    Author,
    Description,
    GraphicsContent,
    IconAsset,
    ImageContent,
    MediaContent,
    Metric,
    TextContent,
    VideoContent,
)
from ..download import Downloader

RemoteRenderer = Callable[..., Awaitable[str]]


class Renderer:
    """将 Description 渲染为图片，支持本地 Playwright 和远程 T2I。"""

    TEMPLATE_DIR = Path(__file__).parent / "default"
    RESOURCE_DIR = Path(__file__).parent.parent / "resources"
    MAX_ASSET_BYTES = 8 * 1024 * 1024
    RENDER_WIDTH = 900

    def __init__(
        self,
        config: PluginConfig,
        downloader: Downloader,
        remote_renderer: RemoteRenderer | None = None,
    ):
        self.cfg = config
        self.downloader = downloader
        self.remote_renderer = remote_renderer
        self.env = Environment(
            loader=FileSystemLoader(self.TEMPLATE_DIR),
            autoescape=select_autoescape(("html", "xml")),
            enable_async=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """校验内置模板；浏览器保持懒加载。"""
        required = (self.TEMPLATE_DIR / "common.html",)
        missing = [path.name for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(f"渲染模板缺失: {', '.join(missing)}")

    async def close(self) -> None:
        """关闭 Playwright 浏览器与驱动。"""
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.exception("[渲染] 关闭浏览器失败")
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                logger.exception("[渲染] 停止 Playwright 失败")

    async def render_card(self, description: Description) -> Path | None:
        """渲染描述卡片，失败返回 None。"""
        try:
            html = await self._render_html(description)
            if not html:
                return None
            if self.cfg.use_local_renderer:
                return await self._render_local(html)
            return await self._render_remote(html)
        except Exception as exc:
            logger.error(
                "[渲染] 卡片渲染失败 | "
                f"platform={description.platform.name} | "
                f"error_type={type(exc).__name__} | "
                f"error={self._safe_exception_text(exc)}"
            )
            return None

    async def _render_html(self, description: Description) -> str | None:
        context = await self._description_context(description)
        is_video = description.video is not None
        platform = self._safe_template_name(description.platform.name)
        candidates = [f"{platform}.html"]
        if is_video:
            candidates.append("video.html")
        candidates.append("common.html")

        for template_name in candidates:
            try:
                template = self.env.get_template(template_name)
                return template.render(card=context)
            except TemplateNotFound:
                continue
            except Exception as exc:
                logger.error(
                    "[渲染] 模板处理失败 | "
                    f"template={template_name} | "
                    f"error_type={type(exc).__name__} | "
                    f"error={self._safe_exception_text(exc)}"
                )
        logger.error("[渲染] 未找到可用模板")
        return None

    async def _description_context(self, description: Description) -> dict[str, Any]:
        return {
            "platform": {
                "name": description.platform.name,
                "display_name": description.platform.display_name,
                "logo": await self._platform_logo(description.platform.name),
            },
            "title": description.title,
            "text": description.text,
            "url": description.url,
            "extra_info": description.extra_info,
            "blocks": await self._content_blocks(description.contents),
            "author": await self._author_context(description.author),
            "metrics": await self._metrics_context(description.metrics),
            "video": await self._video_context(description),
            "repost": (
                await self._description_context(description.repost)
                if description.repost is not None
                else None
            ),
        }

    async def _author_context(self, author: Author | None) -> dict[str, Any] | None:
        if author is None:
            return None
        avatar_path = await author.get_avatar_path()
        return {
            "name": author.name,
            "uid": author.uid,
            "description": author.description,
            "avatar": await self._path_to_data_uri(avatar_path),
            "metrics": await self._metrics_context(author.metrics),
        }

    async def _video_context(self, description: Description) -> dict[str, Any] | None:
        video = description.video
        if video is None:
            return None
        cover_path = await video.get_cover_path()
        return {
            "cover": await self._path_to_data_uri(cover_path),
            "duration": video.display_duration,
            "quality": video.quality,
            "resolution": video.resolution,
        }

    async def _metrics_context(
        self, metrics: list[Metric] | None
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for metric in metrics or ():
            result.append(
                {
                    "icon": await self._icon_to_data_uri(metric.icon),
                    "label": metric.label,
                    "value": metric.value,
                }
            )
        return result

    async def _content_blocks(
        self, contents: list[MediaContent]
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for content in contents:
            if isinstance(content, TextContent):
                if content.text:
                    blocks.append({"type": "text", "text": content.text})
                continue
            if isinstance(content, GraphicsContent):
                path = await content.get_path()
                blocks.append(
                    {
                        "type": "graphics",
                        "src": await self._path_to_data_uri(path),
                        "text": content.text,
                        "alt": content.alt,
                    }
                )
                continue
            if isinstance(content, ImageContent):
                path = await content.get_path()
                blocks.append(
                    {
                        "type": "image",
                        "src": await self._path_to_data_uri(path),
                        "alt": "",
                    }
                )
                continue
            if isinstance(content, VideoContent):
                cover_path = await content.get_cover_path()
                if cover_path is not None:
                    blocks.append(
                        {
                            "type": "image",
                            "src": await self._path_to_data_uri(cover_path),
                            "alt": "",
                        }
                    )
        return blocks

    async def _icon_to_data_uri(self, icon: IconAsset | None) -> str:
        if icon is None:
            return ""
        if icon.kind == "data":
            value = str(icon.value)
            return value if value.startswith("data:image/") else ""
        if icon.kind == "svg":
            svg = str(icon.value).encode("utf-8")
            encoded = base64.b64encode(svg).decode("ascii")
            return f"data:image/svg+xml;base64,{encoded}"
        if icon.kind == "url":
            task = self.downloader.download_img(str(icon.value))
            path = await task
            return await self._path_to_data_uri(path)
        if icon.kind == "path":
            return await self._path_to_data_uri(Path(str(icon.value)))
        return ""

    async def _platform_logo(self, platform_name: str) -> str:
        path = (
            self.RESOURCE_DIR
            / "logos"
            / f"{self._safe_template_name(platform_name)}.png"
        )
        return await self._path_to_data_uri(path)

    async def _path_to_data_uri(self, path: Path | None) -> str:
        if path is None:
            return ""
        resolved = await asyncio.to_thread(path.expanduser().resolve)
        if not self._is_allowed_asset(resolved):
            logger.warning(f"[渲染] 拒绝读取非允许目录资源: {resolved.name}")
            return ""
        try:
            if not resolved.is_file():
                return ""
            size = await asyncio.to_thread(resolved.stat)
            if size.st_size <= 0 or size.st_size > self.MAX_ASSET_BYTES:
                logger.warning(
                    f"[渲染] 资源大小异常: {resolved.name}, size={size.st_size}"
                )
                return ""
            data = await asyncio.to_thread(resolved.read_bytes)
        except OSError as exc:
            logger.warning(f"[渲染] 读取资源失败: {resolved.name}, error={exc}")
            return ""
        mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _is_allowed_asset(self, path: Path) -> bool:
        roots = (
            self.cfg.cache_dir,
            self.cfg.data_dir,
            self.cfg.plugin_dir,
        )
        for root in roots:
            try:
                path.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    async def _ensure_browser(self) -> Browser:
        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--disable-dev-shm-usage"],
                )
            except Exception:
                await self._playwright.stop()
                self._playwright = None
                raise
            return self._browser

    async def _render_local(self, html: str) -> Path:
        browser = await self._ensure_browser()
        timeout_ms = max(15_000, self.cfg.common_timeout * 1000)
        context = await browser.new_context(
            viewport={"width": self.RENDER_WIDTH, "height": 1200},
            device_scale_factor=2,
        )
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        cache = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}.png"
        try:
            await page.set_content(html, wait_until="load")
            await page.wait_for_function(
                "() => Array.from(document.images).every((img) => img.complete)"
            )
            await page.evaluate("document.fonts.ready")
            await page.locator("main.card").screenshot(
                path=str(cache),
                type="png",
                animations="disabled",
            )
        finally:
            await context.close()
        return cache

    async def _render_remote(self, html: str) -> Path | None:
        if self.remote_renderer is None:
            logger.error("[渲染] 远程渲染未配置 Star.html_render")
            return None
        result = await self.remote_renderer(
            html,
            {},
            return_url=False,
            options={"full_page": True, "type": "jpeg", "quality": 90},
        )
        if not result:
            return None
        source = Path(str(result))
        if not await asyncio.to_thread(source.is_file):
            logger.error("[渲染] 远程渲染未返回可用文件")
            return None
        suffix = source.suffix or ".jpg"
        target = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}{suffix}"
        data = await asyncio.to_thread(source.read_bytes)
        async with aiofiles.open(target, "wb") as fp:
            await fp.write(data)
        return target

    @staticmethod
    def _safe_template_name(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", value)
        return cleaned or "unknown"

    @staticmethod
    def _safe_exception_text(exc: Exception) -> str:
        text = str(exc).strip() or "<empty>"

        def redact_url(match: re.Match[str]) -> str:
            raw = match.group(0)
            try:
                parsed = urlsplit(raw)
                if not parsed.hostname:
                    return "<url>"
                port = f":{parsed.port}" if parsed.port else ""
                return f"{parsed.scheme}://{parsed.hostname}{port}/<redacted>"
            except ValueError:
                return "<url>"

        return re.sub(r"https?://[^\s]+", redact_url, text)
