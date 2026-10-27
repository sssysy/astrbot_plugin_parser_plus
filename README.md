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

本地卡片渲染使用 Playwright，还需要安装 Chromium 运行时：

```bash
python -m playwright install chromium
```

插件启动后，也可以由管理员在会话中发送 `安装playwright`，由插件执行相同安装操作。

如果不使用本地渲染，可在插件配置中关闭“使用本地 Playwright 渲染卡片”，改由 AstrBot 配置的远程 T2I 服务渲染卡片。

安装完成后重启 AstrBot。若插件市场已经自动安装依赖，无需重复执行 `pip install`。

媒体处理优先使用系统 FFmpeg；没有系统 FFmpeg 时会自动使用 `imageio-ffmpeg` 内置的 `ffmpeg`。`imageio-ffmpeg` 不提供 `ffprobe`，缺少它时仅跳过部分音频探测/转码，不会中断解析。若需要完整能力，建议安装系统 FFmpeg。

## 丨支持平台

> 当前新版架构包含抖音、哔哩哔哩、小红书、微博、知乎、小黑盒、JMComic、Pixiv、Iwara、磁力链接，以及网易云音乐、QQ音乐、酷狗音乐解析器。其余旧平台代码仅在 `docs/parser_old/` 中作为重写参考归档，不参与导入、不提供兼容入口，也不会被旧实现恢复。

| 平台 | 状态 |
| :--- | :--- |
| 抖音 | 已迁移 |
| 哔哩哔哩 | 已迁移（视频、短链、分 P、动态、图文、专栏、直播、番剧、收藏夹） |
| 小红书 | 已迁移（视频、图文、实况图、长短链、无水印图片） |
| JMComic | 已迁移（章节解析、图片合并/PDF） |
| Pixiv | 已迁移（插画、漫画 PDF、动图 GIF、小说 TXT） |
| Iwara | 已迁移（视频、画质选择、图集、R18 忽略/模糊） |
| 磁力链接 | 已迁移（资源信息、预览图原图/模糊/不发送） |

哔哩哔哩卡片指标包含播放、点赞、评论、收藏、分享、投币与弹幕；视频链接额外显示画质、分辨率和分集时长，UP 主资料会展示获赞、关注与粉丝数。DASH 音视频流由共享下载器下载后合并，优先避开 PCDN 地址。

小红书支持 `xiaohongshu.com`、`xhslink.com` 与 `xhslink.cn` 链接。视频会按配置选择可用画质，图集使用原图地址，实况图优先合成为可播放视频；部分登录态受限的笔记可在解析器配置页填写网页 Cookie。

Pixiv 支持 `pixiv.net/artworks`、小说链接以及 `pid 123`、`pixivid 123` 入口。漫画会按顺序下载并合并为 PDF，动图会合成为 GIF；R18 内容可配置为正常发送、模糊封面并将正文转为 PDF，或直接忽略。

Iwara 支持 `iwara.tv/video/<id>` 与 `iwara.tv/image/<id>`。视频按配置选择 Source、preview、540 或 360 画质并逐级回退；图集按原始顺序发送。R18 内容可配置为模糊封面与图片、原样发送或忽略。私密作品可在解析器配置页填写网页 Cookie。

## 丨命令列表

> 除特别说明外，以下命令均需要 **ADMIN** 权限。普通链接解析不需要发送命令。

<details><summary>会话控制</summary>

| 命令 | 说明 |
| :--- | :--- |
| `开启解析` | 开启当前会话的链接解析，并将会话移出黑名单 |
| `关闭解析` | 关闭当前会话的链接解析，并将会话加入黑名单 |

</details>

<details><summary>本地渲染</summary>

| 命令 | 说明 |
| :--- | :--- |
| `安装playwright` | 为本地卡片渲染下载并安装 Playwright Chromium 运行时 |

> 安装过程可能需要数分钟。安装成功后无需重启插件，可直接重试链接解析。

</details>

## 丨解析器配置

解析器配置不再写入 `_conf_schema.json`。在插件详情页打开“解析器配置”页面即可：

- 启用或停用解析器。
- 修改该解析器声明的参数。
- 查看皮肤包页签（当前为后续功能预留）。

用户配置保存在插件数据目录的 `parser_config/` 下。解析器自带的 `config.json` 只提供默认值和页面字段定义，不应直接修改为用户配置。

新增解析器目录或修改解析器 Python 代码后，需要重载插件；仅修改启停与参数时，保存后可立即生效。

## 致谢

- [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)：本项目核心解析能力的重要来源
- [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)：AstrBot 适配版本的 upstream
- [Zhalslar 饰乐](https://github.com/Zhalslar)：原项目作者
- [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite)：哔哩哔哩解析实现参考
- [karin-plugin-kkk](https://github.com/ikenxuan/karin-plugin-kkk)：默认主题 UI 设计参考
- [AstrBot](https://github.com/Soulter/AstrBot)：插件运行框架
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)、[gallery-dl](https://github.com/mikf/gallery-dl) 及所有依赖项目的作者
- 所有提交 Issue、PR 与参与测试的贡献者
