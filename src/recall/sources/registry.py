"""来源类型 → 连接器注册（开发规格 §3）。"""
from __future__ import annotations

import json
import os
from typing import Any

from ..errors import RecallError
from .base import Connector
from .bilibili import BilibiliFavoritesConnector
from .douyin import DouyinFavoritesConnector
from .github import GitHubStarsConnector
from .rss import RssConnector
from .xiaohongshu import XiaohongshuFavoritesConnector
from .zhihu import ZhihuApiConnector
from .zhihu_web import ZhihuWebConnector
from .youtube import YouTubeFavoritesConnector

CONNECTORS: dict[str, type[Connector]] = {
    connector.kind: connector
    for connector in (
        GitHubStarsConnector,
        ZhihuApiConnector,
        ZhihuWebConnector,
        BilibiliFavoritesConnector,
        XiaohongshuFavoritesConnector,
        DouyinFavoritesConnector,
        YouTubeFavoritesConnector,
        RssConnector,
    )
}


def supported_kinds() -> frozenset[str]:
    return frozenset(CONNECTORS)


def get_connector(
    source_row: dict[str, Any],
    state: dict[str, Any],
    *,
    http: Any = None,
    session: Any = None,
    env: dict[str, str] | None = None,
) -> Connector:
    kind = source_row.get("kind")
    connector_cls = CONNECTORS.get(kind)
    if connector_cls is None:
        raise RecallError(
            "INVALID_ARGUMENT", f"该来源类型不支持自动同步：{kind}"
        )
    try:
        config = json.loads(source_row.get("config_json") or "{}")
    except (TypeError, ValueError):
        config = {}
    if state.get("account_id"):
        config.setdefault("account_id", state["account_id"])
    return connector_cls(
        source_row["source_id"], config,
        http=http, session=session, env=dict(os.environ if env is None else env),
    )
