# astrbot_plugin_parser_plus

<p align="center">
  <a href="logo.png">
    <img src="logo.png" width="227" height="227" alt="PLUS 解析器">
  </a>
</p>
<h1 align="center">PLUS 解析器</h1>
<h4 align="center">基于 AstrBot 的多平台媒体链接解析插件</h4>

<p align="center">
  <a href="https://www.python.org/" target="_blank"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://github.com/Soulter/AstrBot" target="_blank"><img src="https://img.shields.io/badge/AstrBot-3.4+-orange.svg" alt="AstrBot 3.4+"></a>
</p>

<div align="center">
  <a href="https://github.com/Soulter/AstrBot" target="_blank">AstrBot</a> &nbsp; · &nbsp;
  <a href="https://github.com/sssysy/astrbot_plugin_parser_plus/issues" target="_blank">问题反馈</a>
</div>

## 丨安装提醒

> **注意：该插件为 [Astrbot](https://github.com/AstrBotDevs/AstrBot) 的扩展，具体安装方式可参考上方安装文档**
>
>🚧 插件仍在持续完善中 🚧

## 丨支持平台

| 平台 | 图文 | 评论区渲染 | 视频 | Live Photo | 音乐 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| AcFun | ❌ | ❌ | ✅ | 🚫 | 🚫 |
| 哔哩哔哩 | ✅ | ✅ | ✅ | 🚫 | 🚫 |
| 抖音 | ✅ | ✅ | ✅ | ✅ | 🚫 |
| 快手 | ✅ | ✅ | ✅ | 🚫 | 🚫 |
| 微博 | ✅ | ✅ | ✅ | ✅ | 🚫 |
| 小红书 | ✅ | ❌ | ✅ | ✅ | 🚫 |
| X | ✅ | ❌ | ✅ | 🚫 | 🚫 |
| Instagram | ✅ | ❌ | ✅ | 🚫 | 🚫 |
| TikTok | ❌ | ❌ | ✅ | 🚫 | 🚫 |
| YouTube | ❌ | ❌ | ✅ | 🚫 | ✅ |
| 知乎 | ✅ | ✅ | ✅ | 🚫 | 🚫 |
| 百度贴吧 | ✅ | ✅ | ✅ | 🚫 | 🚫 |
| NGA | ✅ | ❌ | 🚫 | 🚫 | 🚫 |
| 小黑盒 | ✅ | ❌ | ✅ | ✅ | 🚫 |
| JMComic | ✅ | ✅ | 🚫 | 🚫 | 🚫 |
| Pixiv | ✅ | ❌ | 🚫 | 🚫 | 🚫 |
| Iwara | ✅ | ❌ | ✅ | 🚫 | 🚫 |
| 磁力链接 | ✅ | 🚫 | 🚫 | 🚫 | 🚫 |
| 网易云音乐 | 🚫 | ❌ | 🚫 | 🚫 | ✅ |
| QQ音乐 | 🚫 | ❌ | 🚫 | 🚫 | ✅ |
| 酷狗音乐 | 🚫 | ❌ | 🚫 | 🚫 | ✅ |
| 酷我音乐 | 🚫 | ❌ | 🚫 | 🚫 | ✅ |
| 波点音乐 | 🚫 | ❌ | 🚫 | 🚫 | ✅ |

> ✅ 表示已支持；❌ 表示尚未适配；🚫 表示该平台暂无此类内容形态。

## 丨渲染预览

![哔哩哔哩渲染预览](https://cnb.cool/shiysssysy/img/-/git/raw/main/astrbot_plugin_parser_plus/preview_bilibili.jpg)

## 丨解析器 & 主题包制作
### 解析器开发
> [!Warning]
> 当前插件虽已有解析器开发流程，但尚未支持自定义导入第三方解析器
> 如需使用，请将解析器包放置在 `core\parsers\` 文件夹下
> 插件在重启时会自动扫描所有解析器

> 本插件已内置解析器开发 Skills
> 正常情况下将自动遵守 Skill 进行编写
> 如需手动导入skills，请访问 [astrbot-parser-parser-development](.agents\skills\astrbot-parser-parser-development)

### 主题包制作

> [!Warning]
> 本插件暂未开放自制主题包，但有相关支持计划
> 敬请期待后续适配！

## 丨命令列表

<details><summary>点击展开</summary>

| 命令 | 说明 |
| :--- | :--- |
| `开启解析` | 开启当前会话的链接解析，并将会话移出黑名单 |
| `关闭解析` | 关闭当前会话的链接解析，并将会话加入黑名单 |
| `安装playwright` | 为本地卡片渲染下载并安装 Playwright Chromium 运行时 |

</details>

## 致谢

- [AstrBot](https://github.com/Soulter/AstrBot)
- [Zhalslar 饰乐](https://github.com/Zhalslar)：插件底座作者
- [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)：本插件底座
- [karin-plugin-kkk](https://github.com/ikenxuan/karin-plugin-kkk)：默认主题 UI 设计参考
- [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite)：本项目核心解析器移植至此
- 所有提交 Issue、PR 与参与测试的贡献者
