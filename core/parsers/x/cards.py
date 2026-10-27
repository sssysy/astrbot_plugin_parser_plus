from __future__ import annotations

from .models import XLinkCard, XPoll


def poll_text(poll: XPoll) -> str:
    total = poll.total_votes
    lines = ["投票"]
    for choice in poll.choices:
        percentage = choice.votes / total * 100 if total else 0
        lines.append(f"{choice.label}: {choice.votes} 票 ({percentage:.0f}%)")
    if total:
        lines.append(f"共 {total} 票")
    if poll.closed:
        lines.append("已结束")
    elif poll.close_at:
        lines.append(f"截止 {poll.close_at}")
    return "\n".join(lines)


def link_text(card: XLinkCard) -> str:
    lines = [f"链接卡片：{card.title}"]
    if card.site_name:
        lines.append(f"站点：{card.site_name}")
    if card.description:
        lines.append(card.description)
    lines.append(card.url)
    return "\n".join(lines)
