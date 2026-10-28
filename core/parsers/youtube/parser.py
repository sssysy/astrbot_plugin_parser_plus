from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import AudioContent, MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .media import (
    audio_format_selector,
    download_ranges,
    media_is_audio,
    parse_target,
    video_format_selector,
)
from .models import VideoData, decode_video


class YouTubeParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="youtube",
        display_name="YouTube",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://www.youtube.com/"})
        self.cookiejar = CookieJar(config, self.mycfg, domain="youtube.com")

    @property
    def _cookiefile(self) -> Path | None:
        cookie_file = self.cookiejar.cookie_file
        return cookie_file if cookie_file.is_file() else None

    @handle(
        "youtube.com/watch",
        r"(?<![A-Za-z0-9.-])(?:www\.|m\.)?youtube\.com/watch\?"
        r"[^\s<>\"']*(?<![A-Za-z0-9_-])v=(?P<vid>[A-Za-z0-9_-]{11})"
        r"(?![A-Za-z0-9_-])",
    )
    @handle(
        "youtube.com/shorts/",
        r"(?<![A-Za-z0-9.-])(?:www\.|m\.)?youtube\.com/shorts/"
        r"(?P<vid>[A-Za-z0-9_-]{11})",
    )
    @handle(
        "youtube.com/live/",
        r"(?<![A-Za-z0-9.-])(?:www\.|m\.)?youtube\.com/live/"
        r"(?P<vid>[A-Za-z0-9_-]{11})",
    )
    @handle(
        "youtube.com/embed/",
        r"(?<![A-Za-z0-9.-])(?:www\.|m\.)?youtube\.com/embed/"
        r"(?P<vid>[A-Za-z0-9_-]{11})",
    )
    @handle(
        "music.youtube.com/",
        r"(?<![A-Za-z0-9.-])music\.youtube\.com/watch\?"
        r"[^\s<>\"']*(?<![A-Za-z0-9_-])v=(?P<vid>[A-Za-z0-9_-]{11})"
        r"(?![A-Za-z0-9_-])",
    )
    @handle(
        "youtu.be",
        r"(?<![A-Za-z0-9.-])youtu\.be/(?P<vid>[A-Za-z0-9_-]{11})",
    )
    async def _parse(self, searched: re.Match[str]) -> list[MediaContent]:
        matched = searched.group(0)
        try:
            target = parse_target(matched, matched)
        except ValueError as exc:
            raise ParseException(str(exc)) from exc

        logger.debug(f"[youtube] 解析视频: {target.video_id}")
        raw = await self._extract(target.url)
        try:
            video = decode_video(raw)
        except ValueError as exc:
            raise ParseException(str(exc)) from exc

        author = self.create_author(
            video.channel or video.uploader or "YouTube",
            uid=video.channel_id or None,
        )
        if self.mycfg.include_channel_info and video.channel_id:
            author = await self._enrich_author(author, video)

        audio = media_is_audio(target, self.mycfg.media_mode)
        clipped = self._clip_seconds(video.duration)
        await self.send_desc_video(
            video.title,
            author,
            cover=video.thumbnail,
            duration=video.duration,
            quality=None if audio else self.mycfg.video_quality,
            text=video.description,
            timestamp=video.timestamp,
            url=target.url,
        )
        if audio:
            audio_content = AudioContent(None, video.duration)

            async def load_audio() -> Path:
                return await self._download_audio(target.url)

            audio_content.set_loader(load_audio)
            content: MediaContent = audio_content
        else:
            content = self.create_video_content_by_loader(
                lambda: self._download_video(target.url, clipped),
                cover_url=None,
                duration=video.duration,
            )
        return self.parse_result([content])

    def _clip_seconds(self, duration: float) -> int | None:
        value = int(getattr(self.mycfg, "clip_seconds", 0) or 0)
        if value > 0 and duration > value:
            return value
        return None

    async def _extract(self, url: str) -> dict:
        try:
            return await self.downloader.ytdlp_extract_raw(
                url,
                cookiefile=self._cookiefile,
                headers=self.headers,
                proxy=self.proxy,
                node=True,
            )
        except Exception as exc:
            logger.warning(
                "[youtube] 元数据获取失败 | "
                f"error_type={type(exc).__name__}"
            )
            raise ParseException("YouTube 视频信息获取失败") from exc

    async def _download_video(self, url: str, clip: int | None) -> Path:
        try:
            return await self.downloader.ytdlp_download_video(
                url,
                cookiefile=self._cookiefile,
                headers=self.headers,
                proxy=self.proxy,
                format=video_format_selector(
                    self.mycfg.video_quality,
                    self.mycfg.video_codec,
                ),
                node=True,
                download_ranges=download_ranges(clip),
                concurrent_fragments=4,
            )
        except Exception as exc:
            logger.warning(
                "[youtube] 视频下载失败 | "
                f"error_type={type(exc).__name__}"
            )
            raise ParseException("YouTube 视频下载失败") from exc

    async def _download_audio(self, url: str) -> Path:
        try:
            return await self.downloader.ytdlp_download_audio(
                url,
                cookiefile=self._cookiefile,
                headers=self.headers,
                proxy=self.proxy,
                format=audio_format_selector(),
                node=True,
            )
        except Exception as exc:
            logger.warning(
                "[youtube] 音频下载失败 | "
                f"error_type={type(exc).__name__}"
            )
            raise ParseException("YouTube 音频下载失败") from exc

    async def _enrich_author(self, author, video: VideoData):
        try:
            payload = await self._fetch_channel_page(video.channel_id)
            avatar, description = self._parse_channel_page(payload)
            if avatar or description:
                return self.create_author(
                    video.channel or video.uploader or "YouTube",
                    avatar_url=avatar,
                    description=description,
                    uid=video.channel_id or None,
                )
        except Exception as exc:
            logger.debug(
                "[youtube] 频道资料补全失败，使用基础信息 | "
                f"error_type={type(exc).__name__}"
            )
        return author

    async def _fetch_channel_page(self, channel_id: str) -> dict:
        path = channel_id if channel_id.startswith("/") else f"/channel/{channel_id}"
        url = f"https://www.youtube.com{path}"
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20251002.00.00",
                    "hl": "zh-CN",
                    "gl": "US",
                }
            },
            "browseId": channel_id,
        }
        async with self.session.post(
            "https://www.youtube.com/youtubei/v1/browse",
            params={"prettyPrint": "false"},
            json=payload,
            headers=self.headers,
        ) as resp:
            if resp.status >= 400:
                raise ParseException("YouTube 频道资料请求失败")
            return await resp.json(content_type=None)

    @staticmethod
    def _parse_channel_page(payload: dict) -> tuple[str | None, str | None]:
        metadata = (
            payload.get("metadata") if isinstance(payload, dict) else None
        ) or {}
        channel = metadata.get("channelMetadataRenderer") or {}
        thumbnails = (channel.get("avatar") or {}).get("thumbnails") or []
        avatar_urls = [
            item.get("url")
            for item in thumbnails
            if isinstance(item, dict) and item.get("url")
        ]
        return (
            avatar_urls[-1] if avatar_urls else None,
            channel.get("description") or None,
        )
