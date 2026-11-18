"""真实 MCP stdio 协议探针（需要安装 mcp SDK）。

用法：
    python tests/mcp_stdio_probe.py

它会临时建一个库、写入样例，然后以子进程方式启动 recall MCP 服务，
按 MCP 规范完成 initialize → notifications/initialized → tools/list →
tools/call 的完整往返，并断言 structuredContent / isError / 文本兼容。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recall import catalog  # noqa: E402
from recall.db import init_db, open_readwrite  # noqa: E402
from recall.urls import sanitize_url  # noqa: E402


def build_fixture_db(path: Path) -> None:
    init_db(path)
    conn = open_readwrite(path)
    try:
        catalog.ensure_source(
            conn, "probe-src", "bookmarks_html", "chrome",
            coverage="complete_export",
        )
        catalog.merge_entries(
            conn, "probe-src",
            [
                catalog.EntryInput(
                    safe=sanitize_url("https://example.com/probe-item"),
                    title="探针样例条目",
                    body="这里是一条仅存在于归档正文里的 evidencecobalt 事实。",
                    path=("探针",),
                    saved_at="2026-10-01T00:00:00Z",
                )
            ],
        )
    finally:
        conn.close()


def send(proc: subprocess.Popen, payload: dict) -> None:
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def receive(proc: subprocess.Popen, expect_id: int) -> dict:
    while True:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("服务在响应前关闭了 stdout")
        message = json.loads(line)
        if message.get("id") == expect_id:
            return message


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="recall-probe-"))
    db_path = workdir / "recall.db"
    build_fixture_db(db_path)

    env = dict(os.environ)
    env["RECALL_DB"] = str(db_path)
    env["RECALL_ALLOWED_SOURCES"] = "probe-src"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), *(p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p)]
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "recall.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )
    failures: list[str] = []
    try:
        send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "recall-probe", "version": "0"},
            },
        })
        init = receive(proc, 1)
        server_info = init["result"].get("serverInfo", {})
        print("serverInfo:", server_info)
        if server_info.get("name") != "recall-ai":
            failures.append("serverInfo.name 不是 recall-ai")

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = receive(proc, 2)["result"]["tools"]
        names = [tool["name"] for tool in listed]
        print("tools:", names)
        expected = ["recall_list_sources", "recall_list_collections",
                    "recall_search", "recall_get_item"]
        if sorted(names) != sorted(expected):
            failures.append(f"工具集合不符: {names}")
        if not all(tool.get("inputSchema") for tool in listed):
            failures.append("工具缺少 inputSchema")
        if not all(tool.get("annotations", {}).get("readOnlyHint") for tool in listed):
            failures.append("工具缺少 readOnlyHint")

        send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "recall_search", "arguments": {"query": "evidencecobalt", "limit": 5}},
        })
        call = receive(proc, 3)["result"]
        structured = call.get("structuredContent") or call.get("structured_content") or {}
        print("search items:", len(structured.get("items", [])))
        if not structured.get("items"):
            failures.append("structuredContent 里没有命中结果")
        elif not any("evidencecobalt" in segment["text"]
                     for segment in structured["items"][0].get("snippets", [])):
            failures.append("搜索未返回归档正文片段")
        if call.get("isError"):
            failures.append("正常查询被标记为 isError")
        if not call.get("content"):
            failures.append("缺少文本兼容 content")

        send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "recall_search", "arguments": {"bogus": 1}},
        })
        bad = receive(proc, 4)["result"]
        print("invalid arg -> isError:", bad.get("isError"), "content:", bad["content"][0]["text"][:80])
        if not bad.get("isError"):
            failures.append("非法参数没有返回 isError")

        send(proc, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "recall_get_item", "arguments": {"item_id": "item_missing"}},
        })
        missing = receive(proc, 5)["result"]
        body = json.loads(missing["content"][0]["text"])
        print("not found body:", body)
        if body.get("error", {}).get("code") != "NOT_FOUND":
            failures.append("不存在的 item 没有返回 NOT_FOUND")
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr = proc.stderr.read()
        proc.stderr.close()
        if stderr.strip():
            print("--- stderr ---")
            print(stderr.strip()[:2000])

    if failures:
        print("\nFAILED:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nMCP stdio 协议往返通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
