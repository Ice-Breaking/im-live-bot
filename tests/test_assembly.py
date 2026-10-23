"""装配特征：启用的入口都挂到同一个网关，答案按来源路由回去。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from assembly import build_runtime
from gateway import Gateway
from tests.support import QuietLoggingTestCase, incoming

BASE_ENV = {
    "FEISHU_APP_ID": "cli_test",
    "FEISHU_APP_SECRET": "secret_test",
    "FEISHU_BOT_OPEN_ID": "ou_bot",
    "STEP_API_KEY": "key_test",
    "STEP_BASE_URL": "https://api.stepfun.com/step_plan/v1",
    "STEP_MODEL": "step-3.7-flash",
    "ALLOWED_OPEN_IDS": "ou_a",
}


class AssemblyTest(QuietLoggingTestCase):
    def load_config(self, **overrides) -> config.Config:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = {**BASE_ENV, **overrides}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(
            config, "ROOT_DIR", root
        ), mock.patch.object(config, "LOG_DIR", root / "logs"), mock.patch.object(
            config, "DATA_DIR", root / "data"
        ):
            return config.load_config()

    def test_terminal_only(self) -> None:
        cfg = self.load_config(ENABLED_CHANNELS="terminal")
        runtime = build_runtime(cfg, cfg.enabled_channels)
        self.assertIsInstance(runtime.gateway, Gateway)
        self.assertIsNone(runtime.feishu)
        self.assertEqual(list(runtime.gateway.outbounds), ["terminal"])
        self.assertIs(
            runtime.gateway._outbound_for(incoming(channel="terminal")), runtime.terminal
        )

    def test_both_channels_share_one_gateway(self) -> None:
        cfg = self.load_config(ENABLED_CHANNELS="feishu,terminal")
        runtime = build_runtime(cfg, cfg.enabled_channels)
        self.assertEqual(sorted(runtime.gateway.outbounds), ["feishu", "terminal"])
        self.assertIs(
            runtime.gateway._outbound_for(incoming(channel="feishu")), runtime.feishu
        )
        self.assertIs(
            runtime.gateway._outbound_for(incoming(channel="terminal")), runtime.terminal
        )

    def test_terminal_identity_is_allowed(self) -> None:
        cfg = self.load_config(ENABLED_CHANNELS="terminal")
        runtime = build_runtime(cfg, cfg.enabled_channels)
        self.assertIn(cfg.terminal_user_open_id, runtime.gateway.allowed_open_ids)

    def test_terminal_waits_on_task_done(self) -> None:
        cfg = self.load_config(ENABLED_CHANNELS="terminal")
        runtime = build_runtime(cfg, cfg.enabled_channels)
        self.assertIsNotNone(runtime.gateway.on_task_done)

    def test_feishu_only_has_no_terminal_callback(self) -> None:
        cfg = self.load_config(ENABLED_CHANNELS="feishu")
        runtime = build_runtime(cfg, cfg.enabled_channels)
        self.assertIsNone(runtime.terminal)
        self.assertIsNone(runtime.gateway.on_task_done)


if __name__ == "__main__":
    unittest.main()
