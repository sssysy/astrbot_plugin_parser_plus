from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from msgspec import Struct, field

from ...data import Metric
from .utils import file_image_url, normalize_media_url

QUALITY_LEVELS = ("540p", "720p", "1080p", "2k", "4k")
QUALITY_LABELS = {
    0: "540P",
    1: "720P",
    2: "1080P",
    3: "2K",
    4: "4K",
}
QUALITY_RANK = {
    "540p": 0,
    "720p": 1,
    "1080p": 2,
    "2k": 3,
    "4k": 4,
}
CODEC_RANK = {
    "h264": 0,
    "avc": 0,
    "h265": 1,
    "hevc": 1,
    "av1": 2,
    "av01": 2,
    "h266": 3,
}


class XhsUser(Struct):
    nickname: str = ""
    nick_name: str = field(name="nickName", default="")
    avatar: str = ""
    user_id: str | int = field(name="userId", default="")


class XhsInteractInfo(Struct):
    liked_count: str | int | None = field(name="likedCount", default=0)
    collected_count: str | int | None = field(name="collectedCount", default=0)
    comment_count: str | int | None = field(name="commentCount", default=0)
    share_count: str | int | None = field(name="shareCount", default=0)


class XhsCover(Struct):
    file_id: str = field(name="fileId", default="")


class XhsImage(Struct):
    url_default: str = field(name="urlDefault", default="")
    url: str = ""
    url_size_large: str = field(name="urlSizeLarge", default="")
    file_id: str = field(name="fileId", default="")
    live_photo: bool = field(name="livePhoto", default=False)
    stream: Any = field(default_factory=dict)

    @property
    def image_url(self) -> str:
        return (
            file_image_url(self.file_id)
            or normalize_media_url(self.url_size_large)
            or normalize_media_url(self.url_default)
            or normalize_media_url(self.url)
        )


class XhsVideoCapa(Struct):
    duration: float = 0


class XhsVideoImage(Struct):
    first_frame_fileid: str = field(name="firstFrameFileid", default="")


class XhsVideoMedia(Struct):
    stream: Any = field(default_factory=dict)


class XhsVideoConsumer(Struct):
    origin_video_key: str = field(name="originVideoKey", default="")


