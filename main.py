# main.py

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Json, Plain, Reply
from astrbot.core.message.components import Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.exception import ParseException
from .core.parser_settings import ParserSettingsService
from .core.parsers import BaseParser, ParserRegistry
from .core.playwright_install import install_playwright_chromium
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url


PLUGIN_NAME = "astrbot_plugin_parser_plus"


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 渲染器：本地 Playwright 或 AstrBot 远程 T2I
        self.renderer = Renderer(
            self.cfg,
            self.downloader,
            remote_renderer=self.html_render,
        )
        # Playwright Chromium 安装任务锁
        self._playwright_install_lock = asyncio.Lock()
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        # 动态扫描得到的解析器注册中心
        self.parser_registry = ParserRegistry(self.cfg, self.downloader)
        self.parser_settings = ParserSettingsService(
            self.parser_registry,
            self.cfg.parser_config_dir,
        )
        self.context.register_web_api(
            f"{PLUGIN_NAME}/parser-settings/parsers",
            self.page_list_parsers,
            ["GET"],
            "列出解析器配置",
        )
        self.context.register_web_api(
            f"{PLUGIN_NAME}/parser-settings/parser/<name>",
            self.page_save_parser,
            ["POST"],
            "保存解析器配置",
        )
        self.context.register_web_api(
            f"{PLUGIN_NAME}/parser-settings/skins",
            self.page_list_skins,
            ["GET"],
            "列出皮肤包",
        )
        self.context.register_web_api(
            f"{PLUGIN_NAME}/parser-settings/skin",
            self.page_save_skin,
            ["POST"],
            "切换皮肤包",
        )

    async def initialize(self):
        """加载、重载插件时触发"""
        # 校验渲染模板；Playwright 浏览器保持懒加载
        await self.renderer.initialize()
        # 扫描并注册解析器
        await self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关闭渲染浏览器
        await self.renderer.close()
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        await self.parser_registry.close()
        # 关缓存清理器
        await self.cleaner.stop()

    async def _register_parser(self):
        """扫描解析器目录并替换注册快照。"""
        snapshot = await self.parser_registry.load()
        self.parser_map = snapshot.parser_map
        self.key_pattern_list = list(snapshot.key_pattern_list)

        logger.debug(
            f"[parser] 关键词-正则对已生成: "
            f"{[kw for kw, _ in self.key_pattern_list]}"
        )

    @staticmethod
    def _is_page_admin() -> bool:
        return bool(request.username)

    async def page_list_parsers(self):
        if not self._is_page_admin():
            return error_response("unauthorized", status_code=403)
        return json_response({"parsers": self.parser_settings.list_parsers()})

    async def page_save_parser(self, name: str):
        if not self._is_page_admin():
            return error_response("unauthorized", status_code=403)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid payload", status_code=400)
        enabled = payload.get("enabled")
        values = payload.get("values", {})
        if not isinstance(enabled, bool) or not isinstance(values, dict):
            return error_response("invalid payload", status_code=400)
        try:
            await self.parser_settings.save_parser(
                name,
                enabled=enabled,
                updates=values,
                preserve_masked=True,
            )
        except KeyError:
            return error_response("parser not found", status_code=404)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"saved": True, "reload_required": False})

    async def page_list_skins(self):
        if not self._is_page_admin():
            return error_response("unauthorized", status_code=403)
        return json_response(self.parser_settings.list_skins())

    async def page_save_skin(self):
        if not self._is_page_admin():
            return error_response("unauthorized", status_code=403)
        payload = await request.json(default={})
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str):
            return error_response("invalid skin", status_code=400)
        try:
            self.parser_settings.save_skin(name)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"saved": True, "reload_required": True})

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        text = event.message_str

        # 指定机制：专门@其他bot的消息不解析
        self_id = event.get_self_id()
        mentioned_ids: set[str] = set()
        for seg in chain:
            if isinstance(seg, At):
                mentioned_ids.add(str(seg.qq))
            elif isinstance(seg, Plain):
                mentioned_ids.update(re.findall(r"<@!?([^>\s]+)>", seg.text))
        if (
            self.cfg.require_at_in_group
            and not isinstance(event, AiocqhttpMessageEvent)
            and event.get_message_type() == MessageType.GROUP_MESSAGE
            and self_id not in mentioned_ids
        ):
            return
        if (
            mentioned_ids
            and self_id not in mentioned_ids
            and "qq_official" not in mentioned_ids
        ):
            return

        # 卡片解析：扫描整条消息链，兼容 @ + JSON 卡片等组合消息。
        for seg in chain:
            if not isinstance(seg, Json):
                continue
            parsed_url = extract_json_url(seg.data)
            logger.debug(f"解析Json组件: {parsed_url}")
            if parsed_url:
                text = parsed_url
                break

        # 引用解析
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if self.cfg.enable_reply_parse and reply_seg and reply_seg.chain:
            reply_texts = []
            for seg in reply_seg.chain:
                if isinstance(seg, Plain):
                    reply_texts.append(seg.text)
                elif isinstance(seg, Json):
                    reply_texts.append(extract_json_url(seg.data))
            if reply_texts:
                reply_text = "".join(t for t in reply_texts if t)
                if reply_text:
                    text = reply_text

        if not text:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析。parser 在取得描述信息后立即发送卡片，最终媒体保持懒加载。
        async def send_desc(description):
            await self.sender.send_desc(event, description)

        try:
            parse_res = await self.parser_map[keyword].parse(
                keyword,
                searched,
                desc_sink=send_desc,
            )

            # 卡片发送完成后，才开始下载并发送最终解析内容。
            await self.sender.parse_result(event, parse_res)
        except Exception as exc:
            await self._notify_parse_failure(event, exc, source="自动解析")

    async def _notify_parse_failure(
        self,
        event: AstrMessageEvent,
        exc: Exception,
        *,
        source: str,
    ) -> None:
        """记录解析异常，并按配置向用户发送安全提示。"""
        error_text = self.sender.safe_exception_text(exc)
        if isinstance(exc, ParseException):
            logger.warning(
                f"[解析] {source}失败 | "
                f"error_type={type(exc).__name__} | "
                f"error={error_text}"
            )
        else:
            logger.error(
                f"[解析] {source}异常 | "
                f"error_type={type(exc).__name__} | "
                f"error={error_text}"
            )

        if not self.cfg.show_download_fail_tip:
            return
        await self.sender.send_error(event, self.sender.format_user_error(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("安装playwright")
    async def install_playwright(self, event: AstrMessageEvent):
        """安装本地卡片渲染所需的 Playwright Chromium。"""
        if self._playwright_install_lock.locked():
            yield event.plain_result("Playwright Chromium 正在安装，请勿重复执行。")
            return

        async with self._playwright_install_lock:
            yield event.plain_result(
                "正在安装 Playwright Chromium，下载时间可能较长，请稍候..."
            )
            result = await install_playwright_chromium()

        if result.ok:
            logger.info("[渲染] Playwright Chromium 安装完成")
            yield event.plain_result(
                "Playwright Chromium 安装完成，可直接重试卡片渲染。"
            )
            return

        logger.error(
            "[渲染] Playwright Chromium 安装失败 | "
            f"timed_out={result.timed_out} | "
            f"returncode={result.returncode} | "
            f"error_type={result.error_type or 'ProcessError'} | "
            f"output={result.output[-1000:]!r}"
        )
        if result.timed_out:
            yield event.plain_result(
                "Playwright Chromium 安装超时，请检查网络后重试。"
            )
        elif result.error_type:
            yield event.plain_result(
                f"无法启动 Playwright 安装程序（{result.error_type}），"
                "请检查插件依赖。"
            )
        else:
            yield event.plain_result(
                f"Playwright Chromium 安装失败（退出码 {result.returncode}），"
                "请查看 AstrBot 日志。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("blogin")
    async def bilibili_login(self, event: AstrMessageEvent):
        """管理员扫码登录哔哩哔哩，并自动保存可续期凭证。"""
        parser = self.parser_map.get("bilibili.com/bangumi/play")
        if parser is None:
            yield event.plain_result("哔哩哔哩解析器未启用。")
            return
        try:
            qrcode = await parser.login_with_qrcode()
        except Exception:
            logger.exception("[哔哩哔哩] 生成登录二维码失败")
            yield event.plain_result("生成哔哩哔哩登录二维码失败。")
            return

        await event.send(event.chain_result([Image.fromBytes(qrcode)]))
        yield event.plain_result("请使用哔哩哔哩 App 扫码登录。")

        try:
            result = await parser.complete_qr_login()
        except Exception as exc:
            logger.warning(
                "[哔哩哔哩] 扫码登录失败 | "
                f"error_type={type(exc).__name__} | "
                f"error={self.sender.safe_exception_text(exc)}"
            )
            yield event.plain_result("哔哩哔哩扫码登录失败。")
            return

        for message in result.messages:
            yield event.plain_result(message)

    def _match_parser(self, url: str):
        for keyword, pattern in self.key_pattern_list:
            if keyword in url and (matched := pattern.search(url)):
                return keyword, matched, self.parser_map[keyword]
        raise ValueError(f"未匹配到支持的解析器: {url}")

    async def parse_url(self, url: str):
        """公开解析 API：返回最终内容的纯列表，不发送消息。"""
        keyword, matched, parser = self._match_parser(url)
        return await parser.parse(keyword, matched)

    async def parse_and_send(self, event: AstrMessageEvent, url: str) -> bool:
        """公开解析 API：先发送描述卡片，再发送最终解析内容。"""
        try:
            keyword, matched, parser = self._match_parser(url)

            async def send_desc(description):
                await self.sender.send_desc(event, description)

            result = await parser.parse(keyword, matched, desc_sink=send_desc)
            return await self.sender.parse_result(event, result)
        except Exception as exc:
            await self._notify_parse_failure(event, exc, source="链接解析")
            return False

    @filter.llm_tool(name="parse_media_url")
    async def llm_parse_media_url(self, event: AstrMessageEvent, url: str) -> str:
        """
        解析用户提供的视频、图集或音频链接并自动发送解析结果。

        Args:
            url(string): 待解析的视频或图文等媒体链接
        """
        try:
            sent = await self.parse_and_send(event, url)
            if not sent:
                return "链接解析失败，已记录错误"
            return "链接解析成功并已发送"
        except Exception as e:
            logger.error(f"[LLM Tool] 解析链接失败: {url}, error: {e}")
            return f"解析链接失败: {e}"
