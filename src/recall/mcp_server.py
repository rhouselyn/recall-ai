"""MCP stdio 服务（开发规格 §6 / R05）。

只暴露四个只读工具；``RECALL_ALLOWED_SOURCES`` 必须由用户明确配置，
缺省即启动失败；默认不开放 restricted。正常结果带 structuredContent，
同时保留文本结果以兼容需要文本的客户端；工具执行错误使用 isError。
"""
from __future__ import annotations

import json
import sys
from typing import Any

from .config import allowed_sources_from_env, include_restricted_from_env
from .errors import RecallError
from .tools import RecallTools

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "recall_list_sources",
        "description": "列出本地已接入的来源、覆盖范围与刷新摘要，用于确定可查范围。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": dict(READ_ONLY_ANNOTATIONS),
    },
    {
        "name": "recall_list_collections",
        "description": "列出可见收藏夹 ID、路径、来源和当前可见条目数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "annotations": dict(READ_ONLY_ANNOTATIONS),
    },
    {
        "name": "recall_search",
        "description": "在本地已入库的收藏里按关键词、来源、平台、收藏夹和时间查询。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "platform": {
                    "type": "string",
                    "enum": ["github", "zhihu", "bilibili", "xiaohongshu", "douyin", "youtube", "web"],
                },
                "collection_id": {"type": "string"},
                "saved_after": {"type": "string"},
                "saved_before": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "annotations": dict(READ_ONLY_ANNOTATIONS),
    },
    {
        "name": "recall_get_item",
        "description": "查看某个内容项的详情与全部可见来源证据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "cursor": {"type": "string"},
            },
            "required": ["item_id"],
            "additionalProperties": False,
        },
        "annotations": dict(READ_ONLY_ANNOTATIONS),
    },
]

ALLOWED_ARGUMENTS = {
    definition["name"]: set(definition["inputSchema"]["properties"])
    for definition in TOOL_DEFINITIONS
}
REQUIRED_ARGUMENTS = {
    definition["name"]: set(definition["inputSchema"].get("required", []))
    for definition in TOOL_DEFINITIONS
}


def tools_from_env() -> RecallTools:
    allowed = allowed_sources_from_env()
    if allowed is None:
        raise RecallError(
            "CONFIG_ERROR", "RECALL_ALLOWED_SOURCES 必须由用户明确配置后才能启动 MCP。"
        )
    return RecallTools.open(
        None, allowed, include_restricted=include_restricted_from_env()
    )


def handle_tool_call(
    tools: RecallTools, name: str, arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """纯逻辑入口，可在不安装 SDK 的情况下测试。"""
    args = arguments or {}
    try:
        if name not in ALLOWED_ARGUMENTS:
            raise RecallError("INVALID_ARGUMENT", "未知工具。")
        if not isinstance(args, dict):
            raise RecallError("INVALID_ARGUMENT", "参数必须是对象。")
        unknown = set(args) - ALLOWED_ARGUMENTS[name]
        missing = REQUIRED_ARGUMENTS[name] - set(args)
        if unknown or missing:
            raise RecallError("INVALID_ARGUMENT", "工具参数无效。")
        result = getattr(tools, name)(**args)
        text = json.dumps(result, ensure_ascii=False)
        return {"structuredContent": result, "text": text, "isError": False}
    except RecallError as exc:
        body = exc.body
        return {
            "structuredContent": body,
            "text": json.dumps(body, ensure_ascii=False),
            "isError": True,
        }
    except Exception:
        body = RecallError("INTERNAL_ERROR").body
        return {
            "structuredContent": body,
            "text": json.dumps(body, ensure_ascii=False),
            "isError": True,
        }


def _tool_models(types: Any) -> list[Any]:
    fields = getattr(types.Tool, "model_fields", {})
    schema_key = "input_schema" if "input_schema" in fields else "inputSchema"
    result = []
    for definition in TOOL_DEFINITIONS:
        payload: dict[str, Any] = {
            "name": definition["name"],
            "description": definition["description"],
            schema_key: definition["inputSchema"],
        }
        if "annotations" in fields or "annotations" in getattr(types.Tool, "__fields__", {}):
            payload["annotations"] = definition["annotations"]
        result.append(types.Tool(**payload))
    return result


def _call_result(types: Any, outcome: dict[str, Any]) -> Any:
    fields = getattr(types.CallToolResult, "model_fields", {})
    payload: dict[str, Any] = {
        "content": [types.TextContent(type="text", text=outcome["text"])]
    }
    if "structured_content" in fields:
        payload["structured_content"] = outcome["structuredContent"]
    elif "structuredContent" in fields:
        payload["structuredContent"] = outcome["structuredContent"]
    if "is_error" in fields:
        payload["is_error"] = outcome["isError"]
    elif "isError" in fields:
        payload["isError"] = outcome["isError"]
    return types.CallToolResult(**payload)


def build_server(tools: RecallTools) -> Any:
    """构造官方 SDK 的 low-level Server；协议层无效请求由 SDK 处理。

    兼容官方 SDK v1（装饰器）与 v2（构造器回调）两条稳定线。
    """
    import inspect

    try:
        from mcp.server.lowlevel import Server  # type: ignore
    except ImportError as exc:
        raise RecallError(
            "CONFIG_ERROR", "缺少 MCP SDK：python -m pip install -e '.[mcp]'"
        ) from exc
    try:
        import mcp_types as types  # type: ignore  # SDK v2 独立协议类型包
    except ImportError:
        from mcp import types  # type: ignore  # SDK v1

    if "on_list_tools" in inspect.signature(Server.__init__).parameters:
        # SDK v2：handler 接收 (ctx, params)，直接返回结果模型。
        async def list_tools_v2(ctx: Any, params: Any) -> Any:
            return types.ListToolsResult(tools=_tool_models(types))

        async def call_tool_v2(ctx: Any, params: Any) -> Any:
            return _call_result(
                types, handle_tool_call(tools, params.name, params.arguments or {})
            )

        return Server(
            "recall-ai", version="0.2.0",
            on_list_tools=list_tools_v2, on_call_tool=call_tool_v2,
        )

    # SDK v1：装饰器注册，返回内容列表。
    server = Server("recall-ai")

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return _tool_models(types)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        outcome = handle_tool_call(tools, name, arguments or {})
        return [types.TextContent(type="text", text=outcome["text"])]

    return server


def main() -> int:
    try:
        tools = tools_from_env()
    except RecallError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    try:
        import anyio  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore

        server = build_server(tools)

        async def serve() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream, write_stream, server.create_initialization_options()
                )

        anyio.run(serve)
        return 0
    except ImportError as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        tools.close()


if __name__ == "__main__":
    raise SystemExit(main())
