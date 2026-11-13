"""目录读写：来源、收藏夹、内容项、收藏证据（开发规格 §4 / §5）。

所有写入都在 SQLite 事务里完成；重复导入不重复计数；快照替换是原子的。
查询侧只看 ``active = 1`` 的关系，且每次调用重新做可见性检查。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from .clock import now_iso
from .db import bump_catalog_version, catalog_version
from .errors import RecallError
from .textnorm import normalize, searchable
from .urls import SafeUrl, stable_id, truncate_display

SOURCE_KINDS = frozenset(
    {
        "bookmarks_html",
        "bookmarks_live",
        "manual",
        "github_stars",
        "zhihu_api",
        "zhihu_web",
        "bilibili_favorites",
        "xiaohongshu_favorites",
        "douyin_favorites",
        "youtube_favorites",
        "rss",
    }
)
COVERAGE_VALUES = frozenset(
    {
        "live_profile",
        "complete_export",
        "imported_subset",
        "authorized_view",
        "authorized_subset",
        "none",
    }
)
CAPTURES = frozenset({"account_sync", "browser_sync", "file_import", "manual_capture"})
ACCESSES = frozenset({"standard", "restricted"})
RUN_MODES = frozenset({"head", "full", "event", "file"})
REFRESH_MODES = frozenset({"event", "poll", "manual"})


@dataclass(frozen=True)
class EntryInput:
    """一条待写入的收藏证据。``safe`` 由 urls.sanitize_url 产生。"""

    safe: SafeUrl
    title: str | None = None
    excerpt: str | None = None
    note: str | None = None
    saved_at: str | None = None  # 已归一为 UTC RFC 3339；未知为 None
    path: tuple[str, ...] = ()
    external_key: str | None = None  # 平台稳定条目 ID；None 时用 canonical_url
    capture: str = "file_import"
    access: str = "standard"
    collection_access: str = "standard"
    collection_key: str | None = None


@dataclass
class WriteResult:
    seen_ids: set[str] = field(default_factory=set, repr=False)
    items_added: int = 0
    collections_added: int = 0
    entries_added: int = 0
    entries_updated: int = 0
    entries_unchanged: int = 0
    entries_deactivated: int = 0
    items_removed: int = 0
    truncated_fields: list[str] = field(default_factory=list)
    catalog_version: int = 0
    changed: bool = False

    def as_counts(self) -> dict[str, Any]:
        return {
            "items_added": self.items_added,
            "collections_added": self.collections_added,
            "entries_added": self.entries_added,
            "entries_updated": self.entries_updated,
            "entries_unchanged": self.entries_unchanged,
            "entries_deactivated": self.entries_deactivated,
            "items_removed": self.items_removed,
            "truncated_fields": sorted(set(self.truncated_fields)),
        }


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """支持嵌套调用：外层事务由最外层提交。"""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def collection_external_key(path: Sequence[str]) -> str:
    """完整路径数组编码，避免用单一斜线连接引发碰撞。"""
    return _jdump(list(path))


# --------------------------------------------------------------------------
# 来源
# --------------------------------------------------------------------------


def ensure_source(
    conn: sqlite3.Connection,
    source_id: str,
    kind: str,
    provider: str,
    label: str | None = None,
    *,
    sync_enabled: bool = False,
    coverage: str = "none",
    refresh_mode: str = "manual",
    remote_account_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    if conn.execute("SELECT 1 FROM meta WHERE key=?", ("source_cleanup:" + source_id,)).fetchone():
        raise RecallError("DB_BUSY", "该来源仍有文件待清理，请再次执行 source remove。")
    if not source_id or not source_id.strip():
        raise RecallError("INVALID_ARGUMENT", "source_id 不能为空。")
    if kind not in SOURCE_KINDS:
        raise RecallError("INVALID_ARGUMENT", f"未知来源类型：{kind}")
    if coverage not in COVERAGE_VALUES:
        raise RecallError("INVALID_ARGUMENT", f"未知覆盖范围：{coverage}")
    if refresh_mode not in REFRESH_MODES:
        raise RecallError("INVALID_ARGUMENT", f"未知刷新方式：{refresh_mode}")
    row = conn.execute(
        "SELECT kind, provider FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is not None:
        if row["kind"] != kind or row["provider"] != provider:
            raise RecallError(
                "INVALID_ARGUMENT", "相同来源 ID 已存在，但来源类型或渠道不匹配。"
            )
        return source_id
    with transaction(conn):
        conn.execute(
            "INSERT INTO sources(source_id, kind, provider, label, remote_account_id, "
            "sync_enabled, coverage, refresh_mode, created_at, config_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                kind,
                provider,
                label or source_id,
                remote_account_id,
                int(sync_enabled),
                coverage,
                refresh_mode,
                now_iso(),
                _jdump(config or {}),
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO sync_state(source_id, coverage, state) VALUES(?,?,?)",
            (source_id, coverage, "idle"),
        )
    return source_id


def get_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def get_sync_state(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM sync_state WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None:
        source = get_source(conn, source_id)
        return {
            "source_id": source_id,
            "coverage": source["coverage"] if source else "none",
            "latest_attempt": "never",
            "state": "idle",
            "failure_count": 0,
        }
    return dict(row)


def update_sync_state(
    conn: sqlite3.Connection, source_id: str, **fields: Any
) -> None:
    allowed = {
        "last_checked_at", "last_success_at", "last_incremental_at", "last_full_sync_at",
        "next_run_at", "next_retry_at", "last_error_type", "last_error_at", "last_etag",
        "last_summary", "lease_owner", "lease_until", "coverage", "latest_attempt",
        "state", "failure_count",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise RecallError("INVALID_ARGUMENT", f"未知同步状态字段：{sorted(unknown)}")
    if not fields:
        return
    row = conn.execute(
        "SELECT 1 FROM sync_state WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO sync_state(source_id) VALUES(?)", (source_id,)
        )
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE sync_state SET {assignments} WHERE source_id = ?",
        (*fields.values(), source_id),
    )
    if "coverage" in fields:
        conn.execute(
            "UPDATE sources SET coverage = ? WHERE source_id = ?",
            (fields["coverage"], source_id),
        )


def source_summary(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    source = get_source(conn, source_id)
    state = get_sync_state(conn, source_id)
    coverage = state.get("coverage") or (source or {}).get("coverage") or "none"
    return {
        "source_id": source_id,
        "coverage": coverage,
        "refresh_mode": (source or {}).get("refresh_mode", "manual"),
        "last_checked_at": state.get("last_checked_at"),
        "last_success_at": state.get("last_success_at"),
        "last_incremental_at": state.get("last_incremental_at"),
        "last_full_sync_at": state.get("last_full_sync_at"),
        "last_error_type": state.get("last_error_type"),
        "next_retry_at": state.get("next_retry_at"),
        "sync_state": state.get("state", "idle"),
        "latest_attempt": state.get("latest_attempt", "never"),
    }


def list_sources(
    conn: sqlite3.Connection,
    allowed: frozenset[str] | None = None,
    *,
    include_restricted: bool = False,
) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
    result = []
    for row in rows:
        source = dict(row)
        sid = source["source_id"]
        if allowed is not None and sid not in allowed:
            continue
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM entries e JOIN collections c "
            "ON c.collection_id = e.collection_id "
            "WHERE c.source_id = ? AND e.active = 1"
            + ("" if include_restricted else " AND e.access = 'standard' AND c.access = 'standard'"),
            (sid,),
        ).fetchone()[0]
        source["visible_entry_count"] = entry_count
        summary = source_summary(conn, sid)
        source.update({k: v for k, v in summary.items() if k != "source_id"})
        result.append(source)
    return result


def delete_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    """删除来源及其独占托管文件；失败的文件清理保留可重试清单。"""
    from pathlib import Path
    import shutil
    from .sources.browser import profile_dir
    from .config import media_dir

    cleanup_key = "source_cleanup:" + source_id
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    root = Path(database).resolve().parent if database else None
    with transaction(conn):
        source = get_source(conn, source_id)
        pending = conn.execute("SELECT value FROM meta WHERE key=?", (cleanup_key,)).fetchone()
        if source is None and pending is None:
            raise RecallError("NOT_FOUND")
        state = get_sync_state(conn, source_id)
        if state.get("lease_owner") and (state.get("lease_until") or "") > now_iso():
            raise RecallError("DB_BUSY", "来源正在同步或登录，暂不能删除。")
        from datetime import timedelta
        from .clock import iso, now
        active = conn.execute(
            "SELECT 1 FROM media_tasks t JOIN entries e USING(entry_id) JOIN collections c USING(collection_id) "
            "WHERE c.source_id=? AND t.status='processing' AND t.updated_at >= ? LIMIT 1",
            (source_id, iso(now() - timedelta(seconds=300))),
        ).fetchone()
        if active:
            raise RecallError("DB_BUSY", "来源的媒体任务正在运行，暂不能删除。")
        paths = set(json.loads(pending[0])) if pending else set()
        if root is not None and source is not None:
            directory = profile_dir(source_id, database)
            if directory.exists():
                paths.add(str(directory))
            paths.update(row[0] for row in conn.execute(
                "SELECT t.source_path FROM media_tasks t JOIN entries e USING(entry_id) "
                "JOIN collections c USING(collection_id) WHERE c.source_id=? AND t.source_path IS NOT NULL",
                (source_id,),
            ))
        counts = conn.execute(
            "SELECT COUNT(*) FROM entries e JOIN collections c USING(collection_id) WHERE c.source_id=?",
            (source_id,),
        ).fetchone()[0]
        conn.execute("DELETE FROM sources WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM import_runs WHERE source_id=?", (source_id,))
        orphaned = _cleanup_orphan_items(conn)
        if source is not None:
            bump_catalog_version(conn)
        owned = []
        if root is not None:
            shared = {Path(row[0]).resolve() for row in conn.execute(
                "SELECT source_path FROM media_tasks WHERE source_path IS NOT NULL"
            )}
            for value in paths:
                path = Path(value).resolve()
                if (path.is_relative_to(media_dir(database)) or
                        (path.is_relative_to(root / "browser-profiles") and
                         path == profile_dir(source_id, database).absolute())):
                    if path not in shared:
                        owned.append(str(path))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (cleanup_key, _jdump(owned)))
    # 先提交待清理清单，再动文件；进程中断后可用同一命令继续。
    retained = paths - set(owned)
    remaining = []
    with transaction(conn):
        shared = {Path(row[0]).resolve() for row in conn.execute(
            "SELECT source_path FROM media_tasks WHERE source_path IS NOT NULL"
        )}
        for value in owned:
            path = Path(value)
            if path in shared:
                retained.add(value)
                continue
            if path.resolve() != path:
                remaining.append(value)
                continue
            try:
                if path.is_dir():
                    if root is None or not path.is_relative_to(root / "browser-profiles"):
                        remaining.append(value)
                        continue
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                remaining.append(value)
        if remaining:
            conn.execute("UPDATE meta SET value=? WHERE key=?", (_jdump(remaining), cleanup_key))
        else:
            conn.execute("DELETE FROM meta WHERE key=?", (cleanup_key,))
    return {"entries_removed": counts, "items_removed": orphaned, "cleanup_pending": remaining,
            "retained_files": sorted(retained)}


def _cleanup_orphan_items(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM items WHERE NOT EXISTS "
        "(SELECT 1 FROM entries e WHERE e.item_id = items.item_id)"
    )
    return max(cur.rowcount, 0)


# --------------------------------------------------------------------------
# 收藏夹 / 内容项 / 证据
# --------------------------------------------------------------------------


def ensure_collection(
    conn: sqlite3.Connection,
    source_id: str,
    path: Sequence[str],
    access: str = "standard",
    *,
    external_key: str | None = None,
) -> tuple[str, bool]:
    if access not in ACCESSES:
        raise RecallError("INVALID_ARGUMENT", "access 只能是 standard 或 restricted。")
    key = external_key if external_key is not None else collection_external_key(path)
    collection_id = stable_id("col", source_id, key)
    row = conn.execute(
        "SELECT * FROM collections WHERE source_id = ? AND external_key = ?", (source_id, key)
    ).fetchone()
    if row is not None:
        collection_id = row["collection_id"]
        if row["path_json"] != _jdump(list(path)):
            conn.execute("UPDATE collections SET path_json=? WHERE collection_id=?",
                         (_jdump(list(path)), collection_id))
            for entry in conn.execute(
                "SELECT e.*, i.canonical_url FROM entries e JOIN items i USING(item_id) WHERE collection_id=?",
                (collection_id,),
            ).fetchall():
                conn.execute("UPDATE entries SET search_norm=? WHERE entry_id=?", (
                    searchable(entry["title"], " ".join(path), entry["excerpt"], entry["note"],
                               entry["link_url"], entry["canonical_url"]), entry["entry_id"],
                ))
            bump_catalog_version(conn)
        # 已受限的夹不因后续写入降级。
        if access == "restricted" and row["access"] != "restricted":
            conn.execute(
                "UPDATE collections SET access = 'restricted' WHERE collection_id = ?",
                (collection_id,),
            )
            bump_catalog_version(conn)
        return collection_id, False
    conn.execute(
        "INSERT INTO collections(collection_id, source_id, external_key, path_json, access) "
        "VALUES(?,?,?,?,?)",
        (collection_id, source_id, key, _jdump(list(path)), access),
    )
    bump_catalog_version(conn)
    return collection_id, True


def upsert_item(conn: sqlite3.Connection, safe: SafeUrl) -> tuple[str, bool]:
    item_id = stable_id("itm", safe.canonical_url)
    row = conn.execute(
        "SELECT 1 FROM items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is not None:
        return item_id, False
    conn.execute(
        "INSERT OR IGNORE INTO items(item_id, canonical_url, platform, created_at) "
        "VALUES(?,?,?,?)",
        (item_id, safe.canonical_url, safe.platform, now_iso()),
    )
    return item_id, True


def _effective_access(entry: EntryInput) -> str:
    if entry.access == "restricted" or entry.collection_access == "restricted":
        return "restricted"
    return "standard"


def _prepare_text(value: str | None, limit: int, result: WriteResult, name: str) -> str | None:
    text, truncated = truncate_display(value, limit)
    if truncated:
        result.truncated_fields.append(name)
    return text


def upsert_entry(
    conn: sqlite3.Connection,
    source_id: str,
    entry: EntryInput,
    result: WriteResult,
) -> tuple[str, str]:
    """写入或合并一条证据，返回 (entry_id, change)。

    显式空字段不覆盖同一来源已有的有效元数据；restricted 不会意外降级。
    """
    if entry.capture not in CAPTURES:
        raise RecallError("INVALID_ARGUMENT", f"未知 capture：{entry.capture}")
    if entry.access not in ACCESSES:
        raise RecallError("INVALID_ARGUMENT", "access 只能是 standard 或 restricted。")
    if len(entry.safe.link_url) > 2048:
        raise RecallError("INVALID_ARGUMENT", "链接超出长度上限。")
    collection_id, created_col = ensure_collection(
        conn, source_id, entry.path, entry.collection_access, external_key=entry.collection_key
    )
    result.collections_added += int(created_col)
    item_id, created_item = upsert_item(conn, entry.safe)
    result.items_added += int(created_item)

    external_key = entry.external_key or entry.safe.canonical_url
    entry_id = stable_id("ent", collection_id, external_key)
    existing = conn.execute(
        "SELECT * FROM entries WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    access = _effective_access(entry)
    title = _prepare_text(entry.title or (existing["title"] if existing else None), 300, result, "title")
    excerpt = _prepare_text(entry.excerpt or (existing["excerpt"] if existing else None), 1000, result, "excerpt")
    note = _prepare_text(entry.note or (existing["note"] if existing else None), 1000, result, "note")
    stamp = now_iso()
    title_norm = normalize(title)
    search_norm = searchable(
        title, " ".join(entry.path), excerpt, note,
        entry.safe.link_url, entry.safe.canonical_url,
    )

    if existing is None:
        conn.execute(
            "INSERT INTO entries(entry_id, item_id, collection_id, external_key, title, "
            "excerpt, note, link_url, saved_at, imported_at, last_seen_at, capture, "
            "access, active, title_norm, search_norm) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (
                entry_id, item_id, collection_id, external_key, title, excerpt, note,
                entry.safe.link_url, entry.saved_at, stamp, stamp, entry.capture, access,
                title_norm, search_norm,
            ),
        )
        result.entries_added += 1
        result.changed = True
        return entry_id, "added"

    updates: dict[str, Any] = {}
    for column, value in (
        ("title", title), ("excerpt", excerpt), ("note", note),
        ("saved_at", entry.saved_at), ("link_url", entry.safe.link_url),
        ("item_id", item_id), ("capture", entry.capture),
        ("title_norm", title_norm), ("search_norm", search_norm),
    ):
        if value is not None and value != existing[column]:
            updates[column] = value
    # 受限是单向的：只有升级为 restricted，绝不降级。
    if access == "restricted" and existing["access"] != "restricted":
        updates["access"] = "restricted"
    if existing["active"] != 1:
        updates["active"] = 1
    # last_seen_at 只是刷新心跳，不影响查询结果，也不能推进目录版本。
    if existing["last_seen_at"] != stamp:
        conn.execute(
            "UPDATE entries SET last_seen_at = ? WHERE entry_id = ?", (stamp, entry_id)
        )
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE entries SET {assignments} WHERE entry_id = ?",
            (*updates.values(), entry_id),
        )
        result.entries_updated += 1
        result.changed = True
        return entry_id, "updated"
    result.entries_unchanged += 1
    return entry_id, "unchanged"


def merge_entries(
    conn: sqlite3.Connection,
    source_id: str,
    entries: Iterable[EntryInput],
    *,
    mark_missing: bool = False,
) -> WriteResult:
    """增量合并：只新增或更新，旧条目保持可查。"""
    entries = list(entries)
    result = WriteResult()
    seen: set[str] = set()
    with transaction(conn):
        migrate_legacy_collections(conn, source_id, entries)
        for entry in entries:
            entry_id, _ = upsert_entry(conn, source_id, entry, result)
            seen.add(entry_id)
        result.seen_ids = seen
        if mark_missing:
            rows = conn.execute(
                "SELECT e.entry_id FROM entries e JOIN collections c "
                "ON c.collection_id = e.collection_id "
                "WHERE c.source_id = ? AND e.active = 1",
                (source_id,),
            ).fetchall()
            for row in rows:
                if row["entry_id"] not in seen:
                    conn.execute(
                        "UPDATE entries SET active = 0 WHERE entry_id = ?", (row["entry_id"],)
                    )
                    result.entries_deactivated += 1
                    result.changed = True
        result.items_removed = _cleanup_orphan_items(conn)
        if result.changed or result.items_removed:
            result.catalog_version = bump_catalog_version(conn)
        else:
            result.catalog_version = catalog_version(conn)
    return result


def migrate_legacy_collections(conn, source_id, entries):
    """只有一批证据能覆盖旧夹且目标唯一时，沿用旧 ID 保留媒体与引用。"""
    groups: dict[str, dict[str, set[str]]] = {}
    for entry in entries:
        if entry.collection_key is not None:
            groups.setdefault(collection_external_key(entry.path), {}).setdefault(
                entry.collection_key, set()).add(entry.external_key or entry.safe.canonical_url)
    for path_key, targets in groups.items():
        if len(targets) != 1:
            continue
        key, seen = next(iter(targets.items()))
        old = conn.execute("SELECT collection_id FROM collections WHERE source_id=? AND external_key=?",
                           (source_id, path_key)).fetchone()
        if old is None or conn.execute("SELECT 1 FROM collections WHERE source_id=? AND external_key=?",
                                       (source_id, key)).fetchone():
            continue
        existing = {row[0] for row in conn.execute("SELECT external_key FROM entries WHERE collection_id=?",
                                                  (old[0],))}
        if existing <= seen:
            conn.execute("UPDATE collections SET external_key=? WHERE collection_id=?", (key, old[0]))


def replace_snapshot(
    conn: sqlite3.Connection,
    source_id: str,
    entries: Sequence[EntryInput],
    *,
    allow_empty: bool = False,
    complete: bool = True,
) -> WriteResult:
    """原子替换该来源的可见收藏关系；任何异常都保留旧数据。

    异常空快照（已有数据却返回零条）需要用户显式允许。
    """
    if not complete:
        raise RecallError("SNAPSHOT_INCOMPLETE", "快照不完整，拒绝替换旧数据。")
    current = conn.execute(
        "SELECT COUNT(*) FROM entries e JOIN collections c "
        "ON c.collection_id = e.collection_id WHERE c.source_id = ? AND e.active = 1",
        (source_id,),
    ).fetchone()[0]
    if current and not entries and not allow_empty:
        raise RecallError(
            "SNAPSHOT_INCOMPLETE",
            "本次快照为空但已有收藏数据，疑似异常空结果；请检查来源或使用 --accept-empty。",
        )
    result = WriteResult()
    try:
        with transaction(conn):
            migrate_legacy_collections(conn, source_id, entries)
            seen: set[str] = set()
            for entry in entries:
                entry_id, _ = upsert_entry(conn, source_id, entry, result)
                seen.add(entry_id)
            result.seen_ids = seen
            rows = conn.execute(
                "SELECT e.entry_id, e.active FROM entries e JOIN collections c "
                "ON c.collection_id = e.collection_id WHERE c.source_id = ?",
                (source_id,),
            ).fetchall()
            for row in rows:
                if row["entry_id"] not in seen and row["active"] == 1:
                    conn.execute(
                        "UPDATE entries SET active = 0 WHERE entry_id = ?", (row["entry_id"],)
                    )
                    result.entries_deactivated += 1
                    result.changed = True
            result.items_removed = _cleanup_orphan_items(conn)
            if result.changed or result.items_removed:
                result.catalog_version = bump_catalog_version(conn)
            else:
                result.catalog_version = catalog_version(conn)
    except RecallError:
        raise
    except Exception as exc:
        raise RecallError("SNAPSHOT_INCOMPLETE", "快照替换失败，旧数据已保留。") from exc
    return result


# --------------------------------------------------------------------------
# 导入记录
# --------------------------------------------------------------------------


def start_run(
    conn: sqlite3.Connection, source_id: str, mode: str, coverage: str | None = None
) -> str:
    if mode not in RUN_MODES:
        raise RecallError("INVALID_ARGUMENT", f"未知运行模式：{mode}")
    run_id = "run_" + uuid.uuid4().hex[:20]
    conn.execute(
        "INSERT INTO import_runs(run_id, source_id, mode, started_at, status, coverage, counts_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (run_id, source_id, mode, now_iso(), "running", coverage, "{}"),
    )
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    *,
    counts: dict[str, Any] | None = None,
    coverage: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    if status not in {"succeeded", "partial", "failed"}:
        raise RecallError("INVALID_ARGUMENT", f"未知运行状态：{status}")
    conn.execute(
        "UPDATE import_runs SET finished_at = ?, status = ?, counts_json = ?, "
        "coverage = COALESCE(?, coverage), error_code = ?, error_detail = ? "
        "WHERE run_id = ?",
        (
            now_iso(), status, _jdump(counts or {}), coverage, error_code,
            error_detail, run_id,
        ),
    )


def latest_runs(conn: sqlite3.Connection, source_id: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM import_runs WHERE source_id = ? ORDER BY started_at DESC LIMIT ?",
        (source_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
