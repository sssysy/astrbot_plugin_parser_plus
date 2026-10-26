import re
from typing import ClassVar
from urllib.parse import urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import Author, MediaContent, Metric
from ...utils import format_num
from ..base import BaseParser, Downloader, ParseException, Platform, handle
from .comment import Comment, decode_comments
from .live import Room
from .video import Aweme, SelectedVideo, decode_aweme, select_video


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")
    TTWID_REGISTER_URL: ClassVar[str] = (
        "https://ttwid.bytedance.com/ttwid/union/register/"
    )
    DETAIL_URL: ClassVar[str] = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    COMMENT_URL: ClassVar[str] = "https://www.douyin.com/aweme/v1/web/comment/list/"
    LIVE_URL: ClassVar[str] = "https://live.douyin.com/webcast/room/web/enter/"
    WEB_RID_RE: ClassVar[re.Pattern[str]] = re.compile(
        r'\\"webRid\\":\\"(\d+)\\"'
    )
    ROUTER_DATA_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
        flags=re.DOTALL,
    )
    RETRYABLE_STATUSES: ClassVar[frozenset[int]] = frozenset({460})

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.cookiejar = CookieJar(config, self.mycfg, domain="douyin.com")
        self._set_cookies()

    @property
    def media_headers(self) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        headers["Referer"] = "https://www.douyin.com/"
        return headers

    def _set_cookies(self, cookies_str: str = "") -> None:
        cookies_str = cookies_str or self.cookiejar.cookies_str
        if cookies_str:
            self.ios_headers["Cookie"] = cookies_str
            self.android_headers["Cookie"] = cookies_str

    def _sync_headers_for_url(self, url: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        if cookies_str := self.cookiejar.get_cookie_header_for_url(url):
            headers["Cookie"] = cookies_str
        elif self._is_iesdouyin_url(url):
            if cookies_str := self.cookiejar.get_cookie_header(domain="iesdouyin.com"):
                headers["Cookie"] = cookies_str
        return headers

    @staticmethod
    def _is_iesdouyin_url(url: str) -> bool:
        hostname = urlparse(url).hostname or ""
        return hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com")

    def _has_ttwid(self) -> bool:
        return any(
            cookie.name == "ttwid" and not cookie.is_expired()
            for cookie in self.cookiejar.cookies
        )

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("webcast.amemv.com", r"douyin/webcast/reflow/(?P<room_id>\d+)")
    async def _parse_live_by_room_id(self, searched: re.Match[str]):
        room_id = searched.group("room_id")
        await self.ensure_ttwid()
        headers = self._sync_headers_for_url("https://webcast.amemv.com/")
        async with self.session.get(
            f"https://webcast.amemv.com/douyin/webcast/reflow/{room_id}",
            headers=headers,
        ) as resp:
            if resp.status >= 400:
                raise ParseException("抖音直播页访问失败")
            html = await resp.text()
        if matched := self.WEB_RID_RE.search(html):
            return await self._parse_live(matched.group(1))
        raise ParseException("提取抖音直播间 ID 失败")

    @handle("live.douyin.com", r"live\.douyin\.com/(?P<rid>\d+)")
    async def _parse_live(self, searched: re.Match[str] | str):
        rid = searched if isinstance(searched, str) else searched.group("rid")
        await self.ensure_ttwid()
        headers = self._sync_headers_for_url("https://live.douyin.com/")
        headers["Referer"] = f"https://live.douyin.com/{rid}"
        params = {
            "aid": 6383,
            "device_platform": "web",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "95.0.4638.69",
            "web_rid": rid,
        }
        async with self.session.get(
            self.LIVE_URL, params=params, headers=headers
        ) as resp:
            if resp.status >= 400:
                raise ParseException("解析抖音直播失败，可能直播间不存在")
            payload = await resp.json(content_type=None)
        rooms = (
            (payload.get("data") or {}).get("data")
            if isinstance(payload, dict)
            else None
        )
        if not rooms:
            raise ParseException("获取抖音直播间信息失败")
        room = msgspec.convert(rooms[0], type=Room)
        cover_url = room.cover.url_list[-1] if room.cover.url_list else None
        author = self.create_author(
            room.owner.nickname,
            room.owner.avatar_thumb.url_list[-1]
            if room.owner.avatar_thumb.url_list
            else None,
            uid=room.owner.id_str,
            headers=self.media_headers,
        )
        metrics = [
            Metric(
                icon=None,
                label="观看",
                value=format_num(int(room.room_view_stats.display_value or 0)),
            ),
            Metric(
                icon=None,
                label="点赞",
                value=format_num(int(room.like_count or 0)),
            ),
        ]
        contents: list[MediaContent] = []
        if cover_url:
            contents.extend(
                self.create_image_contents([cover_url], headers=self.media_headers)
            )
        status = "直播中" if room.status == 2 else "未开播"
        if not room.title:
            room.title = status
        await self.send_desc_common(
            room.title,
            author,
            contents,
            text=status,
            url=f"https://live.douyin.com/{rid}",
            metrics=metrics,
        )
        return self.parse_result(contents)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("aweme_id", r"aweme_id[=:/\s]+(?P<vid>\d{10,})")
    @handle("modal_id", r"modal_id=(?P<vid>\d{10,})")
    @handle("aweme", r"aweme/(?P<vid>\d{10,})")
    @handle("douyin", r"douyin\.com/(?P<ty>video|note|article)/(?P<vid>\d+)")
    @handle(
        "iesdouyin",
        r"iesdouyin\.com/share/(?P<ty>slides|video|note|article)/(?P<vid>\d+)",
    )
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        ty = searched.groupdict().get("ty") or "video"
        vid = searched.group("vid")
        logger.debug(f"[抖音] 解析类型: {ty}, ID: {vid}")
        aweme = await self.fetch_aweme(vid, ty)
        return await self.parse_aweme(aweme)

    async def ensure_ttwid(self) -> None:
        if self._has_ttwid():
            return
        headers = self.ios_headers.copy()
        headers.update(
            {
                "Content-Type": "application/json",
                "Referer": "https://www.douyin.com/",
            }
        )
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.douyin.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https",
            "union": True,
        }
        try:
            async with self.session.post(
                self.TTWID_REGISTER_URL, json=payload, headers=headers
            ) as resp:
                if resp.status >= 400:
                    raise ParseException("抖音 ttwid 注册失败")
                self.cookiejar.update_from_response(
                    resp.headers.getall("Set-Cookie", []),
                    request_url=self.TTWID_REGISTER_URL,
                )
                self._set_cookies()
                body = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as exc:
            raise ParseException("抖音 ttwid 注册失败") from exc
        callback_url = body.get("redirect_url") if isinstance(body, dict) else None
        if callback_url:
            callback_headers = self._sync_headers_for_url(callback_url)
            register_cookie = self.cookiejar.get_cookie_header(
                domain="ttwid.bytedance.com"
            )
            if register_cookie:
                existing_cookie = callback_headers.get("Cookie")
                callback_headers["Cookie"] = "; ".join(
                    part for part in (register_cookie, existing_cookie) if part
                )
            callback_headers["Referer"] = "https://www.douyin.com/"
            try:
                async with self.session.get(
                    callback_url, headers=callback_headers, allow_redirects=False
                ) as resp:
                    if resp.status >= 400:
                        raise ParseException("抖音 ttwid 回调失败")
                    self.cookiejar.update_from_response(
                        resp.headers.getall("Set-Cookie", []),
                        request_url=callback_url,
                    )
                    self._set_cookies()
            except (ClientError, TimeoutError) as exc:
                raise ParseException("抖音 ttwid 回调失败") from exc
        if not self._has_ttwid():
            raise ParseException(
                "抖音 ttwid 注册未返回 iesdouyin.com Cookie；"
                "请检查网络代理、Cookie 配置或稍后重试"
            )

    async def parse_with_redirect(self, url: str) -> list[MediaContent]:
        headers = self._sync_headers_for_url(url)
        try:
            async with self.session.get(
                url, headers=headers, allow_redirects=False
            ) as resp:
                self.cookiejar.update_from_response(
                    resp.headers.getall("Set-Cookie", [])
                )
                self._set_cookies()
                redirect_url = (
                    resp.headers.get("Location", url)
                    if resp.status in (301, 302, 303, 307, 308)
                    else url
                )
        except (ClientError, TimeoutError) as exc:
            raise ParseException("抖音短链重定向失败") from exc
        if redirect_url == url:
            raise ParseException("无法重定向抖音短链")
        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def fetch_aweme(self, aweme_id: str, ty: str = "video") -> Aweme:
        try:
            return await self.fetch_detail(aweme_id)
        except ParseException as exc:
            logger.warning(f"[抖音] 详情接口失败，回退分享页: {exc}")
        return await self.fetch_share_page(aweme_id, ty)

    async def fetch_detail(self, aweme_id: str) -> Aweme:
        await self.ensure_ttwid()
        headers = self.media_headers
        headers["Origin"] = "https://open.douyin.com"
        headers["Referer"] = "https://open.douyin.com/"
        try:
            async with self.session.get(
                self.DETAIL_URL,
                params={"aweme_id": aweme_id, "aid": "6383"},
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"抖音详情接口状态码 {resp.status}")
                payload = await resp.read()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("抖音详情接口请求失败") from exc
        try:
            return decode_aweme(payload, detail=True)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("抖音详情接口返回数据异常") from exc

    async def fetch_share_page(self, aweme_id: str, ty: str = "video") -> Aweme:
        await self.ensure_ttwid()
        share_url = f"https://www.iesdouyin.com/share/{ty}/{aweme_id}/"
        headers = self._sync_headers_for_url(share_url)
        try:
            async with self.session.get(
                share_url, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    raise ParseException(f"抖音分享页状态码 {resp.status}")
                text = await resp.text()
                self.cookiejar.update_from_response(
                    resp.headers.getall("Set-Cookie", [])
                )
                self._set_cookies()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("抖音分享页请求失败") from exc
        matched = self.ROUTER_DATA_RE.search(text)
        if not matched or not matched.group(1):
            raise ParseException("抖音分享页缺少 _ROUTER_DATA")
        try:
            return decode_aweme(matched.group(1).strip(), detail=False)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("抖音分享页返回数据异常") from exc

    async def fetch_comments(
        self,
        aweme_id: str,
        limit: int = 5,
        cursor: int = 0,
    ) -> list[Comment]:
        """评论接口预留：主解析不调用，后续评论卡片直接复用。"""
        if limit <= 0:
            return []
        await self.ensure_ttwid()
        headers = self.media_headers
        try:
            async with self.session.get(
                self.COMMENT_URL,
                params={
                    "device_platform": "webapp",
                    "aid": 6383,
                    "channel": "channel_pc_web",
                    "aweme_id": aweme_id,
                    "cursor": cursor,
                    "count": limit,
                    "msToken": "",
                    "X-Bogus": "",
                },
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"抖音评论接口状态码 {resp.status}")
                payload = await resp.read()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("抖音评论接口请求失败") from exc
        try:
            return decode_comments(payload)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ParseException("抖音评论接口返回数据异常") from exc

    async def parse_aweme(self, aweme: Aweme):
        selected = (
            select_video(
                aweme, getattr(self.mycfg, "video_quality", "auto") or "auto"
            )
            if aweme.is_video
            else None
        )
        contents = await self.build_contents(aweme, selected)
        author = self.build_author(aweme)
        if aweme.is_article:
            article = aweme.article_info
            await self.send_desc_common(
                article.article_title if article else aweme.display_text,
                author,
                contents,
                text=aweme.body_text,
                timestamp=aweme.create_time,
                url=aweme.short_url,
                metrics=aweme.metrics,
            )
        elif aweme.is_video and selected is not None and aweme.video is not None:
            await self.send_desc_video(
                aweme.display_text or None,
                author,
                cover=aweme.cover_url,
                duration=aweme.video.duration / 1000,
                quality=selected.quality,
                resolution=selected.resolution,
                text=aweme.text,
                timestamp=aweme.create_time,
                url=aweme.short_url,
                metrics=aweme.metrics,
                headers=self.media_headers,
            )
        else:
            await self.send_desc_common(
                aweme.display_text or None,
                author,
                contents,
                text=aweme.text,
                timestamp=aweme.create_time,
                url=aweme.short_url,
                metrics=aweme.metrics,
            )
        return self.parse_result(contents)

    def build_author(self, aweme: Aweme) -> Author:
        return self.create_author(
            aweme.author.nickname or "抖音用户",
            aweme.avatar_url,
            description=aweme.author.signature,
            uid=aweme.author_id,
            metrics=aweme.author_metrics,
            headers=self.media_headers,
        )

    async def build_contents(
        self, aweme: Aweme, selected: SelectedVideo | None
    ) -> list[MediaContent]:
        contents: list[MediaContent] = []
        headers = self.media_headers
        if aweme.images:
            for image in aweme.images:
                if image.clip_type in (2, None) and image.url_list:
                    contents.extend(
                        self.create_image_contents([image.url_list[0]], headers=headers)
                    )
                    continue
                if image.clip_type == 4:
                    if image.video and image.video.play_addr.url_list:
                        contents.append(
                            self.create_video_content(
                                image.video.play_addr.url_list[0],
                                cover_url=image.url_list[0]
                                if image.url_list
                                else None,
                                duration=image.video.duration / 1000,
                                headers=headers,
                                fallback_urls=image.video.play_addr.url_list[1:],
                                retry_http_statuses=self.RETRYABLE_STATUSES,
                            )
                        )
                    continue
                if (
                    image.clip_type == 5
                    and image.video
                    and image.video.play_addr.url_list
                ):
                    image_url = (
                        image.url_list[0] if image.url_list else aweme.cover_url
                    )
                    if image_url:
                        contents.append(
                            self.create_live_photo_content(
                                image.video.play_addr.url_list[0],
                                image_url,
                                cover_url=image_url,
                                bgm_url=aweme.music_url,
                                headers=headers,
                                fallback_urls=image.video.play_addr.url_list[1:],
                                retry_http_statuses=self.RETRYABLE_STATUSES,
                            )
                        )
            return contents
        if selected is not None:
            contents.append(
                self.create_video_content(
                    selected.url,
                    cover_url=aweme.cover_url,
                    duration=aweme.video.duration / 1000 if aweme.video else 0,
                    headers=headers,
                    fallback_urls=selected.fallback_urls,
                    retry_http_statuses=self.RETRYABLE_STATUSES,
                )
            )
            return contents
        return contents
