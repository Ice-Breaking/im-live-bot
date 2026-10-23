"""组合根：装配配置、业务能力、网关与飞书通道，主线程跑长连接。

同一时刻只允许一个实例：第二个实例拿不到文件锁会直接退出。
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from typing import Optional

import config  # 必须先于 lark_oapi 导入，用于修正根证书路径
from chat_agent import ChatAgent
from feishu_channel import FeishuChannel
from gateway import Gateway
from llm_client import StepFunClient

logger = logging.getLogger("im_live")


def acquire_single_instance_lock(lock_path: Path) -> Optional[object]:
    """占有单实例文件锁，已被占用时返回 None。

    飞书同一应用存在多条长连接时，事件会被分走，表现为机器人不回复，
    因此必须只允许一个实例运行。
    """
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def main() -> None:
    """加载配置、装配各层并启动飞书机器人。"""
    cfg = config.load_config()
    config.setup_logging(cfg.log_path)

    invalid_ids = [item for item in cfg.allowed_open_ids if not item.startswith("ou_")]
    if invalid_ids:
        logger.warning(
            "ALLOWED_OPEN_IDS 中存在非 ou_ 开头的条目 %s；飞书用户 open_id 形如 ou_xxxx，"
            "未授权账号发消息时机器人会回带其真实 open_id",
            invalid_ids,
        )

    logger.info(
        "配置加载完成: model=%s base_url=%s 白名单=%d 条",
        cfg.step_model,
        cfg.step_base_url,
        len(cfg.allowed_open_ids),
    )

    lock = acquire_single_instance_lock(cfg.lock_path)
    if lock is None:
        logger.error("已有实例在运行（锁文件 %s），请先停掉再启动", cfg.lock_path)
        return

    agent = ChatAgent(StepFunClient(cfg.step_api_key, cfg.step_base_url, cfg.step_model))
    gateway = Gateway(agent, cfg.allowed_open_ids)
    channel = FeishuChannel(
        app_id=cfg.feishu_app_id,
        app_secret=cfg.feishu_app_secret,
        bot_open_id=cfg.feishu_bot_open_id,
        on_message=gateway.handle,
    )
    gateway.attach_outbound(channel)

    logger.info("飞书机器人启动中，等待消息")
    channel.start()


if __name__ == "__main__":
    main()
