from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

from ...exception import ParseException

IMAGE_HOST = "i.iwara.tv"
DEFAULT_AVATAR_URL = "https://www.iwara.tv/images/default-avatar.jpg"
QUALITY_FALLBACK = ("Source", "preview", "540", "360")


@dataclass(frozen=True, slots=True)
class IwaraFile:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class IwaraUser:
    name: str
    username: str
    avatar_url: str | None

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.username})"


@dataclass(frozen=True, slots=True)
class IwaraVideo:
    id: str
    title: str
    body: str | None
    rating: str
    tags: tuple[str, ...]
    user: IwaraUser
    thumbnail_url: str | None
    duration: float
    timestamp: int
    file_url: str

    @property
    def url(self) -> str:
        return f"https://www.iwara.tv/video/{self.id}"


@dataclass(frozen=True, slots=True)
class IwaraImage:
    id: str
    title: str
    body: str | None
    rating: str
    tags: tuple[str, ...]
    user: IwaraUser
    thumbnail_url: str | None
    timestamp: int
    image_urls: tuple[str, ...]
    large_image_urls: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"https://www.iwara.tv/image/{self.id}"


def avatar_url(user: dict[str, Any]) -> str | None:
    avatar = user.get("avatar")
    if not isinstance(avatar, dict):
        return DEFAULT_AVATAR_URL
    avatar_id = avatar.get("id")
    avatar_name = avatar.get("name")
    if not isinstance(avatar_id, str) or not isinstance(avatar_name, str):
        return DEFAULT_AVATAR_URL
    return (
        f"https://{IMAGE_HOST}/image/avatar/{quote(avatar_id, safe='')}/"
        f"{quote(avatar_name, safe='')}"
    )


def parse_user(raw: Any) -> IwaraUser:
    if not isinstance(raw, dict):
        raise ParseException("Iwara 作者信息缺失")
    name = raw.get("name")
    username = raw.get("username")
    if not isinstance(name, str) or not isinstance(username, str):
        raise ParseException("Iwara 作者信息格式异常")
    return IwaraUser(name=name, username=username, avatar_url=avatar_url(raw))


def parse_tags(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    tags: list[str] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            tags.append(item["id"])
    return tuple(tags)


def parse_timestamp(raw: Any) -> int:
    if not isinstance(raw, str):
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def parse_file(raw: Any) -> IwaraFile:
    if not isinstance(raw, dict):
        raise ParseException("Iwara 媒体文件信息缺失")
    file_id = raw.get("id")
    name = raw.get("name")
    if not isinstance(file_id, str) or not isinstance(name, str):
        raise ParseException("Iwara 媒体文件信息格式异常")
    return IwaraFile(id=file_id, name=name)


def video_thumbnail_url(file: IwaraFile, thumbnail: Any) -> str | None:
    if isinstance(thumbnail, bool) or not isinstance(thumbnail, int):
        return None
    number = str(thumbnail).zfill(2)
    return (
        f"https://{IMAGE_HOST}/image/thumbnail/{quote(file.id, safe='')}/"
        f"thumbnail-{number}.jpg"
    )


def image_thumbnail_url(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    image_id = raw.get("id")
    name = raw.get("name")
    if not isinstance(image_id, str) or not isinstance(name, str):
        return None
    return (
        f"https://{IMAGE_HOST}/image/large/{quote(image_id, safe='')}/"
        f"{quote(name, safe='')}"
    )


def image_urls(file: IwaraFile, *, large: bool) -> str:
    size = "large" if large else "original"
    return (
        f"https://{IMAGE_HOST}/image/{size}/{quote(file.id, safe='')}/"
        f"{quote(file.name, safe='')}"
    )


def parse_video(payload: Any) -> IwaraVideo:
    if not isinstance(payload, dict):
        raise ParseException("Iwara 视频信息格式异常")
    video_id = payload.get("id")
    title = payload.get("title")
    rating = payload.get("rating")
    file_url = payload.get("fileUrl")
    if not isinstance(video_id, str) or not isinstance(title, str):
        raise ParseException("Iwara 视频信息缺失")
    if not isinstance(file_url, str):
        raise ParseException("Iwara 视频文件地址缺失")
    if not is_iwara_url(file_url):
        raise ParseException("Iwara 视频文件地址来源异常")
    file = parse_file(payload.get("file"))
    duration = file_duration(payload.get("file"))
    return IwaraVideo(
        id=video_id,
        title=title,
        body=payload.get("body") if isinstance(payload.get("body"), str) else None,
        rating=rating if isinstance(rating, str) else "general",
        tags=parse_tags(payload.get("tags")),
        user=parse_user(payload.get("user")),
        thumbnail_url=video_thumbnail_url(file, payload.get("thumbnail")),
        duration=duration,
        timestamp=parse_timestamp(payload.get("createdAt")),
        file_url=file_url,
    )


def file_duration(raw: Any) -> float:
    if isinstance(raw, dict):
        value = raw.get("duration")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return 0.0


def parse_image(payload: Any) -> IwaraImage:
    if not isinstance(payload, dict):
        raise ParseException("Iwara 图片信息格式异常")
    image_id = payload.get("id")
    title = payload.get("title")
    if not isinstance(image_id, str) or not isinstance(title, str):
        raise ParseException("Iwara 图片信息缺失")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ParseException("Iwara 图片列表为空")
    files = [parse_file(item) for item in raw_files]
    rating = payload.get("rating")
    return IwaraImage(
        id=image_id,
        title=title,
        body=payload.get("body") if isinstance(payload.get("body"), str) else None,
        rating=rating if isinstance(rating, str) else "general",
        tags=parse_tags(payload.get("tags")),
        user=parse_user(payload.get("user")),
        thumbnail_url=image_thumbnail_url(payload.get("thumbnail")),
        timestamp=parse_timestamp(payload.get("createdAt")),
        image_urls=tuple(image_urls(file, large=False) for file in files),
        large_image_urls=tuple(image_urls(file, large=True) for file in files),
    )


def choose_quality_url(payload: Any, quality: str) -> tuple[str, str]:
    if isinstance(payload, list):
        results = payload
    elif isinstance(payload, dict):
        results = payload.get("results")
    else:
        results = None
    if not isinstance(results, list):
        raise ParseException("Iwara 视频画质列表格式异常")

    available: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        src = item.get("src")
        download = src.get("download") if isinstance(src, dict) else None
        if not isinstance(name, str) or not isinstance(download, str):
            continue
        url = download if download.startswith("http") else f"https:{download}"
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        available[name] = url

    start = (
        QUALITY_FALLBACK.index(quality)
        if quality in QUALITY_FALLBACK
        else 0
    )
    for name in QUALITY_FALLBACK[start:]:
        if name in available:
            return available[name], name
    raise ParseException("未找到可用的 Iwara 视频下载链接")


def is_iwara_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "iwara.tv" or host.endswith(".iwara.tv")
    )
