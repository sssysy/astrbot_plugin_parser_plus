from __future__ import annotations

import re

from .models import AcfunPlayInfo


_QUALITY_RE = re.compile(r"(?P<height>\d{3,4})P(?P<fps>\d+)?", re.IGNORECASE)


def quality_label(item: AcfunPlayInfo) -> str:
    return item.quality_label.strip() or "未知画质"


def _quality_key(item: AcfunPlayInfo) -> tuple[int, int]:
    matched = _QUALITY_RE.search(item.quality_label)
    if not matched:
        return (0, item.fps or 0)
    return (int(matched.group("height")), item.fps or int(matched.group("fps") or 0))


def select_play_info(
    play_infos: list[AcfunPlayInfo],
    quality: str = "auto",
) -> AcfunPlayInfo | None:
    candidates = [item for item in play_infos if item.play_urls]
    if not candidates:
        return None
    if quality in {"", "auto"}:
        return max(candidates, key=_quality_key)

    matched = _QUALITY_RE.fullmatch(quality.strip())
    if not matched:
        return max(candidates, key=_quality_key)
    height = int(matched.group("height"))
    fps = int(matched.group("fps") or 0)

    eligible = [item for item in candidates if _quality_key(item)[0] <= height]
    if fps:
        exact_fps = [item for item in eligible if (_quality_key(item)[1]) >= fps]
        if exact_fps:
            return max(exact_fps, key=_quality_key)
    if eligible:
        return max(eligible, key=_quality_key)
    return min(candidates, key=_quality_key)


def play_urls(item: AcfunPlayInfo) -> list[str]:
    urls: list[str] = []
    for value in item.play_urls:
        url = value.strip()
        if not url:
            continue
        if url.startswith("http://"):
            url = f"https://{url.removeprefix('http://')}"
        if url.startswith("https://") and url not in urls:
            urls.append(url)
    return urls
