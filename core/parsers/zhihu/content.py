from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from ...data import ContentItem, TextContent

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
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
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_SKIP_TAGS = frozenset(
    {"audio", "iframe", "noscript", "script", "style", "svg", "video", "source"}
)
_VIDEO_MARKERS = (
    "video.zhihu.com",
    "/playback/",
    "/playlist.m3u8",
    "/stream/",
    ".vod.",
)
_IMAGE_HOSTS = ("zhimg.com",)


def clean_text(value: Any, *, keep_newlines: bool = True) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    if keep_newlines:
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_url(value: Any, page_url: str | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).strip().strip("\"'")
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    text = text.rstrip(".,);")
    if not text or text.startswith(("data:", "blob:")):
        return ""
    if text.startswith("//"):
        text = f"https:{text}"
    elif page_url and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        text = urljoin(page_url, text)
    if not text.startswith(("http://", "https://")):
        return ""
    return text


def looks_like_image_url(url: str) -> bool:
    lowered = url.lower()
    if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", lowered):
        return False
    if re.search(r"\.(jpg|jpeg|png|webp|gif|bmp|avif)(?:$|[?#])", lowered):
        return True
    return any(host in lowered for host in _IMAGE_HOSTS)


def looks_like_video_url(url: str) -> bool:
    lowered = url.lower()
    if looks_like_image_url(lowered):
        return False
    if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", lowered):
        return True
    return any(marker in lowered for marker in _VIDEO_MARKERS)


def media_key(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    value = normalized.split("#", 1)[0]
    if looks_like_image_url(value) or looks_like_video_url(value):
        value = value.split("?", 1)[0]
    return value.replace("http://", "https://")


def _iter_attr_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_attr_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_attr_strings(item)


def extract_image_url(tag: Tag, page_url: str) -> str | None:
    values = [
        tag.get("data-original"),
        tag.get("data-actualsrc"),
        tag.get("data-src"),
        tag.get("data-default-watermark-src"),
        tag.get("src"),
        tag.get("srcset"),
    ]
    for value in values:
        for raw in _iter_attr_strings(value):
            candidates = re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw)
            if not candidates and raw:
                candidates = [raw.split()[0]]
            for candidate in candidates:
                normalized = normalize_url(candidate, page_url)
                if normalized and looks_like_image_url(normalized):
                    return normalized
    for raw in _iter_attr_strings(tag.attrs):
        for candidate in re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw):
            normalized = normalize_url(candidate, page_url)
            if normalized and looks_like_image_url(normalized):
                return normalized
    return None


def extract_video_url(tag: Tag, page_url: str) -> tuple[str, str | None] | None:
    keys = (
        "data-url",
        "data-video-url",
        "data-src",
        "src",
        "href",
        "poster",
    )
    for key in keys:
        for raw in _iter_attr_strings(tag.get(key)):
            for candidate in re.findall(
                r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+",
                raw,
            ):
                normalized = normalize_url(candidate, page_url)
                if normalized and looks_like_video_url(normalized):
                    cover = normalize_url(tag.get("poster"), page_url)
                    return normalized, cover or None
    return None


def _link_text(tag: Tag) -> str | None:
    if tag.find("img"):
        return None
    label = clean_text(tag.get_text(" ", strip=True), keep_newlines=False)
    if not label:
        return None
    href = tag.get("href")
    if not isinstance(href, str) or not href or href.startswith("#"):
        return label
    return f"{label} ({normalize_url(href, 'https://www.zhihu.com/')})"


def _needs_separator(previous: str, current: str) -> bool:
    left = previous[-1:] if previous else ""
    right = current[:1] if current else ""
    if not left or not right:
        return False
    if left.isspace() or right.isspace():
        return False
    if left in "([{（【《“‘":
        return False
    if right in ")]}）】》”’，。！？、；：,!?;:%-/":
        return False
    return bool(
        re.match(r"[A-Za-z0-9]", left)
        and re.match(r"[A-Za-z0-9]", right)
    )


def _normalize_inline_text(value: str) -> str:
    text = value.replace("\xa0", " ").replace("\u3000", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text)


