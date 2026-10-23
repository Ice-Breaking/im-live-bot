"""任务阶段与编号：记录模块与编排模块共用的一套标识。

阶段名是中文且用户可见（日志与 `--task` 输出都用它们），改动等于改变输出，
因此单独放在这里，让 task_log 与 gateway 依赖它，彼此不再互相依赖。
"""

from __future__ import annotations

# 阶段名：日志与记录文件里都用它，保持人能直接看懂
RECEIVED = "收到消息"
ANALYZING = "开始分析"
ANALYZED = "分析完成"
SENT = "回复发出"
SEND_FAILED = "回复失败"
RETRYING = "重试发送"
REUSED = "复用结果重发"
ANALYZE_FAILED = "分析失败"
REJECTED = "拒绝未授权"
SKIPPED = "忽略"

TASK_ID_FORMAT = "T{:05d}"


class TaskIdAllocator:
    """任务编号分配：从给定序号往后发。

    起始序号由记录文件末行决定，重启后接着发，新旧记录不会撞号。
    """

    def __init__(self, start_seq: int = 0) -> None:
        self._seq = start_seq

    def next_id(self) -> str:
        """返回下一个任务编号，形如 T00007。"""
        self._seq += 1
        return TASK_ID_FORMAT.format(self._seq)
