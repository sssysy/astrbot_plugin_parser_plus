from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import msgspec

from ...data import Author, Comment, ContentItem, ImageContent, TextContent
from ...utils import format_num

AvatarFactory = Callable[[str], object]

_EMOTE_RE = re.compile(r"\[[^\[\]\n]{1,32}\]")
_LOCATION_PREFIX = "IP属地："


def comment_limit(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 5
    return max(0, min(limit, 20))


class BiliCommentMember(msgspec.Struct):
    mid: str = ""
    uname: str = ""
    name: str = ""
    avatar: str = ""
    face: str = ""


class BiliCommentContent(msgspec.Struct):
    message: str = ""
    emote: dict[str, dict[str, Any]] | None = None
    pictures: list[dict[str, Any]] | None = None


class BiliReplyControl(msgspec.Struct):
    location: str = ""
    time_desc: str = ""
    sub_reply_entry_text: str = ""


class BiliCommentItem(msgspec.Struct):
    rpid: int | str = 0
    id: int | str = 0
    like: int | str = 0
    count: int | str = 0
    rcount: int | str = 0
    ctime: int | str = 0
    member: BiliCommentMember = msgspec.field(
        default_factory=BiliCommentMember
    )
    content: BiliCommentContent = msgspec.field(
        default_factory=BiliCommentContent
    )
    replies: list["BiliCommentItem"] | None = None
    reply_control: BiliReplyControl = msgspec.field(
        default_factory=BiliReplyControl
    )
    parent_reply_member: dict[str, Any] | None = None


class BiliCommentData(msgspec.Struct):
    replies: list[BiliCommentItem] | None = None
    top_replies: list[BiliCommentItem] | None = None
    top: dict[str, Any] | None = None


class BiliCommentReply(msgspec.Struct):
    code: int = 0
    message: str = ""
    data: BiliCommentData | None = None


def decode_comment_payload(payload: dict[str, Any]) -> BiliCommentData:
    if "code" not in payload and "replies" in payload:
        # 兼容旧版 grpc-gateway 的裸 data 响应结构。
        return msgspec.convert(payload, type=BiliCommentData)
    reply = msgspec.convert(payload, type=BiliCommentReply)
    if reply.code != 0:
        raise ValueError(reply.message or "B站评论接口返回错误")
    return reply.data or BiliCommentData()


def decode_comments(
    payload: dict[str, Any],
    *,
    limit: int,
    avatar_factory: Callable[[str], object] | None = None,
) -> list[Comment]:
    data = decode_comment_payload(payload)
    roots: list[BiliCommentItem] = []
    seen: set[str] = set()

    top = data.top.get("upper") if isinstance(data.top, dict) else None
    if isinstance(top, dict):
        top_comment = msgspec.convert(top, type=BiliCommentItem)
        _append_unique(roots, top_comment, seen)
    for item in (*(data.top_replies or ()), *(data.replies or ())):
        _append_unique(roots, item, seen)

    return [
        _build_comment(item, avatar_factory=avatar_factory)
        for item in roots[: max(0, limit)]
    ]


def _append_unique(
    output: list[BiliCommentItem],
    item: BiliCommentItem,
    seen: set[str],
) -> bool:
    key = str(item.rpid or item.id or "")
    if not key or key in seen:
        return False
    seen.add(key)
    output.append(item)
    return True


def _build_comment(
    item: BiliCommentItem,
    *,
    avatar_factory: Callable[[str], object] | None,
) -> Comment:
    reply_count = _number(item.rcount) or _number(item.count)
    author = Author(
        name=item.member.uname or item.member.name or "哔哩哔哩用户",
        avatar=(
            avatar_factory(item.member.avatar or item.member.face)
            if (item.member.avatar or item.member.face) and avatar_factory is not None
            else None
        ),
        uid=item.member.mid or None,
    )
    contents = _comment_contents(item.content, avatar_factory=avatar_factory)
    replies = [
        _build_reply(reply, parent=author, avatar_factory=avatar_factory)
        for reply in (item.replies or ())[:5]
    ]
    return Comment(
        author=author,
        contents=contents,
        timestamp=_number(item.ctime) or None,
        location=_location(item.reply_control),
        like_count=format_num(_number(item.like)),
        reply_count=format_num(reply_count),
        replies=replies,
    )


def _build_reply(
    item: BiliCommentItem,
    *,
    parent: Author,
    avatar_factory: Callable[[str], object] | None,
) -> Comment:
    author = Author(
        name=item.member.uname or item.member.name or "哔哩哔哩用户",
        avatar=(
            avatar_factory(item.member.avatar or item.member.face)
            if (item.member.avatar or item.member.face) and avatar_factory is not None
            else None
        ),
        uid=item.member.mid or None,
    )
    return Comment(
        author=author,
        contents=_comment_contents(item.content, avatar_factory=avatar_factory),
        timestamp=_number(item.ctime) or None,
        location=_location(item.reply_control),
        like_count=format_num(_number(item.like)),
        reply_count=format_num(_number(item.rcount) or _number(item.count)),
        parent_author=parent,
    )


def _comment_contents(
    content: BiliCommentContent,
    *,
    avatar_factory: Callable[[str], object] | None,
) -> list[ContentItem]:
    message = _replace_emotes(content.message, content.emote or {})
    output: list[ContentItem] = []
    if message:
        output.append(TextContent(message))
    for picture in content.pictures or ():
        if not isinstance(picture, dict):
            continue
        url = str(picture.get("img_src") or "").strip()
        if not url:
            continue
        output.append(_image_content(url, avatar_factory=avatar_factory))
    return output


def _replace_emotes(
    message: str,
    emotes: dict[str, dict[str, Any]],
) -> str:
    if not message or not emotes:
        return message.strip()

    tokens = sorted(
        (
            (key, str(value.get("text") or key))
            for key, value in emotes.items()
            if key and isinstance(value, dict)
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    if not tokens:
        return message.strip()

    def replacement(match: re.Match[str]) -> str:
        token = match.group(0)
        for _, text in tokens:
            if token == text:
                return text
        return token

    return _EMOTE_RE.sub(replacement, message).strip()


def _image_content(
    url: str,
    *,
    avatar_factory: Callable[[str], object] | None,
) -> ImageContent:
    content = ImageContent()
    if avatar_factory is None:
        return content

    async def load() -> Any:
        result = avatar_factory(url)
        if hasattr(result, "__await__"):
            return await result
        return result

    content.set_loader(load)
    return content


def _location(control: BiliReplyControl) -> str | None:
    value = control.location.strip()
    if not value:
        return None
    return value.removeprefix(_LOCATION_PREFIX).strip() or None


def _number(value: int | str | None) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(float(value)))
        except ValueError:
            return 0
    return 0
