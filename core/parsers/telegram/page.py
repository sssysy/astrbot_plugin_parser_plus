from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .models import (
    TelegramButton,
    TelegramForward,
    TelegramMedia,
    TelegramPoll,
    TelegramPollOption,
    TelegramPost,
    TelegramPreview,
    TelegramReaction,
    TelegramReply,
)

MEDIA_SELECTOR = ",".join(
    (
        ".tgme_widget_message_photo_wrap",
        ".tgme_widget_message_video_player",
        ".tgme_widget_message_voice_player",
        ".tgme_widget_message_roundvideo_player",
        ".tgme_widget_message_sticker_wrap",
        ".tgme_widget_message_document_wrap",
        ".tgme_widget_message_document",
        ".tgme_widget_message_audio_player",
        ".tgme_widget_message_audio",
    )
)
BACKGROUND_IMAGE_RE = re.compile(
    r"""background-image\s*:\s*url\(\s*(['"]?)(.*?)\1\s*\)""",
    re.IGNORECASE | re.DOTALL,
)
REPLY_TARGET_RE = re.compile(r"/(\d+)(?:[/?#]|$)")
COUNT_RE = re.compile(r"\d[\d.,]*(?:\s*[KMB万亿])?", re.IGNORECASE)
DIRECT_FILE_SUFFIXES = (
    ".7z",
    ".apk",
    ".doc",
    ".docx",
    ".epub",
    ".pdf",
    ".rar",
    ".tar",
    ".txt",
    ".xls",
    ".xlsx",
    ".zip",
)
TRUSTED_MEDIA_SUFFIXES = ("telesco.pe", "telegram.org", "t.me")


def parse_page_html(
    value: str,
    *,
    channel: str,
    post_id: int,
) -> TelegramPost | None:
    """Parse a public ``t.me`` post page and select the requested post."""
    parser = LexborHTMLParser(value)
    channel_info = _parse_channel_info(parser, channel)
    message = _find_message(parser, post_id)
    if message is None:
        return None
    return _parse_post(
        message,
        channel=channel,
        post_id=post_id,
        channel_info=channel_info,
    )


def _find_message(parser: LexborHTMLParser, post_id: int) -> LexborNode | None:
    for message in parser.css(".tgme_widget_message"):
        data_post = _attr(message, "data-post")
        if not data_post:
            continue
        tail = data_post.rsplit("/", 1)[-1]
        if tail == str(post_id):
            return message
    return None


def _parse_channel_info(
    parser: LexborHTMLParser,
    channel: str,
) -> dict[str, str | None]:
    owner = _first_text(parser, ".tgme_widget_message_owner_name")
    title = (
        _first_text(parser, ".tgme_channel_info_header_title")
        or owner
        or _meta(parser, "property", "og:title")
    )
    description = _first_text(parser, ".tgme_channel_info_description")
    if not description and parser.css_first(".tgme_channel_info"):
        description = _meta(parser, "property", "og:description")
    username = _first_text(parser, ".tgme_channel_info_header_username a") or ""
    if username.startswith("@"):
        username = username[1:]
    avatar = _first_image_url(
        parser,
        (
            ".tgme_widget_message_user_photo img",
            ".tgme_channel_info_header .tgme_page_photo_image img",
            ".tgme_page_photo_image img",
        ),
    )
    if not avatar:
        avatar = _first_background_url(
            parser,
            (
                ".tgme_widget_message_user_photo",
                ".tgme_channel_info_header .tgme_page_photo_image",
                ".tgme_page_photo_image",
            ),
        )
    if not avatar and parser.css_first(".tgme_channel_info"):
        avatar = _meta(parser, "property", "og:image")
    return {
        "username": username or channel,
        "title": title,
        "description": description,
        "avatar": avatar,
    }


def _parse_post(
    message: LexborNode,
    *,
    channel: str,
    post_id: int,
    channel_info: dict[str, str | None],
) -> TelegramPost:
    bubble = message.css_first(".tgme_widget_message_bubble") or message
    text = _post_text(bubble)
    media = _parse_media(bubble)
    preview = _parse_preview(message)
    reply = _parse_reply(message)
    forwarded = _parse_forward(message)
    poll = _parse_poll(bubble)
    buttons = _parse_buttons(message)
    reactions = _parse_reactions(message)
    views = _first_text(bubble, ".tgme_widget_message_views")
    meta_text = _first_text(bubble, ".tgme_widget_message_meta")
    timestamp = _parse_timestamp(
        _first_attr(bubble, ".tgme_widget_message_date time", "datetime")
    )
    owner = _first_text(message, ".tgme_widget_message_owner_name")
    signature = _first_text(message, ".tgme_widget_message_from_author")
    author = signature or owner or channel_info["title"] or channel
    unsupported = bool(
        bubble.css_first(".message_media_not_supported_wrap")
        or any(item.available is False for item in media)
    )
    return TelegramPost(
        channel=str(channel_info["username"] or channel),
        post_id=post_id,
        channel_title=str(channel_info["title"] or author),
        channel_avatar=_absolute_url(channel_info["avatar"]),
        channel_description=channel_info["description"],
        author=author,
        text=text,
        timestamp=timestamp,
        views=views,
        edited=bool(meta_text and meta_text.startswith("edited")),
        media=tuple(media),
        preview=preview,
        reply=reply,
        forwarded=forwarded,
        poll=poll,
        buttons=buttons,
        reactions=reactions,
        unsupported_media=unsupported,
    )


