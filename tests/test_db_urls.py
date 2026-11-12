"""R01 / §4：库版本、URL 与时间规则、搜索归一。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from recall.db import init_db, open_readonly, open_readwrite, schema_version
from recall.errors import RecallError
from recall.textnorm import like_literal, normalize, query_words
from recall.tools import RecallTools
from recall.urls import UrlProblem, sanitize_url, saved_at_iso


class DbAndUrlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "recall.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_is_idempotent_and_version_mismatch_is_clear(self) -> None:
        init_db(self.db)
        init_db(self.db)
        conn = open_readwrite(self.db)
        self.assertEqual(schema_version(conn), 1)
        conn.close()
        conn = sqlite3.connect(str(self.db))
        conn.execute("PRAGMA user_version = 2")
        conn.close()
        with self.assertRaises(RecallError) as ctx:
            open_readonly(self.db)
        self.assertEqual(ctx.exception.code, "DB_NOT_READY")
        self.assertIn("2", ctx.exception.message)
        self.assertIn("1", ctx.exception.message)

    def test_readonly_never_creates_db(self) -> None:
        with self.assertRaises(RecallError) as ctx:
            open_readonly(self.db)
        self.assertEqual(ctx.exception.code, "DB_NOT_READY")
        self.assertFalse(self.db.exists())
        with self.assertRaises(RecallError):
            open_readwrite(self.db)
        self.assertFalse(self.db.exists())

    def test_url_rules(self) -> None:
        safe = sanitize_url("https://EXAMPLE.com:443/a/b?utm_source=mail&p=2&keep=%2F#section-3")
        self.assertEqual(
            safe.canonical_url,
            "https://example.com/a/b?p=2&keep=%2F#section-3",
        )
        self.assertEqual(safe.platform, "web")
        cleaned = sanitize_url("https://example.com/x?access_token=VERY_SECRET&keep=1")
        self.assertTrue(cleaned.sanitized)
        self.assertNotIn("VERY_SECRET", cleaned.link_url)
        self.assertIn("keep=1", cleaned.link_url)
        with self.assertRaises(UrlProblem) as ctx:
            sanitize_url("https://user:pass@example.com/x")
        self.assertEqual(ctx.exception.code, "CREDENTIALS_IN_URL")
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x"):
            with self.assertRaises(UrlProblem):
                sanitize_url(bad)
        self.assertEqual(sanitize_url("http://example.com:80/x").canonical_url, "http://example.com/x")
        self.assertEqual(sanitize_url("https://b23.tv/abc").platform, "bilibili")
        self.assertEqual(sanitize_url("https://zhuanlan.zhihu.com/p/1").platform, "zhihu")
        self.assertEqual(sanitize_url("https://www.douyin.com/video/1").platform, "douyin")
        # 不展开短链，也不因长得像就合并两篇文章。
        self.assertNotEqual(
            sanitize_url("https://t.cn/abc").canonical_url,
            sanitize_url("https://example.com/abc").canonical_url,
        )
        self.assertNotEqual(
            sanitize_url("https://example.com/a?p=1").canonical_url,
            sanitize_url("https://example.com/a?p=2").canonical_url,
        )

    def test_time_and_query_normalization(self) -> None:
        self.assertIsNone(saved_at_iso(None))
        self.assertEqual(saved_at_iso("2026-10-09T06:30:00Z"), "2026-10-09T06:30:00Z")
        self.assertEqual(saved_at_iso("2026-10-09T14:30:00+08:00"), "2026-10-09T06:30:00Z")
        self.assertEqual(saved_at_iso(1696833000), "2023-10-09T06:30:00Z")
        with self.assertRaises(ValueError):
            saved_at_iso("not-a-time")
        words, full = query_words("ＪＰ 电商")
        self.assertEqual(words, ["jp", "电商"])
        self.assertEqual(full, "jp 电商")
        self.assertEqual(like_literal("%"), "%\\%%")
        self.assertEqual(like_literal("a_b"), "%a\\_b%")
        self.assertEqual(normalize("Ｃ＋＋"), "c++")


if __name__ == "__main__":
    unittest.main()
