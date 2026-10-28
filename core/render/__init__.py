from __future__ import annotations

import asyncio
import base64
import copy
import io
import mimetypes
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiofiles
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from astrbot.api import logger

from ..config import PluginConfig
from ..data import (
    Author,
    Comment,
    ContentItem,
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
    CARD_WIDTH = 1000
    MAX_ASSET_BYTES = 8 * 1024 * 1024
    MAX_DIRECT_ASSET_BYTES = 1024 * 1024
    JPEG_QUALITY = 85
    EMBEDDED_IMAGE_QUALITY = 84

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
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._browser_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._warmup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        """校验内置模板，并在后台预热本地浏览器。"""
        required = (self.TEMPLATE_DIR / "common.html",)
        missing = [path.name for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(f"渲染模板缺失: {', '.join(missing)}")
        if self.cfg.use_local_renderer and self._warmup_task is None:
            self._warmup_task = asyncio.create_task(self._warmup_local_renderer())

    async def close(self) -> None:
        """关闭 Playwright 浏览器与驱动。"""
        self._warmup_task = None
        browser, playwright = self._browser, self._playwright
        self._page = None
        self._context = None
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
        meta = {
            "bot_name": self._bot_name(),
            "rendering_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "width": self.CARD_WIDTH,
        }
        # 样式内联：远程 T2I 只接收 HTML，解析不了相对路径；本地也不必放开 file://。
        styles = self._inline_styles()
        platform = self._safe_template_name(description.platform.name)
        candidates = [f"{platform}.html"]
        # 只有视频描述才走 video.html，否则图文帖会拿到视频版式（大封面 + 播放键）。
        if description.video is not None:
            candidates.append("video.html")
        candidates.append("common.html")

        for template_name in candidates:
            try:
                template = self.env.get_template(template_name)
                return template.render(card=context, meta=meta, styles=styles)
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

    def _inline_styles(self) -> dict[str, str]:
        """读取本地样式表，交给模板内联。"""
        styles: dict[str, str] = {}
        for name in ("icon.css", "tailwind.css"):
            path = self.TEMPLATE_DIR / name
            try:
                styles[name] = path.read_text(encoding="utf-8")
            except OSError:
                logger.warning(f"[渲染] 读取样式失败: {name}")
                styles[name] = ""
        return styles

    async def _description_context(self, description: Description) -> dict[str, Any]:
        # cover 可能是 awaitable，先落成 Path，避免被多处重复 await。
        cover_path = description.cover
        if cover_path is not None and not isinstance(cover_path, Path):
            cover_path = await cover_path
        (
            logo,
            content,
            author,
            metrics,
            video,
            repost,
            cover,
            comments,
        ) = await asyncio.gather(
            self._platform_logo(description.platform.name),
            self._content_blocks(description.contents, cover=cover_path),
            self._author_context(description.author),
            self._metrics_context(description.metrics),
            self._video_context(description),
            (
                self._description_context(description.repost)
                if description.repost is not None
                else asyncio.sleep(0, result=None)
            ),
            self._cover_context(cover_path),
            self._comments_context(description.comments),
        )
        return {
            "platform": {
                "name": description.platform.name,
                "display_name": description.platform.display_name,
                "logo": logo,
            },
            "title": description.title,
            "text": description.text,
            "url": description.url,
            "extra_info": description.extra_info,
            "timestamp": description.timestamp,
            "formatted_datetime": description.formatted_datetime("%Y-%m-%d %H:%M"),
            "content": content,
            "author": author,
            "metrics": metrics,
            "cover": cover,
            "video": video,
            "repost": repost,
            "comments": comments,
        }

    async def _cover_context(
        self, cover: Path | Awaitable[Path] | None
    ) -> str:
        """解析器传入的媒体卡片主视觉。"""
        cover_path = cover
        if cover_path is not None and not isinstance(cover_path, Path):
            cover_path = await cover_path
        return await self._path_to_data_uri(cover_path, 2000, 2000)

    def _bot_name(self) -> str:
        """卡片页脚展示的机器人名，取不到时回退插件名。"""
        for attr in ("bot_name", "nickname"):
            value = getattr(self.cfg, attr, None)
            if isinstance(value, str) and value:
                return value
        return "AstrBot Parser"

    async def _author_context(
        self,
        author: Author | None,
        *,
        is_comment: bool = False,
    ) -> dict[str, Any] | None:
        if author is None:
            return None
        try:
            avatar_path = await author.get_avatar_path()
        except Exception as exc:
            if is_comment:
                logger.warning(
                    "[渲染] 评论头像加载失败 | "
                    f"error_type={type(exc).__name__}"
                )
            avatar_path = None
        return {
            "name": author.name,
            "uid": author.uid,
            "description": author.description,
            "avatar": await self._path_to_data_uri(avatar_path, 192, 192),
            "metrics": await self._metrics_context(author.metrics),
        }

    async def _comments_context(
        self,
        comments: list[Comment] | None,
    ) -> list[dict[str, Any]]:
        return await asyncio.gather(
            *(self._comment_context(comment) for comment in comments or ())
        )

    async def _comment_context(self, comment: Comment) -> dict[str, Any]:
        author, content, replies, parent_author = await asyncio.gather(
            self._author_context(comment.author, is_comment=True),
            self._comment_content(comment.contents),
            asyncio.gather(
                *(self._comment_context(reply) for reply in comment.replies[:5])
            ),
            (
                self._author_context(comment.parent_author, is_comment=True)
                if comment.parent_author is not None
                else asyncio.sleep(0, result=None)
            ),
        )
        return {
            "author": author or {"name": "", "avatar": "", "uid": None},
            "content": content,
            "formatted_datetime": comment.formatted_datetime("%Y-%m-%d %H:%M"),
            "location": comment.location,
            "like_count": self._format_comment_count(comment.like_count),
            "reply_count": self._format_comment_count(comment.reply_count),
            "replies": replies,
            "parent_author": parent_author,
        }

    async def _comment_content(
        self,
        contents: list[ContentItem],
    ) -> list[dict[str, Any]]:
        async def build_block(item: ContentItem) -> dict[str, Any] | None:
            try:
                if isinstance(item, str):
                    return {"type": "text", "text": item} if item else None
                if isinstance(item, TextContent):
                    return {"type": "text", "text": item.text} if item.text else None
                if isinstance(item, (ImageContent, GraphicsContent)):
                    path = await item.get_path()
                    return {
                        "type": "image",
                        "src": await self._path_to_data_uri(path, 720, 560),
                    }
            except Exception as exc:
                logger.warning(
                    "[渲染] 评论内容加载失败 | "
                    f"type={type(item).__name__} | "
                    f"error_type={type(exc).__name__}"
                )
            return None

        blocks = await asyncio.gather(*(build_block(item) for item in contents))
        return [block for block in blocks if block is not None]

    @staticmethod
    def _format_comment_count(value: str | int | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    async def _video_context(self, description: Description) -> dict[str, Any] | None:
        video = description.video
        if video is None:
            return None
        cover_path = await video.get_cover_path()
        return {
            # 封面按原始比例完整展示，仅按最大边长缩放，避免竖图被压扁或裁切。
            "cover": await self._path_to_data_uri(cover_path, 2000, 2000),
            "duration": video.display_duration,
            "quality": video.quality,
            "resolution": video.resolution,
        }

    async def _metrics_context(
        self, metrics: list[Metric] | None
    ) -> list[dict[str, Any]]:
        items = list(metrics or ())
        icons = await asyncio.gather(
            *(self._icon_to_data_uri(metric.icon) for metric in items)
        )
        return [
            {
                "icon": icon,
                "label": metric.label,
                "value": metric.value,
                # 模板按 key 选内置矢量图标，复刻 lite 的彩色统计样式。
                "key": self._metric_icon_key(metric.label),
            }
            for metric, icon in zip(items, icons, strict=True)
        ]

    @staticmethod
    def _metric_icon_key(label: str | None) -> str:
        """把本插件的指标名映射到内置图标类名。"""
        return {
            "浏览": "eye",
            "观看": "eye",
            "播放": "play",
            "点赞": "heart",
            "获赞": "heart",
            "评论": "comment-dots",
            "收藏": "star",
            "追番": "star",
            "分享": "share-nodes",
            "投币": "coin",
            "电池": "battery",
            "弹幕": "danmaku",
            "粉丝": "follow",
            "关注": "follow",
            "视频": "play",
        }.get(label or "", "hot")

    async def _content_blocks(
        self,
        contents: list[MediaContent],
        *,
        cover: Path | Awaitable[Path] | None = None,
    ) -> list[dict[str, Any]]:
        async def build_block(content: MediaContent) -> dict[str, Any] | None:
            if isinstance(content, TextContent):
                return {"type": "text", "text": content.text} if content.text else None
            if isinstance(content, GraphicsContent):
                path = await content.get_path()
                return {
                    "type": "graphic",
                    "src": await self._path_to_data_uri(path, self.CARD_WIDTH, 2000),
                    "text": content.text,
                    "alt": content.alt,
                }
            if isinstance(content, ImageContent):
                path = await content.get_path()
                return {
                    "type": "image",
                    "src": await self._path_to_data_uri(path, self.CARD_WIDTH, 2000),
                    "alt": None,
                }
            if isinstance(content, VideoContent):
                cover_path = await content.get_cover_path()
                if cover_path is not None:
                    return {
                        "type": "cover",
                        "src": await self._path_to_data_uri(
                            cover_path, self.CARD_WIDTH, 1000
                        ),
                        "alt": None,
                    }
            return None

        blocks = await asyncio.gather(*(build_block(content) for content in contents))
        result = [block for block in blocks if block is not None]
        if result and any(block["type"] in {"image", "cover"} for block in result):
            return result
        # 正文没有图片时，解析器给的封面就是唯一视觉内容，必须进入正文。
        if cover is not None:
            cover_path = cover if isinstance(cover, Path) else await cover
            result.insert(
                0,
                {
                    "type": "cover",
                    "src": await self._path_to_data_uri(
                        cover_path, self.CARD_WIDTH, 2000
                    ),
                    "alt": None,
                },
            )
        return result

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
            return await self._path_to_data_uri(path, 96, 96)
        if icon.kind == "path":
            return await self._path_to_data_uri(Path(str(icon.value)), 96, 96)
        return ""

    async def _platform_logo(self, platform_name: str) -> str:
        path = (
            self.RESOURCE_DIR
            / "logos"
            / f"{self._safe_template_name(platform_name)}.png"
        )
        return await self._path_to_data_uri(path, 256, 256)

    async def _path_to_data_uri(
        self,
        path: Path | None,
        max_width: int = CARD_WIDTH,
        max_height: int = 2000,
    ) -> str:
        if path is None:
            return ""
        resolved = await asyncio.to_thread(path.expanduser().resolve)
        if not self._is_allowed_asset(resolved):
            logger.warning(f"[渲染] 拒绝读取非允许目录资源: {resolved.name}")
            return ""
        try:
            if not resolved.is_file():
                return ""
            stat = await asyncio.to_thread(resolved.stat)
            if stat.st_size <= 0 or stat.st_size > self.MAX_ASSET_BYTES:
                logger.warning(
                    f"[渲染] 资源大小异常: {resolved.name}, size={stat.st_size}"
                )
                return ""
            data_uri = await asyncio.to_thread(
                self._build_asset_data_uri,
                resolved,
                stat.st_size,
                max_width,
                max_height,
            )
        except OSError as exc:
            logger.warning(f"[渲染] 读取资源失败: {resolved.name}, error={exc}")
            return ""
        return data_uri

    @classmethod
    def _build_asset_data_uri(
        cls,
        path: Path,
        file_size: int,
        max_width: int = CARD_WIDTH,
        max_height: int = 2000,
    ) -> str:
        """在后台线程按最终展示尺寸压缩内嵌图片。"""
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with PILImage.open(path) as source:
                source.seek(0)
                original_width, original_height = source.size
                orientation = source.getexif().get(274, 1)
                needs_resize = (
                    original_width > max_width
                    or original_height > max_height
                    or orientation not in (None, 1)
                )
                if not needs_resize and file_size <= cls.MAX_DIRECT_ASSET_BYTES:
                    data = path.read_bytes()
                    encoded = base64.b64encode(data).decode("ascii")
                    return f"data:{mime};base64,{encoded}"

                image = (
                    ImageOps.exif_transpose(source)
                    if orientation not in (None, 1)
                    else source
                )
                try:
                    image.thumbnail(
                        (max_width, max_height),
                        PILImage.Resampling.LANCZOS,
                    )
                    has_alpha = image.mode in {"RGBA", "LA"} or (
                        image.mode == "P" and "transparency" in image.info
                    )
                    output = io.BytesIO()
                    if has_alpha:
                        image.save(
                            output,
                            format="WEBP",
                            quality=cls.EMBEDDED_IMAGE_QUALITY,
                            method=2,
                        )
                        encoded_mime = "image/webp"
                    else:
                        image.convert("RGB").save(
                            output,
                            format="JPEG",
                            quality=cls.EMBEDDED_IMAGE_QUALITY,
                            optimize=False,
                        )
                        encoded_mime = "image/jpeg"
                finally:
                    if image is not source:
                        image.close()
        except (UnidentifiedImageError, OSError, ValueError):
            data = path.read_bytes()
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{mime};base64,{encoded}"

        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:{encoded_mime};base64,{encoded}"

    def _is_allowed_asset(self, path: Path) -> bool:
        roots = (
            self.cfg.cache_dir,
            self.cfg.data_dir,
            self.cfg.plugin_dir,
            self.RESOURCE_DIR,
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
                    args=[
                        "--disable-dev-shm-usage",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--disable-extensions",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--mute-audio",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-features=Translate,MediaRouter,"
                        "OptimizationHints,CalculateNativeWinOcclusion",
                        "--font-render-hinting=none",
                    ],
                )
                self._page = None
                self._context = None
            except Exception:
                await self._playwright.stop()
                self._playwright = None
                raise
            return self._browser

    async def _ensure_page(self) -> Page:
        browser = await self._ensure_browser()
        if self._context is None:
            self._context = await browser.new_context(
                viewport={"width": self.CARD_WIDTH, "height": 1000},
                device_scale_factor=1,
                color_scheme="light",
                reduced_motion="reduce",
            )
        if self._page is None:
            self._page = await self._context.new_page()
        self._page.set_default_timeout(
            max(15_000, self.cfg.common_timeout * 1000)
        )
        return self._page

    async def _warmup_local_renderer(self) -> None:
        task = asyncio.current_task()
        try:
            async with self._render_lock:
                if task is None or self._warmup_task is not task:
                    return
                page = await self._ensure_page()
                await page.set_content(
                    "<html><body></body></html>",
                    wait_until="domcontentloaded",
                )
        except Exception as exc:
            if task is not None and self._warmup_task is not task:
                return
            logger.warning(
                "[渲染] 浏览器预热失败 | "
                f"error_type={type(exc).__name__} | "
                f"error={self._safe_exception_text(exc)}"
            )

    async def _render_local(
        self,
        html: str,
    ) -> Path:
        async with self._render_lock:
            return await self._render_local_locked(html)

    async def _render_local_locked(self, html: str) -> Path:
        page = await self._ensure_page()
        output = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}.jpg"
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            await page.evaluate(
                """async () => {
                    await document.fonts.ready;
                    await Promise.all(
                        Array.from(document.images, (img) =>
                            img.decode().catch(() => undefined)
                        )
                    );
                }"""
            )
            clip = await page.evaluate(
                """() => {
                    const card = document.querySelector("main");
                    if (!card) return null;
                    const rect = card.getBoundingClientRect();
                    return {
                        x: rect.left + window.scrollX,
                        y: rect.top + window.scrollY,
                        width: rect.width,
                        height: rect.height,
                    };
                }"""
            )
            if (
                not isinstance(clip, dict)
                or float(clip.get("width", 0)) <= 0
                or float(clip.get("height", 0)) <= 0
            ):
                raise RuntimeError("未找到可截图的卡片元素")
            card_size = {
                "width": max(1, round(float(clip["width"]))),
                "height": max(1, round(float(clip["height"]))),
            }
            if page.viewport_size != card_size:
                await page.set_viewport_size(card_size)
                await page.evaluate(
                    """async () => {
                        await document.fonts.ready;
                        await Promise.all(
                            Array.from(document.images, (img) =>
                                img.decode().catch(() => undefined)
                            )
                        );
                    }"""
                )
            clip = await page.evaluate(
                """() => {
                    const card = document.querySelector("main");
                    if (!card) return null;
                    const rect = card.getBoundingClientRect();
                    return {
                        x: rect.left + window.scrollX,
                        y: rect.top + window.scrollY,
                        width: rect.width,
                        height: rect.height,
                    };
                }"""
            )
            if (
                not isinstance(clip, dict)
                or float(clip.get("width", 0)) <= 0
                or float(clip.get("height", 0)) <= 0
            ):
                raise RuntimeError("未找到可截图的卡片元素")
            await page.screenshot(
                path=str(output),
                type="jpeg",
                quality=self.JPEG_QUALITY,
                clip=clip,
                animations="disabled",
                caret="hide",
                scale="css",
            )
        except Exception:
            await self._reset_page()
            raise

        return output

    async def _reset_page(self) -> None:
        page, context = self._page, self._context
        self._page = None
        self._context = None
        for resource in (page, context):
            if resource is None:
                continue
            try:
                await resource.close()
            except Exception:
                logger.warning("[渲染] 重置浏览器页面失败")

    async def _render_remote(self, html: str) -> Path | None:
        if self.remote_renderer is None:
            logger.error("[渲染] 远程渲染未配置 Star.html_render")
            return None
        result = await self.remote_renderer(
            html,
            {},
            return_url=False,
            options={
                "full_page": True,
                "type": "jpeg",
                "quality": self.JPEG_QUALITY,
            },
        )
        if not result:
            return None
        source = Path(str(result))
        if not await asyncio.to_thread(source.is_file):
            logger.error("[渲染] 远程渲染未返回可用文件")
            return None
        target = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}.jpg"
        data = await asyncio.to_thread(source.read_bytes)
        if source.suffix.lower() in {".jpg", ".jpeg"}:
            async with aiofiles.open(target, "wb") as fp:
                await fp.write(data)
        else:
            await asyncio.to_thread(self._write_jpeg, data, target)
        return target

    def _write_jpeg(self, data: bytes, target: Path) -> None:
        with PILImage.open(io.BytesIO(data)) as image:
            image.convert("RGB").save(
                target,
                format="JPEG",
                quality=self.JPEG_QUALITY,
                optimize=False,
            )

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
