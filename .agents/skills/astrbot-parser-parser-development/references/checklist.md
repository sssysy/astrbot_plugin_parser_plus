# 平台解析器检查清单

## 架构

- [ ] 解析器是新的 `core/parsers/<name>/` 包，不是在 `main.py` 中增加平台分支。
- [ ] `__init__.py` 导出 `PARSER`，且不执行 I/O。
- [ ] 目录名、`config.json.name`、`PARSER.platform.name` 一致。
- [ ] 没有通过修改 `core/config.py`、`_conf_schema.json` 或 `core/parsers/__init__.py` 注册平台。
- [ ] `docs/parser_old/` 只作重写参考，没有导入、包装或复用旧运行时实现。

## 匹配与解析

- [ ] 所有链接入口都使用 `@handle(keyword, pattern)`。
- [ ] 关键词全局唯一，并能有效缩小候选范围。
- [ ] 正则匹配明确 host/path，不抢占其他平台链接。
- [ ] 配置通过 `self.mycfg` 读取，没有散落的原始配置访问。
- [ ] `__init__` 不发起网络请求，也不执行长时间阻塞操作。
- [ ] API 请求使用 `self.session`。
- [ ] 访问失败或认证失败转换为 `ParseException` 或其子类。

## 描述与媒体

- [ ] 使用 `send_desc_common()` 或 `send_desc_video()` 发送描述卡片。
- [ ] 图集/图片的描述卡片只传第一张图片作为 `cover`，视频传封面；最终媒体仍按完整有序列表通过 `Description.contents` 传入。
- [ ] 最终结果是按发送顺序排列的 `parse_result([...])`。
- [ ] 描述阶段结束前没有启动最终媒体下载。
- [ ] 媒体使用 `self.downloader` 或 `BaseParser.create_*_content()`。
- [ ] 不存在文字回退、发送分组、媒体类型重排或占位媒体。
- [ ] 解析器没有直接构造 AstrBot 消息组件。

## 配置

- [ ] 默认值和页面字段定义位于解析器包内 `config.json`。
- [ ] 用户值位于 `plugin_data/astrbot_plugin_parser_plus/parser_config/`。
- [ ] 显式设置了 `default_enabled: false`，没有默认启用任何解析器。
- [ ] 修改默认值没有覆盖已有用户配置中的 `enabled`。
- [ ] 敏感字段标记 `secret: true`，且不会通过配置列表接口返回。
- [ ] `_conf_schema.json` 只包含插件全局配置。
- [ ] 没有恢复旧 `parsers_template`/`default_template.json` 兼容层。

## 生命周期与安全

- [ ] 新增会话、任务、调度器等长期资源都有释放路径。
- [ ] 日志不含 Cookie、Token、密码、二维码凭证或原始响应正文。
- [ ] 重定向和外部下载按需校验来源与 host。
- [ ] 用户错误信息不暴露堆栈、路径或内部异常对象。

## 测试与验证

- [ ] 网络测试使用 mock session，不依赖真实 Cookie 或线上站点。
- [ ] 注册测试证明解析器包可被发现，关键词可匹配。
- [ ] 配置测试证明用户配置可创建、校验和启停。
- [ ] `python -m json.tool core\parsers\<name>\config.json > $null` 通过。
- [ ] `python -m compileall -q main.py core tests` 通过。
- [ ] `python -m pytest -q` 通过；若存在无关的既有失败，准确说明失败项。
