from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .media import (
    InstagramMedia,
    InstagramPost,
    InstagramTarget,
    format_selector,
    image_urls,
    parse_target,
    pick_thumbnail,
    unwrap_entries,
    video_formats,
)


class InstagramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="instagram",
        display_name="Instagram",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Origin": "https://www.instagram.com",
                "Referer": "https://www.instagram.com/",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="instagram.com")

    @property
    def _cookiefile(self) -> Path | None:
        cookie_file = self.cookiejar.cookie_file
        return cookie_file if cookie_file.is_file() else None

    @handle(
        "instagram.com/p/",
        r"(?<![A-Za-z0-9.-])https?://(?:www\.)?instagram\.com/p/"
        r"(?P<code>[A-Za-z0-9_-]+)",
    )
    @handle(
        "instagram.com/reel",
        r"(?<![A-Za-z0-9.-])https?://(?:www\.)?instagram\.com/reels?/"
        r"(?P<code>[A-Za-z0-9_-]+)",
    )
    @handle(
        "instagram.com/tv/",
        r"(?<![A-Za-z0-9.-])https?://(?:www\.)?instagram\.com/tv/"
        r"(?P<code>[A-Za-z0-9_-]+)",
    )
    @handle(
        "instagram.com/share/",
        r"(?<![A-Za-z0-9.-])https?://(?:www\.)?instagram\.com/share/"
        r"(?:(?:p|reels?|tv)/)?(?P<code>[A-Za-z0-9_-]+)",
    )
    @handle(
        "instagr.am/",
        r"(?<![A-Za-z0-9.-])https?://(?:www\.)?instagr\.am/"
        r"(?:p|reels?|tv)/(?P<code>[A-Za-z0-9_-]+)",
    )
    async def _parse(self, searched: re.Match[str]):
        try:
            target = parse_target(searched.group(0))
        except ValueError as exc:
            raise ParseException(str(exc)) from exc

        if target.kind == "share":
            target = await self._resolve_share(target)

        info = await self._extract(target)
        post = self._build_post(target, info)
        await self._send_description(post)

        contents = self._build_contents(post)
        if not contents:
            raise ParseException("Instagram 作品没有可发送的媒体")
        return self.parse_result(contents)

    async def _resolve_share(self, target: InstagramTarget) -> InstagramTarget:
        final_url = await self.get_final_url(target.url, headers=self.headers)
        try:
            resolved = parse_target(final_url)
        except ValueError as exc:
            raise ParseException("Instagram 分享链接无法解析") from exc
        if resolved.kind == "share":
            raise ParseException("Instagram 分享链接未跳转到作品页")
        return resolved

    async def _extract(self, target: InstagramTarget) -> dict[str, Any]:
        try:
            return await self.downloader.ytdlp_extract_raw(
                target.url,
                cookiefile=self._cookiefile,
                headers=self.headers,
                proxy=self.proxy,
                ignore_no_formats_error=True,
            )
        except Exception as exc:
            logger.warning(
                "[instagram] 元数据获取失败 | "
                f"error_type={type(exc).__name__} | shortcode={target.shortcode}"
            )
            raise ParseException("Instagram 作品信息获取失败") from exc

    @staticmethod
    def _build_post(target: InstagramTarget, info: dict[str, Any]) -> InstagramPost:
        entries = unwrap_entries(info)
        if not entries:
            raise ParseException("Instagram 作品返回数据为空")

        medias: list[InstagramMedia] = []
        for entry in entries:
            formats = video_formats(entry)
            if formats:
                selected = max(
                    formats,
                    key=lambda item: _int_or_zero(item.get("height")),
                )
                medias.append(
                    InstagramMedia(
                        kind="video",
                        url=str(selected["url"]),
                        cover=pick_thumbnail(entry),
                        duration=_float_or_zero(entry.get("duration")),
                    )
                )
                continue
            for url in image_urls(entry):
                medias.append(InstagramMedia(kind="image", url=url))

        if not medias:
            raise ParseException("Instagram 作品没有可发送的媒体")

        author = _text(info.get("channel")) or _text(info.get("uploader"))
        title = author or _text(info.get("description")) or _text(info.get("title"))
        author_id = _text(info.get("uploader_id"))
        timestamp = info.get("timestamp")
        cover = next(
            (item.cover for item in medias if item.kind == "video" and item.cover),
            None,
        ) or next(
            (item.url for item in medias if item.kind == "image"),
            None,
        )
        return InstagramPost(
            target=target,
            title=title,
            author=author,
            author_id=author_id,
            text=_text(info.get("description")),
            timestamp=int(timestamp) if timestamp else None,
            cover=cover,
            medias=tuple(medias),
        )

    async def _send_description(self, post: InstagramPost) -> None:
        author = None
        if post.author:
            author = self.create_author(post.author, uid=post.author_id)
        if post.has_video:
            video = next(item for item in post.medias if item.kind == "video")
            await self.send_desc_video(
                post.title,
                author,
                cover=post.cover,
                duration=video.duration,
                text=post.text,
                timestamp=post.timestamp,
                url=post.target.url,
            )
            return
        await self.send_desc_common(
            post.title,
            author,
            [],
            cover=post.cover,
            text=post.text,
            timestamp=post.timestamp,
            url=post.target.url,
        )

    def _build_contents(self, post: InstagramPost) -> list[MediaContent]:
        contents: list[MediaContent] = []
        video_index = 0
        for media in post.medias:
            if media.kind == "video":
                video_index += 1
                contents.append(
                    self.create_video_content_by_loader(
                        lambda index=video_index: self._download_video(
                            post.target, index
                        ),
                        cover_url=media.cover,
                        duration=media.duration,
                        headers=self.headers,
                    )
                )
                continue
            contents.extend(
                self.create_image_contents([media.url], headers=self.headers)
            )
        return contents

    async def _download_video(
        self,
        target: InstagramTarget,
        index: int,
    ) -> Path:
        url = target.url
        if index > 1:
            url = f"{url}?img_index={index}"
        try:
            return await self.downloader.ytdlp_download_video_relaxed(
                url,
                cookiefile=self._cookiefile,
                headers=self.headers,
                proxy=self.proxy,
                format=format_selector(self.mycfg.video_quality),
                node=True,
            )
        except Exception as exc:
            logger.warning(
                "[instagram] 视频下载失败 | "
                f"error_type={type(exc).__name__} | shortcode={target.shortcode}"
            )
            raise ParseException("Instagram 视频下载失败") from exc


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float_or_zero(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0
