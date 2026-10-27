# 解析器配置参考

## 源码默认配置

`core/parsers/<name>/config.json` 使用以下结构：

```json
{
  "schema_version": 1,
  "name": "example",
  "display_name": "示例平台",
  "default_enabled": true,
  "fields": {
    "use_proxy": {
      "type": "bool",
      "label": "使用代理",
      "default": false
    },
    "cookies": {
      "type": "text",
      "label": "Cookies",
      "hint": "选填",
      "secret": true,
      "default": ""
    },
    "quality": {
      "type": "select",
      "label": "画质",
      "options": ["auto", "1080p", "720p"],
      "default": "auto"
    }
  }
}
```

支持的字段类型：

- `bool`
- `int`
- `float`
- `string`
- `text`
- `select`
- `list`

`list` 的默认值必须是字符串列表。注册中心会在注册前校验类型、名称、schema 版本和默认值；定义无效时拒绝该解析器。

## 用户配置

解析器首次被发现时，注册中心会创建：

```text
plugin_data/astrbot_plugin_parser_plus/parser_config/<name>.json
```

结构如下：

```json
{
  "schema_version": 1,
  "enabled": true,
  "values": {
    "use_proxy": false,
    "cookies": "",
    "quality": "auto"
  }
}
```

不得把用户值写回源码目录，也不得为解析器另设第二个全局启用开关。

## 读取配置

基类会为当前平台解析 `ParserItem`。直接在解析器中读取声明字段：

```python
quality = self.mycfg.quality
cookies = self.mycfg.cookies or ""
headers = self.ios_headers if self.mycfg.use_proxy else self.headers
```

不要在解析器各模块中散落 `config.parser.<name>` 查询。

## 配置页面边界

页面位于 `pages/parser-settings/index.html`，通过 AstrBot Plugin Page 桥接调用 `main.py` 中注册的接口。

与解析器有关的页面职责仅包括：

- 列出解析器定义和非敏感值。
- 保存单个解析器的 `enabled` 与声明字段。
- 对标记为 `secret` 的值进行遮蔽，不回传真实值。
- 页面提交空敏感字段时保留已存值。
- 保存后刷新解析器注册快照。

页面接口必须拒绝未登录 Dashboard 的请求，也不得接受任意文件路径。

## 新增字段

新增字段时依次完成：

1. 在解析器 `config.json` 中添加字段、类型、标签和合法默认值。
2. 在解析器中使用 `self.mycfg.<字段>`。
3. 凭证字段设置 `secret: true`。
4. 检查旧用户配置缺少该字段时的默认值行为，不要添加隐蔽兼容层。
5. 补充聚焦配置或注册逻辑的测试。
6. 校验 JSON。

不得把解析器字段写入 `_conf_schema.json`。

## 生效时机

- 保存启停状态或参数后会刷新解析器配置，并可能替换运行中的解析器实例。
- 新增或删除解析器目录、修改 Python 代码后，需要重载 AstrBot 插件。
- 注册中心负责解析器实例生命周期，并关闭被替换或停用的实例。
