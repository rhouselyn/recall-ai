"""知乎本人公开范围收藏（开发规格 §5.3 / R07）。

Access Secret 只由同步进程读取；除 HTTP 状态外检查响应 Code；按配额预算分页，
范围始终标为 authorized_subset，私密夹由 zhihu_web 或手动导入补充。
"""
from __future__ import annotations

from typing import Any

from ..clock import now
from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry, checkpoint
from .http import UrllibHttpClient

DEFAULT_BASE = "https://api.zhihu.com"


def _check_code(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise RecallError("SOURCE_FORMAT_CHANGED", "知乎响应不是对象。")
    code = payload.get("Code")
    if code in (0, None, 200, "0"):
        return
    if code == 20001:
        raise RecallError("AUTH_REQUIRED", "知乎 Access Secret 鉴权失败。")
    if code == 30001:
        raise RecallError("RATE_LIMITED", "知乎接口频率限制。")
    if code == 30002:
        raise RecallError("PERMISSION_DENIED", "知乎每日配额耗尽。")
    raise RecallError("SOURCE_FORMAT_CHANGED", "知乎返回未知业务状态码。")


def content_fields(item: dict[str, Any]) -> dict[str, Any]:
    """从官方文档允许的字段里提取需要的内容；不保存完整响应。"""
    content = item.get("Content") if isinstance(item.get("Content"), dict) else item
    url = content.get("Url") or content.get("url") or item.get("Url") or item.get("url")
    title = content.get("Title") or content.get("title") or item.get("Title")
    identifier = (
        content.get("Id") or content.get("id") or item.get("Id")
        or item.get("ContentId") or url
    )
    return {
        "url": url, "title": title, "id": identifier,
        "saved_at": item.get("FavTime") or item.get("fav_time"),
        "excerpt": content.get("Description") or content.get("Excerpt"),
    }


class ZhihuApiConnector(Connector):
    kind = "zhihu_api"
    provider = "zhihu"
    trusted = False

    def fetch(
        self,
        mode: str = "head",
        *,
        state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> FetchResult:
        secret = self.env.get("RECALL_ZHIHU_ACCESS_SECRET")
        if not secret:
            raise RecallError("AUTH_REQUIRED", "同步进程缺少 RECALL_ZHIHU_ACCESS_SECRET。")
        base = self.config.get("api_base", DEFAULT_BASE)
        host = base.split("//", 1)[-1].split("/", 1)[0]
        http = self.http or UrllibHttpClient(allowed_hosts=(host,))
        budget = dict(budget or {})
        budget.setdefault("max_requests", 20)
        max_pages = 1 if mode == "head" else int(budget.get("max_pages", 50))
        result = FetchResult(coverage="authorized_subset")
        headers = {
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(now().timestamp())),
            "Accept": "application/json",
        }

        def get(path: str, params: dict[str, Any]) -> dict[str, Any]:
            if not check_budget(result, budget):
                raise RecallError("RATE_LIMITED", "本次知乎请求预算已用尽。")
            response = http.request("GET", base + path, headers=headers, params=params, timeout=15.0)
            result.requests_used += 1
            if response.status_code == 401:
                raise RecallError("AUTH_REQUIRED", "知乎鉴权失败。")
            if response.status_code in (403, 429):
                raise RecallError("RATE_LIMITED", "知乎限流。")
            if response.status_code >= 400:
                raise RecallError("NETWORK_ERROR", "知乎接口不可用。")
            payload = response.json()
            _check_code(payload)
            return payload

        lists_payload = get("/api/v1/user/favlists", {"Limit": 100})
        data = lists_payload.get("Data") or {}
        favlists = data.get("Items") or []
        if not isinstance(favlists, list):
            raise RecallError("SOURCE_FORMAT_CHANGED", "收藏夹列表格式变化。")
        progress = checkpoint(state, mode)
        start = next((i for i, item in enumerate(favlists)
                      if str(item.get("UrlToken")) == str(progress.get("folder_id"))), 0)
        pages_used = 0
        finished = True
        for index in range(start, len(favlists)):
            favlist = favlists[index]
            token = favlist.get("UrlToken")
            title = favlist.get("Title") or "知乎收藏夹"
            if not token:
                result.warnings.append("MISSING_URLTOKEN")
                finished = False
                continue
            offset = progress.get("offset", 0) if mode == "full" and index == start else 0
            seen_offsets: set[str] = set()
            while True:
                if str(offset) in seen_offsets:
                    raise RecallError("SOURCE_FORMAT_CHANGED", "夹内分页出现循环。")
                seen_offsets.add(str(offset))
                if pages_used >= max_pages or not check_budget(result, budget):
                    result.continuation = {"folder_id": str(token), "offset": offset}
                    finished = False
                    break
                payload = get(
                    "/api/v1/user/favlist_contents",
                    {"FavlistUrlToken": token, "Offset": offset, "Limit": 20},
                )
                pages_used += 1
                payload_data = payload.get("Data") or {}
                contents = payload_data.get("Items") or []
                if not isinstance(contents, list):
                    raise RecallError("SOURCE_FORMAT_CHANGED", "夹内内容格式变化。")
                for item in contents:
                    fields = content_fields(item)
                    if not fields["url"]:
                        result.warnings.append("MISSING_CONTENT_URL")
                        continue
                    result.entries.append(
                        make_entry(
                            fields["url"], title=fields["title"],
                            excerpt=fields["excerpt"], saved_at=fields["saved_at"],
                            path=(str(title),), collection_key=f"zhihu:{token}",
                            external_key=f"fav:{token}:{fields['id']}",
                        )
                    )
                paging = payload_data.get("Paging") or {}
                if mode == "head":
                    following = favlists[(index + 1) % len(favlists)]
                    result.continuation = {"folder_id": str(following.get("UrlToken")), "offset": 0}
                    break
                if paging.get("IsEnd"):
                    break
                next_offset = paging.get("NextOffset")
                if next_offset in (None, "", offset):
                    result.warnings.append("MISSING_NEXT_OFFSET")
                    finished = False
                    break
                offset = next_offset
            if result.continuation or not finished:
                break
        result.complete = False  # 官方接口只覆盖公开范围，绝不据此对账
        result.coverage = "authorized_subset"
        result.summary = f"{len(favlists)} favlists / {pages_used} pages"
        return result
