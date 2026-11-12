"""文件与链接导入（开发规格 §5.1 / R02 / R03）。

HTML 默认合并，``--replace`` 表示该来源的新完整导出；CSV 与单条链接永远
增量合并。整份文件的结构错误直接失败，孤立坏行只跳过并给出行号与错误码。
日志、报告和异常里不出现原文或完整 URL。
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace as dataclass_replace
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import catalog
from .clock import now_iso
from .db import open_readwrite
from .errors import RecallError
from .urls import (
    MAX_FILE_BYTES,
    MAX_ITEMS_PER_IMPORT,
    MAX_TITLE_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_URL_LENGTH,
    SafeUrl,
    UrlProblem,
    sanitize_url,
    saved_at_iso,
    truncate_display,
)

CSV_COLUMNS = frozenset({"url", "title", "collection", "saved_at", "note", "access"})
PRIVATE_SCHEMES = ("javascript:", "chrome:", "edge:", "file:", "data:", "about:", "blob:")


@dataclass
class RowError:
    line: int
    code: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "code": self.code}


@dataclass
class _ImportContext:
    source_id: str
    counts: dict[str, Any] = field(default_factory=dict)
    row_errors: list[RowError] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    collections: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.counts.update(
            {
                "items_added": 0, "collections_added": 0, "entries_added": 0,
                "entries_updated": 0, "entries_unchanged": 0,
                "entries_deactivated": 0, "items_removed": 0,
                "excluded_unsafe": 0, "sanitized": 0, "skipped_rows": 0,
                "truncated_fields": [], "empty_collections": 0,
            }
        )

    def finish(self, result: catalog.WriteResult, status: str, coverage: str, run_id: str) -> dict[str, Any]:
        counts = dict(self.counts)
        for key, value in result.as_counts().items():
            if isinstance(value, int):
                # 导入器先建目录、catalog 再计数，这里相加避免互相覆盖。
                counts[key] = counts.get(key, 0) + value if isinstance(counts.get(key), int) else value
            else:
                counts[key] = value
        counts["truncated_fields"] = sorted(set(self.truncated + result.truncated_fields))
        return {
            "source_id": self.source_id,
            "status": status,
            "counts": counts,
            "coverage": coverage,
            "row_errors": [err.as_dict() for err in self.row_errors],
            "run_id": run_id,
        }


def _read_file(file_path: str | Path, ctx: _ImportContext) -> str:
    path = Path(file_path)
    if not path.is_file():
        raise RecallError("INVALID_ARGUMENT", "导入文件不存在。")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise RecallError("INVALID_ARGUMENT", "文件超过 20 MiB 上限。")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise RecallError("INVALID_ARGUMENT", "文件超过 20 MiB 上限。")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RecallError("INVALID_ARGUMENT", "文件不是可解码的 UTF-8 文本。") from exc


def _prepare_entry(
    raw_url: str,
    *,
    title: str | None,
    note: str | None,
    path: tuple[str, ...],
    saved_at: str | None,
    capture: str,
    access: str,
    ctx: _ImportContext,
    line: int,
) -> catalog.EntryInput | None:
    try:
        safe = sanitize_url(raw_url)
    except UrlProblem as exc:
        ctx.counts["excluded_unsafe"] += 1
        ctx.row_errors.append(RowError(line, exc.code))
        return None
    if safe.sanitized:
        ctx.counts["sanitized"] += 1

    clean_title, truncated = truncate_display(title, MAX_TITLE_LENGTH)
    if truncated:
        ctx.truncated.append("title")
    clean_note, truncated = truncate_display(note, MAX_TEXT_LENGTH)
    if truncated:
        ctx.truncated.append("note")
    return catalog.EntryInput(
        safe=safe,
        title=clean_title,
        note=clean_note,
        saved_at=saved_at,
        path=path,
        external_key=safe.canonical_url,
        capture=capture,
        access=access,
    )


def _ensure_collections(
    conn: Any, source_id: str, paths: Iterable[tuple[str, ...]], ctx: _ImportContext
) -> None:
    """先把目录全部落库（含空目录），再在合并完成后统计其中真正没有条目的。"""
    for path in paths:
        if not path:
            continue
        collection_id, created = catalog.ensure_collection(conn, source_id, path)
        if created:
            ctx.counts["collections_added"] += 1
        ctx.collections.add(collection_id)


def _count_empty_collections(conn: Any, ctx: _ImportContext) -> None:
    for collection_id in ctx.collections:
        row = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE collection_id = ? AND active = 1",
            (collection_id,),
        ).fetchone()
        if row and row[0] == 0:
            ctx.counts["empty_collections"] += 1


# --------------------------------------------------------------------------
# Netscape 书签 HTML
# --------------------------------------------------------------------------


class _BookmarkHTMLParser(HTMLParser):
    """按 Chromium 导出结构还原树；忽略脚本与私有协议。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, ...]] = []
        self.dl_depth = 0
        self.saw_dl = False
        self.saw_link = False
        self.pending_folder: str | None = None
        self.current_a: dict[str, Any] | None = None
        self.current_h3: str | None = None
        self.in_script = 0
        self.unclosed_tag = False
        self.links: list[dict[str, Any]] = []
        self.folders: set[tuple[str, ...]] = set()

    @property
    def path(self) -> tuple[str, ...]:
        return self.stack[-1] if self.stack else ()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            self.in_script += 1
            return
        if self.in_script:
            return
        if tag == "dl":
            self.saw_dl = True
            self.dl_depth += 1
            if self.pending_folder is not None:
                new_path = self.path + (self.pending_folder,)
                self.pending_folder = None
                self.stack.append(new_path)
                self.folders.add(new_path)
            else:
                self.stack.append(self.path)
            return
        if tag == "h3":
            self.current_h3 = ""
            self.unclosed_tag = True
            return
        if tag == "a":
            self.current_a = {
                "href": attr.get("href", ""),
                "add_date": attr.get("add_date", ""),
                "title": "",
                "path": self.path,
            }
            self.unclosed_tag = True
            return
        # <DT> / <P> / <HR> / <H1> / <META> 不参与结构

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in ("dl", "a", "h3", "script"):
            return
        if tag.lower() == "a":
            self.handle_endtag("a")
        elif tag.lower() == "h3":
            self.handle_endtag("h3")

    def handle_data(self, data: str) -> None:
        if self.in_script:
            return
        if self.current_a is not None:
            self.current_a["title"] += data
        elif self.current_h3 is not None:
            self.current_h3 += data

    def handle_entityref(self, name: str) -> None:
        # convert_charrefs=True 时通常不会走到这里，保留兜底。
        self.handle_data(unescape(f"&{name};"))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            self.in_script = max(0, self.in_script - 1)
            return
        if self.in_script:
            return
        if tag == "dl":
            if self.dl_depth > 0:
                self.dl_depth -= 1
            if self.stack:
                self.stack.pop()
            return
        if tag == "h3":
            name = (self.current_h3 or "").strip()
            if name:
                self.pending_folder = name
                self.folders.add(self.path + (name,))
            self.current_h3 = None
            self.unclosed_tag = False
            return
        if tag == "a":
            if self.current_a is not None:
                self.saw_link = True
                self.links.append(self.current_a)
            self.current_a = None
            self.unclosed_tag = False
            return


