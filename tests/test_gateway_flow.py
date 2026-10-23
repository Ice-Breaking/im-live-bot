"""Gateway 行为特征：重构前后这些断言必须全部成立。

每个用例都通过公共接口驱动（Gateway.handle → 假渠道出站），
不依赖内部结构，因此重构拆分模块后仍可作为回归基线。
"""

from __future__ import annotations

import unittest
from unittest import mock

from chat_agent import ChatAgent
from gateway import HELP_TEXT, Gateway
from llm_client import ModelCallError
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
from tests.support import (
    FakeChannel,
    QuietLoggingTestCase,
    StubClient,
    incoming,
    no_sleep,
    temp_task_log,
    wait_until,
)


class GatewayFlowTest(QuietLoggingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = StubClient()
        self.channel = FakeChannel()
        self.task_log = temp_task_log(self)
        self.done: list[str] = []
        self.gateway = Gateway(
            ChatAgent(self.client),
            ("ou_allowed",),
            self.task_log,
            on_task_done=self.done.append,
        )
        self.gateway.attach_outbound(self.channel)

    def stages(self, task_id: str) -> list[str]:
        return [row["stage"] for row in self.task_log.events_of(task_id)]

    def wait_reply(self, count: int = 1) -> None:
        self.assertTrue(wait_until(lambda: len(self.channel.sent) >= count), "等待出站消息超时")

    def wait_stage(self, task_id: str, stage: str) -> None:
        self.assertTrue(wait_until(lambda: stage in self.stages(task_id)), f"等待阶段 {stage} 超时")

    # 正常路径

    def test_normal_answer_records_four_stages(self) -> None:
        task_id = self.gateway.handle(incoming())
        self.wait_stage(task_id, SENT)
        self.assertEqual(
            self.stages(task_id), [RECEIVED, ANALYZING, ANALYZED, SENT]
        )
        self.assertEqual(self.channel.sent, [("om_1", "替身答案")])
        self.assertEqual(len(self.client.prompts), 1)
        self.assertEqual(self.done, [task_id])

    def test_task_ids_start_at_T00001_and_increment(self) -> None:
        first = self.gateway.handle(incoming(message_id="om_1"))
        self.wait_reply()
        second = self.gateway.handle(incoming(message_id="om_2"))
        self.wait_reply(2)
        self.assertEqual((first, second), ("T00001", "T00002"))

    def test_answer_splits_by_channel_limit(self) -> None:
        self.channel.max_segment_chars = 3000
        self.client.answer = "字" * 7000
        self.gateway.handle(incoming())
        self.wait_reply(3)
        self.assertEqual(len(self.channel.sent), 3)
        titles = [text.split("\n", 1)[0] for _, text in self.channel.sent]
        self.assertEqual(titles, ["【回答 1/3】", "【回答 2/3】", "【回答 3/3】"])
        self.assertEqual(sum(len(text) for _, text in self.channel.sent), 7000 + 3 * len("【回答 1/3】") + 3)

    def test_single_segment_has_no_title(self) -> None:
        self.gateway.handle(incoming())
        self.wait_reply()
        self.assertEqual(self.channel.sent[0][1], "替身答案")

    def test_group_at_bot_is_answered(self) -> None:
        self.gateway.handle(incoming(chat_type="group", mentioned_bot=True))
        self.wait_reply()
        self.assertEqual(len(self.client.prompts), 1)

    # 边界情况

    def test_answer_truncated_at_max_chars(self) -> None:
        self.channel.max_segment_chars = 20000
        self.client.answer = "字" * 25000
        self.gateway.handle(incoming())
        self.wait_reply()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(self.channel.sent[0][1]), 20000)

    def test_empty_question(self) -> None:
        task_id = self.gateway.handle(incoming(text="   "))
        self.wait_reply()
        self.assertEqual(self.channel.sent[0][1], "消息内容为空，请直接把想问的内容发给我。")
        self.assertIn(ANALYZE_FAILED, self.stages(task_id))
        self.assertEqual(self.client.prompts, [])

    def test_question_too_long(self) -> None:
        task_id = self.gateway.handle(incoming(text="很" * 9000))
        self.wait_reply()
        self.assertIn("超过 8000 字上限", self.channel.sent[0][1])
        self.assertIn(ANALYZE_FAILED, self.stages(task_id))
        self.assertEqual(self.client.prompts, [])

    def test_help_variants(self) -> None:
        for index, command in enumerate(("/help", "help", "帮助"), start=1):
            self.gateway.handle(incoming(text=command, message_id=f"om_help{index}"))
            self.wait_reply(index)
            self.assertEqual(self.channel.sent[-1][1], HELP_TEXT)
        self.assertEqual(self.client.prompts, [])

    # 异常场景

    def test_model_error_is_reported(self) -> None:
        self.client.error = ModelCallError("模型调用失败：boom")
        task_id = self.gateway.handle(incoming())
        self.wait_reply()
        self.assertTrue(self.channel.sent[0][1].startswith("模型调用失败（MODEL_ERROR）："))
        self.assertIn(ANALYZE_FAILED, self.stages(task_id))

    def test_unauthorized_user_is_rejected_with_open_id(self) -> None:
        task_id = self.gateway.handle(incoming(user_open_id="ou_stranger"))
        self.wait_reply()
        text = self.channel.sent[0][1]
        self.assertIn("ou_stranger", text)
        self.assertIn("ALLOWED_OPEN_IDS", text)
        self.assertIn(REJECTED, self.stages(task_id))
        self.assertEqual(self.client.prompts, [])

    def test_group_without_at_is_ignored(self) -> None:
        task_id = self.gateway.handle(incoming(chat_type="group", mentioned_bot=False))
        self.assertEqual(self.stages(task_id), [RECEIVED, SKIPPED])
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.client.prompts, [])

    def test_unregistered_channel_is_skipped(self) -> None:
        task_id = self.gateway.handle(incoming(channel="nope"))
        self.assertEqual(self.stages(task_id), [RECEIVED, SKIPPED])
        self.assertEqual(self.channel.sent, [])

    def test_duplicate_reuses_result_without_reanalyzing(self) -> None:
        """现状：结果记住后，重复投递会再发一次同一答案，但不会重新分析。"""
        self.gateway.handle(incoming(message_id="om_dup"))
        self.wait_reply()
        task_id = self.gateway.handle(incoming(message_id="om_dup"))
        self.wait_reply(2)
        self.assertIn(REUSED, self.stages(task_id))
        self.assertEqual(self.channel.sent, [("om_dup", "替身答案"), ("om_dup", "替身答案")])
        self.assertEqual(len(self.client.prompts), 1)

    def test_send_failure_retries_then_keeps_result(self) -> None:
        self.channel.fail_times = 3
        with mock.patch("time.sleep", no_sleep):
            task_id = self.gateway.handle(incoming())
            self.wait_stage(task_id, SEND_FAILED)
        stages = self.stages(task_id)
        self.assertEqual(stages.count(RETRYING), 2)
        self.assertEqual(stages[-1], SEND_FAILED)
        self.assertEqual(self.channel.attempts, 3)
        self.assertEqual(len(self.client.prompts), 1)

    def test_failed_result_is_reused_without_reanalyzing(self) -> None:
        self.channel.fail_times = 3
        with mock.patch("time.sleep", no_sleep):
            self.gateway.handle(incoming())
            self.wait_stage("T00001", SEND_FAILED)
        self.channel.fail_times = 0
        task_id = self.gateway.handle(incoming())
        self.wait_reply()
        self.assertIn(REUSED, self.stages(task_id))
        self.assertEqual(self.channel.sent, [("om_1", "替身答案")])
        self.assertEqual(len(self.client.prompts), 1)

    def test_retry_recovers_on_second_attempt(self) -> None:
        self.channel.fail_times = 1
        with mock.patch("time.sleep", no_sleep):
            task_id = self.gateway.handle(incoming())
            self.wait_stage(task_id, SENT)
        stages = self.stages(task_id)
        self.assertEqual(stages.count(RETRYING), 1)
        self.assertEqual(stages[-1], SENT)
        self.assertEqual(self.channel.attempts, 2)

    def test_segment_failure_keeps_result(self) -> None:
        self.channel.max_segment_chars = 3000
        self.channel.fail_times = 3
        self.client.answer = "字" * 7000
        with mock.patch("time.sleep", no_sleep):
            task_id = self.gateway.handle(incoming())
            self.wait_stage(task_id, SEND_FAILED)
        self.assertEqual(self.stages(task_id)[-1], SEND_FAILED)
        self.assertEqual(len(self.channel.sent), 0)


if __name__ == "__main__":
    unittest.main()
