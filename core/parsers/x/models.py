from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_MEDIA_TRAILING_URL_RE = re.compile(
    r"(?:\s*https?://(?:t\.co|pic\.x\.com)/\S+)+\s*$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"[ \t]+\n")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _unwrap_entry(value: Any) -> dict[str, Any]:
    entry = _dict(value)
    result = _dict(entry.get("result"))
    tweet = result.get("tweet")
    if isinstance(tweet, dict):
        return tweet
    return result


@dataclass(frozen=True, slots=True)
class XAuthor:
    name: str
    screen_name: str
    uid: str
    avatar_url: str
    description: str

    @property
    def url(self) -> str:
        return f"https://x.com/{self.screen_name}" if self.screen_name else ""


@dataclass(frozen=True, slots=True)
class XVideoVariant:
    url: str
    content_type: str
    bitrate: int


@dataclass(frozen=True, slots=True)
class XMedia:
    kind: str
    source_url: str
    cover_url: str
    variants: tuple[XVideoVariant, ...]
    duration: float

    @property
    def mp4_variants(self) -> tuple[XVideoVariant, ...]:
        return tuple(
            item
            for item in self.variants
            if item.content_type == "video/mp4" and item.url
        )

    @property
    def best_variant(self) -> XVideoVariant | None:
        variants = self.mp4_variants
        return max(variants, key=lambda item: item.bitrate) if variants else None


@dataclass(frozen=True, slots=True)
class XPollChoice:
    label: str
    votes: int
    image_url: str


@dataclass(frozen=True, slots=True)
class XPoll:
    choices: tuple[XPollChoice, ...]
    closed: bool
    close_at: str

    @property
    def total_votes(self) -> int:
        return sum(max(choice.votes, 0) for choice in self.choices)


@dataclass(frozen=True, slots=True)
class XLinkCard:
    url: str
    title: str
    site_name: str
    description: str
    preview_url: str


@dataclass(frozen=True, slots=True)
class XArticleBlock:
    kind: str
    text: str = ""
    media: XMedia | None = None


@dataclass(frozen=True, slots=True)
class XTweet:
    rest_id: str
    author: XAuthor
    text: str
    lang: str
    timestamp: int
    source_url: str
    media: tuple[XMedia, ...]
    title: str
    article_cover_url: str
    article_blocks: tuple[XArticleBlock, ...]
    card: XLinkCard | None
    poll: XPoll | None
    quoted: XTweet | None
    possibly_sensitive: bool
    view_count: int
    like_count: int
    reply_count: int
    bookmark_count: int
    quote_count: int
    retweet_count: int
    translated_text: str
    translated_lang: str

    @property
    def is_article(self) -> bool:
        return bool(self.article_blocks) or bool(self.article_cover_url)

    @property
    def has_content(self) -> bool:
        return bool(
            self.text
            or self.media
            or self.article_blocks
            or self.article_cover_url
            or self.poll
        )


def _first_media_url(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("//"):
            return f"https:{text}"
        return text if text.startswith(("http://", "https://")) else ""
    if isinstance(value, dict):
        return _first_media_url(value.get("url") or value.get("src"))
    return ""


def _original_image_url(value: str) -> str:
    """请求 pbs.twimg.com 原图；其他主机保持接口返回值。"""
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.hostname != "pbs.twimg.com":
        return value
    if parsed.path.endswith(":orig"):
        return value
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["name"] = "orig"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _image_url(media: dict[str, Any]) -> str:
    image = _original_image_url(_string(media.get("media_url_https")))
    if image:
        return image
    original = _dict(media.get("original_info"))
    return _original_image_url(_string(original.get("url")))


def _video_variants(media: dict[str, Any]) -> tuple[XVideoVariant, ...]:
    variants: list[XVideoVariant] = []
    for item in _list(_dict(media.get("video_info")).get("variants")):
        payload = _dict(item)
        url = _string(payload.get("url"))
        content_type = _string(payload.get("content_type"))
        if not url or not content_type:
            continue
        variants.append(
            XVideoVariant(
                url=url,
                content_type=content_type,
                bitrate=_int(payload.get("bitrate")),
            )
        )
    return tuple(variants)


def _media(value: dict[str, Any]) -> XMedia | None:
    kind = _string(value.get("type")).lower()
    cover = _image_url(value)
    video_info = _dict(value.get("video_info"))
    if kind == "photo":
        if not cover:
            return None
        return XMedia(
            kind="photo", source_url=cover, cover_url=cover, variants=(), duration=0
        )
    if kind in {"video", "animated_gif"}:
        variants = _video_variants(value)
        source = next(
            (
                item.url
                for item in sorted(
                    variants, key=lambda item: item.bitrate, reverse=True
                )
                if item.content_type == "video/mp4"
            ),
            "",
        )
        if not source and not cover:
            return None
        return XMedia(
            kind=kind,
            source_url=source or cover,
            cover_url=cover,
            variants=variants,
            duration=max(_int(video_info.get("duration_millis")), 0) / 1000,
        )
    return None


def _medias(tweet: dict[str, Any]) -> tuple[XMedia, ...]:
    legacy = _dict(tweet.get("legacy"))
    extended = _dict(legacy.get("extended_entities"))
    entities = _dict(legacy.get("entities"))
    raw = _list(extended.get("media")) or _list(entities.get("media"))
    result: list[XMedia] = []
    seen: set[str] = set()
    for item in raw:
        payload = _dict(item)
        media = _media(payload)
        if media is None:
            continue
        key = media.source_url or media.cover_url
        if key in seen:
            continue
        seen.add(key)
        result.append(media)
    return tuple(result)


def _author(tweet: dict[str, Any]) -> XAuthor:
    user = _dict(_dict(_dict(tweet.get("core")).get("user_results")).get("result"))
    core = _dict(user.get("core"))
    legacy = _dict(user.get("legacy"))
    avatar = _dict(user.get("avatar"))
    avatar_url = _string(avatar.get("image_url")).replace("_normal", "_bigger")
    return XAuthor(
        name=_string(core.get("name")) or "X 用户",
        screen_name=_string(core.get("screen_name")),
        uid=_string(user.get("rest_id")),
        avatar_url=avatar_url,
        description=_string(
            _dict(user.get("profile_bio")).get("description")
            or legacy.get("description")
        ),
    )


def _clean_text(value: str) -> str:
    text = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _MEDIA_TRAILING_URL_RE.sub("", text)
    text = _WHITESPACE_RE.sub("\n", text)
    return text.strip()


def _utf16_offset_to_index(text: str, offset: int) -> int:
    """把 Article 的 UTF-16 偏移量转换为 Python 字符串下标。"""
    offset = max(offset, 0)
    units = 0
    for index, char in enumerate(text):
        if offset <= units:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _legacy_text(tweet: dict[str, Any]) -> str:
    legacy = _dict(tweet.get("legacy"))
    text = _string(legacy.get("full_text"))
    if not text:
        return ""
    display = _list(legacy.get("display_text_range"))
    if len(display) == 2:
        try:
            start, end = int(display[0]), int(display[1])
            if 0 <= start <= end <= len(text):
                text = text[start:end]
        except (TypeError, ValueError):
            pass
    return _clean_text(text)


def _note_text(tweet: dict[str, Any]) -> str:
    note = _dict(tweet.get("note_tweet"))
    results = _dict(note.get("note_tweet_results"))
    result = _dict(results.get("result"))
    return _clean_text(_string(result.get("text")))


def _timestamp(tweet: dict[str, Any]) -> int:
    value = _string(_dict(tweet.get("legacy")).get("created_at"))
    if not value:
        return 0
    try:
        return int(datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except ValueError:
        return 0


def _string_value(values: dict[str, dict[str, Any]], key: str) -> str:
    return _string(_dict(values.get(key)).get("string_value"))


def _image_value(values: dict[str, dict[str, Any]], *keys: str) -> str:
    for key in keys:
        payload = _dict(values.get(key))
        url = _first_media_url(payload.get("image_value"))
        if url:
            return _original_image_url(url)
    return ""


def _poll(tweet: dict[str, Any]) -> XPoll | None:
    legacy_card = _dict(_dict(tweet.get("card")).get("legacy"))
    values = {
        key: _dict(item.get("value"))
        for item in _list(legacy_card.get("binding_values"))
        if (key := _string(_dict(item).get("key")))
    }
    if "choice1_label" not in values:
        return None
    count = _int(_string_value(values, "choice_count"))
    if count <= 0:
        while f"choice{count + 1}_label" in values:
            count += 1
    choices: list[XPollChoice] = []
    for index in range(1, count + 1):
        label = _string_value(values, f"choice{index}_label")
        if not label:
            continue
        choices.append(
            XPollChoice(
                label=label,
                votes=_int(_string_value(values, f"choice{index}_count")),
                image_url=_image_value(
                    values,
                    f"choice{index}_image_original",
                    f"choice{index}_image_x_large",
                    f"choice{index}_image_large",
                    f"choice{index}_image",
                ),
            )
        )
    if not choices:
        return None
    end_at = _string_value(values, "end_datetime_utc")
    if end_at:
        try:
            end_at = (
                datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
        except ValueError:
            pass
    final = _dict(values.get("counts_are_final")).get("boolean_value")
    return XPoll(choices=tuple(choices), closed=bool(final), close_at=end_at)


def _link_card(tweet: dict[str, Any]) -> XLinkCard | None:
    if _poll(tweet) is not None:
        return None
    card = _dict(_dict(tweet.get("card")).get("legacy"))
    values = {
        key: _dict(item.get("value"))
        for item in _list(card.get("binding_values"))
        if (key := _string(_dict(item).get("key")))
    }
    url = _string_value(values, "card_url") or _string(card.get("url"))
    if not url:
        return None
    legacy = _dict(tweet.get("legacy"))
    urls = _list(_dict(legacy.get("entities")).get("urls"))
    expanded = next(
        (
            _string(_dict(item).get("expanded_url"))
            for item in urls
            if _string(_dict(item).get("url")) == url
            and _string(_dict(item).get("expanded_url"))
        ),
        "",
    )
    url = expanded or url
    title = _string_value(values, "title")
    site_name = _string_value(values, "vanity_url") or _string_value(values, "domain")
    return XLinkCard(
        url=url,
        title=title or site_name or url,
        site_name=site_name,
        description=_string_value(values, "description"),
        preview_url=_image_value(
            values,
            "thumbnail_image_original",
            "summary_photo_image_original",
            "photo_image_full_size_original",
            "thumbnail_image_large",
            "summary_photo_image_large",
            "photo_image_full_size_large",
        ),
    )


def _article_media(media: dict[str, Any]) -> XMedia | None:
    info = _dict(media.get("media_info"))
    preview = _dict(_dict(info.get("preview_image")))
    cover = _original_image_url(
        _string(info.get("original_img_url"))
        or _string(preview.get("original_img_url"))
    )
    variants = tuple(
        XVideoVariant(
            url=_string(_dict(item).get("url")),
            content_type=_string(_dict(item).get("content_type")),
            bitrate=_int(_dict(item).get("bit_rate")),
        )
        for item in _list(info.get("variants"))
        if _string(_dict(item).get("url"))
    )
    source = next(
        (
            item.url
            for item in sorted(variants, key=lambda item: item.bitrate, reverse=True)
            if item.content_type == "video/mp4"
        ),
        cover,
    )
    if not source:
        return None
    return XMedia(
        kind="video"
        if any(item.content_type == "video/mp4" for item in variants)
        else "photo",
        source_url=source,
        cover_url=cover,
        variants=variants,
        duration=max(_int(info.get("duration_millis")), 0) / 1000,
    )


def _article_blocks(
    tweet: dict[str, Any],
) -> tuple[tuple[XArticleBlock, ...], str, str]:
    article = _dict(tweet.get("article"))
    result = _dict(_dict(article.get("article_results")).get("result"))
    if not result:
        return (), "", ""
    title = _string(result.get("title"))
    cover = _original_image_url(
        _string(
            _dict(_dict(_dict(result.get("cover_media")).get("media_info"))).get(
                "original_img_url"
            )
        )
    )
    media_by_id = {
        _string(_dict(item).get("media_id")): _article_media(_dict(item))
        for item in _list(result.get("media_entities"))
        if _string(_dict(item).get("media_id"))
    }
    content_state = _dict(result.get("content_state"))
    raw_entities = content_state.get("entityMap")
    if isinstance(raw_entities, dict):
        entities = {str(key): _dict(value) for key, value in raw_entities.items()}
    else:
        entities = {
            str(_dict(item).get("key")): _dict(_dict(item).get("value"))
            for item in _list(raw_entities)
        }

    blocks: list[XArticleBlock] = []
    pending: list[str] = []

    def flush() -> None:
        text = _clean_text("\n".join(pending))
        pending.clear()
        if text:
            blocks.append(XArticleBlock(kind="text", text=text))

    for block in _list(content_state.get("blocks")):
        payload = _dict(block)
        text = _string(payload.get("text"))
        ranges = sorted(
            (_dict(item) for item in _list(payload.get("entityRanges"))),
            key=lambda item: _int(item.get("offset")),
        )
        cursor = 0
        for item in ranges:
            entity = entities.get(str(item.get("key")))
            if not entity or _string(entity.get("type")) != "MEDIA":
                continue
            start = max(_int(item.get("offset")), cursor)
            end = max(start + _int(item.get("length")), start)
            pending.append(
                text[
                    _utf16_offset_to_index(text, cursor) : _utf16_offset_to_index(
                        text, start
                    )
                ]
            )
            media_items = [
                media_by_id[media_id]
                for media_id in (
                    str(_dict(entry).get("mediaId"))
                    for entry in _list(_dict(entity.get("data")).get("mediaItems"))
                )
                if media_id in media_by_id
            ]
            media_items = [item for item in media_items if item is not None]
            if media_items:
                flush()
                blocks.extend(
                    XArticleBlock(kind="media", media=item) for item in media_items
                )
            cursor = end
        pending.append(text[_utf16_offset_to_index(text, cursor) :])
    flush()
    if not blocks:
        preview = _clean_text(_string(result.get("preview_text")))
        if preview:
            blocks.append(XArticleBlock(kind="text", text=preview))
    return tuple(blocks), title, cover


def _translation(tweet: dict[str, Any]) -> tuple[str, str]:
    payload = _dict(tweet.get("grok_translated_post_with_availability"))
    if not payload.get("is_available"):
        return "", ""
    data = _dict(payload.get("data"))
    return _clean_text(_string(data.get("translation"))), _string(
        data.get("source_language")
    )


def _count(value: Any) -> int:
    return max(_int(value), 0)


def _metric_counts(tweet: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    legacy = _dict(tweet.get("legacy"))
    views = _dict(tweet.get("views"))
    return (
        _count(views.get("count")),
        _count(legacy.get("favorite_count")),
        _count(legacy.get("reply_count")),
        _count(legacy.get("bookmark_count")),
        _count(legacy.get("quote_count")),
        _count(legacy.get("retweet_count")),
    )


def decode_tweet(value: Any, *, quoted: bool = False) -> XTweet:
    tweet = _unwrap_entry(value)
    if not tweet:
        raise ValueError("tweet data missing")
    note_text = _note_text(tweet)
    article_blocks, article_title, article_cover = _article_blocks(tweet)
    text = note_text or ("" if article_blocks else _legacy_text(tweet))
    author = _author(tweet)
    rest_id = _string(tweet.get("rest_id"))
    quoted_value = tweet.get("quoted_status_result") or tweet.get(
        "retweeted_status_result"
    )
    nested = (
        decode_tweet(quoted_value, quoted=True) if quoted_value and not quoted else None
    )
    legacy = _dict(tweet.get("legacy"))
    translated, translated_lang = _translation(tweet)
    views, likes, replies, bookmarks, quotes, retweets = _metric_counts(tweet)
    return XTweet(
        rest_id=rest_id,
        author=author,
        text=text,
        lang=_string(legacy.get("lang")),
        timestamp=_timestamp(tweet),
        source_url=(
            f"https://x.com/{author.screen_name}/status/{rest_id}"
            if author.screen_name and rest_id
            else ""
        ),
        media=_medias(tweet),
        title=article_title,
        article_cover_url=article_cover,
        article_blocks=article_blocks,
        card=_link_card(tweet),
        poll=_poll(tweet),
        quoted=nested,
        possibly_sensitive=bool(legacy.get("possibly_sensitive")),
        view_count=views,
        like_count=likes,
        reply_count=replies,
        bookmark_count=bookmarks,
        quote_count=quotes,
        retweet_count=retweets,
        translated_text=translated,
        translated_lang=translated_lang or _string(legacy.get("lang")),
    )


def decode_response(value: Any) -> XTweet:
    if not isinstance(value, dict):
        raise ValueError("response is not an object")
    entry = _dict(_dict(value.get("data")).get("tweetResult"))
    if not entry:
        raise ValueError("tweetResult is missing")
    return decode_tweet(entry)