def _parse_bookmarks_html(text: str) -> _BookmarkHTMLParser:
    if "netscape-bookmark-file-1" not in text[:512].lower():
        raise RecallError("INVALID_ARGUMENT", "文件不是可识别的书签导出 HTML。")
    parser = _BookmarkHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise RecallError("INVALID_ARGUMENT", "书签 HTML 结构损坏。") from exc
    if not parser.saw_dl:
        raise RecallError("INVALID_ARGUMENT", "书签 HTML 缺少目录结构。")
    if parser.dl_depth != 0 or parser.unclosed_tag or parser.current_a is not None:
        raise RecallError("INVALID_ARGUMENT", "书签 HTML 疑似截断，目录未闭合。")
    if not re.search(r"</DL>\s*(?:<p>\s*)?$", text[-256:], re.IGNORECASE):
        raise RecallError("INVALID_ARGUMENT", "书签 HTML 尾部不完整，疑似截断。")
    if len(parser.links) > MAX_ITEMS_PER_IMPORT:
        raise RecallError("INVALID_ARGUMENT", "书签条目超过单次导入上限。")
    return parser


def import_bookmarks_html(
    db_path: str | Path,
    file_path: str | Path,
    source_id: str,
    browser: str,
    *,
    replace: bool = False,
    accept_empty: bool = False,
) -> dict[str, Any]:
    if browser not in ("chrome", "edge"):
        raise RecallError("INVALID_ARGUMENT", "browser 只能是 chrome 或 edge。")
    ctx = _ImportContext(source_id)
    text = _read_file(file_path, ctx)
    parser = _parse_bookmarks_html(text)
    conn = open_readwrite(db_path)
    coverage = "complete_export" if replace else "imported_subset"
    run_id = catalog.start_run(conn, source_id, "file", coverage)
    try:
        catalog.ensure_source(
            conn, source_id, "bookmarks_html", browser, coverage=coverage
        )
        entries: list[catalog.EntryInput] = []
        for link in parser.links:
            href = link["href"].strip()
            if href.lower().startswith(PRIVATE_SCHEMES):
                ctx.counts["excluded_unsafe"] += 1
                continue
            if len(href) > MAX_URL_LENGTH:
                ctx.row_errors.append(RowError(len(entries) + 1, "URL_TOO_LONG"))
                ctx.counts["skipped_rows"] += 1
                continue
            try:
                saved_at = saved_at_iso(link["add_date"]) if link["add_date"] else None
            except ValueError:
                saved_at = None
            entry = _prepare_entry(
                href,
                title=link["title"].strip() or None,
                note=None,
                path=link["path"],
                saved_at=saved_at,
                capture="file_import",
                access="standard",
                ctx=ctx,
                line=len(entries) + 1,
            )
            if entry is not None:
                entries.append(entry)
        if len(entries) > MAX_ITEMS_PER_IMPORT:
            raise RecallError("INVALID_ARGUMENT", "书签条目超过单次导入上限。")
        with catalog.transaction(conn):
            _ensure_collections(conn, source_id, parser.folders, ctx)
            if replace:
                # 结构合法但零条的替换可能是异常空快照，必须显式允许。
                if not entries and not accept_empty:
                    raise RecallError(
                        "SNAPSHOT_INCOMPLETE", "完整导出快照为空，拒绝替换旧数据。"
                    )
                result = catalog.replace_snapshot(
                    conn, source_id, entries, allow_empty=accept_empty
                )
            else:
                result = catalog.merge_entries(conn, source_id, entries)
            _count_empty_collections(conn, ctx)
        status = "partial" if ctx.row_errors else "succeeded"
        state_fields: dict[str, Any] = {
            "last_checked_at": now_iso(),
            "last_success_at": now_iso(),
            "coverage": coverage,
            "latest_attempt": "partial" if status == "partial" else "succeeded",
            "state": "idle",
            "failure_count": 0,
        }
        if replace:
            state_fields["last_full_sync_at"] = now_iso()
        catalog.update_sync_state(conn, source_id, **state_fields)
        report = ctx.finish(result, status, coverage, run_id)
        catalog.finish_run(
            conn, run_id, status, counts=report["counts"], coverage=coverage
        )
        return report
    except RecallError as exc:
        catalog.finish_run(conn, run_id, "failed", error_code=exc.code)
        catalog.update_sync_state(
            conn, source_id, last_error_type=exc.code, last_error_at=now_iso(),
            latest_attempt="failed", state="error",
        )
        raise
    except Exception as exc:
        catalog.finish_run(conn, run_id, "failed", error_code="INTERNAL_ERROR")
        raise RecallError("INTERNAL_ERROR", "导入失败，旧数据已保留。") from exc
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Recall 通用交换格式 CSV
# --------------------------------------------------------------------------


