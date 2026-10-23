"""长连接守护：连上、断开、恢复各记一条，状态没变不重复记。

守护线程盯的是 SDK 连接对象（见 `feishu_channel._watch_connection`），
这里用替身对象切换连接状态，不连网就能验证记日志的时机。
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from feishu_channel import FeishuChannel
from tests.support import QuietLoggingTestCase


class ConnectionWatchTest(QuietLoggingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel = FeishuChannel("cli_test", "secret_test", "ou_bot", lambda msg: None)

    def run_watch(self, client: types.SimpleNamespace, states: list) -> list[str]:
        """跑一遍守护循环：每次等待时切换到下一个状态，切完即停。"""
        flips = iter(states)

        def fake_sleep(_seconds: float) -> None:
            try:
                client._conn = next(flips)
            except StopIteration:
                raise KeyboardInterrupt from None

        with mock.patch("feishu_channel.time.sleep", fake_sleep), self.assertLogs(
            "feishu_channel", level="INFO"
        ) as captured:
            with self.assertRaises(KeyboardInterrupt):
                self.channel._watch_connection(client)
        return [record.getMessage() for record in captured.records]

    def test_logs_each_state_change(self) -> None:
        messages = self.run_watch(
            types.SimpleNamespace(_conn=None), [object(), None, object()]
        )
        self.assertEqual(
            messages,
            [
                "平台连接: 飞书长连接 已建立",
                "平台连接: 飞书长连接 断开，重连中",
                "平台连接: 飞书长连接 已建立",
            ],
        )

    def test_stays_quiet_while_state_unchanged(self) -> None:
        connection = object()
        messages = self.run_watch(
            types.SimpleNamespace(_conn=connection), [connection, connection]
        )
        self.assertEqual(messages, ["平台连接: 飞书长连接 已建立"])


if __name__ == "__main__":
    unittest.main()
