from __future__ import annotations

import re
from secrets import choice
from string import ascii_letters, digits
from typing import ClassVar
from urllib.parse import urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...data import Comment, MediaContent, Metric, Platform
from ...download import Downloader
from ...exception import ParseException
from ...utils import format_num
from ..base import BaseParser, handle
from .comment import decode_comments
from .state import Photo, decode_init_state


class KuaiShouParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="kuaishou", display_name="快手")

    INIT_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"window\.INIT_STATE\s*=\s*(.*?)</script>", re.DOTALL
    )
    COMMENT_API: ClassVar[str] = (
        "https://kph8gvfz.m.chenzhongtech.com/rest/wd/photo/comment/list"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.media_headers = self.ios_headers.copy()
        self.media_headers["Referer"] = "https://v.kuaishou.com/"

    @handle(
        "v.kuaishou",
        r"(?:(?:https?://)|^)(?P<host>v\.kuaishou\.com)/(?P<path>[A-Za-z0-9_-]+)(?:\?[^\s#]*)?",
    )
    @handle(
        "kuaishou",
        r"(?:(?:https?://)|^)(?P<host>(?:www\.)?kuaishou\.com)/(?:(?:f|short-video|fw/photo|photo)/(?P<path>[A-Za-z0-9_-]+))(?:\?[^\s#]*)?",
    )
    @handle(
        "chenzhongtech",
        r"(?:(?:https?://)|^)(?P<host>(?:v\.m\.)?chenzhongtech\.com)/fw/(?:photo|long-video)/(?P<path>[A-Za-z0-9_-]+)(?:\?[^\s#]*)?",
    )
    @handle(
        "gifshow",
        r"(?:(?:https?://)|^)(?P<host>m\.gifshow\.com)/fw/photo/(?P<path>[A-Za-z0-9_-]+)(?:\?[^\s#]*)?",
    )
    async def _parse(self, searched: re.Match[str]):
        url = self._matched_url(searched)
        if not self._is_supported_url(url):
            raise ParseException("不支持该快手链接")
        photo = await self._fetch_photo(url)
        contents = self._build_contents(photo)
        if not contents:
            raise ParseException("快手作品缺少可发送的媒体")

        author = self.create_author(
            photo.author_name,
            photo.head_url,
            uid=str(photo.user_id) if photo.user_id is not None else None,
            headers=self.media_headers,
        )
        comments = await self._fetch_comments(photo.photo_id)
        metrics = self._metrics(photo)
        timestamp = photo.timestamp // 1000 if photo.timestamp else None
        page_url = (
            f"https://m.gifshow.com/fw/photo/{photo.photo_id}"
            if photo.photo_id
            else url
        )
        video_urls = photo.video_urls
        cover_url = photo.cover_urls_list[0] if photo.cover_urls_list else None
        if video_urls:
            await self.send_desc_video(
                photo.caption or None,
                author,
                cover=cover_url,
                duration=photo.duration / 1000,
                text=photo.caption or None,
                timestamp=timestamp,
                url=page_url,
                metrics=metrics,
                comments=comments,
                headers=self.media_headers,
            )
        else:
            await self.send_desc_common(
                photo.caption or None,
                author,
                [],
                cover=cover_url,
                text=photo.caption or None,
                timestamp=timestamp,
                url=page_url,
                metrics=metrics,
                comments=comments,
            )
        return self.parse_result(contents)

    @staticmethod
    def _matched_url(searched: re.Match[str]) -> str:
        text = searched.group(0)
        if text.startswith(("http://", "https://")):
            return text
        return f"https://{text}"

    @staticmethod
    def _is_supported_url(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return (
            host == "v.kuaishou.com"
            or host in {"kuaishou.com", "www.kuaishou.com"}
            or host in {"chenzhongtech.com", "v.m.chenzhongtech.com"}
            or host == "m.gifshow.com"
        )

    async def _fetch_photo(self, url: str) -> Photo:
        host = (urlparse(url).hostname or "").lower()
        is_share_code = host in {"www.kuaishou.com", "kuaishou.com"} and (
            urlparse(url).path.startswith("/f/")
        )
        if is_share_code:
            final_url = await self.get_redirect_url(url, headers=self.media_headers)
        else:
            final_url = await self.get_final_url(url, headers=self.media_headers)
        final_url = final_url.replace("/fw/long-video/", "/fw/photo/")
        final_url = self._mobile_photo_url(final_url)
        host = (urlparse(final_url).hostname or "").lower()
        if not (
            host == "kuaishou.com"
            or host.endswith(".kuaishou.com")
            or host == "chenzhongtech.com"
            or host.endswith(".chenzhongtech.com")
            or host == "gifshow.com"
            or host.endswith(".gifshow.com")
        ):
            raise ParseException("快手短链跳转到了不受支持的域名")
        try:
            async with self.session.get(
                final_url,
                headers=self.media_headers,
            ) as response:
                if response.status >= 400:
                    raise ParseException(f"获取快手页面失败 {response.status}")
                html = await response.text()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("获取快手页面失败") from exc
        matched = self.INIT_PATTERN.search(html)
        if matched is None:
            raise ParseException("快手页面缺少作品数据")
        try:
            return decode_init_state(matched.group(1).strip())
        except (msgspec.DecodeError, ValueError) as exc:
            raise ParseException("快手作品数据格式异常") from exc

    def _build_contents(self, photo: Photo) -> list[MediaContent]:
        video_urls = photo.video_urls
        if video_urls:
            return [
                self.create_video_content(
                    video_urls[0],
                    cover_url=photo.cover_urls_list[0]
                    if photo.cover_urls_list
                    else None,
                    duration=photo.duration / 1000,
                    headers=self.media_headers,
                    fallback_urls=video_urls[1:],
                )
            ]
        image_urls = photo.image_urls
        if image_urls:
            return self.create_image_contents(image_urls, headers=self.media_headers)
        return []

    @staticmethod
    def _mobile_photo_url(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if host in {"www.kuaishou.com", "kuaishou.com"} and len(path_parts) >= 2:
            if path_parts[0] in {"short-video", "photo"}:
                photo_id = path_parts[1]
            else:
                photo_id = ""
            if photo_id:
                return f"https://m.gifshow.com/fw/photo/{photo_id}"
        return url

    async def _fetch_comments(self, photo_id: str) -> list[Comment]:
        limit = int(getattr(self.mycfg, "max_comments", 0) or 0)
        if limit <= 0 or not photo_id:
            return []
        headers = self.media_headers.copy()
        headers["Referer"] = f"https://m.gifshow.com/fw/photo/{photo_id}"
        did = "web_" + "".join(choice(ascii_letters + digits) for _ in range(32))
        existing_cookie = headers.get("Cookie")
        headers["Cookie"] = "; ".join(
            part for part in (existing_cookie, f"did={did}") if part
        )
        try:
            async with self.session.post(
                self.COMMENT_API,
                json={"photoId": photo_id, "count": limit},
                headers=headers,
            ) as response:
                if response.status >= 400:
                    raise ClientError(f"HTTP {response.status}")
                payload = await response.read()
            return decode_comments(
                payload,
                limit=limit,
                avatar_factory=lambda url: self.downloader.download_img(
                    url,
                    headers=self.media_headers,
                    proxy=self.proxy,
                ),
            )
        except Exception as exc:
            logger.warning(
                f"[快手] 获取评论失败 | photo_id={photo_id} | "
                f"error_type={type(exc).__name__}"
            )
            return []

    @staticmethod
    def _metrics(photo: Photo) -> list[Metric]:
        values = (
            ("播放", photo.view_count),
            ("点赞", photo.like_count),
            ("评论", photo.comment_count),
            ("分享", photo.share_count),
        )
        return [
            Metric(icon=None, label=label, value=format_num(value))
            for label, value in values
            if value
        ]
