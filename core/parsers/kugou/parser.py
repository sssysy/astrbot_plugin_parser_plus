from __future__ import annotations

import base64
import json
import re
from typing import Any, ClassVar

from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...data import MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle

SONG_INFO_URL = "https://m.kugou.com/app/i/getSongInfo.php"
LYRIC_SEARCH_URL = "https://krcs.kugou.com/search"
LYRIC_DOWNLOAD_URL = "https://lyrics.kugou.com/download"
HASH_RE = re.compile(
    r"var dataFromSmarty\s*=\s*(\[.*?\]),",
    flags=re.DOTALL,
)
JSON_HASH_RE = re.compile(r'"hash"\s*:\s*"([0-9A-Fa-f]{32})"')
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


class KugouParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="kugou", display_name="酷狗音乐")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://m.kugou.com/"})
        self.mobile_headers = {**self.headers, "User-Agent": MOBILE_UA}

    @handle(
        "t1.kugou.com",
        r"https?://t1\.kugou\.com/(?:"
        r"(?P<share_code>[A-Za-z0-9]+)"
        r"|(?P<before_hash>[^\s]*\bhash=(?P<hash>[0-9A-Fa-f]{32}))"
        r")",
    )
    async def _parse_short_link(self, searched: re.Match[str]):
        song_hash = searched.groupdict().get("hash")
        if song_hash:
            return await self._parse_hash(song_hash)
        return await self.parse_with_redirect(searched.group(0))

    @handle(
        "kugou.com",
        r"https?://[^\s]*?kugou\.com[^\s]*?(?:"
        r"/(?:share|mixsong)/[A-Za-z0-9]+\.html"
        r"|\bhash=(?P<hash>[0-9A-Fa-f]{32})"
        r"|\bchain=(?P<chain>[A-Za-z0-9]+)"
        r")",
    )
    async def _parse_share(self, searched: re.Match[str]):
        direct_hash = searched.groupdict().get("hash")
        if direct_hash:
            return await self._parse_hash(direct_hash)
        return await self._parse_share_page(searched.group(0))

    async def _parse_share_page(self, url: str):
        try:
            async with self.session.get(
                url,
                headers=self.mobile_headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"酷狗分享页状态码 {resp.status}")
                html = await resp.text()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("酷狗分享页请求失败") from exc

        song_hash = _extract_hash(html)
        if not song_hash:
            raise ParseException("未找到酷狗歌曲 hash")
        return await self._parse_hash(song_hash)

    async def _parse_hash(self, song_hash: str):
        song_hash = song_hash.upper()
        play_info = await self._get_json(
            SONG_INFO_URL,
            params={"cmd": "playInfo", "hash": song_hash},
        )
        if not isinstance(play_info, dict):
            raise ParseException("酷狗歌曲数据异常")
        if _int(play_info.get("errcode")) != 0:
            raise ParseException("酷狗歌曲不可播放")

        audio_url = str(play_info.get("url") or "")
        if not audio_url:
            raise ParseException("未找到酷狗音频地址")
        title = str(play_info.get("songName") or "未知歌曲")
        artist = str(play_info.get("singerName") or "未知歌手")
        duration = _float(play_info.get("timeLength"))
        actual_hash = str(play_info.get("hash") or song_hash)
        album_audio_id = str(play_info.get("album_audio_id") or "")

        lyric = ""
        try:
            lyric = await self._fetch_lyric(actual_hash)
        except Exception as exc:
            logger.warning(
                "[酷狗] 歌词获取失败 | "
                f"hash={actual_hash} | error_type={type(exc).__name__}"
            )

        cover_url = (
            str(play_info.get("album_img") or "").replace("{size}", "480") or None
        )
        audio = self.create_audio_content(
            audio_url,
            duration=duration,
            headers=self.headers,
        )
        author = self.create_author(artist, headers=self.headers)
        bitrate = _int(play_info.get("bitRate"))
        info = " | ".join(
            part
            for part in (
                f"比特率: {bitrate}K" if bitrate > 0 else "",
                f"时长: {_format_duration(duration)}",
            )
            if part
        )
        song_url = f"https://www.kugou.com/song/#{actual_hash.lower()}"
        if album_audio_id:
            song_url = (
                "https://h5.kugou.com/v2/v-5a15aeb1/index.html"
                f"?album_audio_id={album_audio_id}"
            )
        await self.send_desc_common(
            title,
            author,
            [],
            url=song_url,
            cover=cover_url,
            extra={"info": info},
        )
        contents: list[MediaContent | str] = [audio]
        if lyric:
            contents.append(lyric)
        return self.parse_result(contents)

    async def _fetch_lyric(self, song_hash: str) -> str:
        search_data = await self._get_json(
            LYRIC_SEARCH_URL,
            params={"hash": song_hash},
        )
        if not isinstance(search_data, dict):
            return ""
        candidates = search_data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            return ""
        lyric_id = candidate.get("id")
        accesskey = candidate.get("accesskey")
        if not lyric_id or not accesskey:
            return ""

        lyric_data = await self._get_json(
            LYRIC_DOWNLOAD_URL,
            params={
                "ver": "1",
                "id": lyric_id,
                "accesskey": accesskey,
                "fmt": "lrc",
            },
        )
        if not isinstance(lyric_data, dict):
            return ""
        if _int(lyric_data.get("error_code")) != 0:
            return ""
        content = lyric_data.get("content")
        if not isinstance(content, str) or not content:
            return ""
        charset = str(lyric_data.get("charset") or "utf-8")
        try:
            decoded = base64.b64decode(content)
            return decoded.decode(charset, errors="replace").strip()
        except (ValueError, LookupError):
            return ""

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
    ) -> Any:
        try:
            async with self.session.get(
                url,
                params=params,
                headers=self.headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"酷狗接口状态码 {resp.status}")
                return await resp.json(content_type=None)
        except (ClientError, TimeoutError) as exc:
            raise ParseException("酷狗接口请求失败") from exc
        except ValueError as exc:
            raise ParseException("酷狗接口返回数据异常") from exc


def _extract_hash(html: str) -> str:
    match = HASH_RE.search(html)
    if match:
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            value = str(data[0].get("hash") or "")
            if re.fullmatch(r"[0-9A-Fa-f]{32}", value):
                return value.upper()

    match = JSON_HASH_RE.search(html)
    return match.group(1).upper() if match else ""


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, second = divmod(total, 60)
    return f"{minutes:02d}:{second:02d}"
