"""R04 / R05 / R08 / R15：统一查询契约、权限、cursor、CLI、MCP 核心与媒体片段。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from recall import catalog
from recall.clock import freeze
from recall.db import backup_db, init_db, open_readwrite, restore_db, verify_db
from recall.errors import RecallError
from recall.ingest import import_bookmarks_html, import_csv
from recall.media import index_media
from recall.mcp_server import TOOL_DEFINITIONS, handle_tool_call, tools_from_env
from recall.native_host import handle as native_handle
from recall.tools import RecallTools
from recall.urls import sanitize_url

FIXTURES = Path(__file__).parent / "fixtures"


class ContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "recall.db"
        init_db(self.db)
        freeze("2026-11-10T09:00:00Z")
        import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        import_bookmarks_html(
            self.db, FIXTURES / "edge_favorites.html", "edge-work", "edge"
        )

    def tearDown(self) -> None:
        freeze(None)
        self.tmp.cleanup()

    def add_entry(self, source_id, url, *, title=None, path=(), access="standard", saved_at=None, segment=None):
        conn = open_readwrite(self.db)
        try:
            catalog.ensure_source(conn, source_id, "manual", "web", coverage="imported_subset")
            entry = catalog.EntryInput(
                safe=sanitize_url(url), title=title, saved_at=saved_at, path=path,
                external_key=sanitize_url(url).canonical_url,
                capture="manual_capture", access=access,
                collection_access=access,
            )
            if access == "restricted":
                result = catalog.merge_entries(conn, source_id, [entry])
                entry_id = conn.execute(
                    "SELECT e.entry_id FROM entries e JOIN collections c ON c.collection_id=e.collection_id "
                    "WHERE c.source_id=?", (source_id,)
                ).fetchone()[0]
            else:
                result = catalog.merge_entries(conn, source_id, [entry])
                entry_id = None
            if segment is not None:
                from recall.media.tasks import caption_segment
                with catalog.transaction(conn):
                    if entry_id is None:
                        entry_id = conn.execute(
                            "SELECT e.entry_id FROM entries e JOIN collections c "
                            "ON c.collection_id = e.collection_id WHERE c.source_id = ?",
                            (source_id,),
                        ).fetchone()[0]
                    caption_segment(conn, entry_id, segment, access=access)
            return result
        finally:
            conn.close()

    def test_regression_1_cross_source_dedupe_and_evidence(self) -> None:
        with RecallTools.open(str(self.db)) as tools:
            result = tools.recall_search(query="日本")
            urls = [item["url"] for item in result["items"]]
            self.assertEqual(urls.count("https://example.com/jp-market"), 1)
            item = next(i for i in result["items"] if i["url"] == "https://example.com/jp-market")
            detail = tools.recall_get_item(item["item_id"], limit=50)
            self.assertEqual(len(detail["evidence"]), 3)
            filtered = tools.recall_search(query="日本", source_ids=["edge-work"])
            sources = [s["source_id"] for s in filtered["items"][0]["sources"]]
            self.assertEqual(set(sources), {"edge-work"})

    def test_regression_2_restricted_source_cannot_leak(self) -> None:
        self.add_entry(
            "private-source", "https://example.com/jp-market",
            title="受限标题：供应链机密", access="restricted", segment="供应链 OCR",
        )
        allowed = {"chrome-personal", "edge-work"}
        with RecallTools.open(str(self.db), allowed_sources=allowed) as tools:
            result = tools.recall_search(query="供应链")
            self.assertEqual(result["items"], [])
            japanese = tools.recall_search(query="日本")
            self.assertEqual(len(japanese["items"]), 1)
            self.assertNotIn("受限标题", json.dumps(japanese, ensure_ascii=False))

            with self.assertRaises(RecallError) as ctx:
                tools.recall_get_item("itm_does_not_exist")
            self.assertEqual(ctx.exception.code, "NOT_FOUND")
            self.assertEqual(
                {c["source_id"] for c in tools.recall_list_collections()["collections"]}, allowed
            )
        with RecallTools.open(str(self.db)) as tools:
            all_collections = tools.recall_list_collections()["collections"]
            self.assertTrue(all(c["access"] == "standard" for c in all_collections))
        self.add_entry(
            "private-only", "https://example.com/only-private",
            title="仅受限来源", access="restricted",
        )
        conn = open_readwrite(self.db)
        item_id = conn.execute(
            "SELECT item_id FROM items WHERE canonical_url = ?",
            ("https://example.com/only-private",),
        ).fetchone()[0]
        conn.close()
        with RecallTools.open(str(self.db), allowed_sources=allowed) as tools:
            with self.assertRaises(RecallError) as ctx:
                tools.recall_get_item(item_id)
            self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_search_snippets_respect_source_filter(self) -> None:
        self.add_entry(
            "other-source", "https://example.com/jp-market",
            title="日本资料", segment="日本 隐藏来源的独有片段",
        )
        with RecallTools.open(str(self.db), allowed_sources={"chrome-personal"}) as tools:
            result = tools.recall_search(query="日本")
            self.assertNotIn("隐藏来源的独有片段", json.dumps(result, ensure_ascii=False))
        with RecallTools.open(str(self.db)) as tools:
            result = tools.recall_search(query="日本", source_ids=["edge-work"])
            self.assertNotIn("隐藏来源的独有片段", json.dumps(result, ensure_ascii=False))

    def test_regression_3_time_and_literal_queries(self) -> None:
        with RecallTools.open(str(self.db)) as tools:
            self.assertGreaterEqual(len(tools.recall_search(query="日本")["items"]), 1)
            self.assertGreaterEqual(len(tools.recall_search(query="日本 电商")["items"]), 1)
            self.assertEqual(len(tools.recall_search(query="C++")["items"]), 1)
            self.assertEqual(tools.recall_search(query="%")["items"], [])
            self.assertEqual(tools.recall_search(query="_")["items"], [])
            timed = tools.recall_search(saved_after="2026-01-01T00:00:00Z")
            self.assertEqual(timed["items"], [])  # 未知/早期收藏时间不进入区间
            self.assertEqual(len(timed["coverage"]), 2)
            with self.assertRaises(RecallError) as ctx:
                tools.recall_search(limit=51)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
            with self.assertRaises(RecallError) as ctx:
                tools.recall_search(query="词 " * 13)
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
            with self.assertRaises(RecallError) as ctx:
                tools.recall_search(source_ids=["not-open"])
            self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_regression_4_cursor_stability_and_stale(self) -> None:
        with RecallTools.open(str(self.db)) as tools:
            page1 = tools.recall_search(limit=1)
            self.assertIsNotNone(page1["next_cursor"])
            page2 = tools.recall_search(limit=1, cursor=page1["next_cursor"])
            ids = [i["item_id"] for i in page1["items"] + page2["items"]]
            self.assertEqual(len(ids), len(set(ids)))
            with self.assertRaises(RecallError) as ctx:
                tools.recall_get_item(page1["items"][0]["item_id"], cursor=page1["next_cursor"])
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.add_entry("new-source", "https://example.com/new", title="新增")
        with RecallTools.open(str(self.db)) as tools:
            with self.assertRaises(RecallError) as ctx:
                tools.recall_search(limit=1, cursor=page1["next_cursor"])
            self.assertEqual(ctx.exception.code, "STALE_CURSOR")

    def test_backup_restore_and_source_removal(self) -> None:
        backup = Path(self.tmp.name) / "backup.db"
        backup_db(self.db, backup)
        info = verify_db(backup)
        self.assertEqual(info["user_version"], 1)
        with self.assertRaises(RecallError):
            backup_db(self.db, backup)
        restored = Path(self.tmp.name) / "restored.db"
        restore_db(backup, restored)
        with RecallTools.open(str(restored)) as tools:
            self.assertEqual(len(tools.recall_list_sources()["sources"]), 2)
        conn = open_readwrite(self.db)
        removed = catalog.delete_source(conn, "edge-work")
        conn.close()
        self.assertGreater(removed["entries_removed"], 0)
        with RecallTools.open(str(self.db)) as tools:
            self.assertEqual(len(tools.recall_list_sources()["sources"]), 1)

    def test_media_segment_visibility(self) -> None:
        self.add_entry(
            "public-source", "https://example.com/public-note", title="公开笔记",
        )
        self.add_entry(
            "secret-source", "https://example.com/private-image", title="私密图片",
            access="restricted", segment="供应链 OCR 片段",
        )
        conn = open_readwrite(self.db)
        item_id = conn.execute(
            "SELECT item_id FROM items WHERE canonical_url = ?",
            ("https://example.com/public-note",),
        ).fetchone()[0]
        conn.close()
        # 注入式引擎，不依赖模型权重。
        report = index_media(
            self.db, item_id, FIXTURES / "links.csv",
            kind="ocr", engines={"ocr": lambda path: [{"text": "公开 OCR 供应链", "locator": {"image_no": 1}}]},
        )
        self.assertEqual(report["status"], "ready")
        with RecallTools.open(
            str(self.db), allowed_sources={"public-source"}
        ) as tools:
            result = tools.recall_search(query="供应链")
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["items"][0]["snippets"][0]["label"], "图片识别")
            self.assertEqual(len(tools.recall_get_item(item_id)["evidence"][0]["segments"]), 1)

    def test_native_host_message(self) -> None:
        payload = {
            "source_id": "chrome-live",
            "profile_uuid": "profile-contract",
            "version": 1,
            "browser": "chrome",
            "nodes": [
                {"id": "0", "title": "root", "parentId": ""},
                {"id": "1", "title": "收藏夹", "parentId": "0"},
                {"id": "2", "title": "书签", "parentId": "1",
                 "url": "https://example.com/from-extension"},
            ],
        }
        response = native_handle(payload, db_path=str(self.db))
        self.assertTrue(response["ok"])
        with RecallTools.open(str(self.db)) as tools:
            self.assertEqual(len(tools.recall_search(query="from-extension")["items"]), 1)


class CliMcpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "cli.db"
        self.env = patch.dict(os.environ, {"RECALL_DB": str(self.db)}, clear=False)
        self.env.start()
        freeze("2026-11-10T09:00:00Z")

    def tearDown(self) -> None:
        freeze(None)
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *argv: str) -> tuple[int, str]:
        from recall.cli import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.db), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_cli_end_to_end(self) -> None:
        code, output = self.cli("init")
        self.assertEqual(code, 0)
        code, output = self.cli(
            "import", "bookmarks", str(FIXTURES / "chrome_bookmarks.html"),
            "--source", "chrome-personal", "--browser", "chrome", "--json",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "succeeded")
        code, output = self.cli("search", "日本 电商", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)["items"]), 1)
        code, output = self.cli("search", "--limit", "99", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error"]["code"], "INVALID_ARGUMENT")
        code, output = self.cli("backup", str(Path(self.tmp.name) / "b.db"), "--json")
        self.assertEqual(code, 0)
        self.assertIn("backup", json.loads(output))

    def test_mcp_core_config_and_tool_calls(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RECALL_ALLOWED_SOURCES", None)
            with self.assertRaises(RecallError) as ctx:
                tools_from_env()
            self.assertEqual(ctx.exception.code, "CONFIG_ERROR")

        code, _ = self.cli("init")
        self.assertEqual(code, 0)
        import_bookmarks_html(
            self.db, FIXTURES / "chrome_bookmarks.html", "chrome-personal", "chrome"
        )
        names = [definition["name"] for definition in TOOL_DEFINITIONS]
        self.assertEqual(
            names,
            ["recall_list_sources", "recall_list_collections", "recall_search", "recall_get_item"],
        )
        self.assertTrue(all(d["annotations"]["readOnlyHint"] for d in TOOL_DEFINITIONS))
        with patch.dict(
            os.environ,
            {"RECALL_ALLOWED_SOURCES": "chrome-personal", "RECALL_DB": str(self.db)},
            clear=False,
        ):
            tools = tools_from_env()
        try:
            ok = handle_tool_call(tools, "recall_search", {"query": "日本 电商", "limit": 5})
            self.assertFalse(ok["isError"])
            self.assertEqual(len(ok["structuredContent"]["items"]), 1)
            self.assertIsInstance(json.loads(ok["text"]), dict)
            bad = handle_tool_call(tools, "recall_search", {"source_ids": ["edge-work"]})
            self.assertTrue(bad["isError"])
            self.assertEqual(bad["structuredContent"]["error"]["code"], "NOT_FOUND")
            unknown = handle_tool_call(tools, "not_a_tool", {})
            self.assertEqual(unknown["structuredContent"]["error"]["code"], "INVALID_ARGUMENT")
            sources = handle_tool_call(tools, "recall_list_sources", {})
            self.assertEqual(len(sources["structuredContent"]["sources"]), 1)
        finally:
            tools.close()


if __name__ == "__main__":
    unittest.main()
