"""可注入时钟。

对外时间统一 UTC RFC 3339（``...Z``）。测试与 watch 的固定时钟模拟通过
``freeze`` / ``set_clock`` 注入，便于验证调度而不依赖真实睡眠。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]

_clock: Clock | None = None
_frozen: datetime | None = None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def now() -> datetime:
    if _frozen is not None:
        return _frozen
    if _clock is not None:
        return _ensure_aware(_clock())
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return iso(now())


def iso(value: datetime) -> str:
    """秒精度 UTC RFC 3339。"""
    return _ensure_aware(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def set_clock(fn: Clock | None) -> None:
    global _clock, _frozen
    _clock = fn
    _frozen = None


def freeze(value: datetime | str | None) -> None:
    """冻结时钟；传 None 恢复真实时间。字符串按 UTC 或带时区解析。"""
    global _clock, _frozen
    if value is None:
        _frozen = None
        _clock = None
    elif isinstance(value, datetime):
        _frozen = _ensure_aware(value)
    else:
        from .urls import parse_timestamp

        parsed = parse_timestamp(value)
        if parsed is None:
            raise ValueError("invalid clock value")
        _frozen = _ensure_aware(parsed)


def advance(seconds: float) -> datetime:
    """仅对冻结时钟有效：前进并返回当前时间。"""
    from datetime import timedelta

    global _frozen
    if _frozen is None:
        raise RuntimeError("clock is not frozen")
    _frozen = _frozen + timedelta(seconds=seconds)
    return _frozen
