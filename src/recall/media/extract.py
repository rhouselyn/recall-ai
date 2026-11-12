"""媒体索引入口、独立 worker 与字幕证据（开发规格 §11）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import catalog
from ..db import open_readwrite
from ..errors import RecallError
from . import tasks
from .engines import EngineUnavailable, engine_for, infer_kind
from .tasks import ASR_ENGINE_VERSION, OCR_ENGINE_VERSION

MAX_MEDIA_BYTES = 512 * 1024 * 1024


def _entries_for_item(conn: Any, item_id: str) -> list[Any]:
    return conn.execute(
        "SELECT e.entry_id, e.access FROM entries e WHERE e.item_id = ? AND e.active = 1 "
        "ORDER BY e.entry_id",
        (item_id,),
    ).fetchall()


def _engine_version(kind: str) -> str:
    return OCR_ENGINE_VERSION if kind == "ocr" else ASR_ENGINE_VERSION


def index_media(
    db_path: str | Path,
    item_id: str,
    file_path: str | Path,
    *,
    kind: str | None = None,
    entry_id: str | None = None,
    engines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``recall media index <item_id> <local_file>``。文件由用户提供并绑定到指定证据。"""
    path = Path(file_path)
    if not path.is_file():
        raise RecallError("INVALID_ARGUMENT", "媒体文件不存在。")
    if path.stat().st_size > MAX_MEDIA_BYTES:
        raise RecallError("INVALID_ARGUMENT", "媒体文件超过上限。")
    data = path.read_bytes()
    resolved_kind = kind or infer_kind(path)
    if resolved_kind not in ("ocr", "asr"):
        raise RecallError("INVALID_ARGUMENT", "无法从扩展名判断提取类型，请显式指定 --kind。")

    conn = open_readwrite(db_path)
    try:
        rows = _entries_for_item(conn, item_id)
        if not rows:
            raise RecallError("NOT_FOUND")
        if entry_id:
            rows = [row for row in rows if row["entry_id"] == entry_id]
            if not rows:
                raise RecallError("NOT_FOUND")
        elif len(rows) > 1:
            raise RecallError(
                "INVALID_ARGUMENT", "同一内容存在多条收藏关系，请用 --entry-id 选择证据来源。"
            )
        row = rows[0]
        with catalog.transaction(conn):
            task_id, created = tasks.enqueue(
                conn, row["entry_id"], resolved_kind, data,
                engine_version=_engine_version(resolved_kind),
                source_path=str(path.resolve()),
            )
            task = dict(
                conn.execute(
                    "SELECT * FROM media_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )
        outcome = _process(conn, task, path, engines)
        return {
            "item_id": item_id,
            "entry_id": row["entry_id"],
            "task_id": task_id,
            "kind": resolved_kind,
            "reused": not created,
            **outcome,
        }
    finally:
        conn.close()


def index_caption(
    db_path: str | Path,
    item_id: str,
    entry_id: str,
    text: str,
    *,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """平台已有可读字幕作为独立证据。"""
    conn = open_readwrite(db_path)
    try:
        rows = [row for row in _entries_for_item(conn, item_id) if row["entry_id"] == entry_id]
        if not rows:
            raise RecallError("NOT_FOUND")
        with catalog.transaction(conn):
            count = tasks.caption_segment(
                conn, entry_id, text, locator=locator, access=rows[0]["access"]
            )
        return {"item_id": item_id, "entry_id": entry_id, "status": "ready" if count else "empty", "segments": count}
    finally:
        conn.close()


def run_pending(
    db_path: str | Path,
    limit: int = 10,
    *,
    engines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """独立 worker：处理 pending / 可重试的 failed 任务。"""
    conn = open_readwrite(db_path)
    try:
        pending = tasks.claim(conn, limit)
        processed: list[dict[str, Any]] = []
        for task in pending:
            source = task.get("source_path")
            if not source or not Path(source).is_file():
                tasks.set_status(conn, task["task_id"], "unavailable", error_code="MEDIA_MISSING")
                processed.append({"task_id": task["task_id"], "status": "unavailable"})
                continue
            outcome = _process(conn, task, Path(source), engines)
            processed.append({"task_id": task["task_id"], **outcome})
        return {"processed": processed, "remaining": len(tasks.claim(conn, 1000))}
    finally:
        conn.close()


def _process(
    conn: Any, task: dict[str, Any], path: Path, engines: dict[str, Any] | None
) -> dict[str, Any]:
    runner = (engines or {}).get(task["kind"]) or engine_for(task["kind"])
    try:
        segments = runner(str(path))
    except EngineUnavailable:
        tasks.set_status(conn, task["task_id"], "unavailable", error_code="ENGINE_UNAVAILABLE")
        return {"status": "unavailable", "segments": 0}
    except Exception:
        tasks.set_status(conn, task["task_id"], "failed", error_code="EXTRACT_FAILED")
        return {"status": "failed", "segments": 0}
    with catalog.transaction(conn):
        count = tasks.write_segments(
            conn, task["task_id"], task["entry_id"], task["kind"],
            _entry_access(conn, task["entry_id"]), segments,
        )
        tasks.set_status(conn, task["task_id"], "ready" if count else "empty")
    return {"status": "ready" if count else "empty", "segments": count}


def _entry_access(conn: Any, entry_id: str) -> str:
    row = conn.execute(
        "SELECT e.access, c.access AS collection_access FROM entries e "
        "JOIN collections c ON c.collection_id = e.collection_id WHERE e.entry_id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        return "restricted"
    if row["access"] == "restricted" or row["collection_access"] == "restricted":
        return "restricted"
    return "standard"
