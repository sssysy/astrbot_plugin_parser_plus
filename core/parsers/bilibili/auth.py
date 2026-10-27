"""Bilibili TV 端扫码登录与凭证续期。"""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlencode
from hashlib import md5

import qrcode

from astrbot.api import logger


TV_AUTH_CODE_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
TV_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
REFRESH_TOKEN_URL = "https://passport.bilibili.com/api/v2/oauth2/refresh_token"

# lite 使用的 android_hd 客户端签名参数。
APPKEY = "dfca71928277209b"
APPSEC = "b5475a8825547a4fc26c7d518eaaa02e"


class QrLoginState(str, Enum):
    WAITING = "waiting"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


class BiliAuthError(RuntimeError):
    """扫码或凭证续期失败。"""


@dataclass(slots=True)
class BiliCredential:
    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""
    mid: int = 0
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0

    @property
    def cookie_header(self) -> str:
        return "; ".join(
            f"{name}={value}"
            for name, value in (
                ("SESSDATA", self.sessdata),
                ("bili_jct", self.bili_jct),
                ("DedeUserID", self.dedeuserid),
            )
            if value
        )

    @property
    def needs_refresh(self) -> bool:
        return bool(self.expires_at and time.time() >= self.expires_at)

    @property
    def can_refresh(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    @classmethod
    def from_poll_payload(cls, payload: dict) -> "BiliCredential":
        cookies = {
            item.get("name"): item.get("value", "")
            for item in (payload.get("cookie_info") or {}).get("cookies", [])
            if isinstance(item, dict)
        }
        token = payload.get("token_info") or {}
        credential = cls(
            sessdata=cookies.get("SESSDATA", ""),
            bili_jct=cookies.get("bili_jct", ""),
            dedeuserid=cookies.get("DedeUserID", ""),
            mid=int(token.get("mid") or 0),
            access_token=token.get("access_token", ""),
            refresh_token=token.get("refresh_token", ""),
            expires_at=time.time() + float(token.get("expires_in") or 0),
        )
        if not credential.sessdata or not credential.can_refresh:
            raise BiliAuthError("登录成功，但返回凭证不完整")
        return credential

    @classmethod
    def from_dict(cls, data: dict) -> "BiliCredential":
        return cls(
            sessdata=data.get("sessdata", ""),
            bili_jct=data.get("bili_jct", ""),
            dedeuserid=data.get("dedeuserid", ""),
            mid=int(data.get("mid") or 0),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=float(data.get("expires_at") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "sessdata": self.sessdata,
            "bili_jct": self.bili_jct,
            "dedeuserid": self.dedeuserid,
            "mid": self.mid,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }


def _signed(params: dict) -> dict:
    values = {"appkey": APPKEY, **params}
    query = urlencode(sorted(values.items()))
    values["sign"] = md5((query + APPSEC).encode()).hexdigest()
    return values


@dataclass(slots=True)
class QrLogin:
    link: str = ""
    auth_code: str = ""
    credential: BiliCredential | None = None

    async def generate_qrcode(self) -> bytes:
        import aiohttp

        params = _signed({"local_id": "0", "ts": int(time.time())})
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TV_AUTH_CODE_URL, params=params
            ) as response:
                payload = await response.json(content_type=None)
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise BiliAuthError("获取 B 站登录二维码失败")
        data = payload.get("data") or {}
        self.link = data.get("url", "")
        self.auth_code = data.get("auth_code", "")
        if not self.link or not self.auth_code:
            raise BiliAuthError("B 站登录二维码响应无效")
        image = qrcode.QRCode(error_correction=1, box_size=10, border=2)
        image.add_data(self.link)
        image.make(fit=True)
        buffer = io.BytesIO()
        image.make_image(
            fill_color="black", back_color="white"
        ).save(buffer, format="PNG")
        return buffer.getvalue()

    async def poll_once(self) -> QrLoginState:
        import aiohttp

        if not self.auth_code:
            raise BiliAuthError("二维码尚未生成")
        params = _signed(
            {
                "auth_code": self.auth_code,
                "local_id": "0",
                "ts": int(time.time()),
            }
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(TV_POLL_URL, params=params) as response:
                payload = await response.json(content_type=None)
        code = payload.get("code") if isinstance(payload, dict) else None
        if code == 0:
            self.credential = BiliCredential.from_poll_payload(
                payload.get("data") or {}
            )
            return QrLoginState.CONFIRMED
        if code == 86038:
            return QrLoginState.EXPIRED
        if code == 86039:
            return QrLoginState.WAITING
        if code == 86090:
            return QrLoginState.SCANNED
        raise BiliAuthError("B 站扫码登录状态异常")


async def refresh_credential(
    credential: BiliCredential,
) -> BiliCredential:
    import aiohttp

    if not credential.can_refresh:
        raise BiliAuthError("凭证缺少 access_token 或 refresh_token")
    params = _signed(
        {
            "access_key": credential.access_token,
            "refresh_token": credential.refresh_token,
            "ts": int(time.time()),
        }
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(REFRESH_TOKEN_URL, params=params) as response:
            payload = await response.json(content_type=None)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = "B 站凭证续期失败"
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or message)
        raise BiliAuthError(message)
    return BiliCredential.from_poll_payload(payload.get("data") or {})


def load_credential(path: Path) -> BiliCredential | None:
    try:
        data = path.read_text(encoding="utf-8")
        import json

        return BiliCredential.from_dict(json.loads(data))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"[哔哩哔哩] 读取凭证失败 | file={path.name}")
        logger.debug(f"[哔哩哔哩] 凭证读取错误类型: {type(exc).__name__}")
        return None


def save_credential(path: Path, credential: BiliCredential) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(credential.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass(slots=True)
class PollResult:
    messages: list[str]
    credential: BiliCredential | None


async def poll_login(
    login: QrLogin,
    *,
    interval: float = 2,
    attempts: int = 60,
) -> PollResult:
    """轮询登录状态；每条状态更新和最终凭证分开返回。"""
    messages: list[str] = []
    scanned_announced = False
    for _ in range(attempts):
        state = await login.poll_once()
        if state is QrLoginState.CONFIRMED:
            messages.append("登录成功")
            return PollResult(messages, login.credential)
        if state is QrLoginState.EXPIRED:
            messages.append("二维码过期")
            return PollResult(messages, None)
        if state is QrLoginState.SCANNED and not scanned_announced:
            messages.append("二维码已扫描，请在手机上确认")
            scanned_announced = True
        await asyncio.sleep(interval)
    messages.append("二维码登录超时")
    return PollResult(messages, None)
