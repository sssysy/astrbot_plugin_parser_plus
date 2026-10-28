from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ...data import Metric
from ...utils import format_num


XOR_CODE = 23442827791579
MASK_CODE = 2251799813685247
MAX_AID = 1 << 51
ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
ENCODE_MAP = (8, 7, 0, 5, 1, 3, 2, 4, 6)
DECODE_MAP = tuple(reversed(ENCODE_MAP))


def av2bv(aid: int) -> str:
    bvid = [""] * 9
    temp = (MAX_AID | aid) ^ XOR_CODE
    for index in range(len(ENCODE_MAP)):
        bvid[ENCODE_MAP[index]] = ALPHABET[temp % len(ALPHABET)]
        temp //= len(ALPHABET)
    return "BV1" + "".join(bvid)


def bv2av(bvid: str) -> int:
    if not bvid.startswith("BV1") or len(bvid) != 12:
        raise ValueError("无效的 BV 号")
    temp = 0
    body = bvid[3:]
    for index in range(len(DECODE_MAP)):
        temp = temp * len(ALPHABET) + ALPHABET.index(body[DECODE_MAP[index]])
    return (temp & MASK_CODE) ^ XOR_CODE


def clean_cover(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://"):
        return f"https://{value[7:]}"
    return value


def normalize_url(value: str) -> str:
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://"):
        return f"https://{value[7:]}"
    return value


def metric(label: str, value: Any) -> Metric:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    return Metric(icon=None, label=label, value=format_num(number))


def parse_dynamic_time(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(value)
    if not isinstance(value, str):
        return None
    from datetime import datetime

    for fmt in ("%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    return None


def is_bilibili_media_host(url: str) -> bool:
    """B 站直连 CDN；不包含 PCDN 域名。"""
    host = (urlsplit(url).hostname or "").lower()
    return (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or host == "hdslb.com"
        or host.endswith(".hdslb.com")
        or host == "biliimg.com"
        or host.endswith(".biliimg.com")
        or host == "bilivideo.com"
        or host.endswith(".bilivideo.com")
        or host == "bilivideo.cn"
        or host.endswith(".bilivideo.cn")
    )


def is_allowed_media_host(url: str) -> bool:
    """允许的媒体来源，包含 B 站偶发下发的 PCDN 域名。"""
    if is_bilibili_media_host(url):
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in (
            "mcdn.bilivideo.com",
            "mcdn.bilivideo.cn",
            "szbdyd.com",
            "mountaintoys.cn",
            "nexusedgeio.com",
            "ahdohpiechei.com",
        )
    )


def extract_rich_text(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type") or node.get("node_type")
        if node_type in {"TEXT_NODE_TYPE_WORD", "WORDS"}:
            word = node.get("word")
            if isinstance(word, dict):
                parts.append(str(word.get("words") or ""))
            elif isinstance(word, str):
                parts.append(word)
            else:
                parts.append(str(node.get("raw_text") or ""))
        elif node_type in {"TEXT_NODE_TYPE_RICH", "RICH"}:
            rich = node.get("rich")
            if isinstance(rich, dict):
                parts.append(str(rich.get("text") or rich.get("orig_text") or ""))
        else:
            raw = node.get("raw_text")
            if raw:
                parts.append(str(raw))
    return "".join(parts)
