"""Gateway 会话规则：谁能用、哪些消息要处理、答案怎么回。

收到消息后在后台线程调用模型，答案按原消息回发。
渠道插件只负责连接、消息格式与结果展示，新增入口不改这里。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

import task_log
from chat_agent import ChatAgent, ChatInputError
from llm_client import ModelCallError

logger = logging.getLogger(__name__)

# 答案总长度上限，与具体渠道无关；分段长度由各渠道自己声明
MAX_ANSWER_CHARS = 20000

# 记住最近处理过的消息编号条数：平台可能重复投递同一条事件
DEDUP_CACHE_SIZE = 200

# 发送失败后的重试：平台偶发抖动能自愈，连续失败才放弃并保留结果
SEND_MAX_TRIES = 3
SEND_RETRY_INTERVAL = 3.0

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


class SendError(RuntimeError):
    """渠道发送失败：由 ChannelPlugin.reply 抛出，Gateway 据此重试并记失败。"""


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
        """展示结果；target 由入站消息带回（飞书是 message_id，终端是本轮编号）。

        发送不成功必须抛 SendError，让 Gateway 能重试并记失败状态。
        """


class Gateway:
    """消息进入与离开服务的公共入口。"""

    def __init__(
        self,
        agent: ChatAgent,
        allowed_open_ids: Iterable[str],
        task_log: task_log.TaskLog,
        on_task_done: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.agent = agent
        # 出站渠道由装配阶段注入，见 attach_outbound；可同时挂多个入口
        self.outbounds: dict[str, ChannelPlugin] = {}
        self.allowed_open_ids = frozenset(allowed_open_ids)
        self.task_log = task_log
        # 任务走到终态时通知渠道：需要等待结果的入口（终端）靠它返回
        self.on_task_done = on_task_done
        self._seen_message_ids: deque[str] = deque(maxlen=DEDUP_CACHE_SIZE)
        # 最近的结果：发送失败后留在内存，同一条消息再来时直接重发，不再分析
        self._results: dict[str, str] = {}
        self._result_order: deque[str] = deque(maxlen=DEDUP_CACHE_SIZE)

    def attach_outbound(self, outbound: ChannelPlugin) -> None:
        """注册一个入口渠道：适配器与网关互相依赖，装配时后注入。"""
        self.outbounds[outbound.name] = outbound

    def _finish(self, task_id: str) -> None:
        """任务走到终态：清掉进程内登记并通知渠道，等待结果的入口据此返回。"""
        self.task_log.forget(task_id)
        if self.on_task_done is not None:
            self.on_task_done(task_id)

    def _remember_result(self, message_id: str, answer: str) -> None:
        """记下分析结果，供发送失败后重发复用。"""
        self._results[message_id] = answer
        self._result_order.append(message_id)
        while len(self._results) > DEDUP_CACHE_SIZE:
            oldest = self._result_order.popleft()
            self._results.pop(oldest, None)

    def _outbound_for(self, msg: IncomingMessage) -> Optional[ChannelPlugin]:
        """按消息来源取回渠道：答案只回到提问的那个入口。"""
        return self.outbounds.get(msg.channel)

    def handle(self, msg: IncomingMessage) -> Optional[str]:
        """处理一条入站消息：编号、鉴权、去重，然后交给后台线程回答。

        返回任务编号，渠道可以把它展示给用户（终端入口会打印出来）。
        """
        task_id = self.task_log.new_task(msg.channel, msg.message_id, msg.user_open_id)
        self.task_log.record(
            task_id,
            task_log.RECEIVED,
            detail=f"chat_type={msg.chat_type} 文本长度={len(msg.text)}",
        )

        outbound = self._outbound_for(msg)
        if outbound is None:
            self.task_log.record(task_id, task_log.SKIPPED, detail="渠道未注册")
            logger.error("渠道未注册，丢弃消息: channel=%s message_id=%s", msg.channel, msg.message_id)
            self._finish(task_id)
            return task_id

        if msg.user_open_id not in self.allowed_open_ids:
            self.task_log.record(task_id, task_log.REJECTED, detail="不在白名单")
            logger.warning("拒绝未授权发送人: sender=%s", msg.user_open_id)
            sent = self._send_with_retry(
                task_id,
                outbound,
                msg,
                f"当前账号不在允许名单内，把 open_id（{msg.user_open_id}）"
                "加入 .env 的 ALLOWED_OPEN_IDS 后重启服务即可使用。",
                "未授权提示",
            )
            if not sent:
                self.task_log.record(task_id, task_log.SEND_FAILED, detail="未授权提示发送失败")
            self._finish(task_id)
            return task_id

        if msg.chat_type == "group" and not msg.mentioned_bot:
            self.task_log.record(task_id, task_log.SKIPPED, detail="群聊未 @ 机器人")
            logger.info("群聊未 @ 机器人，忽略: chat_id=%s", msg.chat_id)
            self._finish(task_id)
            return task_id

        text = msg.text.strip()
        if text in ("/help", "help", "帮助"):
            sent = self._send_with_retry(task_id, outbound, msg, HELP_TEXT, "帮助")
            self.task_log.record(
                task_id, task_log.SENT if sent else task_log.SEND_FAILED, detail="帮助文本"
            )
            self._finish(task_id)
            return task_id

        if msg.message_id in self._seen_message_ids:
            cached = self._results.get(msg.message_id)
            if cached is None:
                self.task_log.record(task_id, task_log.SKIPPED, detail="重复投递")
                logger.info("重复投递，忽略: message_id=%s", msg.message_id)
                self._finish(task_id)
                return task_id
            # 上次发送失败但结果还在：直接重发，不再调用模型
            self.task_log.record(task_id, task_log.REUSED, detail="复用上次结果重发")
            self._send_segments(task_id, msg, outbound, cached)
            return task_id

        self._seen_message_ids.append(msg.message_id)

        # 模型调用耗时不可控，放到后台线程，避免占用渠道的接收线程
        threading.Thread(
            target=self._answer_and_reply,
            args=(task_id, msg, outbound, text),
            name=f"answer-{msg.channel}",
            daemon=True,
        ).start()
        return task_id

    def _answer_and_reply(
        self, task_id: str, msg: IncomingMessage, outbound: ChannelPlugin, question: str
    ) -> None:
        """后台线程：调用模型，把答案或错误提示回复到原消息。"""
        self.task_log.record(task_id, task_log.ANALYZING, detail=f"问题长度={len(question)}")
        try:
            answer = self.agent.answer(question)
        except ChatInputError as exc:
            logger.info("输入不合法: task=%s code=%s", task_id, exc.code)
            self.task_log.record(task_id, task_log.ANALYZE_FAILED, detail=f"输入不合法 code={exc.code}")
            sent = self._send_with_retry(task_id, outbound, msg, exc.message, "输入提示")
            if not sent:
                self.task_log.record(task_id, task_log.SEND_FAILED, detail="输入提示发送失败")
            self._finish(task_id)
            return
        except ModelCallError as exc:
            logger.error("模型调用失败: task=%s 原因=%s", task_id, exc)
            self.task_log.record(task_id, task_log.ANALYZE_FAILED, detail=f"模型调用失败：{exc}")
            sent = self._send_with_retry(
                task_id, outbound, msg, f"模型调用失败（MODEL_ERROR）：{exc}", "失败提示"
            )
            if not sent:
                self.task_log.record(task_id, task_log.SEND_FAILED, detail="失败提示发送失败")
            self._finish(task_id)
            return

        # 结果先留下再发：发送失败也能重发，不必重新分析
        self.task_log.record(task_id, task_log.ANALYZED, detail=f"结果长度={len(answer)}", answer=answer)
        self._remember_result(msg.message_id, answer)
        self._send_segments(task_id, msg, outbound, answer)

    def _send_segments(
        self, task_id: str, msg: IncomingMessage, outbound: ChannelPlugin, answer: str
    ) -> bool:
        """按来源渠道声明的单条长度分段回复，保证内容完整送达。"""
        content = answer[:MAX_ANSWER_CHARS]
        size = outbound.max_segment_chars
        segments = [content[index : index + size] for index in range(0, len(content), size)]
        total = len(segments)
        for position, segment in enumerate(segments, start=1):
            title = f"【回答 {position}/{total}】" if total > 1 else ""
            label = f"第 {position}/{total} 段"
            sent = self._send_with_retry(
                task_id, outbound, msg, f"{title}\n{segment}".strip(), label
            )
            if not sent:
                self.task_log.record(
                    task_id,
                    task_log.SEND_FAILED,
                    detail=f"{label} 重试 {SEND_MAX_TRIES} 次仍失败，结果已保留",
                )
                self._finish(task_id)
                return False
        self.task_log.record(task_id, task_log.SENT, detail=f"分段={total}")
        self._finish(task_id)
        return True

    def _send_with_retry(
        self,
        task_id: str,
        outbound: ChannelPlugin,
        msg: IncomingMessage,
        text: str,
        label: str,
    ) -> bool:
        """发送一次，失败按固定间隔重试；渠道用 SendError 报告失败。"""
        for attempt in range(1, SEND_MAX_TRIES + 1):
            try:
                outbound.reply(msg.message_id, text)
                return True
            except SendError as exc:
                logger.warning("回复失败: task=%s %s 第 %d 次 原因=%s", task_id, label, attempt, exc)
                if attempt < SEND_MAX_TRIES:
                    self.task_log.record(
                        task_id,
                        task_log.RETRYING,
                        detail=f"{label} 第 {attempt} 次失败，{SEND_RETRY_INTERVAL:g} 秒后重试",
                    )
                    time.sleep(SEND_RETRY_INTERVAL)
        return False
