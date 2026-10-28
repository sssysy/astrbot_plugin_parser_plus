from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True, slots=True)
class YouTubeTarget:
    video_id: str
    url: str
    is_music: bool


def parse_target(url: str, matched: str) -> YouTubeTarget:
    """Normalize a matched YouTube URL and extract an explicit video id."""
    raw = matched.strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
                video_id = parts[1]
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("YouTube 链接缺少有效的视频 ID")
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    return YouTubeTarget(
        video_id=video_id,
        url=canonical,
        is_music=host == "music.youtube.com",
    )


def quality_height(value: str | None) -> int:
    if not value or value == "auto":
        return 0
    matched = re.fullmatch(r"(\d{3,4})p", str(value).strip())
    return int(matched.group(1)) if matched else 0


def video_format_selector(quality: str | None, codec: str | None) -> str:
    height = quality_height(quality)
    limiting = f"[height<={height}]" if height else ""
    fallback = f"b[height<={height}]" if height else "b"
    selector = f"bv*{limiting}+ba/{fallback}/bv*+ba/b"
    codec = (codec or "auto").lower()
    sorting = {
        "avc": "+codec:avc1",
        "hevc": "+codec:hev1",
        "av1": "+codec:av01",
    }.get(codec)
    return f"{selector}/?{sorting}" if sorting else selector


def audio_format_selector() -> str:
    return "bestaudio/best"


def download_ranges(seconds: int | float | None) -> str | None:
    if not seconds or float(seconds) <= 0:
        return None
    return f"0-{int(float(seconds))}"


def media_is_audio(
    target: YouTubeTarget,
    mode: str | None,
) -> bool:
    selected = (mode or "auto").lower()
    if selected == "audio":
        return True
    if selected == "video":
        return False
    return target.is_music
