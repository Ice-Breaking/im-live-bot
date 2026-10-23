"""装配：把配置、业务能力、网关与启用的入口接起来。

放在这里是为了让 main.py 只管启动方式，装配关系可以单独测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from chat_agent import ChatAgent
from config import Config
from feishu_channel import FeishuChannel
from gateway import Gateway
from llm_client import StepFunClient
from task_log import TaskLog
from terminal_channel import TerminalChannel


@dataclass(frozen=True)
class Runtime:
    """装配结果：一个网关，加上启用到的入口。"""

    gateway: Gateway
    terminal: Optional[TerminalChannel]
    feishu: Optional[FeishuChannel]


def build_runtime(cfg: Config, channels: tuple[str, ...]) -> Runtime:
    """按启用的入口装配：多个入口共用同一个网关与业务对象。"""
    agent = ChatAgent(StepFunClient(cfg.step_api_key, cfg.step_base_url, cfg.step_model))
    task_log = TaskLog(cfg.task_log_path)

    terminal: Optional[TerminalChannel] = None
    if "terminal" in channels:
        # gateway 稍后才建好，这里用延迟调用的闭包
        terminal = TerminalChannel(cfg.terminal_user_open_id, lambda msg: gateway.handle(msg))

    gateway = Gateway(
        agent,
        cfg.allowed_open_ids,
        task_log,
        on_task_done=terminal.notify_done if terminal is not None else None,
    )
    if terminal is not None:
        gateway.attach_outbound(terminal)

    feishu: Optional[FeishuChannel] = None
    if "feishu" in channels:
        feishu = FeishuChannel(
            app_id=cfg.feishu_app_id,
            app_secret=cfg.feishu_app_secret,
            bot_open_id=cfg.feishu_bot_open_id,
            on_message=gateway.handle,
        )
        gateway.attach_outbound(feishu)

    return Runtime(gateway=gateway, terminal=terminal, feishu=feishu)
