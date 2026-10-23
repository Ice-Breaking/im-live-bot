"""最近处理过的消息与结果：容量固定，按时间滚动淘汰。

结果留在内存，发送失败后同一条消息再来时可以直接重发，不必再调用模型。
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class ResultStore:
    """去重缓存与结果缓存，两者容量相同。"""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._seen: deque[str] = deque(maxlen=capacity)
        self._results: dict[str, str] = {}
        # 不用 maxlen：淘汰顺序由这里决定，交给 deque 会淘汰错条目并留下已经不在队里的旧结果
        self._result_order: deque[str] = deque()

    def seen(self, message_id: str) -> bool:
        """这条消息最近是不是处理过。"""
        return message_id in self._seen

    def mark_seen(self, message_id: str) -> None:
        """登记已处理：平台重复投递同一条事件时用来识别。"""
        self._seen.append(message_id)

    def remember(self, message_id: str, answer: str) -> None:
        """记下分析结果：发送失败后重发时复用。"""
        self._results[message_id] = answer
        self._result_order.append(message_id)
        while len(self._results) > self.capacity:
            oldest = self._result_order.popleft()
            self._results.pop(oldest, None)

    def result_for(self, message_id: str) -> Optional[str]:
        """取回上次的分析结果，没有则返回 None。"""
        return self._results.get(message_id)
