"""本机持续刷新进程（开发规格 §5.7 / R09）。

同一来源同一时刻只跑一个任务；手动 sync 走同一租约；休眠唤醒后每个到期
来源只补一次；失败分类退避；浏览器书签走事件路径，不在这里轮询。
"""
from __future__ import annotations

import time
import json
from datetime import timedelta
from typing import Any

from . import catalog
from .clock import iso, now, now_iso
from .db import open_readwrite
from .errors import RecallError, from_exception
from .sources.runner import sync_once

POLL_SECONDS: dict[str, int] = {
    "github_stars": 90,
    "bilibili_favorites": 120,
    "zhihu_api": 600,
    "zhihu_web": 180,
    "xiaohongshu_favorites": 180,
    "douyin_favorites": 180,
    "youtube_favorites": 300,
    "rss": 600,
}
FULL_SECONDS: dict[str, int] = {
    "github_stars": 12 * 3600,
    "bilibili_favorites": 6 * 3600,
    "zhihu_api": 24 * 3600,
    "zhihu_web": 24 * 3600,
    "xiaohongshu_favorites": 24 * 3600,
    "douyin_favorites": 24 * 3600,
    "youtube_favorites": 24 * 3600,
    "rss": 24 * 3600,
}
REQUEST_BUDGET: dict[str, int] = {
    "github_stars": 10,
    "zhihu_api": 6,
    "zhihu_web": 8,
    "bilibili_favorites": 12,
    "xiaohongshu_favorites": 8,
    "douyin_favorites": 8,
    "youtube_favorites": 8,
    "rss": 2,
}


def pause_source(db_path: str, source_id: str) -> dict[str, Any]:
    conn = open_readwrite(db_path)
    try:
        if catalog.get_source(conn, source_id) is None:
            raise RecallError("NOT_FOUND")
        with catalog.transaction(conn):
            state = catalog.get_sync_state(conn, source_id)
            owner = state.get("lease_owner")
            catalog.update_sync_state(
                conn, source_id, state="paused",
                lease_owner=(owner if owner.startswith("cancelled:") else "cancelled:" + owner) if owner else None,
            )
        return {"source_id": source_id, "state": "paused"}
    finally:
        conn.close()


def resume_source(db_path: str, source_id: str) -> dict[str, Any]:
    """重新确认身份后恢复；不清除上次失败记录，由下一次同步重置。"""
    conn = open_readwrite(db_path)
    try:
        if catalog.get_source(conn, source_id) is None:
            raise RecallError("NOT_FOUND")
        catalog.update_sync_state(
            conn, source_id, state="idle", next_retry_at=None,
            next_run_at=now_iso(),
        )
        return {"source_id": source_id, "state": "idle"}
    finally:
        conn.close()


def _parse_time(value: str | None):
    if not value:
        return None
    from .urls import parse_timestamp

    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def run_tick(
    db_path: str,
    *,
    http: Any = None,
    session: Any = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """单轮调度。返回本轮检查的来源、任务结果和目录版本。"""
    conn = open_readwrite(db_path)
    checked: list[str] = []
    results: dict[str, Any] = {}
    try:
        rows = conn.execute(
            "SELECT source_id, kind, refresh_mode, sync_enabled FROM sources "
            "WHERE sync_enabled = 1 AND refresh_mode = 'poll' ORDER BY source_id"
        ).fetchall()
        current = now()
        for row in rows:
            sid, kind = row["source_id"], row["kind"]
            state = catalog.get_sync_state(conn, sid)
            if state.get("state") in ("paused", "needs_login"):
                continue
            retry_at = _parse_time(state.get("next_retry_at"))
            if retry_at and retry_at > current:
                continue
            due_at = _parse_time(state.get("next_run_at"))
            if due_at and due_at > current:
                continue
            summary: dict[str, Any] = {}
            try:
                summary = json.loads(state.get("last_summary") or "{}")
                last_attempt = _parse_time(summary.get("last_full_attempt_at"))
            except (ValueError, AttributeError):
                last_attempt = None
            completed_full = _parse_time(state.get("last_full_sync_at"))
            last_full = max((value for value in (completed_full, last_attempt) if value), default=None)
            full_due = last_full is None or (
                current - last_full >= timedelta(seconds=FULL_SECONDS.get(kind, 86400))
            )
            if isinstance(summary, dict) and summary.get("continuation_full"):
                full_due = True
            budget = {"max_requests": REQUEST_BUDGET.get(kind, 8)}
            if kind == "zhihu_api":
                budget["max_pages"] = 3
            checked.append(sid)
            try:
                results[sid] = sync_once(
                    db_path, sid, full=full_due, http=http, session=session,
                    env=env, budget=budget,
                )
            except Exception as exc:
                error = from_exception(exc)
                catalog.update_sync_state(
                    conn, sid, latest_attempt="failed",
                    state="needs_login" if error.code == "AUTH_REQUIRED" else "error",
                    last_error_type=error.code, last_error_at=now_iso(),
                )
                results[sid] = {"source_id": sid, "status": "failed", **error.body}
            # 休眠唤醒只补一次：无论落后多久，下一轮都从现在重新计时。
            catalog.update_sync_state(
                conn, sid,
                next_run_at=iso(now() + timedelta(seconds=POLL_SECONDS.get(kind, 180))),
            )
        from .db import catalog_version

        return {"checked": checked, "results": results, "catalog_version": catalog_version(conn)}
    finally:
        conn.close()


def watch_forever(
    db_path: str,
    *,
    once: bool = False,
    max_ticks: int | None = None,
    sleep_seconds: float = 1.0,
    sleeper: Any = None,
    http: Any = None,
    session: Any = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """前台持续运行；``once=True`` 用于固定时钟模拟与验收。"""
    wait = sleeper or time.sleep
    ticks = 0
    aggregate = {"ticks": 0, "checked": [], "results": {}}
    while True:
        tick = run_tick(db_path, http=http, session=session, env=env)
        ticks += 1
        aggregate["ticks"] = ticks
        if once or max_ticks is not None:
            aggregate["checked"].extend(tick["checked"])
            aggregate["results"].update(tick["results"])
        if once or (max_ticks is not None and ticks >= max_ticks):
            aggregate["catalog_version"] = tick["catalog_version"]
            return aggregate
        wait(sleep_seconds)
