"""抖音本人收藏视频（开发规格 §5.5 / R14）。

直接 HTTP 存在 403 报告，因此默认从登录页面正常加载的 listcollection 响应
读取；收藏分组与收藏合集分别标记，未验证的范围显式报告为子集。
"""
from __future__ import annotations

from typing import Any

from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry, require_session

LISTCOLLECTION_API = "/aweme/v1/web/aweme/listcollection/"
COLLECTS_API = "/aweme/v1/web/collects/list/"
COLLECTS_VIDEO_API = "/aweme/v1/web/collects/video/list/"


def parse_listcollection(payload: Any) -> tuple[list[dict[str, Any]], str | None, bool]:
    if not isinstance(payload, dict) or "aweme_list" not in payload:
        raise RecallError("SOURCE_FORMAT_CHANGED", "抖音收藏响应缺少条目列表。")
    awemes = payload.get("aweme_list") or []
    if not isinstance(awemes, list):
        raise RecallError("SOURCE_FORMAT_CHANGED", "抖音收藏响应格式变化。")
    parsed = []
    for aweme in awemes:
        aweme_id = aweme.get("aweme_id")
        if not aweme_id:
            continue
        parsed.append(
            {
                "key": f"aweme:{aweme_id}",
                "url": f"https://www.douyin.com/video/{aweme_id}",
                "title": aweme.get("desc"),
                "type": aweme.get("media_type") or aweme.get("aweme_type") or "video",
            }
        )
    return parsed, str((payload or {}).get("cursor") or ""), bool((payload or {}).get("has_more"))


def parse_collects(payload: Any) -> list[dict[str, Any]]:
    items = (payload or {}).get("collects_list") or []
    if not isinstance(items, list):
        raise RecallError("SOURCE_FORMAT_CHANGED", "抖音收藏分组格式变化。")
    return [
        {"id": str(item.get("collects_id") or item.get("collects_name") or ""),
         "name": str(item.get("collects_name") or "收藏分组")}
        for item in items
        if item.get("collects_id") or item.get("collects_name")
    ]


class DouyinFavoritesConnector(Connector):
    kind = "douyin_favorites"
    provider = "douyin"
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
        if not identity.get("user_id") and not identity.get("sec_uid"):
            raise RecallError("AUTH_REQUIRED", "无法确认当前登录身份。")
        bound = self.config.get("account_id")
        if bound and str(bound) not in {str(identity.get("user_id")), str(identity.get("sec_uid"))}:
            raise RecallError("ACCOUNT_MISMATCH", "登录账号与已绑定来源不一致。")
        budget = dict(budget or {})
        budget.setdefault("max_requests", 20)
        result = FetchResult(coverage="authorized_subset",
                             account_id=str(identity.get("user_id") or identity.get("sec_uid")))
        max_pages = 1 if mode == "head" else int(budget.get("max_pages", 30))
        cursor = ""
        seen: set[str] = set()
        pages = 0
        finished = True
        while True:
            if cursor in seen:
                raise RecallError("SOURCE_FORMAT_CHANGED", "抖音收藏分页出现循环。")
            seen.add(cursor)
            if pages >= max_pages or not check_budget(result, budget):
                finished = False
                break
            payload = session.get_json(
                LISTCOLLECTION_API, params={"count": 20, "cursor": cursor}
            )
            result.requests_used += 1
            awemes, next_cursor, has_more = parse_listcollection(payload)
            for aweme in awemes:
                result.entries.append(
                    make_entry(
                        aweme["url"], title=aweme["title"], path=("收藏",), collection_key="douyin:favorites",
                        external_key=aweme["key"], note=f"type={aweme['type']}",
                        access="restricted", collection_access="restricted",
                    )
                )
            pages += 1
            if not has_more or not next_cursor:
                break
            cursor = next_cursor

        if bool(self.config.get("include_collects")):
            groups_payload = session.get_json(COLLECTS_API, params={"count": 20})
            result.requests_used += 1
            for group in parse_collects(groups_payload):
                if not check_budget(result, budget):
                    finished = False
                    break
                payload = session.get_json(
                    COLLECTS_VIDEO_API,
                    params={"collects_id": group["id"], "count": 20},
                )
                result.requests_used += 1
                awemes, _, _ = parse_listcollection(payload)
                for aweme in awemes:
                    result.entries.append(
                        make_entry(
                            aweme["url"], title=aweme["title"],
                            path=("收藏分组", group["name"]), collection_key=f"douyin:group:{group['id']}",
                            external_key=f"collects:{group['id']}:{aweme['key']}",
                            access="restricted", collection_access="restricted",
                        )
                    )
        else:
            result.warnings.append("COLLECTS_NOT_VERIFIED")
        result.complete = False  # 首屏/合集覆盖尚需真人验收，绝不据此对账
        result.summary = f"{pages} pages / {'finished' if finished else 'window'}"
        return result
