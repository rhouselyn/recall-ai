"""Local multilingual embeddings for existing Recall evidence."""
from __future__ import annotations

import os
import re
import threading
from importlib.util import find_spec
from pathlib import Path

from .catalog import transaction
from .db import open_readwrite

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
VERSION = MODEL + ":windows-v1"
DIM = 384
MIN_SCORE = 0.43
_model_lock = threading.Lock()
_model = None


def enabled():
    return os.environ.get("RECALL_SEMANTIC", "1") != "0" and find_spec("fastembed") is not None


def _load_model(offline):
    global _model
    from fastembed import TextEmbedding

    if _model is None:
        root = Path(os.environ["LOCALAPPDATA"]) / "RecallAI" if os.environ.get("LOCALAPPDATA") else (
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "recall-ai"
        )
        cache = Path(os.environ.get("FASTEMBED_CACHE_PATH") or root / "embedding-models")
        _model = TextEmbedding(MODEL, cache_dir=str(cache), threads=2, local_files_only=offline)
    return _model


def embed(texts, *, offline=True):
    import numpy as np

    if not _model_lock.acquire(blocking=not offline):
        raise RuntimeError("Embedding model is loading")
    try:
        model = _load_model(offline)
    finally:
        _model_lock.release()
    vectors = np.asarray(list(model.embed(texts, batch_size=32)), dtype="<f4")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-8)


def _windows(text):
    text = " ".join(text.split())
    width = 110 if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text) else 280
    for start in range(0, len(text), width * 4 // 5):
        yield text[start:start + width]
        if start + width >= len(text):
            break


def index_pending(db_path, limit=16):
    if not enabled():
        return 0
    conn = open_readwrite(db_path)
    try:
        rows = conn.execute(
            "SELECT ms.segment_id, ms.text FROM media_segments ms JOIN entries e USING(entry_id) "
            "LEFT JOIN segment_vectors v USING(segment_id) WHERE e.active=1 "
            "AND (v.segment_id IS NULL OR v.model!=?) ORDER BY ms.rowid LIMIT ?", (VERSION, limit),
        ).fetchall()
        if not rows:
            return 0
        windows = [list(_windows(row["text"])) or [" "] for row in rows]
        vectors = embed([window for group in windows for window in group], offline=False)
        count = offset = 0
        with transaction(conn):
            for row, group in zip(rows, windows):
                block = vectors[offset:offset + len(group)].astype("<f4", copy=False).tobytes()
                offset += len(group)
                count += conn.execute(
                    "INSERT INTO segment_vectors(segment_id, model, vectors) "
                    "SELECT segment_id, ?, ? FROM media_segments WHERE segment_id=? AND text=? "
                    "ON CONFLICT(segment_id) DO UPDATE SET model=excluded.model, vectors=excluded.vectors "
                    "WHERE segment_vectors.model != excluded.model",
                    (VERSION, block, row["segment_id"], row["text"]),
                ).rowcount
            if count:
                conn.execute("INSERT INTO meta(key, value) VALUES('semantic_version', '1') "
                             "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        return count
    finally:
        conn.close()


def nearest(rows, query, limit):
    import numpy as np

    batch = rows.fetchmany(512)
    if not batch:
        return []
    vector = embed([query])[0]
    best = {}
    # shortcut: exact batched cosine scan; add an ANN index only if measured latency requires it.
    while batch:
        valid = [row for row in batch if row["vectors"] and len(row["vectors"]) % (DIM * 4) == 0]
        batch = rows.fetchmany(512)
        if not valid:
            continue
        starts = np.cumsum([0] + [len(row["vectors"]) // (DIM * 4) for row in valid[:-1]])
        matrix = np.frombuffer(b"".join(row["vectors"] for row in valid), dtype="<f4").reshape(-1, DIM)
        scores = np.maximum.reduceat(matrix @ vector, starts)
        for row, score in zip(valid, scores):
            if score >= MIN_SCORE and (row["item_id"] not in best or score > best[row["item_id"]][0]):
                hit = dict(row)
                del hit["vectors"]
                best[row["item_id"]] = (score, hit)
    return [row for _, row in sorted(best.values(), key=lambda pair: (-pair[0], pair[1]["item_id"]))[:limit]]


def fuse(keyword, semantic):
    scores = {}
    items = {}
    for ranked in (keyword, semantic):
        for rank, item in enumerate(ranked):
            key = item["item_id"]
            scores[key] = scores.get(key, 0) + 1 / (12 + rank)
            if key not in items:
                items[key] = dict(item)
            else:
                snippets = [*item["snippets"], *items[key]["snippets"]]
                items[key]["snippets"] = [value for index, value in enumerate(snippets)
                                           if value not in snippets[:index]][:3]
                sources = [*items[key]["sources"], *item["sources"]]
                items[key]["sources"] = [value for index, value in enumerate(sources)
                                          if value not in sources[:index]][:3]
    return sorted(items.values(), key=lambda item: scores[item["item_id"]], reverse=True)
