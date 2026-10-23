"""会话规则判定矩阵：改判定顺序会立刻被这些用例抓到。"""

from __future__ import annotations

import types
import unittest

from session_policy import Decision, decide


def message(chat_type: str = "p2p", text: str = "你好", mentioned_bot: bool = False):
    return types.SimpleNamespace(chat_type=chat_type, text=text, mentioned_bot=mentioned_bot)


class SessionPolicyTest(unittest.TestCase):
    def decide(self, msg, **kwargs) -> Decision:
        defaults = dict(allowed=True, channel_registered=True, seen=False, has_result=False)
        return decide(msg, **{**defaults, **kwargs})

    def test_normal_question_is_answered(self) -> None:
        self.assertIs(self.decide(message()), Decision.ANSWER)

    def test_unregistered_channel_wins_over_everything(self) -> None:
        decision = self.decide(
            message(), channel_registered=False, allowed=False, seen=True, has_result=True
        )
        self.assertIs(decision, Decision.NO_CHANNEL)

    def test_unauthorized_is_rejected_before_group_rules(self) -> None:
        decision = self.decide(
            message(chat_type="group", mentioned_bot=False), allowed=False
        )
        self.assertIs(decision, Decision.REJECT)

    def test_group_without_at_is_ignored(self) -> None:
        self.assertIs(self.decide(message(chat_type="group")), Decision.IGNORE_GROUP)

    def test_group_with_at_is_answered(self) -> None:
        self.assertIs(
            self.decide(message(chat_type="group", mentioned_bot=True)), Decision.ANSWER
        )

    def test_help_commands(self) -> None:
        for command in ("/help", "help", "帮助", "  /help  "):
            self.assertIs(self.decide(message(text=command)), Decision.HELP)

    def test_case_sensitive_help(self) -> None:
        self.assertIs(self.decide(message(text="/HELP")), Decision.ANSWER)

    def test_duplicate_without_result_is_skipped(self) -> None:
        self.assertIs(self.decide(message(), seen=True), Decision.SKIP_DUPLICATE)

    def test_duplicate_with_result_is_reused(self) -> None:
        self.assertIs(self.decide(message(), seen=True, has_result=True), Decision.REUSE)

    def test_empty_text_still_goes_to_answer(self) -> None:
        """空消息由业务层拒绝，不是会话规则的事。"""
        self.assertIs(self.decide(message(text="   ")), Decision.ANSWER)


if __name__ == "__main__":
    unittest.main()
