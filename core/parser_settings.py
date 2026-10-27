from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .parsers.config import write_json_atomic
from .parsers.registry import ParserRegistry


class ParserSettingsService:
    """插件 Page 使用的解析器配置与皮肤设置服务。"""

    SKIN_ROOT = Path(__file__).parent / "render" / "skins"

    def __init__(self, registry: ParserRegistry, config_dir: Path):
        self.registry = registry
        self.config_dir = config_dir
        self.page_settings_file = config_dir / "page_settings.json"

    def list_parsers(self) -> list[dict[str, Any]]:
        result = []
        for package in self.registry.snapshot.packages:
            definition = package.definition
            settings = package.settings
            fields = [
                {
                    "key": field.key,
                    "type": field.type,
                    "label": field.label,
                    "hint": field.hint,
                    "options": list(field.options),
                    "secret": field.secret,
                    "default": field.default,
                    "configured": bool(settings.values.get(field.key))
                    if field.secret
                    else None,
                }
                for field in definition.fields
            ]
            public_values = {
                field.key: settings.values.get(field.key, field.default)
                for field in definition.fields
                if not field.secret
            }
            result.append(
                {
                    "name": definition.name,
                    "display_name": definition.display_name,
                    "enabled": settings.enabled,
                    "fields": fields,
                    "values": public_values,
                }
            )
        return result

    async def save_parser(
        self,
        name: str,
        *,
        enabled: bool,
        updates: dict[str, Any],
        preserve_masked: bool = False,
    ) -> dict[str, Any]:
        settings = await self.registry.save(
            name,
            enabled=enabled,
            values=dict(updates),
            preserve_masked=preserve_masked,
        )
        return {
            "saved": True,
            "reload_required": False,
            "enabled": settings.enabled,
        }

    async def save_parsers(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        """一次保存多个解析器配置，全部校验通过后再写入。"""
        prepared = []
        seen: set[str] = set()
        for item in updates:
            if not isinstance(item, dict):
                raise ValueError("解析器配置格式无效")
            name = item.get("name")
            enabled = item.get("enabled")
            values = item.get("values")
            if not isinstance(name, str) or not name:
                raise ValueError("解析器名称无效")
            if name in seen:
                raise ValueError(f"解析器重复: {name}")
            if not isinstance(enabled, bool) or not isinstance(values, dict):
                raise ValueError(f"解析器 {name} 的配置格式无效")
            prepared.append((name, enabled, values))
            seen.add(name)

        if not prepared:
            return {"saved": True, "reload_required": False, "count": 0}

        await self.registry.save_many(prepared)
        return {
            "saved": True,
            "reload_required": False,
            "count": len(prepared),
        }

    def list_skins(self) -> dict[str, Any]:
        settings = self._load_page_settings()
        skins = []
        directories = (
            sorted(self.SKIN_ROOT.iterdir()) if self.SKIN_ROOT.is_dir() else []
        )
        for directory in directories:
            if directory.is_dir() and not directory.name.startswith((".", "_")):
                skins.append(
                    {
                        "name": directory.name,
                        "display_name": directory.name,
                    }
                )
        if not skins:
            skins = [{"name": "default", "display_name": "默认皮肤"}]
        return {
            "current": settings.get("skin", "default"),
            "skins": skins,
            "editable": False,
            "message": "皮肤包切换功能预留，当前仅支持内置默认皮肤。",
        }

    def save_skin(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("皮肤名称无效")
        if name not in {item["name"] for item in self.list_skins()["skins"]}:
            raise ValueError("皮肤不存在")
        settings = self._load_page_settings()
        settings["skin"] = name
        write_json_atomic(self.page_settings_file, settings)

    def _load_page_settings(self) -> dict[str, Any]:
        if not self.page_settings_file.is_file():
            return {"skin": "default"}
        try:
            data = json.loads(self.page_settings_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[parser-page] 页面设置损坏，使用默认值")
            return {"skin": "default"}
        return data if isinstance(data, dict) else {"skin": "default"}
