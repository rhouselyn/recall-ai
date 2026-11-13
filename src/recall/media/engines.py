"""OCR / ASR 引擎适配（开发规格 §11.1）。

复用本机既有模型权重，运行时只读。依赖缺失时明确报 unavailable，
不影响收藏元数据与查询链路。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import ASR_MODEL_DIR_ENV

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mkv", ".mov"})


class EngineUnavailable(RuntimeError):
    pass


def infer_kind(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "ocr"
    if suffix in AUDIO_SUFFIXES:
        return "asr"
    return None


def default_ocr(path: str | Path) -> list[dict[str, Any]]:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError as exc:
        raise EngineUnavailable("rapidocr-onnxruntime 未安装") from exc
    result, _ = RapidOCR()(str(path))
    segments: list[dict[str, Any]] = []
    for index, row in enumerate(result or []):
        try:
            box, text, confidence = row[0], row[1], row[2]
        except (IndexError, TypeError):
            continue
        segments.append(
            {
                "text": text,
                "locator": {
                    "image_no": index + 1,
                    "box": [[float(x), float(y)] for x, y in box],
                    "confidence": float(confidence),
                },
            }
        )
    return segments


def decode_audio_16k(path: str | Path) -> Any:
    """PyAV 解码并重采样为 16 kHz 单声道 float PCM。"""
    try:
        import av  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise EngineUnavailable("PyAV / numpy 未安装") from exc
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                array = resampled.to_ndarray()
                yield np.asarray(array, dtype=np.float32).reshape(-1)
        for resampled in resampler.resample(None):
            yield np.asarray(resampled.to_ndarray(), dtype=np.float32).reshape(-1)


def default_asr(path: str | Path) -> list[dict[str, Any]]:
    model_dir = os.environ.get(ASR_MODEL_DIR_ENV)
    if not model_dir or not Path(model_dir).is_dir():
        raise EngineUnavailable("RECALL_ASR_MODEL_DIR 未配置")
    try:
        import sherpa_onnx  # type: ignore
    except ImportError as exc:
        raise EngineUnavailable("sherpa-onnx 未安装") from exc
    chunks = decode_audio_16k(path)
    import numpy as np
    recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=os.path.join(model_dir, "model.int8.onnx"),
        tokens=os.path.join(model_dir, "tokens.txt"),
        num_threads=1,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
        debug=False,
    )
    window = 30 * 16000  # 长音频按 30 秒窗口分段，边界重叠 1 秒
    overlap = 16000
    segments: list[dict[str, Any]] = []
    def recognize(samples, offset):
        if len(samples) >= 1600:
            stream = recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            recognizer.decode_stream(stream)
            text = (stream.result.text or "").strip()
            if text:
                segments.append({
                    "text": text,
                    "locator": {
                        "start_seconds": round(offset / 16000, 2),
                        "end_seconds": round((offset + len(samples)) / 16000, 2),
                    },
                })
    buffer = np.empty(window, dtype=np.float32)
    used = offset = 0
    fresh = 0
    for chunk in chunks:
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        position = 0
        while position < len(chunk):
            size = min(window - used, len(chunk) - position)
            buffer[used:used + size] = chunk[position:position + size]
            used += size
            fresh += size
            position += size
            if used == window:
                recognize(buffer, offset)
                buffer[:overlap] = buffer[-overlap:]
                used = overlap
                fresh = 0
                offset += window - overlap
    if fresh:
        recognize(buffer[:used], offset)
    return segments


def engine_for(kind: str) -> Any:
    return default_ocr if kind == "ocr" else default_asr
