"""渠道插件约定：入站消息的形状、插件要实现什么、发送失败怎么报。

约定单独放，Gateway 与渠道都只依赖它，避免适配器与编排互相依赖；
gateway 会把它转出去，`from gateway import IncomingMessage, SendError` 仍然可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IncomingMessage:
    """平台适配器交给 Gateway 的归一化消息。"""

    channel: str
    user_open_id: str
    chat_id: str
    chat_type: str
    message_id: str
    text: str
    mentioned_bot: bool


class SendError(RuntimeError):
    """渠道发送失败：由 ChannelPlugin.reply 抛出，Gateway 据此重试并记失败。"""


class ChannelPlugin(Protocol):
    """渠道插件约定：连接、消息格式、结果展示。

    渠道只做三件事，业务能力与会话规则都不在这里：
    1) 连接或读取本渠道的输入；
    2) 把平台报文转换成 IncomingMessage 交给 Gateway.handle；
    3) 实现 reply，把结果按本渠道的方式展示出去。

    Gateway 按 IncomingMessage.channel 与 name 的对应关系路由回程，
    因此多个入口可以同时挂载，共用同一套业务与会话规则。
    """

    name: str
    """渠道名，与 IncomingMessage.channel 一致，用于回程路由。"""

    max_segment_chars: int
    """该渠道单条消息的长度上限，决定超长答案怎么分段。"""

    def start(self) -> None:
        """开始接收本渠道的消息。"""

    def reply(self, target: str, text: str) -> None:
        """展示结果；target 由入站消息带回（飞书是 message_id，终端是本轮编号）。

        发送不成功必须抛 SendError，让 Gateway 能重试并记失败状态。
        """
