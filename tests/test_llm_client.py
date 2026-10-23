"""模型通道：连接类失败重建客户端重试一次，其余异常不重试。

为什么单独测：断网后旧客户端可能一直连不上（本地实测是 `Errno 61` 直接拒连），
重建 httpx/OpenAI 客户端能把这类状态清掉；超时不重试，否则用户要等两倍时长。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

import llm_client
from llm_client import ModelCallError, StepFunClient
from tests.support import QuietLoggingTestCase

REQUEST = httpx.Request("POST", "https://example.test/v1/chat/completions")


class StubCompletion:
    """够 chat() 取文本的最小返回体。"""

    def __init__(self, text: str) -> None:
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=text))]


class StubOpenAI:
    """按顺序吐出预置结果：异常就抛，正常体就返回。"""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    @property
    def chat(self) -> SimpleNamespace:
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_kwargs) -> StubCompletion:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StepFunClientTest(QuietLoggingTestCase):
    def make(self, outcomes: list) -> tuple[StepFunClient, StubOpenAI, list]:
        """构造客户端，同时记录底层 client 建了几次。"""
        stub = StubOpenAI(outcomes)
        builds: list[dict] = []

        def factory(**kwargs) -> StubOpenAI:
            builds.append(kwargs)
            return stub

        patcher = mock.patch.object(llm_client, "OpenAI", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return StepFunClient("key_test", "https://example.test/v1", "step-test"), stub, builds

    def test_returns_text(self) -> None:
        client, stub, builds = self.make([StubCompletion("可以")])
        self.assertEqual(client.chat("问题"), "可以")
        self.assertEqual(stub.calls, 1)
        self.assertEqual(len(builds), 1)

    def test_rebuilds_client_and_retries_once(self) -> None:
        client, stub, builds = self.make(
            [APIConnectionError(request=REQUEST), StubCompletion("答案")]
        )
        self.assertEqual(client.chat("问题"), "答案")
        self.assertEqual(stub.calls, 2)
        self.assertEqual(len(builds), 2, "连接失败后应重建客户端")

    def test_gives_up_after_retry(self) -> None:
        client, stub, _ = self.make(
            [APIConnectionError(request=REQUEST), APIConnectionError(request=REQUEST)]
        )
        with self.assertRaises(ModelCallError) as caught:
            client.chat("问题")
        self.assertIn("APIConnectionError", str(caught.exception))
        self.assertEqual(stub.calls, 2)

    def test_timeout_is_not_retried(self) -> None:
        client, stub, _ = self.make([APITimeoutError(request=REQUEST)])
        with self.assertRaises(ModelCallError):
            client.chat("问题")
        self.assertEqual(stub.calls, 1)

    def test_status_error_is_not_retried(self) -> None:
        error = APIStatusError(
            "unauthorized", response=httpx.Response(401, request=REQUEST), body=None
        )
        client, stub, _ = self.make([error])
        with self.assertRaises(ModelCallError):
            client.chat("问题")
        self.assertEqual(stub.calls, 1)

    def test_empty_answer_is_error(self) -> None:
        client, _, _ = self.make([StubCompletion("   ")])
        with self.assertRaises(ModelCallError):
            client.chat("问题")


if __name__ == "__main__":
    unittest.main()
