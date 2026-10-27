from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import random
import re
import struct
import time
import uuid
from collections.abc import Mapping
from typing import Any, Final

from curl_cffi.requests import AsyncSession

from ...exception import ParseException

API_HOST: Final[str] = "api.xiaoheihe.cn"
LINK_TREE_PATH: Final[str] = "/bbs/app/link/tree"
CHAR_TABLE: Final[str] = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
SM_PUBLIC_KEY: Final[str] = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCXj9exmI4nQjmT52iwr+yf7hAQ"
    "06bfSZHTAHUfRBYiagCf/whhd8es0R79wBigpiHLd28TKA8b8mGR8OiiI1hV+qfyn"
    "CWihvp3mdj8MiiH6SU3lhro2hkfYzImZB0RmWr2zE4Xt1+A6Oyp6bf+W7JSxYUXHw"
    "3nNv7Td4jw4jEFKQIDAQAB"
)
SM_ORGANIZATION: Final[str] = "0yD85BjYvGFAvHaSQ1mc"
SM_APP_ID: Final[str] = "heybox_website"
SM_DEVICE_URL: Final[str] = "https://fp-it.portal101.cn/deviceprofile/v4"
GAME_INFO_PATH: Final[str] = "/game/get_game_infos/"

_DES_RULE: Final[dict[str, tuple[str, str]]] = {
    "appId": ("uy7mzc4h", "xx"),
    "canvas": ("snrn887t", "yk"),
    "clientSize": ("cpmjjgsu", "zx"),
    "organization": ("78moqjfc", "dp"),
    "os": ("je6vk6t4", "pj"),
    "platform": ("pakxhcd2", "gm"),
    "plugins": ("v51m3pzl", "kq"),
    "pmf": ("2mdeslu3", "vw"),
    "referer": ("y7bmrjlc", "ab"),
    "res": ("whxqm2a7", "hf"),
    "rtype": ("x8o2h2bl", "lo"),
    "sdkver": ("9q3dcxp2", "sc"),
    "status": ("2jbrxxw4", "an"),
    "subVersion": ("eo3i2puh", "ns"),
    "svm": ("fzj3kaeh", "qr"),
    "time": ("q2t3odsk", "nb"),
    "timezone": ("1uv05lj5", "as"),
    "tn": ("x9nzj1bp", "py"),
    "trees": ("acfs0xo4", "pi"),
    "ua": ("k92crp1t", "bj"),
    "url": ("y95hjkoo", "cf"),
    "vpw": ("r9924ab5", "ca"),
}
_PLAIN_RULE: Final[dict[str, str]] = {
    "box": "jf",
    "protocol": "protocol",
    "version": "version",
}


def nonce_for(timestamp: int, *, randomize: bool = True) -> str:
    seed = str(timestamp)
    if randomize:
        seed += str(random.random())
    return hashlib.md5(seed.encode("utf-8")).hexdigest().upper()


def _xtime(value: int) -> int:
    return ((value << 1) ^ 27) & 0xFF if value & 0x80 else (value << 1) & 0xFF


def _mul3(value: int) -> int:
    return _xtime(value) ^ value


def _mul6(value: int) -> int:
    return _mul3(_xtime(value))


def _mul12(value: int) -> int:
    return _mul6(_mul3(_xtime(value)))


def _mul14(value: int) -> int:
    return _mul12(value) ^ _mul6(value) ^ _mul3(value)


def _mix_columns(values: list[int]) -> list[int]:
    group = [*values[:4], *(0 for _ in range(max(0, 4 - len(values[:4]))))]
    output = [
        _mul14(group[0])
        ^ _mul12(group[1])
        ^ _mul6(group[2])
        ^ _mul3(group[3]),
        _mul3(group[0])
        ^ _mul14(group[1])
        ^ _mul12(group[2])
        ^ _mul6(group[3]),
        _mul6(group[0])
        ^ _mul3(group[1])
        ^ _mul14(group[2])
        ^ _mul12(group[3]),
        _mul12(group[0])
        ^ _mul6(group[1])
        ^ _mul3(group[2])
        ^ _mul14(group[3]),
    ]
    return [*output, *values[4:]]


