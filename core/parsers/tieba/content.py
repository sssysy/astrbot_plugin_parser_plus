from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Final

from ...data import Metric
from ...utils import format_num
from .PbPageResIdl_pb2 import PbContent, PbPageResIdl, Post, SubPostList, User

IMAGE_HOSTS: Final[tuple[str, ...]] = (
    "tieba.baidu.com",
    "tiebac.baidu.com",
    "tiebapic.baidu.com",
    "tb.himg.baidu.com",
    "himg.bdimg.com",
    "hiphotos.baidu.com",
    "imgsa.baidu.com",
    "gsp0.baidu.com",
)
VIDEO_HOST = "tieba.baidu.com"
EMOJI_URL = (
    "https://gsp0.baidu.com/5aAHeD3nKhI2p27j8IqW0jdnxx1xbK/tb/editor/"
    "images/client/{name}.png"
)


@dataclass(slots=True)
class ImageRef:
    url: str
    width: int = 0
    height: int = 0


@dataclass(slots=True)
class VideoRef:
    url: str
    cover_url: str = ""
    duration: float = 0.0


@dataclass(slots=True)
class UserInfo:
    user_id: int = 0
    name: str = ""
    portrait: str = ""
    ip: str = ""

    @property
    def avatar_url(self) -> str:
        portrait = self.portrait.strip()
        if not portrait:
            return ""
        return f"https://himg.bdimg.com/sys/portraith/item/{portrait}"


@dataclass(slots=True)
class Fragment:
    kind: str
    text: str = ""
    image: ImageRef | None = None
    video: VideoRef | None = None
    url: str = ""


@dataclass(slots=True)
class CommentInfo:
    user: UserInfo
    contents: list[Fragment] = field(default_factory=list)
    timestamp: int | None = None
    like_count: int = 0
    reply_count: int = 0
    replies: list[CommentInfo] = field(default_factory=list)


@dataclass(slots=True)
class ThreadInfo:
    tid: int
    title: str
    forum_name: str
    author: UserInfo
    contents: list[Fragment]
    timestamp: int | None
    view_count: int
    reply_count: int
    like_count: int
    share_count: int
    comments: list[CommentInfo]


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    value = unescape(url).strip()
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("http://"):
        value = f"https://{value[7:]}"
    if not value.startswith("https://"):
        return False
    host = re.sub(r"^https://", "", value).split("/", 1)[0].split(":", 1)[0]
    return any(host == item or host.endswith(f".{item}") for item in hosts)


def normalize_media_url(url: str, *, hosts: tuple[str, ...]) -> str:
    value = unescape(url).strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("http://"):
        value = f"https://{value[7:]}"
    return value if _host_allowed(value, hosts) else ""


def image_size(value: str) -> tuple[int, int]:
    left, _, right = value.partition(",")
    try:
        return int(left), int(right)
    except ValueError:
        return 0, 0


def _user(payload: User) -> UserInfo:
    return UserInfo(
        user_id=int(payload.id or 0),
        name=payload.name_show or payload.name or "贴吧用户",
        portrait=payload.portrait,
        ip=payload.ip_address,
    )


def _emoji(name: str, description: str) -> Fragment:
    label = description or name
    if not name:
        return Fragment("text", text=label)
    url = EMOJI_URL.format(name=name)
    return Fragment("emoji", text=label, url=url)


def _fragment(payload: PbContent) -> Fragment | None:
    kind = int(payload.type)
    if kind in {0, 9, 18, 27, 34, 40, 52}:
        return Fragment("text", text=payload.text) if payload.text else None
    if kind in {2, 11}:
        return _emoji(payload.text, payload.c)
    if kind in {3, 20}:
        url = normalize_media_url(
            payload.origin_src or payload.big_cdn_src or payload.cdn_src or payload.src,
            hosts=IMAGE_HOSTS,
        )
        if not url:
            return None
        width, height = image_size(payload.bsize)
        if not width:
            width, height = int(payload.width or 0), int(payload.height or 0)
        return Fragment("image", image=ImageRef(url, width, height))
    if kind == 4:
        return Fragment("text", text=f"@{payload.text} ") if payload.text else None
    if kind == 1:
        url = normalize_media_url(payload.link, hosts=("tieba.baidu.com", "baidu.com"))
        label = payload.text or payload.link
        if not label and not url:
            return None
        return Fragment("link", text=label, url=url)
    if kind == 5:
        url = normalize_media_url(payload.link, hosts=(VIDEO_HOST,))
        if not url:
            return None
        return Fragment(
            "video",
            video=VideoRef(
                url=url,
                cover_url=normalize_media_url(payload.src, hosts=IMAGE_HOSTS),
                duration=float(payload.during_time or 0),
            ),
        )
    if kind in {35, 36, 37}:
        info = payload.tiebaplus_info
        label = info.desc or info.title
        url = normalize_media_url(info.jump_url, hosts=("baidu.com",))
        if not label and not url:
            return None
        return Fragment("link", text=label, url=url)
    return None


