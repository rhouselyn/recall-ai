"""Background enrichment of saved links; all output is ordinary searchable evidence."""
from __future__ import annotations

import io
import os
import re
from datetime import timedelta
from importlib.util import find_spec
from urllib.parse import urlsplit

from .. import catalog
from ..clock import iso, now, now_iso
from ..db import open_readwrite
from ..errors import RecallError, from_exception
from ..leases import heartbeat
from ..sources.browser import load_session, profile_lease
from . import tasks
from .bilibili import archive_bilibili
from .fetch import fetch_public
from .video import EXTRACTORS, archive_video

ENGINE = "link-archive-1"
SESSION_KINDS = frozenset({"xiaohongshu_favorites", "bilibili_favorites"})


def enabled():
    return os.environ.get("RECALL_ARCHIVE", "1") != "0" and all(
        find_spec(package) is not None
        for package in ("trafilatura", "lxml_html_clean", "urllib3", "yt_dlp", "webvtt")
    )


def archive_link(url):
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.netloc in EXTRACTORS:
        try:
            return archive_video(url)
        except RecallError as exc:
            if exc.code != "INVALID_ARGUMENT":
                raise
    output = io.BytesIO()
    repo = re.fullmatch(r"/([\w-]+/[\w.-]+)/?", parts.path, flags=re.ASCII)
    if parts.scheme == "https" and parts.netloc == "github.com" and repo:
        try:
            fetch_public("https://api.github.com/repos/" + repo[1] + "/readme", output, limit=1024 * 1024,
                         media_types=("text/plain", "application/vnd.github.raw", "application/vnd.github.raw+json"),
                         accept="application/vnd.github.raw+json")
            return tasks.text_chunks(output.getvalue().decode("utf-8-sig", errors="replace"), "readme"), None
        except RecallError as exc:
            if exc.code != "NOT_FOUND":
                raise
    from trafilatura import extract

    fetch_public(url, output, limit=2 * 1024 * 1024, media_types=("text/html", "application/xhtml+xml"))
    text = extract(output.getvalue(), favor_precision=True, include_comments=False, include_tables=True)
    return tasks.text_chunks(text or "", "article"), None


def _archive_signed_in(db_path, entry):
    if not entry["remote_account_id"]:
        raise RecallError("AUTH_REQUIRED", "尚未绑定平台账号，请先同步收藏。")
    with profile_lease(entry["source_id"], db_path, purpose="archive"):
        try:
            session = load_session(entry["source_id"], entry["kind"], db_path)
        except RecallError:
            raise
        except Exception as exc:
            raise RecallError("NETWORK_ERROR", "无法打开平台登录会话。") from exc
        try:
            if entry["kind"] == "bilibili_favorites":
                try:
                    return archive_bilibili(session, entry["link_url"], entry["remote_account_id"])
                except RecallError:
                    raise
                except (KeyError, TypeError, ValueError) as exc:
                    raise RecallError("SOURCE_FORMAT_CHANGED", "B 站字幕或播放信息格式已变化。") from exc
            if str(session.identity().get("user_id") or "") != entry["remote_account_id"]:
                raise RecallError("ACCOUNT_MISMATCH", "小红书登录账号与收藏来源不一致。")
            chunks = tasks.text_chunks(session.xiaohongshu_note_text(entry["link_url"]), "article")
        finally:
            session.close()
    return chunks, "NO_CAPTIONS" if chunks and entry["note"] == "type=video" else None


