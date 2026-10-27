from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ...constants import COMMON_HEADER
from ...cookie import CookieJar
from ...exception import ParseException

PIXIV_BASE = "https://www.pixiv.net"
PIXIV_API_HEADERS: dict[str, str] = {
    "User-Agent": COMMON_HEADER["User-Agent"],
    "Referer": f"{PIXIV_BASE}/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
PIXIV_MEDIA_HEADERS: dict[str, str] = {
    "User-Agent": COMMON_HEADER["User-Agent"],
    "Referer": f"{PIXIV_BASE}/",
}


class PixivAPI:
    """Pixiv 网页 API 客户端，复用解析器的共享会话。"""

    def __init__(
        self,
        session_factory: Callable[[], ClientSession],
        cookiejar: CookieJar,
        *,
        proxy: str | None,
        retries: int,
    ) -> None:
        self._session_factory = session_factory
        self._cookiejar = cookiejar
        self._proxy = proxy
        self._retries = max(int(retries), 0)

    @property
    def headers(self) -> dict[str, str]:
        headers = PIXIV_API_HEADERS.copy()
        cookie = self._cookiejar.get_cookie_header_for_url(f"{PIXIV_BASE}/")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get(
        self,
        path: str,
        *,
        allow_not_found: bool = False,
    ) -> Any:
        url = f"{PIXIV_BASE}{path}"
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._session_factory().get(
                    url,
                    headers=self.headers,
                    proxy=self._proxy,
                    allow_redirects=True,
                    timeout=ClientTimeout(total=30),
                ) as response:
                    if response.status == 404 and allow_not_found:
                        return None
                    if response.status >= 400:
                        if response.status >= 500 and attempt < self._retries:
                            raise ClientError(
                                f"HTTP {response.status} {response.reason}"
                            )
                        raise ParseException(f"Pixiv API 返回 HTTP {response.status}")
                    set_cookies = response.headers.getall("Set-Cookie", [])
                    raw = await response.text()
            except (ClientError, TimeoutError, UnicodeError) as exc:
                last_error = exc
                if attempt < self._retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException("Pixiv API 请求失败") from exc

            if set_cookies:
                self._cookiejar.update_from_response(
                    set_cookies,
                    request_url=url,
                )
            try:
                data = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ParseException("Pixiv API 返回数据格式异常") from exc
            if not isinstance(data, dict):
                raise ParseException("Pixiv API 返回数据格式异常")
            if data.get("error"):
                message = str(data.get("message") or "未知错误")[:200]
                raise ParseException(f"Pixiv API 错误: {message}")
            if "body" not in data:
                raise ParseException("Pixiv API 返回数据缺少 body")
            return data["body"]
        raise ParseException("Pixiv API 请求失败") from last_error

    async def get_illust_detail(self, pid: str) -> dict[str, Any]:
        body = await self._get(f"/ajax/illust/{pid}")
        if not isinstance(body, dict):
            raise ParseException("Pixiv 作品数据格式异常")
        return body

    async def get_illust_detail_optional(self, pid: str) -> dict[str, Any] | None:
        try:
            body = await self._get(f"/ajax/illust/{pid}", allow_not_found=True)
        except ParseException:
            return None
        if body is not None and not isinstance(body, dict):
            raise ParseException("Pixiv 作品数据格式异常")
        return body

    async def get_pages(self, pid: str) -> list[dict[str, Any]]:
        body = await self._get(f"/ajax/illust/{pid}/pages")
        if not isinstance(body, list):
            raise ParseException("Pixiv 作品页面数据格式异常")
        return [item for item in body if isinstance(item, dict)]

    async def get_ugoira_meta(self, pid: str) -> dict[str, Any] | None:
        try:
            body = await self._get(
                f"/ajax/illust/{pid}/ugoira_meta",
                allow_not_found=True,
            )
        except ParseException:
            return None
        if body is not None and not isinstance(body, dict):
            return None
        return body

    async def get_user_detail(self, uid: str) -> dict[str, Any]:
        body = await self._get(f"/ajax/user/{uid}")
        if not isinstance(body, dict):
            raise ParseException("Pixiv 作者数据格式异常")
        return body

    async def get_novel_detail(self, nid: str) -> dict[str, Any]:
        body = await self._get(f"/ajax/novel/{nid}")
        if not isinstance(body, dict):
            raise ParseException("Pixiv 小说数据格式异常")
        return body

    async def get_novel_detail_optional(self, nid: str) -> dict[str, Any] | None:
        try:
            body = await self._get(f"/ajax/novel/{nid}", allow_not_found=True)
        except ParseException:
            return None
        if body is not None and not isinstance(body, dict):
            raise ParseException("Pixiv 小说数据格式异常")
        return body

    async def get_novel_series_total(self, series_id: str) -> int | None:
        try:
            body = await self._get(f"/ajax/novel/series/{series_id}")
        except ParseException:
            return None
        if not isinstance(body, dict):
            return None
        try:
            return int(body["total"])
        except (KeyError, TypeError, ValueError):
            return None

    async def get_manga_series_total(
        self,
        series_id: str,
        user_id: str,
    ) -> int | None:
        url = f"{PIXIV_BASE}/user/{user_id}/series/{series_id}"
        try:
            async with self._session_factory().get(
                url,
                headers=self.headers,
                proxy=self._proxy,
                allow_redirects=True,
                timeout=ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    return None
                html = await response.text()
        except (ClientError, TimeoutError, UnicodeError):
            return None
        matched = re.search(r"(\d+)部作品", html)
        if not matched:
            return None
        return int(matched.group(1))
