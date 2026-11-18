from __future__ import annotations

import io
import socket
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from recall import catalog, db
from recall.errors import RecallError
from recall.ingest import add_link
from recall.media import archive
from recall.media.extract import index_caption, run_pending
from recall.tools import RecallTools
from recall.urls import sanitize_url


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(db.init_db(Path(temporary.name) / "recall.db"))

    def test_archive_backfills_and_retries_without_replacing_old_evidence_on_failure(self):
        add_link(self.path, "https://example.org/video", "saved", "web", title="Video")
        with RecallTools.open(self.path) as tools:
            item = tools.recall_search()["items"][0]["item_id"]
        with patch.object(archive, "enabled", return_value=True):
            first = archive.run_archive_tick(self.path, extractor=lambda url: ([
                {"kind": "description", "text": "木星磁场证据", "locator": {"part": 1}},
            ], "NO_CAPTIONS"))
            self.assertEqual(first["status"], "partial")
            archive.retry_archive(self.path, item)
            self.assertEqual(run_pending(self.path)["processed"], [])
            failed = archive.run_archive_tick(self.path, extractor=lambda url: ([], "NETWORK_ERROR"))
            self.assertEqual(failed["status"], "failed")
            self.assertIsNone(archive.run_archive_tick(self.path))
        with RecallTools.open(self.path) as tools:
            self.assertEqual(len(tools.recall_search(query="木星")["items"]), 1)
            self.assertEqual(len(tools.recall_search(query="木星磁场")["items"]), 1)
            self.assertEqual(tools.recall_get_item(item)["evidence"][0]["archive"]["status"], "failed")

    def test_xiaohongshu_archive_uses_bound_session_and_keeps_text_restricted(self):
        url = "https://www.xiaohongshu.com/explore/abcdef"
        conn = db.open_readwrite(self.path)
        try:
            catalog.ensure_source(conn, "xhs", "xiaohongshu_favorites", "xiaohongshu", remote_account_id="me")
            catalog.merge_entries(conn, "xhs", [catalog.EntryInput(
                sanitize_url(url), title="Saved note", access="restricted", note="type=video",
            )])
        finally:
            conn.close()
        with patch.object(archive, "enabled", return_value=True), patch.object(archive, "load_session") as load, \
             patch.object(archive, "archive_link") as anonymous:
            session = load.return_value
            session.identity.return_value = {"user_id": "different"}
            outcome = archive.run_archive_tick(self.path)
            self.assertEqual(outcome["error_code"], "ACCOUNT_MISMATCH")
            session.xiaohongshu_note_text.assert_not_called()
            with RecallTools.open(self.path, include_restricted=True) as tools:
                item_id = tools.recall_search()["items"][0]["item_id"]
            archive.retry_archive(self.path, item_id)
            session.identity.return_value = {"user_id": "me"}
            session.xiaohongshu_note_text.return_value = "The original mention is archivecobalt."
            outcome = archive.run_archive_tick(self.path)
            self.assertEqual((outcome["status"], outcome["error_code"]), ("partial", "NO_CAPTIONS"))
            session.xiaohongshu_note_text.assert_called_once_with(url)
            self.assertEqual(session.close.call_count, 2)
            anonymous.assert_not_called()
        with RecallTools.open(self.path) as tools:
            self.assertFalse(tools.recall_search("archivecobalt")["items"])
        with RecallTools.open(self.path, include_restricted=True) as tools:
            snippet = tools.recall_search("archivecobalt")["items"][0]["snippets"][0]
            self.assertEqual((snippet["url"], snippet["locator"], snippet["kind"]), (url, {"part": 1}, "article"))

    def test_source_supplied_body_and_index_rebuild(self):
        conn = db.open_readwrite(self.path)
        try:
            catalog.ensure_source(conn, "native", "manual", "web")
            entry_id, _ = catalog.upsert_entry(conn, "native", catalog.EntryInput(
                safe=sanitize_url("https://example.org/article"), title="Article",
                access="restricted", body="nativesentinel", external_key="native-1",
            ), catalog.WriteResult())
            item_id = conn.execute("SELECT item_id FROM entries WHERE entry_id=?", (entry_id,)).fetchone()[0]
            index_caption(self.path, item_id, entry_id, "legacycaption")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='segment_fts'").fetchone():
                for suffix in ("insert", "delete", "update"):
                    conn.execute("DROP TRIGGER segment_fts_" + suffix)
                conn.execute("DROP TABLE segment_fts")
        finally:
            conn.close()
        db.init_db(self.path)
        with RecallTools.open(self.path, include_restricted=True) as tools:
            self.assertEqual(len(tools.recall_search(query="nativesentinel")["items"]), 1)
            self.assertEqual(len(tools.recall_search(query="legacycaption")["items"]), 1)
        with RecallTools.open(self.path) as tools:
            self.assertEqual(tools.recall_search(query="nativesentinel")["items"], [])
        with patch.object(archive, "enabled", return_value=True):
            self.assertIsNone(archive.run_archive_tick(self.path))

    @unittest.skipUnless(find_spec("urllib3"), "optional archive dependency")
    def test_fetch_rejects_non_public_resolution_before_connect(self):
        from recall.media.fetch import fetch_public

        records = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=records), patch("urllib3.HTTPSConnectionPool") as pool:
            with self.assertRaises(RecallError) as denied:
                fetch_public("https://example.org", io.BytesIO(), limit=20, media_types=("text/html",))
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
            pool.assert_not_called()

    @unittest.skipUnless(archive.enabled(), "optional archive dependencies")
    def test_extracts_html_body_and_timed_subtitles(self):
        from recall.media.video import archive_video

        paragraph = "A cobalt marker identifies this archived source. This explanation only appears in the article text."
        html = ("<html><body><nav>Archive nav</nav><article><h1>Example article</h1><p>" +
                paragraph + "</p></article></body></html>").encode()
        with patch("recall.media.archive.fetch_public", side_effect=lambda url, output, **kw: output.write(html)):
            chunks, warning = archive.archive_link("https://example.org/article")
        self.assertIsNone(warning)
        self.assertIn(paragraph, chunks[0]["text"])
        vtt = "WEBVTT\n\n00:00:03.500 --> 00:00:05.000\nA hidden transcript fact.\n"
        with patch("recall.media.video._video_info", return_value={
            "subtitles": {"en": [{"ext": "vtt", "data": vtt}]},
        }):
            chunks, warning = archive_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIsNone(warning)
        self.assertEqual(chunks[0]["locator"]["start_seconds"], 3.5)
        self.assertEqual(chunks[0]["text"], "A hidden transcript fact.")

    def test_github_repo_prefers_readme(self):
        with patch("recall.media.archive.fetch_public",
                   side_effect=lambda url, output, **kw: output.write(b"# Project\nREADME-only fact")) as fetch:
            chunks, warning = archive.archive_link("https://github.com/owner/project")
        self.assertEqual(fetch.call_args.args[0], "https://api.github.com/repos/owner/project/readme")
        self.assertEqual(chunks[0]["kind"], "readme")
        self.assertIn("README-only fact", chunks[0]["text"])
        self.assertIsNone(warning)

    def test_bilibili_captions_precede_asr_and_link_to_saved_video_time(self):
        from unittest.mock import Mock
        from recall.media import bilibili

        url = "https://www.bilibili.com/video/BV1Test00001?p=2"
        session = Mock()
        session.get_json.side_effect = [
            {"data": {"mid": 123, "isLogin": True}}, {"data": {"bvid": "BV1Test00001", "pages": [{}, {"cid": 23, "duration": 80}]}},
            {"data": {"subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/test"}]}}},
        ]
        conn = db.open_readwrite(self.path)
        try:
            catalog.ensure_source(conn, "bili", "bilibili_favorites", "bilibili", remote_account_id="123")
            catalog.merge_entries(conn, "bili", [catalog.EntryInput(sanitize_url(url), title="Saved")])
        finally:
            conn.close()
        with patch.object(archive, "enabled", return_value=True), patch.object(archive, "load_session", return_value=session), \
             patch.object(bilibili, "transcribe_audio") as transcribe, patch.object(
                 bilibili, "fetch_public", side_effect=lambda url, output, **kwargs:
                 output.write(b'{"body":[{"from":18.1,"to":20,"content":"captioncobalt"}]}'),
             ):
            self.assertEqual(archive.run_archive_tick(self.path)["status"], "ready")
            transcribe.assert_not_called()
        with RecallTools.open(self.path, include_restricted=True) as tools:
            citation = tools.recall_search("captioncobalt")["items"][0]["snippets"][0]
            self.assertEqual(citation["seek_url"], url + "&t=18")
            self.assertEqual(citation["locator"]["start_seconds"], 18.1)
        with RecallTools.open(self.path) as tools:
            self.assertEqual(tools.recall_search("captioncobalt")["items"], [])

    def test_bilibili_uses_local_audio_when_no_subtitle_track_is_available(self):
        from unittest.mock import Mock
        from recall.media import bilibili

        session = Mock()
        session.get_json.side_effect = [
            {"data": {"mid": 123, "isLogin": True}}, {"data": {"bvid": "BV1Test00001", "pages": [{"cid": 23, "duration": 80}]}},
            {"data": {"subtitle": {"subtitles": []}}},
            {"data": {"dash": {"audio": [{"baseUrl": "https://audio.example.org/direct", "bandwidth": 10}]}}},
        ]
        with patch.object(bilibili, "transcribe_audio", return_value=[
            {"kind": "asr", "text": "local transcript", "locator": {"start_seconds": 29}},
        ]) as transcribe:
            chunks, warning = bilibili.archive_bilibili(session, "https://www.bilibili.com/video/BV1Test00001", "123")
        self.assertIsNone(warning)
        self.assertEqual(chunks[0]["locator"], {"start_seconds": 29, "video_part": 1})
        transcribe.assert_called_once_with("https://audio.example.org/direct", "m4a", referer="https://www.bilibili.com/")
