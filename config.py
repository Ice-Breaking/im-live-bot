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
    log_path: Path
    lock_path: Path


def load_config() -> Config:
    """从项目根 .env 加载并校验配置。

    :raises RuntimeError: 缺项或白名单解析为空时抛出
    """
    load_dotenv(ROOT_DIR / ".env")

    missing = [key for key in REQUIRED_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f".env 缺少配置：{', '.join(missing)}，请在项目根 .env 中补齐")

    allowed = tuple(item.strip() for item in os.environ["ALLOWED_OPEN_IDS"].split(",") if item.strip())
    if not allowed:
        raise RuntimeError("ALLOWED_OPEN_IDS 解析后为空，请填入至少一个飞书用户 open_id")

    LOG_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    return Config(
        feishu_app_id=os.environ["FEISHU_APP_ID"],
        feishu_app_secret=os.environ["FEISHU_APP_SECRET"],
        feishu_bot_open_id=os.environ["FEISHU_BOT_OPEN_ID"],
        step_api_key=os.environ["STEP_API_KEY"],
        step_base_url=os.environ["STEP_BASE_URL"],
        step_model=os.environ["STEP_MODEL"],
        allowed_open_ids=allowed,
        log_path=LOG_DIR / "im_live.log",
        lock_path=DATA_DIR / "bot.lock",
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
