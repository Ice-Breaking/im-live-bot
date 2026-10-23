"""测试替身与工具：不碰模型、不碰网络、不碰真实记录文件。"""

from __future__ import annotations

import logging
import tempfile
import time
import unittest
from pathlib import Path

from gateway import IncomingMessage, SendError
from task_log import TaskLog


class StubClient:
    """替身模型通道：只返回固定文本，不联网。"""

    def __init__(self, answer: str = "替身答案", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.prompts: list[str] = []

    def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.answer


class FakeChannel:
    """假渠道：满足插件约定，失败次数可配。"""

    name = "fake"

    def __init__(
        self,
        max_segment_chars: int = 20000,
        fail_times: int = 0,
        error: str = "注入的发送失败",
    ) -> None:
        self.max_segment_chars = max_segment_chars
        self.fail_times = fail_times
        self.error = error
        self.attempts = 0
        self.sent: list[tuple[str, str]] = []

    def start(self) -> None:
        pass

    def reply(self, target: str, text: str) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise SendError(self.error)
        self.sent.append((target, text))


def incoming(**kwargs) -> IncomingMessage:
    """构造一条入站消息，默认是可正常提问的单聊文本。"""
    base = dict(
        channel="fake",
        user_open_id="ou_allowed",
        chat_id="oc_probe",
        chat_type="p2p",
        message_id="om_1",
        text="你好",
        mentioned_bot=False,
    )
    return IncomingMessage(**{**base, **kwargs})


def temp_task_log(testcase: unittest.TestCase) -> TaskLog:
    """每个用例一份临时记录文件，避免污染 data/。"""
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    return TaskLog(Path(directory.name) / "tasks.jsonl")


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """等后台线程跑完：轮询而不是固定 sleep，避免偶发失败。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def no_sleep(_seconds: float) -> None:
    """替身 sleep：发送重试的等待在测试里不真等。"""
    return None


class QuietLoggingTestCase(unittest.TestCase):
    """测试期间日志不落控制台，也不影响其它用例。"""

    def setUp(self) -> None:
        super().setUp()
        root = logging.getLogger()
        self._saved_handlers = list(root.handlers)
        self._saved_level = root.level
        root.handlers = [logging.NullHandler()]
        root.setLevel(logging.INFO)
        self.addCleanup(self._restore_logging, root)

    def _restore_logging(self, root: logging.Logger) -> None:
        root.handlers = self._saved_handlers
        root.setLevel(self._saved_level)
