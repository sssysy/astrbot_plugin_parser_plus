from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from ...config import PluginConfig
from ...data import (
    Comment,
    ContentItem,
    ImageContent,
    MediaContent,
    Platform,
    TextContent,
)
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .content import (
    IMAGE_HOSTS,
    CommentInfo,
    Fragment,
    build_metrics,
    decode_page,
    normalize_media_url,
)
from .request import TiebaClient

_REFERER = "https://tieba.baidu.com/"


def comment_limit(value: object) -> int:
    try:
        return max(0, min(int(value), 20))
    except (TypeError, ValueError):
        return 5


class TiebaParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="tieba",
        display_name="百度贴吧",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": _REFERER})
        self.client = TiebaClient(
            session_provider=lambda: self.session,
            proxy=self.proxy,
            timeout=float(getattr(self.cfg, "common_timeout", 30) or 30),
            retries=int(getattr(self.cfg, "download_retry_times", 2) or 2),
        )

    @handle("tieba.baidu.com/p/", r"tieba\.baidu\.com/p/(?P<post_id>\d+)")
    async def _parse_post(self, searched: re.Match[str]):
        post_id = int(searched.group("post_id"))
        result = await self.client.fetch_page(post_id)
        info = decode_page(
            result,
            comment_limit=comment_limit(getattr(self.mycfg, "comment_limit", 5))
            if bool(getattr(self.mycfg, "include_comments", True))
            else 0,
        )
        if not info.author.name:
            raise ParseException("贴吧帖子数据为空")

        contents = self._contents(info.contents)
        comments = self._comments(info.comments)
        author = self.create_author(
            info.author.name,
            avatar_url=info.author.avatar_url or None,
            uid=str(info.author.user_id or "") or None,
            headers={**self.headers, "Referer": _REFERER},
        )
        card_contents = contents
        cover = self._first_image_url(info.contents)
        await self.send_desc_common(
            info.title,
            author,
            card_contents,
            timestamp=info.timestamp,
            url=f"https://tieba.baidu.com/p/{info.tid or post_id}",
            extra=({"info": f"贴吧：{info.forum_name}"} if info.forum_name else None),
            metrics=build_metrics(info),
            comments=comments,
            cover=cover,
        )
        return self.parse_result(contents)

    def _contents(self, fragments: list[Fragment]) -> list[MediaContent]:
        output: list[MediaContent] = []
        for item in fragments:
            if item.kind == "text":
                if item.text:
                    output.append(TextContent(item.text))
                continue
            if item.kind == "emoji":
                if item.text:
                    output.append(TextContent(item.text))
                continue
            if item.kind == "link":
                text = item.text or item.url
                if text:
                    output.append(TextContent(text))
                continue
            if item.kind == "image" and item.image is not None:
                url = normalize_media_url(item.image.url, hosts=IMAGE_HOSTS)
                if url:
                    output.extend(
                        self.create_image_contents([url], headers=self.headers)
                    )
                continue
            if item.kind == "video" and item.video is not None:
                output.append(
                    self.create_video_content(
                        item.video.url,
                        cover_url=item.video.cover_url or None,
                        duration=item.video.duration,
                        headers=self.headers,
                    )
                )
        return output

    def _comments(self, comments: list[CommentInfo]) -> list[Comment]:
        output: list[Comment] = []
        for item in comments:
            author = self.create_author(
                item.user.name,
                avatar_url=item.user.avatar_url or None,
                uid=str(item.user.user_id or "") or None,
                headers=self.headers,
            )
            replies = [
                Comment(
                    author=self.create_author(
                        reply.user.name,
                        avatar_url=reply.user.avatar_url or None,
                        uid=str(reply.user.user_id or "") or None,
                        headers=self.headers,
                    ),
                    contents=self._comment_contents(reply.contents),
                    timestamp=reply.timestamp,
                    location=reply.user.ip or None,
                    like_count=reply.like_count or None,
                    parent_author=author,
                )
                for reply in item.replies[:3]
            ]
            output.append(
                Comment(
                    author=author,
                    contents=self._comment_contents(item.contents),
                    timestamp=item.timestamp,
                    location=item.user.ip or None,
                    like_count=item.like_count or None,
                    reply_count=item.reply_count or None,
                    replies=replies,
                )
            )
        return output

    def _comment_contents(self, fragments: list[Fragment]) -> list[ContentItem]:
        output: list[ContentItem] = []
        for item in fragments:
            if item.kind in {"text", "emoji", "link"}:
                text = item.text or item.url
                if text:
                    output.append(text)
                continue
            if item.kind == "image" and item.image is not None:
                url = normalize_media_url(item.image.url, hosts=IMAGE_HOSTS)
                if url:
                    output.append(self._image_content(url))
        return output

    def _image_content(self, url: str) -> ImageContent:
        content = ImageContent()

        async def load() -> Path:
            return await self.downloader.download_img(
                url,
                headers=self.headers,
                proxy=self.proxy,
            )

        content.set_loader(load)
        return content

    @staticmethod
    def _first_image_url(fragments: list[Fragment]) -> str | None:
        for item in fragments:
            if item.kind == "image" and item.image is not None:
                url = normalize_media_url(item.image.url, hosts=IMAGE_HOSTS)
                if url:
                    return url
        return None
