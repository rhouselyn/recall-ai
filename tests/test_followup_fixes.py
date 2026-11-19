"""Approved audit follow-up: identity, progress, ownership and bounded work."""
import importlib.util
import json
import tempfile
import threading
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from recall import catalog
from recall.clock import freeze, advance, now_iso
from recall.config import media_dir
from recall.db import init_db, open_readwrite
from recall.errors import RecallError
from recall.ingest import import_bookmark_tree
from recall.media import index_caption, index_media, run_pending
from recall.media import tasks
from recall.sources.base import FetchResult, make_entry
from recall.sources.browser import PlaywrightBrowserSession, profile_dir
from recall.sources.runner import sync_once
from recall.sources.zhihu import ZhihuApiConnector
from recall.sources.zhihu_web import ZhihuWebConnector
from test_sync import FakeHttp, json_response, star, BASE


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = init_db(Path(self.tmp.name) / "recall.db")
        self.conn = open_readwrite(self.db)
        self.addCleanup(self.conn.close)
        freeze("2026-11-13T00:00:00Z")
        self.addCleanup(freeze, None)

    def source(self, sid="s", kind="manual", config=None):
        catalog.ensure_source(self.conn, sid, kind, "test", config=config)

    def seed(self, sid="s", **fields):
        self.source(sid)
        catalog.merge_entries(self.conn, sid, [make_entry("https://example.com/a", **fields)])
        return self.conn.execute(
            "SELECT item_id, entry_id FROM entries JOIN collections USING(collection_id) WHERE source_id=?", (sid,),
        ).fetchone()

    def test_profiles_do_not_collide_and_only_owned_legacy_moves(self):
        old = self.db.parent / "browser-profiles" / "work_github"
        old.mkdir(parents=True)
        (old / "identity.json").write_text(json.dumps({"label": "work.github"}), encoding="utf-8")
        other = profile_dir("work_github", self.db)
        self.assertFalse(other.exists())
        self.assertTrue(old.exists())
        moved = profile_dir("work.github", self.db)
        self.assertTrue((moved / "identity.json").exists())
        self.assertFalse(old.exists())
        self.assertNotEqual(moved, other)
        self.assertNotEqual(moved, profile_dir("Work.github", self.db))
        self.assertNotEqual(moved, profile_dir("work.github", self.db.with_name("another.db")))

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "optional Playwright")
    def test_platform_login_survives_closing_window_and_reopening_browser(self):
        from http.cookies import SimpleCookie
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from recall.sources import browser

        received = []

        class Site(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/saved":
                    received.append(SimpleCookie(self.headers.get("Cookie", "")))
                self.send_response(200)
                if self.path == "/login":
                    self.send_header("Set-Cookie", "session=fixture; HttpOnly; Path=/")
                    self.send_header("Set-Cookie", "persistent=fixture; HttpOnly; Path=/; Max-Age=600")
                if self.path == "/renew":
                    self.send_header("Set-Cookie", "session=renewed; HttpOnly; Path=/")
                if self.path == "/logout":
                    self.send_header("Set-Cookie", "session=; HttpOnly; Path=/; Max-Age=0")
                self.end_headers()

        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        self.addCleanup(site.server_close)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        self.addCleanup(site.shutdown)
        base = f"http://127.0.0.1:{site.server_port}"
        kind = "bilibili_favorites"
        self.source(kind=kind)
        launch = browser._launch

        def close_login_window(playwright, directory, *, headless):
            context = launch(playwright, directory, headless=headless)
            context.add_cookies([{"name": "unrelated", "value": "fixture", "url": "https://example.org"}])
            page = context.pages[0]
            page.wait_for_timeout = lambda _: page.close()
            return context

        with patch.dict(browser.PLATFORM_LOGIN, {kind: base + "/login"}), \
             patch.dict(browser.PLATFORM_START, {kind: base + "/saved"}), \
             patch.object(browser, "PLATFORM_SESSION_DOMAINS", {kind: ("127.0.0.1",)}, create=True):
            try:
                with patch.object(browser, "_launch", side_effect=close_login_window):
                    browser.connect("s", kind, wait_seconds=1, headless=True, db_path=self.db)
            except RecallError as exc:
                if exc.code == "CONFIG_ERROR":
                    self.skipTest(exc.message)
                raise
            for expected, path in (("fixture", "/renew"), ("renewed", "/logout"), (None, None)):
                session = browser.load_session("s", kind, self.db)
                try:
                    self.assertIn("persistent", received[-1])
                    self.assertNotIn("unrelated", {cookie["name"] for cookie in session._context.cookies()})
                    cookie = received[-1].get("session")
                    self.assertEqual(cookie.value if cookie else None, expected)
                    if path:
                        session.page.goto(base + path)
                finally:
                    session.close()

    def test_douyin_identity_requires_observed_self_response(self):
        directory = Path(self.tmp.name)
        (directory / "identity.json").write_text(json.dumps({"user_id": "sec-42"}), encoding="utf-8")
        page = Mock(url="https://www.douyin.com/user/self")
        session = PlaywrightBrowserSession(page, Mock(), Mock(), directory, "douyin_favorites")
        with self.assertRaises(RecallError) as raised:
            session.identity()
        self.assertEqual(raised.exception.code, "AUTH_REQUIRED")
        response = Mock(url="https://www.douyin.com/aweme/v1/web/user/profile/self/", ok=True)
        response.json.return_value = {"status_code": 0, "user": {"uid": "42", "sec_uid": "sec-42"}}
        session._observe_identity(response)
        self.assertEqual(session.identity()["user_id"], "42")
        response.json.return_value["user"] = {"uid": "99", "sec_uid": "sec-99"}
        session._observe_identity(response)
        with self.assertRaises(RecallError) as raised:
            session.identity()
        self.assertEqual(raised.exception.code, "ACCOUNT_MISMATCH")

    def test_collection_rename_preserves_entry_and_media_and_reindexes_all_entries(self):
        item, entry = self.seed(path=("Old",), external_key="a", collection_key="folder:1")
        index_caption(self.db, item, entry, "kept caption")
        second = make_entry("https://example.com/b", path=("Old",), collection_key="folder:1", external_key="b")
        catalog.merge_entries(self.conn, "s", [second])
        catalog.merge_entries(self.conn, "s", [replace(second, path=("New",))])
        self.assertEqual(self.conn.execute("SELECT path_json FROM collections").fetchone()[0], '["New"]')
        self.assertEqual(self.conn.execute("SELECT entry_id FROM media_segments").fetchone()[0], entry)
        rows = self.conn.execute("SELECT search_norm FROM entries").fetchall()
        self.assertTrue(all("new" in row[0] and "old" not in row[0] for row in rows))
        catalog.merge_entries(self.conn, "s", [replace(second, collection_key="folder:2")])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM collections").fetchone()[0], 2)

    def test_unambiguous_legacy_collection_keeps_ids(self):
        item, entry = self.seed(path=("Folder",), external_key="a")
        index_caption(self.db, item, entry, "caption")
        catalog.merge_entries(self.conn, "s", [make_entry(
            "https://example.com/a", path=("Folder",), external_key="a", collection_key="folder:1")])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entries").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT entry_id FROM entries").fetchone()[0], entry)
        self.assertEqual(self.conn.execute("SELECT external_key FROM collections").fetchone()[0], "folder:1")

    def test_browser_folders_with_same_title_and_rename(self):
        nodes = [{"id": "0", "title": ""}, {"id": "1", "parentId": "0", "title": "Same"},
                 {"id": "2", "parentId": "0", "title": "Same"},
                 {"id": "a", "parentId": "1", "url": "https://example.com/a"},
                 {"id": "b", "parentId": "2", "url": "https://example.com/a"}]
        payload = {"profile_uuid": "p", "browser": "chrome", "version": 1, "nodes": nodes}
        result = import_bookmark_tree(self.db, payload, "live")
        self.assertTrue(result["ok"], result)
        before = {row[0] for row in self.conn.execute("SELECT entry_id FROM entries")}
        self.assertEqual(len(before), 2)
        nodes[1]["title"] = "Renamed"
        result = import_bookmark_tree(self.db, {**payload, "version": 2}, "live")
        self.assertTrue(result["ok"], result)
        self.assertEqual(before, {row[0] for row in self.conn.execute("SELECT entry_id FROM entries WHERE active=1")})

    def test_sync_lease_is_acquired_before_opening_browser_and_released_after_close(self):
        self.source(kind="douyin_favorites")
        catalog.update_sync_state(self.conn, "s", lease_owner="other", lease_until="2026-11-14T00:00:00Z")
        with patch("recall.sources.browser.load_session") as load:
            self.assertEqual(sync_once(self.db, "s")["reason"], "lease")
            load.assert_not_called()
        catalog.update_sync_state(self.conn, "s", lease_owner=None, lease_until=None)
        session = Mock()
        def close():
            self.assertIsNotNone(catalog.get_sync_state(self.conn, "s")["lease_owner"])
        session.close.side_effect = close
        connector = types.SimpleNamespace(trusted=False, fetch=lambda **kwargs: FetchResult())
        with patch("recall.sources.browser.load_session", return_value=session), \
             patch("recall.sources.runner.get_connector", return_value=connector):
            self.assertEqual(sync_once(self.db, "s")["status"], "succeeded")
        self.assertIsNone(catalog.get_sync_state(self.conn, "s")["lease_owner"])

    def test_github_resumes_tail_and_does_not_replace_first_page_etag(self):
        self.source(kind="github_stars")
        first = BASE + "/user/starred?per_page=100&page=1"
        second = BASE + "/user/starred?per_page=100&page=2"
        client = FakeHttp({BASE + "/user": [json_response({"id": 1})],
                           first: [json_response([star(1, "a/one")], headers={"ETag": "first", "Link": f'<{second}>; rel="next"'})],
                           second: [json_response([star(2, "a/two")], headers={"ETag": "second"})]})
        options = dict(full=True, http=client, env={"RECALL_GITHUB_TOKEN": "fixture"}, budget={"max_requests": 2})
        self.assertFalse(sync_once(self.db, "s", **options)["complete"])
        self.assertTrue(sync_once(self.db, "s", **options)["complete"])
        self.assertEqual(client.calls.count(first), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entries WHERE active=1").fetchone()[0], 2)
        self.assertEqual(catalog.get_sync_state(self.conn, "s")["last_etag"], "first")

    def test_github_lost_permissions_do_not_delete_old_entries(self):
        self.source(kind="github_stars")
        first = BASE + "/user/starred?per_page=100&page=1"
        env = {"RECALL_GITHUB_TOKEN": "fixture"}
        client = FakeHttp({BASE + "/user": [json_response({"id": 1})], first: [json_response([
            star(1, "a/one"), star(2, "a/private", private=True), star(3, "a/newly-private"), star(4, "a/unstarred")])]})
        self.assertEqual(sync_once(self.db, "s", full=True, http=client, env=env)["status"], "succeeded")
        client.script[first] = [json_response([star(1, "a/one"), star(5, "a/new")])]
        client.script[BASE + "/repositories/3"] = [json_response({}, status=404)]
        client.script[BASE + "/repositories/4"] = [json_response({"id": 4, "private": False})]
        result = sync_once(self.db, "s", full=True, http=client, env=env)
        self.assertEqual(result["status"], "partial", result)
        active = {row[0] for row in self.conn.execute("SELECT external_key FROM entries WHERE active=1")}
        self.assertEqual(active, {"repo:1", "repo:2", "repo:3", "repo:5"})
        self.assertEqual(self.conn.execute("SELECT access FROM entries WHERE external_key='repo:3'").fetchone()[0], "restricted")

    def test_two_strike_reconciliation_uses_whole_batch_not_timestamp(self):
        self.source(kind="rss")
        entries = [make_entry(f"https://example.com/{i}") for i in range(3)]
        catalog.merge_entries(self.conn, "s", entries)
        connector = Mock(trusted=False, spec=["trusted", "fetch"])
        connector.fetch.side_effect = [
            FetchResult(entries=entries[:1], continuation={"page": 2}),
            FetchResult(entries=entries[1:2], complete=True),
        ] * 2
        with patch("recall.sources.runner.get_connector", return_value=connector):
            for _ in range(4):
                result = sync_once(self.db, "s", full=True)
                self.assertIn(result["status"], ("succeeded", "partial"), result)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entries WHERE active=1").fetchone()[0], 2)

    def test_pause_then_resume_does_not_revive_inflight_sync(self):
        from recall.watch import pause_source, resume_source
        self.source(kind="rss")
        def fetch(**kwargs):
            pause_source(self.db, "s")
            resume_source(self.db, "s")
            return FetchResult(entries=[make_entry("https://example.com/a")])
        connector = types.SimpleNamespace(trusted=False, fetch=fetch)
        with patch("recall.sources.runner.get_connector", return_value=connector):
            result = sync_once(self.db, "s")
        self.assertEqual(result["reason"], "lease_lost")
        self.assertIsNone(catalog.get_sync_state(self.conn, "s")["lease_owner"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entries").fetchone()[0], 0)

    def test_zhihu_head_rotates_and_full_resumes(self):
        class Api:
            def request(inner, method, url, **kwargs):
                if url.endswith("favlists"):
                    return json_response({"Data": {"Items": [{"UrlToken": "1"}, {"UrlToken": "2"}]}})
                params = kwargs["params"]
                calls.append((params["FavlistUrlToken"], params["Offset"]))
                return json_response({"Data": {"Items": [], "Paging": {"IsEnd": params["Offset"] == 20, "NextOffset": 20}}})
        calls = []
        api = ZhihuApiConnector("s", http=Api(), env={"RECALL_ZHIHU_ACCESS_SECRET": "fixture"})
        first = api.fetch("head")
        api.fetch("head", state={"last_summary": json.dumps({"continuation_head": first.continuation})})
        self.assertEqual(calls, [("1", 0), ("2", 0)])
        calls.clear()
        first = api.fetch("full", budget={"max_requests": 2})
        api.fetch("full", state={"last_summary": json.dumps({"continuation_full": first.continuation})})
        self.assertEqual(calls, [("1", 0), ("1", 20), ("2", 0), ("2", 20)])

    def test_zhihu_web_head_rotates(self):
        calls = []
        class Session:
            def identity(self): return {"user_id": "me"}
            def status(self): return "ok"
            def get_json(self, url, **kwargs):
                if url.endswith("/me"): return {"id": "me"}
                if url.endswith("/items"):
                    calls.append(url)
                    return {"data": [], "paging": {"is_end": False, "offset": 20}}
                return {"creator": {"id": "me"}}
        connector = ZhihuWebConnector("s", {"collections": [{"id": "1"}, {"id": "2"}]}, session=Session())
        first = connector.fetch()
        connector.fetch(state={"last_summary": json.dumps({"continuation_head": first.continuation})})
        self.assertEqual(calls, ["/api/v4/collections/1/items", "/api/v4/collections/2/items"])

    def test_daily_quota_counts_failure_and_survives_new_client(self):
        from recall.sources.quota import DailyQuotaClient
        client = Mock()
        client.request.side_effect = OSError("offline")
        for _ in range(2):
            with self.assertRaises(OSError):
                DailyQuotaClient(self.conn, client, 2).request("GET", "fixture")
        with self.assertRaises(RecallError) as raised:
            DailyQuotaClient(self.conn, client, 2).request("GET", "fixture")
        self.assertEqual(raised.exception.code, "RATE_LIMITED")
        self.assertEqual(client.request.call_count, 2)
        advance(86400)
        self.assertEqual(DailyQuotaClient(self.conn, client, 2).remaining(), 2)

    def test_media_claim_is_atomic_and_stale_worker_cannot_finish(self):
        _, entry = self.seed()
        task_id, _ = tasks.enqueue(self.conn, entry, "ocr", b"file", engine_version="test")
        first = tasks.claim(self.conn)[0]
        other = open_readwrite(self.db)
        self.addCleanup(other.close)
        self.assertEqual(tasks.claim(other), [])
        advance(301)
        second = tasks.claim(other)[0]
        self.assertEqual(second["task_id"], task_id)
        self.assertFalse(tasks.set_status(self.conn, first, "ready"))
        self.assertTrue(tasks.set_status(other, second, "ready"))

    def test_final_crashed_attempt_does_not_stay_processing_forever(self):
        _, entry = self.seed()
        task_id, _ = tasks.enqueue(self.conn, entry, "ocr", b"file", engine_version="test")
        for _ in range(3):
            self.assertEqual(tasks.claim(self.conn)[0]["task_id"], task_id)
            advance(301)
        self.assertEqual(run_pending(self.db)["processed"], [])
        row = self.conn.execute("SELECT status, error_code FROM media_tasks").fetchone()
        self.assertEqual(tuple(row), ("failed", "WORKER_EXPIRED"))

    def test_concurrent_media_runs_only_one_engine(self):
        item, entry = self.seed()
        path = Path(self.tmp.name) / "input.png"
        path.write_bytes(b"original")
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        def engine(filename):
            entered.set()
            if not release.wait(10): raise RuntimeError("test timeout")
            return [{"text": "recognized"}]
        def run():
            outcomes.append(index_media(self.db, item, path, entry_id=entry, engines={"ocr": engine}))
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            other_engine = Mock()
            result = index_media(self.db, item, path, entry_id=entry, engines={"ocr": other_engine})
            self.assertEqual(result["status"], "skipped")
            other_engine.assert_not_called()
        finally:
            release.set()
            worker.join(10)
        self.assertEqual(outcomes[0]["status"], "ready")

    def test_media_uses_owned_copy_and_source_delete_preserves_shared_and_original(self):
        item, entry = self.seed()
        _, second = self.seed("other")
        path = Path(self.tmp.name) / "input.png"
        path.write_bytes(b"original")
        engine = lambda name: [{"text": Path(name).read_text()}]
        index_media(self.db, item, path, entry_id=entry, engines={"ocr": engine})
        index_media(self.db, item, path, entry_id=second, engines={"ocr": engine})
        stored = Path(self.conn.execute("SELECT source_path FROM media_tasks LIMIT 1").fetchone()[0])
        self.assertTrue(stored.is_relative_to(media_dir(self.db)))
        path.write_bytes(b"changed")
        self.assertEqual(stored.read_bytes(), b"original")
        profile = profile_dir("s", self.db)
        profile.mkdir(parents=True)
        (profile / "identity.json").write_text("{}")
        catalog.delete_source(self.conn, "s")
        self.assertFalse(profile.exists())
        self.assertTrue(stored.exists())
        catalog.delete_source(self.conn, "other")
        self.assertFalse(stored.exists())
        self.assertEqual(path.read_bytes(), b"changed")

    def test_changed_legacy_media_is_not_indexed_under_old_digest(self):
        _, entry = self.seed()
        path = Path(self.tmp.name) / "legacy.png"
        path.write_bytes(b"changed")
        tasks.enqueue(self.conn, entry, "ocr", b"original", engine_version="test", source_path=str(path))
        engine = Mock()
        result = run_pending(self.db, engines={"ocr": engine})
        self.assertEqual(result["processed"][0]["status"], "unavailable")
        engine.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT error_code FROM media_tasks").fetchone()[0], "MEDIA_CHANGED")

    def test_delete_retries_failed_profile_cleanup(self):
        self.seed()
        directory = profile_dir("s", self.db)
        directory.mkdir(parents=True)
        with patch("shutil.rmtree", side_effect=PermissionError("in use")):
            result = catalog.delete_source(self.conn, "s")
        self.assertEqual(result["cleanup_pending"], [str(directory)])
        with self.assertRaises(RecallError):
            self.source()
        self.assertEqual(catalog.delete_source(self.conn, "s")["cleanup_pending"], [])
        self.assertFalse(directory.exists())

    def test_delete_cleanup_survives_interruption_after_database_commit(self):
        self.seed()
        directory = profile_dir("s", self.db)
        directory.mkdir(parents=True)
        with patch("shutil.rmtree", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            catalog.delete_source(self.conn, "s")
        self.assertIsNone(catalog.get_source(self.conn, "s"))
        self.assertEqual(catalog.delete_source(self.conn, "s")["cleanup_pending"], [])
        self.assertFalse(directory.exists())

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "optional numpy")
    def test_asr_consumes_audio_incrementally_with_bounded_windows(self):
        import numpy as np
        from recall.media.engines import default_asr
        seen = []
        class Stream:
            result = types.SimpleNamespace(text="text")
            def accept_waveform(self, rate, samples): seen.append(len(samples))
        recognizer = types.SimpleNamespace(create_stream=Stream, decode_stream=lambda stream: None)
        module = types.SimpleNamespace(OfflineRecognizer=types.SimpleNamespace(from_paraformer=lambda **kw: recognizer))
        def frames():
            for i in range(90):
                if i >= 30:
                    self.assertGreater(len(seen), 0, "decoder was fully materialized before ASR")
                yield np.zeros(16000, dtype=np.float32)
        with patch.dict("sys.modules", {"sherpa_onnx": module}), \
             patch.dict("os.environ", {"RECALL_ASR_MODEL_DIR": self.tmp.name}), \
             patch("recall.media.engines.decode_audio_16k", return_value=frames()):
            segments = default_asr("fixture.wav")
        self.assertEqual(seen, [480000, 480000, 480000, 48000])
        self.assertEqual(segments[-1]["locator"]["end_seconds"], 90)


if __name__ == "__main__":
    unittest.main()
