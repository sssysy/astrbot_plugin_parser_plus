from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import msgspec


class CdnUrl(msgspec.Struct):
    cdn: str = ""
    url: str | None = None


class Atlas(msgspec.Struct):
    cdn_list: list[CdnUrl] = msgspec.field(name="cdnList", default_factory=list)
    img_route_list: list[str] = msgspec.field(name="list", default_factory=list)

    @property
    def img_urls(self) -> list[str]:
        if not self.img_route_list:
            return []
        cdns = [item.cdn for item in self.cdn_list if item.cdn]
        if not cdns:
            return []
        return [
            f"https://{cdn}/{route.lstrip('/')}"
            for route in self.img_route_list
            for cdn in cdns[:1]
        ]


class ExtParams(msgspec.Struct):
    atlas: Atlas | None = None


class Photo(msgspec.Struct):
    caption: str = ""
    timestamp: int = 0
    duration: int = 0
    user_name: str = msgspec.field(default="未知用户", name="userName")
    head_url: str | None = msgspec.field(default=None, name="headUrl")
    like_count: int = msgspec.field(default=0, name="likeCount")
    comment_count: int = msgspec.field(default=0, name="commentCount")
    view_count: int = msgspec.field(default=0, name="viewCount")
    share_count: int = msgspec.field(default=0, name="shareCount")
    cover_urls: list[CdnUrl] = msgspec.field(
        name="coverUrls", default_factory=list
    )
    main_mv_urls: list[CdnUrl] = msgspec.field(
        name="mainMvUrls", default_factory=list
    )
    ext_params: ExtParams = msgspec.field(
        name="ext_params", default_factory=ExtParams
    )
    photo_id: str = msgspec.field(default="", name="photoId")
    user_id: int | str | None = msgspec.field(default=None, name="userId")

    @property
    def author_name(self) -> str:
        return self.user_name.replace("\u3164", "").strip() or "未知用户"

    @property
    def cover_urls_list(self) -> list[str]:
        return [item.url for item in self.cover_urls if item.url]

    @property
    def video_urls(self) -> list[str]:
        return [item.url for item in self.main_mv_urls if item.url]

    @property
    def image_urls(self) -> list[str]:
        atlas = self.ext_params.atlas
        return atlas.img_urls if atlas is not None else []


class PhotoInfo(msgspec.Struct):
    photo: Photo


PHOTO_INFO_PATH = "/rest/wd/ugH5App/photo/simple/info"
ENCODED_PHOTO_INFO_PATH = "0sftu0xe0vhI6Bqq0qipup0tjnqmf"


def _find_photo(payload: object) -> Photo | None:
    if not isinstance(payload, Mapping):
        return None
    raw_photo = payload.get("photo")
    if isinstance(raw_photo, Mapping):
        try:
            return msgspec.convert(raw_photo, type=Photo)
        except (msgspec.ValidationError, TypeError, ValueError):
            return None
    for value in payload.values():
        if isinstance(value, Mapping):
            found = _find_photo(value)
            if found is not None:
                return found
    return None


def decode_init_state(payload: str | bytes) -> Photo:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("INIT_STATE 不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("INIT_STATE 必须是 JSON 对象")

    top_level_photo = _find_photo(data)
    if top_level_photo is not None:
        return top_level_photo

    for key, value in data.items():
        if ENCODED_PHOTO_INFO_PATH in key or PHOTO_INFO_PATH in key:
            try:
                return msgspec.convert(value, type=PhotoInfo).photo
            except (msgspec.ValidationError, TypeError, ValueError) as exc:
                raise ValueError("作品信息字段异常") from exc
        photo = _find_photo(value)
        if photo is not None:
            return photo
    raise ValueError("INIT_STATE 中未找到作品信息")


def decode_photo(payload: Mapping[str, Any]) -> Photo:
    return msgspec.convert(payload, type=Photo)
