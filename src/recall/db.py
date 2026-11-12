"""SQLite v1（开发规格 §3 / §4）。

- ``PRAGMA user_version`` 追踪结构版本；旧程序遇到新版库明确报版本不匹配。
- ``recall init`` 幂等；只读查询用 ``mode=ro``，绝不顺手创建空库。
- 备份使用 SQLite 在线备份接口，目标已存在则拒绝覆盖。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .clock import now_iso
from .config import resolve_db_path
from .errors import RecallError

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS sources (
    source_id          TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    provider           TEXT NOT NULL,
    label              TEXT NOT NULL,
    remote_account_id  TEXT,
    sync_enabled       INTEGER NOT NULL DEFAULT 0,
    coverage           TEXT NOT NULL DEFAULT 'none',
    refresh_mode       TEXT NOT NULL DEFAULT 'manual',
    created_at         TEXT NOT NULL,
    config_json        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS collections (
    collection_id  TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    external_key   TEXT NOT NULL,
    path_json      TEXT NOT NULL,
    access         TEXT NOT NULL DEFAULT 'standard',
    UNIQUE (source_id, external_key)
);

CREATE TABLE IF NOT EXISTS items (
    item_id        TEXT PRIMARY KEY,
    canonical_url  TEXT NOT NULL UNIQUE,
    platform       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    entry_id       TEXT PRIMARY KEY,
    item_id        TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    collection_id  TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    external_key   TEXT NOT NULL,
    title          TEXT,
    excerpt        TEXT,
    note           TEXT,
    link_url       TEXT NOT NULL,
    saved_at       TEXT,
    imported_at    TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    capture        TEXT NOT NULL,
    access         TEXT NOT NULL DEFAULT 'standard',
    active         INTEGER NOT NULL DEFAULT 1,
    title_norm     TEXT NOT NULL DEFAULT '',
    search_norm    TEXT NOT NULL DEFAULT '',
    UNIQUE (collection_id, external_key)
);
CREATE INDEX IF NOT EXISTS idx_entries_item    ON entries(item_id);
CREATE INDEX IF NOT EXISTS idx_entries_active  ON entries(active);
CREATE INDEX IF NOT EXISTS idx_entries_saved   ON entries(saved_at);
CREATE INDEX IF NOT EXISTS idx_entries_coll    ON entries(collection_id);

CREATE TABLE IF NOT EXISTS import_runs (
    run_id       TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    mode         TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    coverage     TEXT,
    counts_json  TEXT NOT NULL DEFAULT '{}',
    error_code   TEXT,
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON import_runs(source_id, started_at);

CREATE TABLE IF NOT EXISTS sync_state (
    source_id           TEXT PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,
    last_checked_at     TEXT,
    last_success_at     TEXT,
    last_incremental_at TEXT,
    last_full_sync_at   TEXT,
    next_run_at         TEXT,
    next_retry_at       TEXT,
    last_error_type     TEXT,
    last_error_at       TEXT,
    last_etag           TEXT,
    last_summary        TEXT,
    lease_owner         TEXT,
    lease_until         TEXT,
    coverage            TEXT NOT NULL DEFAULT 'none',
    latest_attempt      TEXT NOT NULL DEFAULT 'never',
    state               TEXT NOT NULL DEFAULT 'idle',
    failure_count       INTEGER NOT NULL DEFAULT 0,
    catalog_version     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_tasks (
    task_id         TEXT PRIMARY KEY,
    entry_id        TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    digest          TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    params_version  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    error_code      TEXT,
    source_path     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (entry_id, kind, digest, engine_version, params_version)
);

CREATE TABLE IF NOT EXISTS media_segments (
    segment_id   TEXT PRIMARY KEY,
    entry_id     TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES media_tasks(task_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    access       TEXT NOT NULL DEFAULT 'standard',
    search_norm  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_entry ON media_segments(entry_id);
"""


def _connect_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _assert_version(conn: sqlite3.Connection) -> None:
    version = schema_version(conn)
    if version > SCHEMA_VERSION:
        raise RecallError(
            "DB_NOT_READY",
            f"数据库结构版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，请升级 Recall 后再打开。",
        )
    if version < SCHEMA_VERSION and version != 0:
        raise RecallError(
            "DB_NOT_READY",
            f"数据库结构版本 {version} 过旧，请先执行 recall init 完成备份与升级。",
        )
    if version == 0:
        # 空文件或尚未初始化的文件：写库入口在 init_db 里处理，读库入口直接拒绝。
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone()
        if row is None:
            raise RecallError("DB_NOT_READY", "本地库尚未初始化，请先执行 recall init。")


def init_db(path: str | Path | None = None, *, backup_on_upgrade: bool = True) -> Path:
    """创建或幂等打开当前版本库；升级前自动备份。"""
    target = resolve_db_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    conn = _connect_rw(target)
    try:
        version = schema_version(conn)
        if version > SCHEMA_VERSION:
            raise RecallError(
                "DB_NOT_READY",
                f"数据库结构版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，请升级 Recall 后再打开。",
            )
        if 0 < version < SCHEMA_VERSION and backup_on_upgrade:
            auto = target.with_name(f"{target.stem}.pre-v{SCHEMA_VERSION}-{now_iso().replace(':', '')}.db")
            backup_db(target, auto)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(DDL)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('catalog_version', '0')")
        if version != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()
    return target


def open_readonly(path: str | Path | None = None) -> sqlite3.Connection:
    target = resolve_db_path(path)
    if not target.is_file():
        raise RecallError("DB_NOT_READY", "本地库不存在，请先执行 recall init。")
    conn = _connect_ro(target)
    try:
        _assert_version(conn)
    except Exception:
        conn.close()
        raise
    return conn


def open_readwrite(path: str | Path | None = None) -> sqlite3.Connection:
    """写库入口：库必须已由 init_db 初始化，不在此静默创建。"""
    target = resolve_db_path(path)
    if not target.is_file():
        raise RecallError("DB_NOT_READY", "本地库不存在，请先执行 recall init。")
    conn = _connect_rw(target)
    try:
        _assert_version(conn)
    except Exception:
        conn.close()
        raise
    return conn


def catalog_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='catalog_version'").fetchone()
    return int(row[0]) if row else 0


def bump_catalog_version(conn: sqlite3.Connection) -> int:
    """只在查询内容发生变化时调用；纯刷新不增加目录版本。"""
    current = catalog_version(conn)
    new = current + 1
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('catalog_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(new),),
    )
    return new


def backup_db(src: str | Path | None, dst: str | Path) -> Path:
    destination = Path(dst).expanduser()
    if destination.exists():
        raise RecallError("INVALID_ARGUMENT", "备份目标已存在，拒绝覆盖。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = resolve_db_path(src)
    if not source.is_file():
        raise RecallError("DB_NOT_READY", "源库不存在。")
    source_conn = _connect_ro(source)
    dest_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()
    return destination


def verify_db(path: str | Path) -> dict[str, Any]:
    """恢复前的版本与完整性检查（开发规格 §8）。"""
    target = Path(path).expanduser()
    if not target.is_file():
        raise RecallError("NOT_FOUND", "备份文件不存在。")
    conn = _connect_ro(target)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = schema_version(conn)
    finally:
        conn.close()
    if integrity != "ok":
        raise RecallError("INVALID_ARGUMENT", "备份完整性检查未通过。")
    if version > SCHEMA_VERSION:
        raise RecallError("DB_NOT_READY", f"备份结构版本 {version} 高于本程序支持的版本。")
    if version < SCHEMA_VERSION:
        raise RecallError("DB_NOT_READY", f"备份结构版本 {version} 过旧，请先升级后再恢复。")
    return {"integrity": integrity, "user_version": version, "path": str(target)}


def restore_db(backup: str | Path, target: str | Path) -> Path:
    """校验后把备份恢复到指定路径；目标已存在时拒绝覆盖。"""
    info = verify_db(backup)
    destination = Path(target).expanduser()
    if destination.exists():
        raise RecallError("INVALID_ARGUMENT", "恢复目标已存在，拒绝覆盖；请先移走现有库。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src_conn = _connect_ro(Path(info["path"]))
    dst_conn = sqlite3.connect(str(destination))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return destination
