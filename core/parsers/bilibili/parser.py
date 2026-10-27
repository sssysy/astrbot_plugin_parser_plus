from __future__ import annotations

import asyncio
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import Author, MediaContent, Metric
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, Platform, handle
from .auth import (
    BiliAuthError,
    BiliCredential,
    PollResult,
    QrLogin,
    load_credential,
    poll_login,
    refresh_credential,
    save_credential,
)
from .models import (
    BiliDynamicAuthor,
    BiliDynamicContent,
    BiliUserCard,
    BiliVideoInfo,
    decode_bangumi,
    decode_favorite,
    decode_live,
    decode_user,
    decode_video,
)
from .utils import (
    av2bv,
    bv2av,
    clean_cover,
    extract_rich_text,
    is_allowed_media_host,
    metric,
    normalize_url,
    parse_dynamic_time,
)


QUALITY_MAP: ClassVar[dict[str, int]] = {
    "360p": 16,
    "480p": 32,
    "720p": 64,
    "1080p": 80,
    "1080p+": 112,
    "1080p60": 116,
    "4k": 120,
    "8k": 127,
}


class BilibiliParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="哔哩哔哩")
    API_VIEW: ClassVar[str] = "https://api.bilibili.com/x/web-interface/wbi/view"
    API_PLAYURL: ClassVar[str] = (
        "https://api.bilibili.com/x/player/wbi/playurl"
    )
    API_FAVORITE: ClassVar[str] = (
        "https://api.bilibili.com/x/v3/fav/resource/list"
    )
    API_BANGUMI: ClassVar[str] = "https://api.bilibili.com/pgc/view/web/season"
    API_LIVE: ClassVar[str] = "https://api.live.bilibili.com/room/v1/Room/get_info"
    API_USER: ClassVar[str] = "https://api.bilibili.com/x/web-interface/card"
    API_DYNAMIC: ClassVar[str] = (
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
    )
    API_OPUS: ClassVar[str] = (
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
    )
    ARTICLE_CARD_API: ClassVar[str] = "https://api.bilibili.com/x/article/view"
    DANMAKU_API: ClassVar[str] = (
        "https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"
    )
    RETRYABLE_STATUSES: ClassVar[frozenset[int]] = frozenset(
        {403, 404, 408, 425, 429, *range(500, 600)}
    )
    BLOCKED_PCDN_DOMAINS: ClassVar[tuple[str, ...]] = (
        "mcdn.bilivideo.com",
        "mcdn.bilivideo.cn",
        "szbdyd.com",
        "mountaintoys.cn",
        "nexusedgeio.com",
        "ahdohpiechei.com",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="bilibili.com")
        self._credential: BiliCredential | None = None
        self._credential_file = self.cfg.cookie_dir / "bilibili_credential.json"
        self._credential_lock = asyncio.Lock()
        self._set_cookies()
        self._user_cache: dict[int | str, BiliUserCard | None] = {}

    @property
    def media_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        headers["Referer"] = "https://www.bilibili.com/"
        return headers

    @property
    def quality(self) -> int:
        value = str(getattr(self.mycfg, "video_quality", "auto") or "auto")
        return QUALITY_MAP.get(value.lower(), 127)

    def _set_cookies(self) -> None:
        cookies_str = self.cookiejar.cookies_str
        if self._credential is not None:
            cookies_str = self._credential.cookie_header
        if cookies_str:
            self.headers["Cookie"] = cookies_str

    def _request_headers(self, url: str | None = None) -> dict[str, str]:
        headers = self.headers.copy()
        if url:
            cookie = self.cookiejar.get_cookie_header_for_url(url)
            if cookie:
                headers["Cookie"] = cookie
        return headers

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        error: str,
    ) -> dict[str, Any]:
        retries = self.cfg.download_retry_times
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url,
                    params=params,
                    headers=headers or self._request_headers(url),
                    proxy=self.proxy,
                ) as response:
                    if response.status >= 400:
                        raise ClientError(f"HTTP {response.status}")
                    payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise ValueError("响应不是 JSON 对象")
                return payload
            except (ClientError, TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
        logger.warning(
            f"[哔哩哔哩] 请求失败 | host={urlparse(url).hostname or 'unknown'} | "
            f"error_type={type(last_error).__name__}"
        )
        raise ParseException(error) from last_error

    async def _get_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        error: str,
    ) -> bytes:
        retries = self.cfg.download_retry_times
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url,
                    params=params,
                    headers=headers or self._request_headers(url),
                    proxy=self.proxy,
                ) as response:
                    if response.status >= 400:
                        raise ClientError(f"HTTP {response.status}")
                    return await response.read()
            except (ClientError, TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
        logger.warning(
            f"[哔哩哔哩] 请求失败 | host={urlparse(url).hostname or 'unknown'} | "
            f"error_type={type(last_error).__name__}"
        )
        raise ParseException(error) from last_error

    @handle("b23.tv", r"b23\.tv/[0-9A-Za-z._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[0-9A-Za-z._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = normalize_url(searched.group(0))
        if not url.startswith("http"):
            url = f"https://{url}"
        final_url = await self.get_final_url(
            url, headers=self._request_headers(url)
        )
        keyword, matched = self.search_url(final_url)
        return await self.parse(keyword, matched)

    @handle(
        "/BV",
        r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9A-Za-z]{10})"
        r"(?:[?&]p=(?P<page>\d{1,3}))?",
    )
    @handle(
        "BV",
        r"^(?P<bvid>BV[0-9A-Za-z]{10})(?:\s+(?P<page>\d{1,3}))?$",
    )
    async def _parse_bv(self, searched: re.Match[str]):
        bvid = searched.group("bvid")
        page = int(searched.groupdict().get("page") or 1)
        return await self._parse_video(bvid=bvid, page=page)

    @handle(
        "/av",
        r"bilibili\.com(?:/video)?/av(?P<avid>\d{5,})"
        r"(?:[?&]p=(?P<page>\d{1,3}))?",
    )
    @handle(
        "av",
        r"^av(?P<avid>\d{5,})(?:\s+(?P<page>\d{1,3}))?$",
    )
    async def _parse_av(self, searched: re.Match[str]):
        avid = int(searched.group("avid"))
        page = int(searched.groupdict().get("page") or 1)
        return await self._parse_video(bvid=av2bv(avid), avid=avid, page=page)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: re.Match[str]):
        return await self._parse_dynamic_by_id(searched.group("dynamic_id"))

    @handle("/opus/", r"bilibili\.com/opus/(?P<opus_id>\d+)")
    async def _parse_opus(self, searched: re.Match[str]):
        return await self._parse_opus_by_id(searched.group("opus_id"))

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: re.Match[str]):
        read_id = int(searched.group("read_id"))
        try:
            return await self._parse_opus_by_id(str(read_id))
        except ParseException:
            return await self._parse_article_by_id(read_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: re.Match[str]):
        return await self._parse_live_by_id(int(searched.group("room_id")))

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favorite(self, searched: re.Match[str]):
        return await self._parse_favorite_by_id(int(searched.group("fav_id")))

    @handle(
        "bilibili.com/bangumi/play",
        r"bilibili\.com/bangumi/play/(?P<kind>ep|ss)(?P<id>\d+)",
    )
    async def _parse_bangumi(self, searched: re.Match[str]):
        return await self._parse_bangumi_by_id(
            searched.group("kind"), int(searched.group("id"))
        )

    async def _fetch_video(self, bvid: str) -> BiliVideoInfo:
        payload = await self._get_json(
            self.API_VIEW,
            params={"bvid": bvid},
            error="获取视频信息失败",
        )
        try:
            info = decode_video(payload)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("视频信息解析失败") from exc
        if not info.bvid:
            raise ParseException("视频不存在或不可访问")
        return info

    async def _fetch_user(self, mid: int | str) -> BiliUserCard | None:
        if not mid:
            return None
        if mid in self._user_cache:
            return self._user_cache[mid]
        try:
            payload = await self._get_json(
                self.API_USER,
                params={"mid": mid},
                error="获取 UP 主资料失败",
            )
        except ParseException:
            return None
        user = decode_user(payload)
        self._user_cache[mid] = user
        return user

    async def _parse_video(
        self,
        *,
        bvid: str,
        page: int = 1,
        avid: int | None = None,
    ):
        info = await self._fetch_video(bvid)
        pages = info.pages or []
        page_index = page - 1
        if not pages:
            pages_duration = info.duration
            cid = info.cid
            page_title = info.title
            cover = info.cover_url
            timestamp = info.pubdate or info.ctime
        else:
            page_index %= len(pages)
            selected = pages[page_index]
            pages_duration = selected.duration
            cid = selected.cid
            page_title = info.title
            if len(pages) > 1:
                page_title = f"{info.title} | 分集 - {selected.part}"
            cover = clean_cover(selected.first_frame) or info.cover_url
            timestamp = selected.ctime or info.pubdate or info.ctime
        if not cid:
            raise ParseException("视频分集信息缺失")
        if self.cfg.max_duration and pages_duration > self.cfg.max_duration:
            raise ParseException(
                f"视频时长超过限制（{self.cfg.source_max_minute} 分钟）"
            )

        await self._ensure_credential()
        stream = await self._fetch_stream(
            bvid=info.bvid,
            aid=info.aid or 0,
            cid=cid,
            duration=pages_duration,
        )
        stream_urls = stream["video"]
        audio_urls = stream["audio"]
        quality_label = stream["quality"]
        resolution = stream["resolution"]
        user = await self._fetch_user(info.owner.mid)
        author = self._build_author(
            info.owner.name, info.owner.face, info.owner.mid, user
        )
        url = f"https://www.bilibili.com/video/{info.bvid}"
        if page_index > 0:
            url += f"?p={page_index + 1}"
        metrics = info.stat.metrics
        extra: dict[str, Any] = {}
        if getattr(self.mycfg, "danmaku", True):
            danmaku = await self._fetch_hot_danmaku(cid)
            if danmaku:
                extra["info"] = "热门弹幕 · " + "  ".join(danmaku)
        await self.send_desc_video(
            page_title,
            author,
            cover=cover,
            duration=pages_duration,
            quality=quality_label,
            resolution=resolution,
            text=info.desc or None,
            timestamp=timestamp,
            url=url,
            extra=extra or None,
            metrics=metrics,
            headers=self.media_headers,
        )
        content = self._build_video_content(
            stream_urls,
            audio_urls,
            cover,
            pages_duration,
        )
        return self.parse_result([content])

    async def _fetch_stream(
        self,
        *,
        bvid: str,
        aid: int,
        cid: int,
        duration: int,
    ) -> dict[str, Any]:
        if not cid:
            raise ParseException("视频分集信息缺失")
        payload = await self._get_json(
            self.API_PLAYURL,
            params={
                "bvid": bvid,
                "avid": aid or bv2av(bvid),
                "cid": cid,
                "qn": self.quality,
                "fnver": 0,
                "fnval": 4048,
                "fourk": 1,
            },
            error="获取视频播放流失败",
        )
        if payload.get("code") != 0:
            raise ParseException(str(payload.get("message") or "获取视频播放流失败"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ParseException("视频播放流为空")
        dash = data.get("dash")
        if isinstance(dash, dict):
            videos = self._normalize_streams(dash.get("video"), kind="video")
            audios = self._normalize_streams(dash.get("audio"), kind="audio")
            if videos:
                selected = videos[0]
                selected_audio = audios[0] if audios else None
                return {
                    "video": selected["urls"],
                    "audio": selected_audio["urls"] if selected_audio else None,
                    "quality": selected["quality"],
                    "resolution": selected["resolution"],
                    "duration": duration,
                }
        durl = data.get("durl")
        if isinstance(durl, list):
            urls = [
                item["url"]
                for item in durl
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            ]
            if urls:
                return {
                    "video": urls,
                    "audio": None,
                    "quality": "MP4",
                    "resolution": "",
                    "duration": duration,
                }
        raise ParseException("未找到可用的视频流")

    def _normalize_streams(self, streams: Any, *, kind: str) -> list[dict[str, Any]]:
        if not isinstance(streams, list):
            return []
        result: list[dict[str, Any]] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            base = stream.get("baseUrl") or stream.get("base_url")
            if not isinstance(base, str) or not base:
                continue
            backup = stream.get("backupUrl") or stream.get("backup_url") or []
            urls = [base, *[u for u in backup if isinstance(u, str)]]
            urls = [
                u
                for u in urls
                if is_allowed_media_host(u) and not self._is_pcdn_url(u)
            ]
            if not urls:
                continue
            quality_id = self._stream_int(stream.get("id"))
            width = self._stream_int(stream.get("width"))
            height = self._stream_int(stream.get("height"))
            label = self._quality_label(quality_id) if kind == "video" else "音频"
            result.append(
                {
                    "urls": urls,
                    "quality_id": quality_id,
                    "quality": label,
                    "resolution": f"{width} × {height}" if width and height else "",
                    "codec": str(stream.get("codecs") or ""),
                }
            )
        if kind == "video":
            preferred = self._codec_rank()
            result.sort(
                key=lambda item: (
                    item["quality_id"],
                    preferred(item["codec"]),
                ),
                reverse=True,
            )
        else:
            result.sort(key=lambda item: item["quality_id"], reverse=True)
        return result

    @staticmethod
    def _stream_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _codec_rank(self):
        preference = str(getattr(self.mycfg, "video_codec", "auto") or "auto")
        aliases = {
            "auto": ("avc", "hev", "av01"),
            "avc": ("avc",),
            "hev": ("hev",),
            "av1": ("av01",),
        }
        order = list(reversed(aliases.get(preference.lower(), aliases["auto"])))

        def rank(codec: str) -> int:
            lowered = codec.lower()
            return next(
                (index for index, name in enumerate(order) if name in lowered),
                -1,
            )

        return rank

    def _is_pcdn_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(
            host == domain or host.endswith(f".{domain}")
            for domain in self.BLOCKED_PCDN_DOMAINS
        )

    @staticmethod
    def _quality_label(quality_id: int) -> str:
        return {
            127: "8K",
            126: "杜比视界",
            125: "HDR",
            120: "4K",
            116: "1080P60",
            112: "1080P+",
            80: "1080P",
            74: "720P60",
            64: "720P",
            32: "480P",
            16: "360P",
            6: "240P",
        }.get(quality_id, f"{quality_id}P")

    def _build_video_content(
        self,
        videos: list[str],
        audios: list[str] | None,
        cover: str | None,
        duration: int,
    ) -> MediaContent:
        digest = sha256(videos[0].split("?", 1)[0].encode()).hexdigest()[:16]
        output_name = f"bilibili_{digest}.mp4"

        async def load() -> Path:
            output_path = self.cfg.cache_dir / output_name
            if output_path.exists():
                return output_path
            if audios:
                last_error: Exception | None = None
                for video_url in videos:
                    for audio_url in audios:
                        try:
                            return await self.downloader.download_av_and_merge(
                                video_url,
                                audio_url,
                                output_path=output_path,
                                headers=self.media_headers,
                                proxy=self.proxy,
                            )
                        except (ParseException, OSError) as exc:
                            last_error = exc
                raise ParseException("视频音轨下载或合并失败") from last_error
            last_error: Exception | None = None
            for video_url in videos:
                try:
                    return await self.downloader.download_video(
                        video_url,
                        video_name=output_name,
                        headers=self.media_headers,
                        proxy=self.proxy,
                        retry_http_statuses=self.RETRYABLE_STATUSES,
                    )
                except (ParseException, OSError) as exc:
                    last_error = exc
            raise ParseException("视频流下载失败") from last_error

        return self.create_video_content_by_loader(
            load,
            cover_url=cover,
            duration=duration,
            headers=self.media_headers,
        )

    async def close_session(self) -> None:
        self._user_cache.clear()
        self._credential = None
        await super().close_session()

    async def _ensure_credential(self) -> BiliCredential | None:
        if self._credential is not None and not self._credential.needs_refresh:
            return self._credential

        async with self._credential_lock:
            if self._credential is None:
                if self._credential_file.exists():
                    self._credential = load_credential(self._credential_file)
                    if self._credential is not None:
                        self._set_cookies()
                return self._credential

            if not self._credential.needs_refresh:
                return self._credential

            if not self._credential.can_refresh:
                logger.warning("[哔哩哔哩] 凭证过期，缺少 access/refresh_token")
                return self._credential
            try:
                refreshed = await refresh_credential(self._credential)
            except BiliAuthError as exc:
                logger.warning(f"[哔哩哔哩] 凭证续期失败: {exc}")
                return self._credential
            self._credential = refreshed
            save_credential(self._credential_file, refreshed)
            self._set_cookies()
            logger.info("[哔哩哔哩] 凭证续期成功")
            return self._credential

    async def login_with_qrcode(self) -> bytes:
        self._qr_login = QrLogin()
        return await self._qr_login.generate_qrcode()

    async def complete_qr_login(self) -> PollResult:
        if getattr(self, "_qr_login", None) is None:
            raise ParseException("请先生成哔哩哔哩登录二维码")
        result = await poll_login(self._qr_login)
        if result.credential is not None:
            self._credential = result.credential
            save_credential(self._credential_file, result.credential)
            self._set_cookies()
        return result

    async def _fetch_hot_danmaku(self, cid: int) -> list[str]:
        """获取高频弹幕；失败只影响卡片附加信息，不阻断解析。"""
        if not cid:
            return []
        try:
            raw = await self._get_bytes(
                self.DANMAKU_API.format(cid=cid),
                error="获取弹幕失败",
            )
        except ParseException:
            return []
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        counts: dict[str, int] = {}
        for node in root.findall(".//d"):
            text = (node.text or "").strip()
            if not text or len(text) > 60:
                continue
            counts[text] = counts.get(text, 0) + 1
        return [
            text
            for text, _ in sorted(
                counts.items(), key=lambda item: item[1], reverse=True
            )[:5]
        ]

    def _build_author(
        self,
        name: str,
        face: str,
        mid: int | str,
        user: BiliUserCard | None,
    ) -> Author:
        return self.create_author(
            name or (user.name if user else ""),
            clean_cover(face or (user.face if user else "")),
            description=user.sign if user else None,
            uid=str(mid) if mid else None,
            metrics=user.metrics if user else (),
            headers=self.media_headers,
        )

    async def _parse_dynamic_by_id(self, dynamic_id: str):
        payload = await self._get_json(
            self.API_DYNAMIC,
            params={"id": dynamic_id},
            error="获取动态信息失败",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ParseException(str(payload.get("message") or "动态不存在"))
        item = payload["data"].get("item")
        if not isinstance(item, dict):
            raise ParseException("动态内容为空")
        dynamic = self._build_dynamic(item, dynamic_id)
        return await self._finish_dynamic(dynamic)

    async def _parse_opus_by_id(self, opus_id: str):
        payload = await self._get_json(
            self.API_OPUS,
            params={"id": opus_id, "timezone_offset": -480},
            error="获取图文信息失败",
        )
        if payload.get("code") == -352:
            raise ParseException("获取图文信息失败，B 站风控拦截")
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ParseException(str(payload.get("message") or "图文不存在"))
        item = payload["data"].get("item")
        if not isinstance(item, dict):
            raise ParseException("图文内容为空")
        dynamic = self._build_dynamic(item, opus_id)
        return await self._finish_dynamic(dynamic)

    def _build_dynamic(self, item: dict[str, Any], ident: str) -> BiliDynamicContent:
        modules = item.get("modules")
        if not isinstance(modules, dict):
            modules = {}
        author_data = modules.get("module_author")
        author = BiliDynamicAuthor()
        if isinstance(author_data, dict):
            author = BiliDynamicAuthor(
                mid=str(author_data.get("mid") or ""),
                name=str(author_data.get("name") or ""),
                face=normalize_url(str(author_data.get("face") or "")),
                sign=str(author_data.get("sign") or ""),
                timestamp=parse_dynamic_time(
                    author_data.get("pub_ts") or author_data.get("pub_time")
                ),
            )
        basic = item.get("basic")
        title = str(basic.get("title") or "") if isinstance(basic, dict) else ""
        dynamic_data = modules.get("module_dynamic")
        text = ""
        images: list[str] = []
        cover: str | None = None
        url = f"https://t.bilibili.com/{ident}"
        if isinstance(dynamic_data, dict):
            desc = dynamic_data.get("desc")
            if isinstance(desc, dict):
                text = extract_rich_text(desc.get("rich_text_nodes")) or str(
                    desc.get("text") or ""
                )
            major = dynamic_data.get("major")
            if isinstance(major, dict):
                draw = major.get("draw")
                if isinstance(draw, dict):
                    for pic in draw.get("items", []):
                        if isinstance(pic, dict) and isinstance(pic.get("src"), str):
                            images.append(normalize_url(pic["src"]))
                archive = major.get("archive")
                if isinstance(archive, dict):
                    title = title or str(archive.get("title") or "")
                    cover = normalize_url(str(archive.get("cover") or "")) or None
                    jump_url = archive.get("jump_url")
                    if isinstance(jump_url, str) and jump_url.startswith("http"):
                        url = jump_url
                    bvid = str(archive.get("bvid") or "")
                    if bvid:
                        url = f"https://www.bilibili.com/video/{bvid}"
                opus_data = major.get("opus")
                if isinstance(opus_data, dict):
                    title = title or str(opus_data.get("title") or "")
                    summary = opus_data.get("summary")
                    if isinstance(summary, dict):
                        text = (
                            text
                            or extract_rich_text(summary.get("rich_text_nodes"))
                            or str(summary.get("text") or "")
                        )
                    for pic in opus_data.get("pics", []):
                        if isinstance(pic, dict) and isinstance(pic.get("url"), str):
                            images.append(normalize_url(pic["url"]))
        stat = modules.get("module_stat")
        metrics: list[Metric] = []
        if isinstance(stat, dict):
            for label, key in (
                ("点赞", "like"),
                ("评论", "comment"),
                ("分享", "forward"),
            ):
                node = stat.get(key)
                if isinstance(node, dict):
                    metrics.append(metric(label, node.get("count")))
        dynamic = BiliDynamicContent(
            url=url,
            title=title or None,
            text=text or None,
            author=author,
            images=images,
            cover=cover,
            metrics=metrics,
        )
        return dynamic

    async def _finish_dynamic(self, dynamic: BiliDynamicContent):
        author = self.create_author(
            dynamic.author.name or "哔哩哔哩用户",
            clean_cover(dynamic.author.face),
            description=dynamic.author.sign or None,
            uid=dynamic.author.mid or None,
            headers=self.media_headers,
        )
        await self.send_desc_common(
            dynamic.title,
            author,
            [],
            text=dynamic.text,
            cover=next(iter(dynamic.images), dynamic.cover),
            timestamp=dynamic.author.timestamp,
            url=dynamic.url,
            metrics=dynamic.metrics,
        )
        contents: list[MediaContent] = []
        if dynamic.images:
            contents = self.create_image_contents(
                dynamic.images, headers=self.media_headers
            )
        elif dynamic.cover:
            contents = self.create_image_contents(
                [dynamic.cover], headers=self.media_headers
            )
        return self.parse_result(contents)

    async def _parse_live_by_id(self, room_id: int):
        payload = await self._get_json(
            self.API_LIVE,
            params={"room_id": room_id},
            error="获取直播间信息失败",
        )
        try:
            room = decode_live(payload)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("直播间信息不可用") from exc
        user = await self._fetch_user(room.uid)
        author = self._build_author(
            user.name if user else str(room.uid),
            user.face if user else "",
            room.uid,
            user,
        )
        status = {0: "未开播", 1: "直播中", 2: "轮播中"}.get(
            room.live_status, "未开播"
        )
        metrics = [
            metric("在线", room.online),
            metric("关注", room.attention),
        ]
        if room.short_id:
            metrics.append(metric("房间号", room.short_id))
        images = [
            url
            for url in (
                clean_cover(room.keyframe),
                clean_cover(room.user_cover),
                clean_cover(room.cover),
            )
            if url
        ]
        images = list(dict.fromkeys(images))
        contents = self.create_image_contents(images, headers=self.media_headers)
        text = "\n".join(
            part
            for part in (
                status,
                f"分区: {room.parent_area_name} | {room.area_name}"
                if room.area_name
                else None,
                room.description or None,
            )
            if part
        )
        await self.send_desc_common(
            room.title,
            author,
            contents,
            text=text,
            timestamp=parse_dynamic_time(room.live_time),
            url=f"https://live.bilibili.com/{room.room_id}",
            metrics=metrics,
        )
        return self.parse_result(contents)

    async def _parse_favorite_by_id(self, fav_id: int):
        payload = await self._get_json(
            self.API_FAVORITE,
            params={"media_id": fav_id, "pn": 1, "ps": 20},
            error="获取收藏夹失败",
        )
        try:
            fav = decode_favorite(payload)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("收藏夹内容不可用") from exc
        if not fav.medias:
            raise ParseException("收藏夹内容为空，或被风控")
        author = self.create_author(
            fav.info.upper.name,
            clean_cover(fav.info.upper.face),
            uid=str(fav.info.upper.mid),
            headers=self.media_headers,
        )
        contents: list[MediaContent] = []
        for media in fav.medias:
            if not media.cover:
                continue
            text = media.title
            if media.intro:
                text = f"{text}\n{media.intro}"
            contents.append(
                self.create_graphics_content(
                    clean_cover(media.cover) or media.cover,
                    text=text,
                    alt=media.url,
                    headers=self.media_headers,
                )
            )
        await self.send_desc_common(
            f"收藏夹 - {fav.info.title}",
            author,
            contents,
            text=fav.info.intro or None,
            timestamp=fav.info.ctime,
            url=(
                f"https://space.bilibili.com/{fav.info.upper.mid}"
                f"/favlist?fid={fav_id}"
            ),
            metrics=fav.metrics,
        )
        return self.parse_result(contents)

    async def _parse_bangumi_by_id(self, kind: str, ident: int):
        params = {f"{kind}_id": ident}
        payload = await self._get_json(
            self.API_BANGUMI,
            params=params,
            error="获取番剧信息失败",
        )
        try:
            bangumi = decode_bangumi(payload)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("番剧信息不可用") from exc
        author = self.create_author(
            bangumi.season_title,
            clean_cover(bangumi.square_cover),
            headers=self.media_headers,
        )
        contents = self.create_image_contents(
            [out for out in (clean_cover(bangumi.cover),) if out],
            headers=self.media_headers,
        )
        await self.send_desc_common(
            bangumi.title,
            author,
            contents,
            text=bangumi.evaluate or None,
            url=bangumi.share_url,
            metrics=bangumi.stat.metrics,
        )
        return self.parse_result(contents)

    async def _parse_article_by_id(self, read_id: int):
        payload = await self._get_json(
            self.ARTICLE_CARD_API,
            params={"id": read_id},
            error="获取专栏信息失败",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ParseException(str(payload.get("message") or "专栏不存在"))
        data = payload["data"]
        if not data.get("content") and not data.get("image_urls"):
            raise ParseException("专栏正文为空")
        author_data = (
            data.get("author") if isinstance(data.get("author"), dict) else {}
        )
        stat_data = (
            data.get("stats") if isinstance(data.get("stats"), dict) else {}
        )
        metrics = [
            metric("阅读", stat_data.get("view")),
            metric("点赞", stat_data.get("like")),
            metric("评论", stat_data.get("reply")),
            metric("收藏", stat_data.get("favorite")),
            metric("分享", stat_data.get("share")),
            metric("投币", stat_data.get("coin")),
        ]
        author = self.create_author(
            str(author_data.get("name") or "哔哩哔哩用户"),
            clean_cover(str(author_data.get("face") or "")),
            description=str(author_data.get("sign") or "") or None,
            uid=str(author_data.get("mid") or "") or None,
            headers=self.media_headers,
        )
        contents: list[MediaContent] = []
        for image in data.get("image_urls") or []:
            if isinstance(image, str) and is_allowed_media_host(image):
                contents.extend(
                    self.create_image_contents(
                        [normalize_url(image)], headers=self.media_headers
                    )
                )
        await self.send_desc_common(
            str(data.get("title") or "哔哩哔哩专栏"),
            author,
            contents,
            text=str(data.get("summary") or data.get("content") or "") or None,
            timestamp=int(data.get("publish_time") or 0) or None,
            url=f"https://www.bilibili.com/read/cv{read_id}",
            metrics=metrics,
        )
        return self.parse_result(contents)
