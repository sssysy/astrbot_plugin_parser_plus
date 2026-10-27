from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from ...exception import ParseException
from ...cookie import CookieJar

API_HOST: Final[str] = "api.iwara.tv"
IMPERSONATE: Final[str] = "chrome120"
CHALLENGE_MARKERS: Final[tuple[str, ...]] = (
    "just a moment",
    "cf-chl-",
    "challenge-platform",
)


def is_challenge_page(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


class IwaraClient:
    """Iwara 请求层：curl_cffi 浏览器指纹、Cookie 与有限重试。"""

    def __init__(
        self,
        *,
        cookiejar: CookieJar,
        headers: Mapping[str, str],
        proxy: str | None,
        timeout: int,
        retries: int,
    ):
        self.cookiejar = cookiejar
        self.headers = dict(headers)
        self.proxy = proxy
        self.timeout = timeout
        self.retries = max(0, retries)
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                headers=self.headers,
                timeout=self.timeout,
                impersonate=IMPERSONATE,
                proxy=self.proxy,
            )
            self._seed_cookies()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            self.cookiejar.sync_from_http_cookiejar(self._session.cookies.jar)
            await self._session.close()
            self._session = None

    def _seed_cookies(self) -> None:
        if self._session is None:
            return
        for cookie in self.cookiejar.cookies:
            self._session.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
                secure=cookie.secure,
            )

    async def fetch_api(self, url: str) -> Any:
        payload: Any = None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.session.get(
                    url,
                    headers=self._request_headers(),
                )
                if response.status_code == 404:
                    raise ParseException("Iwara 作品不存在或已被删除")
                if response.status_code == 403 and is_challenge_page(
                    response.text
                ):
                    raise ParseException(
                        "Iwara 触发了 Cloudflare 验证，请稍后重试或更新 Cookie"
                    )
                if response.status_code >= 400:
                    raise ParseException(
                        f"Iwara 请求失败（HTTP {response.status_code}）"
                    )
                payload = response.json()
                break
            except ParseException:
                raise
            except (RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(1 + attempt)
        if payload is None:
            raise ParseException("获取 Iwara 作品信息失败") from last_error
        return payload

    async def fetch_video_files(
        self,
        file_url: str,
        *,
        x_version: str,
    ) -> Any:
        payload: Any = None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.session.get(
                    file_url,
                    headers={"x-version": x_version},
                )
                if response.status_code >= 400:
                    raise ParseException(
                        f"Iwara 视频画质请求失败（HTTP {response.status_code}）"
                    )
                payload = response.json()
                break
            except ParseException:
                raise
            except (RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(1 + attempt)
        if payload is None:
            raise ParseException("获取 Iwara 视频画质失败") from last_error
        return payload

    def _request_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        cookie = self.cookiejar.get_cookie_header_for_url(
            "https://www.iwara.tv/"
        )
        if cookie:
            headers["Cookie"] = cookie
        return headers
