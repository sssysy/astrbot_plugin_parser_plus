# astrbot_plugin_parser_plus

<p align="center">
  <a href="logo.png">
    <img src="logo.png" width="227" height="227" alt="PLUS 解析器">
  </a>
</p>
<h1 align="center">PLUS 解析器</h1>
<h4 align="center">基于 AstrBot 的多平台媒体链接解析插件</h4>

<p align="center">
  <a href="https://github.com/sssysy/astrbot_plugin_parser_plus" target="_blank"><img src="https://img.shields.io/github/stars/sssysy/astrbot_plugin_parser_plus?style=flat-square" alt="GitHub Stars"></a>
  <a href="LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
  <a href="https://www.python.org/" target="_blank"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://github.com/Soulter/AstrBot" target="_blank"><img src="https://img.shields.io/badge/AstrBot-3.4+-orange.svg" alt="AstrBot 3.4+"></a>
</p>

<div align="center">
  <a href="https://github.com/sssysy/astrbot_plugin_parser_plus" target="_blank">仓库</a> &nbsp; · &nbsp;
  <a href="https://github.com/Soulter/AstrBot" target="_blank">AstrBot</a> &nbsp; · &nbsp;
  <a href="CHANGELOG.md" target="_blank">更新日志</a> &nbsp; · &nbsp;
  <a href="https://github.com/sssysy/astrbot_plugin_parser_plus/issues" target="_blank">问题反馈</a>
</div>

## 丨安装提醒

> [!WARNING]
> 项目仍在持续迭代。部分平台需要 Cookies、代理或外部服务才能正常工作，请先阅读下方的[配置说明](#丨配置说明)与[常见问题](#丨qa)。

## 丨安装

### 手动安装

将仓库放入 AstrBot 的 `data/plugins` 目录，并安装依赖：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/sssysy/astrbot_plugin_parser_plus.git
pip install -r astrbot_plugin_parser_plus/requirements.txt
```

安装完成后重启 AstrBot。若插件市场已经自动安装依赖，无需重复执行 `pip install`。

## 丨支持平台


## 丨命令列表

> 除特别说明外，以下命令均需要 **ADMIN** 权限。普通链接解析不需要发送命令。

<details><summary>会话控制</summary>

| 命令 | 说明 |
| :--- | :--- |
| `开启解析` | 开启当前会话的链接解析，并将会话移出黑名单 |
| `关闭解析` | 关闭当前会话的链接解析，并将会话加入黑名单 |

</details>

<details><summary>B站账号</summary>

| 命令 | 说明 |
| :--- | :--- |
| `登录B站` / `blogin` / `登录b站` | 扫码登录 B站，用于下载高画质视频或访问登录内容 |

> 使用前需确保已启用 B站解析器。登录凭证会保存在插件数据目录，请勿公开或提交到仓库。

</details>

## 致谢

- [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)：本项目核心解析能力的重要来源
- [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)：AstrBot 适配版本的 upstream
- [AstrBot](https://github.com/Soulter/AstrBot)：插件运行框架
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)、[gallery-dl](https://github.com/mikf/gallery-dl) 及所有依赖项目的作者
- 所有提交 Issue、PR 与参与测试的贡献者