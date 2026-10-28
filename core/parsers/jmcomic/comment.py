from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...data import Author, Comment, TextContent
from ...utils import format_num


def comment_limit(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 5
    return max(0, min(limit, 20))


def decode_comments(
    comments: Iterable[Any],
    *,
    limit: int,
) -> list[Comment]:
    """把 jmcomic 评论实体转换为卡片评论，只保留一层回复。"""
    roots = list(comments)[: max(0, limit)]
    return [_build_comment(item) for item in roots]


def _build_comment(
    item: Any,
    *,
    parent_author: Author | None = None,
) -> Comment:
    name = str(getattr(item, "nickname", "") or "").strip()
    username = str(getattr(item, "username", "") or "").strip()
    user_id = str(getattr(item, "user_id", "") or "").strip()
    content = str(getattr(item, "content", "") or "").strip()
    spoiler = bool(getattr(item, "is_spoiler", False))
    if spoiler:
        content = f"[剧透] {content}".strip()

    author = Author(
        name=name or username or "JM 用户",
        uid=user_id or username or None,
    )
    replies = (
        [
            _build_comment(reply, parent_author=author)
            for reply in (getattr(item, "replies", None) or ())[:5]
        ]
        if parent_author is None
        else []
    )
    likes = getattr(item, "likes", None)
    return Comment(
        author=author,
        contents=[TextContent(content)] if content else [],
        timestamp=_parse_timestamp(getattr(item, "created_at", None)),
        like_count=format_num(likes) if likes is not None else None,
        reply_count=format_num(len(replies)) if replies else None,
        replies=replies,
        parent_author=parent_author,
    )


def _parse_timestamp(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None
