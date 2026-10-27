from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, ClassVar
from urllib.parse import urlsplit

import msgspec
from aiohttp import ClientError

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import (
    ContentItem,
    Description,
    Metric,
    Platform,
    TextContent,
    VideoMeta,
)
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .cards import link_text, poll_text
from .media import description_contents, first_cover_url, tweet_contents
from .models import XTweet, decode_response

V2_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8x"
    "nZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
TWEET_RESULT_URL = (
    "https://x.com/i/api/graphql/Xl0tsHf4AzflMRjbw9e70A/TweetResultByRestId"
)
GUEST_ACTIVATE_URL = "https://api.x.com/1.1/guest/activate.json"
TRANSLATION_URL = "https://api.x.com/2/grok/translation.json"
X_FEATURES = (
    '{"creator_subscriptions_tweet_preview_api_enabled":true,'
    '"premium_content_api_read_enabled":false,'
    '"communities_web_enable_tweet_community_results_fetch":true,'
    '"c9s_tweet_anatomy_moderator_badge_enabled":true,'
    '"responsive_web_grok_analyze_button_fetch_trends_enabled":false,'
    '"responsive_web_grok_analyze_post_followups_enabled":true,'
    '"rweb_cashtags_composer_attachment_enabled":true,'
    '"responsive_web_jetfuel_frame":true,'
    '"rweb_sports_post_context_enabled":true,'
    '"responsive_web_grok_share_attachment_enabled":true,'
    '"responsive_web_grok_annotations_enabled":true,'
    '"articles_preview_enabled":true,'
    '"responsive_web_edit_tweet_api_enabled":true,'
    '"rweb_conversational_replies_downvote_enabled":false,'
    '"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,'
    '"view_counts_everywhere_api_enabled":true,'
    '"longform_notetweets_consumption_enabled":true,'
    '"responsive_web_twitter_article_tweet_consumption_enabled":true,'
    '"content_disclosure_indicator_enabled":true,'
    '"content_disclosure_ai_generated_indicator_enabled":true,'
    '"responsive_web_grok_show_grok_translated_post":true,'
    '"responsive_web_grok_analysis_button_from_backend":true,'
    '"post_ctas_fetch_enabled":false,'
    '"rweb_cashtags_enabled":true,'
    '"freedom_of_speech_not_reach_fetch_enabled":true,'
    '"standardized_nudges_misinfo":true,'
    '"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,'
    '"longform_notetweets_rich_text_read_enabled":true,'
    '"longform_notetweets_inline_media_enabled":false,'
    '"profile_label_improvements_pcf_label_in_post_enabled":true,'
    '"responsive_web_profile_redirect_enabled":true,'
    '"rweb_tipjar_consumption_enabled":false,'
    '"verified_phone_label_enabled":false,'
    '"responsive_web_nested_quote_preview_enabled":false,'
    '"responsive_web_grok_image_annotation_enabled":true,'
    '"responsive_web_grok_imagine_annotation_enabled":true,'
    '"responsive_web_grok_community_note_auto_translation_is_enabled":true,'
    '"responsive_web_graphql_timeline_navigation_enabled":true}'
)
X_FIELD_TOGGLES = (
    '{"withArticleRichContentState":true,'
    '"withArticlePlainText":false,'
    '"withArticleSummaryText":true,'
    '"withArticleVoiceOver":true}'
)
X_GRAPHQL_VARIABLES = {
    "tweetId": "",
    "includePromotedContent": True,
    "withBirdwatchNotes": True,
    "withVoice": True,
    "withCommunity": True,
    "withV2Timeline": True,
    "withQuickPromoteEligibilityTweetFields": True,
}
NON_LINGUISTIC_LANGS = frozenset({"qme", "qst", "zxx"})
STATUS_PATTERN = (
    r"(?<![A-Za-z0-9.-])(?<![/.])https?://(?:(?:www|mobile)\.)?{host}/"
    r"(?:[A-Za-z0-9_]+/)*status/(?P<tweet_id>[0-9]+)"
)


