"""真实模型冒烟：默认跳过，设 IM_LIVE_REAL_MODEL=1 才跑。

日常套件必须能在无密钥、无网络的环境下跑完，真实调用只在人工验收时打开：
    IM_LIVE_REAL_MODEL=1 python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import time
import unittest

import config
from chat_agent import ChatAgent
from gateway import Gateway
from llm_client import StepFunClient
from task_log import ANALYZED, RECEIVED, SENT
from tests.support import FakeChannel, temp_task_log, wait_until

REAL_MODEL_ENABLED = os.environ.get("IM_LIVE_REAL_MODEL") == "1"


@unittest.skipUnless(REAL_MODEL_ENABLED, "需要 IM_LIVE_REAL_MODEL=1 才跑真实模型")
class RealModelTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config.load_config()
        self.agent = ChatAgent(
            StepFunClient(self.cfg.step_api_key, self.cfg.step_base_url, self.cfg.step_model)
        )

    def test_real_answer_is_not_empty(self) -> None:
        started = time.time()
        answer = self.agent.answer("用一句话说明长连接机器人的优点")
        self.assertTrue(answer.strip())
        self.assertLess(time.time() - started, 180)

    def test_real_answer_reaches_channel(self) -> None:
        channel = FakeChannel()
        task_log = temp_task_log(self)
        gateway = Gateway(self.agent, self.cfg.allowed_open_ids, task_log)
        gateway.attach_outbound(channel)

        from tests.support import incoming

        task_id = gateway.handle(incoming(channel="fake", user_open_id=self.cfg.allowed_open_ids[0]))
        self.assertTrue(wait_until(lambda: SENT in [row["stage"] for row in task_log.events_of(task_id)], timeout=60))

        stages = [row["stage"] for row in task_log.events_of(task_id)]
        self.assertEqual(stages[:3], [RECEIVED, "开始分析", ANALYZED])
        self.assertEqual(stages[-1], SENT)
        self.assertEqual(len(channel.sent), 1)
        self.assertTrue(channel.sent[0][1].strip())


if __name__ == "__main__":
    unittest.main()