def _scramble(text: str, table: str = CHAR_TABLE, limit: int | None = None) -> str:
    chars = table[:limit] if limit is not None else table
    return "".join(chars[ord(char) % len(chars)] for char in text)


def hkey_for(path: str, timestamp: int | None = None) -> dict[str, str | int]:
    now = int(time.time()) if timestamp is None else timestamp
    normalized = "/" + "/".join(part for part in path.split("/") if part) + "/"
    nonce = nonce_for(now, randomize=timestamp is None)
    parts = [
        _scramble(str(now + (1 if timestamp is not None else 0)), limit=-2),
        _scramble(normalized),
        _scramble(nonce),
    ]
    interleaved = "".join(
        part[index]
        for index in range(max(map(len, parts)))
        for part in parts
        if index < len(part)
    )[:20]
    digest = hashlib.md5(interleaved.encode("utf-8")).hexdigest()
    prefix = _scramble(digest[:5], limit=-4)
    suffix = str(sum(_mix_columns([ord(char) for char in digest[-6:]])) % 100).zfill(2)
    return {"hkey": prefix + suffix, "_time": now, "nonce": nonce}


def _mod_pow(base: int, exponent: int, modulus: int) -> int:
    result = 1
    base %= modulus
    while exponent:
        if exponent & 1:
            result = result * base % modulus
        base = base * base % modulus
        exponent >>= 1
    return result


def _der_rsa_public_key(der: bytes) -> tuple[int, int]:
    def read_tlv(
        data: bytes, offset: int
    ) -> tuple[int, bytes, int]:
        tag = data[offset]
        first = data[offset + 1]
        if first < 0x80:
            length = first
            start = offset + 2
        else:
            count = first & 0x7F
            length = int.from_bytes(data[offset + 2 : offset + 2 + count], "big")
            start = offset + 2 + count
        end = start + length
        if end > len(data):
            raise ValueError("truncated RSA key")
        return tag, data[start:end], end

    def read_integer(data: bytes, offset: int) -> tuple[int, int]:
        tag, value, end = read_tlv(data, offset)
        if tag != 0x02:
            raise ValueError("invalid RSA integer")
        return int.from_bytes(value, "big"), end

    tag, outer, _ = read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("invalid RSA sequence")
    algorithm_tag, algorithm, offset = read_tlv(outer, 0)
    if algorithm_tag != 0x30:
        raise ValueError("unsupported RSA key")
    oid_tag, oid, algorithm_offset = read_tlv(algorithm, 0)
    if oid_tag != 0x06 or oid != bytes.fromhex("2a864886f70d010101"):
        raise ValueError("unsupported RSA algorithm")
    if algorithm_offset < len(algorithm):
        null_tag, _, _ = read_tlv(algorithm, algorithm_offset)
        if null_tag != 0x05:
            raise ValueError("invalid RSA algorithm parameters")
    bit_tag, bit_string, _ = read_tlv(outer, offset)
    if bit_tag != 0x03 or not bit_string or bit_string[0] != 0x00:
        raise ValueError("invalid RSA bit string")
    sequence_tag, sequence, _ = read_tlv(bit_string, 1)
    if sequence_tag != 0x30:
        raise ValueError("invalid RSA public key")
    modulus, sequence_offset = read_integer(sequence, 0)
    exponent, _ = read_integer(sequence, sequence_offset)
    return modulus, exponent


