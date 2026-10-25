# AGENTS.md

## 0. 项目定位

`astrbot_plugin_parser_plus` 是 AstrBot 原生插件，用于识别消息中的链接、分享卡片和引用内容，调用对应平台解析器生成统一的 `ParseResult`，再按发送策略回复媒体或信息卡片。

- 插件入口是 `main.py` 中的 `ParserPlugin`。
- 运行时配置来自 `AstrBotConfig`。
- 插件数据目录由 `PluginConfig` 统一计算，业务代码不得写入源码目录。
- 本插件不依赖 `gsuid_core`，不得复用其运行时 API。
- QQ 空间解析已从项目中彻底移除。除非用户明确要求恢复，否则不得重新加入 QZone、SnowLuma 凭证或相关配置、测试和链接特判。

## 1. 已确定的架构约束

- 使用 AstrBot 原生 `Star` + `@filter` 体系。
- 自动链接解析只有 `ParserPlugin.on_message` 一个 `@filter.event_message_type(ALL)` 入口。
- 不使用 `@filter.regex` 作为自动链接解析主入口。
- 插件不定义自己的命令前缀，统一使用 AstrBot 全局 `wake_prefix`。
- 不使用数据库。
- 不调用 `event.stop_event()`。
- 不使用 `GreedyStr`。
- 解析器通过 `BaseParser` 子类和 `@handle(...)` 注册，不在 `main.py` 手写平台分支。
- 所有平台结果统一转换为 `core/data.py` 中的 `ParseResult`，不得让发送层理解平台私有结构。
- 信息卡片由 `core/render.py` 使用 Pillow + apilmoji 绘制，不使用 HTML 模板、Playwright、`html_render()` 或本地静态文件服务器。
- 下载能力统一由 `core/download.py` 提供；新增解析器不得自行实现落盘下载链路。
- `core/sender.py` 只负责“怎么发”，不负责“怎么解析”。
- `main.py` 中可以为了 AstrBot 事件与消息组件使用 `astrbot.core.*` 类型；新增解析器代码不得继续扩散框架内部依赖，优先复用 `BaseParser` 和 `core/data.py`。

## 2. 目录结构

```text
astrbot_plugin_parser/
├── main.py                     # 插件入口、事件分发、命令、公开解析 API
├── metadata.yaml               # 插件元数据
├── _conf_schema.json           # AstrBot WebUI 配置 Schema
├── default_template.json       # 首次运行使用的 parser 配置模板
├── requirements.txt            # 第三方依赖
├── README.md
├── CHANGELOG.md
├── core/
│   ├── config.py               # ConfigNode、ParserConfig、PluginConfig
│   ├── data.py                 # ParseResult、MediaContent、SendGroup 等模型
│   ├── download.py             # 共享 aiohttp 下载、yt-dlp、媒体缓存
│   ├── render.py               # Pillow 卡片渲染
│   ├── sender.py               # 发送计划、消息段构建与发送
│   ├── cookie.py               # CookieJar、Netscape/Header Cookie 解析
│   ├── arbiter.py              # QQ 群多 Bot 贴表情仲裁
│   ├── debounce.py             # 会话级链接/资源防抖
│   ├── clean.py                # 定时缓存清理
│   ├── utils.py                # URL、文件、ffmpeg 等通用工具
│   ├── exception.py            # 解析和下载异常
│   ├── constants.py            # 公共请求头
│   ├── resources/              # 字体、按钮、平台 logo
│   └── parsers/
│       ├── __init__.py         # parser 模块导入与注册入口
│       ├── base.py             # BaseParser、handle、结果与下载辅助方法
│       ├── example.py          # 解析器示例
│       └── ...                 # 各平台解析器，复杂平台可使用子包
└── tests/                      # 本地测试
```

目录规则：

