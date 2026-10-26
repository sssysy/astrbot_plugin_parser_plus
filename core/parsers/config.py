from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger


SUPPORTED_FIELD_TYPES = {
    "bool",
    "int",
    "float",
    "string",
    "text",
    "select",
    "list",
}


class ParserConfigError(ValueError):
    """解析器配置定义或用户配置无效。"""


@dataclass(frozen=True, slots=True)
class ParserField:
    key: str
    type: str
    label: str
    default: Any = None
    hint: str | None = None
    options: tuple[str, ...] = ()
    secret: bool = False


@dataclass(frozen=True, slots=True)
class ParserDefinition:
    name: str
    display_name: str
    default_enabled: bool
    fields: tuple[ParserField, ...]
    schema_version: int = 1

    def defaults(self) -> dict[str, Any]:
        return {
            item.key: list(item.default)
            if isinstance(item.default, list)
            else item.default
            for item in self.fields
        }


@dataclass(slots=True)
class ParserSettings:
    """单个解析器的用户配置，值写入底层 values 字典。"""

    definition: ParserDefinition
    enabled: bool
    values: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, key: str) -> Any:
        fields = self.__dict__.get("definition")
        if fields is not None and key in {item.key for item in fields.fields}:
            return self.values.get(key)
        raise AttributeError(key)

    @property
    def name(self) -> str:
        return self.definition.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.definition.schema_version,
            "enabled": self.enabled,
            "values": dict(self.values),
        }


def _require_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ParserConfigError(f"缺少有效的 {key}")
    return value.strip()


def _normalize_value(field: ParserField, value: Any) -> Any:
    if field.type == "bool":
        if not isinstance(value, bool):
            raise ParserConfigError(f"{field.key} 必须是 bool")
        return value
    if field.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParserConfigError(f"{field.key} 必须是 int")
        return value
    if field.type == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ParserConfigError(f"{field.key} 必须是数字")
        return float(value)
    if field.type in {"string", "text", "select"}:
        if not isinstance(value, str):
            raise ParserConfigError(f"{field.key} 必须是字符串")
        if field.type == "select" and field.options and value not in field.options:
            raise ParserConfigError(f"{field.key} 不是允许的选项")
        return value
    if field.type == "list":
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ParserConfigError(f"{field.key} 必须是字符串列表")
        return list(value)
    raise ParserConfigError(f"不支持的字段类型: {field.type}")


def parse_definition(
    data: Mapping[str, Any], *, expected_name: str
) -> ParserDefinition:
    if data.get("schema_version", 1) != 1:
        raise ParserConfigError("不支持的 schema_version")
    name = _require_string(data, "name")
    if name != expected_name:
        raise ParserConfigError(
            f"config.json name={name!r} 与目录名 {expected_name!r} 不一致"
        )
    display_name = _require_string(data, "display_name")
    default_enabled = data.get("default_enabled", False)
    if not isinstance(default_enabled, bool):
        raise ParserConfigError("default_enabled 必须是 bool")
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ParserConfigError("fields 必须是非空对象")

    fields: list[ParserField] = []
    for key, raw in raw_fields.items():
        if not isinstance(key, str) or not key or not isinstance(raw, dict):
            raise ParserConfigError("fields 中的字段定义无效")
        field_type = raw.get("type")
        if field_type not in SUPPORTED_FIELD_TYPES:
            raise ParserConfigError(f"{key} 的 type 不受支持")
        label = raw.get("label", key)
        if not isinstance(label, str) or not label:
            raise ParserConfigError(f"{key} 的 label 无效")
        options = raw.get("options", [])
        if not isinstance(options, list) or not all(
            isinstance(item, str) for item in options
        ):
            raise ParserConfigError(f"{key} 的 options 无效")
        secret = raw.get("secret", False)
        if not isinstance(secret, bool):
            raise ParserConfigError(f"{key} 的 secret 无效")
        field_def = ParserField(
            key=key,
            type=field_type,
            label=label,
            default=raw.get("default"),
            hint=raw.get("hint"),
            options=tuple(options),
            secret=secret,
        )
        try:
            _normalize_value(field_def, field_def.default)
        except ParserConfigError as exc:
            raise ParserConfigError(f"{key} 的 default 无效: {exc}") from exc
        fields.append(field_def)

    return ParserDefinition(
        name=name,
        display_name=display_name,
        default_enabled=default_enabled,
        fields=tuple(fields),
        schema_version=1,
    )


def load_definition(path: Path, *, expected_name: str) -> ParserDefinition:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParserConfigError(f"读取 {path.name} 失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ParserConfigError(f"{path.name} 必须是 JSON 对象")
    return parse_definition(data, expected_name=expected_name)


def load_settings(
    path: Path,
    definition: ParserDefinition,
) -> ParserSettings:
    if not path.is_file():
        return ParserSettings(
            definition=definition,
            enabled=definition.default_enabled,
            values=definition.defaults(),
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[parser] 用户配置损坏，使用默认值: {path.name}")
        raise ParserConfigError(f"读取用户配置 {path.name} 失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ParserConfigError(f"{path.name} 必须是 JSON 对象")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise ParserConfigError(f"{path.name} 的 enabled 无效")
    raw_values = data.get("values", {})
    if not isinstance(raw_values, dict):
        raise ParserConfigError(f"{path.name} 的 values 无效")

    values: dict[str, Any] = {}
    for field_def in definition.fields:
        value = raw_values.get(field_def.key, field_def.default)
        try:
            values[field_def.key] = _normalize_value(field_def, value)
        except ParserConfigError:
            logger.warning(
                f"[parser:{definition.name}] 字段 {field_def.key} 无效，使用默认值"
            )
            values[field_def.key] = field_def.default
    return ParserSettings(definition=definition, enabled=enabled, values=values)


def merge_values(
    definition: ParserDefinition,
    current: Mapping[str, Any],
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    values = dict(current)
    known_keys = {item.key for item in definition.fields}
    unknown_keys = set(updates) - known_keys
    if unknown_keys:
        raise ParserConfigError(
            f"未知字段: {', '.join(sorted(unknown_keys))}"
        )
    for field_def in definition.fields:
        if field_def.key in updates:
            values[field_def.key] = _normalize_value(
                field_def, updates[field_def.key]
            )
    return values


def write_json_atomic(
    path: Path,
    data: MutableMapping[str, Any] | Mapping[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
