"""知乎本人私密收藏夹（开发规格 §5.3 / R12）。

读取用户指定的收藏夹，或从登录后的收藏页发现可见收藏夹；
每次验证当前会话、夹归属和分页结束信号。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry, require_session, checkpoint


def parse_collection_items(payload: Any) -> tuple[list[dict[str, Any]], bool, int]:
    from . import zhihu

    if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload.get("paging"), dict):
        raise RecallError("SOURCE_FORMAT_CHANGED", "知乎收藏响应缺少数据或分页。")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise RecallError("SOURCE_FORMAT_CHANGED", "知乎网页夹内响应格式变化。")
    paging = (payload or {}).get("paging") or {}
    items = []
    for row in data:
        content = row.get("content") or row
        content_type = content.get("type") or row.get("type") or "content"
        identifier = content.get("id") or row.get("id") or content.get("url")
        url = content.get("url") or row.get("url")
        question = content.get("question") or {}
        if identifier and content_type == "answer" and question.get("id"):
            url = f"https://www.zhihu.com/question/{question['id']}/answer/{identifier}"
        elif identifier and content_type == "article":
            url = f"https://zhuanlan.zhihu.com/p/{identifier}"
        elif identifier and content_type == "question":
            url = f"https://www.zhihu.com/question/{identifier}"
        if not url:
            items.append({"invalid": True})
            continue
        items.append(
            {
                "key": f"{content_type}:{identifier}",
                "url": url,
                "title": content.get("title") or question.get("title") or row.get("title"),
                "excerpt": content.get("excerpt") or row.get("excerpt"),
                "body": zhihu.read_body(content.get("content")),
            }
        )
    is_end = bool(paging.get("is_end"))
    from urllib.parse import parse_qs, urlsplit

    next_value = paging.get("next") or paging.get("offset") or 0
    if isinstance(next_value, str) and "://" in next_value:
        next_value = parse_qs(urlsplit(next_value).query).get("offset", [0])[0]
    try:
        next_offset = int(next_value)
    except (TypeError, ValueError) as exc:
        raise RecallError("SOURCE_FORMAT_CHANGED", "知乎分页位置无效。") from exc
    return items, is_end, next_offset


class ZhihuWebConnector(Connector):
    kind = "zhihu_web"
    provider = "zhihu"
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
        me = session.get_json("/api/v4/me")
        if not isinstance(me, dict) or not me.get("id"):
            raise RecallError("AUTH_REQUIRED", "无法验证知乎登录身份。")
        valid_ids = {str(value) for value in (me.get("id"), me.get("url_token")) if value}
        if identity.get("user_id") and str(identity["user_id"]) not in valid_ids:
            raise RecallError("ACCOUNT_MISMATCH", "知乎登录账号与填写的用户 ID 不一致。")
        bound = self.config.get("account_id")
        account_id = str(me["id"])
        if bound and str(bound) != account_id:
            raise RecallError("ACCOUNT_MISMATCH", "登录账号与已绑定来源不一致。")
        collections = self.config.get("collections") or []
        discover = not collections
        if discover:
            ids = {}
            for card in session.visible_cards('a[href*="/collection/"]'):
                try:
                    parts = urlsplit(urljoin("https://www.zhihu.com", card.get("href") or ""))
                except ValueError:
                    continue
                match = re.fullmatch(r"/collection/([0-9]+)/?", parts.path)
                if parts.netloc == "www.zhihu.com" and match:
                    ids[match[1]] = {"id": match[1]}
            collections = list(ids.values())
        if not collections:
            raise RecallError(
                "PERMISSION_DENIED", "未在知乎页面找到收藏夹，可在账号设置中填写收藏夹 ID。"
            )
        budget = dict(budget or {})
        budget.setdefault("max_requests", 20)
        result = FetchResult(coverage="authorized_subset", account_id=account_id, requests_used=1)
        max_pages = 1 if mode == "head" else int(budget.get("max_pages", 50))
        pages = 0
        finished = True
        progress = checkpoint(state, mode)
        start = next((i for i, item in enumerate(collections)
                      if str(item.get("id") or item.get("url_token")) == str(progress.get("folder_id"))), 0)
        indices = ((start + offset) % len(collections) for offset in range(len(collections))) if (
            discover and mode == "head"
        ) else range(start, len(collections))
        for index in indices:
            collection = collections[index]
            cid = str(collection.get("id") or collection.get("url_token") or "")
            title = str(collection.get("title") or f"收藏夹 {cid}")
            if not cid:
                continue
            if pages >= max_pages or result.requests_used + 2 > int(budget["max_requests"]):
                result.continuation = {"folder_id": cid, "offset": progress.get("offset", 0) if index == start else 0}
                break
            metadata = session.get_json(f"/api/v4/collections/{cid}")
            result.requests_used += 1
            folder = metadata.get("collection", metadata) if isinstance(metadata, dict) else {}
            creator = folder.get("creator") or {}
            if not isinstance(creator, dict) or not creator.get("id"):
                raise RecallError("SOURCE_FORMAT_CHANGED", "无法验证知乎收藏夹所有者。")
            if str(creator["id"]) != account_id:
                if discover:
                    continue
                raise RecallError("PERMISSION_DENIED", "该知乎收藏夹不属于当前登录账号。")
            title = str(folder.get("title") or title)
            offset = int(progress.get("offset", 0)) if mode == "full" and index == start else 0
            seen: set[int] = set()
            while True:
                if offset in seen:
                    raise RecallError("SOURCE_FORMAT_CHANGED", "知乎夹内分页循环。")
                seen.add(offset)
                if pages >= max_pages or not check_budget(result, budget):
                    result.continuation = {"folder_id": cid, "offset": offset}
                    finished = False
                    break
                payload = session.get_json(
                    f"/api/v4/collections/{cid}/items",
                    params={"offset": offset, "limit": 20},
                )
                result.requests_used += 1
                items, is_end, next_offset = parse_collection_items(payload)
                for item in items:
                    if item.get("invalid"):
                        result.warnings.append("MISSING_CONTENT_URL")
                        continue
                    result.entries.append(
                        make_entry(
                            item["url"], title=item["title"], excerpt=item["excerpt"], body=item["body"],
                            path=(title,), collection_key=f"zhihu:{cid}",
                            external_key=f"collection:{cid}:{item['key']}",
                            access="restricted", collection_access="restricted",
                        )
                    )
                pages += 1
                if mode == "head":
                    following = collections[(index + 1) % len(collections)]
                    result.continuation = {"folder_id": str(following.get("id") or following.get("url_token")), "offset": 0}
                    break
                if is_end:
                    break
                if next_offset <= offset:
                    result.warnings.append("MISSING_NEXT_OFFSET")
                    finished = False
                    break
                offset = next_offset
            if result.continuation or not finished:
                break
        if discover and not pages and not result.continuation:
            raise RecallError("PERMISSION_DENIED", "未找到本人收藏夹，可在账号设置中填写收藏夹 ID。")
        result.complete = False  # 私密范围未完整枚举，保持子集标记
        result.warnings.append("PRIVATE_FAVLISTS_NOT_DISCOVERED")
        result.summary = f"{len(collections)} known collections / {pages} pages"
        return result
