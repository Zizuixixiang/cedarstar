"""
rcommunity 论坛：经站方 MCP（SSE，token 在 URL query）调用 ``forum`` / ``forum_write`` /
``forum_interact`` / ``chat`` / ``profile`` 五类工具。

鉴权：环境变量 ``RCOMMUNITY_MCP_TOKEN``（见 ``config.py``）；连接 URL 为
``{base}?token=...``，不在 HTTP header 或 MCP 参数中重复注入 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from config import config
from tools.mcp_utils import mcp_call_tool_result_to_json_str

logger = logging.getLogger(__name__)

# ``call_tool`` 在站方处理慢、卡读流或空参导致服务端挂起时可能永不返回，会拖死整轮对话（事件循环）。
RCOMMUNITY_CALL_TOOL_TIMEOUT_SEC = 75.0

TOOL_LOG_SNIP_MAX = 200


def _clip_log(text: str, max_len: int = TOOL_LOG_SNIP_MAX) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _rcommunity_base_url() -> str:
    raw = (os.getenv("RCOMMUNITY_MCP_BASE_URL") or "").strip()
    if raw:
        return raw.rstrip("/")
    return (config.RCOMMUNITY_MCP_BASE_URL or "").strip().rstrip("/")


def get_rcommunity_token() -> Optional[str]:
    """从环境变量读取 token（与 .env 中 ``RCOMMUNITY_MCP_TOKEN`` 一致）。"""
    t = (os.getenv("RCOMMUNITY_MCP_TOKEN") or "").strip()
    if t:
        return t
    return (config.RCOMMUNITY_MCP_TOKEN or "").strip() or None


def rcommunity_sse_url() -> Optional[str]:
    tok = get_rcommunity_token()
    if not tok:
        return None
    return f"{_rcommunity_base_url()}?token={tok}"


# OpenAI function name → MCP 工具名（站方 5 工具）
RCOMMUNITY_OPENAI_TO_MCP: Dict[str, str] = {
    "rcommunity_forum": "forum",
    "rcommunity_forum_write": "forum_write",
    "rcommunity_forum_interact": "forum_interact",
    "rcommunity_chat": "chat",
    "rcommunity_profile": "profile",
}

RCOMMUNITY_OPENAI_TOOL_NAMES: Set[str] = set(RCOMMUNITY_OPENAI_TO_MCP.keys())


def is_rcommunity_openai_tool(name: str) -> bool:
    return (name or "").strip() in RCOMMUNITY_OPENAI_TOOL_NAMES


def _rcommunity_tool_parameters_schema(
    *,
    description: str,
) -> Dict[str, Any]:
    """
    各厂商 OpenAI 兼容层（Vertex/Gemini、智谱 GLM、硅基流动等）对 ``properties: {}`` 且
    仅依赖 ``additionalProperties`` 的 function schema 常校验失败或首包极慢。显式声明
    ``request`` 对象，并对其开启 ``additionalProperties``，便于任意 MCP 键名。

    执行时在 :func:`_normalize_rcommunity_openai_args` 中与顶层平铺参数对齐。
    """
    return {
        "type": "object",
        "properties": {
            "request": {
                "type": "object",
                "description": description,
                "additionalProperties": True,
            },
        },
        "required": [],
    }


def _normalize_rcommunity_openai_args(raw: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 ``{"request": {...}}`` 与站方风格的顶层平铺两种入参。"""
    if not isinstance(raw, dict):
        return {}
    if "request" in raw and isinstance(raw.get("request"), dict):
        out = dict(raw["request"])
        for k, v in raw.items():
            if k != "request":
                out[k] = v
        return out
    return dict(raw)


