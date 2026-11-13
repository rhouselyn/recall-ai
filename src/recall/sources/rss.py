"""公开 RSS 辅助发现（开发规格 §5.6）。

公开订阅单独归档；只按 GUID 或规范链接增量合并，不用于取消收藏对账。
ETag / Last-Modified 条件请求；304 只更新时间检查字段。
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry
from .http import UrllibHttpClient


def parse_feed(body: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RecallError("SOURCE_FORMAT_CHANGED", "RSS 不是合法 XML。") from exc
    items = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in node:
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name == "link":
                # Atom 用 href 属性，RSS 用文本内容。
                value = (child.text or "").strip() or (child.get("href") or "")
                if not fields.get("link"):
                    fields["link"] = value
            elif name in ("guid", "id", "title", "description", "summary"):
                fields[name] = (child.text or "").strip()
        link = fields.get("link") or fields.get("guid") or fields.get("id")
        if link:
            items.append(
                {
                    "guid": fields.get("guid") or fields.get("id") or link,
                    "url": link,
                    "title": fields.get("title"),
                    "excerpt": fields.get("description") or fields.get("summary"),
                }
            )
    return items


class RssConnector(Connector):
    kind = "rss"
    provider = "rss"
    trusted = False
    refresh_mode = "poll"

    def fetch(
        self,
        mode: str = "head",
        *,
        state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> FetchResult:
        url = self.config.get("feed_url")
        if not url:
            raise RecallError("INVALID_ARGUMENT", "RSS 来源缺少 feed_url 配置。")
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
        if not host:
            raise RecallError("INVALID_ARGUMENT", "RSS 地址无效。")
        http = self.http or UrllibHttpClient(allowed_hosts=(host,))
        state = state or {}
        headers: dict[str, str] = {}
        if state.get("last_etag"):
            headers["If-None-Match"] = state["last_etag"]
        try:
            last_modified = json.loads(state.get("last_summary") or "{}").get("text")
        except (TypeError, ValueError, AttributeError):
            last_modified = None
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        result = FetchResult(coverage="authorized_subset")
        if not check_budget(result, budget):
            raise RecallError("RATE_LIMITED", "RSS 请求预算用尽。")
        response = http.request("GET", url, headers=headers, timeout=15.0)
        result.requests_used += 1
        if response.status_code == 304:
            result.not_modified = True
            result.summary = "not_modified"
            return result
        if response.status_code == 401 or response.status_code == 403:
            raise RecallError("PERMISSION_DENIED", "RSS 拒绝访问。")
        if response.status_code >= 400:
            raise RecallError("NETWORK_ERROR", "RSS 拉取失败。")
        result.etag = response.header("ETag")
        result.summary = response.header("Last-Modified")
        for item in parse_feed(response.body):
            result.entries.append(
                make_entry(
                    item["url"], title=item["title"], excerpt=item["excerpt"],
                    path=("RSS", str(self.config.get("label") or host)),
                    external_key=f"guid:{item['guid']}",
                    capture="account_sync",
                )
            )
        result.complete = False
        return result
