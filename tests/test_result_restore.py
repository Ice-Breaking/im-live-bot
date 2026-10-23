"""结果跨重启保留：上次没发出去的答案，重启后重复投递时直接重发。"""

from __future__ import annotations

import unittest
from unittest import mock

from chat_agent import ChatAgent
from gateway import Gateway
from result_store import ResultStore
from task_log import TaskLog
from task_stage import ANALYZED, RECEIVED, REUSED, SENT
from tests.support import (
    FakeChannel,
    QuietLoggingTestCase,
    StubClient,
    incoming,
    no_sleep,
    temp_task_log,
    wait_until,
)


class PendingResultsTest(QuietLoggingTestCase):
    def test_analyzed_without_sent_is_pending(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_a")
        task_log.record(task_id, ANALYZED, detail="结果长度=4", answer="答案一")
        self.assertEqual(task_log.pending_results(), {"om_1": "答案一"})

    def test_sent_result_is_not_pending(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_a")
        task_log.record(task_id, ANALYZED, detail="结果长度=4", answer="答案一")
        task_log.record(task_id, SENT, detail="分段=1")
        self.assertEqual(task_log.pending_results(), {})

    def test_analyzed_without_answer_is_not_pending(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_a")
        task_log.record(task_id, ANALYZED, detail="结果长度=0")
        self.assertEqual(task_log.pending_results(), {})

    def test_later_failure_beats_earlier_success(self) -> None:
        """同一条消息：先成功发出，后来又分析成功但没发出，仍算待重发。"""
        task_log = temp_task_log(self)
        first = task_log.new_task("fake", "om_1", "ou_a")
        task_log.record(first, ANALYZED, detail="结果长度=3", answer="旧答案")
        task_log.record(first, SENT, detail="分段=1")
        second = task_log.new_task("fake", "om_1", "ou_a")
        task_log.record(second, ANALYZED, detail="结果长度=3", answer="新答案")
        self.assertEqual(task_log.pending_results(), {"om_1": "新答案"})

    def test_empty_file_has_nothing_pending(self) -> None:
        self.assertEqual(temp_task_log(self).pending_results(), {})


class RestoreStoreTest(QuietLoggingTestCase):
    def test_restore_keeps_results_and_marks_seen(self) -> None:
        store = ResultStore(10)
        self.assertEqual(store.restore_pending({"om_1": "答案一"}), 1)
        self.assertEqual(store.result_for("om_1"), "答案一")
        self.assertTrue(store.seen("om_1"))

    def test_restore_keeps_only_latest_within_capacity(self) -> None:
        store = ResultStore(2)
        self.assertEqual(store.restore_pending({"om_1": "一", "om_2": "二", "om_3": "三"}), 3)
        self.assertIsNone(store.result_for("om_1"))
        self.assertEqual(store.result_for("om_3"), "三")


class RestartReuseTest(QuietLoggingTestCase):
    def test_reused_after_restart_without_reanalyzing(self) -> None:
        # 第一次运行：分析成功，但发送一直失败
        first_log = temp_task_log(self)
        first_client = StubClient(answer="替身答案")
        failing = FakeChannel(fail_times=99)
        gateway = Gateway(ChatAgent(first_client), ("ou_allowed",), first_log)
        gateway.attach_outbound(failing)
        with mock.patch("time.sleep", no_sleep):
            gateway.handle(incoming(message_id="om_1"))
            self.assertTrue(wait_until(lambda: failing.attempts >= 3))

        # 模拟重启：新网关 + 新缓存，从记录文件恢复待重发的结果
        restarted_store = ResultStore(200)
        restored = restarted_store.restore_pending(first_log.pending_results())
        self.assertEqual(restored, 1)

        second_client = StubClient(answer="不应该再分析")
        channel = FakeChannel()
        restarted = Gateway(
            ChatAgent(second_client), ("ou_allowed",), first_log, store=restarted_store
        )
        restarted.attach_outbound(channel)

        task_id = restarted.handle(incoming(message_id="om_1"))
        self.assertTrue(wait_until(lambda: len(channel.sent) >= 1))
        self.assertIn(REUSED, [row["stage"] for row in first_log.events_of(task_id)])
        self.assertEqual(channel.sent, [("om_1", "替身答案")])
        self.assertEqual(second_client.prompts, [])


if __name__ == "__main__":
    unittest.main()
