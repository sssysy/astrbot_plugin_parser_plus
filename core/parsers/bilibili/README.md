# 哔哩哔哩解析器

该解析器参考 `nonebot-plugin-parser-lite` 的 B 站实现，按本插件 `BaseParser` 契约重写：

- 入口：`parser.py`，只通过 `@handle` 注册。
- 数据：`models.py`，平台响应和卡片指标转换。
- 工具：`utils.py`，BV/AV 转换、媒体 host 校验、动态富文本提取。
- 配置：`config.json`，默认启用，Cookie 字段为 secret。
- 画质：`auto` 请求最高可用清晰度；也可选 8K、4K、1080P60、1080P+、1080P、720P、480P、360P。
- 编码：`auto` 优先 AVC，其次 HEVC、AV1；也可固定偏好。

支持：

- `b23.tv` / `bili2233.cn` 短链。
- 标准链接与纯文本 `BV...`、`av...`，以及 `?p=` 分 P。
- 动态、`t.bilibili.com`、Opus、`/read/cv...` 专栏。
- `live.bilibili.com` 直播、`/bangumi/play/ep|ss` 番剧、`favlist?fid=` 收藏夹。

视频卡片通过 `send_desc_video` 展示封面、分 P 时长、画质、分辨率，以及播放、点赞、评论、收藏、分享、投币、弹幕；UP 主资料与获赞/关注/粉丝同步显示。最终视频始终延迟到卡片发送后，由 `Downloader` 下载并在需要时合并音视频。
