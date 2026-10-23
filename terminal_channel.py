"""终端渠道插件：一行一个问题，答案打印到屏幕。

与飞书渠道遵守同一套 ChannelPlugin 约定：只读输入、把输入转成 IncomingMessage、
按自己的方式展示结果；业务能力与会话规则都在 Gateway 里，两个入口共用同一份。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from gateway import MAX_ANSWER_CHARS, IncomingMessage

logger = logging.getLogger(__name__)

EXIT_COMMANDS = ("exit", "quit", "/exit", "/quit")

# 等待答案的上限，与模型调用超时一致；正常情况答案一到就置位返回
ANSWER_WAIT_SECONDS = 180.0


class TerminalChannel:
    """终端渠道：stdin 读一行问一句，答案打到 stdout。"""

    # 渠道插件约定：名字用于回程路由，单条长度决定答案怎么分段
    name = "terminal"
    # 终端没有单条消息长度限制，取答案总上限，因此答案不会分段
    max_segment_chars = MAX_ANSWER_CHARS

    def __init__(self, user_open_id: str, on_message: Callable[[IncomingMessage], None]) -> None:
        self.user_open_id = user_open_id
        self.on_message = on_message
        self._round = 0
        # 每轮的完成事件：网关在后台线程回答，这里等落地再进入下一轮
        self._answered: dict[str, threading.Event] = {}

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
        """提问一次并等待答案落地；--ask 单次模式与交互循环都走这里。"""
        if not question.strip():
            print("助手> 问题为空，请直接输入想问的内容。")
            return

        self._round += 1
        target = f"t{self._round}"
        event = threading.Event()
        self._answered[target] = event
        self.on_message(
            IncomingMessage(
                channel=self.name,
                user_open_id=self.user_open_id,
                chat_id="local",
                chat_type="p2p",
                message_id=target,
                text=question.strip(),
                mentioned_bot=False,
            )
        )
        event.wait(ANSWER_WAIT_SECONDS)
        self._answered.pop(target, None)

    def reply(self, target: str, text: str) -> None:
        """把结果打印到屏幕：终端的展示方式，与飞书的回复互不影响。"""
        print(f"\n助手> {text}\n")
        event = self._answered.get(target)
        if event is not None:
            event.set()
