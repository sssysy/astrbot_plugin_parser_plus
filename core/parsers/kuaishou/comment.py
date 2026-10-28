from __future__ import annotations

from asyncio import Task
from collections.abc import Callable
import json
from pathlib import Path

import msgspec

from ...data import Author, Comment, TextContent
from ...utils import format_num

AvatarFactory = Callable[[str], Task[Path]]


class CommentItem(msgspec.Struct):
    content: str = ""
    timestamp: int = 0
    liked_count: int | str = msgspec.field(default=0, name="likedCount")
    comment_id: int | str = 0
    head_url: str = msgspec.field(default="", name="headurl")
    author_name: str = msgspec.field(default="", name="author_name")
    user_id: int | str | None = msgspec.field(default=None, name="user_id")
    sub_comment_count: int | str = msgspec.field(
        default=0, name="subCommentCount"
    )
    author_area: str | None = msgspec.field(default=None, name="authorArea")


class SubCommentList(msgspec.Struct):
    comments: list[CommentItem] = msgspec.field(
        name="subComments", default_factory=list
    )


class CommentList(msgspec.Struct):
    sub_comments_map: dict[str, SubCommentList] = msgspec.field(
        name="subCommentsMap", default_factory=dict
    )
    root_comments: list[CommentItem] = msgspec.field(
        name="rootComments", default_factory=list
    )


def _number(value: int | str | None) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _build_comment(
    item: CommentItem,
    *,
    include_reply_count: bool = False,
    avatar_factory: AvatarFactory | None = None,
) -> Comment:
    author = Author(
        name=item.author_name or "未知用户",
        avatar=avatar_factory(item.head_url)
        if item.head_url and avatar_factory is not None
        else None,
        uid=str(item.user_id) if item.user_id is not None else None,
    )
    return Comment(
        author=author,
        contents=[TextContent(item.content)] if item.content else [],
        timestamp=item.timestamp // 1000 if item.timestamp else None,
        location=item.author_area,
        like_count=format_num(_number(item.liked_count)),
        reply_count=(
            format_num(_number(item.sub_comment_count))
            if include_reply_count
            else None
        ),
    )


def decode_comments(
    payload: str | bytes,
    *,
    limit: int | None = None,
    avatar_factory: AvatarFactory | None = None,
) -> list[Comment]:
    if isinstance(payload, str):
        raw: object = json.loads(payload)
    else:
        raw = msgspec.json.decode(payload)
    data = msgspec.convert(raw, type=CommentList)
    roots = data.root_comments
    if limit is not None:
        roots = roots[: max(0, limit)]
    result: list[Comment] = []
    for root in roots:
        comment = _build_comment(
            root,
            include_reply_count=True,
            avatar_factory=avatar_factory,
        )
        replies = data.sub_comments_map.get(str(root.comment_id))
        if replies is not None:
            comment.replies.extend(
                _build_comment(reply, avatar_factory=avatar_factory)
                for reply in replies.comments
            )
        result.append(comment)
    return result
