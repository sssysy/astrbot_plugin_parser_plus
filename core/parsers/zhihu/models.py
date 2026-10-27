from __future__ import annotations

from typing import Any

import msgspec


class Author(msgspec.Struct, kw_only=True):
    id: str = ""
    url_token: str = ""
    name: str = ""
    avatar_url: str = ""
    headline: str = ""
    is_privacy: bool | None = None
    is_anonymous: bool | None = None


class QuestionSummary(msgspec.Struct, kw_only=True):
    id: str = ""
    title: str = ""
    created: int = 0
    updated_time: int = 0
    detail: str = ""
    excerpt: str = ""


class Statistics(msgspec.Struct, kw_only=True):
    comment_count: int = 0
    down_vote_count: int = 0
    favorites: int = 0
    like_count: int = 0
    up_vote_count: int = 0


class Reaction(msgspec.Struct, kw_only=True):
    statistics: Statistics = msgspec.field(default_factory=Statistics)


class Article(msgspec.Struct, kw_only=True):
    id: str
    title: str = ""
    content: str = ""
    excerpt: str = ""
    created: int = 0
    updated: int = 0
    ip_info: str | None = None
    author: Author = msgspec.field(default_factory=Author)
    reaction: Reaction = msgspec.field(default_factory=Reaction)
    type: str = "article"
    content_need_truncated: bool = False
    force_login_when_click_read_more: bool = False
    column: dict[str, Any] | None = None


class Answer(msgspec.Struct, kw_only=True):
    id: str
    content: str = ""
    excerpt: str = ""
    created_time: int = 0
    updated_time: int = 0
    comment_count: int = 0
    voteup_count: int = 0
    thanks_count: int = 0
    ip_info: str | None = None
    author: Author = msgspec.field(default_factory=Author)
    question: QuestionSummary = msgspec.field(default_factory=QuestionSummary)
    reaction: Reaction = msgspec.field(default_factory=Reaction)
    content_need_truncated: bool = False
    force_login_when_click_read_more: bool = False


class Question(msgspec.Struct, kw_only=True):
    id: str
    title: str = ""
    created: int = 0
    updated_time: int = 0
    detail: str = ""
    excerpt: str = ""
    answer_count: int = 0
    visit_count: int = 0
    comment_count: int = 0
    follower_count: int = 0
    voteup_count: int = 0
    author: Author = msgspec.field(default_factory=Author)


class CommentTag(msgspec.Struct, kw_only=True):
    type: str = ""
    text: str = ""


class RootComment(msgspec.Struct, kw_only=True):
    id: str
    content: str = ""
    created_time: int = 0
    like_count: int = 0
    child_comment_count: int = 0
    author: Author = msgspec.field(default_factory=Author)
    comment_tag: list[CommentTag] = msgspec.field(default_factory=list)


class RootCommentPage(msgspec.Struct, kw_only=True):
    data: list[RootComment] = msgspec.field(default_factory=list)


class PlaylistItem(msgspec.Struct, kw_only=True):
    quality: str = ""
    url: list[str] = msgspec.field(default_factory=list)
    duration: float = 0


class VideoPlaylist(msgspec.Struct, kw_only=True):
    mp4: list[PlaylistItem] = msgspec.field(default_factory=list)


class VideoPlay(msgspec.Struct, kw_only=True):
    default_cover: str = ""
    playlist: VideoPlaylist = msgspec.field(default_factory=VideoPlaylist)


class VideoPlayInfo(msgspec.Struct, kw_only=True):
    video_play: VideoPlay = msgspec.field(default_factory=VideoPlay)


class VideoEntry(msgspec.Struct, kw_only=True):
    url: str
    cover_url: str | None = None
    duration: float = 0
    quality: str | None = None


ARTICLE_DECODER = msgspec.json.Decoder(Article)
ANSWER_DECODER = msgspec.json.Decoder(Answer)
QUESTION_DECODER = msgspec.json.Decoder(Question)
COMMENT_DECODER = msgspec.json.Decoder(RootCommentPage)
VIDEO_DECODER = msgspec.json.Decoder(VideoPlayInfo)
