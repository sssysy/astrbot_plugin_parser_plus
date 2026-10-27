from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from astrbot.api import logger

from ...cookie import CookieJar
from ...exception import ParseException
from .sign import sign_zhihu_fetch_request

_CHALLENGE_MARKERS: Final[tuple[str, ...]] = (
    'id="zh-zse-ck"',
    "static.zhihu.com/zse-ck/",
    'appname":"zse_ck"',
)
_LOGIN_TITLE: Final[str] = "知乎 - 有问题，就会有答案"


def extract_initial_data(html_text: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html_text, "html.parser")
    node = soup.select_one('script#js-initialData[type="text/json"]')
    if node is None:
        return None
    raw = node.get_text(strip=True)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("initialState")
    return payload if isinstance(state, dict) else None


def initial_entities(initial_data: Mapping[str, Any]) -> Mapping[str, Any]:
    state = initial_data.get("initialState")
    if not isinstance(state, Mapping):
        return {}
    entities = state.get("entities")
    return entities if isinstance(entities, Mapping) else {}


def is_challenge_page(text: str, status_code: int) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS) or (
        status_code == 403 and "zse-ck" in lowered
    )


def is_login_page(final_url: str, text: str) -> bool:
    lowered_url = final_url.lower()
    lowered_text = text.lower()
    return (
        "/signin" in lowered_url
        or "/signup" in lowered_url
        or _LOGIN_TITLE in text
        or _LOGIN_TITLE in lowered_text
    )


class ZhihuClient:
    """知乎请求层：固定桌面身份、CookieJar 会话和有限降级。"""

    def __init__(
        self,
        *,
        cookiejar: CookieJar,
        headers: Mapping[str, str],
        proxy: str | None,
        timeout: int,
        retries: int,
        impersonate: str = "chrome",
    ):
        self.cookiejar = cookiejar
        self.headers = dict(headers)
        self.proxy = proxy
        self.timeout = timeout
        self.retries = max(0, retries)
        self.impersonate = impersonate
        self._session: AsyncSession | None = None
        self._homepage_ready = False
        self._lock = asyncio.Lock()

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                headers=self.headers,
                timeout=self.timeout,
                impersonate=self.impersonate,
                proxy=self.proxy,
            )
            for cookie in self.cookiejar.cookies:
                self._session.cookies.set(
                    cookie.name,
                    cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                    secure=cookie.secure,
                )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            self.cookiejar.sync_from_http_cookiejar(self._session.cookies.jar)
            await self._session.close()
            self._session = None
        self._homepage_ready = False

    def _cookie_header(self) -> str:
        return self.cookiejar.get_cookie_header_for_url("https://www.zhihu.com/")

    @property
    def dc0(self) -> str:
        return next(
            (cookie.value for cookie in self.cookiejar.cookies if cookie.name == "d_c0"),
            "",
        )

    def _request_headers(
        self,
        url: str,
        *,
        accept: str,
        sign: bool,
    ) -> dict[str, str]:
        token = self.cookiejar.get("_xsrf", domain="www.zhihu.com", secure=True)
        headers = {
            **self.headers,
            "accept": accept,
            "referer": "https://www.zhihu.com/",
            "origin": "https://www.zhihu.com",
        }
        if token:
            headers["x-xsrftoken"] = token
        if sign:
            headers.update(sign_zhihu_fetch_request(url, dc0=self.dc0))
        return headers

    async def ensure_homepage(self) -> None:
        if self._homepage_ready:
            return
        async with self._lock:
            if self._homepage_ready:
                return
            await self._request_text(
                "https://www.zhihu.com/",
                accept=(
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                sign=False,
                allow_login=False,
            )
            self._homepage_ready = True

    async def fetch_api(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_homepage()
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.session.request(
                    method,
                    url,
                    headers=self._request_headers(
                        url,
                        accept="application/json, text/plain, */*",
                        sign=True,
                    ),
                    json=json_body,
                    allow_redirects=True,
                )
                self._sync_session_cookies()
                final_url = str(response.url)
                text = response.text
                status = int(response.status_code)
                if is_challenge_page(text, status):
                    raise ParseException(
                        "知乎请求触发风控，请更新 Cookie 后重试"
                    )
                if is_login_page(final_url, text):
                    raise ParseException("知乎登录态已失效，请更新 Cookie")
                if status in (401, 403):
                    raise ParseException("知乎接口无权限，请更新或补充 Cookie")
                if status >= 400:
                    raise ParseException("知乎接口请求失败")
                payload = self._json_payload(text)
                if payload is None:
                    raise ParseException("知乎接口返回格式异常")
                return payload
            except ParseException:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(1 + attempt)
        logger.warning(
            "[知乎] 接口请求失败 | host=%s | error_type=%s",
            urlparse(url).hostname or "unknown",
            type(last_error).__name__,
        )
        raise ParseException("知乎接口请求失败") from last_error

    async def fetch_page(
        self,
        url: str,
        *,
        validator=None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        await self.ensure_homepage()
        last_error: Exception | None = None
        saw_challenge = False
        saw_login = False
        saw_invalid = False
        for attempt in range(self.retries + 1):
            try:
                response = await self.session.get(
                    url,
                    headers=self._request_headers(
                        url,
                        accept=(
                            "text/html,application/xhtml+xml,application/xml;q=0.9,"
                            "image/avif,image/webp,*/*;q=0.8"
                        ),
                        sign=False,
                    ),
                    allow_redirects=True,
                    impersonate=self.impersonate,
                )
                self._sync_session_cookies()
                text = response.text
                status = int(response.status_code)
                final_url = str(response.url)
                if is_challenge_page(text, status):
                    saw_challenge = True
                    continue
                if is_login_page(final_url, text):
                    saw_login = True
                    continue
                initial = extract_initial_data(text)
                if initial is None:
                    saw_invalid = True
                    continue
                if validator is None or validator(initial):
                    return initial, self._request_headers(
                        url,
                        accept="text/html,*/*",
                        sign=False,
                    )
                saw_invalid = True
            except Exception as exc:
                last_error = exc
            if attempt < self.retries:
                await asyncio.sleep(1 + attempt)
        if saw_challenge:
            raise ParseException("知乎页面触发风控，请更新 Cookie 后重试")
        if saw_login:
            raise ParseException("知乎登录态已失效，请更新 Cookie")
        if saw_invalid:
            raise ParseException("知乎页面结构已变化或内容不可访问")
        logger.warning(
            "[知乎] 页面请求失败 | host=%s | error_type=%s",
            urlparse(url).hostname or "unknown",
            type(last_error).__name__,
        )
        raise ParseException("知乎页面请求失败") from last_error

    async def _request_text(
        self,
        url: str,
        *,
        accept: str,
        sign: bool,
        allow_login: bool,
    ) -> str:
        response = await self.session.get(
            url,
            headers=self._request_headers(url, accept=accept, sign=sign),
            allow_redirects=True,
        )
        self._sync_session_cookies()
        text = response.text
        status = int(response.status_code)
        final_url = str(response.url)
        if is_challenge_page(text, status):
            raise ParseException("知乎访问触发风控，请更新 Cookie 后重试")
        if not allow_login and is_login_page(final_url, text):
            return text
        if status >= 400:
            raise ParseException("知乎主页访问失败")
        return text

    def _sync_session_cookies(self) -> None:
        self.cookiejar.sync_from_http_cookiejar(self.session.cookies.jar)

    @staticmethod
    def _json_payload(text: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None
