from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramButton:
    title: str
    url: str | None


@dataclass(frozen=True, slots=True)
class TelegramPollOption:
    name: str
    percent: int


@dataclass(frozen=True, slots=True)
class TelegramPoll:
    question: str
    kind: str | None
    votes: str | None
    options: tuple[TelegramPollOption, ...]


@dataclass(frozen=True, slots=True)
class TelegramPreview:
    url: str | None
    title: str | None
    site_name: str | None
    description: str | None
    thumb: str | None


@dataclass(frozen=True, slots=True)
class TelegramReply:
    name: str
    text: str
    url: str | None
    to_message: int | None
    thumb: str | None


@dataclass(frozen=True, slots=True)
class TelegramForward:
    name: str
    url: str | None
    author: str | None


@dataclass(frozen=True, slots=True)
class TelegramReaction:
    label: str
    count: str
    kind: str


@dataclass(frozen=True, slots=True)
class TelegramMedia:
    kind: str
    url: str | None = None
    thumb: str | None = None
    duration: float = 0.0
    name: str | None = None
    available: bool = True

    @property
    def sendable(self) -> bool:
        return bool(self.url) and self.available


@dataclass(slots=True)
class TelegramPost:
    channel: str
    post_id: int
    channel_title: str
    channel_avatar: str | None
    channel_description: str | None
    author: str
    text: str | None
    timestamp: int | None
    views: str | None
    edited: bool
    media: tuple[TelegramMedia, ...]
    preview: TelegramPreview | None
    reply: TelegramReply | None
    forwarded: TelegramForward | None
    poll: TelegramPoll | None
    buttons: tuple[TelegramButton, ...]
    reactions: tuple[TelegramReaction, ...]
    unsupported_media: bool

    @property
    def sendable_media(self) -> tuple[TelegramMedia, ...]:
        return tuple(item for item in self.media if item.sendable)

    @property
    def display_author(self) -> str:
        return self.author or self.channel_title or self.channel
