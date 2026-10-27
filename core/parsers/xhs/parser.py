from __future__ import annotations

import asyncio
import json
import re
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import MediaContent
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, Platform, handle
from .models import (
    XhsNote,
    XhsUserPage,
    XhsUserPageData,
    select_live_photo_stream,
    select_video,
)
from .utils import (
    build_note_url,
    is_xhs_page_url,
    note_ref_from_url,
)

INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>",
    flags=re.DOTALL,
)
UNDEFINED_VALUE_RE = re.compile(
    r"(?P<prefix>[:,\[]\s*)undefined(?=\s*[,}\]])"
    r'|(?P<scalar>\bundefined\b)'
)
JS_WRAPPER_RE = re.compile(
    r"\bnew\s+(?:Map|Set|WeakMap|WeakSet)\s*\(\s*(\[[\s\S]*?\])\s*\)"
)
NOTE_UNAVAILABLE_MARKERS = (
    "当前笔记暂时无法浏览",
    "当前笔记暂时无法查看",
    "笔记不存在",
    "笔记已删除",
)


def _unwrap_note(value: Any, note_id: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if "note" in value and isinstance(value["note"], dict):
        value = value["note"]
    if "noteData" in value and isinstance(value["noteData"], dict):
        value = value["noteData"]
    if value.get("noteId") == note_id or value.get("title") is not None:
        return value
    return None


def find_note_data(state: dict[str, Any], note_id: str) -> dict[str, Any] | None:
    """兼容探索页 noteDetailMap 与发现页 noteData.data.noteData。"""
    note_data = state.get("noteData")
    if isinstance(note_data, dict):
        data = note_data.get("data")
        if isinstance(data, dict):
            if found := _unwrap_note(data.get("noteData"), note_id):
                return found
        if found := _unwrap_note(note_data.get("noteData"), note_id):
            return found
        if found := _unwrap_note(note_data, note_id):
            return found

    note = state.get("note")
    if isinstance(note, dict):
        detail_map = note.get("noteDetailMap")
        if isinstance(detail_map, dict):
            if found := _unwrap_note(detail_map.get(note_id), note_id):
                return found
            for value in detail_map.values():
                if found := _unwrap_note(value, note_id):
                    return found
    return None


def extract_initial_state(html: str) -> dict[str, Any]:
    if any(marker in html for marker in NOTE_UNAVAILABLE_MARKERS):
        raise ParseException("小红书笔记暂时无法浏览，可能需要登录 Cookie")
    matched = INITIAL_STATE_RE.search(html)
    if not matched:
        raise ParseException("小红书分享链接失效或内容已删除")
    raw = matched.group(1).strip().rstrip(";")

    def replace_undefined(match: re.Match[str]) -> str:
        if match.group("prefix") is not None:
            return f"{match.group('prefix')}null"
        return "null"

    raw = JS_WRAPPER_RE.sub(r"\1", raw)
    raw = UNDEFINED_VALUE_RE.sub(replace_undefined, raw)
    raw = re.sub(r"\bNaN\b", "null", raw)
    raw = re.sub(r"(?<![\w\"'])-?Infinity\b", "null", raw)
    try:
        state = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ParseException("小红书页面数据格式异常") from exc
    if not isinstance(state, dict):
        raise ParseException("小红书页面数据格式异常")
    return state


def is_unavailable_redirect(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.startswith("/404"):
        return True
    query = parse_qs(parsed.query)
    error_code = (query.get("error_code") or [""])[0]
    message = unquote((query.get("error_msg") or [""])[0])
    return error_code == "300031" or any(
        marker in message for marker in NOTE_UNAVAILABLE_MARKERS
    )


def decode_note_from_state(state: dict[str, Any], note_id: str) -> XhsNote:
    raw = find_note_data(state, note_id)
    if raw is None:
        raise ParseException("小红书笔记不存在、已删除或当前不可见")
    try:
        note = msgspec.convert(raw, type=XhsNote)
    except (msgspec.ValidationError, TypeError, ValueError) as exc:
        raise ParseException("小红书笔记数据格式异常") from exc
    if not note.note_id:
        note.note_id = note_id
    return note


class XhsParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xhs", display_name="小红书")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/132.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.xiaohongshu.com/",
                "Origin": "https://www.xiaohongshu.com",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="xiaohongshu.com")

    def _cookies_for_page(self) -> str:
        return self.cookiejar.get_cookie_header_for_url(
            "https://www.xiaohongshu.com/"
        )

    @property
    def web_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        headers.pop("Cookie", None)
        if cookie := self._cookies_for_page():
            headers["Cookie"] = cookie
        return headers

    @property
    def media_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        headers["Referer"] = "https://www.xiaohongshu.com/"
        headers.pop("Cookie", None)
        if cookie := self._cookies_for_page():
            headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _matched_url(searched: re.Match[str]) -> str:
        value = (
            searched.string[searched.start() :]
            if searched.string
            else searched.group(0)
        )
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+")
    @handle("xhslink.cn", r"xhslink\.cn/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = self._matched_url(searched)
        final_url = await self._resolve_short_link(url)
        if final_url == url:
            raise ParseException("无法展开小红书短链")
        note_id, token = note_ref_from_url(final_url)
        if note_id:
            return await self._parse_note(note_id, token, final_url)
        keyword, matched = self.search_url(final_url)
        return await self.parse(keyword, matched)

    async def _resolve_short_link(self, url: str) -> str:
        """展开可能经过校验页、且由页面脚本二次跳转的小红书短链。"""
        current = url
        for _ in range(4):
            final_url = await self.get_final_url(
                current,
                headers=self.web_headers,
            )
            if note_ref_from_url(final_url)[0]:
                return final_url
            next_url = await self._extract_redirect_from_page(final_url)
            if not next_url or next_url == current or not is_xhs_page_url(next_url):
                return final_url
            current = next_url
        return current

    async def _extract_redirect_from_page(self, url: str) -> str | None:
        try:
            async with self.session.get(
                url,
                headers=self.web_headers,
                proxy=self.proxy,
            ) as response:
                if response.status >= 400:
                    return None
                html = await response.text()
        except (ClientError, TimeoutError, UnicodeError):
            return None

        candidates = (
            re.search(
                r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""",
                html,
            ),
            re.search(r"""<meta[^>]+url=([^"'>\s]+)""", html, re.IGNORECASE),
            re.search(
                r"""["'](?:redirectPath|jumpUrl|targetUrl)["']\s*:\s*["']([^"']+)["']""",
                html,
            ),
        )
        for candidate in candidates:
            if not candidate:
                continue
            target = unquote(candidate.group(1).replace(r"\/", "/"))
            if target.startswith(("http://", "https://")) and is_xhs_page_url(target):
                return target
        return None

    @handle(
        "xiaohongshu.com",
        r"(?:www\.)?xiaohongshu\.com/"
        r"(?:explore|discovery/item|search_result)/(?P<note_id>[0-9A-Za-z]+)",
    )
    async def _parse_common(self, searched: re.Match[str]):
        url = self._matched_url(searched)
        note_id, token = note_ref_from_url(url)
        if not note_id:
            raise ParseException("无法从链接中提取小红书笔记 ID")
        return await self._parse_note(note_id, token, url)

    @handle(
        "target_note_id",
        r"xiaohongshu\.com/[^\s]*[?&]target_note_id="
        r"(?P<note_id>[0-9A-Za-z]+)",
    )
    async def _parse_target_note(self, searched: re.Match[str]):
        url = self._matched_url(searched)
        note_id, token = note_ref_from_url(url)
        if not note_id:
            note_id = searched.group("note_id")
        return await self._parse_note(note_id, token, url)

    async def _parse_note(
        self,
        note_id: str,
        token: str | None,
        source_url: str,
    ):
        last_error: ParseException | None = None
        for candidate in self._candidate_urls(note_id, token, source_url):
            try:
                note = await self._fetch_note(candidate, note_id)
                return await self._build_result(note, note_id, token)
            except ParseException as exc:
                last_error = exc
                logger.debug(
                    "[小红书] 页面尝试失败 | "
                    f"note_id={note_id} | "
                    f"host={urlparse(candidate).hostname or 'unknown'} | "
                    f"error={exc.message}"
                )
        raise last_error or ParseException("小红书笔记解析失败")

    @staticmethod
    def _candidate_urls(
        note_id: str,
        token: str | None,
        source_url: str,
    ) -> list[str]:
        urls: list[str] = []
        if is_xhs_page_url(source_url) and note_id in source_url:
            urls.append(source_url)
        for path in ("discovery/item", "explore"):
            urls.append(build_note_url(note_id, token, path=path))
        return list(dict.fromkeys(urls))

    async def _fetch_note(self, url: str, note_id: str) -> XhsNote:
        retries = self.cfg.download_retry_times
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url,
                    headers=self.web_headers,
                    allow_redirects=True,
                    proxy=self.proxy,
                ) as response:
                    if response.status >= 400:
                        raise ClientError(
                            f"HTTP {response.status} {response.reason}"
                        )
                    html = await response.text()
                    set_cookies = response.headers.getall("Set-Cookie", [])
                    response_url = str(response.url)
            except (ClientError, TimeoutError, UnicodeError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException("小红书页面请求失败") from exc

            if set_cookies:
                self.cookiejar.update_from_response(
                    set_cookies,
                    request_url=response_url,
                )
            if is_unavailable_redirect(response_url):
                raise ParseException("小红书笔记暂时无法浏览，可能需要登录 Cookie")
            state = extract_initial_state(html)
            return decode_note_from_state(state, note_id)
        raise ParseException("小红书页面请求失败") from last_error

    async def _build_result(
        self,
        note: XhsNote,
        note_id: str,
        token: str | None,
    ) -> list[MediaContent]:
        profile = await self._fetch_author_profile(note.author_id)
        author = self.create_author(
            (profile.basic_info.nickname if profile else "") or note.author_name,
            note.avatar_url,
            description=(
                profile.basic_info.desc or profile.basic_info.ip_location or None
                if profile
                else None
            ),
            uid=note.author_id,
            metrics=profile.metrics if profile else None,
            headers=self.media_headers,
        )
        display_url = build_note_url(note_id, token)
        title = note.title.strip() or None
        cover = note.cover_url or None

        if note.video is not None:
            selected = select_video(
                note.video,
                str(getattr(self.mycfg, "video_quality", "auto") or "auto"),
            )
            if selected is None:
                raise ParseException("未找到可用的视频流")
            await self.send_desc_video(
                title,
                author,
                cover=cover,
                duration=note.video.duration,
                quality=selected.quality_label,
                resolution=selected.resolution,
                text=note.text,
                timestamp=note.timestamp,
                url=display_url,
                metrics=note.metrics,
                headers=self.media_headers,
            )
            content = self.create_video_content(
                selected.url,
                cover_url=cover,
                duration=note.video.duration,
                headers=self.media_headers,
                fallback_urls=selected.fallback_urls,
            )
            return self.parse_result([content])

        image_urls = note.image_urls
        if not image_urls:
            raise ParseException("小红书笔记没有可发送的媒体")
        contents = self._build_image_contents(note)
        await self.send_desc_common(
            title,
            author,
            [],
            cover=cover,
            text=note.text,
            timestamp=note.timestamp,
            url=display_url,
            metrics=note.metrics,
        )
        return self.parse_result(contents)

    async def _fetch_author_profile(self, user_id: str | None) -> XhsUserPageData | None:
        if not user_id or not self._cookies_for_page():
            return None
        url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        try:
            async with self.session.get(
                url,
                headers=self.web_headers,
                proxy=self.proxy,
            ) as response:
                if response.status >= 400:
                    return None
                html = await response.text()
        except (ClientError, TimeoutError, UnicodeError):
            return None
        try:
            state = extract_initial_state(html)
            user = state.get("user")
            if not isinstance(user, dict):
                return None
            page = msgspec.convert(user, type=XhsUserPage)
            return page.user_page_data
        except (ParseException, msgspec.ValidationError, TypeError, ValueError):
            return None

    def _build_image_contents(self, note: XhsNote) -> list[MediaContent]:
        contents: list[MediaContent] = []
        for image in note.image_list:
            image_url = image.image_url
            if not image_url:
                continue
            if image.live_photo:
                stream = select_live_photo_stream(image)
                if stream is not None:
                    contents.append(
                        self.create_live_photo_content(
                            stream.url,
                            image_url,
                            cover_url=image_url,
                            headers=self.media_headers,
                            fallback_urls=stream.fallback_urls,
                        )
                    )
                    continue
            contents.extend(
                self.create_image_contents([image_url], headers=self.media_headers)
            )
        return contents
