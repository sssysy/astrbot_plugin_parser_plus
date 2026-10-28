from asyncio import Task, TimeoutError, create_task, gather, sleep, to_thread
from collections.abc import Callable, Collection, Coroutine, Sequence
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout
from msgspec import Struct, convert
from tqdm.asyncio import tqdm

from astrbot.api import logger

from .config import PluginConfig
from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import LimitedSizeDict, generate_file_name, merge_av, safe_unlink

P = ParamSpec("P")
T = TypeVar("T")
def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    duration: int
    """时长"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class Downloader:
    """下载器，支持youtube-dlp 和 流式下载"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.max_size = self.cfg.source_max_size
        self.default_headers: dict[str, str] = COMMON_HEADER.copy()
        # 视频信息缓存
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()
        self._active_downloads: dict[Path, Task[Path]] = {}
        # aiohttp 的 ClientSession 必须在事件循环内创建。
        self._client: ClientSession | None = None

    @property
    def client(self) -> ClientSession:
        if self._client is None or self._client.closed:
            self._client = ClientSession(
                timeout=ClientTimeout(total=self.cfg.download_timeout)
            )
        return self._client

    async def close(self):
        """关闭网络客户端"""
        if self._client is not None and not self._client.closed:
            await self._client.close()
        self._client = None

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
    ) -> Path:
        """流式下载"""
        headers = headers or self.default_headers
        candidates = [item for item in (url, *(fallback_urls or ())) if item]
        if file_name is None:
            file_name = generate_file_name(url)
        file_path = self.cfg.cache_dir / file_name
        if file_path.exists():
            return file_path
        if active := self._active_downloads.get(file_path):
            return await active
        task = create_task(
            self._download_candidates(
                candidates,
                file_path=file_path,
                headers=headers,
                proxy=proxy,
                retry_http_statuses=retry_http_statuses,
            )
        )
        self._active_downloads[file_path] = task
        try:
            return await task
        finally:
            self._active_downloads.pop(file_path, None)

    async def _download_candidates(
        self,
        candidates: list[str],
        *,
        file_path: Path,
        headers: dict[str, str],
        proxy: str | None,
        retry_http_statuses: Collection[int],
    ) -> Path:
        retries = self.cfg.download_retry_times
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            current_url = candidates[attempt % len(candidates)]
            try:
                await self._stream_once(
                    current_url,
                    file_path=file_path,
                    headers=headers,
                    proxy=proxy,
                    retry_http_statuses=retry_http_statuses,
                )
                return file_path
            except (ZeroSizeException, SizeLimitException):
                await safe_unlink(file_path)
                raise
            except (ClientError, TimeoutError) as exc:
                last_error = exc
                await safe_unlink(file_path)
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                logger.exception(
                    f"下载失败 | url: {current_url}, file_path: {file_path}"
                )
                raise DownloadException("媒体下载失败") from exc
        logger.warning(
            f"下载失败 | url: {candidates[0]}, error={last_error}"
        )
        raise DownloadException("媒体下载失败")

    async def _stream_once(
        self,
        url: str,
        *,
        file_path: Path,
        headers: dict[str, str],
        proxy: str | None,
        retry_http_statuses: Collection[int],
    ) -> None:
        async with self.client.get(
            url, headers=headers, allow_redirects=True, proxy=proxy
        ) as response:
            if response.status in retry_http_statuses:
                raise ClientError(f"HTTP {response.status} {response.reason}")
            if response.status >= 400:
                raise ClientError(f"HTTP {response.status} {response.reason}")
            content_length = response.content_length
            max_bytes = self.max_size * 1024 * 1024

            if content_length == 0:
                logger.warning(f"媒体 url: {url}, 大小为 0, 取消下载")
                raise ZeroSizeException
            if content_length and content_length > max_bytes:
                logger.warning(
                    f"媒体 url: {url} 大小 {content_length / 1024 / 1024:.2f} MB 超过 {self.max_size} MB, 取消下载"
                )
                raise SizeLimitException

            downloaded = 0
            with self.get_progress_bar(file_path.name, content_length) as bar:
                async with aiofiles.open(file_path, "wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise SizeLimitException
                        await file.write(chunk)
                        bar.update(len(chunk))

            if downloaded == 0:
                logger.warning(f"媒体 url: {url}, 实际大小为 0, 取消下载")
                raise ZeroSizeException
            if content_length and downloaded < content_length:
                raise ClientError(
                    f"HTTP payload incomplete {downloaded}/{content_length}"
                )

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        """获取进度条 bar

        Args:
            desc (str): 描述
            total (int | None): 总大小. Defaults to None.

        Returns:
            tqdm: 进度条
        """
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(
            url,
            file_name=video_name,
            headers=headers,
            proxy=proxy,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url,
            file_name=audio_name,
            headers=headers,
            proxy=proxy,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url,
            file_name=file_name,
            headers=headers,
            proxy=proxy,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(
            url,
            file_name=img_name,
            headers=headers,
            proxy=proxy,
            fallback_urls=fallback_urls,
            retry_http_statuses=retry_http_statuses,
        )

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> list[Path]:
        paths_or_errs = await gather(
            *[self.download_img(url, headers=headers, proxy=proxy) for url in urls],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> Path:
        """
        download video and audio file by url with stream and merge
        """
        v_path, a_path = await gather(
            self.download_video(v_url, headers=headers, proxy=proxy),
            self.download_audio(a_url, headers=headers, proxy=proxy),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path

    async def ytdlp_extract_info(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format
        if node:
            opts["js_runtimes"] = {"node": {}}
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")
        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def ytdlp_extract_raw(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> dict[str, Any]:
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format
        if node:
            opts["js_runtimes"] = {"node": {}}

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not isinstance(raw, dict):
                raise ParseException("yt-dlp 返回数据异常")
            return raw  # type: ignore

    @auto_task
    async def ytdlp_download_video(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
        download_ranges: str | None = None,
        concurrent_fragments: int | None = None,
    ) -> Path:
        info = await self.ytdlp_extract_info(
            url,
            cookiefile=cookiefile,
            headers=headers,
            proxy=proxy,
            node=node,
        )
        if info.duration > self.cfg.max_duration:
            raise DurationLimitException

        video_path = self.cfg.cache_dir / generate_file_name(url, ".mp4")
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(video_path),
            "merge_output_format": "mp4",
            # "format": f"bv[filesize<={info.duration // 10 + 10}M]+ba/b[filesize<={info.duration // 8 + 10}M]",
            # "format": "bv*[height<=720]+ba/b[height<=720]",
            "format": format or "best",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}
        if download_ranges:
            opts["download_ranges"] = yt_dlp.utils.download_range_func(
                None,
                download_ranges,
            )
        if concurrent_fragments and concurrent_fragments > 1:
            opts["concurrent_fragment_downloads"] = concurrent_fragments

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            await to_thread(ydl.download, [url])
        return video_path

    @auto_task
    async def ytdlp_download_video_relaxed(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> Path:
        file_stem = generate_file_name(url)
        video_path = self.cfg.cache_dir / f"{file_stem}.mp4"
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_stem) + ".%(ext)s",
            "merge_output_format": "mp4",
            "format": format or None,
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
            "quiet": True,
            "no_warnings": True,
        }
        if not opts["format"]:
            opts.pop("format")
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            await to_thread(ydl.download, [url])
        if video_path.exists():
            return video_path

        candidates = sorted(self.cfg.cache_dir.glob(f"{file_stem}*.mp4"))
        if candidates:
            return candidates[0]
        raise DownloadException("yt-dlp 视频下载失败")

    @auto_task
    async def ytdlp_download_audio(
        self,
        url: str,
        *,
        cookiefile: Path | None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
    ) -> Path:
        file_name = generate_file_name(url)
        audio_path = self.cfg.cache_dir / f"{file_name}.flac"
        if audio_path.exists():
            return audio_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_name) + ".%(ext)s",
            "format": format or "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                    "preferredquality": "0",
                }
            ],
            "cookiefile": None,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            await to_thread(ydl.download, [url])
        return audio_path
