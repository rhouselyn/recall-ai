"""Chrome / Edge Native Messaging 主机（开发规格 §5.6）。

校验来源绑定、包结构和大小；写入同一个数据库并返回版本号。
stdout 只用于协议帧；日志写 stderr，只记录来源 ID、数量和错误码。
"""
from __future__ import annotations

import json
import struct
import sys
from typing import Any

from .config import default_db_path
from .ingest import import_bookmark_tree
from .urls import MAX_FILE_BYTES

MAX_MESSAGE_BYTES = MAX_FILE_BYTES


def log(message: str) -> None:
    sys.stderr.write(f"[recall-native-host] {message}\n")
    sys.stderr.flush()


def read_message(stream: Any) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("INVALID_HEADER")
    (length,) = struct.unpack("<I", header)
    if length == 0 or length > MAX_MESSAGE_BYTES:
        raise ValueError("INVALID_SIZE")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("INVALID_BODY")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("INVALID_PAYLOAD")
    return payload


def write_message(stream: Any, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


def handle(payload: dict[str, Any], *, db_path: str | None = None, source_id: str | None = None) -> dict[str, Any]:
    target = payload.get("source_id") or source_id
    if not target:
        return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "缺少来源绑定。"}}
    try:
        response = import_bookmark_tree(db_path or str(default_db_path()), payload, target)
        log(f"source={target} ok={response.get('ok')} version={payload.get('version')}")
        return response
    except Exception as exc:  # 不把底层异常文本或收藏内容写进协议/日志
        log(f"source={target} error=INTERNAL_ERROR")
        return {
            "ok": False,
            "error": {"code": "INTERNAL_ERROR", "message": "写入失败，旧目录已保留。", "retryable": True},
        }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source_id = None
    if "--source" in args:
        index = args.index("--source")
        if index + 1 < len(args):
            source_id = args[index + 1]
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        while True:
            try:
                payload = read_message(stdin)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                log(f"error={exc}")
                break
            if payload is None:
                break
            write_message(stdout, handle(payload, source_id=source_id))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