def count_text(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(value)


def should_translate(tweet: XTweet) -> bool:
    lang = tweet.lang.lower()
    if (
        tweet.is_article
        or not lang
        or lang in NON_LINGUISTIC_LANGS
        or lang.split("-", 1)[0] == "zh"
    ):
        return False
    return bool(tweet.text.strip())


def _safe_error_text(status: int, reason: str) -> str:
    text = reason.strip()
    if not text or len(text) > 80:
        text = "请求失败"
    return f"X 接口请求失败（HTTP {status}，{text}）"


def _resolution(media: Any) -> str | None:
    variants = sorted(media.mp4_variants, key=lambda item: item.bitrate, reverse=True)
    for variant in variants:
        if match := re.search(r"/(\d+)x(\d+)/", variant.url):
            return f"{match.group(1)} × {match.group(2)}"
    return None


class XParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="x", display_name="X")
    guest_token_ttl: ClassVar[float] = 2 * 60 * 60

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Authorization": V2_BEARER,
                "Content-Type": "application/json",
                "Origin": "https://x.com",
                "Referer": "https://x.com/",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="x.com")
        self._guest_token: str | None = None
        self._guest_token_created = 0.0

    def _cookies(self) -> dict[str, str]:
        return self.cookiejar.get()

    def _media_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        headers.pop("Authorization", None)
        headers.pop("Content-Type", None)
        headers["Accept"] = "image/avif,image/webp,image/*,*/*;q=0.8"
        headers["Referer"] = "https://x.com/"
        if self._cookies() and (
            cookie := self.cookiejar.get_cookie_header_for_url("https://x.com/")
        ):
            headers["Cookie"] = cookie
        return headers

    @classmethod
    def search_url(cls, url: str) -> tuple[str, re.Match[str]]:
        """关键词匹配后复核 host，防止路径中的 x.com 抢占其他站点。"""
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if not (searched := pattern.search(url)):
                continue
            host = (urlsplit(searched.group(0)).hostname or "").lower()
            if host in {"x.com", "twitter.com"} or host.endswith(
                (".x.com", ".twitter.com")
            ):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

    async def _ensure_guest_token(self) -> str:
        now = asyncio.get_running_loop().time()
        if self._guest_token and now - self._guest_token_created < self.guest_token_ttl:
            return self._guest_token
        try:
            async with self.session.post(
                GUEST_ACTIVATE_URL,
                headers=self._api_headers(auth=False),
                proxy=self.proxy,
            ) as response:
                if response.status >= 400:
                    raise ParseException(
                        _safe_error_text(response.status, response.reason or "")
                    )
                body = await response.read()
        except ParseException:
            raise
        except (ClientError, TimeoutError) as exc:
            raise ParseException("获取 X 访客凭证失败") from exc
        try:
            payload = msgspec.json.decode(body)
            token = payload.get("guest_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise ValueError("guest token missing")
        except (msgspec.DecodeError, ValueError, AttributeError) as exc:
            raise ParseException("X 访客凭证格式异常") from exc
        self._guest_token = token
        self._guest_token_created = now
        return token

    def _api_headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = self.headers.copy()
        cookies = self._cookies()
        if auth:
            csrf = cookies.get("ct0") or uuid.uuid4().hex
            headers.update(
                {
                    "x-twitter-active-user": "yes",
                    "x-twitter-client-language": "zh-cn",
                    "x-csrf-token": csrf,
                }
            )
            if cookies.get("auth_token"):
                headers["Cookie"] = f"auth_token={cookies['auth_token']}; ct0={csrf};"
                headers["x-twitter-auth-type"] = "OAuth2Session"
        if cookie_header := self.cookiejar.get_cookie_header_for_url("https://x.com/"):
            headers["Cookie"] = cookie_header
        return headers

    async def _request_tweet(self, tweet_id: str) -> dict[str, Any]:
        headers = self._api_headers()
        if "x-guest-token" not in headers and "auth_token=" not in headers.get(
            "Cookie", ""
        ):
            headers["x-guest-token"] = await self._ensure_guest_token()
        params = {
            "variables": msgspec.json.encode(
                {**X_GRAPHQL_VARIABLES, "tweetId": tweet_id}
            ).decode(),
            "features": X_FEATURES,
            "fieldToggles": X_FIELD_TOGGLES,
        }
        retries = max(int(getattr(self.cfg, "download_retry_times", 2)), 0)
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    TWEET_RESULT_URL,
                    params=params,
                    headers=headers,
                    proxy=self.proxy,
                ) as response:
                    body = await response.read()
                    if response.status == 403 and attempt == 0:
                        self._guest_token = None
                    if response.status >= 400:
                        raise ClientError(
                            _safe_error_text(response.status, response.reason or "")
                        )
                    payload = msgspec.json.decode(body)
                    if not isinstance(payload, dict):
                        raise ParseException("X 返回的数据格式异常")
                    return payload
            except ParseException:
                raise
            except (ClientError, TimeoutError, msgspec.DecodeError) as exc:
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    if isinstance(exc, ClientError) and "403" in str(exc):
                        headers["x-guest-token"] = await self._ensure_guest_token()
                    continue
                if isinstance(exc, msgspec.DecodeError):
                    raise ParseException("X 返回了无效 JSON") from exc
                raise ParseException("获取 X 帖子数据失败") from exc
        raise ParseException("获取 X 帖子数据失败")

    async def _fetch_translation(self, tweet_id: str) -> str:
        cookies = self._cookies()
        if not cookies.get("auth_token"):
            return ""
        headers = self._api_headers()
        try:
            async with self.session.post(
                TRANSLATION_URL,
                headers=headers,
                json={
                    "content_type": "POST",
                    "id": tweet_id,
                    "dst_lang": "zh",
                },
                proxy=self.proxy,
            ) as response:
                if response.status >= 400:
                    return ""
                body = await response.read()
            payload = msgspec.json.decode(body)
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                return ""
            value = result.get("text")
            return value.strip() if isinstance(value, str) else ""
        except (ClientError, TimeoutError, msgspec.DecodeError):
            return ""

    async def _translation(self, tweet: XTweet) -> str:
        if not bool(getattr(self.mycfg, "translate", False)):
            return ""
        if tweet.translated_text:
            return tweet.translated_text
        if not should_translate(tweet):
            return ""
        return await self._fetch_translation(tweet.rest_id)

    def _author(self, tweet: XTweet):
        return self.create_author(
            tweet.author.name,
            tweet.author.avatar_url or None,
            description=tweet.author.description or None,
            uid=tweet.author.screen_name or tweet.author.uid or None,
            headers=self._media_headers(),
        )

    @staticmethod
    def _metrics(tweet: XTweet) -> list[Metric]:
        values = (
            ("浏览", tweet.view_count),
            ("点赞", tweet.like_count),
            ("评论", tweet.reply_count),
            ("收藏", tweet.bookmark_count),
            ("分享", tweet.quote_count + tweet.retweet_count),
        )
        return [
            Metric(icon=None, label=label, value=count_text(value))
            for label, value in values
            if value > 0
        ]

    def _create_image(self, url: str):
        return self.create_image_contents([url], headers=self._media_headers())[0]

    def _create_video(self, url: str, **kwargs: Any):
        return self.create_video_content(url, headers=self._media_headers(), **kwargs)

    def _create_dynamic(self, url: str):
        return self.create_dynamic_contents([url], headers=self._media_headers())[0]

    def _create_graphic(self, url: str):
        return self.create_graphics_content(url, headers=self._media_headers())

    def _cover_task(self, url: str):
        if not url:
            return None
        return self.downloader.download_img(
            url, headers=self._media_headers(), proxy=self.proxy
        )

    def _card_image(self, url: str):
        content = self._create_image(url)

        async def load() -> Any:
            return await self.downloader.download_img(
                url, headers=self._media_headers(), proxy=self.proxy
            )

        content.set_loader(load)
        return content

    def _contents(self, tweet: XTweet) -> list[ContentItem]:
        return tweet_contents(
            tweet,
            create_image=self._create_image,
            create_video=self._create_video,
            create_dynamic=self._create_dynamic,
            create_graphic=self._create_graphic,
        )

    async def _description(
        self,
        tweet: XTweet,
        *,
        translation: str = "",
        include_quoted: bool = True,
    ) -> Description:
        repost = None
        if include_quoted and tweet.quoted is not None:
            repost = await self._description(
                tweet.quoted,
                include_quoted=False,
            )
        contents = description_contents(tweet, translation=translation)
        if tweet.poll is not None:
            contents.append(TextContent(poll_text(tweet.poll)))
            contents.extend(
                self._card_image(choice.image_url)
                for choice in tweet.poll.choices
                if choice.image_url
            )
        elif tweet.card is not None and bool(
            getattr(self.mycfg, "include_link_card", True)
        ):
            contents.append(TextContent(link_text(tweet.card)))
            if tweet.card.preview_url:
                contents.append(self._card_image(tweet.card.preview_url))
        text = None
        if tweet.possibly_sensitive:
            text = "该帖子被作者标记为敏感内容。"
        video = None
        video_media = next(
            (media for media in tweet.media if media.kind == "video"),
            None,
        )
        if video_media is not None:
            variant = video_media.best_variant
            video = VideoMeta(
                cover=self._cover_task(video_media.cover_url),
                duration=video_media.duration,
                quality=(
                    f"{variant.bitrate // 1000} kbps" if variant is not None else None
                ),
                resolution=_resolution(video_media),
            )
        return Description(
            platform=self.platform,
            author=self._author(tweet),
            title=tweet.title or None,
            text=text,
            timestamp=tweet.timestamp or None,
            url=tweet.source_url or None,
            contents=contents,
            cover=self._cover_task(first_cover_url(tweet)),
            video=video,
            repost=repost,
        )

    @handle("x.com", STATUS_PATTERN.format(host=r"x\.com"))
    @handle("twitter.com", STATUS_PATTERN.format(host=r"twitter\.com"))
    async def _parse(self, searched: re.Match[str]):
        tweet_id = searched.group("tweet_id")
        payload = await self._request_tweet(tweet_id)
        try:
            tweet = decode_response(payload)
        except (ValueError, TypeError, msgspec.ValidationError) as exc:
            raise ParseException("X 帖子不存在、已删除或当前不可见") from exc
        if not tweet.has_content:
            raise ParseException("X 帖子没有可解析的内容")
        if (
            tweet.possibly_sensitive
            and str(getattr(self.mycfg, "sensitive_mode", "warn")) == "skip"
        ):
            raise ParseException("该帖子被作者标记为敏感内容，已按配置跳过")

        translation = await self._translation(tweet)
        description = await self._description(tweet, translation=translation)
        await self.send_desc_common(
            description.title,
            description.author,
            description.contents,
            text=description.text,
            timestamp=description.timestamp,
            url=description.url,
            metrics=self._metrics(tweet),
            cover=description.cover,
            repost=description.repost,
        )

        contents = self._contents(tweet)
        if bool(getattr(self.mycfg, "include_quoted_media", False)):
            if tweet.quoted is not None:
                contents.extend(self._contents(tweet.quoted))
        return self.parse_result(contents)
