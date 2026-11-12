"""同步进程内使用的 HTTP 客户端（开发规格 §3 / §8）。

只放在 ``sources`` 内；查询工具、CLI 和 MCP 完全不联网。重定向逐跳核对主机。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..errors import RecallError


@dataclass
class HttpResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RecallError("SOURCE_FORMAT_CHANGED", "响应不是合法 JSON。") from exc

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


class HttpClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        raise NotImplementedError


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        host = (urllib.parse.urlsplit(newurl).hostname or "").lower()
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise RecallError("NETWORK_ERROR", "重定向到未授信主机，已中止。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibHttpClient(HttpClient):
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        allowed_hosts: tuple[str, ...] = (),
        max_retries: int = 2,
        sleeper: Any = None,
    ) -> None:
        self.timeout = timeout
        self.allowed_hosts = allowed_hosts
        self.max_retries = max_retries
        self._sleep = sleeper or time.sleep
        self._opener = urllib.request.build_opener(
            _CheckedRedirectHandler(allowed_hosts)
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise RecallError("NETWORK_ERROR", "请求主机不在授信列表内。")
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, data=None, method=method.upper(),
            headers={**(headers or {}), "User-Agent": "RecallAI/0.2"},
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener.open(request, timeout=timeout or self.timeout) as response:
                    return HttpResponse(
                        status_code=response.status,
                        headers={k: v for k, v in response.headers.items()},
                        body=response.read(),
                        url=response.geturl(),
                    )
            except urllib.error.HTTPError as exc:
                # 4xx 不重试；由连接器按状态码分类。
                return HttpResponse(
                    status_code=exc.code,
                    headers={k: v for k, v in exc.headers.items()},
                    body=exc.read(),
                    url=url,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self._sleep(0.5 * (2 ** attempt))
        raise RecallError("NETWORK_ERROR", "网络请求超时或断开。") from last_error


def next_link(header_value: str | None, trusted_host: str) -> str | None:
    """解析 Link: <...>; rel="next"，只接受同一受信 API 主机。"""
    if not header_value:
        return None
    for part in header_value.split(","):
        segments = [seg.strip() for seg in part.split(";")]
        if not segments or not segments[0].startswith("<"):
            continue
        rels = {seg.lower() for seg in segments[1:]}
        if any("rel=\"next\"" in rel or "rel=next" in rel for rel in rels):
            candidate = segments[0][1:-1]
            parsed = urllib.parse.urlsplit(candidate)
            if parsed.scheme == "https" and (parsed.hostname or "").lower() == trusted_host:
                return candidate
            raise RecallError("NETWORK_ERROR", "分页链接指向未授信主机，已中止。")
    return None
