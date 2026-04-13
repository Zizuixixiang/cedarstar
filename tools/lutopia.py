"""
Lutopia Forum HTTP 客户端（异步）。

Token：从全局 ``config`` 表读取 key ``lutopia_uid``（值用作 Bearer token）。
发帖/评论/私信若返回 ``requires_confirmation``，会自动调用 ``POST .../posts/confirm`` 完成二次确认。
启动时会检查 ``agents/me`` 的 ``dm_send_enabled``，为 false 时自动 ``POST .../agents/me/dm-settings`` 打开私信发送。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from memory.database import get_database

logger = logging.getLogger(__name__)

TOOL_LOG_SNIP_MAX = 200
BEHAVIOR_DESC_MAX = 80


def _clip_log(text: str, max_len: int = TOOL_LOG_SNIP_MAX) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def lutopia_tool_display_name(name: str) -> str:
    t = (name or "").strip()
    if t.startswith("lutopia_"):
        return t[8:] or t
    return t or "tool"


def _behavior_result_short_description(parsed: Dict[str, Any], raw_fallback: str) -> str:
    for key in ("title", "message", "summary", "content", "text"):
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

BASE_URL = "https://daskio.de5.net"
LUTOPIA_FORUM_PREFIX = f"{BASE_URL}/forum/api/v1"
# Wiki / 知识库（与论坛 API 不同前缀，见 AGENT_GUIDE）
KNOWLEDGE_API_PREFIX = f"{BASE_URL}/api"


def _lutopia_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


_POLL_PROPERTIES = {
    "question": {"type": "string", "description": "投票问题，≤500 字"},
    "options": {
        "type": "array",
        "items": {"type": "string"},
        "description": "2～20 项选项",
    },
    "multiple_choice": {"type": "boolean"},
    "allow_human_vote": {"type": "boolean"},
    "max_choices": {"type": "integer"},
    "closes_at": {
        "type": "string",
        "description": "截止时间 ISO8601（UTC 或带偏移），可选",
    },
}

# OpenAI / Gemini 兼容 Chat Completions 的 function tools 声明（对齐 Lutopia AGENT_GUIDE REST）
OPENAI_LUTOPIA_TOOLS: List[Dict[str, Any]] = [
    _lutopia_tool(
        "lutopia_get_posts",
        "列出论坛帖子。浏览时默认 view_agent=true 以省 token；可用 offset 分页。",
        {
            "sort": {
                "type": "string",
                "enum": ["hot", "new", "top", "rising"],
                "description": "排序方式",
            },
            "limit": {"type": "integer"},
            "submolt": {"type": "string", "description": "分区 slug"},
            "offset": {"type": "integer"},
            "view_agent": {
                "type": "boolean",
                "description": "true 时带 view=agent，默认 true",
            },
        },
    ),
    _lutopia_tool(
        "lutopia_create_post",
        "发布新帖；可选 poll 在同帖发起投票（与论坛 POST /posts 一致）。",
        {
            "submolt": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "poll": {
                "type": "object",
                "description": "可选；投票对象，须含 question 与 options",
                "properties": _POLL_PROPERTIES,
                "required": ["question", "options"],
            },
        },
        ["submolt", "title", "content"],
    ),
    _lutopia_tool(
        "lutopia_get_post",
        "获取单帖详情。默认 view_agent=true。",
        {
            "post_id": {"type": "string"},
            "view_agent": {
                "type": "boolean",
                "description": "true 时带 view=agent，默认 true",
            },
        },
        ["post_id"],
    ),
    _lutopia_tool(
        "lutopia_get_post_comments",
        "获取帖子下评论树。长评论可能截断，可再调 lutopia_get_comment。",
        {
            "post_id": {"type": "string"},
            "sort": {"type": "string", "description": "如 top"},
            "limit": {"type": "integer"},
            "view_agent": {
                "type": "boolean",
                "description": "默认 true，对应 view=agent",
            },
            "content": {
                "type": "string",
                "enum": ["preview", "full", "none"],
                "description": "评论正文粒度；默认 preview",
            },
        },
        ["post_id"],
    ),
    _lutopia_tool(
        "lutopia_get_comment",
        "获取单条评论全文（用于树响应里截断时展开）。",
        {"comment_id": {"type": "string"}},
        ["comment_id"],
    ),
    _lutopia_tool(
        "lutopia_comment",
        "在帖子下发表评论；可选 parent_id 回复某条评论。",
        {
            "post_id": {"type": "string"},
            "content": {"type": "string"},
            "parent_id": {"type": "string", "description": "父评论 ID"},
        },
        ["post_id", "content"],
    ),
    _lutopia_tool(
        "lutopia_edit_post",
        "编辑自己的帖子；title 与 content 至少填一项。",
        {
            "post_id": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
        },
        ["post_id"],
    ),
    _lutopia_tool(
        "lutopia_edit_comment",
        "编辑自己的评论。",
        {"comment_id": {"type": "string"}, "content": {"type": "string"}},
        ["comment_id", "content"],
    ),
    _lutopia_tool(
        "lutopia_vote",
        "对帖子赞/踩。",
        {
            "post_id": {"type": "string"},
            "value": {
                "type": "string",
                "enum": ["1", "-1"],
                "description": "字符串枚举以兼容 Gemini",
            },
        },
        ["post_id", "value"],
    ),
    _lutopia_tool(
        "lutopia_vote_comment",
        "对评论赞/踩。",
        {
            "comment_id": {"type": "string"},
            "value": {"type": "string", "enum": ["1", "-1"]},
        },
        ["comment_id", "value"],
    ),
    _lutopia_tool(
        "lutopia_get_poll",
        "获取投票详情（含选项票数与本人选择）。",
        {"poll_id": {"type": "string"}},
        ["poll_id"],
    ),
    _lutopia_tool(
        "lutopia_vote_poll",
        "在投票中提交或改票；单选传 1 个 option_id，多选传多个。",
        {
            "poll_id": {"type": "string"},
            "option_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "选项 ID 列表",
            },
        },
        ["poll_id", "option_ids"],
    ),
    _lutopia_tool(
        "lutopia_delete_poll_vote",
        "撤回本人在某投票中的全部选择。",
        {"poll_id": {"type": "string"}},
        ["poll_id"],
    ),
    _lutopia_tool(
        "lutopia_delete_poll",
        "删除投票（作者或管理员）；帖子本身不受影响。",
        {"poll_id": {"type": "string"}},
        ["poll_id"],
    ),
    _lutopia_tool(
        "lutopia_get_profile",
        "获取当前账号资料 GET /agents/me。",
        {},
    ),
    _lutopia_tool(
        "lutopia_get_activity",
        "聚合本人动态：发帖/评论统计、近 30 日桶、近期帖与评等。",
        {},
    ),
    _lutopia_tool(
        "lutopia_lookup_agent",
        "按全局唯一 name 解析公开资料（DM 前可选）。",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _lutopia_tool(
        "lutopia_rename",
        "直接改名（agent；受 7 天冷却等规则约束）。",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _lutopia_tool(
        "lutopia_rename_request",
        "提交改名申请（冷却中时使用）。",
        {
            "name": {"type": "string"},
            "reason": {"type": "string"},
        },
        ["name", "reason"],
    ),
    _lutopia_tool(
        "lutopia_set_avatar",
        "设置头像：clear=true 清空；否则须 avatar_type 为 emoji 或 kaomoji 且提供 value。",
        {
            "clear": {
                "type": "boolean",
                "description": "为 true 时清空头像，忽略 type/value",
            },
            "avatar_type": {"type": "string", "enum": ["emoji", "kaomoji"]},
            "value": {"type": "string"},
        },
    ),
    _lutopia_tool(
        "lutopia_get_rename_requests",
        "列出本人改名申请记录。",
        {},
    ),
    _lutopia_tool(
        "lutopia_list_submolts",
        "列出分区（发帖前可查 slug）。",
        {
            "sort": {"type": "string", "description": "如 popular"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
        },
    ),
    _lutopia_tool(
        "lutopia_create_submolt",
        "创建新版块（需权限）。display_name 为展示名。",
        {
            "name": {"type": "string"},
            "display_name": {"type": "string"},
            "description": {"type": "string"},
        },
        ["name", "display_name", "description"],
    ),
    _lutopia_tool(
        "lutopia_get_summary",
        "按日群聊摘要，日期 YYYY-MM-DD。",
        {"date": {"type": "string"}},
        ["date"],
    ),
    _lutopia_tool(
        "lutopia_knowledge",
        "只读知识库/Wiki；action 指定操作，按需传 category_id、q、theme。",
        {
            "action": {
                "type": "string",
                "enum": [
                    "overview",
                    "categories",
                    "category_docs",
                    "search",
                    "hot_topics",
                    "clusters",
                    "cluster_detail",
                    "faq",
                    "contributors",
                ],
            },
            "category_id": {"type": "string"},
            "q": {"type": "string", "description": "search 时的关键词"},
            "theme": {
                "type": "string",
                "description": "cluster_detail 时的主题（URL 编码由服务端处理）",
            },
        },
        ["action"],
    ),
    _lutopia_tool(
        "lutopia_send_dm",
        "发私信（recipient_name 为对方唯一 handle）。",
        {
            "recipient_name": {"type": "string"},
            "content": {"type": "string"},
        },
        ["recipient_name", "content"],
    ),
    _lutopia_tool(
        "lutopia_get_inbox",
        "收件箱；可 limit、unread。",
        {
            "limit": {"type": "integer", "description": "默认 50，最大 200"},
            "unread": {"type": "boolean", "description": "仅未读"},
        },
    ),
    _lutopia_tool(
        "lutopia_get_dm_sent",
        "已发送私信列表。",
        {"limit": {"type": "integer"}},
    ),
    _lutopia_tool(
        "lutopia_dm_unread_count",
        "未读私信数量（轻量轮询）。",
        {},
    ),
    _lutopia_tool(
        "lutopia_mark_read",
        "标已读：传 message_ids 标指定；否则 all=true/false 表全部（默认 true）。",
        {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "若提供则优先按 ID 标已读",
            },
            "all": {"type": "boolean", "description": "与 message_ids 二选一语义"},
        },
    ),
    _lutopia_tool(
        "lutopia_dm_settings",
        "更新私信开关 receive_enabled / send_enabled（需账号策略允许）。",
        {
            "receive_enabled": {"type": "boolean"},
            "send_enabled": {"type": "boolean"},
        },
    ),
    _lutopia_tool(
        "lutopia_delete_post",
        "删除自己的帖子。",
        {
            "post_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        ["post_id"],
    ),
    _lutopia_tool(
        "lutopia_delete_comment",
        "删除自己的评论。",
        {
            "comment_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        ["comment_id"],
    ),
]


async def get_lutopia_token() -> Optional[str]:
    """从 ``config`` 表读取 ``lutopia_uid``（与论坛 API 的 Bearer UID 一致）。"""
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


async def _response_to_payload(resp: httpx.Response) -> Any:
    text = (resp.text or "").strip()
    if not text:
        if resp.is_success:
            return {}
        return {"error": f"HTTP {resp.status_code}，空响应"}
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return {"error": f"HTTP {resp.status_code}，非 JSON 响应: {text[:200]}"}
    if resp.is_success:
        return data
    if isinstance(data, dict) and data.get("error"):
        return {"error": str(data.get("error"))}
    if isinstance(data, dict) and data.get("message"):
        return {"error": str(data.get("message"))}
    return {"error": f"HTTP {resp.status_code}: {text[:400]}"}


_LUTOPIA_CONFIRM_TEXT = (
    "我已检查内容，不含未授权的隐私信息和过度的NSFW描写。token:{token}"
)


async def _maybe_confirm_after_post_or_comment(
    resp: httpx.Response,
    bearer_token: str,
    client: httpx.AsyncClient,
) -> Any:
    """
    发帖/评论若返回 requires_confirmation，则自动 POST /posts/confirm，对调用方透明。
    """
    text = (resp.text or "").strip()
    if text:
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return await _response_to_payload(resp)
        if isinstance(data, dict) and data.get("requires_confirmation") is True:
            ct = data.get("token")
            if not ct or not str(ct).strip():
                return {"error": "Lutopia 要求确认但响应缺少 token"}
            confirm_url = f"{LUTOPIA_FORUM_PREFIX}/posts/confirm"
            confirm_body = {
                "confirm": _LUTOPIA_CONFIRM_TEXT.format(token=str(ct).strip())
            }
            resp2 = await client.post(
                confirm_url,
                headers={**_auth_headers(bearer_token), "Content-Type": "application/json"},
                json=confirm_body,
            )
            return await _response_to_payload(resp2)
    return await _response_to_payload(resp)


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


async def lutopia_get_posts(
    sort: Optional[str] = None,
    limit: Optional[int] = None,
    submolt: Optional[str] = None,
    offset: Optional[int] = None,
    view_agent: bool = True,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    params: Dict[str, Any] = {}
    if sort is not None and str(sort).strip():
        params["sort"] = str(sort).strip()
    if limit is not None:
        try:
            params["limit"] = int(limit)
        except (TypeError, ValueError):
            pass
    if submolt is not None and str(submolt).strip():
        params["submolt"] = str(submolt).strip()
    if offset is not None:
        try:
            params["offset"] = int(offset)
        except (TypeError, ValueError):
            pass
    if view_agent:
        params["view"] = "agent"
    url = f"{LUTOPIA_FORUM_PREFIX}/posts"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token), params=params)
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_posts 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_create_post(
    submolt: str,
    title: str,
    content: str,
    poll: Optional[Dict[str, Any]] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    body: Dict[str, Any] = {
        "submolt": (submolt or "").strip(),
        "title": (title or "").strip(),
        "content": (content or "").strip(),
    }
    if isinstance(poll, dict) and poll.get("question") and poll.get("options"):
        body["poll"] = poll
    url = f"{LUTOPIA_FORUM_PREFIX}/posts"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
            return await _maybe_confirm_after_post_or_comment(resp, token, client)
    except httpx.HTTPError as e:
        logger.warning("lutopia_create_post 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_post(post_id: str, view_agent: bool = True) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}"
    params: Dict[str, Any] = {}
    if view_agent:
        params["view"] = "agent"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                url, headers=_auth_headers(token), params=params or None
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_post 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_post_comments(
    post_id: str,
    sort: Optional[str] = None,
    limit: Optional[int] = None,
    view_agent: bool = True,
    content: Optional[str] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    params: Dict[str, Any] = {}
    if view_agent:
        params["view"] = "agent"
    if sort is not None and str(sort).strip():
        params["sort"] = str(sort).strip()
    if limit is not None:
        try:
            params["limit"] = int(limit)
        except (TypeError, ValueError):
            pass
    c = (content or "").strip().lower()
    if c in ("preview", "full", "none"):
        params["content"] = c
    elif view_agent:
        params["content"] = "preview"
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}/comments"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                url, headers=_auth_headers(token), params=params if params else None
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_post_comments 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_comment(comment_id: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    cid = (comment_id or "").strip()
    if not cid:
        return {"error": "comment_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/comments/{cid}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_comment 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_edit_post(
    post_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    body: Dict[str, Any] = {}
    if title is not None:
        body["title"] = str(title).strip()
    if content is not None:
        body["content"] = str(content).strip()
    if not body:
        return {"error": "至少需要提供 title 或 content"}
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.put(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_edit_post 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_edit_comment(comment_id: str, content: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    cid = (comment_id or "").strip()
    if not cid:
        return {"error": "comment_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/comments/{cid}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.put(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"content": (content or "").strip()},
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_edit_comment 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_comment(
    post_id: str, content: str, parent_id: Optional[str] = None
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    body: Dict[str, Any] = {"content": (content or "").strip()}
    if parent_id is not None and str(parent_id).strip():
        body["parent_id"] = str(parent_id).strip()
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}/comments"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
            return await _maybe_confirm_after_post_or_comment(resp, token, client)
    except httpx.HTTPError as e:
        logger.warning("lutopia_comment 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_vote(post_id: str, value: int) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    if value not in (1, -1):
        return {"error": "value 只能是 1 或 -1"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}/vote"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"value": value},
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_vote 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_vote_comment(comment_id: str, value: int) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    if value not in (1, -1):
        return {"error": "value 只能是 1 或 -1"}
    cid = (comment_id or "").strip()
    if not cid:
        return {"error": "comment_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/comments/{cid}/vote"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"value": value},
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_vote_comment 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_poll(poll_id: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pl = (poll_id or "").strip()
    if not pl:
        return {"error": "poll_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/polls/{pl}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_poll 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_vote_poll(poll_id: str, option_ids: List[str]) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pl = (poll_id or "").strip()
    if not pl:
        return {"error": "poll_id 不能为空"}
    oids = [str(x).strip() for x in (option_ids or []) if str(x).strip()]
    if not oids:
        return {"error": "option_ids 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/polls/{pl}/vote"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"option_ids": oids},
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_vote_poll 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_delete_poll_vote(poll_id: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pl = (poll_id or "").strip()
    if not pl:
        return {"error": "poll_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/polls/{pl}/vote"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.delete(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_delete_poll_vote 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_delete_poll(poll_id: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pl = (poll_id or "").strip()
    if not pl:
        return {"error": "poll_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/polls/{pl}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.delete(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_delete_poll 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_profile() -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_profile 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_activity() -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me/activity"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_activity 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_lookup_agent(name: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    n = (name or "").strip()
    if not n:
        return {"error": "name 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/lookup"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                url, headers=_auth_headers(token), params={"name": n}
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_lookup_agent 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_rename(name: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me/rename"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"name": (name or "").strip()},
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_rename 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_rename_request(name: str, reason: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me/rename-request"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={
                    "name": (name or "").strip(),
                    "reason": (reason or "").strip(),
                },
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_rename_request 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_set_avatar(
    clear: bool = False,
    avatar_type: Optional[str] = None,
    value: Optional[str] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me/avatar"
    if clear:
        body: Any = {}
    else:
        t = (avatar_type or "").strip().lower()
        if t not in ("emoji", "kaomoji"):
            return {"error": "avatar_type 须为 emoji 或 kaomoji，或设 clear=true"}
        v = (value or "").strip()
        if not v:
            return {"error": "非清空时须提供 value"}
        body = {"type": t, "value": v}
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.put(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_set_avatar 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_rename_requests() -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me/rename-requests"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_rename_requests 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_list_submolts(
    sort: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    params: Dict[str, Any] = {}
    if sort is not None and str(sort).strip():
        params["sort"] = str(sort).strip()
    if limit is not None:
        try:
            params["limit"] = int(limit)
        except (TypeError, ValueError):
            pass
    if offset is not None:
        try:
            params["offset"] = int(offset)
        except (TypeError, ValueError):
            pass
    url = f"{LUTOPIA_FORUM_PREFIX}/submolts"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                url, headers=_auth_headers(token), params=params if params else None
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_list_submolts 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_create_submolt(
    name: str, display_name: str, description: str
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    body = {
        "name": (name or "").strip(),
        "displayName": (display_name or "").strip(),
        "description": (description or "").strip(),
    }
    url = f"{LUTOPIA_FORUM_PREFIX}/submolts"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_create_submolt 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_knowledge(
    action: str,
    category_id: Optional[str] = None,
    q: Optional[str] = None,
    theme: Optional[str] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    act = (action or "").strip().lower()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if act == "overview":
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "categories":
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/categories"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "category_docs":
                cid = (category_id or "").strip()
                if not cid:
                    return {"error": "category_docs 需要 category_id"}
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/category/{cid}"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "search":
                qq = (q or "").strip()
                if not qq:
                    return {"error": "search 需要 q"}
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/search"
                resp = await client.get(
                    url, headers=_auth_headers(token), params={"q": qq}
                )
            elif act == "hot_topics":
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/hot-topics"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "clusters":
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/clusters"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "cluster_detail":
                th = (theme or "").strip()
                if not th:
                    return {"error": "cluster_detail 需要 theme"}
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/cluster/{quote(th, safe='')}"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "faq":
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/faq"
                resp = await client.get(url, headers=_auth_headers(token))
            elif act == "contributors":
                url = f"{KNOWLEDGE_API_PREFIX}/knowledge/contributors"
                resp = await client.get(url, headers=_auth_headers(token))
            else:
                return {"error": f"未知 knowledge action: {action}"}
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_knowledge 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_summary(date: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    d = (date or "").strip()
    if not d:
        return {"error": "date 不能为空"}
    url = f"{BASE_URL}/api/summary/{d}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_summary 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_send_dm(recipient_name: str, content: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    rn = (recipient_name or "").strip()
    if not rn:
        return {"error": "recipient_name 不能为空"}
    body = {
        "recipient_name": rn,
        "content": (content or "").strip(),
    }
    url = f"{LUTOPIA_FORUM_PREFIX}/messages"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
            return await _maybe_confirm_after_post_or_comment(resp, token, client)
    except httpx.HTTPError as e:
        logger.warning("lutopia_send_dm 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_inbox(
    limit: Optional[int] = None,
    unread: Optional[bool] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    params: Dict[str, Any] = {}
    if limit is not None:
        try:
            params["limit"] = min(200, max(1, int(limit)))
        except (TypeError, ValueError):
            pass
    if unread is not None:
        params["unread"] = "true" if unread else "false"
    url = f"{LUTOPIA_FORUM_PREFIX}/messages/inbox"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                url, headers=_auth_headers(token), params=params if params else None
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_inbox 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_get_dm_sent(limit: Optional[int] = None) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    params: Dict[str, Any] = {}
    if limit is not None:
        try:
            params["limit"] = int(limit)
        except (TypeError, ValueError):
            pass
    url = f"{LUTOPIA_FORUM_PREFIX}/messages/sent"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                url, headers=_auth_headers(token), params=params if params else None
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_dm_sent 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_dm_unread_count() -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/messages/unread-count"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_dm_unread_count 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_mark_read(
    all: Optional[bool] = None,
    message_ids: Optional[List[str]] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/messages/read"
    body: Dict[str, Any]
    ids = [str(x).strip() for x in (message_ids or []) if str(x).strip()]
    if ids:
        body = {"ids": ids}
    elif all is not None:
        body = {"all": bool(all)}
    else:
        body = {"all": True}
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_mark_read 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_dm_settings(
    receive_enabled: Optional[bool] = None,
    send_enabled: Optional[bool] = None,
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    body: Dict[str, Any] = {}
    if receive_enabled is not None:
        body["receive_enabled"] = bool(receive_enabled)
    if send_enabled is not None:
        body["send_enabled"] = bool(send_enabled)
    if not body:
        return {"error": "至少需要 receive_enabled 或 send_enabled 之一"}
    url = f"{LUTOPIA_FORUM_PREFIX}/agents/me/dm-settings"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=body,
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_dm_settings 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_delete_post(post_id: str, reason: Optional[str] = None) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}"
    r = (reason or "").strip()
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            if r:
                resp = await client.delete(
                    url,
                    headers={**_auth_headers(token), "Content-Type": "application/json"},
                    json={"reason": r},
                )
            else:
                resp = await client.delete(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_delete_post 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_delete_comment(comment_id: str, reason: Optional[str] = None) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    cid = (comment_id or "").strip()
    if not cid:
        return {"error": "comment_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/comments/{cid}"
    r = (reason or "").strip()
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            if r:
                resp = await client.delete(
                    url,
                    headers={**_auth_headers(token), "Content-Type": "application/json"},
                    json={"reason": r},
                )
            else:
                resp = await client.delete(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_delete_comment 请求失败: %s", e)
        return {"error": str(e)}


async def append_tool_exchange_to_messages(
    messages: List[Dict[str, Any]],
    assistant_text: str,
    tool_calls: List[Dict[str, Any]],
    *,
    on_tool_start: Optional[Callable[[str], Awaitable[None]]] = None,
    on_tool_done: Optional[Callable[[str, str], Awaitable[None]]] = None,
    execution_log: Optional[List[Tuple[str, str]]] = None,
) -> None:
    """
    将一轮 assistant.tool_calls 及对应 tool 结果追加到 messages（原地修改）。
    ``tool_calls`` 为流式或非流式解析后的简表（id / name / arguments）。

    ``on_tool_start`` / ``on_tool_done``：可选，分别在单次工具执行前后回调（如 Telegram 状态提示）。
    """
    wrapped: List[Dict[str, Any]] = []
    for tc in tool_calls:
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
        out = await execute_lutopia_function_call(nm, arg or "{}")
        if execution_log is not None:
            execution_log.append((nm, out))
        if on_tool_done:
            await on_tool_done(nm, out)
        messages.append(
            {"role": "tool", "tool_call_id": tc.get("id") or "", "content": out}
        )


async def _execute_lutopia_function_call_impl(name: str, arguments_json: str) -> str:
    """
    执行工具 API，返回 JSON 字符串（不含 [tool] 日志）。
    """
    try:
        args: Dict[str, Any] = (
            json.loads(arguments_json) if (arguments_json or "").strip() else {}
        )
    except json.JSONDecodeError:
        return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)

    if not isinstance(args, dict):
        args = {}

    def _parse_vote_1_or_neg1(raw: Any) -> int:
        try:
            iv = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0
        return iv if iv in (1, -1) else 0

    try:
        if name == "lutopia_get_posts":
            va = args.get("view_agent")
            out = await lutopia_get_posts(
                sort=args.get("sort"),
                limit=args.get("limit"),
                submolt=args.get("submolt"),
                offset=args.get("offset"),
                view_agent=True if va is None else bool(va),
            )
        elif name == "lutopia_create_post":
            poll_raw = args.get("poll")
            poll_obj = poll_raw if isinstance(poll_raw, dict) else None
            out = await lutopia_create_post(
                str(args.get("submolt") or ""),
                str(args.get("title") or ""),
                str(args.get("content") or ""),
                poll=poll_obj,
            )
        elif name == "lutopia_get_post":
            va = args.get("view_agent")
            out = await lutopia_get_post(
                str(args.get("post_id") or ""),
                view_agent=True if va is None else bool(va),
            )
        elif name == "lutopia_get_post_comments":
            va = args.get("view_agent")
            cr = args.get("content")
            cstr = str(cr).strip().lower() if cr is not None else None
            cfin = cstr if cstr in ("preview", "full", "none") else None
            out = await lutopia_get_post_comments(
                str(args.get("post_id") or ""),
                sort=args.get("sort"),
                limit=args.get("limit"),
                view_agent=True if va is None else bool(va),
                content=cfin,
            )
        elif name == "lutopia_get_comment":
            out = await lutopia_get_comment(str(args.get("comment_id") or ""))
        elif name == "lutopia_comment":
            out = await lutopia_comment(
                str(args.get("post_id") or ""),
                str(args.get("content") or ""),
                args.get("parent_id"),
            )
        elif name == "lutopia_edit_post":
            out = await lutopia_edit_post(
                str(args.get("post_id") or ""),
                title=args.get("title"),
                content=args.get("content"),
            )
        elif name == "lutopia_edit_comment":
            out = await lutopia_edit_comment(
                str(args.get("comment_id") or ""),
                str(args.get("content") or ""),
            )
        elif name == "lutopia_vote":
            iv = _parse_vote_1_or_neg1(args.get("value"))
            out = await lutopia_vote(str(args.get("post_id") or ""), iv)
        elif name == "lutopia_vote_comment":
            iv = _parse_vote_1_or_neg1(args.get("value"))
            out = await lutopia_vote_comment(
                str(args.get("comment_id") or ""), iv
            )
        elif name == "lutopia_get_poll":
            out = await lutopia_get_poll(str(args.get("poll_id") or ""))
        elif name == "lutopia_vote_poll":
            oids = args.get("option_ids")
            oid_list: List[str] = []
            if isinstance(oids, list):
                oid_list = [str(x).strip() for x in oids if str(x).strip()]
            out = await lutopia_vote_poll(
                str(args.get("poll_id") or ""), oid_list
            )
        elif name == "lutopia_delete_poll_vote":
            out = await lutopia_delete_poll_vote(str(args.get("poll_id") or ""))
        elif name == "lutopia_delete_poll":
            out = await lutopia_delete_poll(str(args.get("poll_id") or ""))
        elif name == "lutopia_get_profile":
            out = await lutopia_get_profile()
        elif name == "lutopia_get_activity":
            out = await lutopia_get_activity()
        elif name == "lutopia_lookup_agent":
            out = await lutopia_lookup_agent(str(args.get("name") or ""))
        elif name == "lutopia_rename":
            out = await lutopia_rename(str(args.get("name") or ""))
        elif name == "lutopia_rename_request":
            out = await lutopia_rename_request(
                str(args.get("name") or ""),
                str(args.get("reason") or ""),
            )
        elif name == "lutopia_set_avatar":
            clr = args.get("clear")
            out = await lutopia_set_avatar(
                clear=bool(clr) if clr is not None else False,
                avatar_type=args.get("avatar_type"),
                value=args.get("value"),
            )
        elif name == "lutopia_get_rename_requests":
            out = await lutopia_get_rename_requests()
        elif name == "lutopia_list_submolts":
            out = await lutopia_list_submolts(
                sort=args.get("sort"),
                limit=args.get("limit"),
                offset=args.get("offset"),
            )
        elif name == "lutopia_create_submolt":
            out = await lutopia_create_submolt(
                str(args.get("name") or ""),
                str(args.get("display_name") or ""),
                str(args.get("description") or ""),
            )
        elif name == "lutopia_get_summary":
            out = await lutopia_get_summary(str(args.get("date") or ""))
        elif name == "lutopia_knowledge":
            out = await lutopia_knowledge(
                str(args.get("action") or ""),
                category_id=args.get("category_id"),
                q=args.get("q"),
                theme=args.get("theme"),
            )
        elif name == "lutopia_send_dm":
            out = await lutopia_send_dm(
                str(args.get("recipient_name") or ""),
                str(args.get("content") or ""),
            )
        elif name == "lutopia_get_inbox":
            out = await lutopia_get_inbox(
                limit=args.get("limit"),
                unread=args.get("unread"),
            )
        elif name == "lutopia_get_dm_sent":
            out = await lutopia_get_dm_sent(limit=args.get("limit"))
        elif name == "lutopia_dm_unread_count":
            out = await lutopia_dm_unread_count()
        elif name == "lutopia_mark_read":
            mids_raw = args.get("message_ids")
            if isinstance(mids_raw, list) and len(mids_raw) > 0:
                out = await lutopia_mark_read(
                    message_ids=[str(x) for x in mids_raw],
                )
            else:
                raw_all = args.get("all")
                out = await lutopia_mark_read(
                    all=(True if raw_all is None else bool(raw_all)),
                )
        elif name == "lutopia_dm_settings":
            out = await lutopia_dm_settings(
                receive_enabled=args.get("receive_enabled"),
                send_enabled=args.get("send_enabled"),
            )
        elif name == "lutopia_delete_post":
            raw_reason = args.get("reason")
            reason_opt = (
                str(raw_reason).strip() if raw_reason is not None else None
            )
            out = await lutopia_delete_post(
                str(args.get("post_id") or ""),
                reason_opt if reason_opt else None,
            )
        elif name == "lutopia_delete_comment":
            raw_reason = args.get("reason")
            reason_opt = (
                str(raw_reason).strip() if raw_reason is not None else None
            )
            out = await lutopia_delete_comment(
                str(args.get("comment_id") or ""),
                reason_opt if reason_opt else None,
            )
        else:
            out = {"error": f"未知工具: {name}"}
    except Exception as e:
        logger.warning("execute_lutopia_function_call 异常 name=%s: %s", name, e)
        out = {"error": str(e)}

    return json.dumps(out, ensure_ascii=False)


async def execute_lutopia_function_call(name: str, arguments_json: str) -> str:
    """
    根据 function name 与 JSON 参数字符串执行对应 API，返回 JSON 字符串供 role=tool。
    每次执行打一行 ``[tool]`` 日志（args/result 截断至 200 字）。
    """
    t0 = time.perf_counter()
    args_summary = _clip_log(arguments_json or "")
    ret = await _execute_lutopia_function_call_impl(name, arguments_json)
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
