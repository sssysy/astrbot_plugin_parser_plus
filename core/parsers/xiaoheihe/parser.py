from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from ...config import PluginConfig
from ...data import MediaContent, Platform, TextContent
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .content import PostContent, build_post_content
from .game import (
    GameContent,
    build_game_content_from_info,
    canonical_game_url,
)
from .request import XiaoheiheClient


class XiaoheiheParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xiaoheihe", display_name="小黑盒")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "referer": "https://www.xiaoheihe.cn/",
                "origin": "https://www.xiaoheihe.cn",
            }
        )
        cookies = getattr(self.mycfg, "cookies", "") or ""
        if cookies:
            self.headers["cookie"] = cookies
        self.client = XiaoheiheClient(
            headers=self.headers,
            proxy=self.proxy,
            timeout=int(getattr(self.cfg, "common_timeout", 30) or 30),
            retries=int(getattr(self.cfg, "download_retry_times", 2) or 2),
        )

    async def close_session(self) -> None:
        await self.client.close()
        await super().close_session()

    @handle(
        "xiaoheihe.cn/app/bbs/link",
        r"xiaoheihe\.cn/app/bbs/link/(?P<link_id>[0-9a-z]+)",
    )
    @handle(
        "xiaoheihe.cn/bbs/post_share",
        r"xiaoheihe\.cn/bbs/post_share\?[A-Za-z0-9._%&+=/#@?;-]*link_id=(?P<link_id>[0-9a-z]+)",
    )
    @handle(
        "api.xiaoheihe.cn/v3/bbs/app/api/web/share",
        r"api\.xiaoheihe\.cn/v3/bbs/app/api/web/share\?[A-Za-z0-9._%&+=/#@?;-]*link_id=(?P<link_id>[0-9a-z]+)",
    )
    async def _parse_post(self, searched: re.Match[str]):
        link_id = searched.group("link_id")
        result = await self.client.fetch_link_tree(link_id)
        link = result.get("link")
        if not isinstance(link, dict):
            raise ParseException("小黑盒帖子数据为空")
        content = build_post_content(
            link,
            original_image=bool(getattr(self.mycfg, "use_original_image", True)),
        )
        return await self._finish_post(content, link_id)

    @handle(
        "xiaoheihe.cn/app/topic/game",
        r"xiaoheihe\.cn/app/topic/game/(?P<game_type>[a-z]+)/(?P<appid>[0-9a-z]+)",
    )
    @handle(
        "api.xiaoheihe.cn/game/share_game_detail",
        r"api\.xiaoheihe\.cn/game/share_game_detail\?[A-Za-z0-9._%&+=/#@?;-]*appid=(?P<appid>[0-9a-z]+)[A-Za-z0-9._%&+=/#@?;-]*game_type=(?P<game_type>[a-z]+)",
    )
    async def _parse_game(self, searched: re.Match[str]):
        appid = searched.group("appid").strip()
        game_type = searched.group("game_type").strip().lower() or "pc"
        if not appid:
            raise ParseException("无效的小黑盒游戏 appid")
        url = canonical_game_url(appid, game_type)
        game = await self.client.fetch_game_info(appid)
        intro: dict = {}
        steam_appid = _steam_appid(game, appid)
        if steam_appid:
            intro = await self.client.fetch_game_intro(steam_appid)
        videos: list[tuple[str, str | None]] = []
        content = build_game_content_from_info(
            game, appid, game_type, intro, videos
        )
        return await self._finish_game(content, url)

    async def _finish_post(self, content: PostContent, link_id: str):
        url = f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"
        show_text = bool(getattr(self.mycfg, "show_body_text", True))
        mixed = getattr(self.mycfg, "mixed_layout", True) is not False
        contents: list[MediaContent] = []
        if mixed:
            for kind, value, extra in content.segments:
                if kind == "text":
                    if show_text:
                        contents.append(TextContent(value))
                elif kind == "live":
                    contents.append(
                        self.create_live_photo_content(
                            extra,
                            value,
                            cover_url=value,
                            headers=self.headers,
                        )
                    )
                else:
                    contents.extend(self.create_image_contents([value], headers=self.headers))
            if content.video_url:
                contents.append(self._video_content(content.video_url, content.video_cover))
        else:
            for kind, value, extra in content.segments:
                if kind == "image":
                    contents.extend(self.create_image_contents([value], headers=self.headers))
                elif kind == "live":
                    contents.append(
                        self.create_live_photo_content(
                            extra,
                            value,
                            cover_url=value,
                            headers=self.headers,
                        )
                    )
            if content.video_url:
                contents.append(self._video_content(content.video_url, content.video_cover))
            if show_text:
                for kind, value, _ in content.segments:
                    if kind == "text":
                        contents.append(TextContent(value))
        cover = (
            content.video_cover
            or next(iter(content.image_urls), None)
            or (content.live_entries[0][1] if content.live_entries else None)
        )
        info: list[str] = []
        if content.image_urls:
            info.append(f"正文图片 {len(content.image_urls)} 张")
        if content.live_entries:
            info.append(f"实况图 {len(content.live_entries)} 个")
        if content.video_url:
            info.append("正文视频 1 个")
        author = self.create_author(
            content.author_name,
            content.author_avatar,
            description=content.author_description,
            uid=content.author_uid,
            headers=self.headers,
        )
        await self.send_desc_common(
            content.title,
            author,
            contents,
            text=None if show_text else (content.body_text or None),
            timestamp=content.timestamp,
            url=url,
            extra={"info": "，".join(info)} if info else None,
            metrics=content.metrics,
            cover=cover,
        )
        return self.parse_result(contents)

    async def _finish_game(self, content: GameContent, url: str):
        show_text = bool(getattr(self.mycfg, "show_body_text", True))
        contents: list[MediaContent] = []
        contents.extend(
            self.create_image_contents(content.image_urls, headers=self.headers)
        )
        for video_url, cover in content.video_urls:
            contents.append(self._video_content(video_url, cover))
        if show_text and content.description:
            contents.append(TextContent(content.description))
        info: list[str] = []
        if content.image_urls:
            info.append(f"游戏图片 {len(content.image_urls)} 张")
        if content.video_urls:
            info.append(f"游戏视频 {len(content.video_urls)} 个")
        await self.send_desc_common(
            content.title,
            None,
            contents,
            text=None if show_text else (content.description or None),
            url=url,
            extra={
                "info": "，".join(info) if info else None,
                "game_appid": content.appid,
                "steam_appid": content.steam_appid,
                "game_type": content.game_type,
            },
            cover=next(iter(content.image_urls), None),
        )
        return self.parse_result(contents)

    def _video_content(self, url: str, cover: str | None):
        path = (urlparse(url).path or "").lower()
        if path.endswith(".m3u8"):
            async def load_video() -> Path:
                return await self.downloader.ytdlp_download_video_relaxed(
                    url,
                    headers=self.headers,
                    proxy=self.proxy,
                )

            return self.create_video_content_by_loader(
                load_video,
                cover_url=cover,
                headers=self.headers,
            )
        return self.create_video_content(
            url,
            cover_url=cover,
            headers=self.headers,
        )


def _steam_appid(game: dict, fallback: str) -> int | None:
    try:
        return int(str(game.get("steam_appid") or fallback))
    except (TypeError, ValueError):
        return None
