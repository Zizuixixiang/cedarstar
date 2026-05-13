"""MCP 客户端通用序列化（供 Lutopia / rcommunity 等复用）。"""

from __future__ import annotations

import json
from typing import Any, List


def mcp_call_tool_result_to_json_str(result: Any) -> str:
    """将 MCP ``CallToolResult`` 转为 JSON 字符串（供 role=tool）。"""
    texts: List[str] = []
    for block in getattr(result, "content", None) or []:
        t = getattr(block, "text", None)
        if isinstance(t, str) and t.strip():
            texts.append(t)
        else:
            texts.append(str(block))
    merged = "\n".join(texts).strip()
    sc = getattr(result, "structuredContent", None)
    if getattr(result, "isError", False):
        return json.dumps(
            {"error": merged or "MCP 工具返回错误"},
            ensure_ascii=False,
        )
    if isinstance(sc, dict) and sc:
        return json.dumps(sc, ensure_ascii=False)
    return json.dumps({"output": merged}, ensure_ascii=False)
