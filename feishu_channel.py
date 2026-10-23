"""飞书 IM 平台适配器。

职责：建立 WebSocket 长连接、把消息事件归一化成 IncomingMessage、按原消息回复。
业务规则不在本模块：收到的消息交给 on_message 回调（由 Gateway 提供）。
换其他 IM 时只替换本文件。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Optional

import config  # noqa: F401  保证根证书修正在 lark_oapi 导入前生效
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from gateway import IncomingMessage

logger = logging.getLogger(__name__)

# 群聊 @ 机器人时，文本内容里会带 <at user_id="ou_xxx">@名字</at>
AT_PATTERN = re.compile(r"<at\b[^>]*>[^<]*</at>")

# 允许通过 LARK_LOG_LEVEL 控制的 SDK 日志级别；写错时启动即报 KeyError，不静默降级
LARK_LOG_LEVELS = {
    "DEBUG": lark.LogLevel.DEBUG,
    "INFO": lark.LogLevel.INFO,
    "WARNING": lark.LogLevel.WARNING,
    "ERROR": lark.LogLevel.ERROR,
}


class FeishuChannel:
    """飞书长连接通道：收消息交给回调，发消息走官方 API。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        bot_open_id: str,
        on_message: Callable[[IncomingMessage], None],
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_open_id = bot_open_id
        self.on_message = on_message
        # API 客户端负责发消息，tenant_access_token 由 SDK 自动管理
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        # 长连接模式下 Encrypt Key 与 Verification Token 均留空
        self.event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """接收消息事件：提取文本与发送人身份，交给 Gateway 处理。"""
        message = data.event.message
        if message.message_type != "text":
            logger.info("跳过非文本消息: message_type=%s", message.message_type)
            return

        text = json.loads(message.content).get("text", "")
        mentioned_bot = any(
            mention.id.open_id == self.bot_open_id for mention in (message.mentions or [])
        )
        incoming = IncomingMessage(
            channel="feishu",
            user_open_id=data.event.sender.sender_id.open_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            message_id=message.message_id,
            text=AT_PATTERN.sub("", text).strip(),
            mentioned_bot=mentioned_bot,
        )
        self.on_message(incoming)

    def reply(self, message_id: str, text: str) -> None:
        """按原消息 ID 回复文本消息。"""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.reply(request)
        if response.success():
            logger.info("回复成功: message_id=%s", message_id)
        else:
            logger.error("回复失败: message_id=%s code=%s msg=%s", message_id, response.code, response.msg)

    def start(self) -> None:
        """建立 WebSocket 长连接并阻塞运行，SDK 内置心跳与断线重连。

        SDK 日志级别由 LARK_LOG_LEVEL 控制：排障时设 DEBUG 可以看到收发的数据帧，
        日常运行用默认的 INFO。
        """
        ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=self.event_handler,
            log_level=LARK_LOG_LEVELS[os.environ.get("LARK_LOG_LEVEL", "INFO").upper()],
        )
        logger.info("正在建立飞书长连接: sdk日志级别=%s", os.environ.get("LARK_LOG_LEVEL", "INFO"))
        ws_client.start()
