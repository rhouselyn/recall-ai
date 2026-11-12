"""平台连接器与单次同步。"""
from .registry import get_connector, supported_kinds
from .runner import sync_once

__all__ = ["get_connector", "supported_kinds", "sync_once"]
