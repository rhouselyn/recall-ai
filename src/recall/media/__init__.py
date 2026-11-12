"""本地媒体文本提取（开发规格 §11）。

模型加载与音视频解码都在查询进程之外；查询只读已提交的片段。
"""
from .extract import index_caption, index_media, run_pending

__all__ = ["index_media", "run_pending", "index_caption"]
