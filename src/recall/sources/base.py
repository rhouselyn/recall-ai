"""连接器公共接口（开发规格 §5）。

所有网络读取都发生在写事务之外；连接器只产出 ``EntryInput``，不碰数据库。
凭据只由同步进程从环境变量读取，不进命令参数、配置、SQLite 或日志。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

from .. import catalog
from ..errors import RecallError
from ..urls import SafeUrl, sanitize_url, saved_at_iso


@dataclass
class FetchResult:
    entries: list[catalog.EntryInput] = field(default_factory=list)
    coverage: str = "authorized_subset"
    complete: bool = False  # 是否走完已声明范围并拿到明确末页信号
    account_id: str | None = None
    etag: str | None = None
    summary: str | None = None
    warnings: list[str] = field(default_factory=list)
    requests_used: int = 0
    not_modified: bool = False  # 304：只更新时间检查字段
    continuation: dict[str, Any] | None = None
    collection_keys: set[str] = field(default_factory=set)


def checkpoint(state: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    try:
        value = json.loads((state or {}).get("last_summary") or "{}").get("continuation_" + mode)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


class BrowserSession(Protocol):
    """登录网页连接器共用的浏览器读取机制（由可选 Playwright 实现）。"""

    def identity(self) -> dict[str, Any]: ...

    def status(self) -> str: ...

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any: ...

    def visible_cards(self, selector: str) -> list[dict[str, Any]]: ...


class Connector:
    kind = ""
    provider = ""
    refresh_mode = "poll"
    trusted = False  # 官方接口走完整对账；第三方来源需两次全量缺失才标不可见

    def __init__(
        self,
        source_id: str,
        config: dict[str, Any] | None = None,
        *,
        http: Any = None,
        session: BrowserSession | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.source_id = source_id
        self.config = config or {}
        self.http = http
        self.session = session
        self.env = env or {}

    def fetch(
        self,
        mode: str = "head",
        *,
        state: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> FetchResult:
        raise NotImplementedError


def make_entry(
    raw_url: str,
    *,
    title: str | None = None,
    note: str | None = None,
    excerpt: str | None = None,
    saved_at: Any = None,
    path: tuple[str, ...] = (),
    external_key: str | None = None,
    access: str = "standard",
    collection_access: str = "standard",
    capture: str = "account_sync",
    collection_key: str | None = None,
    body: str | None = None,
) -> catalog.EntryInput:
    safe: SafeUrl = sanitize_url(raw_url)
    return catalog.EntryInput(
        safe=safe,
        title=title,
        note=note,
        excerpt=excerpt,
        saved_at=saved_at_iso(saved_at) if saved_at is not None else None,
        path=path,
        external_key=external_key or safe.canonical_url,
        access=access,
        collection_access=collection_access,
        capture=capture,
        collection_key=collection_key,
        body=body,
    )


def require_session(connector: Connector) -> BrowserSession:
    if connector.session is None:
        raise RecallError(
            "AUTH_REQUIRED", "该来源需要先执行 recall source connect 完成本人登录。"
        )
    status = connector.session.status()
    if status in ("login_required", "expired"):
        raise RecallError("AUTH_REQUIRED", "登录会话已失效，请重新连接。")
    if status in ("captcha", "challenge"):
        raise RecallError("PERMISSION_DENIED", "页面出现验证或风控，已暂停并保留旧结果。")
    if status == "changed":
        raise RecallError("SOURCE_FORMAT_CHANGED", "页面结构异常，已暂停并保留旧结果。")
    return connector.session


def check_budget(result: FetchResult, budget: dict[str, Any] | None, key: str = "max_requests") -> bool:
    if not budget:
        return True
    maximum = budget.get(key)
    if maximum is None:
        return True
    if result.requests_used >= int(maximum):
        result.warnings.append("BUDGET_EXHAUSTED")
        return False
    return True
