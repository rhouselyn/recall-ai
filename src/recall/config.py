"""本地库路径与环境配置（开发规格 §3 / §8）。

CLI 与 MCP 使用同一个解析函数，保证指向同一个文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DB_ENV = "RECALL_DB"
ALLOWED_SOURCES_ENV = "RECALL_ALLOWED_SOURCES"
INCLUDE_RESTRICTED_ENV = "RECALL_INCLUDE_RESTRICTED"
GITHUB_TOKEN_ENV = "RECALL_GITHUB_TOKEN"
ZHIHU_SECRET_ENV = "RECALL_ZHIHU_ACCESS_SECRET"
ASR_MODEL_DIR_ENV = "RECALL_ASR_MODEL_DIR"


def default_db_path() -> Path:
    override = os.environ.get(DB_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "RecallAI" / "recall.db"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RecallAI" / "recall.db"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "recall-ai" / "recall.db"


def resolve_db_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path).expanduser() if path is not None else default_db_path()


def media_dir(db_path: str | os.PathLike[str] | None = None) -> Path:
    import hashlib
    database = resolve_db_path(db_path).resolve()
    key = hashlib.sha256(os.path.normcase(str(database)).encode()).hexdigest()
    return database.parent / "media" / key


def allowed_sources_from_env() -> frozenset[str] | None:
    """返回 None 表示未配置；空字符串视为显式配置成空集合。"""
    raw = os.environ.get(ALLOWED_SOURCES_ENV)
    if raw is None:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def include_restricted_from_env() -> bool:
    return os.environ.get(INCLUDE_RESTRICTED_ENV) == "1"
