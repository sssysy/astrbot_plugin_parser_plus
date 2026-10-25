import re
from typing import ClassVar

import aiohttp
from yt_dlp.utils import DownloadError, UnsupportedError

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Author, Platform
from ..download import Downloader
from .base import BaseParser, handle


class TikTokParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="tiktok", display_name="TikTok")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {"Origin": "https://www.tiktok.com", "Referer": "https://www.tiktok.com/"}
        )
        self.mycfg = config.parser.tiktok
        self.cookiejar = CookieJar(config, self.mycfg, "tiktok.com")

    async def _parse_photo_tikwm(self, url: str):
        """通过 TikWM API 专门解析 TikTok 的图文/照片集"""
        api_url = f"https://www.tikwm.com/api/?url={url}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, proxy=self.proxy) as resp:
                    res = await resp.json()
                    if res.get("code") == 0 and "data" in res:
                        data = res["data"]
                        title = data.get("title", "TikTok 图文")
                        author_name = (
                            data.get("author", {}).get("nickname")
                            or data.get("author", {}).get("unique_id")
                            or "TikTok User"
                        )
                        timestamp = data.get("create_time", 0)

                        # 提取图文中的所有图片地址
                        images = data.get("images", [])
                        if images:
                            contents = self.create_image_contents(images)
                            author = Author(name=author_name)
                            await self.send_desc_common(
                                title,
                                author,
                                contents,
                                timestamp=timestamp,
                                url=url,
                            )
                            return self.parse_result(contents)
        except Exception:
            pass
        return None

    @handle("tiktok.com", r"(www|vt|vm)\.tiktok\.com/[A-Za-z0-9._?%&+\-=/#@]*")
    async def _parse(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url, prefix = searched.group(0), searched.group(1)

        if not url.startswith("http"):
            url = f"https://{url}"

        # 修复点：只要是 vt/vm 短链或包含 /t/ 路径的分享短链，一律先还原为最终跳转后的长链接
        if prefix in ("vt", "vm") or "/t/" in url:
            redirected_url = await self.get_redirect_url(url)
            if redirected_url:
                url = redirected_url

        # 1. 跳转后的实际链接若包含 /photo/，使用图文接口解析
        if "/photo/" in url:
            photo_result = await self._parse_photo_tikwm(url)
            if photo_result:
                return photo_result

        # 2. 普通视频解析逻辑
        try:
            video_info = await self.downloader.ytdlp_extract_info(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
            )

            author = Author(name=video_info.channel)
            await self.send_desc_video(
                video_info.title,
                author,
                cover=video_info.thumbnail,
                duration=video_info.duration,
                timestamp=video_info.timestamp,
                url=url,
            )
            video = self.downloader.ytdlp_download_video(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
                format="best",
            )
            content = self.create_video_content(
                video,
                video_info.thumbnail,
                duration=video_info.duration,
            )
            return self.parse_result([content])

        except (DownloadError, UnsupportedError, Exception):
            # 3. 如果 yt-dlp 提取失败，降级尝试图文解析兜底
            photo_result = await self._parse_photo_tikwm(url)
            if photo_result:
                return photo_result
            raise
