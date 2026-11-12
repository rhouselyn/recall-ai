"""命令行入口（开发规格 §7）。

CLI 面向操作本机的用户；受限条目需显式 --include-restricted。
JSON 模式返回与其他入口相同的错误体；失败一律非零退出。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import catalog, db, ingest, watch
from .config import default_db_path
from .db import open_readwrite
from .errors import RecallError
from .media import index_media, run_pending
from .sources.runner import sync_once
from .tools import RecallTools

CONFIG_KEYS = frozenset(
    {
        "account_id", "label", "api_base", "feed_url", "profile_uuid",
        "collection_ids", "board_ids", "collections", "allow_direct_http",
        "include_collects", "browser_profile",
    }
)
FORBIDDEN_CONFIG_HINTS = ("token", "secret", "cookie", "password", "sessdata", "credential")
ACCOUNT_KINDS = frozenset(
    {
        "github_stars", "zhihu_api", "zhihu_web", "bilibili_favorites",
        "xiaohongshu_favorites", "douyin_favorites", "youtube_favorites", "rss",
    }
)
BROWSER_KINDS = frozenset(
    {"zhihu_web", "bilibili_favorites", "xiaohongshu_favorites", "douyin_favorites", "youtube_favorites"}
)
KIND_PROVIDER = {
    "github_stars": "github", "zhihu_api": "zhihu", "zhihu_web": "zhihu",
    "bilibili_favorites": "bilibili", "xiaohongshu_favorites": "xiaohongshu",
    "douyin_favorites": "douyin", "rss": "rss", "bookmarks_live": "chrome",
    "youtube_favorites": "youtube",
}


def _emit(data: Any, *, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif text is not None:
        print(text)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _parse_config(pairs: list[str]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise RecallError("INVALID_ARGUMENT", "--config 需写成 key=value。")
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in CONFIG_KEYS or any(hint in key.lower() for hint in FORBIDDEN_CONFIG_HINTS):
            raise RecallError("INVALID_ARGUMENT", "该配置项不允许在命令行传递；凭据只放同步进程环境。")
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        if key in ("collection_ids", "board_ids") and isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if key == "collections" and isinstance(value, str):
            value = [{"id": part.strip()} for part in value.split(",") if part.strip()]
        config[key] = value
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recall", description="Recall AI 本地收藏目录")
    parser.add_argument("--db", default=None, help="覆盖 RECALL_DB 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="创建或幂等打开当前版本库")
    web = sub.add_parser("serve", help="启动本地网页")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", help="启动后打开浏览器")

    imp = sub.add_parser("import", help="导入文件或清单")
    imp_sub = imp.add_subparsers(dest="kind", required=True)
    bm = imp_sub.add_parser("bookmarks", help="Chrome / Edge 书签 HTML")
    bm.add_argument("file")
    bm.add_argument("--source", required=True)
    bm.add_argument("--browser", choices=["chrome", "edge"], required=True)
    bm.add_argument("--replace", action="store_true")
    bm.add_argument("--accept-empty", action="store_true")
    bm.add_argument("--json", action="store_true")
    cs = imp_sub.add_parser("csv", help="Recall 通用交换格式 CSV")
    cs.add_argument("file")
    cs.add_argument("--source", required=True)
    cs.add_argument("--platform", required=True)
    cs.add_argument("--json", action="store_true")

    add = sub.add_parser("add", help="录入单条链接")
    add.add_argument("url")
    add.add_argument("--source", required=True)
    add.add_argument("--platform", required=True)
    add.add_argument("--title")
    add.add_argument("--collection")
    add.add_argument("--note")
    add.add_argument("--saved-at")
    add.add_argument("--access", choices=["standard", "restricted"], default="standard")
    add.add_argument("--json", action="store_true")

    source = sub.add_parser("source", help="来源管理")
    source_sub = source.add_subparsers(dest="action", required=True)
    sadd = source_sub.add_parser("add")
    sadd.add_argument("source_id")
    sadd.add_argument("--kind", required=True, choices=sorted(catalog.SOURCE_KINDS))
    sadd.add_argument("--provider")
    sadd.add_argument("--label")
    sadd.add_argument("--config", action="append", default=[])
    sadd.add_argument("--no-sync", action="store_true")
    sadd.add_argument("--json", action="store_true")
    sconnect = source_sub.add_parser("connect")
    sconnect.add_argument("source_id")
    sconnect.add_argument("--wait-seconds", type=int, default=180)
    sconnect.add_argument("--user-id")
    sconnect.add_argument("--headless", action="store_true")
    sconnect.add_argument("--json", action="store_true")
    for action in ("pause", "resume"):
        sp = source_sub.add_parser(action)
        sp.add_argument("source_id")
        sp.add_argument("--json", action="store_true")
    srm = source_sub.add_parser("remove")
    srm.add_argument("source_id")
    srm.add_argument("--yes", action="store_true")
    srm.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="手动刷新一个来源")
    sync.add_argument("source_id")
    sync.add_argument("--full", action="store_true")
    sync.add_argument("--accept-empty", action="store_true")
    sync.add_argument("--json", action="store_true")

    w = sub.add_parser("watch", help="前台持续刷新")
    w.add_argument("--once", action="store_true")
    w.add_argument("--ticks", type=int)
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--json", action="store_true")

    sources = sub.add_parser("sources", help="列出来源与覆盖摘要")
    sources.add_argument("--json", action="store_true")

    collections = sub.add_parser("collections", help="列出可见收藏夹")
    collections.add_argument("--source")
    collections.add_argument("--limit", type=int)
    collections.add_argument("--cursor")
    collections.add_argument("--include-restricted", action="store_true")
    collections.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="关键词查询或浏览")
    search.add_argument("query", nargs="?")
    search.add_argument("--source", action="append", dest="source_ids")
    search.add_argument("--platform")
    search.add_argument("--collection")
    search.add_argument("--saved-after")
    search.add_argument("--saved-before")
    search.add_argument("--limit", type=int)
    search.add_argument("--cursor")
    search.add_argument("--include-restricted", action="store_true")
    search.add_argument("--json", action="store_true")

    get = sub.add_parser("get", help="查看条目详情与来源证据")
    get.add_argument("item_id")
    get.add_argument("--limit", type=int)
    get.add_argument("--cursor")
    get.add_argument("--include-restricted", action="store_true")
    get.add_argument("--json", action="store_true")

    backup = sub.add_parser("backup", help="生成一致的数据库备份")
    backup.add_argument("path")
    backup.add_argument("--json", action="store_true")

    restore = sub.add_parser("restore", help="校验备份并恢复到指定路径")
    restore.add_argument("backup")
    restore.add_argument("--to", required=True)
    restore.add_argument("--json", action="store_true")

    media = sub.add_parser("media", help="本地媒体文本提取")
    media_sub = media.add_subparsers(dest="action", required=True)
    mi = media_sub.add_parser("index")
    mi.add_argument("item_id")
    mi.add_argument("file")
    mi.add_argument("--entry-id")
    mi.add_argument("--kind", choices=["ocr", "asr"])
    mi.add_argument("--json", action="store_true")
    mr = media_sub.add_parser("run")
    mr.add_argument("--limit", type=int, default=10)
    mr.add_argument("--json", action="store_true")

    sub.add_parser("native-host", help="Chrome/Edge Native Messaging 主机")
    return parser


def _tools(args: argparse.Namespace) -> RecallTools:
    return RecallTools.open(
        args.db, include_restricted=getattr(args, "include_restricted", False)
    )


def _run(args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "serve":
        from .web import serve
        serve(args.port, args.db, open_browser=args.open)
        return 0
    if cmd == "init":
        path = db.init_db(args.db)
        _emit({"db": str(path), "user_version": db.SCHEMA_VERSION}, as_json=True,
              text=f"已就绪：{path}")
        return 0
    if cmd == "import":
        if args.kind == "bookmarks":
            report = ingest.import_bookmarks_html(
                args.db, args.file, args.source, args.browser,
                replace=args.replace, accept_empty=args.accept_empty,
            )
        else:
            report = ingest.import_csv(args.db, args.file, args.source, args.platform)
        _emit(report, as_json=args.json, text=f"{report['status']} {report['counts']}")
        return 0 if report["status"] != "failed" else 1
    if cmd == "add":
        report = ingest.add_link(
            args.db, args.url, args.source, args.platform,
            title=args.title, collection=args.collection, note=args.note,
            saved_at=args.saved_at, access=args.access,
        )
        _emit(report, as_json=args.json, text=f"{report['status']} {report['counts']}")
        return 0
    if cmd == "source":
        return _source_command(args)
    if cmd == "sync":
        return _sync_command(args)
    if cmd == "watch":
        report = watch.watch_forever(
            args.db, once=args.once, max_ticks=args.ticks,
            sleep_seconds=args.interval,
        )
        _emit(report, as_json=args.json, text=f"ticks={report['ticks']} checked={len(report['checked'])}")
        return 0
    if cmd == "sources":
        with _tools(args) as tools:
            _emit(tools.recall_list_sources(), as_json=args.json)
        return 0
    if cmd == "collections":
        with _tools(args) as tools:
            result = tools.recall_list_collections(
                source_id=args.source, limit=args.limit, cursor=args.cursor
            )
            _emit(result, as_json=args.json)
        return 0
    if cmd == "search":
        with _tools(args) as tools:
            result = tools.recall_search(
                query=args.query, source_ids=args.source_ids, platform=args.platform,
                collection_id=args.collection, saved_after=args.saved_after,
                saved_before=args.saved_before, limit=args.limit, cursor=args.cursor,
            )
            if not args.json:
                lines = [f"{item['title']}  {item['url']}" for item in result["items"]]
                _emit(result, as_json=False, text="\n".join(lines) if lines else "没有匹配的收藏。")
            else:
                _emit(result, as_json=True)
        return 0
    if cmd == "get":
        with _tools(args) as tools:
            result = tools.recall_get_item(args.item_id, limit=args.limit, cursor=args.cursor)
            _emit(result, as_json=args.json)
        return 0
    if cmd == "backup":
        path = db.backup_db(args.db, args.path)
        _emit({"backup": str(path)}, as_json=args.json, text=f"备份完成：{path}")
        return 0
    if cmd == "restore":
        info = db.verify_db(args.backup)
        path = db.restore_db(args.backup, args.to)
        _emit({**info, "restored_to": str(path)}, as_json=args.json, text=f"已恢复：{path}")
        return 0
    if cmd == "media":
        if args.action == "index":
            report = index_media(
                args.db, args.item_id, args.file, kind=args.kind, entry_id=args.entry_id
            )
        else:
            report = run_pending(args.db, args.limit)
        _emit(report, as_json=args.json)
        return 0
    if cmd == "native-host":
        from .native_host import main as native_main

        return native_main([])
    raise RecallError("INVALID_ARGUMENT", "未知命令。")


def _source_command(args: argparse.Namespace) -> int:
    action = args.action
    if action == "add":
        config = _parse_config(args.config)
        provider = args.provider or KIND_PROVIDER.get(args.kind)
        if not provider:
            raise RecallError("INVALID_ARGUMENT", "该来源类型需要 --provider。")
        refresh = "event" if args.kind == "bookmarks_live" else (
            "poll" if args.kind in ACCOUNT_KINDS else "manual"
        )
        enabled = (not args.no_sync) and refresh != "manual"
        conn = open_readwrite(args.db)
        try:
            catalog.ensure_source(
                conn, args.source_id, args.kind, provider, args.label,
                sync_enabled=enabled, coverage="none", refresh_mode=refresh,
                config=config,
            )
        finally:
            conn.close()
        _emit({"source_id": args.source_id, "kind": args.kind, "refresh_mode": refresh},
              as_json=args.json, text=f"已登记来源：{args.source_id}")
        return 0
    if action == "connect":
        conn = open_readwrite(args.db)
        try:
            source = catalog.get_source(conn, args.source_id)
            if source is None:
                raise RecallError("NOT_FOUND")
            if source["kind"] not in BROWSER_KINDS:
                raise RecallError("INVALID_ARGUMENT", "该来源类型不需要登录浏览器。")
            from .sources.browser import connect as browser_connect

            result = browser_connect(
                args.source_id, source["kind"], wait_seconds=args.wait_seconds,
                headless=args.headless, user_id=args.user_id, db_path=args.db,
            )
            config = json.loads(source["config_json"] or "{}")
            config["browser_profile"] = result["profile_dir"]
            conn.execute(
                "UPDATE sources SET config_json = ?, sync_enabled = 1 WHERE source_id = ?",
                (json.dumps(config, ensure_ascii=False), args.source_id),
            )
            catalog.update_sync_state(conn, args.source_id, state="idle", next_retry_at=None)
        finally:
            conn.close()
        _emit(result, as_json=args.json, text=result["note"])
        return 0
    if action == "pause":
        result = watch.pause_source(args.db, args.source_id)
    elif action == "resume":
        result = watch.resume_source(args.db, args.source_id)
    else:  # remove
        if not args.yes:
            raise RecallError("INVALID_ARGUMENT", "删除来源需要显式 --yes。")
        conn = open_readwrite(args.db)
        try:
            result = catalog.delete_source(conn, args.source_id)
        finally:
            conn.close()
        result = {"source_id": args.source_id, **result}
    _emit(result, as_json=args.json)
    return 0


def _sync_command(args: argparse.Namespace) -> int:
    session = None
    conn = open_readwrite(args.db)
    try:
        source = catalog.get_source(conn, args.source_id)
    finally:
        conn.close()
    if source and source["kind"] in BROWSER_KINDS:
        from .sources.browser import load_session

        session = load_session(args.source_id, source["kind"], args.db)
    try:
        report = sync_once(
            args.db, args.source_id, full=args.full,
            accept_empty=args.accept_empty, session=session,
        )
    finally:
        if session is not None:
            session.close()
    _emit(report, as_json=args.json, text=f"{report['status']} {report.get('counts', '')}")
    return 1 if report.get("status") == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except RecallError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.body, ensure_ascii=False, indent=2))
        else:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        body = RecallError("INTERNAL_ERROR").body
        print("INTERNAL_ERROR", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps(body, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
