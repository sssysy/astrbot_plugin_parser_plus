from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import msgspec
from aiohttp import ClientError

from ...config import PluginConfig
from ...cookie import CookieJar
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, Platform, handle
from .models import AcfunVideoInfo
from .video import play_urls, quality_label, select_play_info


class AcfunParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="acfun", display_name="AcFun")

    INFO_URL: ClassVar[str] = "https://api-new.app.acfun.cn/rest/app/douga/info"
    MKEY: ClassVar[str] = (
        "AAHewK3eIAAyMjA2MDMyMjQAAhAAMEP1uwSG3TvhYAAAAO5fOOpIdKsH2h4IGsF6"
        "BlVwnGQA6_eLEvGiajzUp4_YthxOPC-hxcOpTk0SPSrxyhbdkmIwsXnF9PgS5ly8eQ"
        "yjuXlcS7VpWG0QlK0HakVDamteMHNHIui0A8V4tmELqQ=="
    )
    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Referer": "https://www.acfun.cn/",
                "Origin": "https://www.acfun.cn",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="acfun.cn")
        self._set_cookie_header()

    def _set_cookie_header(self) -> None:
        cookies = self.cookiejar.get_cookie_header_for_url(
            "https://www.acfun.cn/"
        )
        if cookies:
            self.headers["Cookie"] = cookies

    @handle(
        "acfun.cn/v",
        r"(?<![A-Za-z0-9.-])(?:https?://)?(?:www\.|m\.)?acfun\.cn/v/"
        r"(?:\?ac=|ac)(?P<acid>\d+)"
        r"(?:[?&][^\s#]*)?",
    )
    async def _parse(self, searched: re.Match[str]):
        acid = searched.group("acid")
        page_url = f"https://www.acfun.cn/v/ac{acid}"
        info = await self._fetch_video_info(acid)
        selected = select_play_info(
            info.current_video_info.play_infos,
            str(getattr(self.mycfg, "video_quality", "auto") or "auto"),
        )
        if selected is None:
            raise ParseException("AcFun 视频没有可用的播放地址")
        urls = play_urls(selected)
        if not urls:
            raise ParseException("AcFun 视频播放地址无效")

        author = self.create_author(
            info.user.name or "未知用户",
            info.user.head_url or None,
            uid=info.user.user_id or None,
            headers=self.headers,
        )
        await self.send_desc_video(
            info.title or None,
            author,
            cover=info.cover_url or None,
            duration=info.duration,
            quality=quality_label(selected),
            text=info.text or None,
            timestamp=info.timestamp,
            url=page_url,
            metrics=info.metrics,
            headers=self.headers,
        )

        content = self.create_video_content_by_loader(
            lambda: self._download_video(urls[0], urls[1:], acid),
            cover_url=info.cover_url or None,
            duration=info.duration,
            headers=self.headers,
        )
        return self.parse_result([content])

    async def _fetch_video_info(self, acid: str) -> AcfunVideoInfo:
        try:
            async with self.session.get(
                self.INFO_URL,
                params={"mkey": self.MKEY, "dougaId": acid},
                headers=self.headers,
                proxy=self.proxy,
            ) as response:
                if response.status >= 400:
                    raise ParseException("AcFun 视频信息请求失败")
                payload = await response.read()
        except (ClientError, TimeoutError) as exc:
            raise ParseException("AcFun 视频信息请求失败") from exc

        try:
            info = msgspec.json.decode(payload, type=AcfunVideoInfo)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            raise ParseException("AcFun 视频信息格式异常") from exc
        if not info.title and not info.current_video_info.play_infos:
            raise ParseException("AcFun 视频信息为空")
        return info

    async def _download_video(
        self,
        url: str,
        fallback_urls: list[str],
        acid: str,
    ) -> Path:
        return await self.downloader.download_m3u8_video(
            url,
            video_name=f"acfun_{acid}.mp4",
            headers=self.headers,
            proxy=self.proxy,
            fallback_urls=fallback_urls,
        )
