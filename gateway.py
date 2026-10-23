"""Gateway 会话规则：谁能用、哪些消息要处理、答案怎么回。

收到消息后在后台线程调用模型，答案按原消息回发。
渠道插件只负责连接、消息格式与结果展示，新增入口不改这里。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from chat_agent import ChatAgent, ChatInputError
from llm_client import ModelCallError

logger = logging.getLogger(__name__)

# 答案总长度上限，与具体渠道无关；分段长度由各渠道自己声明
MAX_ANSWER_CHARS = 20000

# 记住最近处理过的消息编号条数：平台可能重复投递同一条事件
DEDUP_CACHE_SIZE = 200

HELP_TEXT = """机器人用法：

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


class ChannelPlugin(Protocol):
    """渠道插件约定：连接、消息格式、结果展示。

    渠道只做三件事，业务能力与会话规则都不在这里：
    1) 连接或读取本渠道的输入；
    2) 把平台报文转换成 IncomingMessage 交给 Gateway.handle；
    3) 实现 reply，把结果按本渠道的方式展示出去。

    Gateway 按 IncomingMessage.channel 与 name 的对应关系路由回程，
    因此多个入口可以同时挂载，共用同一套业务与会话规则。
    """

    name: str
    """渠道名，与 IncomingMessage.channel 一致，用于回程路由。"""

    max_segment_chars: int
    """该渠道单条消息的长度上限，决定超长答案怎么分段。"""

    def start(self) -> None:
        """开始接收本渠道的消息。"""

    def reply(self, target: str, text: str) -> None:
        """展示结果；target 由入站消息带回（飞书是 message_id，终端是本轮编号）。"""


class Gateway:
    """消息进入与离开服务的公共入口。"""

    def __init__(self, agent: ChatAgent, allowed_open_ids: Iterable[str]) -> None:
        self.agent = agent
        # 出站渠道由装配阶段注入，见 attach_outbound；可同时挂多个入口
        self.outbounds: dict[str, ChannelPlugin] = {}
        self.allowed_open_ids = frozenset(allowed_open_ids)
        self._seen_message_ids: deque[str] = deque(maxlen=DEDUP_CACHE_SIZE)

    def attach_outbound(self, outbound: ChannelPlugin) -> None:
        """注册一个入口渠道：适配器与网关互相依赖，装配时后注入。"""
        self.outbounds[outbound.name] = outbound

    def _outbound_for(self, msg: IncomingMessage) -> Optional[ChannelPlugin]:
        """按消息来源取回渠道：答案只回到提问的那个入口。"""
        return self.outbounds.get(msg.channel)

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

        outbound = self._outbound_for(msg)
        if outbound is None:
            logger.error("渠道未注册，丢弃消息: channel=%s message_id=%s", msg.channel, msg.message_id)
            return

        if msg.user_open_id not in self.allowed_open_ids:
            logger.warning("拒绝未授权发送人: sender=%s", msg.user_open_id)
            outbound.reply(
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
            outbound.reply(msg.message_id, HELP_TEXT)
            return

        if msg.message_id in self._seen_message_ids:
            logger.info("重复投递，忽略: message_id=%s", msg.message_id)
            return
        self._seen_message_ids.append(msg.message_id)

        # 模型调用耗时不可控，放到后台线程，避免占用渠道的接收线程
        threading.Thread(
            target=self._answer_and_reply,
            args=(msg, outbound, text),
            name=f"answer-{msg.channel}",
            daemon=True,
        ).start()

    def _answer_and_reply(
        self, msg: IncomingMessage, outbound: ChannelPlugin, question: str
    ) -> None:
        """后台线程：调用模型，把答案或错误提示回复到原消息。"""
        try:
            answer = self.agent.answer(question)
        except ChatInputError as exc:
            logger.info("输入不合法: channel=%s message_id=%s code=%s", msg.channel, msg.message_id, exc.code)
            outbound.reply(msg.message_id, exc.message)
            return
        except ModelCallError as exc:
            logger.error("模型调用失败: channel=%s message_id=%s 原因=%s", msg.channel, msg.message_id, exc)
            outbound.reply(msg.message_id, f"模型调用失败（MODEL_ERROR）：{exc}")
            return
        self._reply_in_segments(msg, outbound, answer)

    def _reply_in_segments(
        self, msg: IncomingMessage, outbound: ChannelPlugin, answer: str
    ) -> None:
        """按来源渠道声明的单条长度分段回复，保证内容完整送达。"""
        content = answer[:MAX_ANSWER_CHARS]
        size = outbound.max_segment_chars
        segments = [
            content[index : index + size] for index in range(0, len(content), size)
        ]
        total = len(segments)
        for position, segment in enumerate(segments, start=1):
            title = f"【回答 {position}/{total}】" if total > 1 else ""
            outbound.reply(msg.message_id, f"{title}\n{segment}".strip())
        logger.info(
            "答案已回发: channel=%s message_id=%s 分段=%d", msg.channel, msg.message_id, total
        )
