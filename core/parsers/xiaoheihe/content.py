from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse

from ...exception import ParseException
from ...utils import format_num
from ...data import Metric

Segment = tuple[Literal["text", "image", "live"], str, str]


class _BodyHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[tuple[str, str, str]] = []
        self._text: list[str] = []
        self._image_alt: str | None = None
        self._ignored_depth = 0

    def _flush_text(self) -> None:
        value = clean_text("".join(self._text))
        self._text.clear()
        if value:
            self.parts.append(("text", value, ""))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if lowered in {"br", "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._text.append("\n")
            if lowered.startswith("h"):
                self._text.append("\n")
        if lowered == "img":
            source = next(
                (
                    value
                    for key, value in attrs
                    if key in {"data-original", "data-actualsrc", "data-default-watermark-src", "src"}
                    and value
                ),
                "",
            )
            if source:
                self._flush_text()
                self.parts.append(("image", html.unescape(source), ""))
        if lowered == "h4" and dict(attrs).get("class") == "img-desc":
            self._image_alt = ""

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if lowered in {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._text.append("\n")
        if lowered == "h4" and self._image_alt is not None:
            alt = clean_text(self._image_alt)
            self._image_alt = None
            if alt and self.parts and self.parts[-1][0] == "image":
                kind, source, _ = self.parts[-1]
                self.parts[-1] = (kind, source, alt)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._image_alt is not None:
            self._image_alt += data
        else:
            self._text.append(data)

    def finish(self) -> list[tuple[str, str, str]]:
        self._flush_text()
        return self.parts


def clean_text(value: str) -> str:
    value = html.unescape(value.replace("\xa0", " "))
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_html_segments(source: str) -> list[tuple[str, str, str]]:
    parser = _BodyHTMLParser()
    parser.feed(source)
    parser.close()
    return parser.finish()


def parse_body_segments(raw_text: object) -> list[Segment]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return []
    try:
        blocks = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        value = clean_text(raw_text)
        return [("text", value, "")] if value else []
    if not isinstance(blocks, list):
        value = clean_text(raw_text)
        return [("text", value, "")] if value else []

    result: list[Segment] = []
    seen: set[str] = set()

    def push_text(value: str) -> None:
        value = clean_text(value)
        if not value:
            return
        if result and result[-1][0] == "text":
            result[-1] = ("text", result[-1][1] + "\n\n" + value, "")
        else:
            result.append(("text", value, ""))

    def push_image(value: str, alt: str = "", live: str = "") -> None:
        value = normalize_media_url(value)
        if not value:
            return
        key = dedup_key(value)
        if key in seen:
            return
        seen.add(key)
        result.append(("live" if live else "image", value, alt))
        if live:
            result[-1] = ("live", value, live)

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "img":
            image = str(block.get("url") or "").strip()
            live = str(block.get("live_url") or "").strip()
            push_image(image, str(block.get("text") or ""), live)
            continue
        source = str(block.get("text") or "")
        if not source:
            continue
        for part_kind, value, alt in parse_html_segments(source):
            if part_kind == "text":
                push_text(value)
            else:
                push_image(value, alt)

    return result


def normalize_media_url(value: str) -> str:
    value = html.unescape(value).strip()
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("http://"):
        value = f"https://{value[7:]}"
    if not value.startswith("https://"):
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host not in {"imgheybox.max-c.com", "imgheybox1.max-c.com", "cdn.max-c.com"} and not host.endswith(".max-c.com"):
        return ""
    return value


def normalize_image_url(value: str, *, original: bool) -> str:
    value = normalize_media_url(value)
    if value and original and "imageMogr2" in value:
        value = value.split("?", 1)[0]
    return value


def dedup_key(value: str) -> str:
    return (
        value.split("?", 1)[0]
        .replace("imgheybox1.max-c.com", "imgheybox.max-c.com")
    )


def is_video_url(value: str) -> bool:
    path = (urlparse(value).path or "").lower()
    return path.endswith((".mp4", ".mov", ".m3u8"))


@dataclass(frozen=True, slots=True)
class PostContent:
    title: str | None
    body_text: str
    segments: list[Segment]
    image_urls: list[str]
    live_entries: list[tuple[str, str]]
    video_url: str | None
    video_cover: str | None
    metrics: list[Metric]
    author_name: str
    author_avatar: str | None
    author_uid: str | None
    author_description: str | None
    timestamp: int | None


def build_post_content(link: dict, *, original_image: bool) -> PostContent:
    raw_segments = parse_body_segments(link.get("text"))
    segments: list[Segment] = []
    images: list[str] = []
    lives: list[tuple[str, str]] = []
    body_parts: list[str] = []
    seen_images: set[str] = set()
    for kind, value, extra in raw_segments:
        if kind == "text":
            segments.append(("text", value, ""))
            body_parts.append(value)
            continue
        if kind == "live":
            image = normalize_image_url(value, original=original_image)
            video = normalize_media_url(extra)
            if image and video:
                segments.append(("live", image, video))
                lives.append((video, image))
            continue
        image = normalize_image_url(value, original=original_image)
        key = dedup_key(image)
        if not image or key in seen_images:
            continue
        seen_images.add(key)
        segments.append(("image", image, extra))
        images.append(image)

    video_url = normalize_media_url(str(link.get("video_url") or ""))
    if not video_url or not link.get("has_video"):
        video_url = None
    video_cover = normalize_media_url(str(link.get("video_thumb") or ""))
    user = link.get("user") if isinstance(link.get("user"), dict) else {}
    title = clean_text(str(link.get("title") or "")) or None
    description = clean_text(str(link.get("description") or "")) or None
    metrics = [
        Metric(icon=None, label=label, value=format_num(value))
        for label, value in (
            ("浏览", _number(link.get("click"))),
            ("点赞", _number(link.get("link_award_num"))),
            ("评论", _number(link.get("comment_num"))),
            ("收藏", _number(link.get("favour_count"))),
            ("分享", _number(link.get("forward_num"))),
        )
    ]
    battery = link.get("battery")
    if isinstance(battery, dict) and _number(battery.get("count")):
        metrics.append(
            Metric(icon=None, label="电池", value=format_num(_number(battery.get("count"))))
        )
    return PostContent(
        title=title,
        body_text="\n\n".join(body_parts).strip(),
        segments=segments,
        image_urls=images,
        live_entries=lives,
        video_url=video_url,
        video_cover=video_cover,
        metrics=metrics,
        author_name=clean_text(str(user.get("username") or user.get("nickname") or ""))
        or "小黑盒用户",
        author_avatar=normalize_media_url(str(user.get("avatar") or "")) or None,
        author_uid=str(user.get("userid") or "") or None,
        author_description=description,
        timestamp=_number(link.get("create_at")) or None,
    )


def _number(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def select_video_urls(game: dict, html_text: str, mode: str) -> list[tuple[str, str | None]]:
    if mode == "none":
        return []
    cover = normalize_media_url(str(game.get("video_thumb") or "")) or None
    candidates: list[str] = []
    direct = normalize_media_url(str(game.get("video_url") or ""))
    if direct:
        candidates.append(direct)
    candidates.extend(
        normalize_media_url(item)
        for item in re.findall(
            r'https?://[^"\'\s<>]+\.(?:m3u8|mp4|mov)(?:\?[^"\'\s<>]*)?',
            html_text,
            re.I,
        )
    )
    output: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for value in candidates:
        if not value or value in seen or not is_video_url(value):
            continue
        seen.add(value)
        output.append((value, cover))
        if mode != "all":
            break
    return output