def _csv_rows(text: str, ctx: _ImportContext) -> tuple[list[dict[str, str]], list[RowError]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames
    if not header:
        raise RecallError("INVALID_ARGUMENT", "CSV 表头为空。")
    names = [name.strip() for name in header if name is not None]
    if "url" not in names:
        raise RecallError("INVALID_ARGUMENT", "CSV 缺少必填列 url。")
    if len(set(names)) != len(names) or set(names) - CSV_COLUMNS:
        raise RecallError("INVALID_ARGUMENT", "CSV 表头含重复列或未知列。")
    rows: list[dict[str, str]] = []
    for raw in reader:
        line = reader.line_num
        if raw.get(None) is not None:
            ctx.row_errors.append(RowError(line, "ROW_TOO_WIDE"))
            ctx.counts["skipped_rows"] += 1
            continue
        rows.append({key: (value or "").strip() for key, value in raw.items() if key})
        if len(rows) > MAX_ITEMS_PER_IMPORT:
            raise RecallError("INVALID_ARGUMENT", "CSV 条目超过单次导入上限。")
    return rows, ctx.row_errors


def import_csv(
    db_path: str | Path,
    file_path: str | Path,
    source_id: str,
    platform: str,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    if replace:
        raise RecallError("INVALID_ARGUMENT", "CSV 只支持增量合并，不支持替换。")
    ctx = _ImportContext(source_id)
    text = _read_file(file_path, ctx)
    rows, _ = _csv_rows(text, ctx)
    conn = open_readwrite(db_path)
    coverage = "imported_subset"
    run_id = catalog.start_run(conn, source_id, "file", coverage)
    try:
        catalog.ensure_source(conn, source_id, "manual", platform, coverage=coverage)
        entries: list[catalog.EntryInput] = []
        folder_paths: set[tuple[str, ...]] = set()
        for line, row in enumerate(rows, start=2):
            raw_url = row.get("url", "")
            if not raw_url:
                ctx.row_errors.append(RowError(line, "MISSING_URL"))
                ctx.counts["skipped_rows"] += 1
                continue
            if len(raw_url) > MAX_URL_LENGTH:
                ctx.row_errors.append(RowError(line, "URL_TOO_LONG"))
                ctx.counts["skipped_rows"] += 1
                continue
            try:
                saved_at = saved_at_iso(row.get("saved_at")) if row.get("saved_at") else None
            except ValueError:
                ctx.row_errors.append(RowError(line, "INVALID_TIMESTAMP"))
                ctx.counts["skipped_rows"] += 1
                continue
            access = row.get("access") or "standard"
            if access not in ("standard", "restricted"):
                ctx.row_errors.append(RowError(line, "INVALID_ACCESS"))
                ctx.counts["skipped_rows"] += 1
                continue
            collection = row.get("collection") or ""
            path = (collection,) if collection else ()
            folder_paths.add(path)
            entry = _prepare_entry(
                raw_url,
                title=row.get("title") or None,
                note=row.get("note") or None,
                path=path,
                saved_at=saved_at,
                capture="file_import",
                access=access,
                ctx=ctx,
                line=line,
            )
            if entry is not None:
                entries.append(entry)
        with catalog.transaction(conn):
            _ensure_collections(conn, source_id, folder_paths, ctx)
            result = catalog.merge_entries(conn, source_id, entries)
            _count_empty_collections(conn, ctx)
        status = "partial" if ctx.row_errors else "succeeded"
        catalog.update_sync_state(
            conn, source_id,
            last_checked_at=now_iso(), last_success_at=now_iso(),
            coverage=coverage, latest_attempt="partial" if status == "partial" else "succeeded",
            state="idle", failure_count=0,
        )
        report = ctx.finish(result, status, coverage, run_id)
        catalog.finish_run(
            conn, run_id, status, counts=report["counts"], coverage=coverage
        )
        return report
    except RecallError as exc:
        catalog.finish_run(conn, run_id, "failed", error_code=exc.code)
        catalog.update_sync_state(
            conn, source_id, last_error_type=exc.code, last_error_at=now_iso(),
            latest_attempt="failed", state="error",
        )
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 单条链接
# --------------------------------------------------------------------------


def add_link(
    db_path: str | Path,
    url: str,
    source_id: str,
    platform: str,
    *,
    title: str | None = None,
    collection: str | None = None,
    note: str | None = None,
    saved_at: str | None = None,
    access: str = "standard",
) -> dict[str, Any]:
    if access not in ("standard", "restricted"):
        raise RecallError("INVALID_ARGUMENT", "access 只能是 standard 或 restricted。")
    ctx = _ImportContext(source_id)
    try:
        stamp = saved_at_iso(saved_at) if saved_at else None
    except ValueError as exc:
        raise RecallError("INVALID_ARGUMENT", "收藏时间无效。") from exc
    path = (collection,) if collection else ()
    entry = _prepare_entry(
        url, title=title, note=note, path=path, saved_at=stamp,
        capture="manual_capture", access=access, ctx=ctx, line=1,
    )
    if entry is None:
        raise RecallError("INVALID_ARGUMENT", "链接不安全或无效，请提供安全地址。")
    conn = open_readwrite(db_path)
    coverage = "imported_subset"
    run_id = catalog.start_run(conn, source_id, "file", coverage)
    try:
        catalog.ensure_source(conn, source_id, "manual", platform, coverage=coverage)
        with catalog.transaction(conn):
            _ensure_collections(conn, source_id, [path], ctx)
            result = catalog.merge_entries(conn, source_id, [entry])
            _count_empty_collections(conn, ctx)
        catalog.update_sync_state(
            conn, source_id, last_checked_at=now_iso(), last_success_at=now_iso(),
            coverage=coverage, latest_attempt="succeeded", state="idle", failure_count=0,
        )
        catalog.finish_run(conn, run_id, "succeeded", counts=result.as_counts(), coverage=coverage)
        return ctx.finish(result, "succeeded", coverage, run_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 浏览器扩展的完整书签树（Native Messaging）
# --------------------------------------------------------------------------


def _tree_paths(nodes: Sequence[dict[str, Any]]) -> tuple[dict[str, tuple[str, ...]], set[tuple[str, ...]]]:
    """返回「书签 → 所在目录路径」与「全部目录路径」。

    目录路径包含自身标题（用于子节点），书签的收藏路径只到父目录，
    不能把书签自己的标题当成夹名。
    """
    by_id = {str(node.get("id")): node for node in nodes}
    own_paths: dict[str, tuple[str, ...]] = {}
    folders: set[tuple[str, ...]] = set()

    def compute(node_id: str, seen: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if node_id in own_paths:
            return own_paths[node_id]
        if node_id in seen:
            return ()
        node = by_id.get(node_id)
        if node is None:
            return ()
        parent = str(node.get("parentId") or "")
        parent_path = compute(parent, seen | {node_id}) if parent and parent != "0" else ()
        title = str(node.get("title") or "").strip()
        own = parent_path + ((title,) if title else ())
        own_paths[node_id] = own
        if not node.get("url"):
            folders.add(own)
        return own

    entry_paths: dict[str, tuple[str, ...]] = {}
    for node in nodes:
        node_id = str(node.get("id"))
        compute(node_id)
        if node.get("url"):
            parent = str(node.get("parentId") or "")
            entry_paths[node_id] = (
                compute(parent) if parent and parent != "0" else ()
            )
    return entry_paths, folders


def import_bookmark_tree(
    db_path: str | Path, payload: dict[str, Any], source_id: str
) -> dict[str, Any]:
    """浏览器扩展事件合并后发送的完整树；返回 JSON 可序列化的确认结果。"""
    def failed(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message, "retryable": False}}

    if not isinstance(payload, dict):
        return failed("INVALID_ARGUMENT", "消息体不是对象。")
    profile = payload.get("profile_uuid")
    version = payload.get("version")
    browser = payload.get("browser", "chrome")
    nodes = payload.get("nodes")
    if not isinstance(profile, str) or not profile or len(profile) > 200:
        return failed("INVALID_ARGUMENT", "资料档标识无效。")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return failed("INVALID_ARGUMENT", "快照版本无效。")
    if browser not in ("chrome", "edge"):
        return failed("INVALID_ARGUMENT", "浏览器类型无效。")
    if not isinstance(nodes, list) or len(nodes) > MAX_ITEMS_PER_IMPORT:
        return failed("INVALID_ARGUMENT", "节点数量无效或超过上限。")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_FILE_BYTES:
        return failed("INVALID_ARGUMENT", "消息超过大小上限。")

    conn = open_readwrite(db_path)
    try:
        source = catalog.get_source(conn, source_id)
        config = json.loads(source["config_json"]) if source else {}
        if source is None:
            catalog.ensure_source(
                conn, source_id, "bookmarks_live", browser,
                coverage="live_profile", refresh_mode="event", sync_enabled=True,
                config={"profile_uuid": profile},
            )
        else:
            if source["kind"] != "bookmarks_live" or source["provider"] != browser:
                return failed("INVALID_ARGUMENT", "来源类型与浏览器不匹配。")
            if config.get("profile_uuid") not in (None, profile):
                return failed("ACCOUNT_MISMATCH", "资料档绑定与来源不一致。")
            if config.get("profile_uuid") is None:
                # 首次绑定资料档 UUID。
                conn.execute(
                    "UPDATE sources SET config_json = ? WHERE source_id = ?",
                    (json.dumps({"profile_uuid": profile}), source_id),
                )
        state = catalog.get_sync_state(conn, source_id)
        try:
            previous = json.loads(state.get("last_summary") or "{}").get("tree_version", 0)
        except (ValueError, TypeError, AttributeError):
            previous = 0
        if version < int(previous):
            return failed("STALE_CURSOR", "旧版本快照不能覆盖更新的目录。")

        ctx = _ImportContext(source_id)
        paths, folder_paths = _tree_paths(nodes)
        entries: list[catalog.EntryInput] = []
        for node in nodes:
            raw_url = str(node.get("url") or "").strip()
            if not raw_url:
                continue
            if raw_url.lower().startswith(PRIVATE_SCHEMES):
                ctx.counts["excluded_unsafe"] += 1
                continue
            node_id = str(node.get("id"))
            try:
                added = node.get("dateAdded")
                stamp = saved_at_iso(float(added) / 1000.0) if added else None
            except (TypeError, ValueError):
                stamp = None
            entry = _prepare_entry(
                raw_url,
                title=str(node.get("title") or "").strip() or None,
                note=None,
                path=paths.get(node_id, ()),
                saved_at=stamp,
                capture="browser_sync",
                access="standard",
                ctx=ctx,
                line=len(entries) + 1,
            )
            if entry is not None:
                entries.append(
                    dataclass_replace(entry, external_key=f"node:{profile}:{node_id}")
                )
        if not entries and not bool(payload.get("accept_empty", False)) and int(previous) > 0:
            return failed("SNAPSHOT_INCOMPLETE", "异常空快照，旧目录已保留。")

        run_id = catalog.start_run(conn, source_id, "event", "live_profile")
        with catalog.transaction(conn):
            _ensure_collections(conn, source_id, folder_paths, ctx)
            result = catalog.replace_snapshot(
                conn, source_id, entries,
                allow_empty=bool(payload.get("accept_empty", False)),
            )
            _count_empty_collections(conn, ctx)
            catalog.update_sync_state(
                conn, source_id,
                last_checked_at=now_iso(), last_success_at=now_iso(),
                last_full_sync_at=now_iso(), coverage="live_profile",
                latest_attempt="succeeded", state="idle", failure_count=0,
                last_summary=json.dumps({"tree_version": version}),
            )
        report = ctx.finish(result, "succeeded", "live_profile", run_id)
        counts = report["counts"]
        catalog.finish_run(conn, run_id, "succeeded", counts=counts, coverage="live_profile")
        return {
            "ok": True, "version": version, "source_id": source_id,
            "counts": counts, "catalog_version": result.catalog_version,
        }
    except RecallError as exc:
        return failed(exc.code, exc.message)
    except Exception:
        return failed("INTERNAL_ERROR", "写入失败，旧目录已保留。")
    finally:
        conn.close()
