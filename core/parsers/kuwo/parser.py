from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from aiohttp import ClientError

from ...config import PluginConfig
from ...data import MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle

SHARE_API = "https://parse-api.sokoko.org/api/kuwo/song"
BODIAN_INFO_API = "https://bd-api.kuwo.cn/api/service/music/info"
BODIAN_AUDIO_API = "https://bd-api.kuwo.cn/api/service/music/audioUrl"
BODIAN_MV_API = "https://bd-api.kuwo.cn/api/service/mv/info"
BODIAN_HEADERS = {
    "User-Agent": "Dart/2.18 (dart:io)",
    "plat": "ar",
    "ver": "3.1.0",
    "channel": "aliopen",
}


class KuwoParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="kuwo", display_name="酷我音乐")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.bodian_headers = {**self.headers, **BODIAN_HEADERS}

    @handle(
        "kuwo.cn",
        r"https?://(?:www\.|m\.)?kuwo\.cn/.*play_detail/(?P<rid>\d+)",
    )
    async def _parse_share(self, searched: re.Match[str]):
        rid = searched.group("rid")
        payload = await self._get_json(
            SHARE_API,
            params={"music_id": rid, "quality": "320k"},
            headers=self.headers,
        )
        if _int(payload.get("code")) != 200:
            raise ParseException("酷我音乐接口返回错误")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ParseException("酷我音乐接口缺少歌曲数据")

        audio_url = _http_url(data.get("download_url"))
        if audio_url is None:
            raise ParseException("酷我音乐未返回有效音频地址")
        title = _text(data.get("title"), "未知歌曲")
        artist = _text(data.get("artist"), "未知歌手")
        duration = _float(data.get("duration_seconds"))
        album = _text(data.get("album"))
        cover = _http_url(data.get("cover"))
        avatar = _http_url(data.get("artist_pic"))
        lyric = _text(data.get("lyric"))
        quality = _quality_name(data.get("quality"))

        audio = self.create_audio_content(
            audio_url,
            duration=duration,
            headers=self.headers,
        )
        author = self.create_author(artist, avatar, headers=self.headers)
        info = _track_info(duration, album, quality)
        await self.send_desc_common(
            title,
            author,
            [],
            url=f"https://www.kuwo.cn/play_detail/{rid}",
            cover=cover,
            extra={"info": info},
        )
        contents: list[MediaContent | str] = [audio]
        if lyric:
            contents.append(lyric)
        return self.parse_result(contents)

    @handle(
        "h5app.kuwo.cn",
        r"https?://h5app\.kuwo\.cn/m/bodian/play(?:Music)?\.html\?"
        r"(?:[^\s]*&)?(?P<kind>musicId|mvId)=(?P<item_id>\d+)",
    )
    async def _parse_bodian(self, searched: re.Match[str]):
        item_id = searched.group("item_id")
        is_mv = searched.group("kind") == "mvId"
        info_payload = await self._get_json(
            BODIAN_INFO_API,
            params={"musicId": item_id, "uid": "-1", "token": ""},
            headers=self.bodian_headers,
        )
        info = info_payload.get("data")
        if not isinstance(info, dict):
            raise ParseException("波点音乐接口缺少歌曲数据")

        title = _text(info.get("name"), "未知歌曲")
        artist = _text(info.get("artist"), "未知歌手")
        album = _text(info.get("album"))
        cover = _http_url(info.get("albumPic120"))
        duration = _float(info.get("duration"))
        category_names = _category_names(info.get("categorys"))
        info_text = _track_info(duration, album)

        if is_mv:
            mv_payload = await self._get_json(
                BODIAN_MV_API,
                params={
                    "musicId": item_id,
                    "wifi": "1",
                    "noWifi": "1",
                    "uid": "-1",
                    "token": "",
                },
                headers=self.bodian_headers,
            )
            mv_data = mv_payload.get("data")
            mv = mv_data.get("mv") if isinstance(mv_data, dict) else None
            if not isinstance(mv, dict):
                raise ParseException("波点音乐接口缺少 MV 数据")
            video_url = _http_url(mv.get("highUrl")) or _http_url(mv.get("lowUrl"))
            if video_url is None:
                raise ParseException("波点音乐未返回有效 MV 地址")
            cover = _http_url(mv.get("coverUrl")) or cover
            duration = _float(mv.get("duration")) or duration
            cover_task = (
                self.downloader.download_img(
                    cover,
                    headers=self.bodian_headers,
                    proxy=self.proxy,
                )
                if cover
                else None
            )
            await self.send_desc_video(
                title,
                self.create_author(
                    artist,
                    headers=self.bodian_headers,
                ),
                cover=cover_task,
                duration=duration,
                text=f"专辑: {album}" if album else None,
                url=searched.group(0),
                extra={"info": info_text} if info_text else None,
            )
            content = self.create_video_content(
                video_url,
                cover_url=None,
                duration=duration,
                headers=self.bodian_headers,
            )
            content.cover = cover_task
            return self.parse_result([content])

        audio_payload = await self._get_json(
            BODIAN_AUDIO_API + f"/{item_id}",
            params={
                "format": "mp3",
                "br": "320kmp3",
                "songType": "",
                "fromList": "",
                "weListenUid": "",
                "weListenDevId": "",
            },
            headers=self.bodian_headers,
        )
        audio_data = audio_payload.get("data")
        audio_url = (
            _http_url(audio_data.get("audioUrl"))
            if isinstance(audio_data, dict)
            else None
        )
        if audio_url is None:
            raise ParseException("波点音乐未返回有效音频地址")
        audio = self.create_audio_content(
            audio_url,
            duration=duration,
            headers=self.bodian_headers,
        )
        extra = {"info": info_text} if info_text else None
        if category_names:
            extra = {**(extra or {}), "tags": category_names}
        await self.send_desc_common(
            title,
            self.create_author(artist, headers=self.bodian_headers),
            [],
            url=searched.group(0),
            cover=cover,
            extra=extra,
        )
        return self.parse_result([audio])

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        try:
            async with self.session.get(
                url,
                params=params,
                headers=headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"酷我音乐接口状态码 {resp.status}")
                payload = await resp.json(content_type=None)
        except (ClientError, TimeoutError) as exc:
            raise ParseException("酷我音乐接口请求失败") from exc
        except ValueError as exc:
            raise ParseException("酷我音乐接口返回数据异常") from exc
        if not isinstance(payload, dict):
            raise ParseException("酷我音乐接口返回数据异常")
        return payload


def _http_url(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _text(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


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


def _quality_name(value: object) -> str:
    if isinstance(value, dict):
        return _text(value.get("name"))
    return ""


def _category_names(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names = [
        _text(item.get("name"))
        for item in value
        if isinstance(item, dict) and _text(item.get("name"))
    ]
    return " | ".join(names)


def _track_info(duration: float, album: str, quality: str = "") -> str:
    return " | ".join(
        part
        for part in (
            f"时长: {_format_duration(duration)}" if duration > 0 else "",
            f"专辑: {album}" if album else "",
            quality,
        )
        if part
    )


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, second = divmod(total, 60)
    return f"{minutes:02d}:{second:02d}"
