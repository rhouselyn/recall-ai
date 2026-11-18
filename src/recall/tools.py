"""统一查询契约 v1（开发规格 §6）。

Python / CLI / MCP 调用同一实现。工具只读：不能配置账号、传文件路径、
取凭据、同步、联网取正文或改库。每次调用都重新执行可见性检查。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from contextlib import suppress
from functools import wraps
from typing import Any, Iterable, Sequence

from . import catalog, semantic
from .db import catalog_version, open_readonly
from .errors import RecallError
from .textnorm import DEFAULT_LIMIT, MAX_LIMIT, MAX_QUERY_CHARS, MAX_QUERY_WORDS, like_literal, normalize, query_words
from .urls import display_title, saved_at_iso

PLATFORMS = frozenset(
    {"github", "zhihu", "bilibili", "xiaohongshu", "douyin", "youtube", "web"}
)
SEGMENT_LABELS = {"ocr": "图片识别", "asr": "音频转写", "caption": "平台字幕",
                  "article": "正文", "readme": "仓库 README", "description": "视频简介"}
MAX_SEARCH_SOURCES = 3
MAX_SNIPPETS = 3
MAX_EVIDENCE_SEGMENTS = 5
_TIME_PUNCTUATION = str.maketrans("", "", "-T:Z")


def _read_snapshot(method):
    @wraps(method)
    def read(self, *args, **kwargs):
        if self.conn.in_transaction:
            return method(self, *args, **kwargs)
        self.conn.execute("BEGIN")
        try:
            return method(self, *args, **kwargs)
        finally:
            self.conn.execute("ROLLBACK")
    return read


def _desc_time(value: str | None) -> int:
    """把 UTC RFC 3339 字符串翻成可升序排列的「倒序」键。"""
    return -int(value.translate(_TIME_PUNCTUATION)) if value else 0


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except Exception as exc:
        raise RecallError("INVALID_ARGUMENT", "cursor 无效。") from exc


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _as_source_ids(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        ids = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)) and all(isinstance(part, str) for part in value):
        ids = [part.strip() for part in value if part.strip()]
    else:
        raise RecallError("INVALID_ARGUMENT", "source_ids 必须是字符串列表。")
    if len(set(ids)) > 50:
        raise RecallError("INVALID_ARGUMENT", "source_ids 过多。")
    return ids


class RecallTools:
    """四个只读工具的唯一实现。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        allowed_sources: frozenset[str] | set[str] | None = None,
        include_restricted: bool = False,
        db_path: str | None = None,
    ) -> None:
        self.conn = conn
        self.allowed_sources = (
            None if allowed_sources is None else frozenset(allowed_sources)
        )
        self.include_restricted = bool(include_restricted)
        self.db_path = db_path
        self.segment_fts = False
        with suppress(sqlite3.OperationalError):
            conn.execute("SELECT rowid FROM segment_fts WHERE segment_fts MATCH 'recallprobe' LIMIT 1")
            self.segment_fts = True

    # -- 生命周期 ---------------------------------------------------------
    @classmethod
    def open(
        cls,
        db_path: str | None = None,
        allowed_sources: Iterable[str] | None = None,
        *,
        include_restricted: bool = False,
    ) -> "RecallTools":
        """只读打开现有库；库不存在或版本不符抛 DB_NOT_READY。"""
        conn = open_readonly(db_path)
        allowed = None if allowed_sources is None else frozenset(allowed_sources)
        return cls(
            conn, allowed_sources=allowed,
            include_restricted=include_restricted, db_path=db_path,
        )

    def __enter__(self) -> "RecallTools":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # -- 可见性 -----------------------------------------------------------
    def _scope_hash(self) -> str:
        return _hash(
            {
                "allowed": sorted(self.allowed_sources) if self.allowed_sources is not None else None,
                "restricted": self.include_restricted,
            }
        )

    def _scope_sql(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.allowed_sources is not None:
            ids = sorted(self.allowed_sources)
            if not ids:
                clauses.append("0")
            else:
                clauses.append("c.source_id IN (" + ",".join("?" for _ in ids) + ")")
                params.extend(ids)
        if not self.include_restricted:
            clauses.append("e.access = 'standard' AND c.access = 'standard'")
        return (" AND ".join(clauses) if clauses else "1", params)

    def _collection_scope_sql(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.allowed_sources is not None:
            ids = sorted(self.allowed_sources)
            if not ids:
                clauses.append("0")
            else:
                clauses.append("c.source_id IN (" + ",".join("?" for _ in ids) + ")")
                params.extend(ids)
        if not self.include_restricted:
            clauses.append("c.access = 'standard'")
        return (" AND ".join(clauses) if clauses else "1"), params

    def _visible_source_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()
        ids = [r[0] for r in rows]
        if self.allowed_sources is not None:
            ids = [sid for sid in ids if sid in self.allowed_sources]
        return ids

    def _coverage(self, source_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
        ids = list(source_ids) if source_ids is not None else self._visible_source_ids()
        return [catalog.source_summary(self.conn, sid) for sid in ids]

    def _assert_source_ids(self, source_ids: list[str] | None) -> None:
        if not source_ids:
            return
        visible = set(self._visible_source_ids())
        for sid in source_ids:
            if sid not in visible:
                raise RecallError("NOT_FOUND")

    def _assert_collection(self, collection_id: str) -> None:
        if not isinstance(collection_id, str) or not collection_id:
            raise RecallError("INVALID_ARGUMENT", "collection_id 无效。")
        scope, params = self._collection_scope_sql()
        row = self.conn.execute(
            "SELECT c.collection_id FROM collections c WHERE c.collection_id = ? AND " + scope,
            (collection_id, *params),
        ).fetchone()
        if row is None:
            raise RecallError("NOT_FOUND")

    # -- 参数与游标 -------------------------------------------------------
    @staticmethod
    def _limit(limit: Any) -> int:
        if limit is None:
            return DEFAULT_LIMIT
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise RecallError("INVALID_ARGUMENT", "limit 必须是整数。")
        if limit < 1 or limit > MAX_LIMIT:
            raise RecallError("INVALID_ARGUMENT", f"limit 必须在 1–{MAX_LIMIT} 之间。")
        return limit

    def _cursor(
        self, tool: str, filters: dict[str, Any], offset: int, *, unlimited: bool = False
    ) -> str | None:
        if unlimited:
            return None
        return _encode(
            {
                "t": tool,
                "v": catalog_version(self.conn),
                "f": _hash(filters),
                "s": self._scope_hash(),
                "o": offset,
            }
        )

    def _offset(self, tool: str, filters: dict[str, Any], cursor: Any) -> int:
        if cursor is None or cursor == "":
            return 0
        if not isinstance(cursor, str):
            raise RecallError("INVALID_ARGUMENT", "cursor 必须是字符串。")
        payload = _decode(cursor)
        if payload.get("t") != tool or payload.get("f") != _hash(filters):
            raise RecallError("INVALID_ARGUMENT", "cursor 与当前工具或过滤器不匹配。")
        if payload.get("s") != self._scope_hash():
            raise RecallError("STALE_CURSOR")
        if payload.get("v") != catalog_version(self.conn):
            raise RecallError("STALE_CURSOR")
        offset = payload.get("o")
        if not isinstance(offset, int) or offset < 0:
            raise RecallError("INVALID_ARGUMENT", "cursor 无效。")
        return offset

    @staticmethod
    def _paginate(rows: Sequence[Any], offset: int, limit: int) -> tuple[list[Any], bool]:
        page = list(rows[offset : offset + limit])
        return page, offset + limit < len(rows)

    # -- 工具 1 -----------------------------------------------------------
    @_read_snapshot
    def recall_list_sources(self) -> dict[str, Any]:
        sources = catalog.list_sources(
            self.conn, self.allowed_sources,
            include_restricted=self.include_restricted,
        )
        for source in sources:
            source.pop("config_json", None)  # 只存凭据引用，不外泄配置内容
        return {
            "sources": sources,
            "coverage": self._coverage([s["source_id"] for s in sources]),
        }

    # -- 工具 2 -----------------------------------------------------------
    @_read_snapshot
    def recall_list_collections(
        self,
        source_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        size = self._limit(limit)
        if source_id is not None:
            if not isinstance(source_id, str) or not source_id:
                raise RecallError("INVALID_ARGUMENT", "source_id 无效。")
            self._assert_source_ids([source_id])
        filters = {"source_id": source_id}
        offset = self._offset("recall_list_collections", filters, cursor)
        scope, params = self._collection_scope_sql()
        where = [scope]
        if source_id is not None:
            where.append("c.source_id = ?")
            params.append(source_id)
        entry_scope = "e.active = 1" + (
            "" if self.include_restricted else " AND e.access = 'standard'"
        )
        rows = self.conn.execute(
            "SELECT c.collection_id, c.source_id, c.path_json, c.access, "
            "(SELECT COUNT(*) FROM entries e WHERE e.collection_id = c.collection_id AND "
            + entry_scope + ") AS visible_entry_count "
            "FROM collections c WHERE " + " AND ".join(where)
            + " ORDER BY c.source_id, c.collection_id",
            params,
        ).fetchall()
        collections = [
            {
                "collection_id": r["collection_id"],
                "source_id": r["source_id"],
                "path": json.loads(r["path_json"]),
                "access": r["access"],
                "visible_entry_count": r["visible_entry_count"],
            }
            for r in rows
        ]
        page, more = self._paginate(collections, offset, size)
        covered = [source_id] if source_id is not None else self._visible_source_ids()
        return {
            "collections": page,
            "next_cursor": self._cursor("recall_list_collections", filters, offset + size)
            if more else None,
            "coverage": self._coverage(covered),
        }

    # -- 工具 3 -----------------------------------------------------------
    @_read_snapshot
    def recall_search(
        self,
        query: str | None = None,
        source_ids: Any = None,
        platform: str | None = None,
        collection_id: str | None = None,
        saved_after: str | None = None,
        saved_before: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        mode: str = "keyword",
    ) -> dict[str, Any]:
        if mode not in ("keyword", "hybrid"):
            raise RecallError("INVALID_ARGUMENT", "mode 必须是 keyword 或 hybrid。")
        size = self._limit(limit)
        try:
            words, full = query_words(query)
        except ValueError as exc:
            if mode != "hybrid" or not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
                raise RecallError("INVALID_ARGUMENT", "query 超过长度或词数限制。") from exc
            full = normalize(query).strip()
            words = full.split()[:MAX_QUERY_WORDS]
        ids = _as_source_ids(source_ids)
        self._assert_source_ids(ids)
        if platform is not None and (not isinstance(platform, str) or platform not in PLATFORMS):
            raise RecallError("INVALID_ARGUMENT", f"未知内容平台：{platform}")
        if collection_id is not None:
            self._assert_collection(collection_id)
        try:
            after = saved_at_iso(saved_after) if saved_after is not None else None
            before = saved_at_iso(saved_before) if saved_before is not None else None
        except ValueError as exc:
            raise RecallError("INVALID_ARGUMENT", "时间过滤必须是 UTC 或带时区的时间。") from exc

        filters = {
            "query": query, "source_ids": sorted(set(ids)) if ids is not None else None,
            "platform": platform, "collection_id": collection_id,
            "saved_after": after, "saved_before": before,
        }
        generation = None
        if mode == "hybrid" and query:
            filters["mode"] = mode
            version = self.conn.execute("SELECT value FROM meta WHERE key='semantic_version'").fetchone()
            generation = str(version[0]) if version else "0"
        offset = self._offset("recall_search", filters, cursor)
        if generation is not None and cursor and _decode(cursor).get("g", "0") != generation:
            raise RecallError("STALE_CURSOR")
        scope, params = self._search_scope(ids, platform, collection_id, after, before)
        dense = self._semantic_items(query, scope, params, 50) if generation is not None and words else []
        keyword_size = 50 if dense else size
        rows = self._matched_rows(words, scope, params, full, 0 if dense else offset, keyword_size)
        items, more = self._search_items(rows, words, full, keyword_size)
        if dense:
            fused = semantic.fuse(items, dense)
            items, has_fused_tail = self._paginate(fused, offset, size)
            if len(items) < size and more:
                excluded = [item["item_id"] for item in fused]
                tail_scope = [*scope, "e.item_id NOT IN (" + ",".join("?" for _ in excluded) + ")"]
                tail_size = size - len(items)
                tail_rows = self._matched_rows(words, tail_scope, [*params, *excluded], full,
                                               max(0, offset - len(fused)), tail_size)
                tail, more = self._search_items(tail_rows, words, full, tail_size)
                items.extend(tail)
            else:
                more = has_fused_tail or more
        next_cursor = self._cursor("recall_search", filters, offset + size) if more else None
        if next_cursor and generation is not None:
            next_cursor = _encode({**_decode(next_cursor), "g": generation})
        covered = ids if ids is not None else self._visible_source_ids()
        return {"items": items, "next_cursor": next_cursor, "coverage": self._coverage(covered)}

    def _search_items(self, rows, words, full, size):
        ranked = [(self._rank(row, words, full), row) for row in rows]
        grouped: dict[str, list[tuple[int, sqlite3.Row]]] = {}
        for rank, row in ranked:
            grouped.setdefault(row["item_id"], []).append((rank, row))

        def evidence_key(pair: tuple[int, sqlite3.Row]) -> tuple[Any, ...]:
            rank, row = pair
            return (rank, _desc_time(row["saved_at"] or row["imported_at"]), row["entry_id"])

        best_pairs = [min(pairs, key=evidence_key) for pairs in grouped.values()]
        best_pairs.sort(
            key=lambda pair: (
                pair[0], _desc_time(pair[1]["saved_at"] or pair[1]["imported_at"]),
                pair[1]["item_id"],
            )
        )
        page, more = self._paginate(best_pairs, 0, size)

        items: list[dict[str, Any]] = []
        for rank, best in page:
            item_id = best["item_id"]
            evidence = sorted(grouped[item_id], key=evidence_key)[:MAX_SEARCH_SOURCES]
            items.append(
                {
                    "item_id": item_id,
                    "platform": best["platform"],
                    "title": display_title(best["title"], best["link_url"]),
                    "url": best["link_url"],
                    "excerpt": best["excerpt"],
                    "saved_at": best["saved_at"],
                    "sources": [self._evidence_summary(row) for _, row in evidence],
                    "snippets": self._matched_snippets(
                        [row["entry_id"] for _, row in grouped[item_id]], words
                    ),
                }
            )
        return items, more

    # -- 工具 4 -----------------------------------------------------------
    @_read_snapshot
    def recall_get_item(
        self,
        item_id: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        size = self._limit(limit)
        if not isinstance(item_id, str) or not item_id:
            raise RecallError("INVALID_ARGUMENT", "item_id 无效。")
        filters = {"item_id": item_id}
        offset = self._offset("recall_get_item", filters, cursor)
        rows = self._item_rows(item_id)
        if not rows:
            raise RecallError("NOT_FOUND")
        rows = sorted(
            rows,
            key=lambda row: (
                _desc_time(row["saved_at"] or row["imported_at"]), row["entry_id"]
            ),
        )
        page, more = self._paginate(rows, offset, size)
        best = rows[0]
        evidence = [self._evidence_detail(row) for row in page]
        return {
            "item": {
                "item_id": item_id,
                "platform": best["platform"],
                "title": display_title(best["title"], best["link_url"]),
                "url": best["link_url"],
                "excerpt": best["excerpt"],
                "saved_at": best["saved_at"],
                "imported_at": best["imported_at"],
            },
            "evidence": evidence,
            "next_cursor": self._cursor("recall_get_item", filters, offset + size)
            if more else None,
            "coverage": self._coverage(sorted({r["source_id"] for r in rows})),
        }

    @_read_snapshot
    def read_archived_content(self, item_id: str, entry_id: str, cursor: str | None = None) -> dict[str, Any]:
        if not any(row["entry_id"] == entry_id for row in self._item_rows(item_id)):
            raise RecallError("NOT_FOUND")
        page_size = 30
        filters = {"item_id": item_id, "entry_id": entry_id}
        offset = self._offset("read_archived_content", filters, cursor)
        segments, more = self._paginate(self._segments(entry_id, limit=page_size + 1, offset=offset), 0, page_size)
        return {"segments": segments, "next_cursor": self._cursor("read_archived_content", filters, offset + page_size)
                if more else None}

    # -- 内部查询 ---------------------------------------------------------
    def _search_scope(self, source_ids, platform, collection_id, after, before):
        scope_sql, params = self._scope_sql()
        where = ["e.active = 1", scope_sql]
        if source_ids is not None:
            if not source_ids:
                where.append("0")
            else:
                where.append("c.source_id IN (" + ",".join("?" for _ in source_ids) + ")")
                params.extend(source_ids)
        if platform is not None:
            where.append("i.platform = ?")
            params.append(platform)
        if collection_id is not None:
            where.append("e.collection_id = ?")
            params.append(collection_id)
        if after is not None:
            where.append("e.saved_at >= ?")
            params.append(after)
        if before is not None:
            where.append("e.saved_at < ?")
            params.append(before)
        return where, params

    @_read_snapshot
    def semantic_items(self, query, platform, limit):
        scope, params = self._search_scope(None, platform, None, None, None)
        return self._semantic_items(query, scope, params, limit)

    def _semantic_items(self, query, scope, params, limit):
        if not semantic.enabled():
            return []
        predicates = list(scope)
        if not self.include_restricted:
            predicates.append("ms.access='standard'")
        try:
            rows = self.conn.execute(
                "SELECT e.item_id, e.entry_id, i.platform, e.title, e.excerpt, e.saved_at, e.link_url, "
                "c.source_id, c.path_json, c.collection_id, e.capture, "
                "ms.kind, ms.text, ms.locator_json, ms.created_at, v.vectors "
                "FROM segment_vectors v JOIN media_segments ms USING(segment_id) "
                "JOIN entries e USING(entry_id) JOIN items i USING(item_id) "
                "JOIN collections c USING(collection_id) WHERE v.model=? AND " + " AND ".join(predicates)
                + " ORDER BY v.segment_id", (semantic.VERSION, *params),
            )
            matches = semantic.nearest(rows, query, limit)
        except (sqlite3.OperationalError, ImportError, OSError, ValueError, RuntimeError):
            return []
        return [{
            "item_id": row["item_id"], "platform": row["platform"],
            "title": display_title(row["title"], row["link_url"]), "url": row["link_url"],
            "excerpt": row["excerpt"], "saved_at": row["saved_at"],
            "sources": [self._evidence_summary(row)], "snippets": [self._segment(row)],
        } for row in matches]

    def _matched_rows(self, words, scope, scope_params, full, offset, limit):
        where, params = list(scope), list(scope_params)
        for word in words:
            pattern = like_literal(word)
            segment_scope = "" if self.include_restricted else " AND ms.access = 'standard'"
            indexed = self.segment_fts and len(word) >= 3 and bool(re.fullmatch(r"[^\W_]+", word))
            segment_match = (
                "ms.rowid IN (SELECT rowid FROM segment_fts WHERE segment_fts MATCH ?) "
                "AND ms.search_norm LIKE ? ESCAPE '\\'" if indexed else "ms.search_norm LIKE ? ESCAPE '\\'"
            )
            where.append(
                "(e.search_norm LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM media_segments ms "
                "WHERE ms.entry_id = e.entry_id AND " + segment_match
                + segment_scope + "))"
            )
            params.append(pattern)
            if indexed:
                params.append('"' + word + '"')
            params.append(pattern)
        title_matches = " AND ".join("instr(e.title_norm, ?) > 0" for _ in words) or "0"
        rank_sql = (
            "CASE WHEN ? != '' AND instr(e.title_norm, ?) > 0 THEN 0 "
            "WHEN " + title_matches + " THEN 1 ELSE 2 END"
        )
        joins = (
            "FROM entries e JOIN collections c ON c.collection_id = e.collection_id "
            "JOIN items i ON i.item_id = e.item_id "
        )
        select = (
            "SELECT e.entry_id, e.item_id, e.collection_id, e.external_key, e.title, "
            "e.excerpt, e.note, e.link_url, e.saved_at, e.imported_at, e.last_seen_at, "
            "e.capture, e.access, e.title_norm, e.search_norm, "
            "i.platform, i.canonical_url, c.source_id, c.path_json, c.access AS collection_access "
            + joins
        )
        # 在 SQLite 内选出本页内容项，只把这些内容项的匹配证据送回 Python。
        paged_sql = (
            "WITH matched AS (SELECT e.entry_id, e.item_id, " + rank_sql + " AS match_rank, "
            "COALESCE(e.saved_at, e.imported_at) AS sort_time " + joins
            + "WHERE " + " AND ".join(where) + "), ranked AS ("
            "SELECT item_id, match_rank, sort_time, "
            "ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY match_rank, "
            "sort_time DESC, entry_id) AS evidence_number FROM matched"
            "), page AS (SELECT item_id FROM ranked WHERE evidence_number = 1 "
            "ORDER BY match_rank, sort_time DESC, item_id LIMIT ? OFFSET ?) "
            + select + "WHERE e.entry_id IN (SELECT entry_id FROM matched "
            "WHERE item_id IN (SELECT item_id FROM page))"
        )
        return self.conn.execute(paged_sql, [full, full, *words, *params, limit + 1, offset]).fetchall()

    def _item_rows(self, item_id: str) -> list[sqlite3.Row]:
        scope_sql, params = self._scope_sql()
        return self.conn.execute(
            "SELECT e.entry_id, e.item_id, e.collection_id, e.external_key, e.title, "
            "e.excerpt, e.note, e.link_url, e.saved_at, e.imported_at, e.last_seen_at, "
            "e.capture, e.access, e.title_norm, e.search_norm, "
            "i.platform, i.canonical_url, c.source_id, c.path_json, c.access AS collection_access "
            "FROM items i JOIN entries e ON e.item_id = i.item_id "
            "JOIN collections c ON c.collection_id = e.collection_id "
            "WHERE i.item_id = ? AND e.active = 1 AND " + scope_sql,
            (item_id, *params),
        ).fetchall()

    @staticmethod
    def _rank(row: sqlite3.Row, words: list[str], full: str) -> int:
        title = row["title_norm"] or ""
        if full and full in title:
            return 0
        if words and all(word in title for word in words):
            return 1
        return 2

    @staticmethod
    def _evidence_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "source_id": row["source_id"],
            "collection_id": row["collection_id"],
            "path": json.loads(row["path_json"]),
            "capture": row["capture"],
            "url": row["link_url"],
        }

    def _segments(self, entry_id: str, words: list[str] | None = None, limit: int = MAX_SNIPPETS, *,
                  offset: int = 0) -> list[dict[str, Any]]:
        sql = (
            "SELECT ms.kind, ms.text, ms.locator_json, e.link_url, ms.created_at FROM media_segments ms "
            "JOIN entries e USING(entry_id) WHERE ms.entry_id = ?"
            + ("" if self.include_restricted else " AND ms.access = 'standard'")
        )
        params: list[Any] = [entry_id]
        if words:
            sql += " AND (" + " OR ".join("ms.search_norm LIKE ? ESCAPE '\\'" for _ in words) + ")"
            params.extend(like_literal(word) for word in words)
        sql += " ORDER BY ms.task_id, ms.rowid LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        rows = self.conn.execute(sql, params).fetchall()
        return [self._segment(row) for row in rows]

    @staticmethod
    def _segment(row):
        locator = json.loads(row["locator_json"])
        url = row["link_url"]
        seek_url = None
        seconds = locator.get("start_seconds")
        if isinstance(seconds, (int, float)):
            parts = urlsplit(url)
            domain = parts.hostname or ""
            if domain in ("bilibili.com", "www.bilibili.com", "youtube.com", "www.youtube.com", "youtu.be"):
                params = dict(parse_qsl(parts.query, keep_blank_values=True))
                params["t"] = str(int(seconds))
                if domain.endswith("bilibili.com") and locator.get("video_part", 1) > 1:
                    params["p"] = str(locator["video_part"])
                seek_url = urlunsplit(parts._replace(query=urlencode(params), fragment=""))
        return {
            "kind": row["kind"],
            "label": SEGMENT_LABELS.get(row["kind"], row["kind"]),
            "text": row["text"],
            "locator": locator,
            "url": url,
            **({"seek_url": seek_url} if seek_url else {}),
            "archived_at": row["created_at"],
        }

    def _matched_snippets(self, entry_ids: list[str], words: list[str]) -> list[dict[str, Any]]:
        if not words:
            return []
        snippets: list[dict[str, Any]] = []
        for entry_id in entry_ids:
            snippets.extend(self._segments(entry_id, words, MAX_SNIPPETS))
            if len(snippets) >= MAX_SNIPPETS:
                break
        return snippets[:MAX_SNIPPETS]

    def _evidence_detail(self, row: sqlite3.Row) -> dict[str, Any]:
        archive = self.conn.execute(
            "SELECT status, error_code FROM media_tasks WHERE entry_id=? AND kind IN ('archive','source_body') "
            "ORDER BY kind DESC LIMIT 1", (row["entry_id"],),
        ).fetchone()
        return {
            "entry_id": row["entry_id"],
            "source_id": row["source_id"],
            "collection_id": row["collection_id"],
            "path": json.loads(row["path_json"]),
            "capture": row["capture"],
            "access": row["access"],
            "url": row["link_url"],
            "title": row["title"],
            "note": row["note"],
            "excerpt": row["excerpt"],
            "saved_at": row["saved_at"],
            "imported_at": row["imported_at"],
            "last_seen_at": row["last_seen_at"],
            "segments": self._segments(row["entry_id"], None, MAX_EVIDENCE_SEGMENTS),
            "archive": dict(archive) if archive else {"status": "pending", "error_code": None},
        }
