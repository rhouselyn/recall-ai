"""R02 / R03：HTML、CSV、单链接导入与浏览器书签树。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from recall import RecallTools
from recall.clock import freeze
from recall.db import init_db, open_readwrite
from recall.errors import RecallError
from recall.ingest import (
    add_link,
    import_bookmark_tree,
    import_bookmarks_html,
    import_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"


class ImportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "recall.db"
        init_db(self.db)
        freeze("2026-11-10T09:00:00Z")

    def tearDown(self) -> None:
        freeze(None)
        self.tmp.cleanup()

    def tools(self, **kwargs):
        return RecallTools.open(str(self.db), **kwargs)

    def test_chrome_html_nesting_entities_time_and_dedupe(self) -> None:
        first = import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["counts"]["entries_added"], 5)
        # 5 个目录里只有「空目录」没有条目；新建夹数与空目录数分开统计。
        self.assertEqual(first["counts"]["collections_added"], 4)
        self.assertEqual(first["counts"]["empty_collections"], 1)
        second = import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        self.assertEqual(second["counts"]["entries_added"], 0)
        self.assertEqual(second["counts"]["items_added"], 0)
        self.assertEqual(second["counts"]["collections_added"], 0)
        self.assertEqual(second["counts"]["empty_collections"], 1)

        with self.tools() as t:
            collections = t.recall_list_collections()["collections"]
            paths = {tuple(c["path"]) for c in collections}
            self.assertIn(("书签栏", "市场研究"), paths)
            self.assertIn(("书签栏", "空目录"), paths)  # 空目录保留
            result = t.recall_search(query="日本 电商")
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["items"][0]["title"], "日本电商消费者调研 & 渠道")
            self.assertEqual(result["items"][0]["saved_at"], "2023-10-09T06:30:00Z")
            self.assertEqual(len(t.recall_search(query="C++")["items"]), 1)
            self.assertEqual(len(t.recall_search(query="%")["items"]), 0)
            # 同一链接在两个文件夹：一个内容项，两条证据。
            item_id = result["items"][0]["item_id"]
            detail = t.recall_get_item(item_id)
            self.assertEqual(len(detail["evidence"]), 2)

        conn = open_readwrite(self.db)
        try:
            all_urls = " ".join(
                row[0] for row in conn.execute("SELECT link_url FROM entries")
            )
            all_titles = " ".join(
                row[0] or "" for row in conn.execute("SELECT title FROM entries")
            )
            self.assertNotIn("TEST_SECRET_VALUE", all_urls)
            self.assertNotIn("utm_source", all_urls)
            self.assertIn("ref=keep", all_urls)  # 未知业务参数保留
            self.assertNotIn("javascript:", all_urls)
            self.assertNotIn("__should_not_be_executed__", all_titles)
        finally:
            conn.close()

    def test_replace_truncated_keeps_old_data(self) -> None:
        import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        with self.assertRaises(RecallError) as ctx:
            import_bookmarks_html(
                self.db, FIXTURES / "truncated_bookmarks.html",
                "chrome-personal", "chrome", replace=True,
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.tools() as t:
            self.assertEqual(len(t.recall_search(query="日本")["items"]), 1)

    def test_replace_with_real_export(self) -> None:
        import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        report = import_bookmarks_html(
            self.db, FIXTURES / "edge_favorites.html", "chrome-personal", "chrome",
            replace=True,
        )
        self.assertEqual(report["coverage"], "complete_export")
        self.assertGreater(report["counts"]["entries_deactivated"], 0)
        with self.tools() as t:
            self.assertEqual(t.recall_search(query="C++")["items"], [])
            self.assertEqual(len(t.recall_search(query="日本 电商")["items"]), 1)

    def test_csv_exchange_format_and_partial_rows(self) -> None:
        report = import_csv(self.db, FIXTURES / "links.csv", "manual-all", "web")
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["counts"]["entries_added"], 5)
        with self.tools() as t:
            # 没有标题时展示主机和路径，不冒充平台标题。
            result = t.recall_search(query="example.com/no-title")
            self.assertEqual(result["items"][0]["title"], "example.com/no-title")
            self.assertEqual(
                t.recall_search(query="知乎收藏样本")["items"], []
            )  # 默认不开放受限
        with self.tools(include_restricted=True) as t:
            self.assertEqual(len(t.recall_search(query="知乎收藏样本")["items"]), 1)

        partial = import_csv(self.db, FIXTURES / "unsafe_links.csv", "csv-unsafe", "web")
        self.assertEqual(partial["status"], "partial")
        self.assertGreaterEqual(partial["counts"]["excluded_unsafe"], 2)
        self.assertTrue(any(e["code"] == "CREDENTIALS_IN_URL" for e in partial["row_errors"]))
        conn = open_readwrite(self.db)
        try:
            urls = " ".join(r[0] for r in conn.execute("SELECT link_url FROM entries"))
            self.assertNotIn("password", urls)
        finally:
            conn.close()

    def test_bad_header_fails_whole_file(self) -> None:
        with self.assertRaises(RecallError):
            import_csv(self.db, FIXTURES / "bad_header.csv", "csv-bad", "web")

    def test_add_link_is_idempotent_and_cross_source_dedupes(self) -> None:
        add_link(
            self.db, "https://example.com/jp-market", "web-manual", "web",
            title="日本电商调研", collection="跨境电商",
        )
        add_link(
            self.db, "https://example.com/jp-market", "web-manual", "web",
            title="日本电商调研", collection="跨境电商",
        )
        import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        with self.tools() as t:
            result = t.recall_search(query="日本 电商")
            self.assertEqual(len(result["items"]), 1)
            item = result["items"][0]
            # 搜索只展示命中的证据；详情返回同一内容项的全部可见证据。
            self.assertEqual(len(item["sources"]), 2)
            detail = t.recall_get_item(item["item_id"])
            self.assertEqual(len(detail["evidence"]), 3)
            edge = t.recall_search(query="日本 电商", source_ids=["chrome-personal"])
            self.assertEqual(
                {s["source_id"] for s in edge["items"][0]["sources"]}, {"chrome-personal"}
            )

    def test_bookmark_tree_binding_and_monotonic_version(self) -> None:
        payload = {
            "profile_uuid": "profile-test-0001",
            "version": 2,
            "browser": "chrome",
            "nodes": [
                {"id": "0", "title": "root", "parentId": ""},
                {"id": "1", "title": "书签栏", "parentId": "0"},
                {"id": "2", "title": "日本资料", "parentId": "1"},
                {"id": "3", "title": "日本电商调研", "parentId": "2",
                 "url": "https://example.com/jp-market", "dateAdded": 1696833000000},
                {"id": "4", "title": "空夹", "parentId": "1"},
            ],
        }
        ok = import_bookmark_tree(self.db, payload, "chrome-live")
        self.assertTrue(ok["ok"])
        payload["version"] = 1
        stale = import_bookmark_tree(self.db, payload, "chrome-live")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["code"], "STALE_CURSOR")
        with self.tools() as t:
            self.assertEqual(len(t.recall_search(query="日本 电商")["items"]), 1)
            self.assertEqual(
                t.recall_list_sources()["sources"][0]["coverage"], "live_profile"
            )
            # 书签落到所在目录，不能把它自己的标题当成夹名。
            paths = {tuple(c["path"]) for c in t.recall_list_collections()["collections"]}
            self.assertIn(("书签栏", "日本资料"), paths)
            self.assertIn(("书签栏", "空夹"), paths)
            self.assertNotIn(("书签栏", "日本资料", "日本电商调研"), paths)
            item = t.recall_search(query="日本 电商")["items"][0]
            self.assertEqual(
                item["sources"][0]["path"], ["书签栏", "日本资料"]
            )
        mismatch = dict(payload, version=3, profile_uuid="another-profile")
        self.assertEqual(
            import_bookmark_tree(self.db, mismatch, "chrome-live")["error"]["code"],
            "ACCOUNT_MISMATCH",
        )

    def test_readonly_query_never_creates_empty_db(self) -> None:
        missing = Path(self.tmp.name) / "missing.db"
        with self.assertRaises(RecallError) as ctx:
            RecallTools.open(str(missing))
        self.assertEqual(ctx.exception.code, "DB_NOT_READY")
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
