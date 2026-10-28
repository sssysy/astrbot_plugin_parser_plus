from __future__ import annotations

import re
from typing import ClassVar

from aiohttp import ClientError

from ...config import PluginConfig
from ...data import MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .share import (
    artist_avatar_url,
    cover_url,
    decode_share_html,
    decode_track,
    format_lyrics,
    track_album,
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


class QSMusicParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="qsmusic",
        display_name="汽水音乐",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mobile_headers = {**self.headers, "User-Agent": MOBILE_UA}

    @handle(
        "qishui.douyin.com",
        r"https?://qishui\.douyin\.com/s/[A-Za-z0-9]+/?",
    )
    async def _parse_share(self, searched: re.Match[str]):
        share_url = searched.group(0)
        html = await self._fetch_share_page(share_url)
        track = decode_track(decode_share_html(html))

        title = str(track.get("trackName") or "未知歌曲")
        artist = str(track.get("artistName") or "未知歌手")
        duration = _float(track.get("duration"))
        album = track_album(track)
        lyric = format_lyrics(track)
        cover = cover_url(track)
        avatar = artist_avatar_url(track)

        audio = self.create_audio_content(
            str(track["url"]),
            duration=duration,
            headers=self.mobile_headers,
        )
        author = self.create_author(
            artist,
            avatar,
            headers=self.mobile_headers,
        )
        info = " | ".join(
            part
            for part in (
                f"时长: {_format_duration(duration)}",
                f"专辑: {album}" if album else "",
            )
            if part
        )
        await self.send_desc_common(
            title,
            author,
            [],
            url=share_url,
            cover=cover,
            extra={"info": info},
        )
        contents: list[MediaContent | str] = [audio]
        if lyric:
            contents.append(lyric)
        return self.parse_result(contents)

    async def _fetch_share_page(self, url: str) -> str:
        try:
            async with self.session.get(
                url,
                headers=self.mobile_headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"汽水音乐分享页状态码 {resp.status}")
                return await resp.text()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("汽水音乐分享页请求失败") from exc


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, second = divmod(total, 60)
    return f"{minutes:02d}:{second:02d}"
