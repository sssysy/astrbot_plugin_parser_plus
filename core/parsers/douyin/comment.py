import json
from asyncio import Task
from collections.abc import Callable
from pathlib import Path

import msgspec
from msgspec import Struct, field

from ...data import (
    Author,
    Comment as CardComment,
    ContentItem,
    ImageContent,
    TextContent,
)
from ...utils import format_num

AvatarFactory = Callable[[str], Task[Path]]


def comment_limit(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 5
    return max(0, min(limit, 20))


class UrlList(Struct):
    url_list: list[str] = field(default_factory=list)


class Image(Struct):
    uri: str = ""
    url_list: list[str] = field(default_factory=list)


class ImageList(Struct):
    origin_url: Image = field(default_factory=Image)


class Sticker(Struct):
    static_url: Image = field(default_factory=Image)


class CommentUser(Struct):
    uid: str = ""
    nickname: str = ""
    avatar_thumb: UrlList = field(default_factory=UrlList)


class Comment(Struct):
    cid: str = ""
    aweme_id: str = ""
    digg_count: int | str = 0
    text: str = ""
    user: CommentUser = field(default_factory=CommentUser)
    create_time: int = 0
    image_list: list[ImageList] | None = None
    sticker: Sticker | None = None
    ip_label: str | None = None
    reply_comment_total: int | str = 0


class CommentResponse(Struct):
    comments: list[Comment] | None = None


def decode_comments(
    payload: bytes | str,
    *,
    limit: int | None = None,
    avatar_factory: AvatarFactory | None = None,
) -> list[CardComment]:
    if isinstance(payload, bytes):
        raw = msgspec.json.decode(payload)
    else:
        raw = json.loads(payload)
    response = msgspec.convert(raw, type=CommentResponse)
    comments = response.comments or []
    if limit is not None:
        comments = comments[: max(0, limit)]
    return [
        _to_card_comment(item, avatar_factory=avatar_factory)
        for item in comments
    ]


def _to_card_comment(
    item: Comment,
    *,
    avatar_factory: AvatarFactory | None,
) -> CardComment:
    avatar_url = (
        item.user.avatar_thumb.url_list[0]
        if item.user.avatar_thumb.url_list
        else None
    )
    author = Author(
        name=item.user.nickname or "抖音用户",
        avatar=(
            avatar_factory(avatar_url)
            if avatar_url and avatar_factory is not None
            else None
        ),
        uid=item.user.uid or None,
    )
    contents: list[ContentItem] = []
    text = item.text.replace("[图片表情]", "").strip()
    if text:
        contents.append(TextContent(text))
    for image in item.image_list or ():
        if image.origin_url.url_list:
            contents.append(_image(image.origin_url.url_list[-1], avatar_factory))
    if item.sticker and item.sticker.static_url.url_list:
        contents.append(
            _image(item.sticker.static_url.url_list[-1], avatar_factory)
        )
    return CardComment(
        author=author,
        contents=contents,
        timestamp=item.create_time or None,
        location=item.ip_label or None,
        like_count=format_num(int(item.digg_count or 0)),
        reply_count=format_num(int(item.reply_comment_total or 0)),
    )


def _image(url: str, avatar_factory: AvatarFactory | None) -> ImageContent:
    content = ImageContent()
    if avatar_factory is not None:
        content.path_task = avatar_factory(url)
    return content


def comment_summary(comment: Comment) -> dict[str, str]:
    """提供给后续评论卡片使用的稳定摘要，不影响主解析链路。"""
    return {
        "author": comment.user.nickname,
        "text": comment.text,
        "likes": format_num(int(comment.digg_count or 0)),
        "replies": format_num(int(comment.reply_comment_total or 0)),
        "location": comment.ip_label or "",
    }
