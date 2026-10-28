from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class InstagramTarget:
    url: str
    shortcode: str
    kind: str


@dataclass(frozen=True, slots=True)
class InstagramMedia:
    kind: str
    url: str
    cover: str | None = None
    duration: float = 0.0


@dataclass(frozen=True, slots=True)
class InstagramPost:
    target: InstagramTarget
    title: str | None
    author: str | None
    author_id: str | None
    text: str | None
    timestamp: int | None
    cover: str | None
    medias: tuple[InstagramMedia, ...]

    @property
    def has_video(self) -> bool:
        return any(item.kind == "video" for item in self.medias)


def parse_target(matched: str) -> InstagramTarget:
    raw = matched.strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host not in {"instagram.com", "www.instagram.com", "instagr.am"}:
        raise ValueError("不是 Instagram 链接")
    if len(parts) < 2 or parts[0] not in {"p", "reel", "reels", "tv", "share"}:
        raise ValueError("Instagram 链接缺少作品路径")
    kind = parts[0]
    shortcode_index = 1
    if kind == "share":
        if len(parts) >= 3 and parts[1] in {"p", "reel", "reels", "tv"}:
            kind = "reel" if parts[1] == "reels" else parts[1]
            shortcode_index = 2
    shortcode = parts[shortcode_index].strip()
    if not shortcode or not all(
        char.isascii() and (char.isalnum() or char in "_-") for char in shortcode
    ):
        raise ValueError("Instagram 链接缺少有效作品 ID")
    canonical_kind = "reel" if kind == "reels" else kind
    return InstagramTarget(
        url=f"https://www.instagram.com/{canonical_kind}/{shortcode}/",
        shortcode=shortcode,
        kind=canonical_kind,
    )


def unwrap_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("_type") != "playlist":
        return [info]
    entries = info.get("entries")
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def video_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get("formats")
    if not isinstance(formats, list):
        return []
    return [
        item
        for item in formats
        if isinstance(item, dict)
        and _trusted_media_url(item.get("url"))
        and item.get("vcodec") not in (None, "none")
    ]


def image_urls(info: dict[str, Any]) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in thumbnails:
            if not isinstance(item, dict) or not _trusted_media_url(item.get("url")):
                continue
            width = _dimension(item.get("width"))
            height = _dimension(item.get("height"))
            candidates.append((width * height, len(candidates), str(item["url"])))
    if not candidates:
        return []
    return [max(candidates, key=lambda item: (item[0], item[1]))[2]]


def pick_thumbnail(info: dict[str, Any]) -> str | None:
    thumbnails = image_urls(info)
    if thumbnails:
        return thumbnails[0]
    for key in ("thumbnail", "display_url"):
        value = info.get(key)
        if _trusted_media_url(value):
            return str(value)
    return None


def _dimension(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def format_selector(quality: str | None) -> str:
    value = (quality or "auto").strip().lower()
    height = value.removesuffix("p")
    if value == "auto" or not height.isdigit():
        return "bv*+ba/b"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def _http_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _trusted_media_url(value: object) -> bool:
    if not _http_url(value):
        return False
    host = (urlparse(str(value)).hostname or "").lower()
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in ("instagram.com", "cdninstagram.com", "fbcdn.net")
    )


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