- `core/__init__.py` 保持空包标记，不承载业务逻辑。
- `core/parsers/__init__.py` 是解析器注册入口；新增 parser 必须在此导入并加入 `__all__`。
- 单文件平台放在 `core/parsers/<name>.py`；需要多个模块时使用 `core/parsers/<name>/`。
- 跨平台通用代码放 `core/` 下的公共模块，禁止复制到多个 parser。
- 测试、缓存、截图和临时产物不得写入 `core/resources/`。
- 不新增 `*_SUMMARY.md` 之类的过程报告或临时说明文件。

## 3. 常用命令与验证

在项目根目录运行：

```powershell
python -m pytest -q
python -m compileall -q main.py core tests
python -m json.tool _conf_schema.json > $null
python -m json.tool default_template.json > $null
```

可用时再运行：

```powershell
ruff format main.py core tests
ruff check main.py core tests
```

要求：

- 修改 Python 后至少通过测试和 `compileall`。
- 修改 `_conf_schema.json` 或 `default_template.json` 后必须校验 JSON。
- 修复 bug 时优先补最小回归测试，不依赖真实网络和真实 Cookie。
- 不为了通过检查而大范围格式化无关文件。

## 4. 消息入口与处理顺序

自动解析固定经过 `ParserPlugin.on_message`，当前顺序如下：

1. 检查白名单和黑名单。
2. 读取消息链，拒绝空消息。
3. 处理“专门 @ 其他 Bot”的跳过规则与群聊 @ 要求。
4. 扫描整条消息链中的 `Json` 组件，调用 `extract_json_url()`。
5. 配置允许时扫描被引用消息中的 `Plain` 与 `Json`。
6. 用“关键词预筛选 + 正则匹配”选择第一个 parser。
7. Aiocqhttp 群聊执行贴表情仲裁。
8. 执行链接级防抖。
9. 调用对应 parser 的 `parse()`。
10. 用 `ParseResult.get_resource_id()` 执行资源级防抖。
11. 交给 `MessageSender.send_parse_result()` 发送。

修改此顺序前必须说明原因，并检查白名单、@ 规则、仲裁、防抖和引用解析是否会回归。不要另起第二个自动链接入口。

## 5. 链接与卡片提取

`core/utils.py` 中的 `URL_RE`、`extract_json_url()` 和 `_clean_embedded_url()` 是公共链接提取逻辑。

- 只接受 `http` 和 `https` URL。
- JSON 卡片优先读取明确的跳转字段，如 `jumpUrl`、`qqdocurl`、`shareUrl`、`targetUrl`、`sourceUrl`、`url`。
- 优先字段未命中时，才做递归兜底扫描。
- 提取出的字符串必须反转义、URL 解码并去掉尾部噪声。
- 新增特定平台的卡片字段时，应说明来源并保持通用性，不加入已删除的 QZone 特判。
- parser 匹配使用 `@handle()` 提供的预编译正则，不用简单 `in` 判断代替正则。
- 关键词必须能有效缩小候选 parser 范围，正则必须匹配明确 host/path，不使用过宽规则抢占其他平台链接。

## 6. Parser 契约

每个 parser 必须：

- 继承 `BaseParser`。
- 声明 `platform: ClassVar[Platform]`，`name` 对应配置模板的 `__template_key`。
- 在 `__init__` 中调用 `super().__init__(config, downloader)`。
- 设置 `self.mycfg = config.parser.<platform_name>`，避免直接散读原始配置。
- 使用 `@handle(keyword, pattern)` 注册链接入口。
- 在 `core/parsers/__init__.py` 中导入并加入 `__all__`。
- 在 `core/config.py` 的 `ParserConfig` 中补充类型声明。
- 在 `_conf_schema.json` 的 `parsers_template.templates` 中补充配置。
- 需要默认启用时，才同步加入 `default_template.json`；仅加入 Schema 不会自动启用 parser。

解析实现要求：