def _rsa_pkcs1_v15(uid: bytes) -> bytes:
    modulus, exponent = _der_rsa_public_key(base64.b64decode(SM_PUBLIC_KEY))
    size = (modulus.bit_length() + 7) // 8
    if len(uid) > size - 11:
        raise ValueError("SM payload too large")
    message = b"\x00\x02" + bytes(random.randint(1, 255) for _ in range(size - len(uid) - 3))
    message += b"\x00" + uid
    return _mod_pow(int.from_bytes(message, "big"), exponent, modulus).to_bytes(size, "big")


def _des_encrypt_block(key: str, block: bytes) -> bytes:
    try:
        from Crypto.Cipher import DES
    except ImportError:
        DES = None
    if DES is not None:
        return DES.new(key.encode("utf-8"), DES.MODE_ECB).encrypt(block)

    key_bytes = key.encode("utf-8")
    subkeys = _des_subkeys(key_bytes)
    return _des_process_block(block, subkeys, decrypt=False)


def _des_subkeys(key: bytes) -> list[list[int]]:
    pc1 = (
        57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
        10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
        14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
    )
    pc2 = (
        14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
        23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
        44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
    )
    shifts = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
    permuted = "".join(f"{byte:08b}" for byte in key)
    key56 = [int(permuted[index - 1]) for index in pc1]
    left, right = key56[:28], key56[28:]
    subkeys: list[list[int]] = []
    for shift in shifts:
        left = left[shift:] + left[:shift]
        right = right[shift:] + right[:shift]
        combined = left + right
        subkeys.append([combined[index - 1] for index in pc2])
    return subkeys


def _des_process_block(block: bytes, subkeys: list[list[int]], *, decrypt: bool) -> bytes:
    ip = (
        58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
        62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
        57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
        61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
    )
    fp = (
        40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
        38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
        36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
        34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
    )
    e = (
        32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9,
        8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
        16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
        24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
    )
    p = (
        16, 7, 20, 21, 29, 12, 28, 17,
        1, 15, 23, 26, 5, 18, 31, 10,
        2, 8, 24, 14, 32, 27, 3, 9,
        19, 13, 30, 6, 22, 11, 4, 25,
    )
    s_boxes = (
        (
            14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
            0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
            4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
            15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13,
        ),
        (
            15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
            3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
            0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
            13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9,
        ),
        (
            10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
            13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
            13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
            1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12,
        ),
        (
            7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
            13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
            10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
            3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14,
        ),
        (
            2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
            14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
            4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
            11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3,
        ),
        (
            12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
            10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
            9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
            4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13,
        ),
        (
            4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
            13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
            1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
            6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12,
        ),
        (
            13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
            1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
            7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
            2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11,
        ),
    )

    bits = "".join(f"{byte:08b}" for byte in block)
    permuted = [int(bits[index - 1]) for index in ip]
    left, right = permuted[:32], permuted[32:]
    for key in reversed(subkeys) if decrypt else subkeys:
        expanded = [right[index - 1] for index in e]
        mixed = [expanded[index] ^ key[index] for index in range(48)]
        output: list[int] = []
        for box_index, box in enumerate(s_boxes):
            chunk = mixed[box_index * 6 : box_index * 6 + 6]
            row = (chunk[0] << 1) | chunk[5]
            column = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
            value = box[row * 16 + column]
            output.extend(int(bit) for bit in f"{value:04b}")
        f_result = [output[index - 1] for index in p]
        left, right = right, [left[index] ^ f_result[index] for index in range(32)]
    combined = right + left
    final_bits = [combined[index - 1] for index in fp]
    result = bytes(
        int("".join(str(bit) for bit in final_bits[index : index + 8]), 2)
        for index in range(0, 64, 8)
    )
    return result


def _des_encrypt(key: str, value: str) -> str:
    data = value.encode("utf-8")
    data += b"\x00" * ((-len(data)) % 8)
    encrypted = b"".join(
        _des_encrypt_block(key, data[index : index + 8])
        for index in range(0, len(data), 8)
    )
    return base64.b64encode(encrypted).decode("ascii")


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    iv = b"0102030405060708"
    data += b"\x00"
    data += b"\x00" * ((-len(data)) % 16)
    try:
        from Crypto.Cipher import AES
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(data) + encryptor.finalize()
    return AES.new(key, AES.MODE_CBC, iv).encrypt(data)


