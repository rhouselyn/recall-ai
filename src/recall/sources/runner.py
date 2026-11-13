"""单次同步与快照提交（开发规格 §5.2 / §5.7 / R09）。

网络读取在写事务之外；提交时复核租约；完整范围确认后才对账；失败保留旧副本。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import ExitStack
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
    resources = ExitStack()
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
        from ..leases import heartbeat
        resources.enter_context(heartbeat(db_path, lambda connection: connection.execute(
            "UPDATE sync_state SET lease_until=? WHERE source_id=? AND lease_owner=?",
            (iso(now() + timedelta(seconds=300)), source_id, owner),
        ).rowcount))
        from .browser import PLATFORM_START, load_session
        config = json.loads(source.get("config_json") or "{}")
        if session is None and source["kind"] in PLATFORM_START and not (
            source["kind"] == "bilibili_favorites" and config.get("allow_direct_http") and http is not None
        ):
            session = load_session(source_id, source["kind"], db_path)
            resources.callback(session.close)
        if source["kind"] == "zhihu_api":
            from urllib.parse import urlsplit
            from .http import UrllibHttpClient
            from .quota import DailyQuotaClient
            client = http or UrllibHttpClient(
                allowed_hosts=(urlsplit(config.get("api_base", "https://api.zhihu.com")).hostname,),
                max_retries=0,
            )
            http = DailyQuotaClient(conn, client, config.get("daily_request_limit", 20))
            budget = dict(budget or {})
            budget["max_requests"] = min(budget.get("max_requests", 20), http.remaining())
        scope = hashlib.sha256(json.dumps({k: v for k, v in config.items() if k != "account_id"},
                                         sort_keys=True).encode()).hexdigest()
        prior_scope = _parse_summary(state)
        if prior_scope.get("scope") not in (None, scope):
            for key in ("continuation_full", "continuation_head", "snapshot_seen", "snapshot_valid", "absent_once"):
                prior_scope.pop(key, None)
            state = {**state, "last_summary": json.dumps(prior_scope)}
        connector = get_connector(source, state, http=http, session=session, env=env)
        try:
            result = connector.fetch(mode=mode, state=state, budget=budget)
        except RecallError as exc:
            return _finish_failure(
                conn, source_id, run_id, exc, state, owner,
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
                    next_retry_at=None,
                )
                catalog.finish_run(conn, run_id, "succeeded", counts={"not_modified": 1})
            return {
                "source_id": source_id, "status": "not_modified", "mode": mode,
                "coverage": state.get("coverage") or source["coverage"],
            }

        prior = _parse_summary(state)
        resumed = full and bool(prior.get("continuation_full"))
        accumulated = set(prior.get("snapshot_seen") or []) if resumed else set()
        verified_collections = set(prior.get("snapshot_collections") or []) if resumed else set()
        verified_collections.update(result.collection_keys)
        valid = (not resumed or prior.get("snapshot_valid") is True) and not any(
            warning not in {"BUDGET_EXHAUSTED", "BATCHED_SNAPSHOT"} for warning in result.warnings
        )
        if full and result.complete and not valid:
            result.complete = False
            result.coverage = "authorized_subset"
            result.warnings.append("SNAPSHOT_NOT_VERIFIABLE")
        existing_rows = conn.execute(
            "SELECT e.entry_id, e.external_key, e.access, c.access AS collection_access, "
            "c.external_key AS collection_key FROM entries e JOIN collections c USING(collection_id) "
            "WHERE c.source_id=? AND e.active=1", (source_id,),
        ).fetchall()
        existing = len(existing_rows)
        total = len(accumulated) + len(result.entries)
        if result.complete and full and existing and not accept_empty:
            if not total or total * 2 < existing:
                raise RecallError("SNAPSHOT_INCOMPLETE", "完整快照为空或数量突降，旧收藏关系保持不变。")
        removable = None
        unverified = set()
        if full and result.complete and hasattr(connector, "can_remove") and not accept_empty:
            current_keys = {entry.external_key for entry in result.entries}
            removable = set()
            for row in existing_rows:
                if row["entry_id"] not in accumulated and row["external_key"] not in current_keys:
                    try:
                        if connector.can_remove(dict(row), result):
                            removable.add(row["entry_id"])
                        else:
                            unverified.add(row["entry_id"])
                            result.warnings.append("MISSING_ENTRY_ACCESS_UNVERIFIED")
                    except RecallError:
                        unverified.add(row["entry_id"])
                        result.warnings.append("MISSING_ENTRY_ACCESS_UNVERIFIED")
            result.warnings = list(dict.fromkeys(result.warnings))

        # 提交前复核租约，避免与并发任务重复写入。
        counts: dict[str, Any] = {}
        with catalog.transaction(conn):
            current = catalog.get_sync_state(conn, source_id)
            if current.get("lease_owner") != owner or current.get("state") == "paused":
                catalog.finish_run(conn, run_id, "failed", error_code="DB_BUSY")
                return {"source_id": source_id, "status": "skipped", "reason": "lease_lost"}
            summary = _parse_summary(current)
            if summary.get("scope") not in (None, scope):
                summary = {k: v for k, v in summary.items() if not k.startswith(("snapshot_", "continuation_")) and k != "absent_once"}
            summary["scope"] = scope
            write_result = catalog.merge_entries(conn, source_id, result.entries)
            restricted = 0
            for entry_id in unverified:
                restricted += conn.execute("UPDATE entries SET access='restricted' WHERE entry_id=? AND access='standard'",
                                           (entry_id,)).rowcount
            if restricted:
                write_result.entries_updated += restricted
                write_result.changed = True
                write_result.catalog_version = catalog.bump_catalog_version(conn)
            seen = accumulated | write_result.seen_ids
            if full and result.complete:
                if existing and len(seen) * 2 < existing and not accept_empty:
                    raise RecallError("SNAPSHOT_INCOMPLETE", "结果数量突降，旧收藏关系保持不变。")
                _reconcile_missing(conn, source_id, seen, summary, write_result,
                                   trusted=connector.trusted, removable=removable,
                                   verified_collections=verified_collections, accept_empty=accept_empty)
            if full and result.continuation:
                summary.update(snapshot_seen=sorted(seen), snapshot_valid=valid,
                               snapshot_collections=sorted(verified_collections))
            elif full:
                for key in ("snapshot_seen", "snapshot_valid", "snapshot_collections"):
                    summary.pop(key, None)
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
                        **summary,
                        "account_id": result.account_id,
                        "requests_used": result.requests_used,
                        "text": result.summary,
                    }, ensure_ascii=False,
                ),
                "coverage": result.coverage,
                "latest_attempt": "partial" if result.warnings else "succeeded",
                "state": "idle", "failure_count": 0, "next_retry_at": None,
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
        return _finish_failure(conn, source_id, run_id, exc, state, owner)
    except Exception as exc:
        error = from_exception(exc)
        if run_id is None:
            return _failure_body(error, source_id)
        return _finish_failure(conn, source_id, run_id, error, state, owner)
    finally:
        try:
            resources.close()
        finally:
            conn.execute("UPDATE sync_state SET lease_owner=NULL, lease_until=NULL "
                         "WHERE source_id=? AND lease_owner IN (?,?)", (source_id, owner, "cancelled:" + owner))
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


def _reconcile_missing(conn, source_id, seen, summary, result, *, trusted,
                       removable, verified_collections, accept_empty):
    rows = conn.execute(
        "SELECT e.entry_id, e.access, c.access AS collection_access, c.external_key AS collection_key "
        "FROM entries e JOIN collections c USING(collection_id) WHERE c.source_id=? AND e.active=1",
        (source_id,),
    ).fetchall()
    absent = {row["entry_id"] for row in rows if row["entry_id"] not in seen and (
        accept_empty or (removable is not None and row["entry_id"] in removable) or
        (removable is None and (row["collection_key"] in verified_collections or
         (row["access"] == "standard" and row["collection_access"] == "standard")))
    )}
    confirmed = absent if trusted else absent & set(summary.get("absent_once") or [])
    if confirmed:
        conn.executemany("UPDATE entries SET active=0 WHERE entry_id=?", [(key,) for key in confirmed])
        result.entries_deactivated = len(confirmed)
        result.changed = True
        result.catalog_version = catalog.bump_catalog_version(conn)
    summary["absent_once"] = sorted(absent)


def _finish_failure(
    conn: Any,
    source_id: str,
    run_id: str,
    exc: RecallError,
    state: dict[str, Any],
    owner: str,
) -> dict[str, Any]:
    failures = int(state.get("failure_count") or 0) + 1
    delay = max(_backoff(source_id, failures), exc.details.get("retry_after", 0))
    next_retry = iso(now() + timedelta(seconds=delay))
    new_state = "needs_login" if exc.code in ("AUTH_REQUIRED", "PERMISSION_DENIED") else "error"
    with catalog.transaction(conn):
        current = catalog.get_sync_state(conn, source_id)
        if current.get("lease_owner") == owner and current.get("state") != "paused":
            catalog.update_sync_state(
                conn, source_id,
                last_error_type=exc.code, last_error_at=now_iso(),
                latest_attempt="failed", state=new_state, failure_count=failures,
                next_retry_at=next_retry,
            )
        catalog.finish_run(conn, run_id, "failed", error_code=exc.code)
    return {
        "source_id": source_id, "status": "failed", "error": exc.body["error"],
        "next_retry_at": next_retry,
    }


def _failure_body(exc: RecallError, source_id: str) -> dict[str, Any]:
    return {"source_id": source_id, "status": "failed", "error": exc.body["error"]}