- API 请求优先使用 `self.session`，以复用超时、代理和连接池。
- 媒体下载使用 `self.downloader` 或 `BaseParser.create_*_content()`，不得在 parser 中手工写文件。
- 短链解析优先使用 `parse_with_redirect()`；需要多次重定向时使用 `get_final_url()`。
- 返回统一结果时使用 `self.result(...)`。
- 解析失败抛出 `ParseException` 或其子类，不返回伪造的占位媒体。
- parser 只负责获取和标准化内容，不渲染卡片、不发送消息、不修改会话黑名单。
- 不使用测试链接、测试账号或测试 Cookie 作为运行时兜底。
- `__init__` 中避免网络请求和长时间阻塞；可在首次解析时再懒加载凭证。

推荐骨架：

```python
class FooParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="foo", display_name="示例平台")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.foo

    @handle("foo.com", r"foo\.com/post/(?P<post_id>\d+)")
    async def _parse_post(self, searched: Match[str]):
        post_id = searched.group("post_id")
        # 请求 API、下载媒体、构建内容
        return self.result(
            title="...",
            contents=[...],
            url=f"https://foo.com/post/{post_id}",
        )
```

## 7. 数据模型与结果

- `ParseResult` 是 parser 与 sender 之间唯一的公共结果模型。
- `contents` 保存主要媒体和文本；`repost` 保存转发内容；`send_groups` 用于需要分组发送的特殊场景。
- `MediaContent.path_task` 可以保存 `Path` 或异步下载任务，按需通过 `get_path()` 解析。
- 图片、视频、音频、文件、动态图和图文分别使用对应 `*Content` 类型，不要用字符串冒充媒体。
- `ParseResult.get_resource_id()` 是防抖指纹，必须保持轻量、稳定、无 IO；不要加入随机值、Cookie 或 Token。
- 修改公共模型时同时检查 `core/render.py`、`core/sender.py`、`core/data.py` 中的序列化/遍历逻辑和相关测试。

## 8. 下载、异步与资源释放

- 网络 I/O 使用 `aiohttp`；必须阻塞的 `curl_cffi`、`yt-dlp`、Pillow 保存等调用放入 `asyncio.to_thread()`。
- 文件写入使用 `aiofiles`；简单删除或路径操作可用 `to_thread`。
- 禁止在事件循环中调用 `requests`、`time.sleep()` 或长时间同步阻塞代码。
- 下载必须设置超时、有限重试和大小限制，失败不得无限重试。
- `Downloader` 使用共享 `ClientSession`；不在 parser 内创建新的通用下载 Session。
- 新增长生命周期任务、会话或调度器时，必须在 `ParserPlugin.terminate()` 或所属组件的 `close()/stop()` 中释放。
- 缓存统一使用 `cfg.cache_dir`，Cookie 统一使用 `cfg.cookie_dir`；禁止写入插件源码目录。

## 9. 渲染与发送边界

渲染：

- 只通过 `Renderer` 生成卡片；新增布局或样式放 `core/render.py` 或对应资源目录。
- 不使用 Playwright、HTML 页面、`html_render()`、独立 CSS/JS 或 `file://` 资源。
- `Renderer.render_card()` 失败返回 `None`，日志中不得输出 base64、Cookie、Token 或完整敏感响应。
- 卡片文件写入 `cfg.cache_dir`，由缓存清理器负责回收。

发送：

- `core/sender.py` 不解析站点、不重新请求站点、不修改 parser 配置。
- 发送策略由 `SendGroup` 和当前配置决定：预览卡片、转发阈值、合并转发。
- 统一使用 `await event.send(event.chain_result(...))`。
- 不调用 `event.stop_event()`，不修改其他插件的事件状态。
- 发送失败要记录必要的消息段元信息，但不能记录大段二进制或敏感内容。

## 10. 配置与注册纪律

