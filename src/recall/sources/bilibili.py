"""B 站本人视频收藏夹（开发规格 §5.4 / R11）。

默认走隔离登录浏览器读取页面正常加载的响应；只有用户显式批准且提供
会话时才允许直接 HTTP。协议细节来自固定版本的公开接口记录，需真人验收。
"""
from __future__ import annotations

import json
from typing import Any

from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry, require_session

BASE = "https://api.bilibili.com"


def parse_folders(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RecallError("SOURCE_FORMAT_CHANGED", "B 站收藏夹响应缺少数据。")
    data = payload["data"]
    if "list" not in data:
        raise RecallError("SOURCE_FORMAT_CHANGED", "B 站收藏夹响应缺少列表。")
    raw = data.get("list") or []
    if not isinstance(raw, list):
        raise RecallError("SOURCE_FORMAT_CHANGED", "B 站收藏夹目录格式变化。")
    folders = []
    for item in raw:
        media_id = item.get("id")
        if media_id is None:
            continue
        folders.append(
            {
                "id": str(media_id),
                "title": str(item.get("title") or "未命名收藏夹"),
                "private": item.get("attr") is None or bool(int(item["attr"]) & 1),
            }
        )
    return folders


def parse_medias(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    data = (payload or {}).get("data") or {}
    medias = data.get("medias")
    if medias is None and "medias" in data and (data.get("info") or {}).get("media_count") == 0 and data.get("has_more") is False:
        return [], False
    if medias is None:
        raise RecallError("SOURCE_FORMAT_CHANGED", "B 站夹内响应缺少 medias。")
    if not isinstance(medias, list):
        raise RecallError("SOURCE_FORMAT_CHANGED", "B 站夹内格式变化。")
    result = []
    for media in medias:
        bvid = media.get("bvid")
        aid = media.get("aid")
        if not bvid and not aid:
            result.append({"invalid": True})
            continue
        result.append(
            {
                "key": f"bvid:{bvid}" if bvid else f"aid:{aid}",
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid
                else f"https://www.bilibili.com/video/av{aid}",
                "title": media.get("title"),
                "saved_at": media.get("fav_time"),
                "excerpt": media.get("intro"),
            }
        )
    return result, bool(data.get("has_more"))


class BilibiliFavoritesConnector(Connector):
    kind = "bilibili_favorites"
    provider = "bilibili"
    trusted = False
    refresh_mode = "poll"

    def fetch(
        self,
        mode: str = "head",
        *,
        state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> FetchResult:
        budget = dict(budget or {})
        budget.setdefault("max_requests", 40)
        result = FetchResult(coverage="authorized_subset")
        direct = bool(self.config.get("allow_direct_http")) and self.http is not None
        if not direct and self.session is None:
            require_session(self)  # 明确要求先 source connect

        def get(path: str, params: dict[str, Any] | None = None) -> Any:
            if not check_budget(result, budget):
                raise RecallError("RATE_LIMITED", "B 站请求预算用尽。")
            if direct:
                response = self.http.request(
                    "GET", BASE + path, params=params, timeout=15.0
                )
                result.requests_used += 1
                if response.status_code in (401, 403):
                    raise RecallError("AUTH_REQUIRED", "B 站登录会话失效。")
                if response.status_code >= 400:
                    raise RecallError("NETWORK_ERROR", "B 站接口不可用。")
                return response.json()
            session = require_session(self)
            result.requests_used += 1
            return session.get_json(BASE + path, params=params)

        nav = get("/x/web-interface/nav")
        nav_data = (nav or {}).get("data") or {}
        if not nav_data.get("isLogin") or not nav_data.get("mid"):
            raise RecallError("AUTH_REQUIRED", "未读到本人登录 UID。")
        identity = str(nav_data["mid"])
        bound = self.config.get("account_id")
        if bound and str(bound) != identity:
            raise RecallError("ACCOUNT_MISMATCH", "会话 UID 与已绑定来源不一致。")
        result.account_id = identity

        folders_payload = get("/x/v3/fav/folder/created/list-all", {"up_mid": identity})
        folders = parse_folders(folders_payload)
        selected = self.config.get("collection_ids")
        if selected:
            folders = [folder for folder in folders if folder["id"] in set(map(str, selected))]
        try:
            summary = json.loads((state or {}).get("last_summary") or "{}")
            checkpoint = summary.get("continuation_" + mode) or {}
        except (ValueError, TypeError, AttributeError):
            checkpoint = {}
        start_index = 0
        start_page = 1
        resumed = bool(checkpoint)
        if checkpoint:
            matches = [index for index, folder in enumerate(folders)
                       if folder["id"] == str(checkpoint.get("folder_id"))]
            if matches and isinstance(checkpoint.get("page"), int) and checkpoint["page"] > 0:
                start_index, start_page = matches[0], checkpoint["page"]
            else:
                result.warnings.append("CHECKPOINT_RESET")
                resumed = False
        max_pages = int(budget.get("max_pages", 100))
        pages = 0
        for index in range(start_index, len(folders)):
            folder = folders[index]
            page = start_page if index == start_index else 1
            while True:
                if pages >= max_pages or not check_budget(result, budget):
                    result.continuation = {"folder_id": folder["id"], "page": page}
                    break
                payload = get(
                    "/x/v3/fav/resource/list",
                    {"media_id": folder["id"], "pn": page, "ps": 20, "platform": "web"},
                )
                medias, has_more = parse_medias(payload)
                result.collection_keys.add(f"bilibili:{folder['id']}")
                for media in medias:
                    if media.get("invalid"):
                        result.warnings.append("INVALID_MEDIA")
                        continue
                    result.entries.append(
                        make_entry(
                            media["url"], title=media["title"],
                            excerpt=media["excerpt"], saved_at=media["saved_at"],
                            path=(folder["title"],), collection_key=f"bilibili:{folder['id']}", external_key=media["key"],
                            access="restricted" if folder["private"] else "standard",
                            collection_access="restricted" if folder["private"] else "standard",
                        )
                    )
                pages += 1
                if not has_more or mode == "head":
                    break
                page += 1
            if result.continuation:
                break
        result.complete = mode == "full" and result.continuation is None and not result.warnings
        result.coverage = "authorized_view" if result.complete else "authorized_subset"
        if result.continuation or (mode == "full" and resumed):
            result.warnings.append("BATCHED_SNAPSHOT")
        result.summary = f"{len(folders)} folders / {pages} pages"
        return result
