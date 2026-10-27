from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..config import ParserConfig, ParserItem, PluginConfig
from ..download import Downloader
from .base import BaseParser
from .config import (
    ParserConfigError,
    ParserDefinition,
    ParserSettings,
    load_definition,
    load_settings,
    merge_values,
    write_json_atomic,
)


@dataclass(frozen=True, slots=True)
class ParserPackage:
    definition: ParserDefinition
    parser_cls: type[BaseParser]
    settings: ParserSettings


@dataclass(frozen=True)
class ParserRegistrySnapshot:
    packages: tuple[ParserPackage, ...]
    parser_map: dict[str, BaseParser]
    key_pattern_list: tuple[tuple[str, re.Pattern[str]], ...]
    by_name: dict[str, ParserPackage]


class ParserRegistry:
    PACKAGE_ROOT = Path(__file__).parent

    def __init__(self, config: PluginConfig, downloader: Downloader):
        self.cfg = config
        self.downloader = downloader
        self.config_dir = config.data_dir / "parser_config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot: ParserRegistrySnapshot | None = None
        self._parser_instances: dict[str, BaseParser] = {}

    @property
    def snapshot(self) -> ParserRegistrySnapshot:
        if self._snapshot is None:
            raise RuntimeError("解析器注册表尚未加载")
        return self._snapshot

    def discover(self) -> list[ParserDefinition]:
        definitions: list[ParserDefinition] = []
        for directory in sorted(self.PACKAGE_ROOT.iterdir(), key=lambda p: p.name):
            if (
                not directory.is_dir()
                or directory.name.startswith(("_", "."))
                or not (directory / "config.json").is_file()
            ):
                continue
            try:
                definition = load_definition(
                    directory / "config.json",
                    expected_name=directory.name,
                )
            except ParserConfigError as exc:
                logger.error(f"[parser] 配置无效: {directory.name} | {exc}")
                continue
            definitions.append(definition)
        return definitions

    async def load(self) -> ParserRegistrySnapshot:
        definitions = self.discover()
        package_data: list[tuple[ParserDefinition, type[BaseParser]]] = []
        errors: list[str] = []
        for definition in definitions:
            try:
                package_data.append(self._load_package(definition))
            except (ImportError, AttributeError, TypeError, ValueError) as exc:
                errors.append(definition.name)
                logger.error(
                    f"[parser] 加载失败: {definition.name} | "
                    f"error_type={type(exc).__name__} | error={exc}"
                )

        keyword_owners: dict[str, str] = {}
        for definition, parser_cls in package_data:
            for keyword, _ in parser_cls._key_patterns:
                owner = keyword_owners.get(keyword)
                if owner is not None:
                    raise RuntimeError(
                        f"解析器关键词重复: {keyword!r} ({owner}, {definition.name})"
                    )
                keyword_owners[keyword] = definition.name

        for definition, parser_cls in package_data:
            if parser_cls.platform.name != definition.name:
                raise RuntimeError(
                    f"解析器平台名不一致: {definition.name} != "
                    f"{parser_cls.platform.name}"
                )

        settings_by_name: dict[str, ParserSettings] = {}
        for definition, _ in package_data:
            path = self.config_dir / f"{definition.name}.json"
            try:
                if path.is_file():
                    settings = load_settings(path, definition)
                else:
                    settings = ParserSettings(
                        definition=definition,
                        enabled=definition.default_enabled,
                        values=definition.defaults(),
                    )
                    write_json_atomic(path, settings.to_dict())
                settings_by_name[definition.name] = settings
            except (ParserConfigError, OSError) as exc:
                errors.append(definition.name)
                logger.error(f"[parser] 用户配置无效: {definition.name} | {exc}")
                continue

        package_data = [
            item for item in package_data if item[0].name in settings_by_name
        ]

        packages: list[ParserPackage] = []
        parser_map: dict[str, BaseParser] = {}
        pattern_list: list[tuple[str, re.Pattern[str]]] = []
        by_name: dict[str, ParserPackage] = {}
        self.cfg.parser = self._build_parser_config(
            [
                (definition, settings_by_name[definition.name])
                for definition, _ in package_data
            ]
        )
        for definition, parser_cls in package_data:
            settings = settings_by_name[definition.name]
            package = ParserPackage(
                definition=definition,
                parser_cls=parser_cls,
                settings=settings,
            )
            if not settings.enabled:
                old_parser = self._parser_instances.pop(definition.name, None)
                if old_parser is not None:
                    await old_parser.close_session()
                packages.append(package)
                by_name[definition.name] = package
                continue
            parser = self._parser_instances.get(definition.name)
            if parser is None:
                parser = parser_cls(self.cfg, self.downloader)
                self._parser_instances[definition.name] = parser
            packages.append(package)
            by_name[definition.name] = package
            for keyword, pattern in parser_cls._key_patterns:
                parser_map[keyword] = parser
                pattern_list.append((keyword, pattern))

        pattern_list.sort(key=lambda item: -len(item[0]))
        snapshot = ParserRegistrySnapshot(
            packages=tuple(packages),
            parser_map=parser_map,
            key_pattern_list=tuple(pattern_list),
            by_name=by_name,
        )
        self._snapshot = snapshot
        logger.info(
            "[parser] 注册完成 | 发现=%d | 启用=%d | 加载失败=%s",
            len(definitions),
            len({id(parser) for parser in parser_map.values()}),
            "、".join(errors) or "无",
        )
        return snapshot

    def _load_package(
        self,
        definition: ParserDefinition,
    ) -> tuple[ParserDefinition, type[BaseParser]]:
        module = importlib.import_module(f".{definition.name}", __package__)
        parser_cls = getattr(module, "PARSER", None)
        if not isinstance(parser_cls, type) or not issubclass(parser_cls, BaseParser):
            raise TypeError("__init__.py 必须导出继承 BaseParser 的 PARSER")
        return definition, parser_cls

    async def close(self) -> None:
        for parser in self._parser_instances.values():
            await parser.close_session()
        self._parser_instances.clear()
        self._snapshot = None

    async def reload(self) -> ParserRegistrySnapshot:
        snapshot = await self.load()
        self._snapshot = snapshot
        active_names = {
            package.definition.name
            for package in snapshot.packages
            if package.settings.enabled
        }
        for name in set(self._parser_instances) - active_names:
            await self._parser_instances.pop(name).close_session()
        self.cfg.parser = self._build_parser_config(
            (package.definition, package.settings) for package in snapshot.packages
        )
        return snapshot

    async def save(
        self,
        name: str,
        *,
        enabled: bool,
        values: dict[str, Any],
        preserve_masked: bool = False,
    ) -> ParserSettings:
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是 bool")
        package = self.snapshot.by_name.get(name)
        if package is None:
            raise KeyError(name)
        current_values = dict(package.settings.values)
        secret_keys = {
            field.key for field in package.definition.fields if field.secret
        }
        filtered = {
            key: value
            for key, value in values.items()
            if not preserve_masked
            or key not in secret_keys
            or (isinstance(value, str) and value)
        }
        settings = ParserSettings(
            definition=package.definition,
            enabled=enabled,
            values=merge_values(package.definition, current_values, filtered),
        )
        old_parser = self._parser_instances.pop(name, None)
        if old_parser is not None:
            await old_parser.close_session()
        write_json_atomic(self.config_dir / f"{name}.json", settings.to_dict())
        await self.reload()
        return settings

    async def save_many(
        self,
        updates: Iterable[tuple[str, bool, dict[str, Any]]],
    ) -> list[ParserSettings]:
        """校验并写入多个解析器配置，最后统一重建一次快照。"""
        prepared: list[ParserSettings] = []
        previous: dict[str, ParserSettings] = {}
        names: set[str] = set()
        for name, enabled, values in updates:
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是 bool")
            package = self.snapshot.by_name.get(name)
            if package is None:
                raise KeyError(name)
            if name in names:
                raise ValueError(f"解析器重复: {name}")
            settings = ParserSettings(
                definition=package.definition,
                enabled=enabled,
                values=merge_values(
                    package.definition,
                    dict(package.settings.values),
                    dict(values),
                ),
            )
            prepared.append(settings)
            previous[name] = package.settings
            names.add(name)

        written: list[str] = []
        try:
            for settings in prepared:
                write_json_atomic(
                    self.config_dir / f"{settings.name}.json",
                    settings.to_dict(),
                )
                written.append(settings.name)
        except Exception:
            for name in written:
                write_json_atomic(
                    self.config_dir / f"{name}.json",
                    previous[name].to_dict(),
                )
            raise
        for name in names:
            old_parser = self._parser_instances.pop(name, None)
            if old_parser is not None:
                await old_parser.close_session()
        await self.reload()
        return prepared

    @staticmethod
    def _build_parser_config(
        data: Iterable[tuple[ParserDefinition, ParserSettings]],
    ) -> ParserConfig:
        return ParserConfig(
            ParserItem(
                definition.name,
                settings.enabled,
                {
                    field.key: settings.values.get(field.key, field.default)
                    for field in definition.fields
                },
            )
            for definition, settings in data
        )
