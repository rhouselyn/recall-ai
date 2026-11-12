"""Recall AI：本地收藏目录与统一只读查询。

第一阶段只处理「自己的东西能不能找回来」：搜索只覆盖已导入或已同步的数据。
"""
from __future__ import annotations

from .errors import RecallError
from .tools import RecallTools

__version__ = "0.2.0"
__all__ = ["RecallTools", "RecallError", "__version__"]
