from __future__ import annotations

import asyncio
from pathlib import Path
from re import Match
from typing import Any, ClassVar

from astrbot.api import logger

try:
    from jmcomic import (
        JmImageTool,
        JmOption,
        JmPhotoDetail,
        JmcomicException,
        JmcomicText,
        MissingAlbumPhotoException,
    )
except ImportError:
    JmImageTool = None
    JmOption = None
    JmPhotoDetail = None
    JmcomicException = Exception
    JmcomicText = None
    MissingAlbumPhotoException = Exception

from PIL import Image, ImageFilter

from ...data import FileContent, ImageContent, MediaContent, Platform
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle


class JMComicParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="jmcomic", display_name="jmcomic")
    IMAGE_CONCURRENCY: ClassVar[int] = 16

    @property
    def proxy(self) -> str | None:
        raw_proxy = super().proxy
        if raw_proxy:
            cleaned = raw_proxy.strip()
            return cleaned or None
        return None

    def _get_option(self) -> Any:
        if JmOption is None:
            raise ParseException("未安装 jmcomic 依赖库，请先安装后重载插件")
        option = JmOption.default()
        postman = option.client.get("postman")
        if postman is None:
            postman = {}
            option.client["postman"] = postman
        meta_data = postman.get("meta_data")
        if meta_data is None:
            meta_data = {}
            postman["meta_data"] = meta_data
        meta_data["proxies"] = self.proxy
        return option

    @staticmethod
    def _blur(image_path: Path, radius: int = 5) -> Path:
        output_path = image_path.parent / (
            f"{image_path.stem}_blur{image_path.suffix}"
        )
        with Image.open(image_path) as image:
            blurred = image.filter(ImageFilter.GaussianBlur(radius=radius))
            blurred.save(output_path)
        return output_path

    @staticmethod
    def _decode_img(
        img_url: str,
        scramble_id: str,
        image_path: Path,
    ) -> Path:
        save_path = image_path.parent / (
            f"{image_path.stem}_decode{image_path.suffix}"
        )
        if save_path.exists() and save_path.stat().st_size > 0:
            return save_path
        split_num = JmImageTool.get_num_by_url(scramble_id, img_url)
        with Image.open(image_path) as image:
            JmImageTool.decode_and_save(split_num, image, str(save_path))
        return save_path

    @staticmethod
    def _imgs_to_pdf(img_paths: list[Path], save_path: Path) -> Path:
        if not img_paths:
            raise ParseException("图片列表为空，可能需要检查 jm 号是否正确")
        images = []
        try:
            for img_path in img_paths:
                with Image.open(img_path) as image:
                    images.append(image.convert("RGB"))
            images[0].save(
                save_path,
                "PDF",
                save_all=True,
                append_images=images[1:],
            )
        finally:
            for image in images:
                image.close()
        return save_path

    async def _download_all_photo(
        self,
        photo_detail: JmPhotoDetail,
    ) -> list[Path]:
        semaphore = asyncio.Semaphore(self.IMAGE_CONCURRENCY)

        async def download_and_decode(photo) -> Path | None:
            async with semaphore:
                try:
                    raw_path = await self.downloader.download_img(
                        photo.img_url, proxy=self.proxy
                    )
                    return await asyncio.to_thread(
                        self._decode_img,
                        photo.img_url,
                        photo.scramble_id,
                        raw_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JMComic] 图片下载或解密失败 | "
                        f"error_type={type(exc).__name__}"
                    )
                    return None

        results = await asyncio.gather(
            *(download_and_decode(photo) for photo in photo_detail)
        )
        return [path for path in results if path is not None]

    async def _build_pdf(
        self,
        image_paths: asyncio.Task[list[Path]],
        comic_id: str,
    ) -> Path:
        paths = await image_paths
        if not paths:
            raise ParseException("漫画图片下载失败，未能获取到任何有效图片")
        pdf_path = self.cfg.cache_dir / f"{comic_id}.pdf"
        await asyncio.to_thread(self._imgs_to_pdf, paths, pdf_path)
        return pdf_path

    @handle("18comic.vip/photo", r"18comic\.vip/photo/(?P<comic_id>\d{5,})")
    @handle("18comic.vip/album", r"18comic\.vip/album/(?P<comic_id>\d{5,})")
    @handle("jm", r"jm(?P<comic_id>\d+)")
    @handle("JM", r"JM(?P<comic_id>\d+)")
    @handle("Jm", r"Jm(?P<comic_id>\d+)")
    async def _parse(self, searched: Match[str]) -> list[MediaContent]:
        comic_id = searched.group("comic_id").lstrip("0")
        if not comic_id:
            await self.send_desc_common(
                None,
                None,
                [],
                extra={"info": "JM 号不能为空"},
            )
            return []

        option = self._get_option()
        client_ctx = option.new_jm_async_client(proxies=self.proxy)
        async with client_ctx as jm_client:
            try:
                album_detail = await jm_client.get_album_detail(comic_id)
            except MissingAlbumPhotoException:
                await self.send_desc_common(
                    None,
                    None,
                    [],
                    extra={"info": "当前 JM 号不存在"},
                )
                return []
            except JmcomicException as exc:
                raise ParseException("获取 jm 详情遇到问题") from exc

            album_title = album_detail.title
            album_name = album_detail.authoroname
            album_actors = album_detail.actors
            album_description = album_detail.description
            album_tags = album_detail.tags
            album_views = album_detail.views
            album_likes = album_detail.likes
            album_cover_url = JmcomicText.get_album_cover_url(comic_id)
            album_url = JmcomicText.format_album_url(comic_id)
            episode_count = len(album_detail.episode_list)

            photo_detail = await jm_client.get_photo_detail(comic_id)
            album_episode = photo_detail.sort

            final_contents: list[MediaContent] = []
            cover_task = None
            if self.mycfg.nsfw != "ignore":
                cover_path = await self.downloader.download_img(
                    album_cover_url, proxy=self.proxy
                )
                if self.mycfg.nsfw == "blur":
                    cover_path = await asyncio.to_thread(self._blur, cover_path)
                cover_task = asyncio.create_task(asyncio.sleep(0, cover_path))

            send_text = (
                f"标题: {album_title}\n"
                f"TAGS: {', '.join(f'#{tag}' for tag in album_tags)}\n"
                f"浏览: {album_views}\n"
                f"点赞: {album_likes}\n"
                f"描述: {album_description}\n"
            )
            extra_info = (
                f"共 {episode_count} 话，当前下载第 {album_episode} 话\n"
                f"漫画原链: {album_url}"
            )
            if episode_count > 1:
                extra_info += "\n若需解析其他章节请发送对应章节jm号。"

            send_mode = self.mycfg.image_send_mode
            if send_mode != "ignore":
                page_count = len(photo_detail)
                max_page = self.mycfg.max_page or 0
                if max_page and page_count > max_page:
                    extra_info = (
                        f"\n当前漫画页数 {page_count}，超过最大页数 {max_page}，"
                        "跳过解析。"
                    )
                else:
                    image_paths = asyncio.create_task(
                        self._download_all_photo(photo_detail)
                    )
                    if send_mode == "pdf":
                        pdf_task = asyncio.create_task(
                            self._build_pdf(image_paths, comic_id)
                        )
                        final_contents.append(FileContent(pdf_task))
                    elif send_mode == "merge":
                        downloaded = await image_paths
                        if not downloaded:
                            raise ParseException(
                                "漫画图片下载失败，未能获取到任何有效图片"
                            )
                        final_contents.extend(
                            ImageContent(path) for path in downloaded
                        )

            await self.send_desc_video(
                album_name,
                self.create_author(", ".join(album_actors)),
                cover=cover_task,
                quality="漫画",
                text=send_text,
                extra={"info": extra_info},
                url=album_url,
            )
            return self.parse_result(final_contents)
