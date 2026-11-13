"""媒体索引入口、独立 worker 与字幕证据（开发规格 §11）。"""
from __future__ import annotations

from pathlib import Path
import hashlib
import os
import tempfile
from typing import Any

from .. import catalog
from ..db import open_readwrite
from ..config import media_dir
from ..clock import now_iso
from ..leases import heartbeat
from ..errors import RecallError
from . import tasks
from .engines import EngineUnavailable, engine_for, infer_kind
from .tasks import ASR_ENGINE_VERSION, OCR_ENGINE_VERSION

MAX_MEDIA_BYTES = 512 * 1024 * 1024


def _snapshot(path: Path, db_path: str | Path, *, expected_digest: str | None = None) -> tuple[Path, str]:
    root = media_dir(db_path) / "objects"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as output, path.open("rb") as source:
            temporary = Path(output.name)
            size = 0
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise RecallError("INVALID_ARGUMENT", "媒体文件超过上限。")
                digest.update(chunk)
                output.write(chunk)
        if expected_digest is not None and digest.hexdigest() != expected_digest:
            raise RecallError("SOURCE_FORMAT_CHANGED", "媒体原文件已改变，请重新登记索引。")
        target = root / (digest.hexdigest() + path.suffix.lower())
        if not target.exists():
            os.replace(temporary, target)
        return target.resolve(), digest.hexdigest()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        original = path
        path, digest = _snapshot(path, db_path)
        with catalog.transaction(conn):
            if not path.is_file():
                path, digest = _snapshot(original, db_path)
            task_id, created = tasks.enqueue(
                conn, row["entry_id"], resolved_kind, None, digest=digest,
                engine_version=_engine_version(resolved_kind),
                source_path=str(path.resolve()),
            )
            task = dict(
                conn.execute(
                    "SELECT * FROM media_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )
        outcome = _process(conn, task, engines, db_path)
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
        pending = tasks.pending_ids(conn, limit)
        processed: list[dict[str, Any]] = []
        for task_id in pending:
            row = conn.execute("SELECT * FROM media_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is not None:
                task = dict(row)
                outcome = _process(conn, task, engines, db_path)
                processed.append({"task_id": task_id, **outcome})
        return {"processed": processed, "remaining": len(tasks.pending_ids(conn, 1000))}
    finally:
        conn.close()


def _process(
    conn: Any, task: dict[str, Any], engines: dict[str, Any] | None,
    db_path: str | Path,
) -> dict[str, Any]:
    if task["status"] in ("ready", "empty"):
        count = conn.execute(
            "SELECT COUNT(*) FROM media_segments WHERE task_id = ?", (task["task_id"],)
        ).fetchone()[0]
        return {"status": task["status"], "segments": count}
    claimed = tasks.claim(conn, task_id=task["task_id"])
    if not claimed:
        return {"status": "skipped", "segments": 0}
    task = claimed[0]
    with heartbeat(db_path, lambda connection: connection.execute(
        "UPDATE media_tasks SET updated_at=? WHERE task_id=? AND status='processing' AND attempts=?",
        (now_iso(), task["task_id"], task["attempts"]),
    ).rowcount):
        return _extract_claimed(conn, task, Path(task.get("source_path") or ""), engines, db_path)


def _extract_claimed(conn, task, path, engines, db_path):
    if not path.is_file():
        tasks.set_status(conn, task, "unavailable", error_code="MEDIA_MISSING")
        return {"status": "unavailable", "segments": 0}
    runner = (engines or {}).get(task["kind"]) or engine_for(task["kind"])
    try:
        path, _ = _snapshot(path, db_path, expected_digest=task["digest"])
        conn.execute("UPDATE media_tasks SET source_path=? WHERE task_id=? AND attempts=?",
                     (str(path), task["task_id"], task["attempts"]))
        segments = runner(str(path))
    except RecallError:
        tasks.set_status(conn, task, "unavailable", error_code="MEDIA_CHANGED")
        return {"status": "unavailable", "segments": 0}
    except EngineUnavailable:
        tasks.set_status(conn, task, "unavailable", error_code="ENGINE_UNAVAILABLE")
        return {"status": "unavailable", "segments": 0}
    except Exception:
        tasks.set_status(conn, task, "failed", error_code="EXTRACT_FAILED")
        return {"status": "failed", "segments": 0}
    with catalog.transaction(conn):
        if not tasks.set_status(conn, task, "ready"):
            return {"status": "skipped", "segments": 0}
        count = tasks.write_segments(
            conn, task["task_id"], task["entry_id"], task["kind"],
            _entry_access(conn, task["entry_id"]), segments,
        )
        conn.execute("UPDATE media_tasks SET status=? WHERE task_id=?",
                     ("ready" if count else "empty", task["task_id"]))
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
