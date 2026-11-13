"""Whole-project audit regressions; temporary data and no external services."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from recall import RecallTools, catalog
from recall.db import catalog_version, init_db, open_readwrite
from recall.errors import RecallError
from recall.ingest import add_link, import_bookmark_tree, import_csv
from recall.media import index_caption, index_media
from recall.media.tasks import write_segments
from recall.sources.github import GitHubStarsConnector
from recall.sources.http import HttpResponse, _CheckedRedirectHandler
from recall.urls import sanitize_url, saved_at_iso


class AuditRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = init_db(Path(self.tmp.name) / "recall.db")
        self.conn = open_readwrite(self.db)
        self.addCleanup(self.conn.close)

    def entry(self, **fields):
        return catalog.EntryInput(sanitize_url("https://example.com/item"), **fields)

    def seed(self):
        catalog.ensure_source(self.conn, "source", "manual", "web")
        catalog.merge_entries(self.conn, "source", [self.entry(title="Original title", note="kept note")])
        return self.conn.execute("SELECT item_id, entry_id FROM entries").fetchone()

    def test_missing_metadata_remains_searchable_and_empty_does_not_erase_it(self):
        item_id, _ = self.seed()
        for fields in ({}, {"title": "", "note": ""}):
            catalog.merge_entries(self.conn, "source", [self.entry(**fields)])
            with RecallTools.open(self.db) as tools:
                self.assertEqual(tools.recall_search("Original kept")["items"][0]["item_id"], item_id)
                self.assertEqual(tools.recall_get_item(item_id)["item"]["title"], "Original title")

    def test_collection_changes_invalidate_cursor(self):
        self.seed()
        catalog.ensure_collection(self.conn, "source", ("second",))
        with RecallTools.open(self.db) as tools:
            cursor = tools.recall_list_collections(limit=1)["next_cursor"]
            catalog.ensure_collection(self.conn, "source", ("third",))
            with self.assertRaises(RecallError) as raised:
                tools.recall_list_collections(limit=1, cursor=cursor)
            self.assertEqual(raised.exception.code, "STALE_CURSOR")

    def test_delete_source_removes_its_run_history(self):
        self.seed()
        catalog.start_run(self.conn, "source", "file")
        catalog.delete_source(self.conn, "source")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM import_runs").fetchone()[0], 0)

    def test_incomplete_snapshot_cannot_deactivate_entries(self):
        self.seed()
        with self.assertRaises(RecallError):
            catalog.replace_snapshot(self.conn, "source", [], allow_empty=True, complete=False)
        self.assertEqual(self.conn.execute("SELECT active FROM entries").fetchone()[0], 1)

    def test_invalid_access_is_rejected(self):
        self.seed()
        with self.assertRaises(RecallError):
            catalog.merge_entries(self.conn, "source", [self.entry(access="private")])

    def test_csv_header_whitespace_and_physical_line_numbers(self):
        path = Path(self.tmp.name) / "input.csv"
        path.write_text(' url , title , saved_at \nhttps://example.com/a,"two\nlines",\n'
                        'https://example.com/b,b,,extra\nhttps://example.com/c,c,bad-time\n', encoding="utf-8")
        result = import_csv(self.db, path, "csv", "web")
        self.assertEqual(result["counts"]["entries_added"], 1)
        self.assertEqual(result["row_errors"], [{"line": 4, "code": "ROW_TOO_WIDE"},
                                               {"line": 5, "code": "INVALID_TIMESTAMP"}])

    def test_out_of_range_timestamps_raise_value_error(self):
        for value in (1e100, "9" * 400, float("inf")):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                saved_at_iso(value)

    def test_completed_media_and_captions_are_reused(self):
        item_id, entry_id = self.seed()
        path = Path(self.tmp.name) / "sample.png"
        path.write_bytes(b"fake image")
        engine = Mock(return_value=[{"text": "Recognized"}])
        first = index_media(self.db, item_id, path, engines={"ocr": engine})
        version = catalog_version(self.conn)
        second = index_media(self.db, item_id, path, engines={"ocr": engine})
        self.assertEqual(engine.call_count, 1)
        self.assertTrue(second["reused"])
        self.assertEqual((first["segments"], second["segments"]), (1, 1))
        self.assertEqual(catalog_version(self.conn), version)
        index_caption(self.db, item_id, entry_id, "caption")
        self.assertEqual(index_caption(self.db, item_id, entry_id, "caption")["status"], "ready")

    def test_removing_media_text_invalidates_cursor(self):
        item_id, entry_id = self.seed()
        index_caption(self.db, item_id, entry_id, "caption")
        task_id = self.conn.execute("SELECT task_id FROM media_tasks").fetchone()[0]
        version = catalog_version(self.conn)
        with catalog.transaction(self.conn):
            write_segments(self.conn, task_id, entry_id, "caption", "standard", [])
        self.assertGreater(catalog_version(self.conn), version)

    def test_malformed_bookmark_tree_preserves_previous_snapshot(self):
        payload = {"profile_uuid": "p", "version": 1, "nodes": [
            {"id": "0", "parentId": "", "title": "root"},
            {"id": "1", "parentId": "0", "url": "https://example.com/old"}]}
        self.assertTrue(import_bookmark_tree(self.db, payload, "live")["ok"])
        bad_nodes = [
            [{"id": "2", "parentId": "missing", "url": "https://example.com/new"}],
            [{"id": "2", "parentId": "3"}, {"id": "3", "parentId": "2"}],
            [{"id": "0"}, {"id": "0"}],
            ["invalid"],
        ]
        for nodes in bad_nodes:
            result = import_bookmark_tree(self.db, dict(payload, version=2, nodes=nodes), "live")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        with RecallTools.open(self.db) as tools:
            self.assertEqual(len(tools.recall_search("old")["items"]), 1)

    def test_query_cursor_uses_the_same_snapshot_as_its_rows(self):
        self.seed()
        add_link(self.db, "https://example.com/second", "source", "web")
        with RecallTools.open(self.db) as tools:
            original = tools._matched_rows

            def concurrent_write(*args):
                rows = original(*args)
                add_link(self.db, "https://example.com/new", "source", "web")
                return rows

            with patch.object(tools, "_matched_rows", side_effect=concurrent_write):
                page = tools.recall_search(limit=1)
            with self.assertRaises(RecallError) as raised:
                tools.recall_search(limit=1, cursor=page["next_cursor"])
            self.assertEqual(raised.exception.code, "STALE_CURSOR")

    def test_search_pages_preserve_rank_time_and_deduplication(self):
        self.seed()
        catalog.ensure_source(self.conn, "other", "manual", "web")
        for source in ("source", "other"):
            catalog.merge_entries(self.conn, source, [
                catalog.EntryInput(sanitize_url(f"https://example.com/rank/{i}"),
                    title=["alpha beta", "beta alpha", "other"][i % 3], note="alpha beta",
                    saved_at=f"2026-01-{i + 1:02d}T00:00:00Z") for i in range(24)
            ])
        with RecallTools.open(self.db) as tools:
            actual = []
            cursor = None
            while True:
                page = tools.recall_search("alpha beta", limit=5, cursor=cursor)
                actual.extend(int(item["url"].rsplit("/", 1)[-1]) for item in page["items"])
                self.assertTrue(all(len(item["sources"]) == 2 for item in page["items"]))
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        self.assertEqual(actual, [i for rank in range(3) for i in range(23, -1, -1) if i % 3 == rank])

    def test_cli_media_failure_has_nonzero_exit_code(self):
        import io
        from contextlib import redirect_stdout
        from recall.cli import main
        with patch("recall.cli.index_media", return_value={"status": "unavailable"}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["media", "index", "item", "sample.png", "--json"]), 1)

    def test_native_host_rejects_override_of_bound_source(self):
        from recall.native_host import handle
        result = handle({"source_id": "another"}, db_path=str(self.db), source_id="bound")
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_backup_race_does_not_overwrite_and_failure_cleans_up(self):
        from concurrent.futures import ThreadPoolExecutor
        from recall.db import backup_db
        self.seed()
        target = Path(self.tmp.name) / "backup.db"
        def backup():
            try:
                backup_db(self.db, target)
                return True
            except RecallError as error:
                self.assertEqual(error.code, "INVALID_ARGUMENT")
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: backup(), range(2))), [False, True])
        bad_target = Path(self.tmp.name) / "failed.db"
        source = Mock()
        source.backup.side_effect = OSError("disk full")
        with patch("recall.db._connect_ro", return_value=source), self.assertRaises(OSError):
            backup_db(self.db, bad_target)
        self.assertFalse(bad_target.exists())
        source.close.assert_called_once()

    def test_malformed_html_cannot_replace_valid_data(self):
        from recall.ingest import import_bookmarks_html
        good = Path(self.tmp.name) / "bookmarks.html"
        header = '<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>'
        good.write_text(header + '<DT><A HREF="https://example.com/old">old</A></DL><p>', encoding="utf-8")
        import_bookmarks_html(self.db, good, "html", "chrome")
        for body in ('</DL><DL><A HREF="https://example.com/new">new</A></DL></DL>',
                     '<A HREF="https://example.com/new">new<A HREF="https://example.com/lost">lost</A></DL>'):
            good.write_text(header + body, encoding="utf-8")
            with self.assertRaises(RecallError):
                import_bookmarks_html(self.db, good, "html", "chrome", replace=True)
        with RecallTools.open(self.db) as tools:
            self.assertEqual(len(tools.recall_search("old")["items"]), 1)

    def test_mcp_rejects_invalid_argument_types(self):
        from recall.mcp_server import handle_tool_call
        self.seed()
        with RecallTools.open(self.db) as tools:
            for args in ([], {"source_ids": {}}, {"source_ids": [1]}, {"platform": []}, {"collection_id": []}):
                result = handle_tool_call(tools, "recall_search", args)
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["error"]["code"], "INVALID_ARGUMENT")


class TransportRegressionTest(unittest.TestCase):
    def test_long_chat_queries_are_bounded(self):
        from recall.web import retrieve
        tools = Mock()
        tools.recall_search.return_value = {"items": [], "coverage": []}
        retrieve(tools, "日本市场" * 300, None)
        self.assertLessEqual(tools.recall_search.call_count, 12)
        self.assertTrue(all(len(call.kwargs["query"]) <= 200 for call in tools.recall_search.call_args_list))

    def test_missing_visibility_never_becomes_public(self):
        from recall.sources.bilibili import parse_folders
        self.assertTrue(parse_folders({"data": {"list": [{"id": 1}]}})[0]["private"])
        client = Mock()
        client.request.side_effect = [HttpResponse(200, body=b'{"id":42}'),
            HttpResponse(200, body=json.dumps([{"starred_at": None, "repo": {
                "id": 1, "html_url": "https://github.com/example/repo"}}]).encode())]
        with self.assertRaises(RecallError) as raised:
            GitHubStarsConnector("g", http=client, env={"RECALL_GITHUB_TOKEN": "test"}).fetch()
        self.assertEqual(raised.exception.code, "SOURCE_FORMAT_CHANGED")

    def test_url_cleanup_preserves_business_parameter_bytes(self):
        url = "https://example.com/a?keep=%2f%20X&flag&utm_source=email&also=a+b"
        self.assertEqual(sanitize_url(url).link_url, "https://example.com/a?keep=%2f%20X&flag&also=a+b")

    def test_registration_rejects_shell_metacharacters(self):
        import importlib.util
        file = Path(__file__).resolve().parents[1] / "extensions/bookmarks/register_windows.py"
        spec = importlib.util.spec_from_file_location("registration", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for source, database in (("bad&command", None), ("ok", "%PRIVATE%")):
                with self.assertRaises(ValueError):
                    module.write_launcher(folder, Path("python.exe"), source, database)
            self.assertEqual(list(folder.iterdir()), [])

    def test_github_keeps_first_page_etag(self):
        responses = [
            HttpResponse(200, body=b'{"id":42}'),
            HttpResponse(200, {"ETag": '"first"', "Link":
                '<https://api.github.com/user/starred?page=2>; rel="next"'}, b'[]'),
            HttpResponse(200, {"ETag": '"last"'}, b'[]'),
        ]
        client = Mock()
        client.request.side_effect = responses
        result = GitHubStarsConnector("github", http=client,
                                     env={"RECALL_GITHUB_TOKEN": "test"}).fetch("full")
        self.assertEqual(result.etag, '"first"')

    def test_https_redirect_cannot_downgrade_credentials(self):
        from urllib.request import Request
        handler = _CheckedRedirectHandler(("api.github.com",))
        request = Request("https://api.github.com/user", headers={"Authorization": "Bearer test"})
        with self.assertRaises(RecallError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://api.github.com/user")


if __name__ == "__main__":
    unittest.main()
