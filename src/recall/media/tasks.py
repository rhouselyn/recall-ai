"""SQLite 待办队列与片段写入（开发规格 §11.2）。"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..clock import now_iso
from ..db import bump_catalog_version, open_readwrite
from ..textnorm import normalize
from ..urls import stable_id

OCR_ENGINE_VERSION = "rapidocr-1.4.4"
ASR_ENGINE_VERSION = "paraformer-sherpa-2.0.1"
CAPTION_ENGINE_VERSION = "platform-caption-1"
PARAMS_VERSION = "v1"

SEGMENT_LABELS = {"ocr": "图片识别", "asr": "音频转写", "caption": "平台字幕"}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def enqueue(
    conn: Any,
    entry_id: str,
    kind: str,
    data: bytes,
    *,
    engine_version: str,
    source_path: str | None = None,
) -> tuple[str, bool]:
    digest = _digest(data)
    task_id = stable_id("tsk", entry_id, kind, digest, engine_version, PARAMS_VERSION)
    existing = conn.execute(
        "SELECT status FROM media_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if existing is not None:
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


def claim(conn: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM media_tasks WHERE status IN ('pending','failed') "
        "AND attempts < 3 ORDER BY created_at LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def set_status(conn: Any, task_id: str, status: str, *, error_code: str | None = None) -> None:
    conn.execute(
        "UPDATE media_tasks SET status = ?, attempts = attempts + 1, "
        "error_code = ?, updated_at = ? WHERE task_id = ?",
        (status, error_code, now_iso(), task_id),
    )


def write_segments(
    conn: Any,
    task_id: str,
    entry_id: str,
    kind: str,
    access: str,
    segments: list[dict[str, Any]],
) -> int:
    conn.execute("DELETE FROM media_segments WHERE task_id = ?", (task_id,))
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
    if count:
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
        return 0
    count = write_segments(
        conn, task_id, entry_id, "caption", access,
        [{"text": text, "locator": locator or {}}],
    )
    conn.execute(
        "UPDATE media_tasks SET status = ? WHERE task_id = ?",
        ("ready" if count else "empty", task_id),
    )
    return count
