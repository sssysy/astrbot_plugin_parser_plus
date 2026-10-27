from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, ClassVar

from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...data import AudioContent, MediaContent, Platform
from ...download import Downloader
from ...exception import DownloadException, ParseException
from ..base import BaseParser, handle
from .decrypt import decrypt_qqmusic_file

API_URL = "https://parse-api.sokoko.org/api/qq/song"
SOURCE_SUFFIXES = frozenset(
    {".mflac", ".mflac0", ".mgg", ".mgg1", ".mmp4", ".flac", ".ogg", ".m4a", ".mp3"}
)
AUDIO_SIGNATURES = ((b"fLaC", ".flac"), (b"OggS", ".ogg"), (b"ID3", ".mp3"))


class QQMusicDecrypter:
    """下载 QMC 源文件，并在发送阶段按 ekey 解密。"""

    def __init__(
        self,
        downloader: Downloader,
        url: str,
        mid: str,
        ekey: str,
        headers: dict[str, str],
        proxy: str | None,
    ):
        self.downloader = downloader
        self.url = url
        self.mid = mid
        self.ekey = ekey
        self.headers = headers
        self.proxy = proxy

    async def __call__(self) -> Path:
        source = await self.downloader.download_audio(
            self.url,
            audio_name=f"qqmusic_{self.mid}{_source_suffix(self.url)}",
            headers=self.headers,
            proxy=self.proxy,
        )
        if not self.ekey:
            return await _ensure_audio_suffix(source)
        try:
            decrypted = await decrypt_qqmusic_file(source, self.ekey)
        except Exception as exc:
            logger.warning(
                "[QQ音乐] QMC 解密失败 | "
                f"mid={self.mid} | error_type={type(exc).__name__}"
            )
            raise DownloadException("QQ音乐音频解密失败") from exc
        return decrypted


class QQMusicParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="qqmusic", display_name="QQ音乐")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://y.qq.com/"})

    @handle(
        "c6.y.qq.com/base/fcgi-bin/u",
        r"https?://c6\.y\.qq\.com/base/fcgi-bin/u\?[^\s]*\b__=[A-Za-z0-9]+",
    )
    async def _parse_short_link(self, searched: re.Match[str]):
        return await self.parse_with_redirect(searched.group(0))

    @handle(
        "y.qq.com",
        r"https?://y\.qq\.com/n/(?:ryqq|ryqq_v2|yqq)/song(?:Detail/|/)(?P<song_id>[A-Za-z0-9]+)",
    )
    @handle(
        "i.y.qq.com/v8/playsong.html",
        r"https?://i\.y\.qq\.com/v8/playsong\.html\?[^\s]*(?:songmid|songid)=(?P<song_id>[A-Za-z0-9]+)",
    )
    @handle(
        "i2.y.qq.com/n3/other/pages/playsong",
        r"https?://i2\.y\.qq\.com/n3/other/pages/playsong/index\.html\?[^\s]*(?:songmid|songid)=(?P<song_id>[A-Za-z0-9]+)",
    )
    async def _parse_song(self, searched: re.Match[str]):
        return await self._parse_song_id(searched.group("song_id"))

    async def _parse_song_id(self, song_id: str):
        try:
            async with self.session.get(
                API_URL,
                params={"music_id": song_id, "quality": "high"},
                headers=self.headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"QQ音乐接口状态码 {resp.status}")
                payload = await resp.json(content_type=None)
        except (ClientError, TimeoutError) as exc:
            raise ParseException("QQ音乐接口请求失败") from exc
        except ValueError as exc:
            raise ParseException("QQ音乐接口返回数据异常") from exc

        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ParseException("QQ音乐接口返回错误")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ParseException("QQ音乐接口缺少歌曲数据")

        audio_url = str(data.get("download_url") or "")
        if not audio_url.startswith(("http://", "https://")):
            raise ParseException("QQ音乐接口未返回有效音频地址")
        mid = str(data.get("id") or song_id)
        ekey = str(data.get("ekey") or "")
        duration = _float(data.get("duration_seconds"))
        title = str(data.get("title") or "未知歌曲")
        artist = str(data.get("artist") or "未知歌手")
        album = str(data.get("album") or "")
        cover_url = str(data.get("cover") or "") or None
        lyric = str(data.get("lyric") or "").strip()
        quality = _quality_name(data.get("quality"))

        decrypter = QQMusicDecrypter(
            self.downloader,
            audio_url,
            mid,
            ekey,
            self.headers,
            self.proxy,
        )

        async def load_audio() -> Path:
            return await decrypter()

        audio = AudioContent(None, duration)
        audio.set_loader(load_audio)
        author = self.create_author(
            artist,
            str(data.get("artist_pic") or "") or None,
            headers=self.headers,
        )
        info = " | ".join(
            part
            for part in (
                f"时长: {_format_duration(duration)}",
                f"专辑: {album}" if album else "",
                quality,
            )
            if part
        )
        await self.send_desc_common(
            title,
            author,
            [],
            url=f"https://y.qq.com/n/yqq/song/{mid}",
            cover=cover_url,
            extra={"info": info},
        )
        contents: list[MediaContent | str] = [audio]
        if lyric:
            contents.append(lyric)
        return self.parse_result(contents)


def _source_suffix(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    suffix = Path(path).suffix
    return suffix if suffix in SOURCE_SUFFIXES else ".mp3"


async def _ensure_audio_suffix(path: Path) -> Path:
    try:
        header = await asyncio.to_thread(_read_header, path)
    except OSError:
        return path
    suffix = next(
        (
            extension
            for signature, extension in AUDIO_SIGNATURES
            if header.startswith(signature)
        ),
        "",
    )
    if not suffix and len(header) >= 8 and header[4:8] == b"ftyp":
        suffix = ".m4a"
    if (
        not suffix
        and len(header) >= 2
        and header[0] == 0xFF
        and header[1] & 0xE0 == 0xE0
    ):
        suffix = ".mp3"
    if not suffix or path.suffix.lower() == suffix:
        return path
    target = path.with_suffix(suffix)
    if target.exists():
        return target
    try:
        await asyncio.to_thread(path.replace, target)
    except OSError:
        return path
    return target


def _read_header(path: Path) -> bytes:
    with path.open("rb") as file:
        return file.read(12)


def _quality_name(value: Any) -> str:
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name:
            return name
    return ""


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, second = divmod(total, 60)
    return f"{minutes:02d}:{second:02d}"
