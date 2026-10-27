from __future__ import annotations

import asyncio
from pathlib import Path
from re import Match
from typing import Any, ClassVar
from urllib.parse import urlsplit

from aiohttp import ClientError
from PIL import Image, ImageFilter

from astrbot.api import logger

from ...data import ImageContent, MediaContent, Platform, TextContent
from ...config import PluginConfig
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle

API_URL = "https://whatslink.info/api/v1/link"
SCREENSHOT_HOST = "whatslink.info"


def format_size(size_bytes: object) -> str:
    """把字节数格式化成可读大小。"""
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int | float):
        return "未知"
    size = float(size_bytes)
    if size < 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def extract_screenshot_urls(data: object) -> list[str]:
    """只接受接口返回的 whatslink 预览图，避免任意 URL 进入下载链路。"""
    if not isinstance(data, dict):
        return []
    screenshots = data.get("screenshots")
    if not isinstance(screenshots, list):
        return []

    urls: list[str] = []
    for item in screenshots:
        value = item.get("screenshot") if isinstance(item, dict) else None
        if not isinstance(value, str):
            continue
        url = value.strip()
        if not url.startswith("https://"):
            continue
        parsed = urlsplit(url)
        if parsed.hostname != SCREENSHOT_HOST:
            continue
        if url not in urls:
            urls.append(url)
    return urls


class MagnetParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="magnet",
        display_name="磁力链接",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    async def _fetch_info(self, magnet: str) -> dict[str, Any]:
        try:
            async with self.session.get(
                API_URL,
                params={"url": magnet},
                headers=self.headers,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"磁力解析失败: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (ClientError, TimeoutError) as exc:
            raise ParseException("磁力解析请求失败") from exc
        except ValueError as exc:
            raise ParseException("磁力接口返回数据异常") from exc

        if not isinstance(data, dict):
            raise ParseException("磁力接口返回数据异常")
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            raise ParseException("磁力链接无法识别或资源不存在")
        return data

    @staticmethod
    def _blur(image_path: Path, radius: int = 15) -> Path:
        output_path = image_path.with_name(
            f"{image_path.stem}_blur{image_path.suffix or '.jpg'}"
        )
        with Image.open(image_path) as image:
            blurred = image.filter(ImageFilter.GaussianBlur(radius=radius))
            blurred.save(output_path)
        return output_path

    def _image_contents(
        self,
        image_urls: list[str],
        *,
        blur: bool,
    ) -> list[ImageContent]:
        contents: list[ImageContent] = []
        request_headers = {**self.headers, "Referer": API_URL}
        proxy = self.proxy

        for image_url in image_urls:
            content = ImageContent()

            async def load_image(
                current_url: str = image_url,
                should_blur: bool = blur,
            ) -> Path:
                image_path = await self.downloader.download_img(
                    current_url,
                    headers=request_headers,
                    proxy=proxy,
                )
                if not should_blur:
                    return image_path
                try:
                    return await asyncio.to_thread(self._blur, image_path)
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "[磁力] 预览图模糊失败，回退原图 | "
                        f"error_type={type(exc).__name__}"
                    )
                    return image_path

            content.set_loader(load_image)
            contents.append(content)
        return contents

    @handle("magnet", r"magnet:\?(?P<query>[^\s<>\"']+)")
    async def _parse_magnet(self, searched: Match[str]):
        magnet = searched.group(0)
        data = await self._fetch_info(magnet)

        title = str(data.get("name") or "未知资源")
        file_type = str(data.get("file_type") or "未知")
        size = format_size(data.get("size"))
        image_urls = extract_screenshot_urls(data)
        mode = self.mycfg.image_send_mode

        text = (
            f"标题：{title}\n\n"
            f"磁链类型：{file_type}\n\n"
            f"文件大小：{size}"
        )
        await self.send_desc_common(
            title,
            None,
            [],
            text=text,
            url=magnet,
            extra={"info": f"预览图：{len(image_urls)} 张"},
        )

        contents: list[MediaContent] = [TextContent(text)]
        if image_urls and mode in {"blur", "original"}:
            contents.extend(
                self._image_contents(image_urls, blur=mode == "blur")
            )
        return self.parse_result(contents)