def run_archive_tick(db_path, *, extractor=None):
    if not enabled():
        return None
    conn = open_readwrite(db_path)
    try:
        with catalog.transaction(conn):
            expired = iso(now() - timedelta(minutes=5))
            retry_before = iso(now() - timedelta(minutes=15))
            conn.execute("UPDATE media_tasks SET status='failed', error_code='WORKER_EXPIRED' "
                         "WHERE kind='archive' AND status='processing' AND attempts>=3 AND updated_at<?", (expired,))
            entry = conn.execute(
                "SELECT e.entry_id, e.link_url, e.note, c.source_id, src.kind, src.remote_account_id, t.task_id "
                "FROM entries e JOIN collections c USING(collection_id) JOIN sources src USING(source_id) "
                "LEFT JOIN sync_state s USING(source_id) "
                "LEFT JOIN media_tasks t ON t.entry_id=e.entry_id AND t.kind='archive' "
                "WHERE e.active=1 AND COALESCE(s.state,'idle')!='paused' "
                "AND (src.kind NOT IN ('xiaohongshu_favorites','bilibili_favorites') OR "
                "(COALESCE(s.state,'idle')!='needs_login' AND (s.lease_owner IS NULL OR s.lease_until<=?) "
                "AND (src.sync_enabled=0 OR s.next_run_at IS NULL OR s.next_run_at>?))) "
                "AND NOT EXISTS(SELECT 1 FROM media_tasks n WHERE n.entry_id=e.entry_id "
                "AND n.kind='source_body' AND n.status='ready') "
                "AND (t.task_id IS NULL OR (t.attempts<3 AND (t.status='pending' OR "
                "((t.status='failed' AND t.updated_at<?) "
                "OR (t.status='partial' AND t.error_code IN ('NETWORK_ERROR','RATE_LIMITED') AND t.updated_at<?) "
                "OR (t.status='processing' AND t.updated_at<?))))) "
                "ORDER BY CASE WHEN t.status='pending' THEN 0 WHEN t.task_id IS NULL THEN 1 ELSE 2 END, "
                "e.imported_at DESC, e.entry_id LIMIT 1",
                (now_iso(), iso(now() + timedelta(seconds=5)), retry_before, retry_before, expired),
            ).fetchone()
            if not entry:
                return None
            task_id = entry["task_id"] or tasks.enqueue(
                conn, entry["entry_id"], "archive", entry["link_url"].encode(), engine_version=ENGINE,
            )[0]
            conn.execute("UPDATE media_tasks SET status='pending' WHERE task_id=? AND status='partial'", (task_id,))
            task = tasks.claim(conn, task_id=task_id)[0]
        with heartbeat(db_path, lambda connection: connection.execute(
            "UPDATE media_tasks SET updated_at=? WHERE task_id=? AND status='processing' AND attempts=?",
            (now_iso(), task_id, task["attempts"]),
        ).rowcount):
            signed_in = extractor is None and entry["kind"] in SESSION_KINDS
            try:
                if signed_in:
                    segments, warning = _archive_signed_in(db_path, entry)
                else:
                    segments, warning = (extractor or archive_link)(entry["link_url"])
                status = ("partial" if warning else "ready") if segments else (
                    "failed" if warning in ("NETWORK_ERROR", "RATE_LIMITED") else
                    "unavailable" if warning == "CONFIG_ERROR" else "empty"
                )
            except Exception as exc:
                error = from_exception(exc)
                segments, warning = [], error.code
                status = "failed" if error.retryable else "unavailable"
            with catalog.transaction(conn):
                current = conn.execute(
                    "SELECT e.active, e.link_url, e.access, c.access AS collection_access, s.state, src.remote_account_id "
                    "FROM entries e JOIN collections c USING(collection_id) JOIN sources src USING(source_id) "
                    "LEFT JOIN sync_state s USING(source_id) WHERE e.entry_id=?",
                    (entry["entry_id"],),
                ).fetchone()
                if not current:
                    return None
                if (current["link_url"] != entry["link_url"] or not current["active"]
                    or current["state"] == "paused"
                    or (signed_in and current["remote_account_id"] != entry["remote_account_id"])):
                    if tasks.set_status(conn, task, "pending"):
                        conn.execute("UPDATE media_tasks SET attempts=0 WHERE task_id=?", (task_id,))
                    return None
                if not tasks.set_status(conn, task, status, error_code=warning):
                    return None
                if segments:
                    access = "restricted" if signed_in or "restricted" in (current["access"], current["collection_access"]) else "standard"
                    if status == "ready" or not conn.execute(
                        "SELECT 1 FROM media_segments WHERE task_id=? AND kind IN ('article','caption','asr')",
                        (task_id,),
                    ).fetchone():
                        tasks.write_segments(conn, task_id, entry["entry_id"], "archive", access, segments)
                elif status == "empty" and conn.execute(
                    "SELECT 1 FROM media_segments WHERE task_id=?", (task_id,)
                ).fetchone():
                    conn.execute("UPDATE media_tasks SET status='partial' WHERE task_id=?", (task_id,))
                    status = "partial"
            return {"entry_id": entry["entry_id"], "status": status, "error_code": warning}
    finally:
        conn.close()


def retry_archive(db_path, item_id):
    if not enabled():
        raise RecallError("CONFIG_ERROR", "自动归档未启用；请安装 .[archive] 并检查 RECALL_ARCHIVE。")
    conn = open_readwrite(db_path)
    try:
        conn.execute(
            "UPDATE media_tasks SET status='pending', attempts=0, error_code=NULL "
            "WHERE kind='archive' AND status IN ('failed','empty','partial','unavailable') "
            "AND entry_id IN (SELECT entry_id FROM entries WHERE item_id=? AND active=1)",
            (item_id,),
        )
    finally:
        conn.close()
