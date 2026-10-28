from __future__ import annotations

import asyncio
from typing import Final

from aiohttp import ClientError
from google.protobuf.message import DecodeError

from astrbot.api import logger

from ...exception import ParseException
from .PbPageResIdl_pb2 import PbPageResIdl

PAGE_URL: Final[str] = "https://tiebac.baidu.com/c/f/pb/page"
BOUNDARY: Final[str] = "-*_r1999"
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _message_field(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _string_field(number: int, value: str) -> bytes:
    return _message_field(number, value.encode())


def build_page_request(tid: int) -> bytes:
    """构造贴吧移动端 page 请求，字段号与 PbPageReqIdl.proto 一致。"""
    common = _int_field(1, 2) + _string_field(2, "12.64.1.1")
    data = (
        _message_field(25, common)
        + _int_field(4, tid)
        + _int_field(18, 1)
        + _int_field(13, 30)
        + _int_field(6, 0)
        + _int_field(5, 0)
        + _int_field(8, 1)
        + _int_field(9, 5)
        + _int_field(74, 1)
    )
    return _message_field(1, data)


def multipart_body(payload: bytes) -> bytes:
    return (
        (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="data"; filename="file"\r\n'
            "\r\n"
        ).encode()
        + payload
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )


class TiebaClient:
    """贴吧 protobuf 请求层，复用 BaseParser 会话和有限重试。"""

    def __init__(
        self,
        *,
        session_provider,
        proxy: str | None,
        timeout: float,
        retries: int,
    ):
        self._session_provider = session_provider
        self.proxy = proxy
        self.timeout = timeout
        self.retries = max(0, retries)

    async def fetch_page(self, tid: int) -> PbPageResIdl:
        headers = {
            "x_bd_data_type": "protobuf",
            "Connection": "keep-alive",
            "Accept-Encoding": "gzip",
            "User-Agent": "miku/39",
            "Host": "tiebac.baidu.com",
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        }
        body = multipart_body(build_page_request(tid))
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                async with self._session_provider().post(
                    PAGE_URL,
                    params={"cmd": 302001},
                    headers=headers,
                    data=body,
                    proxy=self.proxy,
                ) as response:
                    raw = await response.read()
                    if response.status in RETRYABLE_STATUSES:
                        last_error = ClientError(f"HTTP {response.status}")
                        if attempt < self.retries:
                            await asyncio.sleep(1 + attempt)
                            continue
                        raise ParseException("贴吧接口暂时不可用，请稍后重试")
                    if response.status >= 400:
                        raise ParseException(
                            f"贴吧接口请求失败: HTTP {response.status}"
                        )
            except ParseException:
                raise
            except (ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise ParseException("贴吧接口请求失败，请稍后重试") from exc

            result = PbPageResIdl()
            try:
                result.ParseFromString(raw)
            except (DecodeError,) as exc:
                raise ParseException("贴吧接口返回数据异常") from exc
            if result.error.errorno:
                logger.warning(
                    "[百度贴吧] 接口返回错误 | tid=%s | code=%s",
                    tid,
                    result.error.errorno,
                )
                raise ParseException("贴吧帖子不可用或已被删除")
            return result
        raise ParseException("贴吧接口请求失败") from last_error
