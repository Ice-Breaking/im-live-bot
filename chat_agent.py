"""问答业务能力：把用户的问题交给模型，返回答案。

不认识飞书、不认识会话，只暴露 answer(question) -> str，
因此换接入渠道不需要改这里；换提示词或换模型风格也只改本文件。
"""

from __future__ import annotations

import logging

from llm_client import StepFunClient

logger = logging.getLogger(__name__)

# 单条提问的长度上限，超过即拒绝，避免超长输入打爆模型上下文
MAX_QUESTION_CHARS = 8000

PROMPT_TEMPLATE = """你是飞书机器人助手。请直接、简洁地回答用户的问题。

用户的问题：
{question}

输出要求：
1. 用中文回答，先给结论，再给必要的理由或步骤；
2. 信息不足时说明缺什么，不要编造事实；
3. 不要复述用户的问题原文。"""


class ChatInputError(ValueError):
    """提问内容不合法。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChatAgent:
    """问答：拼装提示词并调用模型。"""

    def __init__(self, client: StepFunClient) -> None:
        self.client = client

    def answer(self, question: str) -> str:
        """回答一个问题。

        :raises ChatInputError: 提问为空或过长
        :raises ModelCallError: 模型调用失败
        """
        if not question.strip():
            raise ChatInputError("EMPTY_QUESTION", "消息内容为空，请直接把想问的内容发给我。")
        if len(question) > MAX_QUESTION_CHARS:
            raise ChatInputError(
                "QUESTION_TOO_LONG",
                f"消息 {len(question)} 字，超过 {MAX_QUESTION_CHARS} 字上限，请拆分后发送。",
            )
        prompt = PROMPT_TEMPLATE.format(question=question)
        logger.info("开始回答: 问题长度=%d", len(question))
        return self.client.chat(prompt)
