"""组合根：加载配置、按启用的入口启动服务。

入口由 .env 的 ENABLED_CHANNELS 选择，命令行 --channel 可临时覆盖；
--ask 只在终端问一次并退出。多个入口共用同一个网关与业务对象。

启用飞书入口时必须抢单实例锁：同一应用的多条长连接会分走事件，
表现为机器人不回复。
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import stat
import sys
import threading
from pathlib import Path
from typing import Optional

import config  # 必须先于 lark_oapi 导入，用于修正根证书路径
from assembly import build_runtime
from task_log import TaskLog

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


def stdin_can_repl() -> bool:
    """终端 REPL 需要能交互的 stdin：直接敲命令（tty）或管道喂输入都算。

    后台运行（nohup）时 stdin 指向 /dev/null，REPL 会立刻读到 EOF 退出，
    把同进程里的飞书入口一起带走，这种情况不进 REPL。
    """
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except OSError:
        return False
    return sys.stdin.isatty() or stat.S_ISFIFO(mode)


def parse_args() -> argparse.Namespace:
    """命令行参数：--channel 覆盖配置里的入口，--ask 单次提问后退出。"""
    parser = argparse.ArgumentParser(description="IM_live 机器人：飞书长连接入口与终端入口")
    parser.add_argument(
        "--channel",
        help=f"覆盖 .env 的 ENABLED_CHANNELS，逗号分隔：{', '.join(config.CHANNEL_NAMES)}",
    )
    parser.add_argument("--ask", help="只在终端问一次并退出，不进入交互循环，也不拉起飞书长连接")
    parser.add_argument("--task", help="按任务编号打印一次处理的全过程记录，不启动服务")
    return parser.parse_args()


def main() -> None:
    """加载配置、装配各层并启动启用的入口。"""
    args = parse_args()
    cfg = config.load_config()

    if args.task:
        print(TaskLog(cfg.task_log_path).show(args.task))
        return

    config.setup_logging(cfg.log_path)

    channels = cfg.enabled_channels
    if args.channel:
        channels = config.parse_channels(args.channel)
    if args.ask:
        channels = ("terminal",)

    invalid_ids = [item for item in cfg.allowed_open_ids if not item.startswith("ou_")]
    if invalid_ids:
        logger.warning(
            "ALLOWED_OPEN_IDS 中存在非 ou_ 开头的条目 %s；飞书用户 open_id 形如 ou_xxxx，"
            "未授权账号发消息时机器人会回带其真实 open_id",
            invalid_ids,
        )

    logger.info(
        "配置加载完成: model=%s base_url=%s 白名单=%d 条 启用入口=%s 任务记录=%s",
        cfg.step_model,
        cfg.step_base_url,
        len(cfg.allowed_open_ids),
        ",".join(channels),
        cfg.task_log_path,
    )

    if "feishu" in channels:
        lock = acquire_single_instance_lock(cfg.lock_path)
        if lock is None:
            logger.error("已有实例在运行（锁文件 %s），请先停掉再启动", cfg.lock_path)
            return

    runtime = build_runtime(cfg, channels)

    if runtime.feishu is not None:
        threading.Thread(target=runtime.feishu.start, name="feishu-ws", daemon=True).start()
        logger.info("飞书入口已启动，长连接建立中")

    if args.ask:
        runtime.terminal.ask_once(args.ask)
        return

    if runtime.terminal is not None and stdin_can_repl():
        runtime.terminal.start()
        return

    if runtime.terminal is not None:
        logger.warning("终端入口需要交互式 stdin（当前指向 /dev/null），已跳过 REPL")

    logger.info("入口已在后台运行，Ctrl-C 退出")
    threading.Event().wait()


if __name__ == "__main__":
    main()
