"""Custom MCP server management and OpenAI tool dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from config import config
from memory.database import (
    get_mcp_server,
    list_mcp_servers,
    list_mcp_tools,
    upsert_mcp_tool_from_sync,
)
from tools.mcp_utils import mcp_call_tool_result_to_json_str

logger = logging.getLogger(__name__)

CUSTOM_MCP_CALL_TOOL_TIMEOUT_SEC = 75.0
CUSTOM_MCP_HTTP_TIMEOUT_SEC = 30.0
CUSTOM_MCP_STREAM_READ_TIMEOUT_SEC = 120.0
CUSTOM_MCP_INIT_TIMEOUT_SEC = 20.0

_MCP_FUNCTION_PREFIX = "mcp_"
_OPENAI_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _clip_log(text: str, max_len: int = 200) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _parse_headers(raw: Any) -> Optional[Dict[str, str]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("custom MCP headers 不是合法 JSON，已忽略")
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(k): str(v) for k, v in parsed.items()}


def _openai_function_name(server_id: str, tool_name: str) -> str:
    return f"{_MCP_FUNCTION_PREFIX}{server_id}_{tool_name}"


def _split_openai_function_name(tool_name: str) -> Tuple[str, str]:
    raw = (tool_name or "").strip()
    if not raw.startswith(_MCP_FUNCTION_PREFIX):
        raise ValueError(f"不是自定义 MCP 工具名: {tool_name}")
    rest = raw[len(_MCP_FUNCTION_PREFIX):]
    server_id, sep, mcp_tool_name = rest.partition("_")
    if not sep or not server_id or not mcp_tool_name:
        raise ValueError(f"自定义 MCP 工具名格式无效: {tool_name}")
    return server_id, mcp_tool_name


def is_custom_mcp_tool_name(tool_name: str) -> bool:
    return (tool_name or "").strip().startswith(_MCP_FUNCTION_PREFIX)


async def load_enabled_servers() -> List[Dict[str, Any]]:
    """Load enabled custom MCP servers from CedarStar DB."""
    return await list_mcp_servers(enabled_only=True)


def _server_trigger_keywords(server: Dict[str, Any]) -> List[str]:
    raw = server.get("trigger_keywords")
    if not raw:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(str(raw))
        except json.JSONDecodeError:
            return []
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item or "").strip()]


def _server_matches_context(
    server: Dict[str, Any],
    *,
    user_message: Optional[str],
    is_idle: bool,
) -> bool:
    if int(server.get("enabled") or 0) != 1:
        return False
    if is_idle:
        return int(server.get("allow_idle") or 0) == 1
    keywords = _server_trigger_keywords(server)
    if not keywords:
        return True
    haystack = str(user_message or "").lower()
    if not haystack:
        return False
    return any(keyword.lower() in haystack for keyword in keywords)


async def build_openai_tools(
    servers: List[Dict[str, Any]],
    user_message: Optional[str] = None,
    is_idle: bool = False,
) -> List[Dict[str, Any]]:
    """Build OpenAI function schemas for enabled tools under enabled servers."""
    if not config.ENABLE_CUSTOM_MCP:
        return []

    out: List[Dict[str, Any]] = []
    for server in servers or []:
        if not _server_matches_context(
            server,
            user_message=user_message,
            is_idle=is_idle,
        ):
            continue
        server_id = str(server.get("id") or "").strip()
        if not server_id:
            continue
        try:
            tools = await list_mcp_tools(server_id=server_id, enabled_only=True)
        except Exception as e:
            logger.warning("读取自定义 MCP 工具失败 server_id=%s: %s", server_id, e)
            continue
        for tool in tools:
            real_name = str(tool.get("name") or "").strip()
            if not real_name:
                continue
            fn_name = _openai_function_name(server_id, real_name)
            if not _OPENAI_TOOL_NAME_RE.match(fn_name):
                logger.warning("跳过不符合 OpenAI function name 约束的 MCP 工具: %s", fn_name)
                continue
            desc = str(tool.get("description") or "").strip()
            if not desc:
                desc = f"Custom MCP tool {real_name} on server {server.get('name') or server_id}."
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "description": desc,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "request": {
                                    "type": "object",
                                    "description": "Arguments passed to the MCP tool.",
                                    "additionalProperties": True,
                                },
                            },
                            "required": [],
                            "additionalProperties": True,
                        },
                    },
                }
            )
    return out


def _normalize_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {}
    elif isinstance(arguments, dict):
        parsed = arguments
    else:
        parsed = {}
    if not isinstance(parsed, dict):
        return {}
    req = parsed.get("request")
    if isinstance(req, dict):
        out = dict(req)
        for k, v in parsed.items():
            if k != "request":
                out[k] = v
        return out
    return dict(parsed)


@asynccontextmanager
async def _custom_mcp_session(server: Dict[str, Any]) -> AsyncIterator[Any]:
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    transport = str(server.get("transport") or "").strip().lower()
    url = str(server.get("url") or "").strip()
    headers = _parse_headers(server.get("headers"))
    if transport == "sse":
        async with sse_client(
            url,
            headers=headers,
            timeout=CUSTOM_MCP_HTTP_TIMEOUT_SEC,
            sse_read_timeout=CUSTOM_MCP_STREAM_READ_TIMEOUT_SEC,
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(
                    session.initialize(),
                    timeout=CUSTOM_MCP_INIT_TIMEOUT_SEC,
                )
                yield session
        return
    if transport == "streamable_http":
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=CUSTOM_MCP_HTTP_TIMEOUT_SEC,
            sse_read_timeout=CUSTOM_MCP_STREAM_READ_TIMEOUT_SEC,
            terminate_on_close=True,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(
                    session.initialize(),
                    timeout=CUSTOM_MCP_INIT_TIMEOUT_SEC,
                )
                yield session
        return
    raise ValueError(f"不支持的 MCP transport: {transport}")


async def dispatch_tool_call(tool_name: str, arguments: Any) -> str:
    """Dispatch an OpenAI custom MCP tool call and return a result string."""
    t0 = time.perf_counter()
    try:
        server_id, mcp_tool_name = _split_openai_function_name(tool_name)
        server = await get_mcp_server(server_id)
        if not server:
            return json.dumps({"error": f"未找到 MCP server: {server_id}"}, ensure_ascii=False)
        if int(server.get("enabled") or 0) != 1:
            return json.dumps({"error": f"MCP server 已禁用: {server_id}"}, ensure_ascii=False)
        args = _normalize_arguments(arguments)
        async with _custom_mcp_session(server) as session:
            result = await asyncio.wait_for(
                session.call_tool(mcp_tool_name, args),
                timeout=CUSTOM_MCP_CALL_TOOL_TIMEOUT_SEC,
            )
            ret = mcp_call_tool_result_to_json_str(result)
    except asyncio.TimeoutError:
        ret = json.dumps(
            {"error": f"自定义 MCP 在 {int(CUSTOM_MCP_CALL_TOOL_TIMEOUT_SEC)} 秒内未返回"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.warning("自定义 MCP 工具调用失败 tool=%s: %s", tool_name, e, exc_info=True)
        ret = json.dumps({"error": str(e)}, ensure_ascii=False)

    logger.info(
        "[tool] name=%s args=%s result=%s elapsed=%.2fs",
        tool_name,
        _clip_log(json.dumps(_normalize_arguments(arguments), ensure_ascii=False)),
        _clip_log(ret),
        time.perf_counter() - t0,
    )
    return ret


def _description_summary(description: Any, fallback: str) -> str:
    text = str(description or "").strip()
    if not text:
        text = str(fallback or "").strip()
    text = re.split(r"[。.!！?\n]", text, maxsplit=1)[0].strip()
    if not text:
        text = str(fallback or "").strip()
    if len(text) > 28:
        text = text[:27] + "…"
    return text


async def telegram_tool_display_label(tool_name: str) -> str:
    """User-facing Telegram status label for custom MCP tools."""
    try:
        server_id, mcp_tool_name = _split_openai_function_name(tool_name)
        server = await get_mcp_server(server_id)
        server_name = str((server or {}).get("name") or "").strip() or "自定义"
        tools = await list_mcp_tools(server_id=server_id, enabled_only=False)
        tool = next(
            (
                item
                for item in tools
                if str(item.get("name") or "").strip() == mcp_tool_name
            ),
            None,
        )
        summary = _description_summary(
            (tool or {}).get("description"),
            mcp_tool_name,
        )
        return f"已调用{server_name}MCP（{summary}）"
    except Exception as e:
        logger.debug("custom MCP Telegram 展示名生成失败 tool=%s: %s", tool_name, e)
        return f"已调用MCP工具（{tool_name[4:] if tool_name.startswith('mcp_') else tool_name}）"


def _tool_description(tool: Any) -> str:
    val = getattr(tool, "description", None)
    if val is None and isinstance(tool, dict):
        val = tool.get("description")
    return "" if val is None else str(val)


def _tool_name(tool: Any) -> str:
    val = getattr(tool, "name", None)
    if val is None and isinstance(tool, dict):
        val = tool.get("name")
    return "" if val is None else str(val)


async def sync_tools_from_server(server_id: str) -> List[Dict[str, Any]]:
    """List tools from a custom MCP server and upsert them into CedarStar DB."""
    server = await get_mcp_server(server_id)
    if not server:
        raise ValueError(f"未找到 MCP server: {server_id}")

    synced: List[Dict[str, Any]] = []
    async with _custom_mcp_session(server) as session:
        result = await asyncio.wait_for(
            session.list_tools(),
            timeout=CUSTOM_MCP_CALL_TOOL_TIMEOUT_SEC,
        )
    tools = getattr(result, "tools", result)
    for item in tools or []:
        name = _tool_name(item).strip()
        if not name:
            continue
        row = await upsert_mcp_tool_from_sync(
            server_id=server_id,
            name=name,
            description=_tool_description(item).strip() or None,
        )
        synced.append(row)
    return synced
