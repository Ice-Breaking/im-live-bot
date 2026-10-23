"""出站发送特征：分段规则、重试次数与间隔、失败定位。"""

from __future__ import annotations

import unittest
from typing import Optional

from outbound import SendError, send_answer, send_text
from tests.support import FakeChannel, QuietLoggingTestCase


class SegmentChannel(FakeChannel):
    """只让指定段失败的渠道：用来验证失败停在哪一段。"""

    def __init__(self, failing_segment: int, max_segment_chars: int = 3000) -> None:
        super().__init__(max_segment_chars=max_segment_chars)
        self.failing_segment = failing_segment

    def reply(self, target: str, text: str) -> None:
        self.attempts += 1
        if f"【回答 {self.failing_segment}/" in text:
            raise SendError(f"第 {self.failing_segment} 段发不出去")
        self.sent.append((target, text))


class SendTextTest(QuietLoggingTestCase):
    def test_success_on_first_attempt(self) -> None:
        channel = FakeChannel()
        outcome = send_text(channel, "om_1", "答案")
        self.assertTrue(outcome.ok)
        self.assertEqual(channel.attempts, 1)
        self.assertEqual(channel.sent, [("om_1", "答案")])

    def test_retries_then_succeeds(self) -> None:
        channel = FakeChannel(fail_times=1)
        retries: list[tuple] = []
        sleeps: list[float] = []
        outcome = send_text(
            channel,
            "om_1",
            "答案",
            label="提示",
            sleep=sleeps.append,
            on_retry=lambda label, attempt, exc: retries.append((label, attempt, str(exc))),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(channel.attempts, 2)
        self.assertEqual(retries, [("提示", 1, "注入的发送失败")])
        self.assertEqual(sleeps, [3.0])

    def test_gives_up_after_max_tries(self) -> None:
        channel = FakeChannel(fail_times=99)
        retries: list[int] = []
        sleeps: list[float] = []
        outcome = send_text(
            channel,
            "om_1",
            "答案",
            label="提示",
            sleep=sleeps.append,
            on_retry=lambda label, attempt, exc: retries.append(attempt),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failed_segment, 1)
        self.assertEqual(outcome.reason, "注入的发送失败")
        self.assertEqual(retries, [1, 2, 3])
        self.assertEqual(sleeps, [3.0, 3.0])

    def test_custom_tries_and_interval(self) -> None:
        channel = FakeChannel(fail_times=99)
        sleeps: list[float] = []
        send_text(channel, "om_1", "答案", max_tries=2, interval=0.5, sleep=sleeps.append)
        self.assertEqual(channel.attempts, 2)
        self.assertEqual(sleeps, [0.5])


class SendAnswerTest(QuietLoggingTestCase):
    def test_single_segment_has_no_title(self) -> None:
        channel = FakeChannel()
        outcome = send_answer(channel, "om_1", "答案", max_chars=20000)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.total_segments, 1)
        self.assertEqual(channel.sent, [("om_1", "答案")])

    def test_splits_by_channel_limit(self) -> None:
        channel = FakeChannel(max_segment_chars=3000)
        outcome = send_answer(channel, "om_1", "字" * 7000, max_chars=20000)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.total_segments, 3)
        titles = [text.split("\n", 1)[0] for _, text in channel.sent]
        self.assertEqual(titles, ["【回答 1/3】", "【回答 2/3】", "【回答 3/3】"])

    def test_truncated_at_max_chars(self) -> None:
        channel = FakeChannel()
        send_answer(channel, "om_1", "字" * 25000, max_chars=20000)
        self.assertEqual(len(channel.sent[0][1]), 20000)

    def test_stops_at_failing_segment(self) -> None:
        channel = SegmentChannel(failing_segment=2)
        outcome = send_answer(channel, "om_1", "字" * 7000, max_chars=20000, sleep=lambda _: None)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failed_segment, 2)
        self.assertEqual(outcome.total_segments, 3)
        self.assertEqual(len(channel.sent), 1)

    def test_empty_answer_sends_nothing(self) -> None:
        channel = FakeChannel()
        outcome = send_answer(channel, "om_1", "", max_chars=20000)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.total_segments, 0)
        self.assertEqual(channel.sent, [])


if __name__ == "__main__":
    unittest.main()
