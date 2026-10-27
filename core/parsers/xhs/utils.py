from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, unquote, urlparse

XHS_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com", "xhslink.cn")
MEDIA_HOST_SUFFIXES = ("xiaohongshu.com", "xhscdn.com")

NOTE_PATH_RE = re.compile(
    r"/(?:explore|discovery/item|search_result)/(?P<note_id>[0-9A-Za-z]+)"
)


def _host_matches(hostname: str, suffixes: tuple[str, ...]) -> bool:
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in suffixes
    )


def is_xhs_page_url(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return _host_matches(hostname, XHS_HOST_SUFFIXES)


def is_xhs_media_url(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return _host_matches(hostname, MEDIA_HOST_SUFFIXES)


def normalize_media_url(url: str | None) -> str:
    if not isinstance(url, str):
        return ""
    value = url.strip().replace("\\/", "/")
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("http://"):
        value = f"https://{value[7:]}"
    if not value.startswith("https://") or not is_xhs_media_url(value):
        return ""
    return value


def file_image_url(file_id: str | None) -> str:
    if not isinstance(file_id, str) or not file_id.strip():
        return ""
    encoded = quote(file_id.strip(), safe="")
    return normalize_media_url(
        f"https://ci.xiaohongshu.com/{encoded}"
        "?imageView2/2/w/1080/format/jpg"
    )


def _first_param(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    return values[0] if values else None


def _first_param_ci(params: dict[str, list[str]], name: str) -> str | None:
    lowered = name.lower()
    for key, values in params.items():
        if key.lower() == lowered and values:
            return values[0]
    return None


def note_ref_from_url(url: str) -> tuple[str | None, str | None]:
    """从长链、短链最终地址或 404 redirectPath 中提取笔记 ID。"""
    current = url
    token: str | None = None
    for _ in range(4):
        parsed = urlparse(current)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)

        token = (
            token
            or _first_param_ci(query, "xsec_token")
            or _first_param_ci(fragment, "xsec_token")
        )
        matched = NOTE_PATH_RE.search(parsed.path)
        if matched:
            return matched.group("note_id"), token

        target_note_id = (
            _first_param_ci(query, "target_note_id")
            or _first_param_ci(fragment, "target_note_id")
        )
        if target_note_id and re.fullmatch(r"[0-9A-Za-z]+", target_note_id):
            return target_note_id, token

        redirect_path = (
            _first_param_ci(query, "redirectPath")
            or _first_param_ci(fragment, "redirectPath")
        )
        if not redirect_path:
            break
        current = unquote(redirect_path)

    return None, token


def build_note_url(
    note_id: str,
    token: str | None = None,
    *,
    path: str = "discovery/item",
) -> str:
    url = f"https://www.xiaohongshu.com/{path}/{note_id}"
    if token:
        params = f"xsec_token={quote(token, safe='')}&xsec_source=pc_share"
        url = f"{url}?{params}"
    return url
