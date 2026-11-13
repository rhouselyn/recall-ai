"""小红书本人收藏与专辑（开发规格 §5.5 / R13）。

共用登录浏览器读取机制：只读取页面正常加载产生的列表数据或可见卡片，
不执行任何写入动作。覆盖范围分别报告平铺列表与专辑。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry, require_session

NOTE_API = "/api/sns/web/v2/note/collect/page"


def parse_collect_page(payload: Any) -> tuple[list[dict[str, Any]], str | None, bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RecallError("SOURCE_FORMAT_CHANGED", "小红书收藏响应缺少数据。")
    data = payload["data"]
    if "notes" not in data and "note_list" not in data:
        raise RecallError("SOURCE_FORMAT_CHANGED", "小红书收藏响应缺少条目列表。")
    notes = data.get("notes") or data.get("note_list") or []
    if not isinstance(notes, list):
        raise RecallError("SOURCE_FORMAT_CHANGED", "小红书收藏列表格式变化。")
    parsed = []
    for note in notes:
        note_id = note.get("note_id") or note.get("id")
        if not note_id:
            continue
        token = note.get("xsec_token")
        suffix = "?" + urlencode({"xsec_token": token, "xsec_source": "pc_share"}) if token else ""
        parsed.append(
            {
                "key": f"note:{note_id}",
                "url": f"https://www.xiaohongshu.com/explore/{note_id}{suffix}",
                "title": (note.get("display_title") or note.get("title") or ""),
                "type": note.get("type") or "normal",
            }
        )
    return parsed, data.get("cursor"), bool(data.get("has_more"))


class XiaohongshuFavoritesConnector(Connector):
    kind = "xiaohongshu_favorites"
    provider = "xiaohongshu"
    trusted = False

    def fetch(
        self,
        mode: str = "head",
        *,
        state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> FetchResult:
        session = require_session(self)
        identity = session.identity()
        user_id = str(identity.get("user_id") or "")
        if not user_id:
            raise RecallError("AUTH_REQUIRED", "无法确认当前登录身份。")
        bound = self.config.get("account_id")
        if bound and str(bound) != user_id:
            raise RecallError("ACCOUNT_MISMATCH", "登录账号与已绑定来源不一致。")
        budget = dict(budget or {})
        budget.setdefault("max_requests", 20)
        result = FetchResult(coverage="authorized_subset", account_id=user_id)
        max_pages = 1 if mode == "head" else int(budget.get("max_pages", 30))

        cursor = ""
        seen: set[str] = set()
        pages = 0
        finished = True
        while True:
            if cursor in seen:
                raise RecallError("SOURCE_FORMAT_CHANGED", "收藏分页出现循环。")
            seen.add(cursor)
            if pages >= max_pages or not check_budget(result, budget):
                finished = False
                break
            payload = session.get_json(
                NOTE_API, params={"user_id": user_id, "num": 20, "cursor": cursor}
            )
            result.requests_used += 1
            notes, next_cursor, has_more = parse_collect_page(payload)
            for note in notes:
                result.entries.append(
                    make_entry(
                        note["url"], title=note["title"], path=("收藏",), collection_key="xhs:favorites",
                        external_key=note["key"],
                        note=f"type={note['type']}",
                        access="restricted", collection_access="restricted",
                    )
                )
            pages += 1
            if not has_more or not next_cursor:
                break
            cursor = str(next_cursor)

        boards = self.config.get("board_ids") or []
        for board_id in boards:
            if not check_budget(result, budget):
                finished = False
                break
            payload = session.get_json(
                f"/api/sns/web/v1/board/{board_id}/notes", params={"num": 20}
            )
            result.requests_used += 1
            notes, _, _ = parse_collect_page(payload)
            for note in notes:
                result.entries.append(
                    make_entry(
                        note["url"], title=note["title"], path=("收藏专辑", str(board_id)), collection_key=f"xhs:board:{board_id}",
                        external_key=f"board:{board_id}:{note['key']}",
                        access="restricted", collection_access="restricted",
                    )
                )
        if not boards:
            result.warnings.append("BOARDS_NOT_ENUMERATED")
        result.complete = False
        result.coverage = "authorized_subset"
        result.summary = f"{pages} pages / {len(boards)} boards"
        return result
