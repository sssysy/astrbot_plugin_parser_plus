from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import Author, MediaContent
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, Platform, handle


class TikTokParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="tiktok", display_name="TikTok")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Origin": "https://www.tiktok.com",
                "Referer": "https://www.tiktok.com/",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="tiktok.com")
        self._set_cookie_header()

    def _set_cookie_header(self) -> None:
        cookies = self.cookiejar.get_cookie_header_for_url(
            "https://www.tiktok.com/"
        )
        if cookies:
            self.headers["Cookie"] = cookies

    def _format_selector(self, quality: str) -> str:
        if quality == "auto":
            return "bv*+ba/b"
        height = quality.removesuffix("p")
        if not height.isdigit():
            return "bv*+ba/b"
        return (
            f"bv*[height<={height}]+ba/"
            f"b[height<={height}]/"
            "bv*+ba/b"
        )

    @staticmethod
    def _author_uid(info: dict[str, Any], url: str) -> str | None:
        uploader = str(info.get("uploader") or "").strip().lstrip("@")
        if uploader:
            return f"@{uploader}"
        matched = re.search(r"tiktok\.com/@([^/?#\s]+)/", url)
        if matched:
            return f"@{matched.group(1)}"
        return None

    async def _extract_info(self, url: str) -> dict[str, Any]:
        info = await self.downloader.ytdlp_extract_raw(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
            format=self._format_selector(self.mycfg.video_quality or "auto"),
        )
        if info.get("_type") == "playlist":
            entries = [
                item
                for item in info.get("entries") or ()
                if isinstance(item, dict)
            ]
            if len(entries) != 1:
                raise ParseException("TikTok 链接返回了多个作品，无法自动选择")
            info = entries[0]
        if not any(
            isinstance(item, dict)
            and item.get("vcodec") not in (None, "none")
            for item in info.get("formats") or ()
        ):
            raise ParseException("TikTok 作品没有可用的视频格式")
        return info

    async def _download_video(self, url: str) -> Path:
        try:
            return await self.downloader.ytdlp_download_video_relaxed(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
                format=self._format_selector(self.mycfg.video_quality or "auto"),
                node=True,
            )
        except Exception as exc:
            logger.warning(
                "[TikTok] 视频下载失败 | "
                f"error_type={type(exc).__name__}"
            )
            raise ParseException("TikTok 视频下载失败") from exc

    async def _send_description(
        self,
        *,
        title: str,
        author_name: str | None,
        author_uid: str | None,
        thumbnail: str | None,
        duration: float,
        description: str | None,
        timestamp: int | None,
        url: str,
    ) -> None:
        author = None
        if author_name:
            author = Author(name=author_name, uid=author_uid)
        await self.send_desc_video(
            title,
            author,
            cover=thumbnail,
            duration=duration,
            text=description,
            timestamp=timestamp,
            url=url,
        )

    @handle(
        "tiktok.com",
        r"(?:www|vt|vm)\.tiktok\.com/"
        r"(?:@[^/?#\s]+/(?:video|photo)/\d+|t/[A-Za-z0-9_-]+/?|"
        r"[A-Za-z0-9_-]+/?)",
    )
    async def _parse(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        info = await self._extract_info(url)

        title = info.get("title") or info.get("description") or "TikTok"
        channel = info.get("channel") or info.get("uploader")
        thumbnails = [
            str(item["url"])
            for item in info.get("thumbnails") or ()
            if isinstance(item, dict) and item.get("url")
        ]
        thumbnail_value = info.get("thumbnail")
        thumbnail = (
            str(thumbnail_value)
            if thumbnail_value
            else (thumbnails[-1] if thumbnails else None)
        )
        duration = info.get("duration")
        timestamp = info.get("timestamp")
        video_url = info.get("webpage_url") or url
        await self._send_description(
            title=str(title),
            author_name=str(channel) if channel else None,
            author_uid=self._author_uid(info, video_url),
            thumbnail=thumbnail,
            duration=float(duration or 0),
            description=info.get("description"),
            timestamp=int(timestamp) if timestamp else None,
            url=video_url,
        )

        content: MediaContent = self.create_video_content_by_loader(
            lambda: self._download_video(video_url),
            cover_url=thumbnail,
            duration=float(duration or 0),
            headers=self.headers,
        )
        return self.parse_result([content])
