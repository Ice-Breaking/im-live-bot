"""出站发送：按渠道声明的单条长度分段，失败按固定间隔重试。

只负责发送，不记日志、不碰任务记录：重试过程通过 on_retry 告诉调用方，
结果用 SendOutcome 返回，怎么记由调用方决定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from channel_contract import SendError

# 发送失败后的重试：平台偶发抖动能自愈，连续失败才放弃并保留结果
SEND_MAX_TRIES = 3
SEND_RETRY_INTERVAL = 3.0


class ReplyChannel(Protocol):
    """发送只需要渠道的这两项，不必依赖 Gateway。"""

    max_segment_chars: int

    def reply(self, target: str, text: str) -> None: ...


@dataclass(frozen=True)
class SendOutcome:
    """一次发送的结果。"""

    ok: bool
    total_segments: int
    failed_segment: int
    """失败发生在第几段，1 起；0 表示没失败。"""
    reason: str


def send_text(
    channel: ReplyChannel,
    message_id: str,
    text: str,
    *,
    label: str = "",
    max_tries: int = SEND_MAX_TRIES,
    interval: float = SEND_RETRY_INTERVAL,
    sleep: Optional[Callable[[float], None]] = None,
    on_retry: Optional[Callable[[str, int, Exception], None]] = None,
) -> SendOutcome:
    """发一条文本：失败重试，成功即返回。"""
    wait = sleep or time.sleep
    reason = ""
    for attempt in range(1, max_tries + 1):
        try:
            channel.reply(message_id, text)
            return SendOutcome(True, 1, 0, "")
        except SendError as exc:
            reason = str(exc)
            if on_retry is not None:
                on_retry(label, attempt, exc)
            if attempt < max_tries:
                wait(interval)
    return SendOutcome(False, 1, 1, reason)


def send_answer(
    channel: ReplyChannel,
    message_id: str,
    answer: str,
    *,
    max_chars: int,
    max_tries: int = SEND_MAX_TRIES,
    interval: float = SEND_RETRY_INTERVAL,
    sleep: Optional[Callable[[float], None]] = None,
    on_retry: Optional[Callable[[str, int, Exception], None]] = None,
) -> SendOutcome:
    """按渠道声明的单条长度分段发送，保证内容完整送达；任一段失败即停止。"""
    content = answer[:max_chars]
    size = channel.max_segment_chars
    segments = [content[index : index + size] for index in range(0, len(content), size)]
    total = len(segments)
    for position, segment in enumerate(segments, start=1):
        title = f"【回答 {position}/{total}】" if total > 1 else ""
        label = f"第 {position}/{total} 段"
        outcome = send_text(
            channel,
            message_id,
            f"{title}\n{segment}".strip(),
            label=label,
            max_tries=max_tries,
            interval=interval,
            sleep=sleep,
            on_retry=on_retry,
        )
        if not outcome.ok:
            return SendOutcome(False, total, position, outcome.reason)
    return SendOutcome(True, total, 0, "")
