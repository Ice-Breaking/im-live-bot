"""会话规则判定：只回答「这条消息该做什么」。

不执行、不记日志、不碰 IO，因此判定矩阵可以单独测试。
判定顺序与 Gateway 原来的分支顺序一致，改顺序即改行为。
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

HELP_COMMANDS = ("/help", "help", "帮助")


class Decision(Enum):
    """判定结果：只描述该做什么，由 Gateway 执行。"""

    ANSWER = "answer"
    REJECT = "reject"
    IGNORE_GROUP = "ignore_group"
    HELP = "help"
    SKIP_DUPLICATE = "skip_duplicate"
    REUSE = "reuse"
    NO_CHANNEL = "no_channel"


class MessageLike(Protocol):
    """判定只用这几个字段，不必依赖 IncomingMessage。"""

    chat_type: str
    text: str
    mentioned_bot: bool


def decide(
    msg: MessageLike,
    *,
    allowed: bool,
    channel_registered: bool,
    seen: bool,
    has_result: bool,
) -> Decision:
    """按顺序判定：渠道未注册 → 越权 → 群聊未 @ → 帮助 → 重复投递 → 正常提问。"""
    if not channel_registered:
        return Decision.NO_CHANNEL
    if not allowed:
        return Decision.REJECT
    if msg.chat_type == "group" and not msg.mentioned_bot:
        return Decision.IGNORE_GROUP
    if msg.text.strip() in HELP_COMMANDS:
        return Decision.HELP
    if seen:
        return Decision.REUSE if has_result else Decision.SKIP_DUPLICATE
    return Decision.ANSWER
