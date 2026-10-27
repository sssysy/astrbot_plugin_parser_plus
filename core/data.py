from asyncio import Task, create_task
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

PathLoader = Callable[[], Awaitable[Path]]


def repr_path_task(path_task: Path | Task[Path] | None) -> str:
    if path_task is None:
        return "path=None"
    if isinstance(path_task, Path):
        return f"path={path_task.name}"
    return f"task={path_task.get_name()}, done={path_task.done()}"


@dataclass(repr=False, slots=True)
class MediaContent:
    path_task: Path | Task[Path] | None = None
    _loader: PathLoader | None = field(default=None, init=False, repr=False)

    def set_loader(self, loader: PathLoader) -> None:
        self.path_task = None
        self._loader = loader

    async def get_path(self) -> Path:
        if isinstance(self.path_task, Path):
            return self.path_task
        if self.path_task is None:
            if self._loader is None:
                raise RuntimeError("媒体内容没有可用的下载任务")
            self.path_task = create_task(self._loader())
            self._loader = None
        self.path_task = await self.path_task
        return self.path_task

    def __repr__(self) -> str:
        prefix = self.__class__.__name__
        return f"{prefix}({repr_path_task(self.path_task)})"


@dataclass(repr=False, slots=True)
class AudioContent(MediaContent):
    """音频内容"""

    duration: float = 0.0


@dataclass(repr=False, slots=True)
class FileContent(MediaContent):
    """文件内容"""

    name: str | None = None
    """文件名"""


@dataclass(repr=False, slots=True)
class VideoContent(MediaContent):
    """视频内容"""

    cover: Path | Task[Path] | None = None
    """视频封面"""
    duration: float = 0.0
    """时长 单位: 秒"""

    async def get_cover_path(self) -> Path | None:
        if self.cover is None:
            return None
        if isinstance(self.cover, Path):
            return self.cover
        self.cover = await self.cover
        return self.cover

    @property
    def display_duration(self) -> str:
        minutes = int(self.duration) // 60
        seconds = int(self.duration) % 60
        return f"时长: {minutes}:{seconds:02d}"

    def __repr__(self) -> str:
        repr = f"VideoContent(path={repr_path_task(self.path_task)}"
        if self.cover is not None:
            repr += f", cover={repr_path_task(self.cover)}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ImageContent(MediaContent):
    """图片内容"""

    pass


@dataclass(repr=False, slots=True, init=False)
class TextContent(MediaContent):
    """文本内容，用于把纯文本作为标准消息项参与发送/合并"""

    text: str

    def __init__(self, text: str):
        MediaContent.__init__(self, Path("."))
        self.text = text

    async def get_path(self) -> Path:
        raise RuntimeError("TextContent does not have a filesystem path")

    def __repr__(self) -> str:
        return f"TextContent(text={self.text})"


@dataclass(repr=False, slots=True)
class DynamicContent(MediaContent):
    """动态内容 视频格式 后续转 gif"""

    gif_path: Path | None = None


@dataclass(repr=False, slots=True)
class LivePhotoContent(MediaContent):
    """Live Photo：静态底图 + 可播放的实况视频。"""

    image_path_task: Path | Task[Path] | None = None
    """静态底图"""
    cover: Path | Task[Path] | None = None
    """发送前的封面"""
    duration: float = 0.0
    """时长 单位: 秒"""
    loop: int = 3
    """循环次数"""

    async def get_image_path(self) -> Path | None:
        if self.image_path_task is None:
            return None
        if isinstance(self.image_path_task, Path):
            return self.image_path_task
        self.image_path_task = await self.image_path_task
        return self.image_path_task


@dataclass(repr=False, slots=True)
class GraphicsContent(MediaContent):
    """图文内容 渲染时文字在前 图片在后"""

    text: str | None = None
    """图片前的文本内容"""
    alt: str | None = None
    """图片描述 渲染时居中显示"""

    def __repr__(self) -> str:
        repr = f"GraphicsContent(path={repr_path_task(self.path_task)}"
        if self.text:
            repr += f", text={self.text}"
        if self.alt:
            repr += f", alt={self.alt}"
        return repr + ")"


@dataclass(slots=True)
class Platform:
    """平台信息"""

    name: str
    """ 平台名称 """
    display_name: str
    """ 平台显示名称 """


@dataclass(frozen=True, slots=True)
class IconAsset:
    """可渲染图标资源。"""

    kind: Literal["path", "url", "data", "svg"]
    value: str | Path
    alt: str = ""


@dataclass(frozen=True, slots=True)
class Metric:
    """通用指标项，值由调用方预先格式化。"""

    icon: IconAsset | None
    label: str | None
    value: str