def _business_tn(value: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(value):
        item = value[key]
        if isinstance(item, int | float):
            parts.append(str(item * 10000))
        elif isinstance(item, Mapping):
            parts.append(_business_tn(item))
        else:
            parts.append(str(item))
    return "".join(parts)


def _business_smid() -> str:
    now = time.localtime()
    stamp = (
        f"{now.tm_year}{now.tm_mon:02d}{now.tm_mday:02d}"
        f"{now.tm_hour:02d}{now.tm_min:02d}{now.tm_sec:02d}"
    )
    value = stamp + hashlib.md5(str(uuid.uuid4()).encode("utf-8")).hexdigest() + "00"
    suffix = hashlib.md5(f"smsk_web_{value}".encode("utf-8")).hexdigest()[:14]
    return value + suffix + "0"


def browser_payload() -> dict[str, Any]:
    uid = str(uuid.uuid4()).encode("utf-8")
    primary = hashlib.md5(uid).hexdigest()[:16]
    encrypted_uid = base64.b64encode(_rsa_pkcs1_v15(uid)).decode("ascii")
    current = int(time.time() * 1000)
    browser: dict[str, Any] = {
        "plugins": (
            "MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,"
            "MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1"
        ),
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
        ),
        "canvas": "".join(random.choice("0123456789abcdef") for _ in range(8)),
        "timezone": -480,
        "platform": "Win32",
        "url": "https://www.xiaoheihe.cn/",
        "referer": "",
        "res": "1920_1080_24_1.25",
        "clientSize": "0_0_1080_1920_1920_1080_1920_1080",
        "status": "0011",
        "vpw": str(uuid.uuid4()),
        "svm": current,
        "trees": str(uuid.uuid4()),
        "pmf": current,
    }
    target = {
        **browser,
        "protocol": 102,
        "organization": SM_ORGANIZATION,
        "appId": SM_APP_ID,
        "os": "web",
        "version": "3.0.0",
        "sdkver": "3.0.0",
        "box": "",
        "rtype": "all",
        "smid": _business_smid(),
        "subVersion": "1.0.0",
        "time": 0,
    }
    target["tn"] = hashlib.md5(_business_tn(target).encode("utf-8")).hexdigest()
    encrypted: dict[str, Any] = {}
    for key, value in target.items():
        if key in _DES_RULE:
            des_key, alias = _DES_RULE[key]
            encrypted[alias] = _des_encrypt(des_key, str(value))
        else:
            encrypted[_PLAIN_RULE.get(key, key)] = value
    compressed = gzip.compress(
        json.dumps(encrypted, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=2,
        mtime=0,
    )
    return {
        "appId": SM_APP_ID,
        "compress": 2,
        "data": _aes_encrypt(
            base64.b64encode(compressed), primary.encode("utf-8")
        ).hex(),
        "encode": 5,
        "ep": encrypted_uid,
        "organization": SM_ORGANIZATION,
        "os": "web",
    }


def build_link_tree_params(
    link_id: str,
    *,
    device_id: str,
    timestamp: int | None = None,
) -> dict[str, str | int]:
    return {
        "os_type": "web",
        "app": "heybox",
        "client_type": "web",
        "version": "999.0.4",
        **hkey_for(LINK_TREE_PATH, timestamp),
        "link_id": link_id,
        "page": "1",
        "index": "1",
        "limit": "5",
        "x_client_type": "weboutapp",
        "x_app": "heybox_website",
        "x_os_type": "Windows",
        "web_version": "2.5",
    }


class XiaoheiheClient:
    """小黑盒请求与临时设备凭证，不负责解析或下载媒体。"""

    def __init__(
        self,
        *,
        headers: Mapping[str, str],
        proxy: str | None,
        timeout: int,
        retries: int = 2,
    ):
        self.headers = dict(headers)
        self.proxy = proxy
        self.timeout = timeout
        self.retries = max(0, retries)
        self._session: AsyncSession | None = None
        self._device_id: str | None = None
        self._device_lock = asyncio.Lock()

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                headers=self.headers,
                timeout=self.timeout,
                impersonate="chrome",
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._device_id = None

    @property
    def cookie_token(self) -> str | None:
        cookie = self.headers.get("cookie") or self.headers.get("Cookie") or ""
        matched = re.search(r"(?:^|;\s*)x_xhh_tokenid=([^;]+)", cookie)
        return matched.group(1) if matched else None

    @property
    def token(self) -> str | None:
        if self.cookie_token:
            return self.cookie_token
        return f"B{self._device_id}" if self._device_id else None

    async def ensure_device(self) -> None:
        if self.token:
            return
        async with self._device_lock:
            if self.token:
                return
            payload: Any = None
            last_error: Exception | None = None
            for attempt in range(self.retries + 1):
                try:
                    response = await self.session.post(
                        SM_DEVICE_URL,
                        json=browser_payload(),
                        headers={"accept": "application/json, text/plain, */*"},
                        proxies={"https": self.proxy, "http": self.proxy}
                        if self.proxy
                        else None,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(1 + attempt)
            if payload is None:
                raise ParseException("获取小黑盒设备凭证失败") from last_error
            detail = payload.get("detail") if isinstance(payload, dict) else None
            device_id = detail.get("deviceId") if isinstance(detail, dict) else None
            if not device_id:
                raise ParseException("小黑盒设备凭证为空")
            self._device_id = str(device_id)

    async def fetch_link_tree(self, link_id: str) -> dict[str, Any]:
        await self.ensure_device()
        payload: Any = None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.session.get(
                    f"https://{API_HOST}{LINK_TREE_PATH}",
                    params=build_link_tree_params(
                        link_id,
                        device_id=self._device_id or "",
                    ),
                    cookies={"x_xhh_tokenid": self.token}
                    if self.token
                    else None,
                    headers=self.headers,
                    proxies={"https": self.proxy, "http": self.proxy}
                    if self.proxy
                    else None,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(1 + attempt)
        if payload is None:
            raise ParseException("小黑盒帖子请求失败") from last_error
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ParseException("小黑盒帖子不存在、不可访问或触发风控")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ParseException("小黑盒帖子数据为空")
        return result

    async def fetch_game_intro(self, steam_appid: int) -> dict[str, Any]:
        try:
            response = await self.session.get(
                "https://api.xiaoheihe.cn/game/game_introduction/",
                params={"steam_appid": steam_appid, "return_json": 1},
                headers=self.headers,
                proxies={"https": self.proxy, "http": self.proxy}
                if self.proxy
                else None,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return {}
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return {}
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def fetch_game_info(self, appid: str) -> dict[str, Any]:
        params = {
            "os_type": "web",
            "app": "heybox",
            "client_type": "web",
            "version": "999.0.4",
            **hkey_for(GAME_INFO_PATH),
            "appids": appid,
            "x_client_type": "weboutapp",
            "x_app": "heybox_website",
            "x_os_type": "Windows",
            "web_version": "2.5",
        }
        try:
            response = await self.session.get(
                f"https://{API_HOST}{GAME_INFO_PATH}",
                params=params,
                headers=self.headers,
                proxies={"https": self.proxy, "http": self.proxy}
                if self.proxy
                else None,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ParseException("获取小黑盒游戏信息失败") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ParseException("小黑盒游戏不存在、不可访问或触发风控")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ParseException("小黑盒游戏信息为空")
        base = result.get("base_infos")
        if not isinstance(base, list) or not base or not isinstance(base[0], dict):
            raise ParseException("小黑盒游戏信息为空")
        return base[0]

