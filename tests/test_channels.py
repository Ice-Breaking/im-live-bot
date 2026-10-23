"""渠道插件特征：终端的等待与输出语义，飞书的归一化与发送失败抛错。"""

from __future__ import annotations

import io
import json
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from feishu_channel import FeishuChannel
from gateway import SendError
from terminal_channel import TerminalChannel
from tests.support import QuietLoggingTestCase


class TerminalChannelTest(QuietLoggingTestCase):
    def make_channel(self) -> tuple[TerminalChannel, list]:
        """终端入口要等任务结束才返回，这里用后台线程模拟网关的通知。"""
        holder: dict = {}
        dispatched: list = []

        def on_message(message) -> str:
            dispatched.append(message)

            def finish() -> None:
                time.sleep(0.05)
                holder["channel"].notify_done("T1")

            threading.Thread(target=finish, daemon=True).start()
            return "T1"

        channel = TerminalChannel("ou_terminal", on_message)
        holder["channel"] = channel
        return channel, dispatched

    def test_ask_once_dispatches_and_waits(self) -> None:
        channel, dispatched = self.make_channel()
        out = io.StringIO()
        with redirect_stdout(out):
            channel.ask_once("你好")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].text, "你好")
        self.assertEqual(dispatched[0].channel, "terminal")
        self.assertEqual(dispatched[0].user_open_id, "ou_terminal")
        self.assertIn("（任务 T1）已收到，正在分析…", out.getvalue())

    def test_ask_once_rejects_empty_question(self) -> None:
        channel, dispatched = self.make_channel()
        out = io.StringIO()
        with redirect_stdout(out):
            channel.ask_once("   ")
        self.assertEqual(dispatched, [])
        self.assertIn("问题为空", out.getvalue())

    def test_reply_prints_answer(self) -> None:
        channel, _ = self.make_channel()
        out = io.StringIO()
        with redirect_stdout(out):
            channel.reply("t1", "这是答案")
        self.assertIn("助手> 这是答案", out.getvalue())

    def test_repl_exits_on_exit_command(self) -> None:
        channel, dispatched = self.make_channel()
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=["你好", "exit"]), redirect_stdout(out):
            channel.start()
        self.assertEqual(len(dispatched), 1)
        self.assertIn("exit / quit", out.getvalue())

    def test_repl_stops_on_eof(self) -> None:
        channel, dispatched = self.make_channel()
        with mock.patch("builtins.input", side_effect=EOFError), redirect_stdout(io.StringIO()):
            channel.start()
        self.assertEqual(dispatched, [])

    def test_plugin_contract(self) -> None:
        self.assertEqual(TerminalChannel.name, "terminal")
        self.assertEqual(TerminalChannel.max_segment_chars, 20000)


def mention(open_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=types.SimpleNamespace(open_id=open_id))


def make_event(
    text: str = "你好",
    chat_type: str = "p2p",
    mentions: tuple = (),
    message_type: str = "text",
) -> types.SimpleNamespace:
    """构造平台事件：只提供适配器会读到的字段。"""
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            message=types.SimpleNamespace(
                message_type=message_type,
                content=json.dumps({"text": text}, ensure_ascii=False),
                chat_id="oc_1",
                chat_type=chat_type,
                message_id="om_1",
                mentions=list(mentions),
            ),
            sender=types.SimpleNamespace(sender_id=types.SimpleNamespace(open_id="ou_user")),
        )
    )


class FeishuChannelTest(QuietLoggingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.received: list = []
        self.channel = FeishuChannel(
            "cli_test", "secret_test", "ou_bot", lambda msg: self.received.append(msg)
        )

    def test_p2p_text_is_normalized(self) -> None:
        self.channel._on_message_receive(make_event())
        message = self.received[0]
        self.assertEqual(message.channel, "feishu")
        self.assertEqual(message.chat_type, "p2p")
        self.assertEqual(message.text, "你好")
        self.assertEqual(message.user_open_id, "ou_user")
        self.assertEqual(message.message_id, "om_1")

    def test_group_at_bot_strips_at_tag(self) -> None:
        self.channel._on_message_receive(
            make_event(
                text='<at user_id="ou_bot">@IM_Live</at> 帮我看下这条',
                chat_type="group",
                mentions=(mention("ou_bot"),),
            )
        )
        message = self.received[0]
        self.assertTrue(message.mentioned_bot)
        self.assertEqual(message.text, "帮我看下这条")

    def test_group_at_other_is_not_mentioning_bot(self) -> None:
        self.channel._on_message_receive(
            make_event(
                text='<at user_id="ou_other">@别人</at> 你好',
                chat_type="group",
                mentions=(mention("ou_other"),),
            )
        )
        self.assertFalse(self.received[0].mentioned_bot)

    def test_non_text_is_skipped(self) -> None:
        self.channel._on_message_receive(make_event(message_type="image"))
        self.assertEqual(self.received, [])

    def test_reply_raises_send_error_on_failure(self) -> None:
        failure = types.SimpleNamespace(success=lambda: False, code=99992354, msg="boom")
        with mock.patch.object(
            self.channel.client.im.v1.message, "reply", return_value=failure
        ):
            with self.assertRaises(SendError) as ctx:
                self.channel.reply("om_1", "答案")
        self.assertIn("code=99992354", str(ctx.exception))

    def test_reply_succeeds_silently(self) -> None:
        success = types.SimpleNamespace(success=lambda: True, code=0, msg="ok")
        with mock.patch.object(
            self.channel.client.im.v1.message, "reply", return_value=success
        ) as reply:
            self.channel.reply("om_1", "答案")
        reply.assert_called_once()

    def test_plugin_contract(self) -> None:
        self.assertEqual(FeishuChannel.name, "feishu")
        self.assertEqual(FeishuChannel.max_segment_chars, 3000)


if __name__ == "__main__":
    unittest.main()
