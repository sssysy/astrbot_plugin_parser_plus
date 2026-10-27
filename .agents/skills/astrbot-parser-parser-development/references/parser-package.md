# 解析器包编写参考

## 最小包结构

`core/parsers/example/__init__.py`

```python
from .parser import ExampleParser

name = "example"
PARSER = ExampleParser

__all__ = ["ExampleParser", "PARSER", "name"]
```

`core/parsers/example/parser.py`

```python
from __future__ import annotations

import re
from typing import ClassVar

from ...config import PluginConfig
from ...data import Platform
from ...download import Downloader
from ..base import BaseParser, ParseException, handle


class ExampleParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="example",
        display_name="示例平台",
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle("example.com/post/", r"example\.com/post/(?P<post_id>\d+)")
    async def _parse_post(self, searched: re.Match[str]):
        post_id = searched.group("post_id")
        info = await self.fetch_info(post_id)
        if info is None:
            raise ParseException("作品不存在")

        await self.send_desc_common(
            info.title,
            info.author,
            [info.text, *info.card_images],
            url=f"https://example.com/post/{post_id}",
        )
        return self.parse_result([info.video, *info.images])
```

示例省略了真实 HTTP 实现。应按照平台接口和现有辅助方法实现，不要另建下载器。

## `__init__.py` 规则

- 从同包模块导入解析器类，并导出为 `PARSER`。
- 模块级 `name` 必须与目录名、平台名一致。
- 不在这里发起网络请求、写文件、加载凭证或启动浏览器。
- 不在包入口逐个导入所有内部模块。

## `@handle` 规则

- 第一个参数是用于低成本预筛选的关键词。
- 第二个参数是正则，必须匹配明确 host/path，或有明确来源的紧凑 ID 形式。
- 优先使用命名分组，在处理方法中通过 `searched.group(...)` 读取。
- 不使用 `.*` 或宽泛的 `https?://` 规则抢占其他平台链接。
- 关键词必须在所有解析器中唯一；冲突会中止注册。
- 同一平台有多种明确链接形式时，可以注册多个处理方法。

## 处理方法

处理方法必须是异步函数，接收正则匹配对象：

```python
@handle("example.com/", r"example\.com/(?P<kind>video|note)/(?P<item_id>\d+)")
async def _parse_item(self, searched: re.Match[str]):
    kind = searched.group("kind")
    item_id = searched.group("item_id")
    ...
```

标准流程是：获取元信息、构造描述内容、调用 `send_desc_*`、构造延迟下载内容、返回 `parse_result([...])`。

## 内容构造

优先使用基类方法，不要手工下载：

- `create_author(name, avatar_url, ...)`
- `create_video_content(url, ...)`
- `create_video_content_by_loader(loader, ...)`
- `create_video_content_by_task(task, ...)`
- `create_image_contents(urls, ...)`
- `create_graphics_content(image_url, text, alt, ...)`
- `create_dynamic_contents(urls, ...)`
- `create_audio_content(url, ...)`
- `create_live_photo_content(video_url, image_url, ...)`
- `create_file_content(url, name, ...)`

这些方法会按需创建延迟 loader。解析阶段不要调用 `get_path()` 强制提前下载。

## 网络与凭证

- HTTP 请求使用 `self.session`，统一复用超时、代理和连接池。
- 凭证在首次使用时懒加载；通过现有 `CookieJar`/配置机制保存到插件数据目录。
- 代理使用 `self.proxy`，它由 `self.mycfg.use_proxy` 和全局代理配置派生。
- 设置有限重试并检查响应状态，不得把未知响应静默转换成假数据。
- 日志不得包含真实 Cookie、Token、密码、二维码凭证或完整原始响应。

## 数据模型

平台响应模型可以放在解析器包内，使用 `msgspec.Struct` 或 dataclass。只有确实跨平台复用的模型才放进 `core/`。

解析器边界最终必须转换为 `Description` 和标准 `*Content` 类型。列表顺序就是发送顺序，不构造中间发送分组，也不按媒体类型排序。

## 拆包原则

职责确实不同时可以拆分：

```text
douyin/
├── __init__.py
├── config.json
├── parser.py
├── video.py
├── live.py
├── comment.py
├── slides.py
└── utils.py
```

不要为了表面整洁拆散短暂、线性的解析流程。
