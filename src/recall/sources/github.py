"""GitHub 本人 Stars（开发规格 §5.2 / R06）。

GET /user 绑定稳定用户 ID；GET /user/starred 串行分页；完整拉取并校验后
由 runner 原子提交快照。凭据只从 RECALL_GITHUB_TOKEN 读取。
"""
from __future__ import annotations

from typing import Any

from ..errors import RecallError
from .base import Connector, FetchResult, check_budget, make_entry
from .http import HttpResponse, UrllibHttpClient, next_link

API_HOST = "api.github.com"
BASE = f"https://{API_HOST}"
MAX_PAGES = 100
MAX_ITEMS = 20_000


class GitHubStarsConnector(Connector):
    kind = "github_stars"
    provider = "github"
    trusted = True

    def fetch(
        self,
        mode: str = "head",
        *,
        state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> FetchResult:
        token = self.env.get("RECALL_GITHUB_TOKEN")
        if not token:
            raise RecallError("AUTH_REQUIRED", "同步进程缺少 RECALL_GITHUB_TOKEN。")
        http = self.http or UrllibHttpClient(allowed_hosts=(API_HOST,))
        state = state or {}
        result = FetchResult(coverage="authorized_subset")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.star+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }

        user_response = self._get(http, f"{BASE}/user", headers, result)
        self._classify(user_response, check_304=False)
        user = user_response.json()
        account_id = user.get("id")
        if not isinstance(account_id, int):
            raise RecallError("SOURCE_FORMAT_CHANGED", "无法读取稳定的 GitHub 用户 ID。")
        result.account_id = str(account_id)
        bound = self.config.get("account_id") or state.get("account_id")
        if bound and str(bound) != str(account_id):
            raise RecallError("ACCOUNT_MISMATCH", "令牌对应的账号与已绑定来源不一致。")

        page_headers = dict(headers)
        etag = state.get("last_etag")
        if etag and mode == "head":
            page_headers["If-None-Match"] = etag
        url: str | None = f"{BASE}/user/starred?per_page=100&page=1"
        max_pages = 1 if mode == "head" else int((budget or {}).get("max_pages", MAX_PAGES))
        seen_urls: set[str] = set()
        pages = 0
        while url:
            if url in seen_urls:
                raise RecallError("SOURCE_FORMAT_CHANGED", "分页出现循环。")
            seen_urls.add(url)
            if pages >= max_pages:
                if mode == "full":
                    raise RecallError("SNAPSHOT_INCOMPLETE", "超过最大页数，未提交半份快照。")
                break
            response = self._get(http, url, page_headers, result)
            if response.status_code == 304:
                result.not_modified = True
                result.summary = "not_modified"
                return result
            self._classify(response)
            data = response.json()
            if not isinstance(data, list):
                raise RecallError("SOURCE_FORMAT_CHANGED", "Stars 响应不是列表。")
            for row in data:
                if not isinstance(row, dict) or "repo" not in row or "starred_at" not in row:
                    raise RecallError("SOURCE_FORMAT_CHANGED", "Stars 响应缺少白名单字段。")
                repo = row["repo"]
                if not isinstance(repo, dict) or "id" not in repo or "html_url" not in repo:
                    raise RecallError("SOURCE_FORMAT_CHANGED", "仓库字段不完整。")
                rid = repo["id"]
                private = bool(repo.get("private", False))
                result.entries.append(
                    make_entry(
                        repo["html_url"],
                        title=repo.get("full_name"),
                        excerpt=repo.get("description"),
                        saved_at=row.get("starred_at"),
                        path=(),
                        external_key=f"repo:{rid}",
                        access="restricted" if private else "standard",
                        capture="account_sync",
                    )
                )
                if len(result.entries) > MAX_ITEMS:
                    raise RecallError("SNAPSHOT_INCOMPLETE", "Stars 超过单次条目上限。")
            result.etag = response.header("ETag") or result.etag
            pages += 1
            url = next_link(response.header("Link"), API_HOST)
            if not check_budget(result, budget):
                break
        result.complete = mode == "full" and url is None
        result.coverage = "authorized_view" if result.complete else "authorized_subset"
        result.summary = f"{len(result.entries)} stars / {pages} pages"
        return result

    def _get(self, http: Any, url: str, headers: dict[str, str], result: FetchResult) -> HttpResponse:
        last: HttpResponse | None = None
        for attempt in range(3):
            response = http.request("GET", url, headers=headers, timeout=15.0)
            result.requests_used += 1
            last = response
            if response.status_code < 500 or attempt == 2:
                return response
        assert last is not None
        return last

    @staticmethod
    def _classify(response: HttpResponse, *, check_304: bool = True) -> None:
        status = response.status_code
        if check_304 and status == 304:
            return
        if status == 401:
            raise RecallError("AUTH_REQUIRED", "GitHub 凭据无效或已过期。")
        if status == 429 or (
            status == 403
            and (response.header("Retry-After") or response.header("X-RateLimit-Remaining") == "0")
        ):
            raise RecallError("RATE_LIMITED", "GitHub 限流，按返回的重试时间退避。")
        if status == 403:
            raise RecallError("PERMISSION_DENIED", "GitHub 令牌权限不足。")
        if status >= 500:
            raise RecallError("NETWORK_ERROR", "GitHub 服务暂时不可用。")
        if status >= 400:
            raise RecallError("PERMISSION_DENIED", "GitHub 请求被拒绝。")