def _post_text(bubble: LexborNode) -> str | None:
    node = bubble.css_first(".tgme_widget_message_text.js-message_text")
    if node is None:
        for candidate in bubble.css(".tgme_widget_message_text"):
            classes = _attr(candidate, "class")
            if "js-message_reply_text" not in classes:
                node = candidate
                break
    return _clean_text(node.text(separator="\n")) if node is not None else None


def _parse_media(bubble: LexborNode) -> list[TelegramMedia]:
    media: list[TelegramMedia] = []
    seen_nodes: set[int] = set()
    seen_items: set[tuple[str, str | None]] = set()
    message = bubble
    while message is not None and "tgme_widget_message" not in _attr(
        message, "class"
    ).split():
        message = message.parent
    owner_post = _attr(message, "data-post") if message is not None else ""
    for node in bubble.css(MEDIA_SELECTOR):
        media_message = node
        while media_message is not None and "tgme_widget_message" not in _attr(
            media_message, "class"
        ).split():
            media_message = media_message.parent
        if (
            owner_post
            and media_message is not None
            and _attr(media_message, "data-post") != owner_post
        ):
            continue
        identity = id(node)
        if identity in seen_nodes:
            continue
        seen_nodes.add(identity)
        classes = _attr(node, "class").split()
        first_class = classes[0] if classes else ""
        item: TelegramMedia | None
        if first_class == "tgme_widget_message_photo_wrap":
            item = TelegramMedia(
                kind="image",
                url=_background_url(_attr(node, "style")),
            )
        elif first_class == "tgme_widget_message_video_player":
            item = _parse_video(node)
        elif first_class == "tgme_widget_message_voice_player":
            item = _parse_voice(node)
        elif first_class == "tgme_widget_message_roundvideo_player":
            item = _parse_roundvideo(node)
        elif first_class == "tgme_widget_message_sticker_wrap":
            item = _parse_sticker(node)
        elif "tgme_widget_message_document" in classes:
            item = _parse_document(node)
        elif "tgme_widget_message_audio" in classes:
            item = _parse_audio(node)
        else:
            item = None
        if item is None:
            continue
        key = (item.kind, item.url)
        if key in seen_items:
            continue
        seen_items.add(key)
        media.append(item)
    return media


def _parse_video(node: LexborNode) -> TelegramMedia:
    video = node.css_first("video.tgme_widget_message_video")
    url = _absolute_url(_attr(video, "src")) if video is not None else None
    thumb = _background_url(
        _first_attr(node, ".tgme_widget_message_video_thumb", "style")
    )
    duration = _duration(_first_text(node, "time.message_video_duration"))
    return TelegramMedia(
        kind="video" if duration else "gif",
        url=url,
        thumb=thumb,
        duration=duration,
        available=bool(url),
    )


def _parse_voice(node: LexborNode) -> TelegramMedia:
    audio = node.css_first("audio.tgme_widget_message_voice")
    return TelegramMedia(
        kind="voice",
        url=_absolute_url(_attr(audio, "src")) if audio is not None else None,
        duration=_duration(
            _first_text(node, "time.tgme_widget_message_voice_duration")
        ),
        available=audio is not None and bool(_attr(audio, "src")),
    )


def _parse_roundvideo(node: LexborNode) -> TelegramMedia:
    video = node.css_first("video.tgme_widget_message_roundvideo")
    return TelegramMedia(
        kind="roundvideo",
        url=_absolute_url(_attr(video, "src")) if video is not None else None,
        thumb=_first_background_url(
            node, (".tgme_widget_message_roundvideo_thumb",)
        ),
        duration=_duration(
            _first_text(node, "time.tgme_widget_message_roundvideo_duration")
        ),
        available=video is not None and bool(_attr(video, "src")),
    )


