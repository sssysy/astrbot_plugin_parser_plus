from __future__ import annotations

import html
import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from bs4.element import Comment, NavigableString, PageElement

_NEWLINE_TAGS = frozenset({"br", "p", "div", "li", "blockquote"})
_TRAILING_SHORT_URL = re.compile(r"\s*https?://t\.cn/\S+\s*$")
_WHITESPACE = re.compile(r"[ \t\f\v]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_BROKEN_NBSP = re.compile(r"(?m)^\s*xa0\s*$")


def _attr(value: str | list[str] | None) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _unescape(value: str) -> str:
    for _ in range(2):
        unescaped = html.unescape(value)
        if unescaped == value:
            break
        value = unescaped
    return value


def html_to_text(value: str) -> str:
    """把微博正文 HTML 转成保留段落与表情文本的纯文本。"""
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    parts: list[str] = []

    def walk(node: BeautifulSoup | PageElement) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            parts.append(_unescape(str(node)))
            return
        if not isinstance(node, Tag):
            return
        if node.name in _NEWLINE_TAGS:
            parts.append("\n")
        if node.name == "img":
            parts.append(_attr(node.get("alt")))
            return
        if node.name == "a":
            label = node.get_text(" ", strip=True)
            href = _attr(node.get("href"))
            if label:
                parts.append(label)
            elif href:
                parts.append(href)
            return
        for child in node.children:
            walk(child)

    walk(soup)
    text = "".join(parts).replace("\u200b", "").replace("\xa0", " ")
    text = _BROKEN_NBSP.sub("", text)
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _MULTI_NEWLINE.sub("\n\n", text).strip()


def clean_status_text(html_text: str, raw_text: str) -> str:
    """优先清洗 HTML 正文，缺失时回退到 text_raw 并去掉末尾短链。"""
    text = html_to_text(html_text) or html_to_text(raw_text)
    return _TRAILING_SHORT_URL.sub("", text).strip()


def parse_article_blocks(content: str) -> tuple[list[tuple[str, str | None]], str]:
    """把微博专栏 HTML 转成按出现顺序排列的文字/图片块。"""
    soup = BeautifulSoup(content or "", "html.parser")
    blocks: list[tuple[str, str | None]] = []
    pending: list[str] = []

    def flush() -> None:
        text = html_to_text("<br>".join(pending))
        pending.clear()
        if text:
            blocks.append(("text", text))

    for element in soup.find_all(["p", "img"]):
        if not isinstance(element, Tag):
            continue
        if element.name == "p":
            text = html_to_text(str(element))
            if text:
                pending.append(text)
            continue
        src = _attr(element.get("src") or element.get("data-src"))
        if not src:
            continue
        if src.startswith("//"):
            src = f"https:{src}"
        flush()
        blocks.append(("image", src))
    flush()
    text = "\n\n".join(value for kind, value in blocks if kind == "text" and value)
    return blocks, text


def comment_text(value: Any) -> str:
    return html_to_text(str(value or ""))
