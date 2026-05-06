"""
Internal memory tools exposed to the OpenAI-compatible tool loop.

These tools call CedarClio's local FastAPI endpoints instead of talking to the
MCP SSE server directly, so they can be dispatched through the existing tool
loop used by the in-process AI.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

from config import config

logger = logging.getLogger(__name__)

MEMORY_API_BASE_URL = "http://127.0.0.1:8001/api"
MEMORY_TOOL_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

OPENAI_MEMORY_TOOLS: List[Dict[str, Any]] = [
    # ── 读取类 ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "检索用户的长期记忆（向量+BM25双路召回）。传入查询语句，返回与之最相关的记忆片段，"
                "用于辅助理解用户偏好、历史和上下文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用于检索的查询语句",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get_summaries",
            "description": (
                "分页查询记忆摘要列表（chunk 和日摘要）。"
                "可按日期范围、摘要类型、是否收藏过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "具体日期 YYYY-MM-DD，与 days 二选一",
                    },
                    "days": {
                        "type": "integer",
                        "description": "最近 N 天，与 date 二选一",
                    },
                    "summary_type": {
                        "type": "string",
                        "description": "摘要类型：chunk / daily / 省略=全部",
                        "enum": ["chunk", "daily"],
                    },
                    "starred_only": {
                        "type": "boolean",
                        "description": "仅返回收藏的条目，默认 false",
                        "default": False,
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码，默认 1",
                        "default": 1,
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "每页条数，默认 20",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get_cards",
            "description": (
                "获取记忆卡片（七维卡片）列表。可按角色 ID 和维度过滤。"
                "维度只有以下 7 个合法值：preferences / interaction_patterns / current_status / "
                "goals / relationships / key_events / rules。省略则返回全部维度。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "character_id": {
                        "type": "string",
                        "description": "角色 ID",
                    },
                    "dimension": {
                        "type": "string",
                        "description": (
                            "维度名称，**必须是以下 7 个枚举值之一**："
                            "preferences（偏好）/ interaction_patterns（互动模式）/ "
                            "current_status（当前状态）/ goals（目标）/ "
                            "relationships（关系）/ key_events（关键事件）/ rules（规则）"
                        ),
                        "enum": [
                            "preferences",
                            "interaction_patterns",
                            "current_status",
                            "goals",
                            "relationships",
                            "key_events",
                            "rules",
                        ],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最大条数，默认 50",
                        "default": 50,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get_temporal_states",
            "description": "获取时效状态列表（含已停用），按创建时间倒序。可按天数过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "仅返回最近 N 天的状态，省略则返回全部",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get_relationship_timeline",
            "description": "获取关系时间线条目，按创建时间倒序。可按天数过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "仅返回最近 N 天的条目，省略则返回全部",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get_approval_status",
            "description": (
                "查询自己之前提交的记忆更新审批的当前状态，用于跟进结果。\n"
                "传入 approval_id 时返回该条审批详情（status: pending/approved/rejected/expired，含 tool_name、arguments、resolved_at、resolution_note 等）。\n"
                "不传 approval_id 时返回最近的审批列表，可用 status 过滤、limit 控制条数（默认 10，上限 100，按 created_at 倒序）。\n"
                "审批被同意/拒绝时聊天里会出现『[系统通知] 南杉同意/拒绝了你「xxx」的申请』，看到后通常已经更新；本工具用于主动复查或在自己拿不准时确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "approval_id": {
                        "type": "string",
                        "description": "审批 ID（提交 memory_update_request 时返回的 approval_id），省略则返回列表",
                    },
                    "status": {
                        "type": "string",
                        "description": "仅在不传 approval_id 时生效，按状态过滤",
                        "enum": ["pending", "approved", "rejected", "expired"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "仅在不传 approval_id 时生效，最多返回多少条，默认 10，最大 100",
                        "default": 10,
                    },
                },
                "required": [],
            },
        },
    },
    # ── 写入类（全部走审批）──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "memory_update_request",
            "description": (
                "提交记忆更新/新增的审批请求，经用户在 Mini App 确认后才会生效。\n"
                "调用方式：top-level 必须有 tool_name 和 arguments 两个键，更新字段全部嵌进 arguments 对象，**不要拍平到顶层**。\n"
                "示例：{\"tool_name\":\"update_memory_card\",\"arguments\":{\"persona_id\":1,\"dimension\":\"preferences\",\"content\":\"喜欢猫\"}}\n"
                "\n"
                "【支持的 tool_name 及对应 arguments 字段】\n"
                "\n"
                "1) update_memory_card —— 修改七维记忆卡片\n"
                "   arguments: {persona_id: int, dimension: str, content: str}\n"
                "   dimension **必须是以下 7 个枚举值之一**：\n"
                "     - preferences          偏好\n"
                "     - interaction_patterns 互动模式\n"
                "     - current_status       当前状态\n"
                "     - goals                目标\n"
                "     - relationships        关系\n"
                "     - key_events           关键事件\n"
                "     - rules                规则\n"
                "\n"
                "2) update_temporal_state —— 修改时效状态内容\n"
                "   arguments: {id: int, content: str}\n"
                "\n"
                "3) update_relationship_timeline_entry —— 修改关系时间线条目内容\n"
                "   arguments: {id: int, content: str}\n"
                "\n"
                "4) update_persona_field —— 修改人设字段\n"
                "   arguments: {persona_id: int, field_name: str, content: str}\n"
                "   field_name **必须是以下 7 个枚举值之一**：\n"
                "     - char_identity      身份\n"
                "     - char_personality   性格\n"
                "     - char_speech_style  说话风格\n"
                "     - char_redlines      红线\n"
                "     - char_appearance    外貌\n"
                "     - char_relationships 关系网\n"
                "     - char_nsfw          NSFW 段落\n"
                "\n"
                "5) update_summary —— 修改摘要正文\n"
                "   arguments: {id: int, content: str}\n"
                "\n"
                "6) create_relationship_timeline_entry —— 新增关系时间线条目\n"
                "   arguments: {event_type: str, content: str, source_summary_id?: int}\n"
                "   event_type **必须是以下 4 个枚举值之一**（不能用中文，不能自创）：\n"
                "     - milestone        里程碑（关系性质转折）\n"
                "     - emotional_shift  情绪转折（争吵 / 和好 / 感情升温等）\n"
                "     - conflict         冲突摩擦\n"
                "     - daily_warmth     日常温情（仅极特殊的温馨互动，普通日常严禁写入）\n"
                "\n"
                "7) create_temporal_state —— 新增时效状态\n"
                "   arguments: {content: str, action_rule?: str, expire_at?: str}\n"
                "   expire_at 格式：ISO 8601，例如 \"2026-05-08T23:59:59\"\n"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "要执行的操作名称（见 description 中的 7 个候选）",
                        "enum": [
                            "update_memory_card",
                            "update_temporal_state",
                            "update_relationship_timeline_entry",
                            "update_persona_field",
                            "update_summary",
                            "create_relationship_timeline_entry",
                            "create_temporal_state",
                        ],
                    },
                    "arguments": {
                        "type": "object",
                        "description": (
                            "操作参数对象。具体字段及枚举约束见外层 description；"
                            "**不要把字段拍平到顶层，必须嵌套在本对象内**。"
                        ),
                    },
                },
                "required": ["tool_name", "arguments"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _headers() -> Dict[str, str]:
    token = (config.MINIAPP_TOKEN or "").strip()
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Cedarstar-Token"] = token
    return headers


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def _api_get(path: str, params: Dict[str, Any] | None = None) -> str:
    try:
        logger.info("memory GET %s params=%s", path, params)
        async with httpx.AsyncClient(timeout=MEMORY_TOOL_TIMEOUT) as client:
            resp = await client.get(
                f"{MEMORY_API_BASE_URL}{path}",
                params=params,
                headers=_headers(),
            )
        if not resp.is_success:
            logger.warning("memory GET %s HTTP %d: %s", path, resp.status_code, resp.text[:200])
            return _json_text({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"})
        logger.info("memory GET %s OK", path)
        return _json_text(resp.json())
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.warning("memory GET %s failed: %s", path, e)
        return _json_text({"error": str(e)})


async def _approval_post(tool_name: str, arguments: Dict[str, Any]) -> str:
    try:
        logger.info("memory approval tool_name=%s args=%s", tool_name, arguments)
        async with httpx.AsyncClient(timeout=MEMORY_TOOL_TIMEOUT) as client:
            resp = await client.post(
                f"{MEMORY_API_BASE_URL}/approvals/request",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"tool_name": tool_name, "arguments": arguments},
            )
        logger.info(
            "memory approval response tool_name=%s status=%s body=%s",
            tool_name,
            resp.status_code,
            resp.text[:800],
        )
        if not resp.is_success:
            return _json_text({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"})
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("success") and isinstance(payload.get("data"), dict):
            data = payload["data"]
            return _json_text(
                {
                    "status": data.get("status", "pending"),
                    "approval_id": data.get("approval_id"),
                    "expires_at": data.get("expires_at"),
                }
            )
        return _json_text(payload)
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.warning("approval request failed: %s", e)
        return _json_text({"error": str(e)})


# ---------------------------------------------------------------------------
# Execute functions — read
# ---------------------------------------------------------------------------

async def execute_memory_search(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    query = str(args.get("query") or "").strip()
    if not query:
        return _json_text({"error": "query 不能为空"})
    try:
        top_k = int(args.get("top_k") or 5)
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(top_k, 20))
    return await _api_get("/memory/longterm", {"query": query, "top_k": top_k, "page_size": top_k})


async def execute_memory_get_summaries(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    params: Dict[str, Any] = {}
    if args.get("date"):
        params["source_date_from"] = str(args["date"]).strip()
        params["source_date_to"] = str(args["date"]).strip()
    if args.get("days"):
        params["days"] = int(args["days"])
    if args.get("summary_type"):
        params["summary_type"] = str(args["summary_type"]).strip()
    if args.get("starred_only"):
        params["starred_only"] = True
    params["page"] = int(args.get("page") or 1)
    params["page_size"] = int(args.get("page_size") or 20)
    return await _api_get("/memory/summaries", params)


async def execute_memory_get_cards(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    params: Dict[str, Any] = {}
    if args.get("character_id"):
        params["character_id"] = str(args["character_id"]).strip()
    if args.get("dimension"):
        params["dimension"] = str(args["dimension"]).strip()
    params["limit"] = int(args.get("limit") or 50)
    return await _api_get("/memory/cards", params)


async def execute_memory_get_temporal_states(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    params: Dict[str, Any] = {}
    if args.get("days"):
        params["days"] = int(args["days"])
    return await _api_get("/memory/temporal-states", params)


async def execute_memory_get_relationship_timeline(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    params: Dict[str, Any] = {}
    if args.get("days"):
        params["days"] = int(args["days"])
    return await _api_get("/memory/relationship-timeline", params)


async def execute_memory_get_approval_status(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    approval_id = str(args.get("approval_id") or "").strip()
    if approval_id:
        return await _api_get(f"/memory/approvals/{approval_id}")
    params: Dict[str, Any] = {}
    status = str(args.get("status") or "").strip()
    if status:
        params["status"] = status
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    params["limit"] = max(1, min(limit, 100))
    return await _api_get("/memory/approvals", params)


# ---------------------------------------------------------------------------
# Execute functions — write (via approval)
# ---------------------------------------------------------------------------

async def execute_memory_update_request(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    logger.info(
        "[memory_update_request] entry args_type=%s args_keys=%s args=%s",
        type(arguments).__name__,
        sorted(args.keys()) if isinstance(args, dict) else None,
        args,
    )
    tool_name = str(args.get("tool_name") or "").strip()
    raw_tool_args = args.get("arguments")
    if isinstance(raw_tool_args, dict):
        tool_args = raw_tool_args
    elif isinstance(raw_tool_args, str):
        text = raw_tool_args.strip()
        if not text:
            tool_args = {}
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "[memory_update_request] early-return: arguments JSON decode failed, raw=%r",
                    text[:300],
                )
                return _json_text({"error": "arguments 必须是对象或可解析的 JSON 字符串"})
            if not isinstance(parsed, dict):
                logger.warning(
                    "[memory_update_request] early-return: arguments JSON not dict, type=%s",
                    type(parsed).__name__,
                )
                return _json_text({"error": "arguments JSON 必须是对象"})
            tool_args = parsed
    elif raw_tool_args is None:
        tool_args = {}
    else:
        logger.warning(
            "[memory_update_request] early-return: arguments unexpected type=%s value=%r",
            type(raw_tool_args).__name__,
            str(raw_tool_args)[:300],
        )
        return _json_text({"error": "arguments 必须是对象或 JSON 字符串"})
    if not tool_name:
        extra_keys = sorted(set(args.keys()) - {"tool_name", "arguments"})
        if extra_keys:
            logger.warning("[memory_update_request] flat args detected, keys=%s", extra_keys)
            return _json_text({
                "error": (
                    f"参数格式错误：字段被直接放在了顶层。"
                    f"正确格式是嵌套的：{{\"tool_name\": \"<工具名>\", \"arguments\": {{...}}}}。"
                    f"请把顶层这些字段 {extra_keys} 放进 arguments 对象里，并补上 tool_name，再调用一次。"
                )
            })
        logger.warning(
            "[memory_update_request] early-return: tool_name empty, args=%s",
            args,
        )
        return _json_text({"error": "tool_name 不能为空"})
    return await _approval_post(tool_name, tool_args)
