"""Parser 基类定义"""

from abc import ABC
from asyncio import Task, TimeoutError, sleep
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Iterable,
    Sequence,
)
from contextvars import ContextVar
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..config import ParserItem, PluginConfig
from ..constants import ANDROID_HEADER, COMMON_HEADER, IOS_HEADER
from ..data import (
    AudioContent,
    Author,
    ContentItem,
    Description,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    LivePhotoContent,
    MediaContent,
    Metric,
    Platform,
    TextContent,
    VideoContent,
    VideoMeta,
)
from ..download import Downloader
from ..exception import ParseException, RedirectException
from ..utils import merge_to_live_mp4

T = TypeVar("T", bound="BaseParser")
HandlerFunc = Callable[[T, Match[str]], Coroutine[Any, Any, list[MediaContent]]]
DescriptionSink = Callable[[Description], Awaitable[None]]
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"
_DESCRIPTION_SINK: ContextVar[DescriptionSink | None] = ContextVar(
    "parser_description_sink",
    default=None,
)


# 注册处理器装饰器
def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""

    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])

        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))

        return func

    return decorator


class BaseParser:
    """所有平台 Parser 的抽象基类

    子类必须实现：
    - platform: 平台信息（包含名称和显示名称)
    """

    _registry: ClassVar[list[type["BaseParser"]]] = []
    """ 存储所有已注册的 Parser 类 """

    platform: ClassVar[Platform]
    """ 平台信息（包含名称和显示名称） """

    if TYPE_CHECKING:
        _key_patterns: ClassVar[KeyPatterns]
        _handlers: ClassVar[dict[str, HandlerFunc]]

    def __init__(self, config: PluginConfig, downloader: Downloader):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.cfg = config
        self.data_dir = self.cfg.data_dir
        self.downloader = downloader
        self._session: ClientSession | None = None

    @property
    def proxy(self) -> str | None:
        try:
            parser_cfg: ParserItem = getattr(
                self.cfg.parser, self.__class__.platform.name
            )
            return self.cfg.proxy if parser_cfg.use_proxy else None
        except AttributeError:
            return None

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:  # 跳过抽象类
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        # 获取所有被 handle 装饰的方法
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                handler = cast(HandlerFunc, attr)
                for keyword, pattern in key_patterns:
                    cls._handlers[keyword] = handler
                    cls._key_patterns.append((keyword, pattern))

        # 按关键字长度降序排序
        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        """获取所有已注册的 Parser 类"""
        return cls._registry

    @property
    def session(self) -> ClientSession:
        """获取当前实例的 session，惰性创建"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                proxy=self.proxy, timeout=ClientTimeout(total=self.cfg.common_timeout)
            )
        return self._session

    async def close_session(self) -> None:
        """关闭当前实例的 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def parse(
        self,
        keyword: str,
        searched: Match[str],
        desc_sink: DescriptionSink | None = None,
    ) -> list[MediaContent]:
        """执行解析，并在需要时发送描述卡片。"""
        token = _DESCRIPTION_SINK.set(desc_sink) if desc_sink is not None else None
        try:
            return await self._handlers[keyword](self, searched)
        finally:
            if token is not None:
                _DESCRIPTION_SINK.reset(token)

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[MediaContent]:
        """先重定向再解析"""
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        """搜索 URL 匹配模式"""
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

    def parse_result(self, contents: Iterable[ContentItem]) -> list[MediaContent]:
        """透传最终需要发送的有序内容列表。"""
        return [
            TextContent(item) if isinstance(item, str) else item for item in contents
        ]

    async def _emit_description(self, description: Description) -> None:
        sink = _DESCRIPTION_SINK.get()
        if sink is not None:
            await sink(description)

    @staticmethod
    def _normalize_desc_contents(
        contents: Iterable[ContentItem],
    ) -> tuple[list[MediaContent], str | None]:
        """把文字和图片组合成按顺序渲染的图文块。"""
        media: list[MediaContent] = []
        pending_text: list[str] = []

        for item in contents:
            if isinstance(item, str):
                pending_text.append(item)
                continue
            if isinstance(item, TextContent):
                pending_text.append(item.text)
                continue

            text = "\n".join(pending_text).strip() or None
            pending_text.clear()

            if isinstance(item, ImageContent):
                graphics = GraphicsContent(item.path_task, text)
                graphics._loader = item._loader
                media.append(graphics)
            elif isinstance(item, GraphicsContent) and text:
                graphics = GraphicsContent(
                    item.path_task,
                    text=f"{text}\n{item.text}" if item.text else text,
                    alt=item.alt,
                )
                graphics._loader = item._loader
                media.append(graphics)
            else:
                if text:
                    media.append(TextContent(text))
                media.append(item)

        trailing_text = "\n".join(pending_text).strip() or None
        return media, trailing_text

    async def send_desc_common(
        self,
        title: str | None,
        author: Author | None,
        contents: Iterable[ContentItem],
        *,
        text: str | None = None,
        timestamp: int | None = None,
        url: str | None = None,
        extra: dict[str, Any] | None = None,
        metrics: Iterable[Metric] | None = None,
        repost: Description | None = None,
    ) -> None:
        """发送通用描述卡片。"""
        if _DESCRIPTION_SINK.get() is None:
            return
        card_contents, trailing_text = self._normalize_desc_contents(contents)
        description = Description(
            platform=self.platform,
            title=title,
            author=author,
            text="\n".join(part for part in (text, trailing_text) if part) or None,
            timestamp=timestamp,
            url=url,
            contents=card_contents,
            extra=extra or {},
            metrics=list(metrics or ()),
            repost=repost,
        )
        await self._emit_description(description)

    async def send_desc_video(
        self,
        title: str | None,
        author: Author | None,
        *,
        cover: str | Path | Task[Path] | None = None,
        duration: float = 0.0,
        quality: str | None = None,
        resolution: str | None = None,
        text: str | None = None,
        timestamp: int | None = None,
        url: str | None = None,
        extra: dict[str, Any] | None = None,
        metrics: Iterable[Metric] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """发送视频描述卡片。"""
        if _DESCRIPTION_SINK.get() is None:
            return
        request_headers = headers or self.headers
        cover_task = cover
        if isinstance(cover, str):
            cover_task = self.downloader.download_img(
                cover, headers=request_headers, proxy=self.proxy
            )
        description = Description(
            platform=self.platform,
            title=title,
            author=author,
            text=text,
            timestamp=timestamp,
            url=url,
            extra=extra or {},
            metrics=list(metrics or ()),
            video=VideoMeta(
                cover=cover_task,
                duration=duration,
                quality=quality,
                resolution=resolution,
            ),
        )
        await self._emit_description(description)

    async def get_redirect_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 单次重定向"""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        headers = headers or COMMON_HEADER.copy()
        retries = self.cfg.download_retry_times
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=False, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(f"redirect check {resp.status} {resp.reason}")
                    return resp.headers.get("Location", url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    async def get_final_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 允许多次重定向"""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        headers = headers or COMMON_HEADER.copy()
        retries = 2
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=True, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(
                            f"final url check {resp.status} {resp.reason}"
                        )
                    return str(resp.url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    def create_author(
        self,
        name: str,
        avatar_url: str | None = None,
        description: str | None = None,
        *,
        uid: str | None = None,
        metrics: Iterable[Metric] | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建作者对象"""

        avatar_task = None
        if avatar_url:
            avatar_task = self.downloader.download_img(
                avatar_url, headers=headers or self.headers, proxy=self.proxy
            )
        return Author(
            name=name,
            avatar=avatar_task,
            description=description,
            uid=uid,
            metrics=list(metrics or ()),
        )

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
    ):
        """创建视频内容"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=headers or self.headers, proxy=self.proxy
            )
        if isinstance(url_or_task, str):
            url = url_or_task
            request_headers = headers or self.headers
            proxy = self.proxy
            content = VideoContent(None, cover_task, duration)

            async def load_video() -> Path:
                return await self.downloader.download_video(
                    url,
                    headers=request_headers,
                    proxy=proxy,
                    fallback_urls=fallback_urls,
                    retry_http_statuses=retry_http_statuses,
                )

            content.set_loader(load_video)
            return content

        return VideoContent(url_or_task, cover_task, duration)

    def create_live_photo_content(
        self,
        video_url: str,
        image_url: str,
        *,
        cover_url: str | None = None,
        duration: float = 0.0,
        bgm_url: str | None = None,
        headers: dict[str, str] | None = None,
        fallback_urls: Sequence[str] | None = None,
        retry_http_statuses: Collection[int] = (),
        loop: int = 3,
    ) -> LivePhotoContent:
        """创建 Live Photo 内容，实际合成在最终发送阶段执行。"""
        request_headers = headers or self.headers
        proxy = self.proxy
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=request_headers, proxy=proxy
            )
        content = LivePhotoContent(
            path_task=None,
            image_path_task=None,
            cover=cover_task,
            duration=duration,
            loop=loop,
        )

        async def load_live_photo() -> Path:
            video_path = await self.downloader.download_video(
                video_url,
                headers=request_headers,
                proxy=proxy,
                fallback_urls=fallback_urls,
                retry_http_statuses=retry_http_statuses,
            )
            try:
                image_path = await self.downloader.download_img(
                    image_url, headers=request_headers, proxy=proxy
                )
                bgm_path = None
                if bgm_url:
                    bgm_path = await self.downloader.download_audio(
                        bgm_url, headers=request_headers, proxy=proxy
                    )
                output = video_path.with_name(f"{video_path.stem}_live.mp4")
                if output.exists():
                    return output
                return await merge_to_live_mp4(
                    image_path=image_path,
                    video_path=video_path,
                    output_path=output,
                    bgm_path=bgm_path,
                )
            except Exception as exc:
                logger.warning(
                    "[抖音] Live Photo 合成失败，回退实况原视频 | "
                    f"error_type={type(exc).__name__}"
                )
                return video_path

        content.set_loader(load_live_photo)
        return content

    def create_video_content_by_task(
        self,
        path_task: Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ):
        """创建视频内容，允许调用方自行决定下载任务实现"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=headers or self.headers, proxy=self.proxy
            )
        return VideoContent(path_task, cover_task, duration)

    def create_video_content_by_loader(
        self,
        loader: Callable[[], Awaitable[Path]],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> VideoContent:
        """创建延迟下载的视频内容。"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=headers or self.headers, proxy=self.proxy
            )
        content = VideoContent(None, cover_task, duration)
        content.set_loader(loader)
        return content

    def create_image_contents(
        self,
        image_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建图片内容列表"""
        contents: list[ImageContent] = []
        request_headers = headers or self.headers
        proxy = self.proxy
        for url in image_urls:
            content = ImageContent()

            async def load_image(current_url: str = url) -> Path:
                return await self.downloader.download_img(
                    current_url, headers=request_headers, proxy=proxy
                )

            content.set_loader(load_image)
            contents.append(content)
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建动态图片内容列表"""
        contents: list[DynamicContent] = []
        request_headers = headers or self.headers
        proxy = self.proxy
        for url in dynamic_urls:
            content = DynamicContent()

            async def load_dynamic(current_url: str = url) -> Path:
                return await self.downloader.download_video(
                    current_url, headers=request_headers, proxy=proxy
                )

            content.set_loader(load_dynamic)
            contents.append(content)
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ):
        """创建音频内容"""
        if isinstance(url_or_task, str):
            url = url_or_task
            request_headers = headers or self.headers
            proxy = self.proxy
            content = AudioContent(None, duration)

            async def load_audio() -> Path:
                return await self.downloader.download_audio(
                    url, headers=request_headers, proxy=proxy
                )

            content.set_loader(load_audio)
            return content

        return AudioContent(url_or_task, duration)

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建图文内容 图片不能为空 文字可空 渲染时文字在前 图片在后"""
        content = GraphicsContent(None, text, alt)
        request_headers = headers or self.headers
        proxy = self.proxy

        async def load_image() -> Path:
            return await self.downloader.download_img(
                image_url, headers=request_headers, proxy=proxy
            )

        content.set_loader(load_image)
        return content

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建文件内容"""
        if isinstance(url_or_task, str):
            url = url_or_task
            request_headers = headers or self.headers
            proxy = self.proxy
            content = FileContent(None, name)

            async def load_file() -> Path:
                return await self.downloader.download_file(
                    url,
                    headers=request_headers,
                    file_name=name,
                    proxy=proxy,
                )

            content.set_loader(load_file)
            return content

        return FileContent(url_or_task, name)
