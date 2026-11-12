"""单次同步与快照提交（开发规格 §5.2 / §5.7 / R09）。

网络读取在写事务之外；提交时复核租约；完整范围确认后才对账；失败保留旧副本。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import timedelta
from typing import Any

from .. import catalog
from ..clock import iso, now, now_iso
from ..db import open_readwrite
from ..errors import RecallError, from_exception
from .registry import get_connector


def _jitter(source_id: str) -> int:
    return int(hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:2], 16) % 5


def _backoff(source_id: str, failure_count: int) -> int:
    return min(2 ** min(max(failure_count, 1), 5) * 5, 300) + _jitter(source_id)


def _parse_summary(state: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(state.get("last_summary") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _release(conn: Any, source_id: str, state: dict[str, Any]) -> None:
    catalog.update_sync_state(
        conn, source_id, lease_owner=None, lease_until=None, state=state.get("state", "idle")
    )


def sync_once(
    db_path: str | os.PathLike[str],
    source_id: str,
    *,
    full: bool = False,
    accept_empty: bool = False,
    http: Any = None,
    session: Any = None,
    env: dict[str, str] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """手动 ``recall sync`` 与 ``recall watch`` 共用同一租约和提交路径。"""
    conn = open_readwrite(db_path)
    owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    started = now_iso()
    lease_until = iso(now() + timedelta(seconds=300))
    state: dict[str, Any] = {}
    run_id: str | None = None
    try:
        with catalog.transaction(conn):
            source = catalog.get_source(conn, source_id)
            if source is None:
                raise RecallError("NOT_FOUND", "来源不存在。")
            state = catalog.get_sync_state(conn, source_id)
            if state.get("state") == "paused":
                return {"source_id": source_id, "status": "skipped", "reason": "paused"}
            lease_owner = state.get("lease_owner")
            lease_expires = state.get("lease_until")
            if lease_owner and lease_owner != owner and lease_expires and lease_expires > started:
                return {"source_id": source_id, "status": "skipped", "reason": "lease"}
            catalog.update_sync_state(
                conn, source_id, lease_owner=owner, lease_until=lease_until,
                last_checked_at=started,
            )
            mode = "full" if full else "head"
            run_id = catalog.start_run(conn, source_id, mode, state.get("coverage"))
        connector = get_connector(source, state, http=http, session=session, env=env)
        try:
            result = connector.fetch(mode=mode, state=state, budget=budget)
        except RecallError as exc:
            return _finish_failure(
                conn, source_id, run_id, exc, state, owner, started,
            )

        # 304：只更新时间检查字段，不写内容、不推进目录版本。
        if result.not_modified:
            with catalog.transaction(conn):
                current = catalog.get_sync_state(conn, source_id)
                if current.get("lease_owner") != owner or current.get("state") == "paused":
                    catalog.finish_run(conn, run_id, "failed", error_code="DB_BUSY")
                    return {"source_id": source_id, "status": "skipped", "reason": "lease_lost"}
                catalog.update_sync_state(
                    conn, source_id,
                    last_incremental_at=now_iso(), last_etag=result.etag or state.get("last_etag"),
                    latest_attempt="succeeded", state="idle", failure_count=0,
                    next_retry_at=None, lease_owner=None, lease_until=None,
                )
                catalog.finish_run(conn, run_id, "succeeded", counts={"not_modified": 1})
            return {
                "source_id": source_id, "status": "not_modified", "mode": mode,
                "coverage": state.get("coverage") or source["coverage"],
            }

        existing = conn.execute(
            "SELECT COUNT(*) FROM entries e JOIN collections c "
            "ON c.collection_id = e.collection_id WHERE c.source_id = ? AND e.active = 1",
            (source_id,),
        ).fetchone()[0]
        if result.complete and full:
            if existing and not result.entries and not accept_empty:
                raise RecallError(
                    "SNAPSHOT_INCOMPLETE", "完整快照为空，旧收藏关系保持不变。"
                )
            if existing and len(result.entries) * 2 < existing and not accept_empty:
                raise RecallError(
                    "SNAPSHOT_INCOMPLETE", "结果数量突降，只记录状态，不做对账。"
                )

        # 提交前复核租约，避免与并发任务重复写入。
        counts: dict[str, Any] = {}
        with catalog.transaction(conn):
            current = catalog.get_sync_state(conn, source_id)
            if current.get("lease_owner") != owner or current.get("state") == "paused":
                catalog.finish_run(conn, run_id, "failed", error_code="DB_BUSY")
                return {"source_id": source_id, "status": "skipped", "reason": "lease_lost"}
            summary = _parse_summary(current)
            if result.complete and full and connector.trusted:
                write_result = catalog.replace_snapshot(
                    conn, source_id, result.entries, allow_empty=accept_empty,
                )
            elif result.complete and full:
                write_result = _two_strike_reconcile(
                    conn, source_id, result.entries, started, summary,
                )
            else:
                write_result = catalog.merge_entries(conn, source_id, result.entries)
            counts = write_result.as_counts()
            if result.account_id:
                _bind_account(conn, source_id, result.account_id)
            continuation_key = "continuation_" + mode
            if result.continuation:
                summary[continuation_key] = result.continuation
            else:
                summary.pop(continuation_key, None)
            if full:
                summary["last_full_attempt_at"] = now_iso()
            success_fields: dict[str, Any] = {
                "last_success_at": now_iso(),
                "last_summary": json.dumps(
                    {
                        "account_id": result.account_id,
                        "requests_used": result.requests_used,
                        "text": result.summary,
                        **{k: v for k, v in summary.items() if k != "text"},
                    }, ensure_ascii=False,
                ),
                "coverage": result.coverage,
                "latest_attempt": "partial" if result.warnings else "succeeded",
                "state": "idle", "failure_count": 0, "next_retry_at": None,
                "lease_owner": None, "lease_until": None,
            }
            if not full:
                success_fields["last_incremental_at"] = now_iso()
            if full and result.complete:
                success_fields["last_full_sync_at"] = now_iso()
            if result.etag:
                success_fields["last_etag"] = result.etag
            catalog.update_sync_state(conn, source_id, **success_fields)
        catalog.finish_run(
            conn, run_id, "partial" if result.warnings else "succeeded",
            counts=counts, coverage=result.coverage,
        )
        return {
            "source_id": source_id,
            "status": "partial" if result.warnings else "succeeded",
            "mode": mode, "complete": result.complete,
            "coverage": result.coverage,
            "counts": counts,
            "warnings": result.warnings,
            "requests_used": result.requests_used,
            "catalog_version": write_result.catalog_version,
        }
    except RecallError as exc:
        if run_id is None:
            return _failure_body(exc, source_id)
        return _finish_failure(conn, source_id, run_id, exc, state, owner, started)
    except Exception as exc:
        error = from_exception(exc)
        if run_id is None:
            return _failure_body(error, source_id)
        return _finish_failure(conn, source_id, run_id, error, state, owner, started)
    finally:
        conn.close()


def _bind_account(conn: Any, source_id: str, account_id: str) -> None:
    row = conn.execute(
        "SELECT config_json FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    config = {}
    if row:
        try:
            config = json.loads(row[0] or "{}")
        except ValueError:
            config = {}
    if config.get("account_id") != account_id:
        config["account_id"] = account_id
        conn.execute(
            "UPDATE sources SET config_json = ?, remote_account_id = ? WHERE source_id = ?",
            (json.dumps(config), account_id, source_id),
        )


def _two_strike_reconcile(
    conn: Any,
    source_id: str,
    entries: list[Any],
    started: str,
    summary: dict[str, Any],
) -> catalog.WriteResult:
    """第三方来源：两次成功全量都缺失才把关系标为不可见。"""
    result = catalog.merge_entries(conn, source_id, entries, mark_missing=False)
    absent = [
        row[0] for row in conn.execute(
            "SELECT e.entry_id FROM entries e JOIN collections c "
            "ON c.collection_id = e.collection_id "
            "WHERE c.source_id = ? AND e.active = 1 AND e.last_seen_at < ?",
            (source_id, started),
        ).fetchall()
    ]
    previous = set(summary.get("absent_once") or [])
    confirmed = [entry_id for entry_id in absent if entry_id in previous]
    if confirmed:
        conn.executemany(
            "UPDATE entries SET active = 0 WHERE entry_id = ?",
            [(entry_id,) for entry_id in confirmed],
        )
        result.entries_deactivated = len(confirmed)
        result.changed = True
        catalog.bump_catalog_version(conn)
    summary["absent_once"] = absent
    from ..db import catalog_version

    result.catalog_version = catalog_version(conn)
    return result


def _finish_failure(
    conn: Any,
    source_id: str,
    run_id: str,
    exc: RecallError,
    state: dict[str, Any],
    owner: str,
    started: str,
) -> dict[str, Any]:
    failures = int(state.get("failure_count") or 0) + 1
    next_retry = iso(now() + timedelta(seconds=_backoff(source_id, failures)))
    new_state = "needs_login" if exc.code in ("AUTH_REQUIRED", "PERMISSION_DENIED") else "error"
    with catalog.transaction(conn):
        current = catalog.get_sync_state(conn, source_id)
        if current.get("lease_owner") == owner and current.get("state") != "paused":
            catalog.update_sync_state(
                conn, source_id,
                last_error_type=exc.code, last_error_at=now_iso(),
                latest_attempt="failed", state=new_state, failure_count=failures,
                next_retry_at=next_retry, lease_owner=None, lease_until=None,
            )
        catalog.finish_run(conn, run_id, "failed", error_code=exc.code)
    return {
        "source_id": source_id, "status": "failed", "error": exc.body["error"],
        "next_retry_at": next_retry,
    }


def _failure_body(exc: RecallError, source_id: str) -> dict[str, Any]:
    return {"source_id": source_id, "status": "failed", "error": exc.body["error"]}
