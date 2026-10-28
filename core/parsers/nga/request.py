from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout

from ...cookie import CookieJar
from ...exception import ParseException
from .content import extract_guest_js, is_blocked_page


@dataclass(slots=True)
class NgaResponse:
    url: str
    status: int
    text: str
    set_cookies: list[str]


class NgaRequest:
    def __init__(
        self,
        *,
        session: ClientSession,
        cookiejar: CookieJar,
        headers: dict[str, str],
        timeout: int,
        retries: int,
    ) -> None:
        self.session = session
        self.cookiejar = cookiejar
        self.headers = headers
        self.timeout = timeout
        self.retries = max(0, retries)

    @staticmethod
    def canonical_url(tid: str, pid: str | None = None) -> str:
        query = {"tid": tid}
        if pid:
            query["pid"] = pid
        return f"https://bbs.nga.cn/read.php?{urlencode(query)}"

    @staticmethod
    def parse_identifiers(url: str) -> tuple[str, str | None]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        tid = (query.get("tid") or [""])[0]
        pid = (query.get("pid") or [None])[0]
        if not tid.isdigit():
            fragment_pid = parse_qs(parsed.fragment).get("pid", [None])[0]
            if fragment_pid:
                pid = fragment_pid
        if not tid.isdigit():
            raise ParseException("NGA 链接缺少有效的 tid")
        return tid, pid if pid and pid.isdigit() else None

    def _headers_for(self, url: str) -> dict[str, str]:
        headers = self.headers.copy()
        cookie = self.cookiejar.get_cookie_header_for_url(url)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get_once(self, url: str, *, allow_redirects: bool = True) -> NgaResponse:
        try:
            async with self.session.get(
                url,
                headers=self._headers_for(url),
                allow_redirects=allow_redirects,
                timeout=ClientTimeout(total=self.timeout),
            ) as resp:
                text = await resp.text(errors="replace")
                content_type = resp.headers.get("X-NGA-CONTENT-TYPE", "")
                if content_type.lower() == "short-message":
                    text = "访客不能直接访问"
                set_cookies = resp.headers.getall("Set-Cookie", [])
                self.cookiejar.update_from_response(
                    set_cookies,
                    request_url=str(resp.url),
                )
                return NgaResponse(
                    url=str(resp.url),
                    status=resp.status,
                    text=text,
                    set_cookies=set_cookies,
                )
        except (ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            raise ParseException("NGA 请求失败，请稍后重试") from exc

    async def get(self, url: str) -> NgaResponse:
        response: NgaResponse | None = None
        for attempt in range(self.retries + 1):
            response = await self._get_once(url)
            if response.status == 200 and not is_blocked_page(response.text):
                return response
            if response.status == 403:
                guest_js = extract_guest_js(response.text)
                if guest_js:
                    self.cookiejar.update_from_response(
                        [
                            f"guestJs={guest_js}; domain=.nga.cn; path=/; secure",
                            "lastpath=0; domain=.nga.cn; path=/; secure",
                        ],
                        request_url=url,
                    )
                    await asyncio.sleep(0.3)
                    retry_url = (
                        f"{url}{'&' if '?' in url else '?'}rand={random.randint(0, 999)}"
                    )
                    response = await self._get_once(retry_url)
                    if response.status == 200 and not is_blocked_page(response.text):
                        return response
            if attempt < self.retries:
                await asyncio.sleep(0.6 * (attempt + 1))
        if response is None:
            raise ParseException("NGA 请求失败")
        if response.status == 403:
            raise ParseException("NGA 拒绝访客访问，可能需要登录 Cookie")
        if response.status >= 400:
            raise ParseException(f"NGA 页面请求失败，HTTP {response.status}")
        raise ParseException("NGA 页面内容为空或被拦截")
