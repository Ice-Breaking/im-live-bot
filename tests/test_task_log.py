"""任务记录特征：编号、落盘字段、按编号回看、正文不进日志。"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path

from task_log import TaskLog
from task_stage import ANALYZED, RECEIVED, SENT
from tests.support import QuietLoggingTestCase, temp_task_log


class TaskLogTest(QuietLoggingTestCase):
    def test_first_id_is_T00001(self) -> None:
        task_log = temp_task_log(self)
        self.assertEqual(task_log.new_task("fake", "om_1", "ou_allowed"), "T00001")

    def test_ids_continue_after_restart(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tasks.jsonl"

        first_run = TaskLog(path)
        first_run.new_task("fake", "om_1", "ou_a")
        first_run.record("T00001", RECEIVED)
        first_run.new_task("fake", "om_2", "ou_a")
        first_run.record("T00002", SENT)

        restarted = TaskLog(path)
        self.assertEqual(restarted.new_task("fake", "om_3", "ou_a"), "T00003")

    def test_record_writes_expected_fields(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_allowed")
        task_log.record(task_id, RECEIVED, detail="chat_type=p2p 文本长度=2")

        row = task_log.events_of(task_id)[0]
        self.assertEqual(
            set(row),
            {
                "ts",
                "task_id",
                "stage",
                "channel",
                "message_id",
                "user_open_id",
                "detail",
                "elapsed_ms",
                "answer_chars",
            },
        )
        self.assertEqual(row["channel"], "fake")
        self.assertEqual(row["message_id"], "om_1")
        self.assertEqual(row["user_open_id"], "ou_allowed")
        self.assertEqual(row["detail"], "chat_type=p2p 文本长度=2")
        self.assertIsInstance(row["elapsed_ms"], int)

    def test_answer_text_stays_in_file_only(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_allowed")

        with self.assertLogs("im_live.task", level="INFO") as captured:
            task_log.record(task_id, ANALYZED, detail="结果长度=4", answer="秘密答案")

        row = task_log.events_of(task_id)[0]
        self.assertEqual(row["answer_chars"], 4)
        self.assertEqual(row["answer"], "秘密答案")
        self.assertNotIn("秘密答案", "\n".join(captured.output))

    def test_show_lists_stages(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_allowed")
        task_log.record(task_id, RECEIVED, detail="文本长度=2")
        task_log.record(task_id, SENT, detail="分段=1")

        shown = task_log.show(task_id)
        self.assertIn(f"任务 {task_id}  渠道=fake  消息=om_1  发送人=ou_allowed", shown)
        self.assertIn(RECEIVED, shown)
        self.assertIn(SENT, shown)
        self.assertIn("分段=1", shown)

    def test_show_reports_missing_task(self) -> None:
        task_log = temp_task_log(self)
        self.assertIn("没有找到任务 T99999 的记录", task_log.show("T99999"))

    def test_concurrent_records_are_not_interleaved(self) -> None:
        task_log = temp_task_log(self)
        task_id = task_log.new_task("fake", "om_1", "ou_allowed")

        def write_many() -> None:
            for _ in range(20):
                task_log.record(task_id, SENT, detail="分段=1")

        threads = [threading.Thread(target=write_many) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = [line for line in task_log.path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 100)
        for line in lines:
            self.assertEqual(json.loads(line)["task_id"], task_id)


if __name__ == "__main__":
    unittest.main()