def fragments(content: PbContent | list[PbContent]) -> list[Fragment]:
    output: list[Fragment] = []
    for item in content:
        fragment = _fragment(item)
        if fragment is not None:
            output.append(fragment)
    return output


def merge_text_fragments(items: list[Fragment]) -> list[Fragment]:
    output: list[Fragment] = []
    pending: list[str] = []

    def flush() -> None:
        text = "".join(pending).strip()
        pending.clear()
        if text:
            output.append(Fragment("text", text=text))

    for item in items:
        if item.kind == "text":
            pending.append(item.text)
            continue
        if item.kind == "link":
            pending.append(item.text or item.url)
            continue
        flush()
        output.append(item)
    flush()
    return output


def _poll_lines(result: PbPageResIdl) -> list[str]:
    poll = result.data.thread.poll_info
    if not poll.options:
        return []
    title = poll.title or "投票"
    lines = [f"投票：{title}"]
    for option in poll.options:
        lines.append(f"{option.text}：{option.num} 票")
    if poll.total_num:
        lines.append(f"参与人数：{poll.total_num}")
    if poll.total_poll:
        lines.append(f"总票数：{poll.total_poll}")
    return lines


def _comment(payload: SubPostList, users: dict[int, UserInfo]) -> CommentInfo:
    user = users.get(int(payload.author_id or 0))
    if user is None:
        user = _user(payload.author)
    items = merge_text_fragments(fragments(payload.content))
    return CommentInfo(
        user=user,
        contents=items,
        timestamp=int(payload.time) or None,
        like_count=int(payload.agree.agree_num or 0),
    )


def _post_comment(payload: Post, users: dict[int, UserInfo]) -> CommentInfo:
    user = users.get(int(payload.author_id or 0))
    if user is None:
        user = _user(payload.author)
    items = merge_text_fragments(fragments(payload.content))
    replies = [
        _comment(item, users)
        for item in payload.sub_post_list.sub_post_list
        if not item.title
    ]
    return CommentInfo(
        user=user,
        contents=items,
        timestamp=int(payload.time) or None,
        like_count=int(payload.agree.agree_num or 0),
        reply_count=int(payload.sub_post_number or 0),
        replies=replies,
    )


def decode_page(result: PbPageResIdl, *, comment_limit: int) -> ThreadInfo:
    thread = result.data.thread
    users = {int(item.id): _user(item) for item in result.data.user_list if item.id}
    author = users.get(int(thread.author_id or 0))
    if author is None:
        author = _user(thread.author)
    posts = [item for item in result.data.post_list if not item.chat_content.bot_uk]
    main = next(
        (item for item in posts if item.id == thread.post_id or item.floor == 1),
        None,
    )
    if main is None:
        for item in posts:
            if int(item.author_id or 0) == int(thread.author_id or 0):
                main = item
                break
    contents = merge_text_fragments(fragments(main.content)) if main is not None else []
    if not contents and thread.first_post_content:
        contents = merge_text_fragments(fragments(thread.first_post_content))
    contents.extend(Fragment("text", text=line) for line in _poll_lines(result))
    comments: list[CommentInfo] = []
    if comment_limit > 0:
        for item in posts:
            if item is main:
                continue
            comments.append(_post_comment(item, users))
            if len(comments) >= comment_limit:
                break
    return ThreadInfo(
        tid=int(thread.id or 0),
        title=thread.title or "无标题",
        forum_name=result.data.forum.name,
        author=author,
        contents=contents,
        timestamp=int(thread.create_time) or None,
        view_count=int(result.data.thread_freq_num or 0),
        reply_count=int(thread.reply_num or 0),
        like_count=int(thread.agree.agree_num or 0),
        share_count=int(thread.share_num or 0),
        comments=comments,
    )


def build_metrics(info: ThreadInfo) -> list[Metric]:
    return [
        Metric(icon=None, label=label, value=format_num(value))
        for label, value in (
            ("浏览", info.view_count),
            ("点赞", info.like_count),
            ("回复", info.reply_count),
            ("分享", info.share_count),
        )
        if value
    ]
