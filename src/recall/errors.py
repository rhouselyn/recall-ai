"""统一错误形状（开发规格 §6）。

公共查询错误、同步错误的码在所有入口（Python / CLI / MCP）保持一致。
``{"error":{"code":...,"message":...,"retryable":...}}``
"""
from __future__ import annotations

from typing import Any

PUBLIC_ERROR_CODES = frozenset(
    {
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "STALE_CURSOR",
        "DB_NOT_READY",
        "DB_BUSY",
        "INTERNAL_ERROR",
    }
)
SYNC_ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "PERMISSION_DENIED",
        "ACCOUNT_MISMATCH",
        "RATE_LIMITED",
        "NETWORK_ERROR",
        "SOURCE_FORMAT_CHANGED",
        "SNAPSHOT_INCOMPLETE",
    }
)
# MCP 启动缺少开放范围属于配置错误，不属于查询工具错误。
CONFIG_ERROR_CODES = frozenset({"CONFIG_ERROR"})

ALL_ERROR_CODES = PUBLIC_ERROR_CODES | SYNC_ERROR_CODES | CONFIG_ERROR_CODES

_MESSAGES = {
    "INVALID_ARGUMENT": "参数无效。",
    "NOT_FOUND": "对象不存在或未开放。",
    "STALE_CURSOR": "目录已更新，请从第一页重新查询。",
    "DB_NOT_READY": "本地库不可用，请先执行 recall init。",
    "DB_BUSY": "本地库正忙，请稍后重试。",
    "INTERNAL_ERROR": "内部错误。",
    "AUTH_REQUIRED": "需要重新登录或提供凭据。",
    "PERMISSION_DENIED": "当前凭据权限不足。",
    "ACCOUNT_MISMATCH": "凭据对应的账号与已绑定来源不一致。",
    "RATE_LIMITED": "达到平台限流或额度上限。",
    "NETWORK_ERROR": "网络请求失败。",
    "SOURCE_FORMAT_CHANGED": "平台响应格式发生变化。",
    "SNAPSHOT_INCOMPLETE": "快照未完整拉取，本次不提交。",
    "CONFIG_ERROR": "配置缺失或无效。",
}

_RETRYABLE = frozenset(
    {"STALE_CURSOR", "DB_BUSY", "RATE_LIMITED", "NETWORK_ERROR"}
)


class RecallError(Exception):
    """所有入口共用的异常类型。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        retryable: bool | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ALL_ERROR_CODES:
            code = "INTERNAL_ERROR"
        self.code = code
        self.message = message or _MESSAGES.get(code, code)
        self.retryable = code in _RETRYABLE if retryable is None else bool(retryable)
        self.details = details or {}
        super().__init__(f"{self.code}: {self.message}")

    @property
    def body(self) -> dict[str, Any]:
        return error_body(self.code, self.message, self.retryable)

    def with_details(self, **kwargs: Any) -> "RecallError":
        return RecallError(
            self.code, self.message, self.retryable, details={**self.details, **kwargs}
        )


def error_body(
    code: str, message: str | None = None, retryable: bool | None = None
) -> dict[str, Any]:
    err = RecallError(code, message, retryable)
    return {
        "error": {"code": err.code, "message": err.message, "retryable": err.retryable}
    }


def from_exception(exc: Exception) -> RecallError:
    """把底层异常归一到公共错误码，绝不把底层文本塞进对外消息。"""
    import sqlite3

    if isinstance(exc, RecallError):
        return exc
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return RecallError("DB_BUSY")
        if "no such table" in text or "unable to open" in text or "not a database" in text:
            return RecallError("DB_NOT_READY")
    if isinstance(exc, sqlite3.DatabaseError):
        return RecallError("DB_NOT_READY")
    return RecallError("INTERNAL_ERROR")


def translate_sqlite(exc: Exception) -> RecallError:
    return from_exception(exc)
