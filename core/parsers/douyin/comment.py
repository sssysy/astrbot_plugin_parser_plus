import json

import msgspec
from msgspec import Struct, field

from ...utils import format_num


class UrlList(Struct):
    url_list: list[str] = field(default_factory=list)


class Image(Struct):
    uri: str = ""
    url_list: list[str] = field(default_factory=list)


class ImageList(Struct):
    origin_url: Image = field(default_factory=Image)


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
    ip_label: str | None = None
    reply_comment_total: int | str = 0


class CommentResponse(Struct):
    comments: list[Comment] | None = None


def decode_comments(payload: bytes | str) -> list[Comment]:
    raw = json.loads(payload) if isinstance(payload, str) else payload
    response = msgspec.convert(raw, type=CommentResponse)
    return response.comments or []


def comment_summary(comment: Comment) -> dict[str, str]:
    """提供给后续评论卡片使用的稳定摘要，不影响主解析链路。"""
    return {
        "author": comment.user.nickname,
        "text": comment.text,
        "likes": format_num(int(comment.digg_count or 0)),
        "replies": format_num(int(comment.reply_comment_total or 0)),
        "location": comment.ip_label or "",
    }
