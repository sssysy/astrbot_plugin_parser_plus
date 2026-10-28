from __future__ import annotations

import re
from typing import ClassVar

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import MediaContent, Platform, TextContent
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .content import parse_post_html
from .request import NgaRequest


class NGAParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="nga", display_name="NGA")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://bbs.nga.cn/",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="nga.cn")
        self.request = NgaRequest(
            session=self.session,
            cookiejar=self.cookiejar,
            headers=self.headers,
            timeout=int(getattr(config, "common_timeout", 30) or 30),
            retries=int(getattr(config, "download_retry_times", 2) or 2),
        )

    @handle(
        "bbs.nga.cn/read.php",
        r"bbs\.nga\.cn/read\.php\?(?P<query>[^\s#]+)",
    )
    @handle(
        "nga.178.com/read.php",
        r"nga\.178\.com/read\.php\?(?P<query>[^\s#]+)",
    )
    @handle(
        "ngabbs.com/read.php",
        r"ngabbs\.com/read\.php\?(?P<query>[^\s#]+)",
    )
    async def _parse_post(self, searched: re.Match[str]) -> list[MediaContent]:
        source_url = f"https://{searched.group(0)}"
        tid, pid = self.request.parse_identifiers(source_url)
        if not bool(getattr(self.mycfg, "include_floor", True)):
            pid = None
        url = self.request.canonical_url(tid, pid)
        response = await self.request.get(url)
        post = parse_post_html(response.text, page_url=url, pid=pid)
        if not post.segments and not post.title:
            raise ParseException("NGA 页面未找到帖子内容")

        contents: list[MediaContent] = []
        show_text = bool(getattr(self.mycfg, "show_body_text", True))
        mixed_layout = bool(getattr(self.mycfg, "mixed_layout", True))
        image_urls = [value for kind, value in post.segments if kind == "image"]
        text_parts = [value for kind, value in post.segments if kind == "text"]
        if mixed_layout:
            for kind, value in post.segments:
                if kind == "image":
                    contents.extend(
                        self.create_image_contents(
                            [value],
                            headers=self.headers,
                        )
                    )
                elif show_text:
                    contents.append(TextContent(value))
        else:
            contents.extend(
                self.create_image_contents(image_urls, headers=self.headers)
            )
            if show_text and text_parts:
                contents.append(TextContent("\n\n".join(text_parts)))

        author = (
            self.create_author(
                post.author,
                uid=post.author_uid,
                headers=self.headers,
            )
            if post.author
            else None
        )
        extra: dict[str, str] = {}
        if post.floor_label:
            extra["info"] = post.floor_label
        if image_urls:
            image_info = f"正文图片 {len(image_urls)} 张"
            extra["info"] = (
                f"{extra['info']}，{image_info}" if extra.get("info") else image_info
            )
        await self.send_desc_common(
            post.title,
            author,
            contents,
            text=None if show_text else (post.body_text or None),
            timestamp=post.timestamp,
            url=url,
            extra=extra or None,
            cover=image_urls[0] if image_urls else None,
        )
        return self.parse_result(contents)