- 配置入口是 `_conf_schema.json`，运行时强类型封装在 `core/config.py`。
- `ParserItem` 的公共字段要与至少一个 parser 的实际需求对应，不保留废弃字段。
- 新增 parser 配置时必须同步检查：Schema、`ParserConfig` 类型声明、`default_template.json`、parser 的 `self.mycfg`。
- `parsers_template` 为空时才会从 `default_template.json` 生成默认配置；已有用户配置不会被自动重写。
- `enable` 是 parser 是否启用的唯一来源，不在代码中另设启用开关。
- 不添加配置兼容层，不为旧字段维护隐蔽 fallback。
- 只注册明确需要的命令、别名和 parser。
- 新增命令别名时使用 `alias={...}`，不堆叠多个命令装饰器。
- 不恢复已删除的平台、旧路径 re-export 或废弃前缀。
- 修改默认模板时要考虑已有用户和首次安装用户的行为差异。

## 11. 安全与日志

- 禁止提交或记录真实 Cookie、Token、密码、二维码登录凭证和完整原始响应。
- Cookie 文件只能放在插件数据目录。
- 面向用户的错误保持简洁，不返回堆栈、路径、请求头或内部异常对象。
- 面向日志使用 `astrbot.api.logger`，记录 parser 名称和可安全展示的上下文。
- 禁止静默吞掉未知异常后返回假数据；可恢复异常应降级并记录，未知异常应转换后上抛或记录。
- 新增外链请求前检查 URL 来源和 host 限制，避免把任意用户输入当成可信下载地址。

## 12. 代码风格与变更边界

- 遵循仓库根 `AGENTS.md` 的通用 AstrBot 规范；本文件只补充插件专属约束。
- 使用 `pathlib.Path`，不拼接字符串路径。
- 优先复用现有模型、异常、下载器和工具函数，不新增重复抽象。
- 不为了“整洁”拆分线性逻辑；只有高复用或明显复杂时才提取 helper。
- 不顺手重构无关模块，不改变既有函数结构，除非修改本身需要。
- 保留现有公开入口：`parse_url()`、`parse_and_send()`、`llm_parse_media_url()`。
- `core/parsers/__init__.py` 的聚合导入是注册机制，不能被当作普通 re-export 清理掉。
- 不未经确认增加新依赖、外部服务、数据库、AI 工具或平台兼容层。

## 13. 测试要求

- 测试优先覆盖纯逻辑：URL 提取、Cookie 域/路径匹配、parser 正则、结果结构、发送策略。
- 网络测试必须 mock session 或使用 `monkeypatch`，不依赖真实站点。
- Cookie 测试使用 `tmp_path`，不得读写真实插件数据目录。
- 修改 parser 注册或配置时，补一个能证明 parser 能被导入/匹配的测试。
- 修改 `extract_json_url()` 时，至少覆盖明确的 JSON 字段和兜底扫描。
- 修改下载、渲染或发送边界时，检查失败降级和资源释放。

## 14. 自检清单

- [ ] QQ 空间、QZone、SnowLuma 相关代码没有被重新引入。
- [ ] 自动解析仍然只有一个 ALL 入口。
- [ ] 新 parser 已在 `core/parsers/__init__.py` 注册。
- [ ] 新 parser 的 `platform.name`、配置模板 key 和 `ParserConfig` 声明一致。
- [ ] 新 parser 已在 `_conf_schema.json` 中配置；需要默认启用时也更新了 `default_template.json`。
- [ ] `@handle()` 关键词唯一，正则只匹配明确 host/path。
- [ ] API 请求异步、有限重试，媒体下载经过 `Downloader`。
- [ ] parser 返回 `ParseResult`，不自行发送消息或渲染卡片。
- [ ] sender 不执行解析或二次请求。
- [ ] 长生命周期会话、任务和调度器均有释放路径。
- [ ] 错误信息不暴露 Cookie、Token、堆栈和原始响应。
- [ ] `python -m pytest -q` 通过。
- [ ] `python -m compileall -q main.py core tests` 通过。
- [ ] 修改 JSON 配置后已通过 `python -m json.tool`。