class XhsVideo(Struct):
    media: XhsVideoMedia | None = None
    media_v2: str = field(name="mediaV2", default="")
    capa: XhsVideoCapa = field(default_factory=XhsVideoCapa)
    consumer: XhsVideoConsumer = field(default_factory=XhsVideoConsumer)
    image: XhsVideoImage | None = None
    cover: XhsCover | None = None

    @property
    def stream_data(self) -> Any:
        if self.media is not None and isinstance(self.media.stream, dict):
            if self.media.stream:
                return self.media.stream
        if self.media_v2:
            try:
                payload = json.loads(self.media_v2)
            except (TypeError, ValueError):
                return {}
            if isinstance(payload, dict):
                stream = payload.get("stream")
                if isinstance(stream, dict):
                    return stream
        return {}

    @property
    def cover_url(self) -> str:
        if self.image is not None:
            if url := file_image_url(self.image.first_frame_fileid):
                return url
        if self.cover is not None:
            return file_image_url(self.cover.file_id)
        return ""

    @property
    def duration(self) -> float:
        try:
            return max(float(self.capa.duration or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0


class XhsNote(Struct):
    note_id: str = field(name="noteId", default="")
    type: str = ""
    title: str = ""
    desc: str = ""
    user: XhsUser = field(default_factory=XhsUser)
    image_list: list[XhsImage] = field(name="imageList", default_factory=list)
    video: XhsVideo | None = None
    interact_info: XhsInteractInfo = field(
        name="interactInfo", default_factory=XhsInteractInfo
    )
    time: int = 0
    last_update_time: int = field(name="lastUpdateTime", default=0)
    cover: XhsCover | None = None

    @property
    def author_name(self) -> str:
        return self.user.nickname or self.user.nick_name or "小红书用户"

    @property
    def author_id(self) -> str | None:
        value = str(self.user.user_id or "").strip()
        return value or None

    @property
    def avatar_url(self) -> str | None:
        return normalize_media_url(self.user.avatar) or None

    @property
    def text(self) -> str | None:
        return self.desc.strip() or None

    @property
    def timestamp(self) -> int | None:
        value = self.last_update_time or self.time
        if not value:
            return None
        number = int(value)
        if number > 10_000_000_000:
            number //= 1000
        return number

    @property
    def image_urls(self) -> list[str]:
        return [url for image in self.image_list if (url := image.image_url)]

    @property
    def cover_url(self) -> str:
        if self.image_urls:
            return self.image_urls[0]
        if self.video is not None:
            if url := self.video.cover_url:
                return url
        if self.cover is not None:
            return file_image_url(self.cover.file_id)
        return ""

    @property
    def metrics(self) -> list[Metric]:
        info = self.interact_info
        return [
            Metric(icon=None, label="点赞", value=format_count(info.liked_count)),
            Metric(
                icon=None,
                label="收藏",
                value=format_count(info.collected_count),
            ),
            Metric(icon=None, label="评论", value=format_count(info.comment_count)),
            Metric(icon=None, label="分享", value=format_count(info.share_count)),
        ]


@dataclass(frozen=True, slots=True)
class StreamOption:
    url: str
    fallback_urls: tuple[str, ...]
    width: int
    height: int
    size: int
    codec: str
    level: int

    @property
    def quality_label(self) -> str:
        return QUALITY_LABELS.get(self.level, "视频")

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width} × {self.height}"
        return ""


def format_count(value: str | int | None) -> str:
    if value is None:
        return "0"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "0"
        if not text.isdigit():
            return text
        number = int(text)
    else:
        number = int(value)
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    return str(number)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _quality_level(width: int, height: int, text: str = "") -> int:
    pixels = width * height
    if pixels >= 3840 * 2160:
        return 4
    if pixels >= 2560 * 1440:
        return 3
    if pixels >= 1920 * 1080:
        return 2
    if pixels >= 1280 * 720:
        return 1
    if pixels:
        return 0

    lowered = text.lower()
    for level, key in reversed(tuple(enumerate(QUALITY_LEVELS))):
        if key in lowered:
            return level
    return 0


def _stream_options(stream_data: Any) -> list[StreamOption]:
    if not isinstance(stream_data, dict):
        return []

    options: list[StreamOption] = []
    for codec, items in stream_data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = normalize_media_url(
                item.get("masterUrl") or item.get("master_url")
            )
            if not url:
                continue
            raw_backups = item.get("backupUrls") or item.get("backup_urls") or []
            if not isinstance(raw_backups, list):
                raw_backups = [raw_backups]
            fallback_urls: list[str] = []
            for backup in raw_backups:
                normalized = normalize_media_url(backup)
                if normalized and normalized != url and normalized not in fallback_urls:
                    fallback_urls.append(normalized)
            width = _to_int(item.get("width"))
            height = _to_int(item.get("height"))
            codec_name = str(
                item.get("videoCodec")
                or item.get("video_codec")
                or codec
            ).lower()
            text = " ".join(
                str(item.get(key) or "")
                for key in ("qualityType", "quality_type", "streamDesc", "stream_desc")
            )
            options.append(
                StreamOption(
                    url=url,
                    fallback_urls=tuple(fallback_urls),
                    width=width,
                    height=height,
                    size=_to_int(item.get("size")),
                    codec=codec_name,
                    level=_quality_level(width, height, text),
                )
            )
    return options


def _codec_rank(codec: str) -> int:
    return CODEC_RANK.get(codec.lower(), 99)


def _ordered(options: list[StreamOption]) -> list[StreamOption]:
    return sorted(
        options,
        key=lambda item: (item.level, item.size, -_codec_rank(item.codec)),
        reverse=True,
    )


def _choose_stream(
    options: list[StreamOption],
    quality: str = "auto",
    max_bytes: int | None = None,
) -> StreamOption | None:
    if not options:
        return None
    ordered = _ordered(options)
    preference = quality.strip().lower()
    if preference in {"", "auto", "adapt"}:
        if max_bytes:
            suitable = [
                item
                for item in ordered
                if item.size <= 0 or item.size <= max_bytes
            ]
            if suitable:
                return suitable[0]
        return ordered[0]

    target = QUALITY_RANK.get(preference)
    if target is None:
        return ordered[0]

    by_level: dict[int, list[StreamOption]] = {}
    for item in ordered:
        by_level.setdefault(item.level, []).append(item)
    if target in by_level:
        return by_level[target][0]
    for level in range(target - 1, -1, -1):
        if level in by_level:
            return by_level[level][0]
    for level in range(target + 1, 5):
        if level in by_level:
            return by_level[level][0]
    return ordered[0]


def select_video(
    video: XhsVideo,
    quality: str = "auto",
    max_bytes: int | None = None,
) -> StreamOption | None:
    options = _stream_options(video.stream_data)
    if not options and video.consumer.origin_video_key:
        key = video.consumer.origin_video_key
        options = _stream_options(
            {
                "h264": [
                    {
                        "masterUrl": f"https://sns-video-hw.xhscdn.com/{key}",
                        "backupUrls": [
                            f"https://sns-video-bd.xhscdn.com/{key}"
                        ],
                    }
                ]
            }
        )
    return _choose_stream(options, quality, max_bytes)


def select_live_photo_stream(image: XhsImage) -> StreamOption | None:
    return _choose_stream(_stream_options(image.stream))
