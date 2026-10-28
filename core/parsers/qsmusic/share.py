from __future__ import annotations

import json
import re
from typing import Any

from ...exception import ParseException

ROUTER_DATA_RE = re.compile(
    r'<script\s+async=""\s+data-script-src="modern-inline">'
    r"_ROUTER_DATA\s*=\s*({.*?});",
    flags=re.DOTALL,
)


def decode_share_html(html: str) -> dict[str, Any]:
    matched = ROUTER_DATA_RE.search(html)
    if matched is None:
        raise ParseException("汽水音乐分享页缺少结构化数据")
    try:
        payload = json.loads(matched.group(1))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ParseException("汽水音乐分享页数据异常") from exc
    if not isinstance(payload, dict):
        raise ParseException("汽水音乐分享页数据异常")
    return payload


def decode_track(payload: dict[str, Any]) -> dict[str, Any]:
    loader_data = payload.get("loaderData")
    track_page = (
        loader_data.get("track_page") if isinstance(loader_data, dict) else None
    )
    track = (
        track_page.get("audioWithLyricsOption")
        if isinstance(track_page, dict)
        else None
    )
    if not isinstance(track, dict):
        raise ParseException("汽水音乐分享中缺少歌曲数据")
    if not str(track.get("url") or "").startswith(("http://", "https://")):
        raise ParseException("汽水音乐未返回有效音频地址")
    return track


def cover_url(track: dict[str, Any]) -> str | None:
    value = track.get("coverURL")
    return (
        value
        if isinstance(value, str) and value.startswith(("http://", "https://"))
        else None
    )


def artist_avatar_url(track: dict[str, Any]) -> str | None:
    track_info = track.get("trackInfo")
    artists = track_info.get("artists") if isinstance(track_info, dict) else None
    if not isinstance(artists, list):
        return None
    for artist in artists:
        if not isinstance(artist, dict):
            continue
        for key in ("medium_avatar_url", "url_avatar"):
            value = artist.get(key)
            if isinstance(value, dict):
                urls = value.get("urls")
                if isinstance(urls, list) and urls:
                    url = urls[0]
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        return url
    return None


def track_album(track: dict[str, Any]) -> str:
    track_info = track.get("trackInfo")
    album = track_info.get("album") if isinstance(track_info, dict) else None
    name = album.get("name") if isinstance(album, dict) else None
    return name if isinstance(name, str) else ""


def format_lyrics(track: dict[str, Any]) -> str:
    lyrics = track.get("lyrics")
    sentences = lyrics.get("sentences") if isinstance(lyrics, dict) else None
    if not isinstance(sentences, list):
        return ""

    lines: list[str] = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        start_ms = _int(sentence.get("startMs"))
        text = sentence.get("text")
        if not isinstance(text, str) or not text.strip():
            words = sentence.get("words")
            if isinstance(words, list):
                text = "".join(
                    word.get("text", "")
                    for word in words
                    if isinstance(word, dict) and isinstance(word.get("text"), str)
                )
        if not isinstance(text, str) or not text.strip():
            continue
        minutes, remainder = divmod(max(0, start_ms), 60_000)
        seconds, millis = divmod(remainder, 1000)
        centiseconds = millis // 10
        lines.append(f"[{minutes:02d}:{seconds:02d}.{centiseconds:02d}]{text.strip()}")
    return "\n".join(lines)


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
