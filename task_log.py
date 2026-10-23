"""任务运行记录：给每次处理分配编号，按阶段留痕，支持按编号回看。

同一条记录写两处：
- 日志（终端 + logs/im_live.log）：只记编号、阶段、耗时、长度，不记消息与答案原文；
- data/tasks.jsonl：记完整字段，发送失败时把答案正文留下，便于重发而不重新分析。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from task_stage import ANALYZED, SENT, TaskIdAllocator

logger = logging.getLogger("im_live.task")


class TaskLog:
    """任务编号与阶段记录：写日志 + 追加 JSONL，可按编号回看。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 重启后接着历史编号往下发，避免新旧记录撞号
        self._ids = TaskIdAllocator(self._read_last_seq())
        self._meta: dict[str, dict] = {}
        self._last_time: dict[str, float] = {}

    def _read_last_seq(self) -> int:
        """读记录文件末行的编号，作为本次启动的起始序号。"""
        if not self.path.exists():
            return 0
        last_line = ""
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_line = line
        if not last_line:
            return 0
        try:
            return int(json.loads(last_line)["task_id"][1:])
        except (ValueError, KeyError):
            return 0

    def new_task(self, channel: str, message_id: str, user_open_id: str) -> str:
        """给一条入站消息分配编号，返回形如 T00007 的任务号。"""
        with self._lock:
            task_id = self._ids.next_id()
        self._meta[task_id] = {
            "channel": channel,
            "message_id": message_id,
            "user_open_id": user_open_id,
        }
        self._last_time[task_id] = time.time()
        return task_id

    def record(self, task_id: str, stage: str, detail: str = "", answer: str = "") -> None:
        """记一个阶段：写一行 JSON，并打一条不带原文的日志。"""
        now = time.time()
        started = self._last_time.get(task_id, now)
        elapsed_ms = int((now - started) * 1000)
        self._last_time[task_id] = now
        meta = self._meta.get(task_id, {})

        row = {
            "ts": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
            "task_id": task_id,
            "stage": stage,
            "channel": meta.get("channel", ""),
            "message_id": meta.get("message_id", ""),
            "user_open_id": meta.get("user_open_id", ""),
            "detail": detail,
            "elapsed_ms": elapsed_ms,
            "answer_chars": len(answer),
        }
        if answer:
            # 正文只落记录文件，供发送失败后重发，不进日志
            row["answer"] = answer

        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
                handle.flush()

        logger.info("任务 %s %s %s 耗时=%dms", task_id, stage, detail, elapsed_ms)

    def events_of(self, task_id: str) -> list[dict]:
        """按编号取回全部阶段记录，按时间顺序。"""
        if not self.path.exists():
            return []
        events = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("task_id") == task_id:
                    events.append(row)
        return events

    def pending_results(self, tail_lines: int = 2000) -> dict[str, str]:
        """取回「已分析但没发出」的结果，供重启后重发而不重新分析。

        只看记录末尾若干行：结果只在内存里留最近一批，扫全文没有意义。
        同一条消息以最后一次为准，后面出现「回复发出」就说明已经发出去了。
        """
        rows = self._read_tail(tail_lines)
        pending: dict[str, str] = {}
        for row in rows:
            message_id = row.get("message_id", "")
            if not message_id:
                continue
            stage = row.get("stage")
            if stage == ANALYZED and row.get("answer"):
                pending[message_id] = row["answer"]
            elif stage == SENT:
                pending.pop(message_id, None)
        return pending

    def _read_tail(self, tail_lines: int) -> list[dict]:
        """读记录末尾若干行，坏的或空的行跳过。"""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        rows = []
        for line in lines[-tail_lines:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def show(self, task_id: str) -> str:
        """把某个任务的全过程整理成人能看的几行，供 --task 使用。"""
        events = self.events_of(task_id)
        if not events:
            return f"没有找到任务 {task_id} 的记录（记录文件：{self.path}）"

        first = events[0]
        lines = [
            f"任务 {task_id}  渠道={first.get('channel', '')}  "
            f"消息={first.get('message_id', '')}  发送人={first.get('user_open_id', '')}"
        ]
        for event in events:
            stamp = event.get("ts", "")[11:23] or event.get("ts", "")
            detail = event.get("detail", "")
            elapsed = event.get("elapsed_ms", 0)
            lines.append(f"  {stamp}  {event.get('stage', '')}  {detail}  +{elapsed}ms")
        return "\n".join(lines)

    def forget(self, task_id: str) -> None:
        """任务结束后清掉进程内的登记，避免长时间运行累积。"""
        self._meta.pop(task_id, None)
        self._last_time.pop(task_id, None)
