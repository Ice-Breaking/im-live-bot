"""StepFun 模型通道：通过 OpenAI 兼容端点把提示词换成模型输出。

只负责一次对话调用，不含业务语义；网络异常、鉴权失败、返回为空都统一包装成
ModelCallError，由 Gateway 转成用户能看懂的回复。

连接类失败会重建底层客户端再试一次：本地实测断网后旧客户端一直 `Errno 61`
直接拒连，只有重启进程才恢复；重建客户端能把这类连接状态清掉。超时不重试，
否则用户要等两倍超时时长。
"""

from __future__ import annotations

import logging
import time

from openai import APIConnectionError, APITimeoutError, OpenAI

logger = logging.getLogger(__name__)


class ModelCallError(RuntimeError):
    """模型调用失败。"""


class StepFunClient:
    """阶跃星辰模型客户端，接入方式与 OpenAI 兼容。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 180.0) -> None:
        self.model = model
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self.client = self._build_client()

    def _build_client(self) -> OpenAI:
        """新建底层客户端：整层换掉，旧连接池不会残留。"""
        return OpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout)

    def _create(self, prompt: str):
        """发一次对话请求，返回原始 completion。"""
        return self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

    def chat(self, prompt: str) -> str:
        """把提示词交给模型，返回输出文本。

        :raises ModelCallError: 调用失败或返回为空
        """
        started = time.perf_counter()
        logger.info("模型调用开始: model=%s 提示词长度=%d", self.model, len(prompt))
        completion = self._complete(prompt)

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

    def _complete(self, prompt: str):
        """调用模型：连接类失败重建客户端重试一次，超时不重试。"""
        try:
            return self._create(prompt)
        except APITimeoutError as exc:
            logger.exception("模型调用超时: model=%s", self.model)
            raise ModelCallError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc
        except APIConnectionError as exc:
            logger.warning("模型连接失败，重建客户端后重试一次: %s", exc)
            self.client = self._build_client()
        except Exception as exc:
            logger.exception("模型调用异常: model=%s", self.model)
            raise ModelCallError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        try:
            return self._create(prompt)
        except Exception as exc:
            logger.exception("模型调用重试仍失败: model=%s", self.model)
            raise ModelCallError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc
