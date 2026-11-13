"""SQLite 待办队列与片段写入（开发规格 §11.2）。"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from ..clock import now_iso, now, iso
from ..catalog import transaction
from ..db import bump_catalog_version
from ..textnorm import normalize
from ..urls import stable_id

OCR_ENGINE_VERSION = "rapidocr-1.4.4"
ASR_ENGINE_VERSION = "paraformer-sherpa-2.0.1"
CAPTION_ENGINE_VERSION = "platform-caption-1"
PARAMS_VERSION = "v1"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def enqueue(
    conn: Any,
    entry_id: str,
    kind: str,
    data: bytes | None,
    *,
    engine_version: str,
    source_path: str | None = None,
    digest: str | None = None,
) -> tuple[str, bool]:
    digest = digest or _digest(data or b"")
    task_id = stable_id("tsk", entry_id, kind, digest, engine_version, PARAMS_VERSION)
    existing = conn.execute(
        "SELECT status FROM media_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if existing is not None:
        if source_path:
            conn.execute("UPDATE media_tasks SET source_path=? WHERE task_id=? AND status != 'processing'",
                         (source_path, task_id))
            conn.execute("UPDATE media_tasks SET status='pending', attempts=0 WHERE task_id=? "
                         "AND status IN ('failed','unavailable')", (task_id,))
        return task_id, False
    stamp = now_iso()
    conn.execute(
        "INSERT INTO media_tasks(task_id, entry_id, kind, digest, engine_version, "
        "params_version, status, attempts, source_path, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,'pending',0,?,?,?)",
        (
            task_id, entry_id, kind, digest, engine_version, PARAMS_VERSION,
            source_path, stamp, stamp,
        ),
    )
    return task_id, True


def pending_ids(conn: Any, limit: int = 10) -> list[str]:
    conn.execute("UPDATE media_tasks SET status='failed', error_code='WORKER_EXPIRED' "
                 "WHERE status='processing' AND attempts >= 3 AND updated_at < ?",
                 (iso(now() - timedelta(seconds=300)),))
    return [row[0] for row in conn.execute(
        "SELECT task_id FROM media_tasks WHERE attempts < 3 AND "
        "(status IN ('pending','failed') OR (status='processing' AND updated_at < ?)) "
        "ORDER BY created_at, task_id LIMIT ?",
        (iso(now() - timedelta(seconds=300)), limit),
    )]


def claim(conn: Any, limit: int = 10, *, task_id: str | None = None) -> list[dict[str, Any]]:
    with transaction(conn):
        ids = [task_id] if task_id else pending_ids(conn, limit)
        claimed = []
        for key in ids:
            changed = conn.execute(
                "UPDATE media_tasks SET status='processing', attempts=attempts+1, updated_at=? "
                "WHERE task_id=? AND attempts < 3 AND (status IN ('pending','failed','unavailable') "
                "OR (status='processing' AND updated_at < ?))",
                (now_iso(), key, iso(now() - timedelta(seconds=300))),
            ).rowcount
            if changed:
                claimed.append(dict(conn.execute("SELECT * FROM media_tasks WHERE task_id=?", (key,)).fetchone()))
        return claimed


def set_status(conn: Any, task: dict[str, Any], status: str, *, error_code: str | None = None) -> bool:
    return bool(conn.execute(
        "UPDATE media_tasks SET status=?, error_code=?, updated_at=? "
        "WHERE task_id=? AND status='processing' AND attempts=?",
        (status, error_code, now_iso(), task["task_id"], task["attempts"]),
    ).rowcount)


def write_segments(
    conn: Any,
    task_id: str,
    entry_id: str,
    kind: str,
    access: str,
    segments: list[dict[str, Any]],
) -> int:
    removed = conn.execute("DELETE FROM media_segments WHERE task_id = ?", (task_id,)).rowcount
    stamp = now_iso()
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        locator = segment.get("locator") or {}
        segment_id = stable_id("seg", task_id, str(index), text)
        conn.execute(
            "INSERT OR IGNORE INTO media_segments(segment_id, entry_id, task_id, kind, "
            "text, locator_json, access, search_norm, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                segment_id, entry_id, task_id, kind, text,
                json.dumps(locator, ensure_ascii=False), access,
                normalize(text), stamp,
            ),
        )
    count = conn.execute(
        "SELECT COUNT(*) FROM media_segments WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    if count or removed:
        bump_catalog_version(conn)
    return count


def caption_segment(
    conn: Any,
    entry_id: str,
    text: str,
    *,
    locator: dict[str, Any] | None = None,
    access: str = "standard",
) -> int:
    """平台已有可读字幕作为独立证据直接入库。"""
    task_id, created = enqueue(
        conn, entry_id, "caption", text.encode("utf-8"),
        engine_version=CAPTION_ENGINE_VERSION,
    )
    if not created:
        return conn.execute(
            "SELECT COUNT(*) FROM media_segments WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    count = write_segments(
        conn, task_id, entry_id, "caption", access,
        [{"text": text, "locator": locator or {}}],
    )
    conn.execute(
        "UPDATE media_tasks SET status = ? WHERE task_id = ?",
        ("ready" if count else "empty", task_id),
    )
    return count
