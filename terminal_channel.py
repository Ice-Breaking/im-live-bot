"""终端渠道插件：一行一个问题，答案打印到屏幕。

与飞书渠道遵守同一套 ChannelPlugin 约定：只读输入、把输入转成 IncomingMessage、
按自己的方式展示结果；业务能力与会话规则都在 Gateway 里，两个入口共用同一份。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from gateway import MAX_ANSWER_CHARS, IncomingMessage

logger = logging.getLogger(__name__)

EXIT_COMMANDS = ("exit", "quit", "/exit", "/quit")

# 等待答案的上限，与模型调用超时一致；正常情况任务一结束就置位返回
ANSWER_WAIT_SECONDS = 180.0


class TerminalChannel:
    """终端渠道：stdin 读一行问一句，答案打到 stdout。"""

    # 渠道插件约定：名字用于回程路由，单条长度决定答案怎么分段
    name = "terminal"
    # 终端没有单条消息长度限制，取答案总上限，因此答案不会分段
    max_segment_chars = MAX_ANSWER_CHARS

    def __init__(
        self, user_open_id: str, on_message: Callable[[IncomingMessage], Optional[str]]
    ) -> None:
        self.user_open_id = user_open_id
        self.on_message = on_message
        self._round = 0
        # 按任务编号等待：网关在后台线程跑完整个任务后通知这里
        self._done: dict[str, threading.Event] = {}

    def start(self) -> None:
        """交互循环：读到退出命令或 EOF（Ctrl-D）结束。"""
        print("终端入口已启动，输入问题后回车；exit / quit / Ctrl-D 退出。")
        while True:
            try:
                line = input("你> ")
            except EOFError:
                print()
                break
            command = line.strip().lower()
            if command in EXIT_COMMANDS:
                break
            if not command:
                continue
            self.ask_once(line.strip())

    def ask_once(self, question: str) -> None:
        """提问一次并等任务结束；--ask 单次模式与交互循环都走这里。"""
        if not question.strip():
            print("助手> 问题为空，请直接输入想问的内容。")
            return

        self._round += 1
        event = threading.Event()
        task_id = self.on_message(
            IncomingMessage(
                channel=self.name,
                user_open_id=self.user_open_id,
                chat_id="local",
                chat_type="p2p",
                message_id=f"t{self._round}",
                text=question.strip(),
                mentioned_bot=False,
            )
        )
        if task_id is None:
            return
        self._done[task_id] = event
        print(f"（任务 {task_id}）已收到，正在分析…")
        event.wait(ANSWER_WAIT_SECONDS)
        self._done.pop(task_id, None)

    def notify_done(self, task_id: str) -> None:
        """网关通知任务结束：置位对应事件，让提问处返回。"""
        event = self._done.get(task_id)
        if event is not None:
            event.set()

    def reply(self, target: str, text: str) -> None:
        """把结果打印到屏幕：终端的展示方式，与飞书的回复互不影响。"""
        print(f"\n助手> {text}\n")
