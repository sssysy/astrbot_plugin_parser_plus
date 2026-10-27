from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import (
    Author,
    Comment,
    ContentItem,
    Description,
    MediaContent,
    Metric,
    Platform,
)
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .content import clean_status_text, comment_text, parse_article_blocks
from .models import (
    WeiboStatus,
    decode_comments,
    decode_long_text,
    decode_status,
)


def _count(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if abs(number) >= 100_000_000:
        text = f"{number / 100_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}亿"
    if abs(number) >= 10_000:
        text = f"{number / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}万"
    return str(number)


def _timestamp(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number // 1000 if number >= 10**12 else number


def _metric(label: str, value: Any) -> Metric | None:
    formatted = _count(value)
    return Metric(icon=None, label=label, value=formatted) if formatted else None


def _metrics(*items: tuple[str, Any]) -> list[Metric]:
    return [
        metric
        for label, value in items
        if (metric := _metric(label, value)) is not None
    ]


def _article_id(source_url: str) -> str:
    if match := re.search(r"/id/(\d+)", source_url):
        return match.group(1)
    if match := re.search(r"#*/id=(\d+)", source_url):
        return match.group(1)
    if match := re.search(r"(?:[?#]|&)id=(\d+)", source_url):
        return match.group(1)
    query = parse_qs(urlparse(source_url).query)
    for key in ("id", "article_id"):
        value = query.get(key)
        if value:
            return value[0]
    return ""


def _fid(source_url: str) -> str:
    query = parse_qs(urlparse(source_url).query)
    value = query.get("fid")
    return value[0] if value else ""


def parsed_fid(matched: str) -> str:
    value = parse_qs(matched).get("fid")
    return value[0] if value else ""


def comment_limit(value: Any) -> int:
    try:
        return max(0, min(int(value), 20))
    except (TypeError, ValueError):
        return 5


class WeiboParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="weibo", display_name="微博")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.cookiejar = CookieJar(config, self.mycfg, domain="weibo.com")
        self.media_headers = {
            **self.headers,
            "Referer": "https://weibo.com/",
        }
        if self.cookiejar.cookies_str:
            self.media_headers["Cookie"] = self.cookiejar.cookies_str

    @handle(
        "weibo.com/tv",
        r"weibo\.com/tv/show/\d{4}:\d+(?:\?mid=(?P<mid>\d+))?",
    )
    async def _parse_tv(self, searched: re.Match[str]):
        mid = searched.group("mid")
        if mid:
            return await self.parse_status(self._mid2id(mid))
        source = self._matched_url(searched)
        fid = parse_qs(urlparse(source).query).get("fid", [""])[0]
        if not fid:
            match = re.search(r"/show/(\d{4}:\d+)", source)
            fid = match.group(1) if match else ""
        if not fid:
            raise ParseException("无法从微博视频链接中提取 fid")
        return await self.parse_video(fid)

    @handle(
        "video.weibo.com/show",
        r"video\.weibo\.com/show\?(?P<query>[^#\s]*\bfid=\d+:\d+[^#\s]*)",
    )
    async def _parse_video(self, searched: re.Match[str]):
        fid = parsed_fid(searched.group("query"))
        return await self.parse_video(fid)

    @handle(
        "m.weibo.cn",
        r"(?:m\.)?weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9A-Za-z]+)",
    )
    @handle(
        "weibo.com",
        r"(?:www\.)?weibo\.com/\d+/(?P<wid>[0-9A-Za-z]+)",
    )
    async def _parse_status(self, searched: re.Match[str]):
        return await self.parse_status(searched.group("wid"))

    @handle(
        "mapp.api.weibo",
        r"mapp\.api\.weibo\.cn/fx/[0-9A-Za-z]+\.html",
    )
    async def _parse_short_link(self, searched: re.Match[str]):
        source = self._matched_url(searched)
        final_url = await self.get_final_url(source, headers=self.media_headers)
        candidates = [final_url]
        query = parse_qs(urlparse(final_url).query)
        candidates.extend(unquote(value) for value in query.get("url", []))
        for candidate in candidates:
            for pattern in (
                r"weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9A-Za-z]+)",
                r"weibo\.com/(?P<uid>\d+)/(?P<wid>[0-9A-Za-z]+)",
            ):
                if match := re.search(pattern, candidate):
                    return await self.parse_status(match.group("wid"))
        raise ParseException("微博分享链接未指向可解析的微博")

    @handle(
        "weibo.com/ttarticle",
        r"weibo\.com/ttarticle/(?:p/show|x/m/show)[^#\s]*[?#][^\s]*id=(?P<id>\d+)",
    )
    @handle(
        "card.weibo.com/article",
        r"card\.weibo\.com/article/m/show/id/(?P<id>\d+)",
    )
    async def _parse_article(self, searched: re.Match[str]):
        article_id = searched.groupdict().get("id")
        if not article_id:
            article_id = _article_id(self._matched_url(searched))
        if not article_id:
            raise ParseException("无法从微博专栏链接中提取文章 ID")
        return await self.parse_article(article_id)

    def _matched_url(self, searched: re.Match[str]) -> str:
        source = str(getattr(searched, "string", "") or "")
        start = source.rfind("http://")
        if start < 0:
            start = source.rfind("https://")
        if start >= 0:
            return source[start:]
        matched = searched.group(0)
        return matched if matched.startswith(("http://", "https://")) else ""

    async def _request_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        error: str,
        allow_redirects: bool = True,
    ) -> dict[str, Any]:
        request_headers = {**self.media_headers, **(headers or {})}
        retries = self.cfg.download_retry_times
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self.session.request(
                    "POST" if data is not None else "GET",
                    url,
                    params=params,
                    data=data,
                    headers=request_headers,
                    allow_redirects=allow_redirects,
                    proxy=self.proxy,
                ) as response:
                    if response.status >= 400:
                        raise ClientError(f"HTTP {response.status} {response.reason}")
                    body = await response.read()
                    content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    raise ParseException(f"{error}：接口返回了非 JSON 内容")
                payload = msgspec.json.decode(body)
                if not isinstance(payload, dict):
                    raise ParseException(f"{error}：返回数据格式异常")
                return payload
            except ParseException:
                raise
            except (ClientError, TimeoutError, ValueError, msgspec.DecodeError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException(error) from exc
        raise ParseException(error) from last_error

    async def _fetch_status(self, weibo_id: str) -> WeiboStatus:
        payload = await self._request_json(
            f"https://m.weibo.cn/statuses/show?id={weibo_id}",
            params={"_": int(time.time() * 1000)},
            headers={
                "Referer": f"https://m.weibo.cn/detail/{weibo_id}",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "mweibo-pwa": "1",
            },
            error="获取微博数据失败",
            allow_redirects=False,
        )
        try:
            return decode_status(payload)
        except (TypeError, ValueError) as exc:
            raise ParseException("微博数据不可用或已删除") from exc

    async def _long_text(self, status: WeiboStatus) -> str | None:
        if not status.is_long_text:
            return None
        try:
            payload = await self._request_json(
                f"https://m.weibo.cn/statuses/extend?id={status.mid}",
                headers={
                    "Referer": f"https://m.weibo.cn/detail/{status.mid}",
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
                error="获取微博长文失败",
            )
            content = decode_long_text(payload)
            return clean_status_text(content, "") or None
        except ParseException as exc:
            logger.warning(
                "[微博] 长文获取失败，使用摘要正文 | mid=%s | error_type=%s",
                status.mid,
                type(exc).__name__,
            )
            return None

    async def parse_status(self, weibo_id: str):
        status = await self._fetch_status(weibo_id)
        return await self._build_status_result(status, include_comments=True)

    async def _build_status_result(
        self,
        status: WeiboStatus,
        *,
        include_comments: bool,
    ) -> list[MediaContent]:
        text = await self._long_text(status) or clean_status_text(
            status.html_text, status.raw_text
        )
        contents = self._status_contents(status)
        author = self._author(status)
        comments = await self._fetch_comments(status) if include_comments else []
        description = await self._description(status, text, contents)
        metrics = _metrics(
            ("转发", status.reposts_count),
            ("评论", status.comments_count),
            ("点赞", status.attitudes_count),
        )
        if status.video is not None:
            await self.send_desc_video(
                status.video.title or None,
                author,
                cover=status.video.cover_url or None,
                duration=status.video.duration,
                text=text or None,
                timestamp=status.timestamp or None,
                url=status.url,
                metrics=metrics,
                comments=comments,
                repost=description.repost,
                headers=self.media_headers,
            )
        else:
            await self.send_desc_common(
                None,
                author,
                [],
                cover=self._cover(status) or None,
                text=text or None,
                timestamp=status.timestamp or None,
                url=status.url,
                metrics=metrics,
                comments=comments,
                repost=description.repost,
            )
        return self.parse_result(contents)

    async def _description(
        self,
        status: WeiboStatus,
        text: str,
        contents: list[MediaContent],
    ) -> Description:
        repost = None
        if (
            status.retweeted is not None
            and bool(getattr(self.mycfg, "include_reposts", True))
        ):
            repost = await self._description(
                status.retweeted,
                clean_status_text(
                    status.retweeted.html_text,
                    status.retweeted.raw_text,
                ),
                self._status_contents(status.retweeted),
            )
        return Description(
            platform=self.platform,
            author=self._author(status),
            text=text or None,
            timestamp=status.timestamp or None,
            url=status.url,
            contents=contents,
            repost=repost,
        )

    def _status_contents(self, status: WeiboStatus) -> list[MediaContent]:
        contents: list[MediaContent] = []
        for pic in status.pics:
            if pic.is_live and pic.live_video_url:
                contents.append(
                    self.create_live_photo_content(
                        pic.live_video_url,
                        pic.image_url,
                        cover_url=pic.image_url,
                        headers=self.media_headers,
                    )
                )
            else:
                contents.extend(
                    self.create_image_contents(
                        [pic.image_url], headers=self.media_headers
                    )
                )
        if status.video is not None:
            contents.append(
                self.create_video_content(
                    status.video.url,
                    cover_url=status.video.cover_url or None,
                    duration=status.video.duration,
                    headers=self.media_headers,
                    fallback_urls=status.video.fallback_urls,
                )
            )
        if bool(getattr(self.mycfg, "include_reposts", True)):
            if status.retweeted is not None:
                contents.extend(self._status_contents(status.retweeted))
        return contents

    @staticmethod
    def _cover(status: WeiboStatus) -> str | None:
        if status.video is not None and status.video.cover_url:
            return status.video.cover_url
        if status.pics:
            return status.pics[0].image_url
        return None

    def _author(self, status: WeiboStatus) -> Author:
        return self.create_author(
            status.author.name,
            status.author.avatar_url or None,
            description=status.author.description or None,
            uid=status.author.uid or None,
            headers=self.media_headers,
        )

    async def _fetch_comments(self, status: WeiboStatus) -> list[Comment]:
        limit = comment_limit(getattr(self.mycfg, "comment_limit", 5))
        if limit == 0:
            return []
        try:
            payload = await self._request_json(
                "https://m.weibo.cn/comments/hotflow",
                params={"mid": status.mid},
                headers={
                    "Referer": f"https://m.weibo.cn/detail/{status.mid}",
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
                error="获取微博评论失败",
            )
            items, _ = decode_comments(payload)
        except ParseException as exc:
            logger.warning(
                "[微博] 评论获取失败 | mid=%s | error_type=%s",
                status.mid,
                type(exc).__name__,
            )
            return []
        return [
            comment
            for item in items[:limit]
            if (comment := self._comment(item)) is not None
        ]

    def _comment(self, item: dict[str, Any]) -> Comment | None:
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        text = comment_text(item.get("text"))
        if not user or not text:
            return None
        author = self.create_author(
            str(user.get("screen_name") or "微博用户"),
            str(user.get("profile_image_url") or "") or None,
            uid=str(user.get("id") or "") or None,
            headers=self.media_headers,
        )
        replies: list[Comment] = []
        raw_replies = item.get("comments")
        if isinstance(raw_replies, list):
            for raw in raw_replies[:3]:
                if isinstance(raw, dict) and (reply := self._comment(raw)):
                    replies.append(reply)
        return Comment(
            author=author,
            contents=[text],
            timestamp=_timestamp(item.get("created_at")),
            location=str(item.get("source") or "") or None,
            like_count=_count(item.get("like_count")),
            reply_count=_count(item.get("total_number")),
            replies=replies,
        )

    async def parse_video(self, fid: str):
        payload = await self._request_json(
            f"https://h5.video.weibo.com/api/component?page=/show/{fid}",
            data={
                "data": json.dumps(
                    {"Component_Play_Playinfo": {"oid": fid}},
                    ensure_ascii=False,
                )
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            error="获取微博视频信息失败",
        )
        data = (
            ((payload.get("data") or {}).get("Component_Play_Playinfo") or {})
            if isinstance(payload.get("data"), dict)
            else {}
        )
        if not data:
            raise ParseException("微博视频信息为空")
        urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}
        candidates = [
            value
            for value in urls.values()
            if isinstance(value, str) and value
        ]
        if not candidates and isinstance(data.get("stream_url"), str):
            candidates.append(data["stream_url"])
        if not candidates:
            raise ParseException("未找到可用的微博视频流")
        candidates = [
            f"https:{url}" if url.startswith("//") else url for url in candidates
        ]
        cover = str(data.get("cover_image") or "")
        if cover.startswith("//"):
            cover = f"https:{cover}"
        try:
            duration = float(data.get("duration_time") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        author = self.create_author(
            str(data.get("author") or "微博用户"),
            str(data.get("avatar") or "") or None,
            uid=str(data.get("author_id") or "") or None,
            headers=self.media_headers,
        )
        text = comment_text(data.get("text"))
        await self.send_desc_video(
            str(data.get("title") or "") or None,
            author,
            cover=cover or None,
            duration=duration,
            text=text or None,
            timestamp=_timestamp(data.get("real_date")),
            url=f"https://video.weibo.com/show?fid={fid}",
            headers=self.media_headers,
        )
        return self.parse_result(
            [
                self.create_video_content(
                    candidates[0],
                    cover_url=cover or None,
                    duration=duration,
                    headers=self.media_headers,
                    fallback_urls=candidates[1:],
                )
            ]
        )

    async def parse_article(self, article_id: str):
        payload = await self._request_json(
            "https://card.weibo.com/article/m/aj/detail",
            params={"id": article_id},
            headers={"Referer": "https://weibo.com/"},
            error="获取微博专栏失败",
        )
        if str(payload.get("msg") or "").lower() != "success":
            raise ParseException("微博专栏不可用")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data:
            raise ParseException("微博专栏数据为空")
        title = str(data.get("title") or "") or None
        html_content = str(data.get("content") or "")
        blocks, text = parse_article_blocks(html_content)
        contents: list[MediaContent] = []
        for kind, value in blocks:
            if kind == "image" and value:
                contents.extend(
                    self.create_image_contents([value], headers=self.media_headers)
                )
        user = data.get("userinfo") if isinstance(data.get("userinfo"), dict) else {}
        author = self.create_author(
            str(user.get("screen_name") or "微博用户"),
            str(user.get("profile_image_url") or "") or None,
            uid=str(user.get("id") or "") or None,
            headers=self.media_headers,
        )
        metrics = _metrics(("浏览", data.get("read_count")))
        cover = next((value for kind, value in blocks if kind == "image"), None)
        await self.send_desc_common(
            title,
            author,
            [],
            cover=cover,
            text=text or None,
            timestamp=_timestamp(data.get("create_at_unix")),
            url=str(
                data.get("url")
                or f"https://weibo.com/ttarticle/p/show?id={article_id}"
            ),
            metrics=metrics,
        )
        return self.parse_result(contents)

    @staticmethod
    def _base62_encode(number: int) -> str:
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if number == 0:
            return "0"
        result = ""
        while number > 0:
            result = alphabet[number % 62] + result
            number //= 62
        return result

    def _mid2id(self, mid: str) -> str:
        from math import ceil

        reversed_mid = str(mid)[::-1]
        size = ceil(len(reversed_mid) / 7)
        chunks: list[str] = []
        for index in range(size):
            chunk = reversed_mid[index * 7 : (index + 1) * 7][::-1]
            encoded = self._base62_encode(int(chunk))
            if index < size - 1 and len(encoded) < 4:
                encoded = "0" * (4 - len(encoded)) + encoded
            chunks.append(encoded)
        chunks.reverse()
        return "".join(chunks)
