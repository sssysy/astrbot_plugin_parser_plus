from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

Segment = tuple[Literal["text", "image"], str]

_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINE_RE = re.compile(r"\n{3,}")
_NGA_TIMEZONE = ZoneInfo("Asia/Shanghai")
_IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}
_IMAGE_HOSTS = {
    "img.nga.cn",
    "img.nga.178.com",
    "img.ngabbs.com",
    "nga.178.com",
    "bbs.nga.cn",
}
_IGNORED_TAGS = {"script", "style", "noscript", "template"}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}


def clean_text(value: str) -> str:
    value = html.unescape(value.replace("\xa0", " "))
    value = _SPACE_RE.sub(" ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = _BLANK_LINE_RE.sub("\n\n", value)
    return value.strip()


def normalize_image_url(value: str, base_url: str) -> str:
    value = html.unescape(value).strip().strip("\"'")
    if not value or value.startswith(("data:", "javascript:")):
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    if value.startswith(("./", "../")):
        value = urljoin("https://bbs.nga.cn/attachments/", value)
    elif not value.startswith(("http://", "https://")):
        value = urljoin("https://bbs.nga.cn/attachments/", value.lstrip("/"))
    else:
        value = urljoin(base_url, value)
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    if host not in _IMAGE_HOSTS and not host.endswith(".nga.cn"):
        return ""
    path = (parsed.path or "").lower()
    if path.endswith(tuple(_IMAGE_EXTENSIONS)):
        return value
    if "/attachments/" in path:
        return value
    return ""


def _source_from_attrs(attrs: list[tuple[str, str | None]]) -> str:
    values = {key.lower(): value or "" for key, value in attrs}
    for key in (
        "data-src",
        "data-original",
        "data-actualsrc",
        "data-lazyload",
        "src",
    ):
        value = values.get(key, "").strip()
        if value:
            return value
    return ""


class _NGABodyParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.segments: list[Segment] = []
        self._text: list[str] = []
        self._ignored = 0
        self._seen_images: set[str] = set()

    def _flush_text(self) -> None:
        value = clean_text("".join(self._text))
        self._text.clear()
        if not value:
            return
        if self.segments and self.segments[-1][0] == "text":
            self.segments[-1] = ("text", f"{self.segments[-1][1]}\n\n{value}")
        else:
            self.segments.append(("text", value))

    def _push_image(self, value: str) -> None:
        value = normalize_image_url(value, self.base_url)
        if not value:
            return
        key = value.split("?", 1)[0]
        if key in self._seen_images:
            return
        self._seen_images.add(key)
        self._flush_text()
        self.segments.append(("image", value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in _IGNORED_TAGS:
            self._ignored += 1
            return
        if self._ignored:
            return
        if lowered in _BLOCK_TAGS:
            self._text.append("\n")
        if lowered == "img":
            self._push_image(_source_from_attrs(attrs))
        if lowered == "a":
            href = dict(attrs).get("href") or ""
            if any(ext in (urlparse(href).path or "").lower() for ext in _IMAGE_EXTENSIONS):
                self._push_image(href)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _IGNORED_TAGS:
            self._ignored = max(0, self._ignored - 1)
            return
        if self._ignored:
            return
        if lowered in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self._text.append(data)


def parse_html_segments(source: str, base_url: str) -> list[Segment]:
    parser = _NGABodyParser(base_url)
    parser.feed(source)
    parser.close()
    parser._flush_text()
    return parser.segments


def parse_bbcode_segments(source: str, base_url: str) -> list[Segment]:
    source = html.unescape(source.replace("\xa0", " "))
    segments: list[Segment] = []
    for part in re.split(
        r"(\[img\][^\[]*?\[/img\])",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        image_match = re.fullmatch(r"\[img\](.*?)\[/img\]", part, re.IGNORECASE | re.DOTALL)
        if image_match:
            image = normalize_image_url(image_match.group(1), base_url)
            if image:
                segments.append(("image", image))
            continue
        value = _clean_bbcode_text(part)
        if value:
            segments.append(("text", value))
    return segments


def _clean_bbcode_text(source: str) -> str:
    for tag in ("quote", "collapse", "code", "list", "table"):
        source = re.sub(
            rf"\[{tag}(?:=[^\]]*)?\].*?\[/{tag}\]",
            "\n",
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
    source = re.sub(
        r"\[(?:b|i|u|del|s|color|size|font)(?:=[^\]]*)?\]",
        "",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"\[/(?:b|i|u|del|s|color|size|font)\]",
        "",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"\[url=[^\]]*\](.*?)\[/url\]",
        r"\1",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    source = re.sub(
        r"\[url\](.*?)\[/url\]",
        r"\1",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    source = re.sub(r"\[/?[^\[\]]+\]", "\n", source)
    return clean_text(source)


def extract_segments(raw_content: str, base_url: str) -> list[Segment]:
    value = raw_content.strip()
    if not value:
        return []
    if "<" in value and ">" in value:
        segments = parse_html_segments(value, base_url)
        if segments:
            return segments
    return parse_bbcode_segments(value, base_url)


@dataclass(slots=True)
class NgaPost:
    title: str | None
    author: str | None
    author_uid: str | None
    timestamp: int | None
    segments: list[Segment]
    floor_label: str | None = None
    body_text: str = field(default="")


def _find_tag_by_id(source: str, element_id: str) -> str | None:
    pattern = re.compile(
        rf"<(?P<tag>[a-z][a-z0-9]*)\b(?P<attrs>[^>]*\bid\s*=\s*['\"]"
        rf"{re.escape(element_id)}['\"][^>]*)>(?P<body>.*?)</(?P=tag)>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(source)
    return match.group(0) if match else None


def _tag_text(fragment: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return clean_text(html.unescape(value))


def _parse_timestamp(value: str) -> int | None:
    value = clean_text(value)
    value = re.sub(r"\s+", " ", value)
    for pattern, fmt in (
        (r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", "%Y-%m-%d %H:%M"),
        (r"(\d{4}-\d{2}-\d{2})", "%Y-%m-%d"),
        (r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2})", "%Y/%m/%d %H:%M"),
        (r"(\d{4}/\d{2}/\d{2})", "%Y/%m/%d"),
    ):
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            parsed = datetime.strptime(match.group(1), fmt)
            return int(parsed.replace(tzinfo=_NGA_TIMEZONE).timestamp())
        except ValueError:
            continue
    return None


def parse_post_html(
    source: str,
    *,
    page_url: str,
    pid: str | None = None,
) -> NgaPost:
    title_tag = _find_tag_by_id(source, "postsubject0")
    content_tag = _find_tag_by_id(source, "postcontent0")
    author_tag = _find_tag_by_id(source, "postauthor0")
    date_tag = _find_tag_by_id(source, "postdate0")

    title = _tag_text(title_tag) if title_tag else None
    title = re.sub(r"^\[[^\]]+\]\s*", "", title or "").strip() or None

    body = ""
    if content_tag:
        match = re.search(r">(?P<body>.*)</[a-z][^>]*>$", content_tag, re.DOTALL)
        body = match.group("body") if match else content_tag
    segments = extract_segments(body, page_url)

    author = None
    author_uid = None
    if author_tag:
        author_link = re.search(r"href\s*=\s*(['\"])(.*?)\1", author_tag, re.I | re.S)
        if author_link:
            uid_match = re.search(r"[?&]uid=(\d+)", author_link.group(2))
            if uid_match:
                author_uid = uid_match.group(1)
        author = _tag_text(author_tag)
        if not author or author.startswith(("UID", "用户")):
            author = None
    if author is None and author_uid:
        author_match = re.search(
            rf"[\"']?{re.escape(author_uid)}[\"']?\s*:\s*\{{[^{{}}]*?"
            rf"[\"']username[\"']\s*:\s*[\"']([^\"']+)",
            source,
            re.DOTALL,
        )
        if author_match:
            author = html.unescape(author_match.group(1)).strip() or None

    timestamp = _parse_timestamp(_tag_text(date_tag)) if date_tag else None
    floor_label = None
    if pid:
        floor_match = re.search(
            rf"id\s*=\s*['\"]post(\d+)['\"]",
            source,
            re.IGNORECASE,
        )
        if floor_match:
            floor_label = f"楼层 {floor_match.group(1)}"

    return NgaPost(
        title=title,
        author=author,
        author_uid=author_uid,
        timestamp=timestamp,
        segments=segments,
        floor_label=floor_label,
        body_text=_segments_text(segments),
    )


def _segments_text(segments: list[Segment]) -> str:
    return "\n\n".join(value for kind, value in segments if kind == "text")


def extract_guest_js(source: str) -> str | None:
    patterns = (
        r"document\.cookie\s*=\s*['\"]guestJs=([^;'\"]+)",
        r"guestJs=([^;'\"]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value and not value.startswith("document"):
                return value
    return None


def is_blocked_page(source: str) -> bool:
    return "访客不能直接访问" in source
