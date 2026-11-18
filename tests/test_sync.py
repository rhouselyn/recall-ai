"""R06 / R09：GitHub 模拟多页、身份变化、限流、断线、快照保护与调度。"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any

from recall import catalog
from recall.clock import advance, freeze
from recall.db import catalog_version, init_db, open_readwrite
from recall.sources.http import HttpResponse
from recall.sources.runner import sync_once
from recall.tools import RecallTools
from recall.watch import run_tick
from recall.errors import RecallError
from recall.sources.douyin import parse_listcollection
from recall.sources.xiaohongshu import parse_collect_page
from recall.sources.zhihu_web import parse_collection_items
from recall.sources.zhihu_web import ZhihuWebConnector
from recall.sources.xiaohongshu import XiaohongshuFavoritesConnector
from recall.sources.douyin import DouyinFavoritesConnector
from recall.sources.youtube import YouTubeFavoritesConnector

BASE = "https://api.github.com"


def json_response(payload: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


def star(repo_id: int, name: str, *, private: bool = False, starred_at: str = "2026-10-01T00:00:00Z") -> dict:
    return {
        "starred_at": starred_at,
        "repo": {
            "id": repo_id, "full_name": name,
            "html_url": f"https://github.com/{name}",
            "description": "示例仓库", "private": private,
        },
    }


class FakeHttp:
    def __init__(self, script: dict[str, list[HttpResponse]]) -> None:
        self.script = script
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        self.calls.append(url)
        queue = self.script.get(url)
        if not queue:
            raise AssertionError(f"unexpected request: {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class PlatformResponseTest(unittest.TestCase):
    def test_xiaohongshu_favorites_stay_on_the_logged_in_domain(self):
        from unittest.mock import Mock
        from recall.sources.browser import PlaywrightBrowserSession

        page = Mock(url="https://www.xiaohongshu.com/explore")
        page.evaluate.return_value = [{"id": "note", "noteCard": {"displayTitle": "Saved"}}]
        session = PlaywrightBrowserSession(page, None, None, None)
        session.identity = Mock(return_value={"user_id": "me"})
        result = session._xhs_collection({})
        page.goto.assert_called_once_with("https://www.xiaohongshu.com/user/profile/me?tab=fav&subTab=note",
                                          wait_until="domcontentloaded", timeout=30000)
        self.assertEqual(result["data"]["notes"][0]["note_id"], "note")

    def test_rss_last_modified_survives_runner_state(self):
        from recall.sources.rss import RssConnector
        from unittest.mock import Mock
        client = Mock()
        client.request.return_value = HttpResponse(304)
        stamp = "Fri, 13 Nov 2026 01:00:00 GMT"
        result = RssConnector("rss", {"feed_url": "https://example.com/feed"}, http=client).fetch(
            state={"last_summary": json.dumps({"text": stamp})})
        self.assertTrue(result.not_modified)
        self.assertEqual(client.request.call_args.kwargs["headers"]["If-Modified-Since"], stamp)

    def test_youtube_reads_private_lists_and_resumes_without_reconciliation(self):
        class Session:
            def status(self): return "ok"
            def youtube_playlists(self):
                return "account-hash", [{"id": "WL", "title": "稍后观看"},
                                        {"id": "LL", "title": "喜欢的视频"}]
            def youtube_playlist(self, playlist_id, *, limit=None):
                return [{"id": "abcdefghijk", "title": playlist_id}]

        connector = YouTubeFavoritesConnector("yt", session=Session())
        first = connector.fetch("full", budget={"max_requests": 1})
        self.assertEqual(first.continuation, {"playlist_id": "LL"})
        self.assertEqual(first.entries[0].access, "restricted")
        self.assertEqual(first.entries[0].safe.platform, "youtube")
        resumed = connector.fetch("full", state={"last_summary": json.dumps({"continuation_full": first.continuation})})
        self.assertEqual(resumed.entries[0].path, ("喜欢的视频",))
        self.assertFalse(resumed.complete)
        with self.assertRaises(RecallError):
            YouTubeFavoritesConnector("yt", {"account_id": "another"}, session=Session()).fetch()

    def test_invalid_platform_payloads_are_not_empty_successes(self):
        for parser in (parse_listcollection, parse_collect_page, parse_collection_items):
            with self.subTest(parser=parser.__name__), self.assertRaises(RecallError):
                parser({"error": "login required"})

    def test_zhihu_next_page_url(self):
        _, ended, offset = parse_collection_items({
            "data": [],
            "paging": {"is_end": False, "next": "https://www.zhihu.com/api/v4/collections/1/items?offset=20&limit=20"},
        })
        self.assertFalse(ended)
        self.assertEqual(offset, 20)

    def test_zhihu_checks_owner_and_uses_readable_article_url(self):
        class Session:
            def status(self): return "ok"
            def identity(self): return {"user_id": "me"}
            def get_json(self, url, **kwargs):
                if url == "/api/v4/me": return {"id": "me", "url_token": "my-name"}
                if url.endswith("/items"):
                    return {"data": [{"content": {"id": 7, "type": "article", "url": "https://www.zhihu.com/api/v4/articles/7"}}],
                            "paging": {"is_end": True}}
                return {"collection": {"creator": {"id": "me"}}}

        result = ZhihuWebConnector("z", {"collections": [{"id": "12"}]}, session=Session()).fetch("full")
        self.assertEqual(result.entries[0].safe.canonical_url, "https://zhuanlan.zhihu.com/p/7")
        self.assertEqual(result.entries[0].access, "restricted")

        class WrongOwner(Session):
            def get_json(self, url, **kwargs):
                if url == "/api/v4/me": return {"id": "me"}
                return {"collection": {"creator": {"id": "someone-else"}}}

        with self.assertRaises(RecallError):
            ZhihuWebConnector("z", {"collections": [{"id": "12"}]}, session=WrongOwner()).fetch("full")

        class Discovered(Session):
            def visible_cards(self, selector):
                return [{"href": href} for href in ("/collection/11", "/collection/12", "https://example.net/collection/13")]
            def get_json(self, url, **kwargs):
                if url == "/api/v4/collections/11":
                    return {"collection": {"creator": {"id": "someone-else"}}}
                if "/collections/11/items" in url or "/collections/13" in url:
                    raise AssertionError("Must not read another owner's or an external site's collection")
                return super().get_json(url, **kwargs)

        discovered = ZhihuWebConnector("z", {}, session=Discovered()).fetch("full")
        self.assertEqual(len(discovered.entries), 1)
        self.assertEqual(discovered.coverage, "authorized_subset")
        self.assertFalse(discovered.complete)

    def test_social_favorites_are_restricted(self):
        class Session:
            def status(self): return "ok"
            def identity(self): return {"user_id": "me"}
            def get_json(self, url, **kwargs):
                if "xiaohongshu" in url or "sns" in url:
                    return {"data": {"notes": [{"id": "a", "title": "笔记"}], "has_more": False}}
                return {"aweme_list": [{"aweme_id": "1", "desc": "视频"}], "has_more": False}

        for connector in (XiaohongshuFavoritesConnector("x", session=Session()),
                          DouyinFavoritesConnector("d", session=Session())):
            with self.subTest(connector=connector.kind):
                result = connector.fetch("full")
                self.assertFalse(result.complete)
                self.assertEqual(result.entries[0].access, "restricted")
                self.assertEqual(result.account_id, "me")

    def test_bilibili_budget_keeps_first_page(self):
        from recall.sources.bilibili import BilibiliFavoritesConnector

        class Session:
            def status(self): return "ok"
            def get_json(self, url, **kwargs):
                if url.endswith("/nav"):
                    return {"data": {"isLogin": True, "mid": 123}}
                if url.endswith("/list-all"):
                    return {"data": {"list": [
                        {"id": 1, "title": "first"}, {"id": 2, "title": "second"}
                    ]}}
                return {"data": {"medias": [
                    {"bvid": "BV1234567890", "title": "first video"}
                ], "has_more": True}}

        result = BilibiliFavoritesConnector("b", session=Session()).fetch(
            "full", budget={"max_requests": 3}
        )
        self.assertEqual(len(result.entries), 1)
        self.assertFalse(result.complete)
        self.assertEqual(result.continuation, {"folder_id": "1", "page": 2})

        seen = []
        class ResumedSession(Session):
            def get_json(inner, url, **kwargs):
                if url.endswith("/resource/list"):
                    seen.append(kwargs["params"]["pn"])
                    return {"data": {"medias": [
                        {"bvid": "BV1234567891", "title": "next"}
                    ], "has_more": False}}
                return super().get_json(url, **kwargs)

        resumed = BilibiliFavoritesConnector("b", session=ResumedSession()).fetch(
            "full", state={"last_summary": json.dumps({"continuation_full": result.continuation})},
            budget={"max_requests": 4},
        )
        self.assertEqual(seen, [2, 1])
        self.assertIsNone(resumed.continuation)
        self.assertTrue(resumed.complete)  # runner 负责验证并合并前面各批的证据。

    def test_bilibili_empty_folder_is_not_a_format_error(self):
        from recall.sources.bilibili import parse_medias
        items, more = parse_medias({"data": {"medias": None, "info": {"media_count": 0}, "has_more": False}})
        self.assertEqual((items, more), ([], False))


def two_page_script(page2: HttpResponse | None = None) -> dict[str, list[HttpResponse]]:
    link = f'<{BASE}/user/starred?per_page=100&page=2>; rel="next"'
    return {
        f"{BASE}/user": [json_response({"id": 42, "login": "sample"})],
        f"{BASE}/user/starred?per_page=100&page=1": [
            json_response(
                [star(1, "acme/one"), star(2, "acme/two", private=True), star(3, "acme/three")],
                headers={"Link": link, "ETag": '"v1"'},
            )
        ],
        f"{BASE}/user/starred?per_page=100&page=2": [
            page2 or json_response([star(4, "acme/four")])
        ],
    }


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "recall.db"
        init_db(self.db)
        freeze("2026-11-10T09:00:00Z")
        conn = open_readwrite(self.db)
        try:
            catalog.ensure_source(
                conn, "github-personal", "github_stars", "github",
                coverage="none", refresh_mode="poll", sync_enabled=True,
            )
        finally:
            conn.close()
        self.env = {"RECALL_GITHUB_TOKEN": "TEST_TOKEN_VALUE"}

    def tearDown(self) -> None:
        freeze(None)
        self.tmp.cleanup()

    def count_active(self) -> int:
        conn = open_readwrite(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM entries e JOIN collections c "
                "ON c.collection_id = e.collection_id "
                "WHERE c.source_id = 'github-personal' AND e.active = 1"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_full_multipage_then_rerun_is_idempotent(self) -> None:
        http = FakeHttp(two_page_script())
        first = sync_once(self.db, "github-personal", full=True, http=http, env=self.env)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["coverage"], "authorized_view")
        self.assertEqual(self.count_active(), 4)
        second = sync_once(self.db, "github-personal", full=True, http=http, env=self.env)
        self.assertEqual(second["counts"]["entries_added"], 0)
        self.assertEqual(self.count_active(), 4)
        self.assertEqual(second["catalog_version"], first["catalog_version"])
        with RecallTools.open(str(self.db)) as t:
            result = t.recall_search(query="acme")
            self.assertEqual(len(result["items"]), 3)  # 私有仓库默认受限
            private = t.recall_search(query="acme/two")
            self.assertEqual(private["items"], [])
        with RecallTools.open(str(self.db), include_restricted=True) as t:
            self.assertEqual(len(t.recall_search(query="acme")["items"]), 4)
        conn = open_readwrite(self.db)
        try:
            state = catalog.get_sync_state(conn, "github-personal")
            self.assertEqual(state["coverage"], "authorized_view")
            self.assertIsNotNone(state["last_full_sync_at"])
            self.assertNotIn("TEST_TOKEN_VALUE", json.dumps(dict(state)))
        finally:
            conn.close()

    def test_pause_during_fetch_prevents_commit_and_concurrent_sync(self) -> None:
        from recall.watch import pause_source

        class PausingHttp(FakeHttp):
            def request(inner, method, url, **kwargs):
                if not inner.calls:
                    concurrent = sync_once(self.db, "github-personal", full=True,
                                           http=FakeHttp(two_page_script()), env=self.env)
                    self.assertEqual(concurrent["reason"], "lease")
                    pause_source(self.db, "github-personal")
                return super().request(method, url, **kwargs)

        result = sync_once(self.db, "github-personal", full=True,
                           http=PausingHttp(two_page_script()), env=self.env)
        self.assertEqual(result["reason"], "lease_lost")
        self.assertEqual(self.count_active(), 0)
        conn = open_readwrite(self.db)
        try:
            self.assertEqual(catalog.get_sync_state(conn, "github-personal")["state"], "paused")
        finally:
            conn.close()

    def test_incomplete_full_attempt_does_not_repeat_every_tick(self) -> None:
        from recall.sources.base import FetchResult
        modes = []

        class Connector:
            trusted = False

            def fetch(self, mode, **kwargs):
                modes.append(mode)
                return FetchResult(coverage="authorized_subset", complete=False)

        with patch("recall.sources.runner.get_connector", return_value=Connector()):
            run_tick(self.db)
            advance(seconds=91)
            run_tick(self.db)
        self.assertEqual(modes, ["full", "head"])

    def test_account_mismatch_keeps_old_snapshot(self) -> None:
        sync_once(self.db, "github-personal", full=True,
                  http=FakeHttp(two_page_script()), env=self.env)
        script = two_page_script()
        script[f"{BASE}/user"] = [json_response({"id": 99, "login": "other"})]
        failed = sync_once(self.db, "github-personal", full=True,
                           http=FakeHttp(script), env=self.env)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "ACCOUNT_MISMATCH")
        self.assertEqual(self.count_active(), 4)

    def test_second_page_failure_keeps_old_snapshot(self) -> None:
        sync_once(self.db, "github-personal", full=True,
                  http=FakeHttp(two_page_script()), env=self.env)
        broken = two_page_script(json_response({}, status=503))
        failed = sync_once(self.db, "github-personal", full=True,
                           http=FakeHttp(broken), env=self.env)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.count_active(), 4)
        conn = open_readwrite(self.db)
        try:
            state = catalog.get_sync_state(conn, "github-personal")
            self.assertEqual(state["latest_attempt"], "failed")
            self.assertEqual(state["last_error_type"], "NETWORK_ERROR")
            self.assertIsNotNone(state["last_success_at"])
            self.assertEqual(state["coverage"], "authorized_view")
        finally:
            conn.close()

    def test_abnormal_empty_snapshot_requires_accept_empty(self) -> None:
        sync_once(self.db, "github-personal", full=True,
                  http=FakeHttp(two_page_script()), env=self.env)
        script = {
            f"{BASE}/user": [json_response({"id": 42})],
            f"{BASE}/user/starred?per_page=100&page=1": [json_response([])],
        }
        blocked = sync_once(self.db, "github-personal", full=True,
                            http=FakeHttp(script), env=self.env)
        self.assertEqual(blocked["status"], "failed")
        self.assertEqual(blocked["error"]["code"], "SNAPSHOT_INCOMPLETE")
        self.assertEqual(self.count_active(), 4)
        accepted = sync_once(self.db, "github-personal", full=True, accept_empty=True,
                             http=FakeHttp(script), env=self.env)
        self.assertEqual(accepted["status"], "succeeded")
        self.assertEqual(self.count_active(), 0)

    def test_rate_limit_and_conditional_304(self) -> None:
        script = {
            f"{BASE}/user": [json_response({"id": 42})],
            f"{BASE}/user/starred?per_page=100&page=1": [
                json_response({}, status=403, headers={"X-RateLimit-Remaining": "0"})
            ],
        }
        limited = sync_once(self.db, "github-personal", http=FakeHttp(script), env=self.env)
        self.assertEqual(limited["error"]["code"], "RATE_LIMITED")
        not_modified_script = {
            f"{BASE}/user": [json_response({"id": 42})],
            f"{BASE}/user/starred?per_page=100&page=1": [
                json_response({}, status=304, headers={"ETag": '"v2"'})
            ],
        }
        conn = open_readwrite(self.db)
        catalog.update_sync_state(conn, "github-personal", last_etag='"v1"')
        conn.close()
        result = sync_once(self.db, "github-personal",
                           http=FakeHttp(not_modified_script), env=self.env)
        self.assertEqual(result["status"], "not_modified")

    def test_rate_limit_respects_server_retry_delay(self):
        script = {
            f"{BASE}/user": [json_response({"id": 42})],
            f"{BASE}/user/starred?per_page=100&page=1": [
                json_response({}, status=429, headers={"Retry-After": "600"})
            ],
        }
        result = sync_once(self.db, "github-personal", http=FakeHttp(script), env=self.env)
        self.assertEqual(result["next_retry_at"], "2026-11-10T09:10:00Z")

    def test_watch_rotation_with_fixed_clock(self) -> None:
        http = FakeHttp({
            f"{BASE}/user": [json_response({"id": 42})],
            f"{BASE}/user/starred?per_page=100&page=1": [
                json_response([star(1, "acme/one")], headers={"ETag": '"w1"'})
            ],
        })
        first = run_tick(str(self.db), http=http, env=self.env)
        self.assertEqual(first["checked"], ["github-personal"])
        second = run_tick(str(self.db), http=http, env=self.env)
        self.assertEqual(second["checked"], [])  # 未到期且未重复补跑
        advance(91)
        third = run_tick(str(self.db), http=http, env=self.env)
        self.assertEqual(third["checked"], ["github-personal"])

    def test_watch_does_not_postpone_a_source_busy_archiving(self):
        with patch("recall.watch.sync_once", return_value={"status": "skipped", "reason": "lease"}):
            run_tick(str(self.db), env=self.env)
        conn = open_readwrite(self.db)
        try:
            self.assertIsNone(catalog.get_sync_state(conn, "github-personal")["next_run_at"])
        finally:
            conn.close()

    def test_no_token_fails_without_touching_data(self) -> None:
        failed = sync_once(self.db, "github-personal", http=FakeHttp({}), env={})
        self.assertEqual(failed["error"]["code"], "AUTH_REQUIRED")
        conn = open_readwrite(self.db)
        try:
            state = catalog.get_sync_state(conn, "github-personal")
            self.assertEqual(state["state"], "needs_login")
            self.assertIsNotNone(state["next_retry_at"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
