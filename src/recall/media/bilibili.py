"""Read Bilibili playback evidence using the bound source session."""
from __future__ import annotations

import io
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

from ..errors import RecallError
from .fetch import fetch_public
from .video import transcribe_audio

API = "https://api.bilibili.com"


def _caption_chunks(rows, language, part):
    segments = []
    for cue in rows[:20000]:
        if not isinstance(cue, dict) or not isinstance(cue.get("content"), str):
            continue
        start, end = cue.get("from"), cue.get("to")
        if not all(isinstance(value, (float, int)) and math.isfinite(value) for value in (start, end)):
            continue
        line = cue["content"].strip()
        if not line or not 0 <= start <= end:
            continue
        if segments and len(segments[-1]["text"]) + len(line) < 280 and end - segments[-1]["locator"]["start_seconds"] < 25:
            segments[-1]["text"] += " " + line
            segments[-1]["locator"]["end_seconds"] = end
        else:
            segments.append({"kind": "caption", "text": line[:900], "locator": {
                "start_seconds": start, "end_seconds": end, "video_part": part,
                "language": language, "automatic": language.startswith("ai-"),
            }})
    return segments


def archive_bilibili(session, url, account_id):
    parts = urlsplit(url)
    video = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", parts.path)
    pages = parse_qs(parts.query).get("p", ["1"])
    if parts.scheme != "https" or parts.netloc not in ("www.bilibili.com", "bilibili.com") or not video or not pages[0].isascii() or not pages[0].isdigit() or len(pages[0]) > 4:
        raise RecallError("INVALID_ARGUMENT", "不是支持的 B 站视频页链接。")
    part = int(pages[0])
    nav = session.get_json(API + "/x/web-interface/nav")["data"]
    if not nav.get("isLogin") or not nav.get("mid"):
        raise RecallError("AUTH_REQUIRED", "B 站登录过期，请重新登录。")
    if str(nav["mid"]) != account_id:
        raise RecallError("ACCOUNT_MISMATCH", "B 站登录账号与收藏来源不一致。")
    identity = {"bvid": video[1]} if video[1].startswith("BV") else {"aid": video[1][2:]}
    view = session.get_json(API + "/x/web-interface/view", params=identity)["data"]
    if any(str(view.get(key)) != value for key, value in identity.items()):
        raise RecallError("SOURCE_FORMAT_CHANGED", "B 站返回了其他视频，未归档该响应。")
    if not 1 <= part <= len(view.get("pages") or []):
        raise RecallError("NOT_FOUND", "未找到该视频分 P。")
    page = view["pages"][part - 1]
    query = {**identity, "cid": page["cid"]}
    track_info = session.get_json(API + "/x/player/v2", params=query)["data"]
    tracks = (track_info.get("subtitle") or {}).get("subtitles") or []
    tracks = sorted(
        (track for track in tracks if isinstance(track, dict) and isinstance(track.get("lan"), str) and isinstance(track.get("subtitle_url"), str)
         and track["subtitle_url"]),
        key=lambda track: ("zh" not in track["lan"], track["lan"].startswith("ai-"), track["lan"]),
    )
    last_error = None
    for track in tracks[:3]:
        endpoint = track["subtitle_url"]
        endpoint = "https:" + endpoint if endpoint.startswith("//") else endpoint
        remote = urlsplit(endpoint)
        if remote.scheme != "https" or remote.netloc not in ("aisubtitle.hdslb.com", "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com", "s1.hdslb.com"):
            continue
        output = io.BytesIO()
        try:
            fetch_public(endpoint, output, limit=2 * 1024 * 1024,
                         media_types=("application/json", "text/", "application/octet-stream"))
            rows = json.loads(output.getvalue()).get("body")
            if isinstance(rows, list):
                chunks = _caption_chunks(rows, track["lan"], part)
                if chunks:
                    return chunks, None
        except RecallError as exc:
            if exc.retryable:
                last_error = exc
        except (ValueError, AttributeError):
            continue
    duration = page.get("duration")
    if isinstance(duration, (int, float)) and 0 < duration <= 1800:
        playback = session.get_json(API + "/x/player/playurl", params={**query, "fnval": 16, "qn": 16})["data"]
        streams = (playback.get("dash") or {}).get("audio") or []
        for stream in sorted(streams, key=lambda audio: audio.get("bandwidth") or float("inf"))[:2]:
            for endpoint in [stream.get("baseUrl") or stream.get("base_url"), *((stream.get("backupUrl") or stream.get("backup_url") or [])[:1])]:
                if not endpoint or urlsplit(endpoint).scheme != "https":
                    continue
                try:
                    chunks = transcribe_audio(endpoint, "m4a", referer="https://www.bilibili.com/")
                except RecallError as exc:
                    if exc.code == "CONFIG_ERROR":
                        return [], exc.code
                    last_error = exc
                    continue
                if chunks:
                    for chunk in chunks:
                        chunk["locator"]["video_part"] = part
                    return chunks, None
    return [], last_error.code if last_error else "NO_CAPTIONS"
