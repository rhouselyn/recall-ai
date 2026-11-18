from __future__ import annotations

import json
import importlib.util
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from recall.web import RecallServer


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch("recall.web.load_secret", return_value=None), patch("recall.web.archive.enabled", return_value=False), \
             patch("recall.web.semantic.enabled", return_value=False):
            self.server = RecallServer(0, str(Path(self.tmp.name) / "recall.db"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, data=None, **headers):
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(data).encode()
        request = Request(self.base + path, data=data, headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_import_search_chat_and_origin(self):
        token = self.request("/api/session")["token"]
        headers = {"X-Recall-Token": token, "Origin": self.base}
        self.request("/api/add", {"url": "https://example.com/japan", "title": "日本市场研究"}, **headers)
        results = self.request("/api/items?query=%E6%97%A5%E6%9C%AC")
        self.assertEqual(results["items"][0]["title"], "日本市场研究")
        detail = self.request("/api/items/" + results["items"][0]["item_id"])
        self.assertEqual(detail["item"]["url"], "https://example.com/japan")
        with patch("recall.web.answer", return_value="找到收藏"), patch("recall.web.deepseek_key", return_value=None):
            chat = self.request("/api/chat", {"text": "日本市场"}, **headers)
        self.assertEqual(chat["items"][0]["title"], "日本市场研究")
        with self.assertRaises(HTTPError) as forbidden:
            self.request("/api/add", {"url": "https://example.com/"}, **{
                "X-Recall-Token": token, "Origin": "https://attacker.example"
            })
        self.assertEqual(forbidden.exception.code, 403)

    def test_mcp_settings_describe_current_local_server_and_require_source_selection(self):
        self.request("/api/add", {"url": "https://example.org", "title": "Saved"}, **{"X-Recall-Token": self.server.token})
        result = self.request("/api/settings/mcp")
        self.assertEqual(result["sources"], ["manual-web"])
        self.assertEqual(result["server"]["args"], ["-m", "recall.mcp_server"])
        self.assertEqual(result["server"]["env"], {"RECALL_DB": self.server.db_path})

    def test_invalid_post_data_is_reported_without_tracebacks(self):
        import io
        from contextlib import redirect_stderr
        headers = {"X-Recall-Token": self.server.token}
        invalid = [
            ("/api/chat", {"text": 7}),
            ("/api/chat", {"text": "search", "history": [{"role": "user", "text": []}]}),
            ("/api/import", {"kind": "csv", "content": "invalid!"}),
            ("/api/media", {"filename": "test.png", "content": "invalid!"}),
        ]
        log = io.StringIO()
        for path, payload in invalid:
            with self.subTest(path=path), redirect_stderr(log), self.assertRaises(HTTPError) as raised:
                self.request(path, payload, **headers)
            self.assertEqual(json.load(raised.exception)["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(log.getvalue(), "")

    def test_item_evidence_supports_next_page(self):
        from recall.ingest import add_link
        for index in range(51):
            add_link(self.server.db_path, "https://example.com/shared", "manual", "web", collection=str(index))
        item = self.request("/api/items")["items"][0]
        first = self.request("/api/items/" + item["item_id"])
        second = self.request("/api/items/" + item["item_id"] + "?cursor=" + first["next_cursor"])
        self.assertEqual((len(first["evidence"]), len(second["evidence"])), (50, 1))
        self.assertIsNone(second["next_cursor"])

    def test_archived_content_paginates_beyond_evidence_preview(self):
        from recall.ingest import add_link
        from recall.media import archive
        from recall.tools import RecallTools
        from urllib.parse import urlencode

        add_link(self.server.db_path, "https://www.bilibili.com/video/BV1Test00001", "saved", "web",
                 title="Saved video", access="restricted")
        with patch.object(archive, "enabled", return_value=True):
            archive.run_archive_tick(self.server.db_path, extractor=lambda url: ([
                {"kind": "caption", "text": f"Line {index}", "locator": {"start_seconds": index}}
                for index in range(32)
            ], None))
        item = self.request("/api/items")["items"][0]["item_id"]
        path = f"/api/items/{item}"
        entry = self.request(path)["evidence"][0]
        self.assertEqual(len(entry["segments"]), 5)
        query = {"entry_id": entry["entry_id"]}
        first = self.request(path + "/content?" + urlencode(query))
        second = self.request(path + "/content?" + urlencode({**query, "cursor": first["next_cursor"]}))
        self.assertEqual([s["text"] for s in first["segments"] + second["segments"]],
                         [f"Line {index}" for index in range(32)])
        self.assertTrue(second["segments"][-1]["seek_url"].endswith("t=31"))
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(HTTPError):
            self.request("/api/items/other/content?" + urlencode(query))
        with RecallTools.open(self.server.db_path) as tools:
            from recall.errors import RecallError
            with self.assertRaises(RecallError):
                tools.read_archived_content(item, entry["entry_id"])

    def test_pending_logins_reuse_running_jobs_and_skip_connected_or_paused(self):
        from recall import catalog, db
        from recall.clock import now_iso

        conn = db.open_readwrite(self.server.db_path)
        try:
            catalog.ensure_source(conn, "bilibili-personal", "bilibili_favorites", "bilibili")
            catalog.update_sync_state(conn, "bilibili-personal", last_success_at=now_iso())
            catalog.ensure_source(conn, "douyin-personal", "douyin_favorites", "douyin")
            catalog.update_sync_state(conn, "douyin-personal", state="paused")
            catalog.ensure_source(conn, "youtube-personal", "youtube_favorites", "youtube")
            catalog.update_sync_state(conn, "youtube-personal", state="needs_login", last_success_at=now_iso())
        finally:
            conn.close()

        headers = {"X-Recall-Token": self.server.token}
        release = threading.Event()
        with patch("recall.web.browser_connect", side_effect=lambda *args, **kwargs: release.wait(5)) as launch, \
             patch.object(self.server, "sync", return_value={"status": "succeeded"}):
            try:
                jobs = self.request("/api/connect/pending", {}, **headers)["jobs"]
                self.assertEqual(set(jobs), {"youtube", "zhihu", "xiaohongshu"})
                self.assertEqual(self.request("/api/connect/pending", {}, **headers)["jobs"], jobs)
                self.assertEqual(self.request("/api/connect", {"platform": "zhihu"}, **headers)["job_id"], jobs["zhihu"])
            finally:
                release.set()
            for job in jobs.values():
                self.assertEqual(self.finished_job(job)["status"], "succeeded")
            self.assertEqual(launch.call_count, len(jobs))

    def finished_job(self, job):
        for attempt in range(50):
            result = self.request("/api/jobs/" + job)
            if result["status"] != "running":
                return result
            time.sleep(0.02)
        self.fail("Job did not finish")

    def test_local_bookmarks_import_uses_default_profile_directory(self):
        local = Path(self.tmp.name) / "Local"
        profile = local / "Google" / "Chrome" / "User Data" / "Profile 1"
        profile.mkdir(parents=True)
        file = profile / "Bookmarks"
        file.write_text(json.dumps({"roots": {"bookmark_bar": {
            "type": "folder", "name": "收藏栏", "children": [
                {"type": "url", "name": "示例", "url": "https://example.com/browser"},
                {"type": "url", "name": "本地", "url": "file:///private.txt"},
            ],
        }}}), encoding="utf-8")
        headers = {"X-Recall-Token": self.server.token}
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            for attempt in range(2):
                self.request("/api/bookmarks/import-local", {"platform": "chrome"}, **headers)
            file.write_text('{"roots":', encoding="utf-8")
            with self.assertRaises(HTTPError):
                self.request("/api/bookmarks/import-local", {"platform": "chrome"}, **headers)
        items = self.request("/api/items")["items"]
        self.assertEqual([item["title"] for item in items], ["示例"])
        detail = self.request("/api/items/" + items[0]["item_id"])
        self.assertEqual([entry["path"] for entry in detail["evidence"]], [["Profile 1", "收藏栏"]])

    def test_browser_filter_uses_bookmark_source_not_link_platform(self):
        from recall.ingest import add_link

        add_link(self.server.db_path, "https://github.com/example/repo", "chrome-import", "chrome", title="Chrome")
        add_link(self.server.db_path, "https://github.com/example/repo", "edge-live", "edge", title="Edge")
        add_link(self.server.db_path, "https://example.com/", "other", "web", title="Other")
        for browser, source_id in (("chrome", "chrome-import"), ("edge", "edge-live")):
            with self.subTest(browser=browser):
                items = self.request(f"/api/items?browser={browser}")["items"]
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["platform"], "github")
                self.assertEqual([source["source_id"] for source in items[0]["sources"]], [source_id])

    def test_bookmark_file_polling_without_extensions(self):
        from recall import catalog, db, watch

        local = Path(self.tmp.name) / "Local"
        files = {}
        def save(browser, revision):
            url = f"https://example.org/{browser}-{revision}"
            files[browser].write_text(json.dumps({"roots": {"bookmark_bar": {
                "type": "folder", "name": "Bookmarks", "children": [
                    {"type": "url", "name": f"{browser} {revision}", "url": url},
                ],
            }}}), encoding="utf-8")

        for browser, directory in (("chrome", "Google/Chrome"), ("edge", "Microsoft/Edge Dev")):
            files[browser] = local / directory / "User Data" / "Default" / "Bookmarks"
            files[browser].parent.mkdir(parents=True)
            save(browser, "old")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            for browser in files:
                self.request("/api/bookmarks/import-local", {"platform": browser},
                             **{"X-Recall-Token": self.server.token})
            tick = watch.run_tick(self.server.db_path)
            self.assertEqual({source: result["status"] for source, result in tick["results"].items()},
                             {"chrome-local": "not_modified", "edge-local": "not_modified"})
            for browser in files:
                save(browser, "new")
            conn = db.open_readwrite(self.server.db_path)
            try:
                for browser in files:
                    catalog.update_sync_state(conn, f"{browser}-local", next_run_at=None)
            finally:
                conn.close()
            watch.pause_source(self.server.db_path, "chrome-local")
            self.assertNotIn("chrome-local", watch.run_tick(self.server.db_path)["checked"])
            watch.resume_source(self.server.db_path, "chrome-local")
            watch.run_tick(self.server.db_path)
            for browser in files:
                items = self.request(f"/api/items?browser={browser}")["items"]
                self.assertEqual({item["title"] for item in items}, {f"{browser} old", f"{browser} new"})

    def test_colloquial_question_finds_private_title_and_reports_missing_body(self):
        from recall.ingest import add_link

        title = "高盛给每个量化研究员开高薪，靠的就是这个仓库！"
        add_link(self.server.db_path, "https://example.com/saved", "private", "web", title=title, access="restricted")
        headers = {"X-Recall-Token": self.server.token}
        with patch.dict(os.environ, {"RECALL_CLOUD_INCLUDE_RESTRICTED": "0"}), \
             patch("recall.web.deepseek_key", return_value="fake"), \
             patch("recall.web.urllib.request.urlopen") as local, patch("recall.web.urllib.request.build_opener") as cloud:
            result = self.request("/api/chat", {"text": "高盛靠的是啥", "history": [{"role": "user", "text": "上句话"}]}, **headers)
        self.assertEqual([item["title"] for item in result["items"]], [title])
        self.assertIn("只拿到标题", result["text"])
        cloud.assert_not_called()
        local.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "optional video dependency: yt-dlp")
    def test_video_metadata_uses_saved_url_without_downloading(self):
        from yt_dlp import YoutubeDL

        headers = {"X-Recall-Token": self.server.token}
        saved_url = "https://www.youtube.com/watch?v=BaW_jenozKc"
        self.request("/api/add", {"url": saved_url}, **headers)
        item = self.request("/api/items")["items"][0]
        with patch.object(YoutubeDL, "extract_info", return_value={
            "title": "Video", "subtitles": {"en": [{"url": "https://example.com/private"}]},
            "url": "https://example.com/stream",
        }) as extract:
            job = self.request("/api/media/video-info", {"item_id": item["item_id"]}, **headers)
            info = self.finished_job(job["job_id"])["result"]
        extract.assert_called_once_with(saved_url, download=False, ie_key="Youtube")
        self.assertEqual(info["subtitles"], ["en"])
        self.assertNotIn("url", info)

        with patch.object(YoutubeDL, "extract_info", return_value={
            "title": "youtube video #BaW_jenozKc", "extractor": "youtube", "id": "BaW_jenozKc",
        }):
            job = self.request("/api/media/video-info", {"item_id": item["item_id"]}, **headers)
            self.assertEqual(self.finished_job(job["job_id"])["status"], "failed")

    def test_archive_only_fact_reaches_chat_and_mcp(self):
        from recall.ingest import add_link
        from recall.mcp_server import handle_tool_call
        from recall.media import archive, tasks
        from recall.tools import RecallTools

        add_link(self.server.db_path, "https://example.com/article", "public", "web", title="Article")
        add_link(self.server.db_path, "https://example.com/private", "private", "web",
                 title="Private", access="restricted")
        bodies = {"article": "The archive password is violetcobalt.", "private": "Do not disclose privatecobalt."}
        with patch.object(archive, "enabled", return_value=True):
            for _ in bodies:
                outcome = archive.run_archive_tick(self.server.db_path, extractor=lambda url: (
                    tasks.text_chunks(bodies[url.rsplit("/", 1)[-1]], "article"), None
                ))
                self.assertEqual(outcome["status"], "ready")
            self.assertIsNone(archive.run_archive_tick(self.server.db_path))

        headers = {"X-Recall-Token": self.server.token}
        with patch("recall.web.answer", return_value="violetcobalt [1]") as answer, \
             patch("recall.web.deepseek_key", return_value="fake"):
            chat = self.request("/api/chat", {"text": "violetcobalt"}, **headers)
            self.assertIn(bodies["article"], answer.call_args.args[1][0]["snippets"][0]["text"])
            self.assertEqual(chat["text"], "violetcobalt [1]")
            self.assertEqual(chat["items"][0]["url"], "https://example.com/article")
            self.assertEqual(answer.call_args.kwargs["key"], "fake")
            with patch.dict(os.environ, {"RECALL_CLOUD_INCLUDE_RESTRICTED": "0"}):
                private = self.request("/api/chat", {"text": "privatecobalt"}, **headers)
            self.assertEqual(private["items"][0]["title"], "Private")
            self.assertIsNone(answer.call_args.kwargs["key"])
        with RecallTools.open(self.server.db_path, {"public", "private"}) as tools:
            public = handle_tool_call(tools, "recall_search", {"query": "violetcobalt"})["structuredContent"]
            self.assertEqual(public["items"][0]["snippets"][0]["locator"], {"part": 1})
            self.assertEqual(tools.recall_search(query="privatecobalt")["items"], [])
        with RecallTools.open(self.server.db_path, {"private"}, include_restricted=True) as tools:
            self.assertTrue(tools.recall_search(query="privatecobalt")["items"])

    def test_answer_sends_archived_evidence_not_untrusted_history(self):
        from unittest.mock import MagicMock
        from recall.web import answer

        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = (
            b'{"choices":[{"message":{"content":"violetcobalt [1]"}}]}'
        )
        with patch("recall.web.urllib.request.build_opener", return_value=opener):
            result = answer("What is the marker?", [{
                "title": "Article", "snippets": [{"label": "正文", "text": "Marker: violetcobalt",
                                                   "locator": {"part": 1}}],
            }], [{"role": "assistant", "text": "privatecobalt"}], key="fake")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertIn("Marker: violetcobalt", payload["messages"][-1]["content"])
        self.assertNotIn("privatecobalt", json.dumps(payload))
        self.assertEqual(result, "violetcobalt [1]")

    def test_local_answer_labels_single_source_when_model_omits_reference(self):
        from io import BytesIO
        from recall.web import answer

        with patch("recall.web.urllib.request.urlopen", side_effect=[
            BytesIO(b'{"models":[{"name":"local"}]}'),
            BytesIO(b'{"message":{"content":"The saved note names archivecobalt."}}'),
        ]):
            result = answer("Which project?", [{"title": "Note", "snippets": [{"text": "archivecobalt"}]}])
        self.assertTrue(result.endswith("参考原文：[1]"))


class MediaRegressionTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("numpy"), "optional media dependency: numpy")
    def test_small_decoder_frames_are_combined_before_asr(self):
        import sys
        import types
        import numpy as np
        from recall.media.engines import default_asr

        seen = []

        class Stream:
            result = types.SimpleNamespace(text="识别文字")

            def accept_waveform(self, rate, samples):
                seen.append((rate, len(samples)))

        class Recognizer:
            def create_stream(self):
                return Stream()

            def decode_stream(self, stream):
                pass

        module = types.SimpleNamespace(
            OfflineRecognizer=types.SimpleNamespace(from_paraformer=lambda **kwargs: Recognizer())
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict("sys.modules", {"sherpa_onnx": module}), \
             patch.dict("os.environ", {"RECALL_ASR_MODEL_DIR": directory}), \
             patch("recall.media.engines.decode_audio_16k", return_value=[np.zeros(800)] * 4):
            segments = default_asr("sample.wav")
        self.assertEqual(seen, [(16000, 3200)])
        self.assertEqual(segments[0]["locator"]["end_seconds"], 0.2)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class CredentialTest(unittest.TestCase):
    def test_encrypted_credential_round_trip(self):
        from recall.secrets import load_secret, save_secret
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.dpapi"
            with patch("recall.secrets._secret_path", return_value=path):
                self.assertTrue(save_secret("github", "test-only-secret"))
                self.assertNotIn(b"test-only-secret", path.read_bytes())
                self.assertEqual(load_secret("github"), "test-only-secret")


if __name__ == "__main__":
    unittest.main()
