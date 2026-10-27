from __future__ import annotations

import asyncio
from pathlib import Path
from re import Match
from typing import Any, ClassVar

import aiofiles
from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import (
    Author,
    FileContent,
    ImageContent,
    MediaContent,
    Platform,
)
from ...download import Downloader
from ...exception import DownloadException, ParseException
from ..base import BaseParser, handle
from .api import (
    PIXIV_BASE,
    PIXIV_MEDIA_HEADERS,
    PixivAPI,
)
from .utils import (
    blur_image,
    build_text,
    clean_novel_text,
    images_to_pdf,
    parse_timestamp,
    ugoira_to_gif,
    ugoira_to_pdf,
)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class PixivParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="pixiv", display_name="Pixiv")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.cookiejar = CookieJar(config, self.mycfg, domain="pixiv.net")
        self.api = PixivAPI(
            lambda: self.session,
            self.cookiejar,
            proxy=self.proxy,
            retries=self.cfg.download_retry_times,
        )

    @property
    def media_headers(self) -> dict[str, str]:
        return PIXIV_MEDIA_HEADERS.copy()

    def _nsfw_action(self, x_restrict: int) -> str | None:
        if x_restrict <= 0:
            return None
        return str(self.mycfg.nsfw or "send")

    async def _get_author(self, uid: str) -> Author:
        body = await self.api.get_user_detail(uid)
        avatar_url = str(body.get("imageBig") or body.get("image") or "")
        return self.create_author(
            name=str(body.get("name") or "未知作者"),
            avatar_url=avatar_url or None,
            uid=uid,
            headers=self.media_headers,
        )

    async def _prepare_cover(self, cover_url: str, *, blur: bool) -> Path | None:
        if not cover_url:
            return None
        try:
            cover_path = await self.downloader.download_img(
                cover_url,
                headers=self.media_headers,
                proxy=self.proxy,
            )
            if blur:
                return await asyncio.to_thread(blur_image, cover_path)
            return cover_path
        except (DownloadException, OSError, ValueError) as exc:
            logger.warning(
                "[Pixiv] 封面处理失败 | "
                f"error_type={type(exc).__name__}"
            )
            return None

    async def _page_urls(self, pid: str) -> list[str]:
        pages = await self.api.get_pages(pid)
        urls = [
            str(page.get("urls", {}).get("original") or "")
            for page in pages
        ]
        return [url for url in urls if url]

    def _create_pdf_content(
        self,
        pid: str,
        image_urls: list[str],
    ) -> FileContent:
        content = FileContent(name=f"pixiv_{pid}.pdf")

        async def load_pdf() -> Path:
            paths = await self.downloader.download_imgs_without_raise(
                image_urls,
                headers=self.media_headers,
                proxy=self.proxy,
            )
            if not paths:
                raise ParseException("Pixiv 图片下载失败")
            output_path = self.cfg.cache_dir / f"pixiv_{pid}.pdf"
            return await asyncio.to_thread(images_to_pdf, paths, output_path)

        content.set_loader(load_pdf)
        return content

    async def _build_ugoira(
        self,
        pid: str,
        meta: dict[str, Any],
        *,
        as_pdf: bool,
    ) -> Path:
        zip_url = str(meta.get("originalSrc") or meta.get("src") or "")
        frames = meta.get("frames")
        if not zip_url or not isinstance(frames, list):
            raise ParseException("Pixiv 动图元数据不完整")

        zip_path = await self.downloader.download_file(
            zip_url,
            file_name=f"pixiv_ugoira_{pid}.zip",
            headers=self.media_headers,
            proxy=self.proxy,
        )
        if as_pdf:
            output_path = self.cfg.cache_dir / f"pixiv_{pid}.pdf"
            return await asyncio.to_thread(
                ugoira_to_pdf,
                zip_path,
                frames,
                output_path,
            )
        output_path = self.cfg.cache_dir / f"pixiv_{pid}.gif"
        return await asyncio.to_thread(
            ugoira_to_gif,
            zip_path,
            frames,
            output_path,
        )

    async def _get_series_info(self, body: dict[str, Any], user_id: str) -> str:
        series_nav = body.get("seriesNavData")
        if not isinstance(series_nav, dict):
            return ""
        series_id = str(series_nav.get("seriesId") or "")
        order = _to_int(series_nav.get("order"))
        series_title = str(series_nav.get("title") or "")
        series_type = str(series_nav.get("seriesType") or "manga")
        series_url = (
            f"{PIXIV_BASE}/user/{user_id}/series/{series_id}"
            if series_id and user_id
            else ""
        )
        label = "小说系列" if series_type == "novel" else "漫画系列"

        total: int | None = None
        if series_id:
            if series_type == "novel":
                total = await self.api.get_novel_series_total(series_id)
            else:
                total = await self.api.get_manga_series_total(series_id, user_id)
        if total is not None:
            return f"总共 {total} 话，现在下载第 {order} 话\n{label}: {series_url}"
        return f"系列: {series_title}\n当前下载第 {order} 话\n{label}: {series_url}"

    @handle("pixiv.net/artworks", r"pixiv\.net/artworks/(?P<pid>\d+)")
    async def _handle_artworks(self, searched: Match[str]) -> list[MediaContent]:
        pid = searched.group("pid")
        body = await self.api.get_illust_detail(pid)
        if _to_int(body.get("illustType")) == 1:
            return await self._handle_manga(pid, body)
        return await self._handle_illust(pid, body)

    @handle("pixivid", r"(?<![a-zA-Z])pixivid\s*(?P<pid>\d+)")
    @handle("pid", r"(?<![a-zA-Z])pid\s*(?P<pid>\d+)")
    async def _handle_pid(self, searched: Match[str]) -> list[MediaContent]:
        pid = searched.group("pid")
        body = await self.api.get_illust_detail_optional(pid)
        if body is not None:
            if _to_int(body.get("illustType")) == 1:
                return await self._handle_manga(pid, body)
            return await self._handle_illust(pid, body)
        novel = await self.api.get_novel_detail_optional(pid)
        if novel is None:
            raise ParseException(f"PID {pid} 不是有效的插画、漫画或小说 ID")
        return await self._handle_novel_core(pid, novel)

    @handle("pixiv.net/novel/show", r"pixiv\.net/novel/show\.php\?id=(?P<nid>\d+)")
    @handle("pixiv.net/novel/", r"pixiv\.net/novel/(?P<nid>\d+)")
    async def _handle_novel(self, searched: Match[str]) -> list[MediaContent]:
        nid = searched.group("nid")
        body = await self.api.get_novel_detail(nid)
        return await self._handle_novel_core(nid, body)

    async def _handle_novel_core(
        self,
        nid: str,
        body: dict[str, Any],
    ) -> list[MediaContent]:
        action = self._nsfw_action(_to_int(body.get("xRestrict")))
        if action == "ignore":
            await self.send_desc_common(
                None,
                None,
                [],
                extra={"info": "R18 内容已忽略"},
            )
            return self.parse_result([])

        user_id = str(body.get("userId") or "")
        author = await self._get_author(user_id) if user_id else None
        title = str(body.get("title") or "")
        text = build_text(body)
        cover = await self._prepare_cover(
            str(body.get("coverUrl") or ""),
            blur=action == "blur",
        )
        novel_text = clean_novel_text(str(body.get("content") or ""))
        if not novel_text:
            raise ParseException("Pixiv 小说正文为空")

        file_name = f"pixiv_{nid}.txt"
        content = FileContent(name=file_name)

        async def load_novel() -> Path:
            output_path = self.cfg.cache_dir / file_name
            async with aiofiles.open(output_path, "w", encoding="utf-8") as file:
                await file.write(novel_text)
            return output_path

        content.set_loader(load_novel)
        character_count = _to_int(body.get("characterCount"))
        extra = {"info": f"字数: {character_count}"} if character_count > 0 else {}
        await self.send_desc_common(
            title or None,
            author,
            [],
            cover=cover,
            text=text or None,
            url=f"{PIXIV_BASE}/novel/show.php?id={nid}",
            extra=extra,
        )
        return self.parse_result([content])

    async def _handle_illust(
        self,
        pid: str,
        body: dict[str, Any],
    ) -> list[MediaContent]:
        action = self._nsfw_action(_to_int(body.get("xRestrict")))
        if action == "ignore":
            await self.send_desc_common(
                None,
                None,
                [],
                extra={"info": "R18 内容已忽略"},
            )
            return self.parse_result([])

        user_id = str(body.get("userId") or "")
        author = await self._get_author(user_id) if user_id else None
        title = str(body.get("title") or "")
        text = build_text(body)
        page_count = max(_to_int(body.get("pageCount"), 1), 1)
        original_type = _to_int(body.get("illustType"))
        illust_type = original_type
        cover_url = str((body.get("urls") or {}).get("regular") or "")
        cover = await self._prepare_cover(cover_url, blur=action == "blur")

        ugoira_meta: dict[str, Any] | None = None
        if illust_type == 2:
            ugoira_meta = await self.api.get_ugoira_meta(pid)
            if ugoira_meta is None:
                illust_type = 0

        contents: list[MediaContent] = []
        if action == "blur":
            pdf = FileContent(name=f"pixiv_{pid}.pdf")
            if illust_type == 2 and ugoira_meta is not None:

                async def load_ugoira_pdf() -> Path:
                    return await self._build_ugoira(
                        pid,
                        ugoira_meta,
                        as_pdf=True,
                    )

                pdf.set_loader(load_ugoira_pdf)
            else:
                image_urls = await self._page_urls(pid)
                if not image_urls:
                    raise ParseException("Pixiv 未找到插画图片")
                pdf = self._create_pdf_content(pid, image_urls)
            contents.append(pdf)
        elif illust_type == 2 and ugoira_meta is not None:
            gif = ImageContent()

            async def load_gif() -> Path:
                return await self._build_ugoira(
                    pid,
                    ugoira_meta,
                    as_pdf=False,
                )

            gif.set_loader(load_gif)
            contents.append(gif)
        else:
            image_urls = await self._page_urls(pid)
            if not image_urls:
                raise ParseException("Pixiv 未找到插画图片")
            contents.extend(
                self.create_image_contents(
                    image_urls,
                    headers=self.media_headers,
                )
            )

        info_parts: list[str] = []
        if page_count > 1:
            info_parts.append(f"共 {page_count} 张")
        if original_type == 2 and ugoira_meta is None:
            info_parts.append("动图元数据获取失败，已回退为静态图片")
        await self.send_desc_common(
            title or None,
            author,
            [],
            cover=cover,
            text=text or None,
            url=f"{PIXIV_BASE}/artworks/{pid}",
            timestamp=parse_timestamp(str(body.get("createDate") or "")),
            extra={"info": "\n".join(info_parts)} if info_parts else {},
        )
        return self.parse_result(contents)

    async def _handle_manga(
        self,
        pid: str,
        body: dict[str, Any],
    ) -> list[MediaContent]:
        action = self._nsfw_action(_to_int(body.get("xRestrict")))
        if action == "ignore":
            await self.send_desc_common(
                None,
                None,
                [],
                extra={"info": "R18 内容已忽略"},
            )
            return self.parse_result([])

        user_id = str(body.get("userId") or "")
        author = await self._get_author(user_id) if user_id else None
        title = str(body.get("title") or "")
        text = build_text(body)
        page_count = max(_to_int(body.get("pageCount"), 1), 1)
        cover_url = str((body.get("urls") or {}).get("regular") or "")
        series_info = await self._get_series_info(body, user_id)
        cover = await self._prepare_cover(cover_url, blur=action == "blur")

        max_page = _to_int(self.mycfg.max_page)
        if max_page > 0 and page_count > max_page:
            info = (
                f"{series_info}\n"
                if series_info
                else ""
            ) + f"漫画共 {page_count} 页，超过最大页数 {max_page}，跳过下载。"
            await self.send_desc_common(
                title or None,
                author,
                [],
                cover=cover,
                text=text or None,
                url=f"{PIXIV_BASE}/artworks/{pid}",
                timestamp=parse_timestamp(str(body.get("createDate") or "")),
                extra={"info": info},
            )
            return self.parse_result([ImageContent(cover)] if cover else [])

        image_urls = await self._page_urls(pid)
        if not image_urls:
            raise ParseException("Pixiv 未找到漫画图片")
        pdf = self._create_pdf_content(pid, image_urls)
        await self.send_desc_common(
            title or None,
            author,
            [],
            cover=cover,
            text=text or None,
            url=f"{PIXIV_BASE}/artworks/{pid}",
            timestamp=parse_timestamp(str(body.get("createDate") or "")),
            extra={"info": series_info} if series_info else {},
        )
        return self.parse_result([pdf])
