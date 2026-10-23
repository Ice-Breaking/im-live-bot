"""结果缓存特征：去重登记、结果保存、容量滚动淘汰。"""

from __future__ import annotations

import unittest

from result_store import ResultStore


class ResultStoreTest(unittest.TestCase):
    def test_seen_and_mark_seen(self) -> None:
        store = ResultStore(2)
        self.assertFalse(store.seen("om_1"))
        store.mark_seen("om_1")
        self.assertTrue(store.seen("om_1"))

    def test_remember_and_read_back(self) -> None:
        store = ResultStore(2)
        store.remember("om_1", "答案一")
        self.assertEqual(store.result_for("om_1"), "答案一")
        self.assertIsNone(store.result_for("om_missing"))

    def test_oldest_result_is_evicted(self) -> None:
        store = ResultStore(2)
        store.remember("om_1", "答案一")
        store.remember("om_2", "答案二")
        store.remember("om_3", "答案三")
        self.assertIsNone(store.result_for("om_1"))
        self.assertEqual(store.result_for("om_2"), "答案二")
        self.assertEqual(store.result_for("om_3"), "答案三")

    def test_seen_cache_keeps_only_recent(self) -> None:
        store = ResultStore(2)
        for message_id in ("om_1", "om_2", "om_3"):
            store.mark_seen(message_id)
        self.assertFalse(store.seen("om_1"))
        self.assertTrue(store.seen("om_2"))
        self.assertTrue(store.seen("om_3"))

    def test_remembering_again_keeps_latest(self) -> None:
        store = ResultStore(2)
        store.remember("om_1", "旧答案")
        store.remember("om_1", "新答案")
        self.assertEqual(store.result_for("om_1"), "新答案")


if __name__ == "__main__":
    unittest.main()