class RichContentParser:
    """按块级/行内语义提取知乎 HTML，避免每个 span 都被拆成新行。"""

    def __init__(self, page_url: str, content_type: str):
        self.page_url = page_url
        self.content_type = content_type
        self.blocks: list[dict[str, Any]] = []
        self.text_parts: list[str] = []
        self.seen_images: set[str] = set()
        self.seen_videos: set[str] = set()

    def parse(self, html_text: str) -> list[dict[str, Any]]:
        if not html_text.strip():
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        self._walk(soup)
        self._flush()
        return self.blocks

    def plain_text(self) -> str:
        return clean_text("\n\n".join(
            str(block.get("value") or "")
            for block in self.blocks
            if block.get("kind") == "text"
        ))

    def _flush(self) -> None:
        if not self.text_parts:
            return
        text = clean_text("".join(self.text_parts))
        self.text_parts.clear()
        if text:
            self.blocks.append({"kind": "text", "value": text})

    def _append_image(self, url: str | None) -> None:
        if not url:
            return
        key = media_key(url)
        if not key or key in self.seen_images:
            return
        self.seen_images.add(key)
        self._flush()
        self.blocks.append({"kind": "image", "value": url})

    def _append_video(self, url: str, cover: str | None) -> None:
        key = media_key(url)
        if not key or key in self.seen_videos:
            return
        self.seen_videos.add(key)
        self._flush()
        self.blocks.append(
            {"kind": "video", "url": url, "cover_url": cover}
        )

    def _append_video_placeholder(self, video_id: str) -> None:
        key = f"zhihu-video:{video_id}"
        if key in self.seen_videos:
            return
        self.seen_videos.add(key)
        self._flush()
        self.blocks.append(
            {
                "kind": "video_placeholder",
                "value": video_id,
                "content_type": self.content_type,
            }
        )

    def _walk(self, node: Tag | NavigableString) -> None:
        if isinstance(node, NavigableString):
            text = _normalize_inline_text(str(node))
            if not text:
                return
            if self.text_parts and _needs_separator(self.text_parts[-1], text):
                self.text_parts.append(" ")
            self.text_parts.append(text)
            return
        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if not name or name in _SKIP_TAGS:
            return
        if name == "br":
            self.text_parts.append("\n")
            return
        if name == "img":
            self._append_image(extract_image_url(node, self.page_url))
            return
        if name == "a":
            if "video-box" in (node.get("class") or []):
                lens_id = str(node.get("data-lens-id") or "").strip()
                if lens_id:
                    self._append_video_placeholder(lens_id)
                    return
            video = extract_video_url(node, self.page_url)
            if video:
                self._append_video(*video)
                return
            link = _link_text(node)
            if link:
                link = _normalize_inline_text(link)
                if self.text_parts and _needs_separator(self.text_parts[-1], link):
                    self.text_parts.append(" ")
                self.text_parts.append(link)
                return
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            heading = clean_text(node.get_text(" ", strip=True))
            if heading:
                self.blocks.append({"kind": "text", "value": heading})
            return
        if name == "blockquote":
            self._flush()
            quote = clean_text(node.get_text("\n", strip=True))
            if quote:
                quoted = "\n".join(
                    f"> {line}" if line else ">" for line in quote.splitlines()
                )
                self.blocks.append({"kind": "text", "value": quoted})
            return
        if name in {"ul", "ol"}:
            self._flush()
            lines = []
            for index, item in enumerate(node.find_all("li", recursive=False), 1):
                value = clean_text(item.get_text(" ", strip=True), keep_newlines=False)
                if value:
                    prefix = f"{index}. " if name == "ol" else "- "
                    lines.append(prefix + value)
            if lines:
                self.blocks.append({"kind": "text", "value": "\n".join(lines)})
            return
        if name == "pre":
            self._flush()
            code = node.get_text("", strip=False).strip("\n")
            if code.strip():
                language = str(
                    node.get("data-language")
                    or node.get("data-lang")
                    or ""
                ).strip()
                fence = f"```{language}" if language else "```"
                self.blocks.append(
                    {"kind": "text", "value": f"{fence}\n{code}\n```"}
                )
            return
        if name == "hr":
            self._flush()
            self.blocks.append({"kind": "text", "value": "---"})
            return

        is_block = name in _BLOCK_TAGS
        if is_block:
            self._flush()
        for child in node.children:
            self._walk(child)
        if is_block:
            self._flush()


def parse_rich_content(
    html_text: str,
    page_url: str,
    content_type: str = "answer",
) -> tuple[list[dict[str, Any]], str]:
    parser = RichContentParser(page_url, content_type)
    blocks = parser.parse(html_text)
    return blocks, parser.plain_text()


def blocks_to_contents(
    blocks: list[dict[str, Any]],
    *,
    image_loader,
) -> list[ContentItem]:
    contents: list[ContentItem] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        text = clean_text("\n\n".join(pending))
        pending.clear()
        if text:
            contents.append(TextContent(text))

    for block in blocks:
        kind = block.get("kind")
        value = str(block.get("value") or "").strip()
        if not value:
            continue
        if kind == "text":
            pending.append(value)
            continue
        if kind == "image":
            flush()
            image = image_loader(value)
            if image is not None:
                contents.append(image)
    flush()
    return contents
