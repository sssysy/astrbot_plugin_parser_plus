from msgspec import Struct, field


class UrlList(Struct):
    url_list: list[str] = field(default_factory=list)


class Owner(Struct):
    id_str: str = ""
    nickname: str = ""
    avatar_thumb: UrlList = field(default_factory=UrlList)


class RoomViewStats(Struct):
    display_value: int | str = 0


class Room(Struct):
    id_str: str = ""
    status: int = 0
    title: str = ""
    cover: UrlList = field(default_factory=UrlList)
    owner: Owner = field(default_factory=Owner)
    room_view_stats: RoomViewStats = field(default_factory=RoomViewStats)
    like_count: int | str = 0
