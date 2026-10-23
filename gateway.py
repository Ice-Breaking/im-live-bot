"""Gateway 会话规则：谁能用、哪些消息要处理、答案怎么回。

收到消息后在后台线程调用模型，答案按原消息回发。
换 IM 平台时不改这里，只替换平台适配器。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Protocol

from chat_agent import ChatAgent, ChatInputError
from llm_client import ModelCallError

logger = logging.getLogger(__name__)

# 飞书单条文本消息有长度上限，超长答案先截断再分段
MAX_ANSWER_CHARS = 20000
MAX_SEGMENT_CHARS = 3000

# 记住最近处理过的消息编号条数：平台可能重复投递同一条事件
DEDUP_CACHE_SIZE = 200

HELP_TEXT = """飞书机器人用法：

1) 直接提问
   把问题原样发给我，我会调用模型回答。

2) 查看本帮助
   /help

群聊里请 @ 机器人 再发送内容。"""


@dataclass(frozen=True)
class IncomingMessage:
    """平台适配器交给 Gateway 的归一化消息。"""

    channel: str
    user_open_id: str
    chat_id: str
    chat_type: str
    message_id: str
    text: str
    mentioned_bot: bool


class OutboundChannel(Protocol):
    """平台适配器需要具备的出站能力。"""

    def reply(self, message_id: str, text: str) -> None:
        """按原消息编号回复。"""


class Gateway:
    """消息进入与离开服务的公共入口。"""

    def __init__(self, agent: ChatAgent, allowed_open_ids: Iterable[str]) -> None:
        self.agent = agent
        # 出站通道由装配阶段注入，见 attach_outbound
        self.outbound: OutboundChannel
        self.allowed_open_ids = frozenset(allowed_open_ids)
        self._seen_message_ids: deque[str] = deque(maxlen=DEDUP_CACHE_SIZE)

    def attach_outbound(self, outbound: OutboundChannel) -> None:
        """绑定出站通道：适配器与网关互相依赖，装配时后注入。"""
        self.outbound = outbound

    def handle(self, msg: IncomingMessage) -> None:
        """处理一条入站消息：鉴权、去重，然后交给后台线程回答。"""
        logger.info(
            "收到消息: channel=%s chat_type=%s chat_id=%s message_id=%s sender=%s 文本长度=%d",
            msg.channel,
            msg.chat_type,
            msg.chat_id,
            msg.message_id,
            msg.user_open_id,
            len(msg.text),
        )

        if msg.user_open_id not in self.allowed_open_ids:
            logger.warning("拒绝未授权发送人: sender=%s", msg.user_open_id)
            self.outbound.reply(
                msg.message_id,
                f"当前账号不在允许名单内，把 open_id（{msg.user_open_id}）"
                "加入 .env 的 ALLOWED_OPEN_IDS 后重启服务即可使用。",
            )
            return

        if msg.chat_type == "group" and not msg.mentioned_bot:
            logger.info("群聊未 @ 机器人，忽略: chat_id=%s", msg.chat_id)
            return

        text = msg.text.strip()
        if text in ("/help", "help", "帮助"):
            self.outbound.reply(msg.message_id, HELP_TEXT)
            return

        if msg.message_id in self._seen_message_ids:
            logger.info("重复投递，忽略: message_id=%s", msg.message_id)
            return
        self._seen_message_ids.append(msg.message_id)

        # 模型调用耗时不可控，放到后台线程，避免占用长连接的事件回调线程
        threading.Thread(
            target=self._answer_and_reply,
            args=(msg, text),
            name="answer",
            daemon=True,
        ).start()

    def _answer_and_reply(self, msg: IncomingMessage, question: str) -> None:
        """后台线程：调用模型，把答案或错误提示回复到原消息。"""
        try:
            answer = self.agent.answer(question)
        except ChatInputError as exc:
            logger.info("输入不合法: message_id=%s code=%s", msg.message_id, exc.code)
            self.outbound.reply(msg.message_id, exc.message)
            return
        except ModelCallError as exc:
            logger.error("模型调用失败: message_id=%s 原因=%s", msg.message_id, exc)
            self.outbound.reply(msg.message_id, f"模型调用失败（MODEL_ERROR）：{exc}")
            return
        self._reply_in_segments(msg.message_id, answer)

    def _reply_in_segments(self, message_id: str, answer: str) -> None:
        """答案超长时按固定长度分段回复，保证用户能收到完整内容。"""
        content = answer[:MAX_ANSWER_CHARS]
        segments = [
            content[index : index + MAX_SEGMENT_CHARS]
            for index in range(0, len(content), MAX_SEGMENT_CHARS)
        ]
        total = len(segments)
        for position, segment in enumerate(segments, start=1):
            title = f"【回答 {position}/{total}】" if total > 1 else ""
            self.outbound.reply(message_id, f"{title}\n{segment}".strip())
        logger.info("答案已回发: message_id=%s 分段=%d", message_id, total)
