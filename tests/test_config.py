"""配置特征：校验规则、入口解析、路径约定、凭证脱敏。"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config

BASE_ENV = {
    "FEISHU_APP_ID": "cli_test",
    "FEISHU_APP_SECRET": "secret_test",
    "FEISHU_BOT_OPEN_ID": "ou_bot",
    "STEP_API_KEY": "key_test",
    "STEP_BASE_URL": "https://api.stepfun.com/step_plan/v1",
    "STEP_MODEL": "step-3.7-flash",
    "ALLOWED_OPEN_IDS": "ou_a, ou_b",
}


class ConfigTest(unittest.TestCase):
    def load_with(self, env: dict) -> config.Config:
        """在临时根目录里加载配置：不读项目 .env，也不建真实目录。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            config, "ROOT_DIR", root
        ), mock.patch.object(config, "LOG_DIR", root / "logs"), mock.patch.object(
            config, "DATA_DIR", root / "data"
        ):
            return config.load_config()

    def test_missing_keys_are_reported(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self.load_with({})
        self.assertIn("FEISHU_APP_ID", str(ctx.exception))

    def test_empty_whitelist_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self.load_with({**BASE_ENV, "ALLOWED_OPEN_IDS": "  "})
        self.assertIn("ALLOWED_OPEN_IDS", str(ctx.exception))

    def test_whitelist_and_terminal_identity(self) -> None:
        cfg = self.load_with(BASE_ENV)
        self.assertEqual(cfg.allowed_open_ids, ("ou_a", "ou_b", "ou_terminal"))

    def test_terminal_identity_is_not_duplicated(self) -> None:
        cfg = self.load_with({**BASE_ENV, "ALLOWED_OPEN_IDS": "ou_terminal"})
        self.assertEqual(cfg.allowed_open_ids, ("ou_terminal",))

    def test_default_channels(self) -> None:
        cfg = self.load_with(BASE_ENV)
        self.assertEqual(cfg.enabled_channels, ("feishu",))

    def test_channels_from_env(self) -> None:
        cfg = self.load_with({**BASE_ENV, "ENABLED_CHANNELS": "feishu,terminal"})
        self.assertEqual(cfg.enabled_channels, ("feishu", "terminal"))

    def test_parse_channels_rejects_empty(self) -> None:
        with self.assertRaises(RuntimeError):
            config.parse_channels(" , ")

    def test_parse_channels_rejects_unknown(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            config.parse_channels("feishu,sms")
        self.assertIn("sms", str(ctx.exception))

    def test_known_channel_names(self) -> None:
        self.assertEqual(config.CHANNEL_NAMES, ("feishu", "terminal"))

    def test_file_paths(self) -> None:
        cfg = self.load_with(BASE_ENV)
        self.assertEqual(cfg.log_path.name, "im_live.log")
        self.assertEqual(cfg.lock_path.name, "bot.lock")
        self.assertEqual(cfg.task_log_path.name, "tasks.jsonl")

    def test_credentials_are_redacted(self) -> None:
        record = logging.LogRecord(
            "lark", logging.INFO, "f", 1, "wss://host/ws?access_key=abc&ticket=xyz", None, None
        )
        self.assertTrue(config.CredentialRedactingFilter().filter(record))
        self.assertEqual(record.getMessage(), "wss://host/ws?access_key=***&ticket=***")


if __name__ == "__main__":
    unittest.main()
