"""链接规则、平台识别与时间规则（开发规格 §4）。

- 只收 http / https；拒绝带用户名密码的链接；凭据参数可可靠移除时保留
  可安全回跳的链接，否则由调用方排除并计数。
- 仅移除明确的跟踪参数；未知业务参数、锚点、分 P 和文章标识一律保留。
- 不联网展开短链，不因 URL 长得像就合并两篇内容。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 300
MAX_TEXT_LENGTH = 1000
MAX_ITEMS_PER_IMPORT = 20_000
MAX_FILE_BYTES = 20 * 1024 * 1024

# 仅移除明确的跟踪参数；其余查询参数保持原样（含编码形式）。
_TRACKING_EXACT = frozenset({"fbclid", "gclid", "msclkid", "igshid"})
# 已知凭据参数：出现即从回跳链接中剥离，绝不写进库。
_CREDENTIAL_PARAMS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "credential",
        "key",
        "password",
        "passwd",
        "pwd",
        "private_token",
        "secret",
        "sessid",
        "session",
        "sessionid",
        "sig",
        "signature",
        "token",
    }
)

_HOST_PLATFORM: tuple[tuple[str, str], ...] = (
    ("github.com", "github"),
    ("github.io", "web"),
    ("zhihu.com", "zhihu"),
    ("bilibili.com", "bilibili"),
    ("b23.tv", "bilibili"),
    ("acg.tv", "bilibili"),
    ("xiaohongshu.com", "xiaohongshu"),
    ("xhslink.com", "xiaohongshu"),
    ("douyin.com", "douyin"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("iesdouyin.com", "douyin"),
)


class UrlProblem(ValueError):
    """链接不可按原样保存。code 用于导入统计，不含原文。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SafeUrl:
    canonical_url: str
    link_url: str
    platform: str
    host: str
    removed: tuple[str, ...] = ()
    sanitized: bool = False


def detect_platform(host: str) -> str:
    lowered = host.lower().rstrip(".")
    for suffix, platform in _HOST_PLATFORM:
        if lowered == suffix or lowered.endswith("." + suffix):
            return platform
    return "web"


def sanitize_url(raw: str) -> SafeUrl:
    """校验并安全归一链接。不可靠时抛 UrlProblem，调用方负责计数/跳过。

    不记录原始 URL 文本；异常只带错误码。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UrlProblem("EMPTY_URL")
    raw = raw.strip()
    if len(raw) > MAX_URL_LENGTH:
        raise UrlProblem("URL_TOO_LONG")
    try:
        parts = urlsplit(raw)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise UrlProblem("MALFORMED_URL") from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlProblem("UNSUPPORTED_SCHEME")
    if not parts.netloc or not host:
        raise UrlProblem("MALFORMED_URL")
    # 用户名密码无法可靠清理：拒绝整条，提示调用方提供安全地址。
    if parts.username is not None or parts.password is not None:
        raise UrlProblem("CREDENTIALS_IN_URL")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in host):
        raise UrlProblem("MALFORMED_URL")
    if port is not None and not (0 < port < 65536):
        raise UrlProblem("MALFORMED_URL")

    removed: list[str] = []
    sanitized = False
    query = parts.query
    if query:
        original_pairs = parse_qsl(query, keep_blank_values=True)
        kept: list[tuple[str, str]] = []
        for key, value in original_pairs:
            low = key.lower()
            if low.startswith("utm_") or low in _TRACKING_EXACT:
                removed.append(key)
            elif low in _CREDENTIAL_PARAMS:
                removed.append(key)
                sanitized = True
            else:
                kept.append((key, value))
        if len(kept) != len(original_pairs):
            query = urlencode(kept, doseq=True)

    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    canonical = urlunsplit((scheme, netloc, path, query, parts.fragment))
    if len(canonical) > MAX_URL_LENGTH:
        raise UrlProblem("URL_TOO_LONG")
    return SafeUrl(
        canonical_url=canonical,
        link_url=canonical,
        platform=detect_platform(host),
        host=host,
        removed=tuple(removed),
        sanitized=sanitized,
    )


def parse_timestamp(value: object) -> datetime | None:
    """解析收藏时间。未知返回 None，非法抛 ValueError。

    接受 RFC 3339 / ISO 8601（UTC 或带时区，naive 视为 UTC）与 Unix 秒。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("INVALID_TIMESTAMP") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def saved_at_iso(value: object) -> str | None:
    from .clock import iso

    parsed = parse_timestamp(value)
    return iso(parsed) if parsed is not None else None


def stable_id(prefix: str, *parts: str) -> str:
    """本地稳定 ID：同一输入永远得到同一 ID。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def truncate_display(text: str | None, limit: int = MAX_TEXT_LENGTH) -> tuple[str | None, bool]:
    """展示文字超限时截断并报告，结构字段超限则由调用方拒绝该条。"""
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def display_title(title: str | None, url: SafeUrl | str) -> str:
    """没有标题时展示安全链接的主机和路径，不冒充平台返回的标题。"""
    if title and title.strip():
        return title.strip()
    from urllib.parse import urlsplit

    parts = urlsplit(url.link_url if isinstance(url, SafeUrl) else url)
    return f"{parts.netloc}{parts.path}"


@dataclass
class Limits:
    max_file_bytes: int = MAX_FILE_BYTES
    max_items: int = MAX_ITEMS_PER_IMPORT
    max_url: int = MAX_URL_LENGTH
    max_title: int = MAX_TITLE_LENGTH
    max_text: int = MAX_TEXT_LENGTH
    truncated_fields: list[str] = field(default_factory=list)
