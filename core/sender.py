import re
from itertools import chain
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
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DurationLimitException,
    RedirectException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer
from .utils import transcode_video_audio_to_lc


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

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

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 是否渲染卡片只由调用方显式指定，不再因单条重媒体自动开启。
        render_card = (
            render_card_override if render_card_override is not None else False
        )

        # 实际媒体段数量
        media_count = len(light) + len(heavy)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = (media_count + (1 if render_card else 0)) >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        # 合并转发时始终内联预览卡片（除非显式禁用）
        if force_merge and render_card_override is not False:
            render_card = True

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ):
        """
        发送预览卡片（独立消息）

        场景：
        - 调用方显式要求渲染卡片
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        if not plan["preview_card"]:
            return

        if image_path := await self.renderer.render_card(result):
            await event.send(event.chain_result([self._image_from_path(image_path)]))

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(self._image_from_path(image_path))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except DownloadException as exc:
                self._append_content_failure(result, cont, exc, segs)
                continue

            match cont:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as g:
                    segs.append(self._image_from_path(path))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except DownloadException as exc:
                self._append_content_failure(result, cont, exc, segs)
                continue

            match cont:
                case VideoContent() | DynamicContent():
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
            case VideoContent():
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
    def _safe_exception_text(exc: Exception) -> str:
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

    def _append_content_failure(
        self,
        result: ParseResult,
        content: object,
        exc: DownloadException,
        segs: list[BaseMessageComponent],
    ) -> None:
        label = self._content_label(content)
        if self.cfg.show_download_fail_tip:
            reason = self._brief_content_error(exc)
            segs.append(Plain(f"{label}下载失败：{reason}"))

        logger.error(
            "[发送] 媒体处理失败 | "
            f"platform={result.platform.name} | "
            f"content={type(content).__name__} | "
            f"error_type={type(exc).__name__} | "
            f"error={self._safe_exception_text(exc)}"
        )

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        await self._send_preview_card(event, result, plan)

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if not segs:
            return False

        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return False

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
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

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        sent = False
        for group in groups:
            sent = await self._send_group(event, result, group) or sent

        if not sent:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return

            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return
