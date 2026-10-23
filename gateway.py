"""Gateway 会话编排：判定该做什么，再交给发送与任务记录去执行。

收到消息后在后台线程调用模型，答案按原消息回发。
渠道插件只负责连接、消息格式与结果展示，新增入口不改这里。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

import task_log
from channel_contract import ChannelPlugin, IncomingMessage, SendError  # SendError 转出给渠道
from chat_agent import ChatAgent, ChatInputError
from llm_client import ModelCallError
from outbound import SEND_MAX_TRIES, SEND_RETRY_INTERVAL, SendOutcome, send_answer, send_text
from result_store import ResultStore
from session_policy import Decision, decide
from task_stage import (
    ANALYZED,
    ANALYZE_FAILED,
    ANALYZING,
    RECEIVED,
    REJECTED,
    RETRYING,
    REUSED,
    SEND_FAILED,
    SENT,
    SKIPPED,
)

logger = logging.getLogger(__name__)

# 答案总长度上限，与具体渠道无关；分段长度由各渠道自己声明
MAX_ANSWER_CHARS = 20000

# 记住最近处理过的消息与结果的条数：平台可能重复投递同一条事件
DEDUP_CACHE_SIZE = 200

HELP_TEXT = """机器人用法：

1) 直接提问
   把问题原样发给我，我会调用模型回答。

2) 查看本帮助
   /help

