from __future__ import annotations

import zoneinfo
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ================ 插件自定义配置 ==================


class ParserItem:
    """单个解析器的运行时配置视图。"""

    def __init__(self, name: str, enabled: bool, values: Mapping[str, Any]):
        self._name = name
        self._enabled = enabled
        self._values = dict(values)

    @property
    def name(self) -> str:
        return self._name

    @property
    def enable(self) -> bool:
        return self._enabled

    def __getattr__(self, key: str) -> Any:
        if key in self._values:
            return self._values[key]
        raise AttributeError(key)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)


class ParserConfig:
    """解析器名称到运行时配置视图的只读容器。"""

    def __init__(self, items: Iterable[ParserItem]):
        self._nodes = {item.name: item for item in items}

    def __getattr__(self, name: str) -> ParserItem:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()

    def platforms(self) -> list[str]:
        return list(self._nodes)

    def enabled_platforms(self) -> list[str]:
        return [name for name, item in self._nodes.items() if item.enable]


class PluginConfig(ConfigNode):
    whitelist: list[str]
    blacklist: list[str]

    arbiter: bool
    require_at_in_group: bool
    enable_reply_parse: bool
    debounce_interval: int

    source_max_size: int
    source_max_minute: int

    audio_to_file: bool
    use_local_renderer: bool
    forward_threshold: int

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None

    clean_cron: str

    _plugin_name = "astrbot_plugin_parser"

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.parser_config_dir = self.data_dir / "parser_config"

        # 解析器注册完成后由 ParserRegistry 注入。
        self.parser = ParserConfig(())

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()
