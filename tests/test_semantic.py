from __future__ import annotations

import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from recall import catalog, db, semantic
from recall.errors import RecallError
from recall.mcp_server import handle_tool_call, tools_from_env
from recall.tools import RecallTools
from recall.urls import sanitize_url
from recall.web import retrieve


@unittest.skipUnless(find_spec("numpy"), "optional vector runtime")
class SemanticTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(db.init_db(Path(directory.name) / "recall.db"))
        enabled = patch.object(semantic, "enabled", return_value=True)
        encoder = patch.object(semantic, "embed", side_effect=self.embed)
        enabled.start()
        encoder.start()
        self.addCleanup(enabled.stop)
        self.addCleanup(encoder.stop)

    @staticmethod
    def embed(texts, **kwargs):
        import numpy as np

        vectors = np.zeros((len(texts), semantic.DIM), dtype="<f4")
        for offset, text in enumerate(texts):
            vectors[offset, 0 if ("光伏" in text or "阳光" in text) else 1] = 1
        return vectors

    def add(self, name, title, body, *, source="allowed", access="standard"):
        conn = db.open_readwrite(self.path)
        try:
            catalog.ensure_source(conn, source, "manual", "web")
            catalog.merge_entries(conn, source, [catalog.EntryInput(
                sanitize_url(f"https://example.org/{name}"), title=title, body=body, access=access,
                path=(source,), saved_at="2026-03-01T00:00:00Z",
            )])
        finally:
            conn.close()

    def test_hybrid_matches_paraphrase_after_first_window_with_original_evidence(self):
        question = "怎样把阳光变成电"
        body = "简介资料。" * 75 + "用光伏转换电能。"
        self.add("solar", "研究笔记", body)
        self.add("literal", question, "烘焙文章")
        self.assertEqual(semantic.index_pending(self.path), 2)
        self.assertEqual(semantic.index_pending(self.path), 0)
        with RecallTools.open(self.path) as tools:
            self.assertEqual([item["title"] for item in tools.recall_search(question)["items"]], [question])
            hybrid = tools.recall_search(question, mode="hybrid")["items"]
            self.assertEqual({item["title"] for item in hybrid}, {question, "研究笔记"})
            answer_items, _ = retrieve(tools, question, "web")
            solar = next(item for item in answer_items if item["title"] == "研究笔记")
            self.assertIn("用光伏转换电能", solar["snippets"][0]["text"])
            self.assertEqual(solar["snippets"][0]["url"], "https://example.org/solar")
            self.assertEqual(solar["snippets"][0]["locator"], {"part": 1})

    def test_mcp_private_default_still_obeys_source_allowlist_and_opt_out(self):
        self.add("private", "收藏 A", "光伏私密甲", access="restricted")
        self.add("outside", "收藏 B", "光伏私密乙", source="outside", access="restricted")
        semantic.index_pending(self.path)
        with patch.dict(os.environ, {"RECALL_DB": self.path, "RECALL_ALLOWED_SOURCES": "allowed"}):
            os.environ.pop("RECALL_INCLUDE_RESTRICTED", None)
            with tools_from_env() as tools:
                result = handle_tool_call(tools, "recall_search", {"query": "阳光取电"})["structuredContent"]
                self.assertEqual([item["title"] for item in result["items"]], ["收藏 A"])
                self.assertEqual(tools.recall_search("阳光取电", saved_after="2026-04-01T00:00:00Z",
                                                    mode="hybrid")["items"], [])
            os.environ["RECALL_INCLUDE_RESTRICTED"] = "0"
            with tools_from_env() as tools:
                self.assertEqual(tools.recall_search("阳光取电", mode="hybrid")["items"], [])

    def test_hybrid_pagination_keeps_keyword_tail_and_invalidates_on_new_vectors(self):
        for index in range(54):
            self.add(f"item-{index}", f"阳光 条目 {index}", "光伏" if index == 0 else None)
        semantic.index_pending(self.path)
        with RecallTools.open(self.path) as tools:
            page = tools.recall_search("阳光", mode="hybrid", limit=49)
            first_cursor = page["next_cursor"]
            tail = tools.recall_search("阳光", mode="hybrid", limit=49, cursor=first_cursor)
            self.assertEqual(len({item["item_id"] for item in page["items"] + tail["items"]}), 54)
            self.assertIsNone(tail["next_cursor"])
            browse = tools.recall_search(limit=1)
            conn = db.open_readwrite(self.path)
            try:
                conn.execute("UPDATE meta SET value='2' WHERE key='semantic_version'")
            finally:
                conn.close()
            self.assertTrue(tools.recall_search(limit=1, cursor=browse["next_cursor"])["items"])
            with self.assertRaises(RecallError) as error:
                tools.recall_search("阳光", mode="hybrid", limit=49, cursor=first_cursor)
            self.assertEqual(error.exception.code, "STALE_CURSOR")
