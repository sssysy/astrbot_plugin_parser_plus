---
name: astrbot-parser-platform
description: 编写、重写或审查 astrbot_plugin_parser_plus 的平台解析器。用于新增独立解析器包、修改解析器 config.json、调整 @handle 匹配、实现解析与媒体发送，或排查解析器注册和运行时配置问题。
---

# AstrBot 平台解析器

每个平台解析器独立放在 `core/parsers/<name>/`，禁止在 `main.py` 为平台增加解析分支。

## 目录结构

新解析器必须采用以下结构：

```text
core/parsers/<name>/
├── __init__.py    # 导出 PARSER = <Name>Parser
├── config.json    # 默认值与页面字段定义
├── parser.py      # BaseParser 子类与 @handle 入口
└── 其他模块       # 平台私有数据模型、请求和解析逻辑
```

目录名、`config.json.name`、`PARSER.platform.name` 必须完全一致。注册中心只导入 `<name>/__init__.py`，包内其他模块由解析器自行组织，不作为注册入口。

新增解析器时不得修改以下位置：

- `core/config.py`
- `_conf_schema.json`
- `core/parsers/__init__.py`

`core/parsers/registry.py` 会扫描 `core/parsers/*/config.json`，从各包 `__init__.py` 读取 `PARSER` 并完成注册。

## 解析器契约

- 继承 `BaseParser`。
- 在 `__init__` 中调用 `super().__init__(config, downloader)`。
- 通过 `self.mycfg.<字段>` 读取本平台配置。
- 只使用 `@handle(keyword, pattern)` 注册链接入口。
- 关键词要能有效缩小候选范围，正则必须匹配明确的 host/path。
- 跨解析器关键词必须唯一；重复关键词会导致注册失败。
- API 请求使用 `self.session`，以复用超时、代理和连接池。
- 下载使用 `self.downloader` 或 `BaseParser.create_*_content()`，不得自行实现文件下载链路。
- 单次重定向使用 `parse_with_redirect()`；多次重定向使用 `get_final_url()`。
- 先调用 `send_desc_common()` 或 `send_desc_video()` 发送描述卡片，再返回 `self.parse_result([...])`。
- 最终列表顺序就是消息发送顺序。
- 描述卡片发送前不得启动最终媒体下载。
- 解析失败必须抛出 `ParseException` 或其子类，不得返回占位媒体。
- `__init__` 中不得发起网络请求或执行长时间阻塞操作。
- 解析器不得直接构造 AstrBot 消息组件。
- 图集、图片和视频统一使用 `core/render/default/video.html` 媒体卡片。解析器负责构造卡片主视觉：图集/图片只传第一张图，视频传封面，并通过 `send_desc_common(cover=...)` 或 `send_desc_video(cover=...)` 提供。

创建或重写解析器前，阅读 [references/parser-package.md](references/parser-package.md)。

## 配置边界

`core/parsers/<name>/config.json` 是源码中的默认值和字段定义，只包含：

- `schema_version`
- `name`
- `display_name`
- `default_enabled`
- `fields`

不得把 Cookie、用户值或运行状态写入该文件。

实际用户配置固定存放在：

```text
plugin_data/astrbot_plugin_parser_plus/parser_config/<name>.json
```

用户配置包含 `schema_version`、`enabled` 和 `values`。`enabled` 是解析器启停的唯一来源。敏感字段必须标记 `"secret": true`，配置列表接口不得返回其值。

所有解析器的 `default_enabled` 必须为 `false`。新增解析器默认关闭，修改现有解析器默认值时也不得改为 `true`；用户需要时在配置页显式启用。修改 `default_enabled` 只影响首次生成用户配置，不得覆盖已有 `parser_config/<name>.json` 中的 `enabled`。

`_conf_schema.json` 只保存插件全局配置，不再包含解析器配置；不得恢复 `parsers_template` 或 `default_template.json`。

新增或修改配置字段时，阅读 [references/configuration.md](references/configuration.md)。

## 注册与生效

启动或重载插件时，注册中心会：

1. 扫描 `core/parsers/` 下含 `config.json` 的直接子目录。
2. 只导入各包的 `__init__.py`。
3. 校验目录名、配置名和平台名。
4. 收集 `@handle` 关键词并检查冲突。
5. 读取或创建 `parser_config/<name>.json`。
6. 实例化已启用解析器，并替换 `parser_map` 与 `key_pattern_list`。

页面保存启停状态或参数后可刷新解析器配置；新增、删除解析器目录或修改 Python 代码后，必须重载 AstrBot 插件。

不得使用已废弃的 `context.register_commands()`，也不得导入目录下所有 `.py`。平台聊天命令仍在 `main.py` 静态声明；扫描器只负责链接解析器和关键词。

## 项目边界

- 保留公开入口 `parse_url()`、`parse_and_send()`、`llm_parse_media_url()`。
- 保持 `ParserPlugin.on_message` 是唯一自动消息入口。
- 不添加文字回退、`SendGroup`、按媒体类型重排或第二个自动链接入口。
- 卡片只能通过 `core/render/Renderer` 渲染，消息只能通过 `core/sender.py` 发送。
- `core/render/default/video.html` 是图集/图片/视频通用媒体模板；新增解析器必须通过 `Description.cover`、`Description.contents` 和 `Description.video` 提供媒体元信息，不得直接拼接模板 HTML 或为媒体类型另建卡片入口。
- 不加入 QZone、SnowLuma 或 `gsuid_core` 运行时依赖。
- 不为 `astrbot_plugin_parser_extensions`、旧解析器字段或旧导入路径增加兼容层。
- `docs/parser_old/` 只供重写参考，不得导入、扫描、注册、包装或恢复其中的旧实现。

## 验证

在插件根目录运行：

```powershell
python -m pytest -q
python -m compileall -q main.py core tests
python -m json.tool _conf_schema.json > $null
python -m json.tool core\parsers\<name>\config.json > $null
```

新增解析器或修改注册逻辑时，补充不依赖真实网络的回归测试，至少验证解析器可被发现、身份字段一致、关键词可匹配、用户配置能正确创建。

完成前阅读 [references/checklist.md](references/checklist.md)。