群聊里请 @ 机器人 再发送内容。"""


class Gateway:
    """消息进入与离开服务的公共入口：判定之后编排，具体活交给其它模块。"""

    def __init__(
        self,
        agent: ChatAgent,
        allowed_open_ids: Iterable[str],
        task_log: task_log.TaskLog,
        on_task_done: Optional[Callable[[str], None]] = None,
        store: Optional[ResultStore] = None,
    ) -> None:
        self.agent = agent
        # 出站渠道由装配阶段注入，见 attach_outbound；可同时挂多个入口
        self.outbounds: dict[str, ChannelPlugin] = {}
        self.allowed_open_ids = frozenset(allowed_open_ids)
        self.task_log = task_log
        # 任务走到终态时通知渠道：需要等待结果的入口（终端）靠它返回
        self.on_task_done = on_task_done
        self.store = store if store is not None else ResultStore(DEDUP_CACHE_SIZE)

    def attach_outbound(self, outbound: ChannelPlugin) -> None:
        """注册一个入口渠道：适配器与网关互相依赖，装配时后注入。"""
        self.outbounds[outbound.name] = outbound

    def handle(self, msg: IncomingMessage) -> Optional[str]:
        """处理一条入站消息：编号、判定、编排。

        返回任务编号，渠道可以把它展示给用户（终端入口会打印出来）。
        """
        task_id = self.task_log.new_task(msg.channel, msg.message_id, msg.user_open_id)
        self.task_log.record(
            task_id, RECEIVED, detail=f"chat_type={msg.chat_type} 文本长度={len(msg.text)}"
        )
        outbound = self._outbound_for(msg)
        decision = decide(
            msg,
            allowed=msg.user_open_id in self.allowed_open_ids,
            channel_registered=outbound is not None,
            seen=self.store.seen(msg.message_id),
            has_result=self.store.result_for(msg.message_id) is not None,
        )
        if decision is not Decision.ANSWER:
            return self._handle_direct(task_id, msg, outbound, decision)

        self.store.mark_seen(msg.message_id)
        # 模型调用耗时不可控，放到后台线程，避免占用渠道的接收线程
        threading.Thread(
            target=self._answer_and_reply,
            args=(task_id, msg, outbound, msg.text.strip()),
            name=f"answer-{msg.channel}",
            daemon=True,
        ).start()
        return task_id

    def _handle_direct(
        self,
        task_id: str,
        msg: IncomingMessage,
        outbound: Optional[ChannelPlugin],
        decision: Decision,
    ) -> str:
        """不进模型的几条路径：回一句就结束，或者干脆忽略。"""
        if decision is Decision.NO_CHANNEL:
            self.task_log.record(task_id, SKIPPED, detail="渠道未注册")
            logger.error("渠道未注册，丢弃消息: channel=%s message_id=%s", msg.channel, msg.message_id)
            self._finish(task_id)
            return task_id

        if decision is Decision.REJECT:
            self.task_log.record(task_id, REJECTED, detail="不在白名单")
            logger.warning("拒绝未授权发送人: sender=%s", msg.user_open_id)
            self._notify(
                task_id,
                msg,
                outbound,
                f"当前账号不在允许名单内，把 open_id（{msg.user_open_id}）"
                "加入 .env 的 ALLOWED_OPEN_IDS 后重启服务即可使用。",
                "未授权提示",
            )
            return task_id

        if decision is Decision.IGNORE_GROUP:
            self.task_log.record(task_id, SKIPPED, detail="群聊未 @ 机器人")
            logger.info("群聊未 @ 机器人，忽略: chat_id=%s", msg.chat_id)
            self._finish(task_id)
            return task_id

        if decision is Decision.HELP:
            outcome = self._send(task_id, outbound, msg.message_id, HELP_TEXT, "帮助")
            self.task_log.record(task_id, SENT if outcome.ok else SEND_FAILED, detail="帮助文本")
            self._finish(task_id)
            return task_id

        if decision is Decision.SKIP_DUPLICATE:
            self.task_log.record(task_id, SKIPPED, detail="重复投递")
            logger.info("重复投递，忽略: message_id=%s", msg.message_id)
            self._finish(task_id)
            return task_id

        # 上次发送失败但结果还在：直接重发，不再调用模型
        self.task_log.record(task_id, REUSED, detail="复用上次结果重发")
        self._send_answer(task_id, msg, outbound, self.store.result_for(msg.message_id))
        return task_id

    def _answer_and_reply(
        self, task_id: str, msg: IncomingMessage, outbound: ChannelPlugin, question: str
    ) -> None:
        """后台线程：调用模型，把答案或错误提示回复到原消息。"""
        self.task_log.record(task_id, ANALYZING, detail=f"问题长度={len(question)}")
        try:
            answer = self.agent.answer(question)
        except ChatInputError as exc:
            logger.info("输入不合法: task=%s code=%s", task_id, exc.code)
            self.task_log.record(task_id, ANALYZE_FAILED, detail=f"输入不合法 code={exc.code}")
            self._notify(task_id, msg, outbound, exc.message, "输入提示")
            return
        except ModelCallError as exc:
            logger.error("模型调用失败: task=%s 原因=%s", task_id, exc)
            self.task_log.record(task_id, ANALYZE_FAILED, detail=f"模型调用失败：{exc}")
            self._notify(task_id, msg, outbound, f"模型调用失败（MODEL_ERROR）：{exc}", "失败提示")
            return

        # 结果先留下再发：发送失败也能重发，不必重新分析
        self.task_log.record(task_id, ANALYZED, detail=f"结果长度={len(answer)}", answer=answer)
        self.store.remember(msg.message_id, answer)
        self._send_answer(task_id, msg, outbound, answer)

    def _send_answer(
        self, task_id: str, msg: IncomingMessage, outbound: ChannelPlugin, answer: str
    ) -> None:
        """分段发送答案，并按结果记「回复发出」或「回复失败」。"""
        outcome = send_answer(
            outbound,
            msg.message_id,
            answer,
            max_chars=MAX_ANSWER_CHARS,
            sleep=time.sleep,
            on_retry=self._retry_reporter(task_id),
        )
        if outcome.ok:
            self.task_log.record(task_id, SENT, detail=f"分段={outcome.total_segments}")
        else:
            failed = f"第 {outcome.failed_segment}/{outcome.total_segments} 段"
            detail = f"{failed} 重试 {SEND_MAX_TRIES} 次仍失败，结果已保留"
            self.task_log.record(task_id, SEND_FAILED, detail=detail)
        self._finish(task_id)

    def _notify(
        self,
        task_id: str,
        msg: IncomingMessage,
        outbound: ChannelPlugin,
        text: str,
        label: str,
    ) -> None:
        """把一句提示发出去，发不出去也记一条失败，然后结束任务。"""
        outcome = self._send(task_id, outbound, msg.message_id, text, label)
        if not outcome.ok:
            self.task_log.record(task_id, SEND_FAILED, detail=f"{label}发送失败")
        self._finish(task_id)

    def _send(
        self,
        task_id: str,
        outbound: ChannelPlugin,
        message_id: str,
        text: str,
        label: str,
    ) -> SendOutcome:
        """发一条文本，重试过程记「重试发送」。"""
        return send_text(
            outbound,
            message_id,
            text,
            label=label,
            sleep=time.sleep,
            on_retry=self._retry_reporter(task_id),
        )

    def _retry_reporter(self, task_id: str) -> Callable[[str, int, Exception], None]:
        """重试一次就打一条告警并记一个阶段；失败原因只在这里出现。"""

        def report(label: str, attempt: int, exc: Exception) -> None:
            logger.warning("回复失败: task=%s %s 第 %d 次 原因=%s", task_id, label, attempt, exc)
            if attempt < SEND_MAX_TRIES:
                detail = f"{label} 第 {attempt} 次失败，{SEND_RETRY_INTERVAL:g} 秒后重试"
                self.task_log.record(task_id, RETRYING, detail=detail)

        return report

    def _outbound_for(self, msg: IncomingMessage) -> Optional[ChannelPlugin]:
        """按消息来源取回渠道：答案只回到提问的那个入口。"""
        return self.outbounds.get(msg.channel)

    def _finish(self, task_id: str) -> None:
        """任务走到终态：清掉进程内登记并通知渠道，等待结果的入口据此返回。"""
        self.task_log.forget(task_id)
        if self.on_task_done is not None:
            self.on_task_done(task_id)
