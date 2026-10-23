"""统一配置：先修正根证书路径，再从项目根 .env 加载并校验运行所需配置。

导入顺序约定：本模块必须先于 lark_oapi 被导入。
本机 OpenSSL 的默认信任路径不是有效的根证书包，不修正会让 WebSocket 握手报
CERTIFICATE_VERIFY_FAILED。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import certifi
from dotenv import load_dotenv

# 把进程默认根证书显式指向 certifi 的证书包，消除握手时的证书校验失败场景
os.environ["SSL_CERT_FILE"] = certifi.where()

ROOT_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"

# 缺任一项都直接终止启动，不带着不完整配置运行
REQUIRED_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_BOT_OPEN_ID",
    "STEP_API_KEY",
    "STEP_BASE_URL",
    "STEP_MODEL",
    "ALLOWED_OPEN_IDS",
)

# 可选配置：启用哪些入口。不配时只跑飞书；写错直接报错，不静默降级
CHANNEL_NAMES = ("feishu", "terminal")
DEFAULT_CHANNELS = ("feishu",)
DEFAULT_TERMINAL_USER_OPEN_ID = "ou_terminal"


def parse_channels(raw: str) -> tuple[str, ...]:
    """解析入口列表：逗号分隔，只接受已知渠道名。

    :raises RuntimeError: 列表为空或含未知渠道名时抛出
    """
    channels = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not channels:
        raise RuntimeError("ENABLED_CHANNELS 解析后为空，至少启用一个入口：feishu 或 terminal")
    unknown = [item for item in channels if item not in CHANNEL_NAMES]
    if unknown:
        raise RuntimeError(
            f"ENABLED_CHANNELS 含未知入口 {unknown}，可用入口：{', '.join(CHANNEL_NAMES)}"
        )
    return channels


# SDK 会把长连接地址原样打进日志，里面带着连接凭证，出口统一替换掉
SECRET_PATTERN = re.compile(r"\b(access_key|ticket)=[^&\s]+")


class CredentialRedactingFilter(logging.Filter):
    """把日志里的 access_key 与 ticket 换成 ***。

    日志形如 wss://...?...&access_key=xxx&...&ticket=xxx，这两项是真实连接凭证，
    落到 logs/im_live.log 等于泄漏。挂在 handler 上可覆盖所有 logger，包括 SDK 自己的。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = SECRET_PATTERN.sub(r"\1=***", record.msg)
        return True


@dataclass(frozen=True)
class Config:
    """运行所需的全部配置，加载后不可变。"""

    feishu_app_id: str
    feishu_app_secret: str
    feishu_bot_open_id: str
    step_api_key: str
    step_base_url: str
    step_model: str
    allowed_open_ids: tuple[str, ...]
    enabled_channels: tuple[str, ...]
    terminal_user_open_id: str
    log_path: Path
    lock_path: Path
    task_log_path: Path


def read_required_env() -> dict[str, str]:
    """读项目根 .env 与环境变量，缺任一项直接终止启动。

    :raises RuntimeError: 缺少必填项时抛出
    """
    load_dotenv(ROOT_DIR / ".env")
    values = {key: os.environ.get(key, "") for key in REQUIRED_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f".env 缺少配置：{', '.join(missing)}，请在项目根 .env 中补齐")
    return values


def parse_open_ids(raw: str, terminal_open_id: str) -> tuple[str, ...]:
    """解析白名单，并把终端身份并入。

    终端是本机入口，默认可信，不需要在 ALLOWED_OPEN_IDS 里重复配置。

    :raises RuntimeError: 白名单解析为空时抛出
    """
    allowed = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not allowed:
        raise RuntimeError("ALLOWED_OPEN_IDS 解析后为空，请填入至少一个飞书用户 open_id")
    return allowed if terminal_open_id in allowed else allowed + (terminal_open_id,)


def load_config() -> Config:
    """加载并校验配置：读环境 → 解析白名单与入口 → 组装 Config。"""
    env = read_required_env()
    terminal_user_open_id = os.environ.get(
        "TERMINAL_USER_OPEN_ID", DEFAULT_TERMINAL_USER_OPEN_ID
    ).strip()
    allowed_open_ids = parse_open_ids(env["ALLOWED_OPEN_IDS"], terminal_user_open_id)
    enabled_channels = parse_channels(os.environ.get("ENABLED_CHANNELS", ",".join(DEFAULT_CHANNELS)))

    LOG_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    return Config(
        feishu_app_id=os.environ["FEISHU_APP_ID"],
        feishu_app_secret=os.environ["FEISHU_APP_SECRET"],
        feishu_bot_open_id=os.environ["FEISHU_BOT_OPEN_ID"],
        step_api_key=os.environ["STEP_API_KEY"],
        step_base_url=os.environ["STEP_BASE_URL"],
        step_model=os.environ["STEP_MODEL"],
        allowed_open_ids=allowed_open_ids,
        enabled_channels=enabled_channels,
        terminal_user_open_id=terminal_user_open_id,
        log_path=LOG_DIR / "im_live.log",
        lock_path=DATA_DIR / "bot.lock",
        task_log_path=DATA_DIR / "tasks.jsonl",
    )


def setup_logging(log_path: Path) -> None:
    """配置控制台与文件双输出日志。

    验收与排障靠它区分三段：消息有没有收到、分析有没有完成、回复有没有发出。
    """
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(CredentialRedactingFilter())
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(CredentialRedactingFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
