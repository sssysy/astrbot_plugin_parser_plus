from random import choice
from typing import Any
from urllib.parse import parse_qs, urlparse

import msgspec
from msgspec import Struct, field

from ...data import Metric
from ..base import ParseException


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    uri: str | None = None
    url_list: list[str] = field(default_factory=list)


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Statistics(Struct):
    digg_count: int | str = 0
    comment_count: int | str = 0
    collect_count: int | str = 0
    share_count: int | str = 0


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None
    statistics: Statistics | None = None

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images] if self.images else []

    @property
    def video_url(self) -> str | None:
        if not self.video or not self.video.play_addr.url_list:
            return None
        return choice(self.video.play_addr.url_list).replace("playwm", "play")

    @property
    def play_token(self) -> str | None:
        if not self.video:
            return None

        play_addr = self.video.play_addr
        if play_addr.uri:
            return play_addr.uri

        for url in play_addr.url_list:
            query = parse_qs(urlparse(url).query)
            if video_id := query.get("video_id"):
                return video_id[0]
        return None

    @property
    def cover_url(self) -> str | None:
        return choice(self.video.cover.url_list) if self.video else None

    @property
    def duration_seconds(self) -> float:
        """抖音接口的 duration 单位是毫秒。"""
        if not self.video:
            return 0.0
        return max(self.video.duration, 0) / 1000

    def apply_detail(self, detail: dict[str, Any]) -> None:
        """用 Web 详情接口补齐分享页可能缺失的统计与时长。"""
        if stats := detail.get("statistics"):
            self.statistics = msgspec.convert(stats, type=Statistics)
        duration = (detail.get("video") or {}).get("duration")
        if self.video and isinstance(duration, int) and duration > 0:
            self.video.duration = duration

    @property
    def metrics(self) -> list[Metric]:
        stats = self.statistics
        if stats is None:
            return []
        return [
            Metric(icon=None, label="点赞", value=self._format_count(stats.digg_count)),
            Metric(icon=None, label="评论", value=self._format_count(stats.comment_count)),
            Metric(icon=None, label="收藏", value=self._format_count(stats.collect_count)),
            Metric(icon=None, label="分享", value=self._format_count(stats.share_count)),
        ]

    @staticmethod
    def _format_count(value: int | str) -> str:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return "0"
        if value >= 100_000_000:
            return f"{value / 100_000_000:.1f}亿"
        if value >= 10_000:
            return f"{value / 10_000:.1f}万"
        return str(value)

    @property
    def avatar_url(self) -> str | None:
        if avatar := self.author.avatar_thumb:
            return choice(avatar.url_list)
        elif avatar := self.author.avatar_medium:
            return choice(avatar.url_list)
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ParseException("can't find data in videoInfoRes")
        return choice(self.item_list)


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(
        name="videoInfoRes", default_factory=VideoInfoRes
    )


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        if page := self.loader_data.video_page:
            return page.video_info_res.video_data
        elif page := self.loader_data.note_page:
            return page.video_info_res.video_data
        raise ParseException(
            "can't find video_(id)/page or note_(id)/page in router data"
        )