def _parse_sticker(node: LexborNode) -> TelegramMedia | None:
    candidates = (
        ("picture.tgme_widget_message_tgsticker source", "srcset"),
        ("i.tgme_widget_message_sticker", "data-webp"),
        ("video.js-videosticker_video", "src"),
    )
    for selector, attribute in candidates:
        source = node.css_first(selector)
        if source is None:
            continue
        raw = _attr(source, attribute)
        if attribute == "srcset":
            raw = _first_srcset_url(raw)
        url = _absolute_url(raw)
        if not url:
            continue
        path = urlparse(url).path.lower()
        kind = (
            "dynamic"
            if path.endswith((".webm", ".mp4"))
            else "file"
            if path.endswith(".tgs")
            else "image"
        )
        thumb = _first_attr(source, "img", "src")
        return TelegramMedia(
            kind=kind,
            url=url,
            thumb=_absolute_url(thumb),
        )
    return None


def _parse_document(node: LexborNode) -> TelegramMedia:
    link = node if node.tag == "a" else node.css_first("a[href]")
    if link is None:
        parent = node.parent
        while parent is not None and parent.tag != "a":
            parent = parent.parent
        link = parent
    href = _absolute_url(_attr(link, "href")) if link is not None else None
    url = href if href and _is_direct_file_url(href) else None
    name = (
        _first_text(node, ".tgme_widget_message_document_title")
        or _first_text(node, ".tgme_widget_message_document_extra")
    )
    return TelegramMedia(
        kind="file",
        url=url,
        name=name,
        available=bool(url),
    )


def _parse_audio(node: LexborNode) -> TelegramMedia | None:
    audio = node if node.tag == "audio" else node.css_first("audio[src]")
    if audio is None:
        return None
    src = _absolute_url(_attr(audio, "src"))
    if not src:
        return None
    return TelegramMedia(
        kind="audio",
        url=src,
        duration=_duration(_first_text(node, "time")),
    )


def _parse_preview(message: LexborNode) -> TelegramPreview | None:
    preview = message.css_first(".tgme_widget_message_link_preview")
    if preview is None:
        return None
    thumb = _first_background_url(
        preview,
        (
            ".link_preview_image",
            ".link_preview_right_image",
            ".link_preview_square_image",
        ),
    )
    return TelegramPreview(
        url=_absolute_url(_attr(preview, "href")),
        title=_first_text(preview, ".link_preview_title"),
        site_name=_first_text(preview, ".link_preview_site_name"),
        description=_first_text(preview, ".link_preview_description"),
        thumb=thumb,
    )


def _parse_reply(message: LexborNode) -> TelegramReply | None:
    reply = message.css_first(".tgme_widget_message_reply")
    if reply is None:
        return None
    text = (
        _first_text(reply, ".tgme_widget_message_text.js-message_reply_text")
        or _first_text(reply, ".tgme_widget_message_metatext")
        or _first_text(reply, ".tgme_widget_message_text")
    )
    name = (
        _first_text(reply, ".tgme_widget_message_author_name")
        or _first_text(reply, ".tgme_widget_message_author")
    )
    if not text:
        return None
    href = _absolute_url(_attr(reply, "href"))
    target = REPLY_TARGET_RE.search(href or "")
    return TelegramReply(
        name=name or "引用消息",
        text=text,
        url=href,
        to_message=int(target.group(1)) if target else None,
        thumb=_first_background_url(
            reply, (".tgme_widget_message_reply_thumb",)
        ),
    )


def _parse_forward(message: LexborNode) -> TelegramForward | None:
    container = message.css_first(".tgme_widget_message_forwarded_from")
    if container is None:
        return None
    forwarded = container.css_first(".tgme_widget_message_forwarded_from_name")
    name = _clean_text(forwarded.text()) if forwarded is not None else None
    url = _absolute_url(_attr(forwarded, "href")) if forwarded is not None else None
    url = url or _absolute_url(_attr(container, "href"))
    if not name:
        # 嵌入页有时只留下来源链接，名称被延迟加载或隐藏。
        matched = re.search(
            r"(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][A-Za-z0-9_]{2,31})",
            url or "",
            re.IGNORECASE,
        )
        name = matched.group(1) if matched else "未知来源"
    return TelegramForward(
        name=name,
        url=url,
        author=_first_text(
            message, ".tgme_widget_message_forwarded_from_author"
        ),
    )


