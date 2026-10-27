from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from re import Match
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageFilter

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import ImageContent, MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .models import (
    IMAGE_HOST,
    IwaraImage,
    choose_quality_url,
    is_iwara_url,
    parse_image,
    parse_video,
)
from .request import API_HOST, IwaraClient

IWARA_SALT = "_mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
IMAGE_HOSTS = {IMAGE_HOST}


class IwaraParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="iwara", display_name="Iwara")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Accept": "application/json",
                "Origin": "https://www.iwara.tv",
                "Referer": "https://www.iwara.tv/",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="iwara.tv")
        self.client = IwaraClient(
            cookiejar=self.cookiejar,
            headers=self.headers,
            proxy=self.proxy,
            timeout=int(getattr(self.cfg, "common_timeout", 30) or 30),
            retries=int(getattr(self.cfg, "download_retry_times", 2) or 2),
        )

    async def close_session(self) -> None:
        await self.client.close()
        await super().close_session()

    @property
    def api_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        cookie = self.cookiejar.get_cookie_header_for_url("https://www.iwara.tv/")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @property
    def media_headers(self) -> dict[str, str]:
        headers = self.api_headers
        headers.pop("Accept", None)
        headers["Referer"] = "https://www.iwara.tv/"
        return headers

    @handle(
        "iwara.tv/video",
        r"(?<![A-Za-z0-9.-])(?:www\.)?iwara\.tv/video/"
        r"(?P<video_id>[A-Za-z0-9]+)",
    )
    async def _parse_video(self, searched: Match[str]) -> list[MediaContent]:
        video_id = searched.group("video_id")
        video = await self._fetch_video(video_id)
        if self._should_ignore(video.rating):
            await self.send_desc_common(
                video.title,
                None,
                [],
                extra={"info": "⚠ 该视频为 R18 内容，已按配置忽略"},
                url=video.url,
            )
            return self.parse_result([])

        quality = str(getattr(self.mycfg, "video_quality", None) or "Source")
        download_url, selected_quality = await self._resolve_video_url(
            video.file_url,
            quality,
        )
        cover = self._cover_task(video.thumbnail_url, video.rating)
        await self.send_desc_video(
            video.title,
            self.create_author(
                video.user.display_name,
                video.user.avatar_url,
                headers=self.media_headers,
            ),
            cover=cover,
            duration=video.duration,
            quality=selected_quality,
            text=self._description_text(video.body, video.tags, "视频"),
            timestamp=video.timestamp or None,
            url=video.url,
        )
        return self.parse_result(
            [
                self.create_video_content(
                    download_url,
                    cover_url=None,
                    duration=video.duration,
                    headers=self.media_headers,
                    retry_http_statuses={403},
                )
            ]
        )

    @handle(
        "iwara.tv/image",
        r"(?<![A-Za-z0-9.-])(?:www\.)?iwara\.tv/image/"
        r"(?P<image_id>[A-Za-z0-9]+)",
    )
    async def _parse_image(self, searched: Match[str]) -> list[MediaContent]:
        image_id = searched.group("image_id")
        image = await self._fetch_image(image_id)
        if self._should_ignore(image.rating):
            await self.send_desc_common(
                image.title,
                None,
                [],
                extra={"info": "⚠ 该图片为 R18 内容，已按配置忽略"},
                url=image.url,
            )
            return self.parse_result([])

        nsfw_mode = str(getattr(self.mycfg, "nsfw", None) or "blur")
        blurred = image.rating == "ecchi" and nsfw_mode == "blur"
        urls = image.large_image_urls if blurred else image.image_urls
        contents = self._build_image_contents(urls, blur=blurred)
        if not contents:
            raise ParseException("Iwara 图片列表为空")

        await self.send_desc_common(
            image.title,
            self.create_author(
                image.user.display_name,
                image.user.avatar_url,
                headers=self.media_headers,
            ),
            [],
            cover=self._cover_task(image.thumbnail_url, image.rating),
            text=self._description_text(image.body, image.tags, "图片"),
            timestamp=image.timestamp or None,
            url=image.url,
        )
        return self.parse_result(contents)

    async def _fetch_video(self, video_id: str) -> IwaraVideo:
        payload = await self._fetch_json(f"https://{API_HOST}/video/{video_id}")
        return parse_video(payload)

    async def _fetch_image(self, image_id: str) -> IwaraImage:
        payload = await self._fetch_json(f"https://{API_HOST}/image/{image_id}")
        return parse_image(payload)

    async def _fetch_json(self, url: str) -> Any:
        return await self.client.fetch_api(url)

    async def _resolve_video_url(self, file_url: str, quality: str) -> tuple[str, str]:
        if not self._is_iwara_url(file_url):
            raise ParseException("Iwara 视频文件地址来源异常")
        payload = await self._fetch_video_files(file_url)
        download_url, selected = choose_quality_url(payload, quality)
        if not self._is_iwara_url(download_url):
            raise ParseException("Iwara 视频下载地址来源异常")
        return download_url, selected

    async def _fetch_video_files(self, file_url: str) -> Any:
        parsed = urlparse(file_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        expires = parse_qs(parsed.query).get("expires", [""])[0]
        if not file_id or not expires:
            raise ParseException("Iwara 视频文件地址格式异常")
        x_version = hashlib.sha1(
            f"{file_id}_{expires}{IWARA_SALT}".encode("utf-8")
        ).hexdigest()
        return await self.client.fetch_video_files(
            file_url,
            x_version=x_version,
        )

    def _should_ignore(self, rating: str) -> bool:
        return rating == "ecchi" and self.mycfg.nsfw == "ignore"

    def _cover_task(self, url: str | None, rating: str):
        if not url:
            return None
        nsfw_mode = str(getattr(self.mycfg, "nsfw", None) or "blur")
        if rating != "ecchi" or nsfw_mode == "send":
            return self.downloader.download_img(
                url,
                headers=self.media_headers,
                proxy=self.proxy,
            )

        async def load_blurred_cover() -> Path:
            path = await self.downloader.download_img(
                url,
                headers=self.media_headers,
                proxy=self.proxy,
            )
            return await asyncio.to_thread(self._blur, path)

        return asyncio.create_task(load_blurred_cover())

    def _build_image_contents(self, urls: tuple[str, ...], *, blur: bool):
        contents: list[MediaContent] = []
        for url in urls:
            if not self._is_image_url(url):
                continue
            content = ImageContent()

            async def load_image(
                image_url: str = url,
                apply_blur: bool = blur,
            ) -> Path:
                path = await self.downloader.download_img(
                    image_url,
                    headers=self.media_headers,
                    proxy=self.proxy,
                )
                if apply_blur:
                    return await asyncio.to_thread(self._blur, path)
                return path

            content.set_loader(load_image)
            contents.append(content)
        return contents

    @staticmethod
    def _is_image_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS

    @staticmethod
    def _is_iwara_url(url: str) -> bool:
        return is_iwara_url(url)

    @staticmethod
    def _blur(image_path: Path, radius: int = 20) -> Path:
        output_path = image_path.parent / (
            f"{image_path.stem}_blur{image_path.suffix}"
        )
        if output_path.exists():
            return output_path
        with Image.open(image_path) as image:
            image.filter(ImageFilter.GaussianBlur(radius=radius)).save(output_path)
        return output_path

    @staticmethod
    def _description_text(
        body: str | None,
        tags: tuple[str, ...],
        kind: str,
    ) -> str | None:
        parts: list[str] = []
        if body:
            parts.append(f"{kind}描述: {body}")
        if tags:
            parts.append("TAG: " + ", ".join(f"#{tag}" for tag in tags))
        return "\n\n".join(parts) or None
