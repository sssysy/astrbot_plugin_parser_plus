from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...data import AudioContent, MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle

API_BASE = "https://nextmusic.toubiec.cn/api"
API_REFERER = "https://wyapi.toubiec.cn/"
AUDIO_EXTENSIONS = frozenset({"flac", "wav", "m4a", "aac", "mp3"})


def random_ip() -> str:
    return ".".join(str(random.randint(0, 255)) for _ in range(4))


def parse_duration_to_seconds(duration: str) -> int:
    """把接口返回的 ``mm:ss`` 或 ``hh:mm:ss`` 转成秒。"""
    parts = duration.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("非法时长")
    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError("非法时长") from exc

    if len(values) == 1:
        hours, minutes, seconds = 0, 0, values[0]
    elif len(values) == 2:
        hours, minutes, seconds = 0, values[0], values[1]
    else:
        hours, minutes, seconds = values
    if not (hours >= 0 and 0 <= minutes < 60 and 0 <= seconds < 60):
        raise ValueError("非法时长")
    return hours * 3600 + minutes * 60 + seconds


def audio_extension(url: str) -> str:
    path = url.split("?", 1)[0]
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return extension if extension in AUDIO_EXTENSIONS else "mp3"


class NeteaseParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="netease",
        display_name="网易云音乐",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": API_REFERER})

    async def _fetch(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_payload = {
            **payload,
            "timestamp": int(time.time() * 1000),
            "ip": random_ip(),
        }
        try:
            async with self.session.post(
                f"{API_BASE}/{endpoint}",
                json=request_payload,
                headers=self.headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"网易云接口状态码 {resp.status}")
                result = await resp.json(content_type=None)
        except (ClientError, TimeoutError) as exc:
            raise ParseException("网易云接口请求失败") from exc
        except ValueError as exc:
            raise ParseException("网易云接口返回数据异常") from exc

        if not isinstance(result, dict) or result.get("code") != 200:
            raise ParseException("网易云接口返回错误")
        data = result.get("data")
        if not isinstance(data, dict):
            raise ParseException("网易云接口缺少歌曲数据")
        return data

    @handle("163cn.tv", r"https?://[^\s]*163cn\.tv/[A-Za-z0-9]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        return await self.parse_with_redirect(searched.group(0))

    @handle(
        "music.163.com",
        r"https?://music\.163\.com/(?:"
        r"song/(?P<path_id>\d+)"
        r"|(?:#/)?song\?[^\s]*\bid=(?P<query_id>\d+)"
        r")",
    )
    async def _parse_song(self, searched: re.Match[str]):
        song_id = searched.groupdict().get("path_id") or searched.groupdict().get(
            "query_id"
        )
        if not song_id:
            raise ParseException("无效的网易云歌曲链接")
        return await self._parse_song_id(song_id)

    async def _parse_song_id(self, song_id: str):
        song = await self._fetch("getSongInfo", {"id": song_id})
        title = str(song.get("name") or "未知歌曲")
        artist = str(song.get("singer") or "未知歌手")
        duration = _duration(song.get("duration"))

        lyric = ""
        try:
            lyric_data = await self._fetch("getSongLyric", {"id": song_id})
            lyric = str(lyric_data.get("lrc") or "").strip()
        except Exception as exc:
            logger.warning(
                "[网易云] 歌词获取失败 | "
                f"song_id={song_id} | error_type={type(exc).__name__}"
            )

        audio_url = ""
        level = "standard"
        for candidate in ("lossless", "standard"):
            try:
                url_data = await self._fetch(
                    "getSongUrl",
                    {"id": song_id, "level": candidate},
                )
            except Exception as exc:
                logger.warning(
                    "[网易云] 音源获取失败 | "
                    f"song_id={song_id} | level={candidate} | "
                    f"error_type={type(exc).__name__}"
                )
                continue
            candidate_url = url_data.get("url")
            if isinstance(candidate_url, str) and candidate_url:
                audio_url = candidate_url
                level = candidate
                break
        if not audio_url:
            raise ParseException("无法获取网易云音频地址")

        cover_url = str(song.get("picimg") or "") or None

        async def load_audio() -> Path:
            return await self.downloader.download_audio(
                audio_url,
                audio_name=f"netease_{song_id}.{audio_extension(audio_url)}",
                headers=self.headers,
                proxy=self.proxy,
            )

        audio = AudioContent(None, duration)
        audio.set_loader(load_audio)
        author = self.create_author(artist, headers=self.headers)
        await self.send_desc_common(
            title,
            author,
            [],
            url=f"https://music.163.com/song/{song_id}",
            cover=cover_url,
            extra={
                "info": (
                    f"音质: {level} | 格式: {audio_extension(audio_url)} | "
                    f"时长: {_format_duration(duration)}"
                )
            },
        )
        contents: list[MediaContent | str] = [audio]
        if lyric:
            contents.append(lyric)
        return self.parse_result(contents)


def _duration(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return float(parse_duration_to_seconds(value))
    except ValueError:
        return 0.0


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, second = divmod(total, 60)
    return f"{minutes:02d}:{second:02d}"
