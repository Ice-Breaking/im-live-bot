"""StepFun 模型通道：通过 OpenAI 兼容端点把提示词换成模型输出。

只负责一次对话调用，不含业务语义；网络异常、鉴权失败、返回为空都统一包装成
ModelCallError，由 Gateway 转成用户能看懂的回复。
"""

from __future__ import annotations

import logging
import time

from openai import OpenAI

logger = logging.getLogger(__name__)


class ModelCallError(RuntimeError):
    """模型调用失败。"""


class StepFunClient:
    """阶跃星辰模型客户端，接入方式与 OpenAI 兼容。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 180.0) -> None:
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def chat(self, prompt: str) -> str:
        """把提示词交给模型，返回输出文本。

        :raises ModelCallError: 调用失败或返回为空
        """
        started = time.perf_counter()
        logger.info("模型调用开始: model=%s 提示词长度=%d", self.model, len(prompt))
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as exc:
            logger.exception("模型调用异常: model=%s", self.model)
            raise ModelCallError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        text = (completion.choices[0].message.content or "").strip()
        if not text:
            raise ModelCallError(f"模型返回为空: model={self.model}")

        logger.info(
            "模型调用完成: model=%s 耗时=%.1fs 输出长度=%d",
            self.model,
            time.perf_counter() - started,
            len(text),
        )
        return text
