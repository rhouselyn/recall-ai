"""搜索归一与字面匹配（开发规格 §6）。

Unicode NFKC + casefold；中文/日文字面子串；``%``、``_`` 按普通字符。
所有用户输入都通过参数化 SQL 传入，绝不拼进 SQL 或 FTS 表达式。
"""
from __future__ import annotations

import unicodedata

MAX_QUERY_CHARS = 200
MAX_QUERY_WORDS = 12
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def normalize(value: str | None) -> str:
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).casefold()


def like_literal(word: str) -> str:
    """把词转成 LIKE 字面量（配合 ESCAPE '\\' 使用）。"""
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def query_words(query: str | None) -> tuple[list[str], str]:
    """返回 (词列表, 归一后的完整查询)。"""
    if query is None:
        return [], ""
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError("query too long")
    full = normalize(query).strip()
    words = full.split()
    if len(words) > MAX_QUERY_WORDS:
        raise ValueError("too many query words")
    return words, full


def searchable(*parts: str | None) -> str:
    """写入时预先生成的匹配文本：各字段以空格分隔。"""
    return " ".join(normalize(part) for part in parts if part)
