from __future__ import annotations

import asyncio
import re
from typing import ClassVar
from urllib.parse import urlparse

from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import (
    Author,
    Description,
    MediaContent,
    Metric,
    Platform,
)
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .models import TelegramMedia, TelegramPost
from .page import parse_page_html

CHANNEL_PATTERN = r"(?P<channel>[A-Za-z][A-Za-z0-9_]{2,31})"
POST_PATTERN = r"(?P<message_id>\d{1,10})(?![0-9])"
URL_PREFIX = r"(?<![\w./%?=&-])https?://"
RESERVED_CHANNELS = (
    "addstickers",
    "c",
    "contact",
    "joinchat",
    "login",
    "proxy",
    "s",
    "setlanguage",
    "share",
)
TRUSTED_MEDIA_SUFFIXES = ("telesco.pe", "telegram.org", "t.me")
PAGE_HOST_SUFFIXES = ("t.me", "telegram.me", "telegram.dog")
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_EXTRA_TEXT = 500


def _reserved_pattern() -> str:
    return "|".join(re.escape(item) for item in RESERVED_CHANNELS)


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == item or host.endswith(f".{item}") for item in suffixes)


def _is_trusted_media_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and _host_matches(host, TRUSTED_MEDIA_SUFFIXES)
    )


def _short(value: str | None, limit: int = MAX_EXTRA_TEXT) -> str | None:
    if not value:
        return None
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


class TelegramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="telegram",
        display_name="Telegram",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                "Referer": "https://t.me/",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="t.me")

    @property
    def media_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        headers["Accept"] = "image/avif,image/webp,image/*,video/*,*/*;q=0.8"
        if cookie := self.cookiejar.get_cookie_header_for_url("https://t.me/"):
            headers["Cookie"] = cookie
        return headers

    @handle(
        "t.me/s/",
        URL_PREFIX
        + r"(?:www\.)?t\.me/s/"
        + CHANNEL_PATTERN
        + "/"
        + POST_PATTERN,
    )
    @handle(
        "t.me/",
        URL_PREFIX
        + r"(?:www\.)?t\.me/(?!(?:"
        + _reserved_pattern()
        + r")(?:/|$))"
        + CHANNEL_PATTERN
        + "/"
        + POST_PATTERN,
    )
    @handle(
        "telegram.me/s/",
        URL_PREFIX
        + r"(?:www\.)?telegram\.me/s/"
        + CHANNEL_PATTERN
        + "/"
        + POST_PATTERN,
    )
    @handle(
        "telegram.me/",
        URL_PREFIX
        + r"(?:www\.)?telegram\.me/(?!(?:"
        + _reserved_pattern()
        + r")(?:/|$))"
        + CHANNEL_PATTERN
        + "/"
        + POST_PATTERN,
    )
    @handle(
        "telegram.dog/s/",
        URL_PREFIX
        + r"(?:www\.)?telegram\.dog/s/"
        + CHANNEL_PATTERN
        + "/"
        + POST_PATTERN,
    )
    @handle(
        "telegram.dog/",
        URL_PREFIX
        + r"(?:www\.)?telegram\.dog/(?!(?:"
        + _reserved_pattern()
        + r")(?:/|$))"
        + CHANNEL_PATTERN
        + "/"
        + POST_PATTERN,
    )
    async def _parse(self, searched: re.Match[str]) -> list[MediaContent]:
        channel = searched.group("channel")
        post_id = int(searched.group("message_id"))
        post = await self._load_post(channel, post_id)
        if post is None:
            raise ParseException("Telegram 帖子不存在、已删除或当前不可见")
        if not self._has_content(post):
            raise ParseException("Telegram 帖子没有可解析内容")

        await self._send_description(post)
        contents = self._build_contents(post)
        return self.parse_result(contents)

    async def _load_post(
        self,
        channel: str,
        post_id: int,
    ) -> TelegramPost | None:
        channel_url = f"https://t.me/{channel}/{post_id}"
        candidates = (
            (f"https://t.me/s/{channel}/{post_id}", None),
            (channel_url, {"embed": "1", "single": ""}),
        )
        last_error: Exception | None = None
        for url, params in candidates:
            try:
                html = await self._fetch_page(url, params=params)
            except ParseException as exc:
                last_error = exc
                continue
            post = await asyncio.to_thread(
                parse_page_html,
                html,
                channel=channel,
                post_id=post_id,
            )
            if post is not None:
                return post
        if last_error is not None:
            logger.warning(
                "[telegram] 公开页面获取失败 | "
                f"channel={channel} | post={post_id} | "
                f"error_type={type(last_error).__name__}"
            )
        return None

    async def _fetch_page(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> str:
        retries = max(int(getattr(self.cfg, "download_retry_times", 2) or 0), 0)
        last_error: Exception | None = None
        headers = self.headers.copy()
        if cookie := self.cookiejar.get_cookie_header_for_url(url):
            headers["Cookie"] = cookie
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    proxy=self.proxy,
                    allow_redirects=True,
                ) as response:
                    host = (response.url.host or "").lower()
                    if not _host_matches(host, PAGE_HOST_SUFFIXES):
                        raise ParseException("Telegram 页面跳转到了不受支持的站点")
                    if response.status >= 400:
                        raise ClientError(
                            f"Telegram public page HTTP {response.status}"
                        )
                    length = response.content_length
                    if length is not None and length > MAX_PAGE_BYTES:
                        raise ParseException("Telegram 页面体积超过限制")
                    body = await response.content.read(MAX_PAGE_BYTES + 1)
                    if len(body) > MAX_PAGE_BYTES:
                        raise ParseException("Telegram 页面体积超过限制")
                    return body.decode("utf-8", errors="replace")
            except ParseException:
                raise
            except (ClientError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
        raise ParseException("Telegram 公开页面请求失败") from last_error

    @staticmethod
    def _has_content(post: TelegramPost) -> bool:
        return bool(
            post.text
            or post.sendable_media
            or post.media
            or post.preview
            or post.poll
            or post.buttons
        )

    async def _send_description(self, post: TelegramPost) -> None:
        author = self._author(post)
        text = self._description_text(post)
        extra = self._extra(post)
        metrics = self._metrics(post)
        cover_url = self._cover_url(post)
        repost = self._repost(post)
        video = next(
            (item for item in post.sendable_media if item.kind == "video"),
            None,
        )
        if video is not None:
            await self.send_desc_video(
                None,
                author,
                cover=cover_url,
                duration=video.duration,
                text=text,
                timestamp=post.timestamp,
                url=self._post_url(post),
                extra=extra,
                metrics=metrics,
                headers=self.media_headers,
                repost=repost,
            )
            return
        await self.send_desc_common(
            None,
            author,
            [],
            text=text,
            timestamp=post.timestamp,
            url=self._post_url(post),
            extra=extra,
            metrics=metrics,
            cover=cover_url,
            repost=repost,
        )

    def _author(self, post: TelegramPost) -> Author:
        avatar = (
            post.channel_avatar
            if _is_trusted_media_url(post.channel_avatar)
            else None
        )
        return self.create_author(
            post.display_author,
            avatar_url=avatar,
            description=post.channel_description,
            uid=f"@{post.channel}",
            headers=self.media_headers,
        )

    @staticmethod
    def _description_text(post: TelegramPost) -> str | None:
        parts = [post.text] if post.text else []
        if post.reply and post.forwarded:
            parts.append(
                f"回复 {post.reply.name}：{_short(post.reply.text) or ''}"
            )
        return "\n\n".join(part for part in parts if part) or None

    def _extra(self, post: TelegramPost) -> dict[str, str] | None:
        sections: list[str] = []
        if post.channel_description:
            sections.append(f"频道：{post.channel_description}")
        if post.forwarded:
            source = post.forwarded.author or post.forwarded.name
            source_url = post.forwarded.url
            forward_line = f"转发自：{source}"
            if source_url:
                forward_line += f"\n{source_url}"
            sections.append(forward_line)
        if post.reply:
            sections.append(
                f"引用：{post.reply.name}\n{_short(post.reply.text) or ''}"
            )
        if post.preview and bool(getattr(self.mycfg, "include_link_preview", True)):
            preview_lines = [
                f"链接预览：{post.preview.title or post.preview.site_name or '链接'}",
            ]
            if post.preview.description:
                preview_lines.append(_short(post.preview.description) or "")
            if post.preview.url:
                preview_lines.append(post.preview.url)
            sections.append("\n".join(line for line in preview_lines if line))
        if post.poll and bool(getattr(self.mycfg, "include_poll", True)):
            poll_lines = [f"投票：{post.poll.question}"]
            poll_lines.extend(
                f"{option.name} {option.percent}%"
                for option in post.poll.options
            )
            if post.poll.votes:
                poll_lines.append(f"票数：{post.poll.votes}")
            sections.append("\n".join(poll_lines))
        if post.buttons and bool(getattr(self.mycfg, "include_buttons", True)):
            button_lines = ["按钮："]
            button_lines.extend(
                f"{button.title}：{button.url}"
                if button.url
                else button.title
                for button in post.buttons
            )
            sections.append("\n".join(button_lines))
        if post.unsupported_media and bool(
            getattr(self.mycfg, "show_unsupported_media", True)
        ):
            sections.append("网页未提供部分媒体文件，可能需要 Telegram 客户端或登录账号。")
        return {"info": "\n\n".join(sections)} if sections else None

    @staticmethod
    def _metrics(post: TelegramPost) -> list[Metric]:
        metrics: list[Metric] = []
        if post.views:
            metrics.append(Metric(icon=None, label="浏览", value=post.views))
        metrics.extend(
            Metric(icon=None, label=item.label, value=item.count)
            for item in post.reactions
        )
        return metrics

    def _repost(self, post: TelegramPost) -> Description | None:
        if post.reply:
            return Description(
                platform=self.platform,
                author=Author(name=post.reply.name),
                text=post.reply.text,
                url=post.reply.url,
            )
        return None

    @staticmethod
    def _cover_url(post: TelegramPost) -> str | None:
        for item in post.media:
            for candidate in (item.thumb, item.url):
                if _is_trusted_media_url(candidate):
                    return candidate
        if post.preview and _is_trusted_media_url(post.preview.thumb):
            return post.preview.thumb
        if _is_trusted_media_url(post.channel_avatar):
            return post.channel_avatar
        return None

    def _build_contents(self, post: TelegramPost) -> list[MediaContent]:
        contents: list[MediaContent] = []
        for item in post.media:
            content = self._media_content(item)
            if content is not None:
                contents.append(content)
        return contents

    def _media_content(self, item: TelegramMedia) -> MediaContent | None:
        if not item.sendable or not _is_trusted_media_url(item.url):
            return None
        headers = self.media_headers
        if item.kind == "image":
            return self.create_image_contents([item.url], headers=headers)[0]
        if item.kind in {"video", "roundvideo"}:
            return self.create_video_content(
                item.url,
                cover_url=item.thumb,
                duration=item.duration,
                headers=headers,
            )
        if item.kind in {"gif", "dynamic"}:
            return self.create_dynamic_contents([item.url], headers=headers)[0]
        if item.kind in {"voice", "audio"}:
            return self.create_audio_content(
                item.url,
                duration=item.duration,
                headers=headers,
            )
        if item.kind == "file":
            return self.create_file_content(
                item.url,
                name=item.name,
                headers=headers,
            )
        return None

    @staticmethod
    def _post_url(post: TelegramPost) -> str:
        return f"https://t.me/{post.channel}/{post.post_id}"
