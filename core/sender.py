import re
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    Description,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    LivePhotoContent,
    MediaContent,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    RedirectException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer
from .utils import transcode_video_audio_to_lc


class MessageSender:
    """负责发送描述卡片和最终解析内容，不参与平台解析。"""

    USER_ERROR_MAX_LENGTH = 300

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    async def send_desc(
        self,
        event: AstrMessageEvent,
        description: Description,
    ) -> bool:
        """渲染并发送描述卡片。"""
        image_path = await self.renderer.render_card(description)
        if image_path is None:
            logger.error(
                "[发送] 描述卡片渲染失败 | "
                f"platform={description.platform.name} | "
                f"title={description.title or '<empty>'}"
            )
            return False

        try:
            await event.send(event.chain_result([self._image_from_path(image_path)]))
            return True
        except Exception as exc:
            logger.error(
                "[发送] 描述卡片发送失败 | "
                f"platform={description.platform.name} | "
                f"title={description.title or '<empty>'} | "
                f"error_type={type(exc).__name__} | "
                f"error={self.safe_exception_text(exc)}"
            )
            return False

    async def parse_result(
        self,
        event: AstrMessageEvent,
        contents: list[MediaContent],
    ) -> bool:
        """按传入顺序下载并发送最终解析内容。"""
        if not contents:
            logger.warning("[发送] 解析结果为空，不执行发送")
            return False

        segs = await self._build_segments(contents)
        if not segs:
            logger.warning("[发送] 没有可发送的最终内容")
            return False

        force_merge = len(contents) >= self.cfg.forward_threshold
        segs = self._merge_segments_if_needed(event, segs, force_merge)

        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as exc:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(
                "[发送] 解析结果发送失败 | "
                f"error_type={type(exc).__name__} | "
                f"error={self.safe_exception_text(exc)} | "
                f"segments={seg_meta}"
            )
            return False

    async def _build_segments(
        self,
        contents: list[MediaContent],
    ) -> list[BaseMessageComponent]:
        """按内容列表原始顺序构建消息段。"""
        segs: list[BaseMessageComponent] = []

        for content in contents:
            if isinstance(content, TextContent):
                if content.text:
                    segs.append(Plain(content.text))
                continue
            if (
                isinstance(content, (VideoContent, LivePhotoContent))
                and self.cfg.max_duration
                and content.duration > self.cfg.max_duration
            ):
                self._append_content_failure(
                    content,
                    DurationLimitException(),
                    segs,
                )
                continue

            try:
                path = await content.get_path()
            except DownloadException as exc:
                self._append_content_failure(content, exc, segs)
                continue

            match content:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as graphics:
                    segs.append(self._image_from_path(path))
                    if graphics.text:
                        segs.append(Plain(graphics.text))
                    if graphics.alt:
                        segs.append(Plain(graphics.alt))
                case VideoContent() | DynamicContent() | LivePhotoContent():
                    path = await transcode_video_audio_to_lc(path)
                    segs.append(self._video_from_path(path))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else self._record_from_path(path)
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    @staticmethod
    def _content_label(content: object) -> str:
        match content:
            case ImageContent() | GraphicsContent():
                return "图片"
            case VideoContent() | LivePhotoContent():
                return "视频"
            case DynamicContent():
                return "动图"
            case AudioContent():
                return "音频"
            case FileContent():
                return "文件"
            case _:
                return "媒体"

    def _brief_content_error(self, exc: DownloadException) -> str:
        if isinstance(exc, SizeLimitException):
            return f"大小超过限制（{self.cfg.source_max_size} MB）"
        if isinstance(exc, DurationLimitException):
            return f"时长超过限制（{self.cfg.source_max_minute} 分钟）"
        if isinstance(exc, ZeroSizeException):
            return "资源为空"
        if isinstance(exc, RedirectException):
            return "链接跳转异常"
        return "网络或服务异常"

    @staticmethod
    def safe_exception_text(exc: Exception) -> str:
        text = str(exc).strip() or "<empty>"

        def redact_url(match: re.Match[str]) -> str:
            raw = match.group(0)
            try:
                parsed = urlsplit(raw)
                if not parsed.hostname:
                    return "<url>"
                port = f":{parsed.port}" if parsed.port else ""
                return f"{parsed.scheme}://{parsed.hostname}{port}/<redacted>"
            except ValueError:
                return "<url>"

        return re.sub(r"https?://[^\s]+", redact_url, text)

    @classmethod
    def format_user_error(cls, exc: Exception) -> str:
        """把异常转换为可安全发送给用户的简短提示。"""
        if not isinstance(exc, ParseException):
            return "解析失败，请稍后重试。"

        detail = " ".join(cls.safe_exception_text(exc).split())
        if len(detail) > cls.USER_ERROR_MAX_LENGTH:
            detail = f"{detail[: cls.USER_ERROR_MAX_LENGTH - 3]}..."
        return f"解析失败：{detail}"

    async def send_error(self, event: AstrMessageEvent, message: str) -> bool:
        """向当前会话发送纯文本错误提示。"""
        try:
            await event.send(event.chain_result([Plain(message)]))
            return True
        except Exception as exc:
            logger.error(
                "[发送] 错误提示发送失败 | "
                f"error_type={type(exc).__name__} | "
                f"error={self.safe_exception_text(exc)}"
            )
            return False

    def _append_content_failure(
        self,
        content: MediaContent,
        exc: DownloadException,
        segs: list[BaseMessageComponent],
    ) -> None:
        label = self._content_label(content)
        if self.cfg.show_download_fail_tip:
            reason = self._brief_content_error(exc)
            segs.append(Plain(f"{label}下载失败：{reason}"))

        logger.error(
            "[发送] 媒体处理失败 | "
            f"content={type(content).__name__} | "
            f"error_type={type(exc).__name__} | "
            f"error={self.safe_exception_text(exc)}"
        )

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()
        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))
        return [nodes]

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        meta: list[dict[str, str]] = []
        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)
        return meta
