from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VideoData:
    title: str
    channel: str
    uploader: str
    channel_id: str
    duration: float
    timestamp: int | None
    thumbnail: str | None
    description: str | None


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return str(value).strip() if value is not None else ""


def decode_video(data: dict[str, Any]) -> VideoData:
    entries = data.get("entries")
    if data.get("_type") == "playlist" and isinstance(entries, list):
        usable = [item for item in entries if isinstance(item, dict)]
        if len(usable) != 1:
            raise ValueError("YouTube 链接返回了多个视频，无法自动选择")
        data = usable[0]
    title = _text(data, "title") or "YouTube 视频"
    channel = _text(data, "channel") or _text(data, "uploader")
    uploader = _text(data, "uploader_id") or _text(data, "uploader")
    channel_id = _text(data, "channel_id")
    thumbnail = _text(data, "thumbnail") or None
    description = _text(data, "description") or None
    timestamp_value = data.get("timestamp")
    try:
        timestamp = int(timestamp_value) if timestamp_value is not None else None
    except (TypeError, ValueError):
        timestamp = None
    try:
        duration = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return VideoData(
        title=title,
        channel=channel,
        uploader=uploader,
        channel_id=channel_id,
        duration=max(duration, 0.0),
        timestamp=timestamp,
        thumbnail=thumbnail,
        description=description,
    )
