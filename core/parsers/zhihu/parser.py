from __future__ import annotations

import json
import re
from typing import Any, ClassVar

import msgspec
from bs4 import BeautifulSoup

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import (
    Author,
    Comment,
    ContentItem,
    ImageContent,
    MediaContent,
    Metric,
    Platform,
    TextContent,
    VideoContent,
)
from ...download import Downloader
from ...exception import ParseException
from ..base import BaseParser, handle
from .content import (
    blocks_to_contents,
    clean_text,
    looks_like_video_url,
    normalize_url,
    parse_rich_content,
)
from .models import (
    ANSWER_DECODER,
    ARTICLE_DECODER,
    COMMENT_DECODER,
    QUESTION_DECODER,
    Answer,
    Article,
    PlaylistItem,
    Question,
    RootComment,
)
from .request import ZhihuClient, initial_entities

_ZHIHU_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _count(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if abs(number) >= 100_000_000:
        text = f"{number / 100_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}亿"
    if abs(number) >= 10_000:
        text = f"{number / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}万"
    return str(number)


def _timestamp(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number // 1000 if number >= 10**12 else number


def _metrics(*items: tuple[str | None, Any]) -> list[Metric]:
    result: list[Metric] = []
    for label, value in items:
        formatted = _count(value)
        if label and formatted is not None:
            result.append(Metric(icon=None, label=label, value=formatted))
    return result


def _card_info(kind: str, *items: tuple[str | None, Any]) -> str:
    tokens = [kind]
    for label, value in items:
        count = _count(value)
        if count:
            tokens.append(f"{label} {count}")
    return " · ".join(tokens)


def _summary(*values: Any, limit: int = 80) -> str | None:
    for value in values:
        text = _plain_text(str(value or ""))
        text = clean_text(text, keep_newlines=False)
        if not text:
            continue
        if len(text) <= limit:
            return text
        return text[:limit].rstrip("，,；;。！？!?、") + "…"
    return None


def _plain_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.find_all(["script", "style", "noscript", "img", "video"]):
        node.decompose()
    return soup.get_text("\n", strip=False)


def _comment_images(content: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    urls: list[str] = []
    for anchor in soup.find_all("a", class_="comment_img"):
        href = anchor.get("href")
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            urls.append(href)
    return urls


class ZhihuParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="zhihu", display_name="知乎")
    HOMEPAGE: ClassVar[str] = "https://www.zhihu.com/"

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(_ZHIHU_HEADERS)
        self.cookiejar = CookieJar(config, self.mycfg, domain="zhihu.com")
        self.client = ZhihuClient(
            cookiejar=self.cookiejar,
            headers=self.headers,
            proxy=self.proxy,
            timeout=int(getattr(self.cfg, "common_timeout", 30) or 30),
            retries=int(getattr(self.cfg, "download_retry_times", 2) or 2),
        )

    async def close_session(self) -> None:
        await self.client.close()
        await super().close_session()

    @property
    def media_headers(self) -> dict[str, str]:
        headers = self.headers.copy()
        headers.pop("Cookie", None)
        headers.pop("cookie", None)
        headers["Referer"] = "https://www.zhihu.com/"
        return headers

    @handle(
        "zhuanlan.zhihu.com/p/",
        r"zhuanlan\.zhihu\.com/p/(?P<article_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_article(self, searched: re.Match[str]):
        return await self.parse_article(searched.group("article_id"))

    @handle(
        "/answer/",
        r"www\.zhihu\.com/question/(?P<question_id>\d+)/answer/(?P<answer_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_answer(self, searched: re.Match[str]):
        return await self.parse_answer(
            searched.group("question_id"),
            searched.group("answer_id"),
        )

    @handle(
        "www.zhihu.com/question/",
        r"www\.zhihu\.com/question/(?P<question_id>\d+)(?!\d)(?!/answer)(?:[/?#][^\s]*)?",
    )
    async def _parse_question(self, searched: re.Match[str]):
        return await self.parse_question(searched.group("question_id"))

    @handle(
        "www.zhihu.com/pin/",
        r"www\.zhihu\.com/pin/(?P<pin_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_pin(self, searched: re.Match[str]):
        return await self.parse_pin(searched.group("pin_id"))

    async def parse_article(self, article_id: str):
        url = f"https://zhuanlan.zhihu.com/p/{article_id}"
        article = await self._load_article(article_id)
        bodies, text = await self._content(article.content, url, "article")
        comments = await self._load_comments(f"articles/{article_id}", url)
        author = self._author(article.author)
        metrics = _metrics(
            ("赞同", article.reaction.statistics.up_vote_count),
            ("评论", article.reaction.statistics.comment_count),
            ("收藏", article.reaction.statistics.favorites),
        )
        contents = self._contents(
            bodies,
            header_text=self._header(
                title=None,
                author=author,
                timestamp=article.created,
                label="作者",
            ),
        )
        prefix = self._meta_header(
            title=article.title,
            kind="文章",
            column=(
                str(article.column.get("title") or "")
                if isinstance(article.column, dict)
                else ""
            ),
        )
        contents = _prepend_text(contents, prefix)
        await self.send_desc_common(
            article.title or "知乎文章",
            author,
            contents,
            text=_summary(article.excerpt, text),
            timestamp=_timestamp(article.created),
            url=url,
            extra={"info": _card_info("文章", *[(m.label, m.value) for m in metrics])},
            metrics=metrics,
            comments=comments,
        )
        return self.parse_result(contents)

    async def parse_answer(self, question_id: str, answer_id: str):
        url = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
        answer = await self._load_answer(answer_id)
        bodies, text = await self._content(answer.content, url, "answer")
        comments = await self._load_comments(f"answers/{answer_id}", url)
        author = self._author(answer.author)
        metrics = _metrics(
            ("赞同", answer.reaction.statistics.up_vote_count),
            ("评论", answer.reaction.statistics.comment_count),
            ("收藏", answer.reaction.statistics.favorites),
        )
        title = answer.question.title or "知乎回答"
        contents = self._contents(
            bodies,
            header_text=self._header(
                title=None,
                author=author,
                timestamp=answer.created_time,
                label="答主",
            ),
        )
        contents = _prepend_text(
            contents,
            self._meta_header(title=title, kind="回答"),
        )
        await self.send_desc_common(
            title,
            author,
            contents,
            text=_summary(answer.excerpt, text),
            timestamp=_timestamp(answer.created_time),
            url=url,
            extra={"info": _card_info("回答", *[(m.label, m.value) for m in metrics])},
            metrics=metrics,
            comments=comments,
        )
        return self.parse_result(contents)

    async def parse_question(self, question_id: str):
        url = f"https://www.zhihu.com/question/{question_id}"
        question = await self._load_question(question_id)
        bodies, text = await self._content(question.detail, url, "question")
        comments = await self._load_comments(f"questions/{question_id}", url)
        author = self._author(question.author)
        first_answer: Answer | None = None
        answer_blocks: list[dict[str, Any]] = []
        answer_text = ""
        if bool(getattr(self.mycfg, "question_first_answer", True)):
            first_answer = await self._first_answer(question_id)
            if first_answer is not None:
                answer_blocks, answer_text = await self._content(
                    first_answer.content,
                    f"https://www.zhihu.com/question/{question_id}/answer/{first_answer.id}",
                    "answer",
                )
        metrics = _metrics(
            ("回答", question.answer_count),
            ("关注", question.follower_count),
            ("浏览", question.visit_count),
        )
        contents = self._contents(
            [
                *bodies,
                *(
                    [{"kind": "text", "value": "默认排序首条回答:"}, *answer_blocks]
                    if first_answer is not None
                    else []
                ),
            ],
            header_text=self._header(
                title=None,
                author=author if first_answer is None else self._author(first_answer.author),
                timestamp=(
                    question.created
                    if first_answer is None
                    else first_answer.created_time
                ),
                label="提问者" if first_answer is None else "首条回答作者",
            ),
        )
        contents = _prepend_text(
            contents,
            self._meta_header(title=question.title, kind="问题"),
        )
        await self.send_desc_common(
            question.title or "知乎问题",
            author if first_answer is None else self._author(first_answer.author),
            contents,
            text=_summary(question.detail, question.excerpt, text, answer_text),
            timestamp=_timestamp(
                question.created
                if first_answer is None
                else first_answer.created_time
            ),
            url=url,
            extra={"info": _card_info("问题", *[(m.label, m.value) for m in metrics])},
            metrics=metrics,
            comments=comments,
        )
        return self.parse_result(contents)

    async def parse_pin(self, pin_id: str):
        url = f"https://www.zhihu.com/pin/{pin_id}"
        payload = await self.client.fetch_api(
            "https://www.zhihu.com/api/v4/pins/"
            f"{pin_id}?include=content,content_html,created_time,updated_time,"
            "author,origin_pin"
        )
        author_data = payload.get("author")
        author = self._author(author_data if isinstance(author_data, dict) else {})
        body_html = str(
            payload.get("content_html")
            or payload.get("contentHtml")
            or payload.get("content")
            or ""
        )
        bodies, text = await self._content(body_html, url, "pin")
        card_title = _summary(body_html, text) or "知乎想法"
        contents = self._contents(
            bodies,
            header_text=self._header(
                title=None,
                author=author,
                timestamp=payload.get("created_time") or payload.get("updated_time"),
                label="作者",
            ),
        )
        contents = _prepend_text(
            contents,
            self._meta_header(title=card_title, kind="想法"),
        )
        await self.send_desc_common(
            _summary(body_html, text) or "知乎想法",
            author,
            contents,
            text=_summary(body_html, text),
            timestamp=_timestamp(payload.get("created_time") or payload.get("updated_time")),
            url=url,
            extra={"info": _card_info("想法")},
            metrics=_metrics(
                ("赞同", payload.get("voteup_count") or payload.get("voteupCount")),
                ("评论", payload.get("comment_count") or payload.get("commentCount")),
            ),
        )
        return self.parse_result(contents)

    async def _load_article(self, article_id: str) -> Article:
        url = (
            f"https://www.zhihu.com/api/v4/articles/{article_id}?"
            "include=content,topics,paid_info,can_comment,excerpt,thanks_count,"
            "voteup_count,comment_count,visited_count,relationship,ip_info,"
            "relationship.vote,author.badge_v2"
        )
        try:
            return ARTICLE_DECODER.decode(
                await self._fetch_api_bytes(url)
            )
        except ParseException:
            initial, _ = await self.client.fetch_page(
                f"https://zhuanlan.zhihu.com/p/{article_id}",
                validator=lambda data: article_id
                in (initial_entities(data).get("articles") or {}),
            )
            article = (initial_entities(initial).get("articles") or {}).get(article_id)
            if not isinstance(article, dict):
                raise ParseException("知乎文章数据不存在")
            return _article_from_legacy(article)

    async def _load_answer(self, answer_id: str) -> Answer:
        url = (
            f"https://www.zhihu.com/api/v4/answers/{answer_id}?"
            "include=content,paid_info,can_comment,excerpt,thanks_count,"
            "voteup_count,comment_count,visited_count,attachment,reaction,ip_info,"
            "pagination_info,question.topics,reaction.relation.voting,author.badge_v2"
        )
        try:
            return ANSWER_DECODER.decode(await self._fetch_api_bytes(url))
        except ParseException:
            initial, _ = await self.client.fetch_page(
                f"https://www.zhihu.com/api/v4/answers/{answer_id}",
                validator=lambda data: answer_id
                in (initial_entities(data).get("answers") or {}),
            )
            answer = (initial_entities(initial).get("answers") or {}).get(answer_id)
            if not isinstance(answer, dict):
                raise ParseException("知乎回答数据不存在")
            return _answer_from_legacy(answer)

    async def _load_question(self, question_id: str) -> Question:
        url = (
            f"https://www.zhihu.com/api/v4/questions/{question_id}?"
            "include=read_count,visit_count,answer_count,voteup_count,"
            "comment_count,follower_count,detail,excerpt,author,"
            "relationship.is_following,topics"
        )
        try:
            return QUESTION_DECODER.decode(await self._fetch_api_bytes(url))
        except ParseException:
            initial, _ = await self.client.fetch_page(
                f"https://www.zhihu.com/question/{question_id}",
                validator=lambda data: self._has_question(data, question_id),
            )
            question = (initial_entities(initial).get("questions") or {}).get(question_id)
            if not isinstance(question, dict):
                raise ParseException("知乎问题数据不存在")
            return _question_from_legacy(question)

    async def _first_answer(self, question_id: str) -> Answer | None:
        try:
            initial, _ = await self.client.fetch_page(
                f"https://www.zhihu.com/question/{question_id}",
                validator=lambda data: self._pick_answer_id(data, question_id)
                is not None,
            )
            answer_id = self._pick_answer_id(initial, question_id)
            if answer_id is None:
                return None
            answer = (initial_entities(initial).get("answers") or {}).get(answer_id)
            if isinstance(answer, dict) and answer.get("content"):
                return _answer_from_legacy(answer)
            return await self._load_answer(answer_id)
        except ParseException:
            return None

    async def _load_comments(self, path: str, url: str) -> list[Comment]:
        if not bool(getattr(self.mycfg, "include_comments", True)):
            return []
        try:
            payload = await self.client.fetch_api(
                f"https://www.zhihu.com/api/v4/comment_v5/{path}/"
                "root_comment?order_by=score&limit=20"
            )
            page = COMMENT_DECODER.decode(self._json_bytes(payload))
        except Exception as exc:
            logger.warning(
                "[知乎] 评论获取失败 | error_type=%s",
                type(exc).__name__,
            )
            return []
        result: list[Comment] = []
        for item in page.data:
            result.append(
                Comment(
                    author=self._author(item.author),
                    contents=self._comment_contents(item.content),
                    timestamp=_timestamp(item.created_time),
                    location=self._comment_location(item),
                    like_count=_count(item.like_count),
                    reply_count=_count(item.child_comment_count),
                )
            )
        return result

    async def _fetch_api_bytes(self, url: str) -> bytes:
        payload = await self.client.fetch_api(url)
        return self._json_bytes(payload)

    @staticmethod
    def _json_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False).encode()

    @staticmethod
    def _has_question(initial: dict[str, Any], question_id: str) -> bool:
        question = (initial_entities(initial).get("questions") or {}).get(question_id)
        return isinstance(question, dict)

    @staticmethod
    def _pick_answer_id(initial: dict[str, Any], question_id: str) -> str | None:
        state = initial.get("initialState") or {}
        answers = ((state.get("question") or {}).get("answers") or {}).get(question_id)
        ids = answers.get("ids") if isinstance(answers, dict) else None
        if not isinstance(ids, list) or not ids or not isinstance(ids[0], dict):
            return None
        value = ids[0].get("target")
        return str(value) if value else None

    def _author(self, data: Any) -> Author:
        if isinstance(data, msgspec.Struct):
            data = msgspec.to_builtins(data)
        if not isinstance(data, dict):
            data = {}
        raw_name = clean_text(data.get("name"), keep_newlines=False)
        token = clean_text(data.get("url_token"), keep_newlines=False)
        uid = str(data.get("id") or "").strip()
        anonymous = bool(data.get("is_privacy")) or bool(data.get("is_anonymous"))
        if raw_name and raw_name != "知乎用户":
            name = raw_name
        elif raw_name:
            suffix = uid[-4:] if uid else token[-8:]
            name = f"匿名知乎用户 {suffix}" if suffix else "匿名知乎用户"
        elif token:
            name = f"知乎用户 {token}"
        else:
            name = "匿名知乎用户"
        avatar = normalize_url(data.get("avatar_url") or data.get("avatarUrl"))
        headline = clean_text(
            data.get("headline") or data.get("description"), keep_newlines=False
        )
        avatar_task = None
        if avatar:
            avatar_task = self.downloader.download_img(
                avatar,
                headers=self.media_headers,
                proxy=self.proxy,
            )
        return Author(
            name=name,
            avatar=avatar_task,
            description=headline or None,
            uid=uid or token or None,
        )

    async def _content(
        self,
        html_text: str,
        page_url: str,
        content_type: str,
    ) -> tuple[list[dict[str, Any]], str]:
        blocks, text = parse_rich_content(html_text, page_url, content_type)
        resolved: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("kind") != "video_placeholder":
                resolved.append(block)
                continue
            video = await self._resolve_video(
                str(block.get("value") or ""),
                str(block.get("content_type") or content_type),
            )
            if video is not None:
                resolved.append(
                    {
                        "kind": "video",
                        "url": video.url,
                        "cover_url": video.cover_url,
                        "duration": video.duration,
                        "quality": video.quality,
                    }
                )
        return resolved, text

    def _contents(
        self,
        blocks: list[dict[str, Any]],
        *,
        header_text: str,
    ) -> list[MediaContent]:
        prefix = [{"kind": "text", "value": header_text}] if header_text else []
        contents = blocks_to_contents(
            [*prefix, *blocks],
            image_loader=self._image_content,
        )
        videos = self._video_contents(blocks)
        contents.extend(videos)
        return self.parse_result(contents)

    def _image_content(self, url: str) -> ImageContent:
        content = ImageContent()

        async def load() -> Any:
            return await self.downloader.download_img(
                url,
                headers=self.media_headers,
                proxy=self.proxy,
            )

        content.set_loader(load)
        return content

    def _video_contents(
        self,
        blocks: list[dict[str, Any]],
    ) -> list[VideoContent]:
        result: list[VideoContent] = []
        seen: set[str] = set()
        for block in blocks:
            if block.get("kind") != "video":
                continue
            url = str(block.get("url") or "")
            if not url or url in seen or not looks_like_video_url(url):
                continue
            seen.add(url)
            result.append(
                self.create_video_content(
                    url,
                    block.get("cover_url"),
                    float(block.get("duration") or 0),
                    headers=self.media_headers,
                )
            )
        return result

    async def _resolve_video(
        self,
        video_id: str,
        content_type: str,
    ) -> PlaylistItem | None:
        if not video_id:
            return None
        try:
            payload = await self.client.fetch_api(
                "https://www.zhihu.com/api/v4/video/play_info",
                method="POST",
                json_body={
                    "content_id": video_id,
                    "video_id": video_id,
                    "content_type_str": content_type,
                    "is_only_video": True,
                    "scene_code": "answer_detail_web",
                },
            )
        except ParseException:
            return None
        playlist = (
            (payload.get("video_play") or {}).get("playlist") or {}
        ) if isinstance(payload, dict) else {}
        mp4 = playlist.get("mp4") if isinstance(playlist, dict) else None
        if not isinstance(mp4, list):
            return None
        preference = str(getattr(self.mycfg, "video_quality", "auto") or "auto")
        rank = {"FHD": 3, "HD": 2, "SD": 1}
        candidates = [
            item
            for item in mp4
            if isinstance(item, dict)
            and isinstance(item.get("url"), list)
            and item["url"]
            and isinstance(item["url"][0], str)
        ]
        if not candidates:
            return None
        if preference == "auto":
            candidates.sort(
                key=lambda item: rank.get(str(item.get("quality") or "").upper(), 0),
                reverse=True,
            )
        else:
            candidates.sort(
                key=lambda item: str(item.get("quality") or "").upper()
                != preference.upper()
            )
        item = candidates[0]
        return PlaylistItem(
            quality=str(item.get("quality") or ""),
            url=[str(item["url"][0])],
            duration=float(item.get("duration") or 0),
        )

    def _comment_contents(self, html_text: str) -> list[ContentItem]:
        contents: list[ContentItem] = []
        text = _plain_text(html_text)
        text = clean_text(text, keep_newlines=True)
        if text:
            contents.append(TextContent(text))
        for url in _comment_images(html_text):
            contents.append(self._image_content(url))
        return contents

    @staticmethod
    def _comment_location(comment: RootComment) -> str | None:
        for tag in comment.comment_tag:
            if tag.type == "ip_info" and tag.text:
                return tag.text
        return None

    @staticmethod
    def _header(
        *,
        title: str | None,
        author: Author | None,
        timestamp: Any,
        label: str,
    ) -> str:
        sections: list[str] = []
        if title:
            sections.append(f"标题: {clean_text(title, keep_newlines=False)}")
        if author:
            sections.append(f"{label}: {author.name}")
        stamp = _timestamp(timestamp)
        if stamp:
            from datetime import datetime

            sections.append(
                datetime.fromtimestamp(stamp).strftime("发布时间: %Y-%m-%d %H:%M:%S")
            )
        return "\n\n".join(sections)

    @staticmethod
    def _meta_header(
        *,
        title: str | None,
        kind: str,
        column: str = "",
    ) -> str:
        tokens = [kind]
        if column:
            tokens.append(column)
        text = " · ".join(token for token in tokens if token)
        if title:
            return f"标题: {clean_text(title, keep_newlines=False)}\n\n{text}"
        return text


def _article_from_legacy(data: dict[str, Any]) -> Article:
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    stats = data.get("reaction") if isinstance(data.get("reaction"), dict) else {}
    stats = stats.get("statistics") if isinstance(stats, dict) else {}
    return Article(
        id=str(data.get("id") or ""),
        title=str(data.get("title") or ""),
        content=str(data.get("content") or ""),
        excerpt=str(data.get("excerpt") or ""),
        created=int(data.get("created") or 0),
        updated=int(data.get("updated") or 0),
        ip_info=data.get("ip_info"),
        author=Author(
            id=str(author.get("id") or ""),
            url_token=str(author.get("url_token") or author.get("urlToken") or ""),
            name=str(author.get("name") or ""),
            avatar_url=str(author.get("avatar_url") or author.get("avatarUrl") or ""),
            headline=str(author.get("headline") or ""),
        ),
        reaction={
            "statistics": {
                "comment_count": int(stats.get("comment_count") or data.get("commentCount") or 0),
                "down_vote_count": int(stats.get("down_vote_count") or data.get("downVoteCount") or 0),
                "favorites": int(stats.get("favorites") or data.get("favlistsCount") or data.get("favoriteCount") or 0),
                "like_count": int(stats.get("like_count") or data.get("likedCount") or 0),
                "up_vote_count": int(stats.get("up_vote_count") or data.get("voteupCount") or 0),
            }
        },
        content_need_truncated=bool(data.get("contentNeedTruncated")),
        force_login_when_click_read_more=bool(data.get("forceLoginWhenClickReadMore")),
        column=data.get("column") if isinstance(data.get("column"), dict) else None,
    )


def _answer_from_legacy(data: dict[str, Any]) -> Answer:
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    question = data.get("question") if isinstance(data.get("question"), dict) else {}
    reaction = data.get("reaction") if isinstance(data.get("reaction"), dict) else {}
    stats = reaction.get("statistics") if isinstance(reaction, dict) else {}
    return Answer(
        id=str(data.get("id") or ""),
        content=str(data.get("content") or ""),
        excerpt=str(data.get("excerpt") or ""),
        created_time=int(data.get("createdTime") or data.get("created_time") or 0),
        updated_time=int(data.get("updatedTime") or data.get("updated_time") or 0),
        comment_count=int(data.get("commentCount") or data.get("comment_count") or 0),
        voteup_count=int(data.get("voteupCount") or data.get("voteup_count") or 0),
        thanks_count=int(data.get("thanksCount") or data.get("thanks_count") or 0),
        ip_info=data.get("ipInfo") or data.get("ip_info"),
        author=Author(
            id=str(author.get("id") or ""),
            url_token=str(author.get("url_token") or author.get("urlToken") or ""),
            name=str(author.get("name") or ""),
            avatar_url=str(author.get("avatar_url") or author.get("avatarUrl") or ""),
            headline=str(author.get("headline") or ""),
        ),
        question={
            "id": str(question.get("id") or ""),
            "title": str(question.get("title") or ""),
            "created": int(question.get("created") or 0),
            "updated_time": int(question.get("updated_time") or question.get("updatedTime") or 0),
        },
        reaction={
            "statistics": {
                "comment_count": int(stats.get("comment_count") or 0),
                "down_vote_count": int(stats.get("down_vote_count") or 0),
                "favorites": int(stats.get("favorites") or 0),
                "like_count": int(stats.get("like_count") or 0),
                "up_vote_count": int(stats.get("up_vote_count") or 0),
            }
        },
        content_need_truncated=bool(data.get("contentNeedTruncated")),
        force_login_when_click_read_more=bool(data.get("forceLoginWhenClickReadMore")),
    )


def _question_from_legacy(data: dict[str, Any]) -> Question:
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    return Question(
        id=str(data.get("id") or ""),
        title=str(data.get("title") or ""),
        created=int(data.get("created") or 0),
        updated_time=int(data.get("updated_time") or data.get("updatedTime") or 0),
        detail=str(data.get("detail") or ""),
        excerpt=str(data.get("excerpt") or ""),
        answer_count=int(data.get("answerCount") or data.get("answer_count") or 0),
        visit_count=int(data.get("visitCount") or data.get("visit_count") or 0),
        comment_count=int(data.get("commentCount") or data.get("comment_count") or 0),
        follower_count=int(data.get("followerCount") or data.get("follower_count") or 0),
        voteup_count=int(data.get("voteupCount") or data.get("voteup_count") or 0),
        author=Author(
            id=str(author.get("id") or ""),
            url_token=str(author.get("url_token") or author.get("urlToken") or ""),
            name=str(author.get("name") or ""),
            avatar_url=str(author.get("avatar_url") or author.get("avatarUrl") or ""),
            headline=str(author.get("headline") or ""),
        ),
    )


def _prepend_text(
    contents: list[MediaContent],
    text: str | None,
) -> list[MediaContent]:
    value = clean_text(text or "")
    if not value:
        return contents
    return [TextContent(value), *contents]
