from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any

from ...exception import ParseException
from .content import clean_text, is_video_url, normalize_media_url


@dataclass(frozen=True, slots=True)
class GameContent:
    title: str
    description: str
    image_urls: list[str]
    video_urls: list[tuple[str, str | None]]
    appid: str
    steam_appid: int | None
    game_type: str


def canonical_game_url(appid: str, game_type: str) -> str:
    kind = game_type.strip().lower() or "pc"
    return f"https://www.xiaoheihe.cn/app/topic/game/{kind}/{appid}"


def extract_nuxt_payload(html_text: str) -> list[Any]:
    matched = re.search(
        r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>',
        html_text,
        re.S | re.I,
    )
    if not matched:
        raise ParseException("小黑盒游戏页未找到详情数据")
    try:
        payload = json.loads(matched.group(1).strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise ParseException("小黑盒游戏页数据格式异常") from exc
    if not isinstance(payload, list):
        raise ParseException("小黑盒游戏页数据格式异常")
    return payload


def resolve_devalue(payload: list[Any]) -> Any:
    total = len(payload)
    memo: dict[int, Any] = {}
    resolving: set[int] = set()

    def resolve(value: Any) -> Any:
        if isinstance(value, int) and 0 <= value < total:
            return resolve_index(value)
        if isinstance(value, list):
            if (
                len(value) == 2
                and isinstance(value[0], str)
                and value[0]
                in {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "Readonly", "ShallowReadonly"}
            ):
                return resolve(value[1])
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        return value

    def resolve_index(index: int) -> Any:
        if index in memo:
            return memo[index]
        if index in resolving:
            return None
        resolving.add(index)
        memo[index] = None
        memo[index] = resolve(payload[index])
        resolving.remove(index)
        return memo[index]

    return resolve(0)


def find_game(root: Any, appid: str) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_score = 0
    stack = [root]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            score = sum(
                3
                for key in (
                    "about_the_game",
                    "name",
                    "name_en",
                    "price",
                    "heybox_price",
                    "user_num",
                    "game_award",
                    "comment_stats",
                    "screenshots",
                    "share_url",
                    "share_title",
                )
                if key in current
            )
            if str(current.get("appid") or "") == appid:
                score += 50
            if str(current.get("steam_appid") or "") == appid:
                score += 50
            if appid and appid in str(current.get("share_url") or ""):
                score += 20
            if str(current.get("type") or "").lower() == "game":
                score += 5
            if score > best_score and score >= 12:
                best, best_score = current, score
            stack.extend(value for value in current.values() if isinstance(value, (dict, list)))
        elif isinstance(current, list):
            stack.extend(value for value in current if isinstance(value, (dict, list)))
    if best is None:
        raise ParseException("小黑盒游戏页未找到游戏数据")
    return best


def build_game_content(
    html_text: str,
    appid: str,
    game_type: str,
    intro: dict[str, Any],
    video_urls: list[tuple[str, str | None]],
) -> GameContent:
    game = find_game(resolve_devalue(extract_nuxt_payload(html_text)), appid)
    steam_appid = _steam_appid(game, appid)
    title = _game_title(game)
    description = _game_description(html_text, game, intro)
    images = _game_images(game, html_text)
    return GameContent(
        title=title,
        description=description,
        image_urls=images,
        video_urls=video_urls,
        appid=appid,
        steam_appid=steam_appid,
        game_type=game_type,
    )


def build_game_content_from_info(
    game: dict[str, Any],
    appid: str,
    game_type: str,
    intro: dict[str, Any],
    video_urls: list[tuple[str, str | None]],
) -> GameContent:
    title = _game_title(game)
    description = _game_description("", game, intro)
    images = _game_images_from_info(game)
    steam_appid = _steam_appid(game, appid)
    return GameContent(
        title=title,
        description=description,
        image_urls=images,
        video_urls=video_urls,
        appid=appid,
        steam_appid=steam_appid,
        game_type=game_type,
    )


def _steam_appid(game: dict[str, Any], fallback: str) -> int | None:
    try:
        return int(str(game.get("steam_appid") or fallback))
    except (TypeError, ValueError):
        return None


def _game_title(game: dict[str, Any]) -> str:
    name = str(game.get("name") or "").strip()
    name_en = str(game.get("name_en") or "").strip()
    if name and name_en:
        return f"{name}（{name_en}）"
    return name or name_en or "小黑盒游戏详情"


def _game_description(html_text: str, game: dict[str, Any], intro: dict[str, Any]) -> str:
    lines: list[str] = []
    about = _strip_tags(str(intro.get("about_the_game") or ""))
    if about:
        lines.append(about)
    tags = _game_tags(html_text)
    if tags:
        lines.append(f"类型：{tags}")
    score = str(game.get("score") or "").strip()
    if score:
        stats = game.get("comment_stats")
        count = stats.get("score_comment") if isinstance(stats, dict) else None
        if isinstance(count, int) and count > 0:
            lines.append(f"小黑盒评分：{score}（{_people(count)}）")
        else:
            lines.append(f"小黑盒评分：{score}")
    release = _date(str(intro.get("release_date") or ""))
    if release:
        lines.append(f"发布时间：{release}")
    developers = _company(intro.get("developers"))
    if developers:
        lines.append(f"开发商：{developers}")
    publishers = _company(intro.get("publishers"))
    if publishers:
        lines.append(f"发行商：{publishers}")
    price = game.get("price")
    if isinstance(price, dict):
        initial = str(price.get("initial") or price.get("current") or "").strip()
        if initial:
            lines.append(f"价格：¥ {initial.replace('¥', '').strip()}")
        lowest = str(price.get("lowest_price") or "").strip()
        if lowest:
            lines.append(f"史低价格：¥ {lowest.replace('¥', '').strip()}")
    heybox_price = game.get("heybox_price")
    if isinstance(heybox_price, dict):
        coin = heybox_price.get("cost_coin")
        try:
            yuan = int(coin) / 1000
        except (TypeError, ValueError):
            yuan = 0
        if yuan:
            lines.append(f"当前价格：¥ {yuan:g}")
    return "\n\n".join(lines).strip()


def _game_images(game: dict[str, Any], html_text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def push(value: object) -> None:
        if not isinstance(value, str):
            return
        image = normalize_media_url(value)
        lower = image.lower()
        if not image or "/thumbnail/" in lower:
            return
        if not any(token in lower for token in ("gameimg", "steam_item_assets", "screenshot")):
            return
        key = image.split("?", 1)[0]
        if key in seen:
            return
        seen.add(key)
        result.append(image)

    for key in ("screenshots", "screenshot_list", "screen_shot", "screen_shots", "images", "image_list", "game_imgs"):
        value = game.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                for nested in ("url", "image", "img", "src"):
                    push(item.get(nested))
            else:
                push(item)
    for key in ("header_img", "cover", "cover_img", "poster", "share_img"):
        push(game.get(key))
    if result:
        return result
    for value in re.findall(
        r'https?://[^"\'\s<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\s<>]*)?',
        html_text,
        re.I,
    ):
        push(value)
    return result


def _game_images_from_info(game: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in (
        game.get("image"),
        game.get("icon"),
        game.get("appicon"),
    ):
        image = normalize_media_url(str(value or ""))
        if not image:
            continue
        key = image.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        result.append(image)
    return result


def _game_tags(html_text: str) -> str:
    matched = re.search(r'<div class="row-2">.*?<div class="tags">(.*?)</div></div>', html_text, re.S | re.I)
    source = matched.group(1) if matched else ""
    output: list[str] = []
    common = re.search(r'<div class="tag common"[^>]*>(.*?)</div>', source, re.S | re.I)
    if common:
        tokens = [
            re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", _strip_tags(item))
            for item in re.findall(r"<span[^>]*>(.*?)</span>", common.group(1), re.S | re.I)
        ]
        tokens = [token for token in tokens if token]
        if tokens:
            output.append(f"[ {' '.join(tokens)} ]")
    tags = [_strip_tags(item) for item in re.findall(r'<p class="tag"[^>]*>(.*?)</p>', source, re.S | re.I)]
    tags = [tag for tag in tags if tag]
    if tags:
        output.append(f"[ {' '.join(tags)} ]")
    return " ".join(output)


def _company(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return ",".join(
        str(item["value"])
        for item in items
        if isinstance(item, dict) and item.get("value")
    )


def _people(count: int) -> str:
    return f"{count / 10000:.1f} 万人评价" if count >= 10000 else f"{count} 人评价"


def _date(value: str) -> str:
    value = html.unescape(value).strip()
    matched = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$", value)
    if matched:
        return f"{matched.group(1)}.{int(matched.group(2))}.{int(matched.group(3))}"
    matched = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", value)
    if matched:
        return f"{matched.group(1)}.{int(matched.group(2))}.{int(matched.group(3))}"
    return value


def _strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script[^>]*>.*?</script>", "", value)
    value = re.sub(r"(?is)<style[^>]*>.*?</style>", "", value)
    value = re.sub(r"(?is)<video[^>]*>.*?</video>", "", value)
    value = re.sub(r"(?is)<img[^>]*>", "", value)
    value = re.sub(r"(?i)</p\s*>", "\n\n", value)
    value = re.sub(r"(?i)<p[^>]*>", "", value)
    value = re.sub(r"(?i)</li\s*>", "\n", value)
    value = re.sub(r"(?i)<li[^>]*>", "\n・", value)
    value = re.sub(r"(?i)</h[1-6]\s*>", "\n", value)
    value = re.sub(r"(?i)<h[1-6][^>]*>", "\n", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    return clean_text(html.unescape(value))