def _parse_poll(bubble: LexborNode) -> TelegramPoll | None:
    poll = bubble.css_first(".tgme_widget_message_poll")
    if poll is None:
        return None
    options: list[TelegramPollOption] = []
    for option in poll.css(".tgme_widget_message_poll_option"):
        name = _first_text(option, ".tgme_widget_message_poll_option_text")
        percent = _first_text(
            option, ".tgme_widget_message_poll_option_percent"
        )
        if not name or not percent:
            continue
        matched = re.search(r"\d+", percent)
        if matched:
            options.append(
                TelegramPollOption(
                    name=name,
                    percent=int(matched.group(0)),
                )
            )
    return TelegramPoll(
        question=_first_text(poll, ".tgme_widget_message_poll_question") or "",
        kind=_first_text(poll, ".tgme_widget_message_poll_type"),
        votes=_first_text(bubble, ".tgme_widget_message_voters"),
        options=tuple(options),
    )


def _parse_buttons(message: LexborNode) -> tuple[TelegramButton, ...]:
    buttons: list[TelegramButton] = []
    for button in message.css(".tgme_widget_message_inline_button"):
        title = _first_text(button, "span")
        if not title:
            continue
        buttons.append(
            TelegramButton(
                title=title,
                url=_absolute_url(_attr(button, "href")),
            )
        )
    return tuple(buttons)


def _parse_reactions(message: LexborNode) -> tuple[TelegramReaction, ...]:
    reactions: list[TelegramReaction] = []
    for node in message.css(".tgme_widget_message_reactions .tgme_reaction"):
        full_text = _clean_text(node.text())
        count = _last_child_text(node)
        if not count:
            matched = COUNT_RE.search(full_text)
            count = matched.group(0) if matched else ""
        if not count:
            continue
        classes = _attr(node, "class")
        if "tgme_reaction_paid" in classes:
            label, kind = "⭐", "telegram_stars"
        elif node.css_first("i.emoji b") is not None:
            label, kind = (
                _clean_text(node.css_first("i.emoji b").text()) or "表情",
                "emoji",
            )
        elif node.css_first("tg-emoji") is not None:
            emoji = node.css_first("tg-emoji")
            label, kind = _clean_text(emoji.text()) or "表情", "custom_emoji"
        else:
            label, kind = "互动", "unknown"
        reactions.append(
            TelegramReaction(
                label=label,
                count=count,
                kind=kind,
            )
        )
    return tuple(reactions)


def _duration(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parts = [int(part) for part in value.strip().split(":")]
    except ValueError:
        return 0.0
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    if len(parts) == 3:
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    return 0.0


def _parse_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = (
        html.unescape(value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u200b", "")
        .replace("\xa0", " ")
    )
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    cleaned = text.strip()
    return cleaned or None


def _first_text(node: LexborNode, selector: str) -> str | None:
    matched = node.css_first(selector)
    return _clean_text(matched.text()) if matched is not None else None


def _first_attr(
    node: LexborNode,
    selector: str,
    attribute: str,
) -> str | None:
    matched = node.css_first(selector)
    return _attr(matched, attribute) if matched is not None else None


def _attr(node: LexborNode | None, name: str) -> str:
    if node is None:
        return ""
    value = node.attributes.get(name)
    return value.strip() if isinstance(value, str) else ""


def _meta(parser: LexborHTMLParser, attribute: str, value: str) -> str | None:
    for node in parser.css("meta"):
        if _attr(node, attribute) == value:
            return _clean_text(_attr(node, "content"))
    return None


def _first_image_url(
    node: LexborNode | LexborHTMLParser,
    selectors: tuple[str, ...],
) -> str | None:
    for selector in selectors:
        matched = node.css_first(selector)
        if matched is not None:
            url = _absolute_url(_attr(matched, "src"))
            if url:
                return url
    return None


def _first_background_url(
    node: LexborNode,
    selectors: tuple[str, ...],
) -> str | None:
    for selector in selectors:
        matched = node.css_first(selector)
        if matched is not None:
            url = _background_url(_attr(matched, "style"))
            if url:
                return url
    return None


def _background_url(style: str | None) -> str | None:
    if not style:
        return None
    matched = BACKGROUND_IMAGE_RE.search(style)
    return _absolute_url(matched.group(2)) if matched else None


def _first_srcset_url(value: str) -> str:
    candidate = value.split(",", 1)[0].strip()
    return candidate.split(" ", 1)[0] if candidate else ""


def _absolute_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = html.unescape(value).strip()
    if text.startswith("//"):
        text = f"https:{text}"
    if not text.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    return text


def _is_direct_file_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in TRUSTED_MEDIA_SUFFIXES
    ):
        return False
    path = parsed.path.lower()
    if path.startswith("/file/"):
        return True
    return path.endswith(DIRECT_FILE_SUFFIXES)


def _last_child_text(node: LexborNode) -> str | None:
    child = node.last_child
    return _clean_text(child.text()) if child is not None else None
