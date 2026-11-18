"""Read metadata for a saved video link without downloading media."""
from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import RecallError
from .fetch import fetch_public
from .engines import EngineUnavailable, default_asr
from .tasks import text_chunks

EXTRACTORS = {
    "www.youtube.com": "Youtube", "youtube.com": "Youtube", "m.youtube.com": "Youtube",
    "youtu.be": "Youtube", "www.bilibili.com": "BiliBili", "bilibili.com": "BiliBili",
    "www.douyin.com": "Douyin", "www.iesdouyin.com": "Douyin",
    "www.xiaohongshu.com": "XiaoHongShu",
}


def _video_info(url: str) -> dict:
    parts = urlsplit(url)
    extractor = EXTRACTORS.get(parts.netloc.lower())
    if parts.scheme != "https" or not extractor:
        raise RecallError("INVALID_ARGUMENT", "请使用支持平台的 HTTPS 视频原始链接。")
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise RecallError("CONFIG_ERROR", "视频信息提取需要安装：python -m pip install -e '.[youtube]'。") from exc

    logger = logging.Logger(__name__)
    logger.addHandler(logging.NullHandler())
    options = {
        "allowed_extractors": [f"(?i){extractor}"], "cachedir": False,
        "extract_flat": "in_playlist", "noplaylist": True, "playlistend": 1,
        "skip_download": True, "ignore_no_formats_error": True, "socket_timeout": 15,
        "retries": 1, "logger": logger, "writesubtitles": True, "writeautomaticsub": True, "simulate": True,
        "js_runtimes": {"deno": {}, "node": {}}, "enable_file_urls": False,
    }
    try:
        with YoutubeDL(options) as downloader:
            if not downloader.get_info_extractor(extractor).suitable(url):
                raise RecallError("INVALID_ARGUMENT", "该链接不是支持的单条视频地址。")
            info = downloader.extract_info(url, download=False, ie_key=extractor)
    except DownloadError as exc:
        raise RecallError("NETWORK_ERROR", "无法读取视频信息；请检查链接、网络或平台访问限制。") from exc
    if not isinstance(info, dict) or info.get("_type", "video") != "video" or not info.get("title"):
        raise RecallError("SOURCE_FORMAT_CHANGED", "未找到可用的单条视频信息。")
    extractor_name = str(info.get("extractor") or "").replace(":", "-")
    if info["title"] == f"{extractor_name} video #{info.get('id')}":
        raise RecallError("NETWORK_ERROR", "平台没有返回视频标题，可能限制了匿名访问。")
    return info


def extract_video_info(url: str) -> dict:
    info = _video_info(url)
    return {
        "title": str(info["title"])[:300],
        "description": str(info.get("description") or "")[:4000],
        "uploader": str(info.get("uploader") or info.get("channel") or "")[:300],
        "duration": info.get("duration"),
        "subtitles": sorted((info.get("subtitles") or {}).keys()),
        "automatic_captions": sorted((info.get("automatic_captions") or {}).keys()),
    }


def _caption_segments(info):
    import webvtt

    retry_error = None
    for field in ("subtitles", "automatic_captions"):
        tracks = info.get(field) or {}
        languages = sorted(tracks, key=lambda lang: (
            lang not in (info.get("language"), str(info.get("language")) + "-orig"),
            not lang.endswith("-orig"), not lang.startswith("zh"), not lang.startswith("en"), lang,
        ))
        available = [(language, track) for language in languages for track in tracks[language]
                     if track.get("ext") in ("vtt", "srt") and (track.get("url") or track.get("data"))]
        for language, track in available[:3]:
            try:
                if track.get("data"):
                    raw = track["data"].encode("utf-8")
                    if len(raw) > 2 * 1024 * 1024:
                        continue
                else:
                    output = io.BytesIO()
                    fetch_public(track["url"], output, limit=2 * 1024 * 1024, media_types=(
                        "text/", "application/octet-stream", "application/x-subrip",
                    ))
                    raw = output.getvalue()
                parsed = webvtt.from_buffer(io.StringIO(raw.decode("utf-8-sig")), format=track["ext"])
            except RecallError as exc:
                if exc.retryable:
                    retry_error = exc
                continue
            except ValueError:
                continue
            segments = []
            previous = ""
            for cue in parsed:
                line = " ".join(cue.text.split())
                if not line or line == previous:
                    continue
                previous = line
                start = cue.start_time
                second = start.hours * 3600 + start.minutes * 60 + start.seconds + start.milliseconds / 1000
                if segments and len(segments[-1]["text"]) + len(line) < 700:
                    segments[-1]["text"] += " " + line
                else:
                    segments.append({"kind": "caption", "text": line[:900], "locator": {
                        "start_seconds": second, "language": language, "automatic": field == "automatic_captions",
                    }})
            if segments:
                return segments[:512]
    if retry_error:
        raise retry_error
    return []


def transcribe_audio(url, extension, *, referer=None):
    try:
        with tempfile.TemporaryDirectory(prefix="recall-audio-") as directory:
            path = Path(directory) / ("audio." + extension)
            with path.open("wb") as output:
                fetch_public(url, output, limit=32 * 1024 * 1024, referer=referer,
                             media_types=("audio/", "video/webm", "video/mp4", "application/mp4",
                                          "application/octet-stream"))
            return [{**segment, "kind": "asr"} for segment in default_asr(path, max_seconds=1800)]
    except EngineUnavailable as exc:
        raise RecallError("CONFIG_ERROR", "未找到本地转写模型或依赖。") from exc
    except (OSError, ValueError) as exc:
        raise RecallError("SOURCE_FORMAT_CHANGED", "无法解码视频音轨。") from exc


def archive_video(url):
    info = _video_info(url)
    description = text_chunks(str(info.get("description") or ""), "description")
    try:
        segments = _caption_segments(info)
    except RecallError as exc:
        if exc.retryable:
            return description, exc.code
        segments = []
    if segments:
        return [*segments, *description], None
    if os.environ.get("RECALL_ARCHIVE_ASR") == "1":
        duration = info.get("duration")
        audio = min((stream for stream in info.get("formats", [])
                     if stream.get("acodec") not in (None, "none")
                     and stream.get("ext") in ("m4a", "webm", "mp4") and stream.get("protocol") == "https"
                     and not stream.get("has_drm") and stream.get("url")),
                    key=lambda stream: (stream.get("vcodec") != "none", stream.get("tbr") or 128), default=None)
        if audio and isinstance(duration, (int, float)) and 0 < duration <= 1800:
            try:
                segments = transcribe_audio(audio["url"], audio["ext"])
                if segments:
                    return [*segments, *description], None
            except RecallError as exc:
                if exc.retryable or exc.code == "CONFIG_ERROR":
                    return description, exc.code
    return description, "NO_CAPTIONS"
