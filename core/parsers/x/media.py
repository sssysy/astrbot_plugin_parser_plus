from __future__ import annotations

from ...data import ContentItem, TextContent
from .models import XMedia, XTweet


def media_content(
    media: XMedia,
    *,
    create_image,
    create_video,
    create_dynamic,
) -> ContentItem | None:
    if media.kind == "photo":
        return create_image(media.source_url)
    if media.kind == "animated_gif":
        return create_dynamic(media.source_url)
    best = media.best_variant
    if best is None:
        return None
    fallbacks = tuple(
        item.url
        for item in sorted(
            media.mp4_variants, key=lambda item: item.bitrate, reverse=True
        )
        if item.url != best.url
    )
    return create_video(
        best.url,
        cover_url=media.cover_url or None,
        duration=media.duration,
        fallback_urls=fallbacks,
    )


def tweet_contents(
    tweet: XTweet,
    *,
    create_image,
    create_video,
    create_dynamic,
    create_graphic,
) -> list[ContentItem]:
    contents: list[ContentItem] = []
    if tweet.article_cover_url:
        contents.append(create_graphic(tweet.article_cover_url))
    if tweet.article_blocks:
        for block in tweet.article_blocks:
            if block.kind == "text" and block.text:
                contents.append(TextContent(block.text))
            elif block.media is not None:
                item = media_content(
                    block.media,
                    create_image=create_image,
                    create_video=create_video,
                    create_dynamic=create_dynamic,
                )
                if item is not None:
                    contents.append(item)
    elif tweet.text:
        contents.append(TextContent(tweet.text))
    for media in tweet.media:
        item = media_content(
            media,
            create_image=create_image,
            create_video=create_video,
            create_dynamic=create_dynamic,
        )
        if item is not None:
            contents.append(item)
    return contents


def description_contents(tweet: XTweet, *, translation: str = "") -> list[ContentItem]:
    """描述卡片正文：保留文章段落；普通帖子媒体交给卡片封面。"""
    contents: list[ContentItem] = []
    if tweet.article_blocks:
        for block in tweet.article_blocks:
            if block.kind == "text" and block.text:
                contents.append(TextContent(block.text))
    elif tweet.text:
        contents.append(TextContent(tweet.text))
    if translation:
        contents.append(TextContent(f"Grok 翻译：\n{translation}"))
    return contents


def first_image_url(tweet: XTweet) -> str:
    if tweet.article_cover_url:
        return tweet.article_cover_url
    for media in tweet.media:
        if media.kind == "photo" and media.source_url:
            return media.source_url
    return ""
