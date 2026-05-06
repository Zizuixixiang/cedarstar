"""
Lutopia Forum：通过站方 MCP（``cli`` / ``get_guide``）操作论坛，不再维护大量 REST 封装。

站方文档：``AGENT_GUIDE.md`` — https://daskio.de5.net/AGENT_GUIDE.md

Token：从全局 ``config`` 表读取 key ``lutopia_uid``（值用作 Bearer token，与 MCP ``cli`` 的 ``token`` 一致）。
启动时会检查 ``agents/me`` 的 ``dm_send_enabled``，为 false 时自动 ``POST .../agents/me/dm-settings`` 打开私信发送。
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from memory.database import get_database

from tools.memory_tools import (
    execute_memory_search,
    execute_memory_get_summaries,
    execute_memory_get_cards,
    execute_memory_get_temporal_states,
    execute_memory_get_relationship_timeline,
    execute_memory_get_approval_status,
    execute_memory_update_request,
)
from tools.search import execute_search_function_call
from tools.weather import execute_weather_function_call
from tools.weibo import execute_weibo_function_call

logger = logging.getLogger(__name__)

TOOL_LOG_SNIP_MAX = 200
BEHAVIOR_DESC_MAX = 80
TOOL_CONTEXT_SUMMARY_MAX = 150


def _clip_log(text: str, max_len: int = TOOL_LOG_SNIP_MAX) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _tool_result_text_candidates(value: Any) -> List[str]:
    """Extract human-readable text from common MCP / OpenAI-compatible result shapes."""
    out: List[str] = []
    if isinstance(value, str):
        s = value.strip()
        if s:
            out.append(s)
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_tool_result_text_candidates(item))
        return out
    if not isinstance(value, dict):
        return out

    for key in (
        "summary",
        "message",
        "title",
        "stdout",
        "stderr",
        "content",
        "text",
        "output",
        "result",
        "data",
    ):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, (dict, list)):
            out.extend(_tool_result_text_candidates(v))

    if value.get("type") == "text":
        block_text = value.get("text")
        if isinstance(block_text, str) and block_text.strip():
            out.append(block_text.strip())
    return out


def summarize_tool_result_for_context(tool_name: str, arguments_json: str, result_text: str) -> str:
    """生成下一轮 Context 用的短摘要；不把长 raw 直接塞给模型。短结果直接返回。"""
    raw = (result_text or "").strip()
    # 短结果直接存，不做摘要
    if len(raw) <= TOOL_CONTEXT_SUMMARY_MAX:
        return raw or "已执行，但没有返回可读结果。"
    summary = ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if err is not None and str(err).strip():
            summary = f"执行失败：{str(err).strip()}"
        else:
            candidates = _tool_result_text_candidates(parsed)
            if candidates:
                seen = set()
                deduped: List[str] = []
                for item in candidates:
                    compact_item = item.strip()
                    if not compact_item or compact_item in seen:
                        continue
                    seen.add(compact_item)
                    deduped.append(compact_item)
                summary = "\n\n".join(deduped)
            if not summary:
                compact = json.dumps(parsed, ensure_ascii=False)
                summary = compact
    else:
        summary = raw
    summary = summary.replace("\r\n", "\n").strip()
    if len(summary) > TOOL_CONTEXT_SUMMARY_MAX:
        summary = summary[:TOOL_CONTEXT_SUMMARY_MAX] + "..."
    if not summary:
        summary = "已执行，但没有返回可读结果。"
    return summary


def tool_result_for_model(tool_name: str, arguments_json: str, result_text: str) -> str:
    """本轮回传给聊天模型的工具结果；长结果先压成短 JSON，防止一次工具吞掉大量 token。"""
    raw = result_text or ""
    if len(raw) <= 6000:
        return raw
    summary = summarize_tool_result_for_context(tool_name, arguments_json, raw)
    return json.dumps(
        {
            "summary": summary,
            "truncated": True,
            "note": "工具原始结果较长，已压缩为摘要供本轮继续对话。",
        },
        ensure_ascii=False,
    )


async def save_tool_execution_record(
    *,
    session_id: Optional[str],
    turn_id: Optional[str],
    seq: int,
    tool_name: str,
    arguments_json: str,
    result_text: str,
    platform: Optional[str] = None,
    user_message_id: Optional[int] = None,
    assistant_message_id: Optional[int] = None,
) -> None:
    """尽力落库工具执行记录；失败只记日志，不影响对话。"""
    if not session_id or not turn_id:
        return
    try:
        from memory.database import save_tool_execution

        await save_tool_execution(
            session_id=session_id,
            turn_id=turn_id,
            seq=seq,
            tool_name=tool_name,
            arguments_json=arguments_json or "{}",
            result_summary=summarize_tool_result_for_context(
                tool_name, arguments_json or "{}", result_text
            ),
            result_raw=result_text,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            platform=platform,
        )
    except Exception as e:
        logger.warning("保存工具执行记录失败: %s", e)


def lutopia_tool_display_name(name: str) -> str:
    t = (name or "").strip()
    if t.startswith("lutopia_"):
        return t[8:] or t
    return t or "tool"


def _behavior_result_short_description(parsed: Dict[str, Any], raw_fallback: str) -> str:
    for key in ("title", "message", "summary", "content", "text", "output", "result"):
        v = parsed.get(key)
        if isinstance(v, str) and v.strip():
            return _clip_log(v.replace("\n", " "), BEHAVIOR_DESC_MAX)
    for v in parsed.values():
        if isinstance(v, str) and v.strip():
            return _clip_log(v.replace("\n", " "), BEHAVIOR_DESC_MAX)
        if isinstance(v, (int, float)) and str(v).strip():
            return _clip_log(str(v), BEHAVIOR_DESC_MAX)
    return _clip_log(raw_fallback, BEHAVIOR_DESC_MAX)


def lutopia_behavior_line(tool_name: str, result_json: str) -> str:
    disp = lutopia_tool_display_name(tool_name)
    try:
        parsed = json.loads(result_json)
    except json.JSONDecodeError:
        return f"· 已尝试{disp}：执行失败"
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if err is not None and str(err).strip() != "":
            return f"· 已尝试{disp}：执行失败"
        desc = _behavior_result_short_description(parsed, result_json)
    else:
        desc = _clip_log(str(parsed), BEHAVIOR_DESC_MAX)
    if not desc:
        desc = "已完成"
    return f"· 已{disp}：{desc}"


def build_lutopia_behavior_appendix(executions: List[Tuple[str, str]]) -> str:
    """
    根据 (tool_name, result_json) 列表生成落库用「行为记录」块（不含用户消息正文）。
    """
    if not executions:
        return ""
    lines = [lutopia_behavior_line(nm, res) for nm, res in executions]
    return "\n\n[行为记录]\n" + "\n".join(lines)


def strip_lutopia_behavior_appendix(text: str) -> str:
    """
    去掉 ``build_lutopia_behavior_appendix`` 追加的 ``\\n\\n[行为记录]…`` 后缀。
    用于 Telegram 等不向用户展示附录、仅数据库保留完整正文的场景。
    """
    s = text or ""
    sep = "\n\n[行为记录]"
    if sep not in s:
        return s
    return s.split(sep, 1)[0].rstrip()


_RE_INTERNAL_MEMORY_BLOCK = re.compile(r"\[系统内部记忆：[^\]]*\]")


def strip_lutopia_internal_memory_blocks(text: str) -> str:
    """去掉落库用 ``[系统内部记忆：…]`` 块（发往用户前调用；库内可保留完整旁白）。"""
    return _RE_INTERNAL_MEMORY_BLOCK.sub("", text or "").strip()


def strip_lutopia_user_facing_assistant_text(text: str) -> str:
    """助手正文发往用户前：去掉历史 ``[行为记录]`` 后缀与 ``[系统内部记忆：…]`` 块。"""
    s = strip_lutopia_behavior_appendix(text or "")
    s = strip_lutopia_internal_memory_blocks(s)
    return s


_LUTOPIA_WRITE_VERBS = frozenset(
    {"comment", "post", "dm", "delete", "vote", "rename", "avatar", "confirm"}
)
_LUTOPIA_READ_VERBS = frozenset(
    {
        "list",
        "search",
        "wander",
        "show",
        "comment-show",
        "inbox",
        "whoami",
        "dm-settings",
    }
)
# 从工具返回文本中提取 id（JSON ``"post_id": 1`` 或 MCP 纯文本 ``post_id: 1``）
_RE_RESULT_ID_BITS = re.compile(
    r'"?([A-Za-z_][A-Za-z0-9_]*_id)"?\s*[:：]\s*'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,，;；\]\}\"]+)',
    re.MULTILINE,
)


def _lutopia_extract_id_suffix(result_text: str, max_bits: int = 6) -> str:
    """从 ``result_text`` 中提取 ``*_id``，去重后拼成 ``，xxx_id: yyy`` 形式。"""
    s = result_text or ""
    seen: set[Tuple[str, str]] = set()
    bits: List[str] = []

    def _add(key: str, raw_val: str) -> None:
        if raw_val.startswith('"') and raw_val.endswith('"'):
            val = raw_val[1:-1].replace('\\"', '"')
        elif raw_val.startswith("'") and raw_val.endswith("'"):
            val = raw_val[1:-1].replace("\\'", "'")
        else:
            val = raw_val.strip()
        if not val:
            return
        t = (key, val)
        if t in seen:
            return
        seen.add(t)
        bits.append(f"{key}: {val}")

    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if str(k).endswith("_id") and v is not None and str(v).strip() != "":
                    _add(str(k), str(v).strip())
                if len(bits) >= max_bits:
                    break
            for nk in ("post", "comment", "message", "data", "output"):
                if len(bits) >= max_bits:
                    break
                sub = parsed.get(nk)
                if isinstance(sub, dict):
                    for k, v in sub.items():
                        if str(k).endswith("_id") and v is not None and str(v).strip() != "":
                            _add(str(k), str(v).strip())
                        if len(bits) >= max_bits:
                            break
    except json.JSONDecodeError:
        pass

    if len(bits) < max_bits:
        for m in _RE_RESULT_ID_BITS.finditer(s):
            _add(m.group(1), m.group(2))
            if len(bits) >= max_bits:
                break

    if not bits:
        return ""
    return "，" + "，".join(bits[:max_bits])


def _lutopia_parse_cli_command(arguments_json: str) -> Tuple[str, List[str]]:
    """从 ``lutopia_cli`` 的 ``arguments_json`` 解析出命令串与分词列表。"""
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return "", []
    if not isinstance(args, dict):
        return "", []
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "", []
    return cmd, cmd.split()


def _lutopia_write_action_description(first_verb: str, words: List[str]) -> str:
    """写操作的自然语言主干（不含 id 后缀）。"""
    w = words or []
    second = w[1] if len(w) > 1 else ""
    if first_verb == "comment":
        return f"已回复帖子 #{second or '?'}"
    if first_verb == "post":
        return "已发布新帖"
    if first_verb == "dm":
        return f"已向 {second or '对方'} 发送私信"
    if first_verb == "delete":
        return f"已删除帖子 #{second or '?'}"
    if first_verb == "vote":
        return "已投票"
    if first_verb == "rename":
        return "已修改用户名"
    if first_verb == "avatar":
        return "已修改头像"
    if first_verb == "confirm":
        return "已确认操作"
    return ""


def lutopia_internal_memory_line(
    name: str, arguments_json: str, result_text: str
) -> str:
    """
    单次 Lutopia 工具执行后的落库旁白（单行）。

    先按 CLI 首词区分读/写：读操作与 ``lutopia_get_guide`` 不生成旁白（空串）；
    写操作从 ``result_text`` 正则提取 ``*_id`` 拼在句末。
    """
    nm = (name or "").strip()
    if nm == "lutopia_get_guide":
        return ""

    if nm != "lutopia_cli":
        # 非 CLI 工具：保守起见不记录（当前仅暴露 cli / get_guide）
        return ""

    _cmd, words = _lutopia_parse_cli_command(arguments_json)
    if not words:
        return ""

    first = words[0].lower()
    if first in _LUTOPIA_READ_VERBS or first not in _LUTOPIA_WRITE_VERBS:
        return ""

    base = _lutopia_write_action_description(first, words)
    if not base:
        return ""

    err_detail: Optional[str] = None
    try:
        parsed = json.loads(result_text or "")
        if isinstance(parsed, dict):
            ev = parsed.get("error")
            if ev is not None and str(ev).strip() != "":
                err_detail = str(ev).strip()
    except json.JSONDecodeError:
        pass

    if err_detail is not None:
        detail = _clip_log(err_detail.replace("\n", " "), 120)
        return f"[系统内部记忆：{base}，操作失败：{detail}]"

    suffix = _lutopia_extract_id_suffix(result_text or "")
    return f"[系统内部记忆：{base}{suffix}]"


def build_lutopia_internal_memory_appendix(
    executions: List[Tuple[str, str, str]],
) -> str:
    """根据 ``(tool_name, arguments_json, result_text)`` 列表生成多行旁白，用于落库。"""
    if not executions:
        return ""
    lines: List[str] = []
    for nm, args_j, res in executions:
        line = lutopia_internal_memory_line(nm, args_j, res)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


BASE_URL = "https://daskio.de5.net"
LUTOPIA_FORUM_PREFIX = f"{BASE_URL}/forum/api/v1"
LUTOPIA_MCP_SSE_URL = f"{BASE_URL}/mcp/sse"

# OpenAI / Gemini 兼容 Chat Completions：经 MCP ``cli`` / ``get_guide`` 操作论坛（见 AGENT_GUIDE）
OPENAI_LUTOPIA_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lutopia_cli",
            "description": "执行 Lutopia 论坛 CLI 命令，用于发帖、评论、私信、查帖等所有论坛操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "CLI 命令字符串，例如：comment 123 回复内容、post tech 标题 正文、dm Kai 你好",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_get_guide",
            "description": "获取 Lutopia 论坛指南，查询可用命令和 API 文档。不确定命令格式时先调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "可选，指定章节，如 cli、api.posts、api.dm 等",
                    }
                },
                "required": [],
            },
        },
    },
]


async def get_lutopia_token() -> Optional[str]:
    """从 ``config`` 表读取 ``lutopia_uid``（与论坛 API / MCP 的 Bearer UID 一致）。"""
    try:
        db = get_database()
        raw = await db.get_config("lutopia_uid")
        s = (raw or "").strip()
        return s or None
    except Exception as e:
        logger.warning("读取 lutopia_uid 失败: %s", e)
        return None


def _auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "CedarStar-Lutopia/0.1",
    }


async def ensure_lutopia_dm_send_enabled_on_startup() -> None:
    """
    启动时检查 ``GET .../agents/me`` 的 ``dm_send_enabled``；
    若非 true，则 ``POST .../agents/me/dm-settings`` 设为 ``send_enabled: true``。
    未配置 ``lutopia_uid`` 或请求失败时仅打日志，不抛错。
    """
    token = await get_lutopia_token()
    if not token:
        return
    url_me = f"{LUTOPIA_FORUM_PREFIX}/agents/me"
    url_settings = f"{LUTOPIA_FORUM_PREFIX}/agents/me/dm-settings"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url_me, headers=_auth_headers(token))
        if not resp.is_success:
            logger.warning(
                "Lutopia agents/me HTTP %s，跳过 DM 发送开关检查",
                resp.status_code,
            )
            return
        text = (resp.text or "").strip()
        if not text:
            return
        try:
            data = resp.json()
        except json.JSONDecodeError:
            logger.warning("Lutopia agents/me 非 JSON，跳过 DM 发送开关检查")
            return
        if not isinstance(data, dict):
            return
        if data.get("dm_send_enabled") is True:
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp2 = await client.post(
                url_settings,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"send_enabled": True},
            )
        if resp2.is_success:
            logger.info("已开启 Lutopia 私信发送（agents/me/dm-settings send_enabled=true）")
        else:
            logger.warning(
                "开启 Lutopia 私信发送失败 HTTP %s: %s",
                resp2.status_code,
                (resp2.text or "")[:300],
            )
    except httpx.HTTPError as e:
        logger.warning("Lutopia DM 设置检查请求失败: %s", e)


def _mcp_call_tool_result_to_json_str(result: Any) -> str:
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


async def _invoke_lutopia_mcp_tool(
    mcp_tool_name: str,
    arguments: Dict[str, Any],
    *,
    session: Optional[Any] = None,
) -> str:
    """
    调用 MCP 工具 ``cli`` / ``get_guide``。

    ``session`` 为已 ``initialize`` 的 ``ClientSession`` 时复用连接；为 ``None`` 时临时建连并断开（兼容单调用）。
    """
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    token = await get_lutopia_token()
    if not token:
        return json.dumps(
            {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"},
            ensure_ascii=False,
        )

    merged_args = dict(arguments)
    merged_args.setdefault("token", token)

    async def _call(s: Any) -> str:
        try:
            result = await s.call_tool(mcp_tool_name, merged_args)
            return _mcp_call_tool_result_to_json_str(result)
        except Exception as e:
            logger.warning(
                "Lutopia MCP 调用失败 tool=%s: %s",
                mcp_tool_name,
                e,
                exc_info=True,
            )
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    if session is not None:
        return await _call(session)

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with sse_client(
            LUTOPIA_MCP_SSE_URL,
            headers=headers,
            timeout=120.0,
            sse_read_timeout=300.0,
        ) as (read, write):
            async with ClientSession(read, write) as inner:
                await inner.initialize()
                return await _call(inner)
    except Exception as e:
        logger.warning(
            "Lutopia MCP 调用失败 tool=%s: %s",
            mcp_tool_name,
            e,
            exc_info=True,
        )
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@asynccontextmanager
async def create_lutopia_mcp_session() -> AsyncIterator[Optional[Any]]:
    """
    建立 MCP SSE 连接并完成 ``initialize``，供一轮工具循环内复用。

    未配置 ``lutopia_uid`` 时 ``yield None``（调用方仍可用无 session 的一次性连接逻辑）；
    退出上下文时关闭连接。
    """
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    token = await get_lutopia_token()
    if not token:
        yield None
        return

    headers = {"Authorization": f"Bearer {token}"}
    async with sse_client(
        LUTOPIA_MCP_SSE_URL,
        headers=headers,
        timeout=120.0,
        sse_read_timeout=300.0,
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _safe_load_tool_args(arg: str, tool_name: str) -> Dict[str, Any]:
    """容错地把 OpenAI tool_call 的 arguments 字符串解析成 dict。

    模型流式输出 JSON 时偶尔会在收尾处漏 ``}`` 或 ``]``（GLM-5.1 已观测到），
    此处先按原样解析，失败再用括号差补齐重试。任何失败都打 WARNING，绝不
    再静默吞成 ``{}``。
    """
    s = arg if isinstance(arg, str) else ""
    if not s.strip():
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError as e1:
        miss_brace = max(0, s.count("{") - s.count("}"))
        miss_bracket = max(0, s.count("[") - s.count("]"))
        if miss_brace or miss_bracket:
            patched = s + ("}" * miss_brace) + ("]" * miss_bracket)
            try:
                v = json.loads(patched)
                logger.warning(
                    "[tool_args repair] tool=%s padded missing brackets brace=%d bracket=%d",
                    tool_name,
                    miss_brace,
                    miss_bracket,
                )
                return v if isinstance(v, dict) else {}
            except json.JSONDecodeError as e2:
                logger.warning(
                    "[tool_args parse fail after repair] tool=%s err=%s arg=%r",
                    tool_name,
                    e2,
                    s[:500],
                )
                return {}
        logger.warning(
            "[tool_args parse fail] tool=%s err=%s arg=%r",
            tool_name,
            e1,
            s[:500],
        )
        return {}


async def append_tool_exchange_to_messages(
    messages: List[Dict[str, Any]],
    assistant_text: str,
    tool_calls: List[Dict[str, Any]],
    *,
    on_tool_start: Optional[Callable[[str], Awaitable[None]]] = None,
    on_tool_done: Optional[Callable[[str, str], Awaitable[None]]] = None,
    execution_log: Optional[List[Tuple[str, str, str]]] = None,
    mcp_session: Optional[Any] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    platform: Optional[str] = None,
    user_message_id: Optional[int] = None,
) -> None:
    """
    将一轮 assistant.tool_calls 及对应 tool 结果追加到 messages（原地修改）。
    ``tool_calls`` 为流式或非流式解析后的简表（id / name / arguments）。

    ``on_tool_start`` / ``on_tool_done``：可选，分别在单次工具执行前后回调（如 Telegram 状态提示）。

    ``mcp_session``：由 ``create_lutopia_mcp_session()`` 提供时复用 MCP 连接；未传则每次工具调用单独建连。
    """
    wrapped: List[Dict[str, Any]] = []
    for seq, tc in enumerate(tool_calls, start=1):
        if not isinstance(tc, dict):
            continue
        tid = tc.get("id") or ""
        nm = tc.get("name") or ""
        arg = tc.get("arguments")
        if not isinstance(arg, str):
            arg = json.dumps(arg if arg is not None else {}, ensure_ascii=False)
        wrapped.append(
            {
                "id": tid,
                "type": "function",
                "function": {"name": nm, "arguments": arg or "{}"},
            }
        )
    at = (assistant_text or "").strip()
    messages.append(
        {
            "role": "assistant",
            "content": at if at else None,
            "tool_calls": wrapped,
        }
    )
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        nm = tc.get("name") or ""
        arg = tc.get("arguments")
        if not isinstance(arg, str):
            arg = json.dumps(arg if arg is not None else {}, ensure_ascii=False)
        if on_tool_start:
            await on_tool_start(nm)
        if nm == "memory_search":
            args_mem = _safe_load_tool_args(arg, nm)
            out = await execute_memory_search(args_mem)
        elif nm == "memory_get_summaries":
            args_ms = _safe_load_tool_args(arg, nm)
            out = await execute_memory_get_summaries(args_ms)
        elif nm == "memory_get_cards":
            args_mc = _safe_load_tool_args(arg, nm)
            out = await execute_memory_get_cards(args_mc)
        elif nm == "memory_get_temporal_states":
            args_mt = _safe_load_tool_args(arg, nm)
            out = await execute_memory_get_temporal_states(args_mt)
        elif nm == "memory_get_relationship_timeline":
            args_mr = _safe_load_tool_args(arg, nm)
            out = await execute_memory_get_relationship_timeline(args_mr)
        elif nm == "memory_get_approval_status":
            args_ma = _safe_load_tool_args(arg, nm)
            out = await execute_memory_get_approval_status(args_ma)
        elif nm == "memory_update_request":
            args_mem_up = _safe_load_tool_args(arg, nm)
            out = await execute_memory_update_request(args_mem_up)
        elif nm == "get_weather":
            args_d = _safe_load_tool_args(arg, nm)
            out = await execute_weather_function_call(nm, args_d)
        elif nm == "get_weibo_hot":
            args_wb = _safe_load_tool_args(arg, nm)
            out = await execute_weibo_function_call(nm, args_wb)
        elif nm == "web_search":
            args_ws = _safe_load_tool_args(arg, nm)
            out = await execute_search_function_call(nm, args_ws)
        elif nm in (
            "post_tweet", "read_mentions", "like_tweet", "unlike_tweet",
            "reply_tweet", "search_tweets", "get_timeline", "get_user",
            "follow_user", "unfollow_user", "get_followers",
        ):
            args_xx = _safe_load_tool_args(arg, nm)
            from tools.x_tool import execute_x_function_call
            out = await execute_x_function_call(nm, args_xx)
        else:
            out = await execute_lutopia_function_call(
                nm, arg or "{}", mcp_session=mcp_session
            )
        if execution_log is not None and nm not in (
            "get_weather",
            "get_weibo_hot",
            "web_search",
        ):
            execution_log.append((nm, arg or "{}", out))
        if on_tool_done:
            await on_tool_done(nm, out)
        await save_tool_execution_record(
            session_id=session_id,
            turn_id=turn_id,
            seq=seq,
            tool_name=nm,
            arguments_json=arg or "{}",
            result_text=out,
            platform=platform,
            user_message_id=user_message_id,
        )
        model_out = tool_result_for_model(nm, arg or "{}", out)
        messages.append(
            {"role": "tool", "tool_call_id": tc.get("id") or "", "content": model_out}
        )


async def _execute_lutopia_function_call_impl(
    name: str,
    arguments_json: str,
    *,
    mcp_session: Optional[Any] = None,
) -> str:
    """执行 MCP 封装工具，返回 JSON 字符串（不含 [tool] 日志）。"""
    try:
        args: Dict[str, Any] = (
            json.loads(arguments_json) if (arguments_json or "").strip() else {}
        )
    except json.JSONDecodeError:
        return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)

    if not isinstance(args, dict):
        args = {}

    token = await get_lutopia_token()
    if not token:
        return json.dumps(
            {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"},
            ensure_ascii=False,
        )

    if name == "lutopia_cli":
        command = str(args.get("command") or "").strip()
        if not command:
            return json.dumps({"error": "command 不能为空"}, ensure_ascii=False)
        return await _invoke_lutopia_mcp_tool(
            "cli",
            {"token": token, "command": command},
            session=mcp_session,
        )

    if name == "lutopia_get_guide":
        mcp_args: Dict[str, Any] = {"token": token}
        sec = args.get("section")
        if sec is not None and str(sec).strip():
            mcp_args["section"] = str(sec).strip()
        return await _invoke_lutopia_mcp_tool(
            "get_guide", mcp_args, session=mcp_session
        )

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


async def execute_lutopia_function_call(
    name: str,
    arguments_json: str,
    *,
    mcp_session: Optional[Any] = None,
) -> str:
    """
    根据 function name 与 JSON 参数字符串执行 MCP 工具，返回 JSON 字符串供 role=tool。
    每次执行打一行 ``[tool]`` 日志（args/result 截断至 200 字）。

    ``mcp_session``：由 ``create_lutopia_mcp_session()`` 提供时，整轮工具循环复用同一 MCP 连接；未传则每次调用单独建连。
    """
    t0 = time.perf_counter()
    args_summary = _clip_log(arguments_json or "")
    ret = await _execute_lutopia_function_call_impl(
        name, arguments_json, mcp_session=mcp_session
    )
    elapsed = time.perf_counter() - t0
    result_summary = _clip_log(ret)
    logger.info(
        "[tool] name=%s args=%s result=%s elapsed=%.2fs",
        name,
        args_summary,
        result_summary,
        elapsed,
    )
    return ret