@dataclass(repr=False, slots=True)
class VideoMeta:
    """视频描述卡片元信息，不包含最终视频下载任务。"""

    cover: Path | Task[Path] | None = None
    duration: float = 0.0
    quality: str | None = None
    resolution: str | None = None

    async def get_cover_path(self) -> Path | None:
        if self.cover is None:
            return None
        if isinstance(self.cover, Path):
            return self.cover
        self.cover = await self.cover
        return self.cover

    @property
    def display_duration(self) -> str | None:
        if self.duration <= 0:
            return None
        minutes = int(self.duration) // 60
        seconds = int(self.duration) % 60
        return f"{minutes:02d}:{seconds:02d}"


@dataclass(repr=False, slots=True)
class Author:
    """作者信息"""

    name: str
    """作者名称"""
    avatar: Path | Task[Path] | None = None
    """作者头像 URL 或本地路径"""
    description: str | None = None
    """作者个性签名等"""
    uid: str | None = None
    """平台作者 ID"""
    metrics: list[Metric] = field(default_factory=list)
    """作者指标，例如获赞、关注、粉丝"""

    async def get_avatar_path(self) -> Path | None:
        if self.avatar is None:
            return None
        if isinstance(self.avatar, Path):
            return self.avatar
        self.avatar = await self.avatar
        return self.avatar

    def __repr__(self) -> str:
        repr = f"Author(name={self.name}"
        if self.avatar:
            repr += f", avatar_{repr_path_task(self.avatar)}"
        if self.description:
            repr += f", description={self.description}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class Description:
    """描述卡片数据，不包含最终发送分组或下载策略。"""

    platform: Platform
    author: Author | None = None
    title: str | None = None
    text: str | None = None
    timestamp: int | None = None
    url: str | None = None
    contents: list[MediaContent] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    metrics: list[Metric] = field(default_factory=list)
    cover: Path | Task[Path] | None = None
    """媒体卡片主视觉；由解析器按图集/图片/视频规则提供。"""
    video: VideoMeta | None = None
    repost: "Description | None" = None

    @property
    def header(self) -> str:
        header = self.platform.display_name
        if self.author:
            header += f" @{self.author.name}"
        if self.title:
            header += f" | {self.title}"
        return header

    @property
    def display_url(self) -> str | None:
        return f"链接: {self.url}" if self.url else None

    @property
    def repost_display_url(self) -> str | None:
        return f"原帖: {self.repost.url}" if self.repost and self.repost.url else None

    @property
    def extra_info(self) -> str | None:
        info = self.extra.get("info")
        return str(info) if info is not None else None

    @property
    def video_contents(self) -> list[VideoContent]:
        return [cont for cont in self.contents if isinstance(cont, VideoContent)]

    @property
    def img_contents(self) -> list[ImageContent]:
        return [cont for cont in self.contents if isinstance(cont, ImageContent)]

    @property
    def audio_contents(self) -> list[AudioContent]:
        return [cont for cont in self.contents if isinstance(cont, AudioContent)]

    @property
    def file_contents(self) -> list[FileContent]:
        return [cont for cont in self.contents if isinstance(cont, FileContent)]

    @property
    def dynamic_contents(self) -> list[DynamicContent]:
        return [cont for cont in self.contents if isinstance(cont, DynamicContent)]

    @property
    def graphics_contents(self) -> list[GraphicsContent]:
        return [cont for cont in self.contents if isinstance(cont, GraphicsContent)]

    @property
    def text_contents(self) -> list[TextContent]:
        return [cont for cont in self.contents if isinstance(cont, TextContent)]

    @property
    async def cover_path(self) -> Path | None:
        if self.video is not None:
            return await self.video.get_cover_path()
        for cont in self.contents:
            if isinstance(cont, VideoContent):
                return await cont.get_cover_path()
        return None

    def formatted_datetime(self, fmt: str = "%Y-%m-%d %H:%M:%S") -> str | None:
        return (
            datetime.fromtimestamp(self.timestamp).strftime(fmt)
            if self.timestamp is not None
            else None
        )

    def __repr__(self) -> str:
        return (
            f"platform: {self.platform.display_name}, "
            f"timestamp: {self.timestamp}, "
            f"title: {self.title}, "
            f"text: {self.text}, "
            f"url: {self.url}, "
            f"author: {self.author}, "
            f"contents: {self.contents}, "
            f"extra: {self.extra}, "
            f"repost: <<<<<<<{self.repost}>>>>>>>"
        )


ContentItem = MediaContent | str
