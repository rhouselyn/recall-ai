"""使用本人登录的 YouTube 页面和 yt-dlp 读取播放列表。"""
from __future__ import annotations

import json
import re
from typing import Any

from ..errors import RecallError
from .base import Connector, FetchResult, make_entry, require_session


class YouTubeFavoritesConnector(Connector):
    kind = "youtube_favorites"
    provider = "youtube"
    trusted = False

    def fetch(self, mode: str = "head", *, state: dict[str, Any] | None = None,
              budget: dict[str, Any] | None = None) -> FetchResult:
        session = require_session(self)
        account, playlists = session.youtube_playlists()
        if self.config.get("account_id") and self.config["account_id"] != account:
            raise RecallError("ACCOUNT_MISMATCH", "YouTube 登录账号与已绑定来源不一致。")
        result = FetchResult(account_id=account, coverage="authorized_subset")
        try:
            summary = json.loads((state or {}).get("last_summary") or "{}")
            checkpoint = summary.get("continuation_" + mode) or {}
        except (ValueError, TypeError, AttributeError):
            checkpoint = {}
        start = next((i for i, item in enumerate(playlists)
                      if item["id"] == checkpoint.get("playlist_id")), 0)
        maximum = max(1, int((budget or {}).get("max_requests", 8)))
        failures = 0
        successes = 0
        first_error: RecallError | None = None
        for index in range(start, len(playlists)):
            playlist = playlists[index]
            if result.requests_used >= maximum:
                result.continuation = {"playlist_id": playlist["id"]}
                result.warnings.append("BATCHED_SNAPSHOT")
                break
            result.requests_used += 1
            try:
                videos = session.youtube_playlist(playlist["id"], limit=50 if mode == "head" else None)
            except RecallError as exc:
                if exc.code == "CONFIG_ERROR":
                    raise
                first_error = first_error or exc
                failures += 1
                result.warnings.append("PLAYLIST_UNAVAILABLE")
                continue
            successes += 1
            for video in videos:
                video_id = str(video.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                    continue
                result.entries.append(make_entry(
                    f"https://www.youtube.com/watch?v={video_id}", title=video.get("title"),
                    path=(playlist["title"],), collection_key=f"youtube:{playlist['id']}", external_key=f"youtube:{video_id}",
                    access="restricted", collection_access="restricted"))
        if failures and not successes and not result.continuation:
            raise first_error or RecallError("NETWORK_ERROR", "无法读取 YouTube 播放列表。")
        result.warnings.append("PLAYLIST_DISCOVERY_SUBSET")
        result.summary = f"{len(playlists)} playlists / {result.requests_used} read"
        return result