# 供 ``tools/prompts`` 与 ``llm_interface`` 引用
OPENAI_RCOMMUNITY_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "rcommunity_forum",
            "description": (
                "rcommunity 论坛只读：浏览分区、读取帖子、搜索、星章墙等。"
                "参数对象原样传给 MCP 工具 ``forum``（字段以站方为准）。"
                "请把 MCP 参数字段放在 ``request`` 内（与顶层平铺等价，见实现）。"
            ),
            "parameters": _rcommunity_tool_parameters_schema(
                description=(
                    "传给 MCP ``forum`` 的参数（如 action、分区、帖子 id、关键词、分页等）；"
                    "无参调用可传空对象。"
                ),
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rcommunity_forum_write",
            "description": (
                "rcommunity 论坛写入：发帖、回复、编辑、删除。"
                "参数原样传给 MCP ``forum_write``；字段放在 ``request`` 内。"
            ),
            "parameters": _rcommunity_tool_parameters_schema(
                description="传给 MCP ``forum_write`` 的参数；与站方入参一致。",
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rcommunity_forum_interact",
            "description": (
                "rcommunity 论坛互动：点赞、收藏、置顶等。"
                "参数原样传给 MCP ``forum_interact``；字段放在 ``request`` 内。"
            ),
            "parameters": _rcommunity_tool_parameters_schema(
                description="传给 MCP ``forum_interact`` 的参数；与站方入参一致。",
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rcommunity_chat",
            "description": (
                "rcommunity 聊天室：读取与发送消息。"
                "参数原样传给 MCP ``chat``；字段放在 ``request`` 内。"
            ),
            "parameters": _rcommunity_tool_parameters_schema(
                description="传给 MCP ``chat`` 的参数；与站方入参一致。",
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rcommunity_profile",
            "description": (
                "rcommunity 个人与通知：个人信息、我的帖子、我的回复、通知、查看他人等。"
                "查看自己在他人帖子下的全部回复时可使用 profile，并传 action=\"my_replies\"（及站方要求的其它字段）。"
                "参数原样传给 MCP ``profile``；字段放在 ``request`` 内。"
            ),
            "parameters": _rcommunity_tool_parameters_schema(
                description="传给 MCP ``profile`` 的参数；含 action 等。",
            ),
        },
    },
]


async def _invoke_rcommunity_mcp_tool(
    mcp_tool_name: str,
    arguments: Dict[str, Any],
    *,
    session: Optional[Any] = None,
) -> str:
    """调用 MCP 工具；不在 arguments 中注入 token。"""
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    sse = rcommunity_sse_url()
    if not sse:
        return json.dumps(
            {"error": "未配置 RCOMMUNITY_MCP_TOKEN（环境变量）"},
            ensure_ascii=False,
        )

    async def _call(s: Any) -> str:
        try:
            result = await asyncio.wait_for(
                s.call_tool(mcp_tool_name, arguments),
                timeout=RCOMMUNITY_CALL_TOOL_TIMEOUT_SEC,
            )
            return mcp_call_tool_result_to_json_str(result)
        except asyncio.TimeoutError:
            arg_snip = _clip_log(
                json.dumps(arguments, ensure_ascii=False),
                max_len=160,
            )
            logger.warning(
                "rcommunity MCP call_tool 超时 tool=%s timeout=%ss args=%s",
                mcp_tool_name,
                int(RCOMMUNITY_CALL_TOOL_TIMEOUT_SEC),
                arg_snip,
            )
            return json.dumps(
                {
                    "error": (
                        f"rcommunity MCP 在 {int(RCOMMUNITY_CALL_TOOL_TIMEOUT_SEC)} "
                        "秒内未返回。请检查 ``request`` 内是否含站方要求的字段（如 action、"
                        "分区或帖子 id），勿发送空对象；若用户指 Lutopia 论坛请改用 lutopia_cli。"
                    )
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.warning(
                "rcommunity MCP 调用失败 tool=%s: %s",
                mcp_tool_name,
                e,
                exc_info=True,
            )
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    if session is not None:
        return await _call(session)

    try:
        async with sse_client(
            sse,
            timeout=120.0,
            sse_read_timeout=300.0,
        ) as (read, write):
            async with ClientSession(read, write) as inner:
                await inner.initialize()
                return await _call(inner)
    except Exception as e:
        logger.warning(
            "rcommunity MCP 调用失败 tool=%s: %s",
            mcp_tool_name,
            e,
            exc_info=True,
        )
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@asynccontextmanager
async def create_rcommunity_mcp_session() -> AsyncIterator[Optional[Any]]:
    """
    建立 MCP SSE 连接并完成 ``initialize``，供一轮工具循环内复用。
    无 token 时 ``yield None``。
    """
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    sse = rcommunity_sse_url()
    if not sse:
        yield None
        return

    try:
        async with sse_client(
            sse,
            timeout=120.0,
            sse_read_timeout=300.0,
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    except BaseException as exc:
        try:
            from exceptiongroup import BaseExceptionGroup
        except ImportError:
            BaseExceptionGroup = ()  # type: ignore[misc, assignment]
        if BaseExceptionGroup and isinstance(exc, BaseExceptionGroup):
            logger.warning(
                "rcommunity MCP SSE 会话关闭触发 ExceptionGroup（多为清理噪声）: %s",
                exc,
                exc_info=True,
            )
            return
        raise


@asynccontextmanager
async def maybe_rcommunity_mcp_session(enabled: bool) -> AsyncIterator[Optional[Any]]:
    """
    仅在人设开启 rcommunity 时建立 SSE；否则立即 ``yield None``。

    避免在 ``RCOMMUNITY_MCP_TOKEN`` 已配置但人设未启用时，每条走工具循环的对话都去建连
    （可能阻塞或拖垮首轮回复）。
    """
    if not enabled:
        yield None
        return
    async with create_rcommunity_mcp_session() as session:
        yield session


async def _execute_rcommunity_function_call_impl(
    name: str,
    arguments_json: str,
    *,
    mcp_session: Optional[Any] = None,
) -> str:
    try:
        args: Dict[str, Any] = (
            json.loads(arguments_json) if (arguments_json or "").strip() else {}
        )
    except json.JSONDecodeError:
        return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)

    if not isinstance(args, dict):
        args = {}
    args = _normalize_rcommunity_openai_args(args)

    mcp_name = RCOMMUNITY_OPENAI_TO_MCP.get((name or "").strip())
    if not mcp_name:
        return json.dumps({"error": f"未知 rcommunity 工具: {name}"}, ensure_ascii=False)

    if not get_rcommunity_token():
        return json.dumps(
            {"error": "未配置 RCOMMUNITY_MCP_TOKEN（环境变量）"},
            ensure_ascii=False,
        )

    return await _invoke_rcommunity_mcp_tool(
        mcp_name, args, session=mcp_session
    )


async def execute_rcommunity_function_call(
    name: str,
    arguments_json: str,
    *,
    mcp_session: Optional[Any] = None,
) -> str:
    t0 = time.perf_counter()
    args_summary = _clip_log(arguments_json or "")
    ret = await _execute_rcommunity_function_call_impl(
        name, arguments_json, mcp_session=mcp_session
    )
    elapsed = time.perf_counter() - t0
    result_summary = _clip_log(ret)
    logger.info(
        "[tool] rcommunity name=%s args=%s result=%s elapsed=%.2fs",
        name,
        args_summary,
        result_summary,
        elapsed,
    )
    return ret
