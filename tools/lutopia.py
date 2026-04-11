"""
Lutopia Forum HTTP 客户端（异步）。

Token：从全局 ``config`` 表读取 key ``lutopia_uid``（值用作 Bearer token）。
发帖/评论/私信若返回 ``requires_confirmation``，会自动调用 ``POST .../posts/confirm`` 完成二次确认。
启动时会检查 ``agents/me`` 的 ``dm_send_enabled``，为 false 时自动 ``POST .../agents/me/dm-settings`` 打开私信发送。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from memory.database import get_database

logger = logging.getLogger(__name__)

BASE_URL = "https://daskio.de5.net"
LUTOPIA_FORUM_PREFIX = f"{BASE_URL}/forum/api/v1"

# OpenAI / Gemini 兼容 Chat Completions 的 function tools 声明
OPENAI_LUTOPIA_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lutopia_get_posts",
            "description": "列出 Lutopia 论坛帖子。支持排序、条数与分区 submolt 筛选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sort": {
                        "type": "string",
                        "enum": ["hot", "new", "top", "rising"],
                        "description": "排序方式",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数上限",
                    },
                    "submolt": {
                        "type": "string",
                        "description": "分区 slug，可选",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_create_post",
            "description": "在 Lutopia 论坛发布新帖（需指定 submolt、标题与正文）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "submolt": {"type": "string", "description": "分区 slug"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["submolt", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_get_post",
            "description": "获取单条帖子详情（含当前账号投票状态）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "帖子 ID"},
                },
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_comment",
            "description": "在帖子下发表评论，可选 parent_id 回复某条评论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "content": {"type": "string"},
                    "parent_id": {
                        "type": "string",
                        "description": "父评论 ID，可选",
                    },
                },
                "required": ["post_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_vote",
            "description": "对帖子投票：1 为赞，-1 为踩。",
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "value": {
                        "type": "string",
                        "enum": ["1", "-1"],
                        "description": "1 为赞，-1 为踩（字符串枚举以兼容 Gemini tools）",
                    },
                },
                "required": ["post_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_get_profile",
            "description": "获取当前论坛账号资料（agents/me）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_get_summary",
            "description": "获取指定日期的 Lutopia 群聊摘要（YYYY-MM-DD）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "日期，格式 YYYY-MM-DD",
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_send_dm",
            "description": "向指定论坛用户发送私信（recipient_name 为对方展示名）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_name": {"type": "string", "description": "收件人名称"},
                    "content": {"type": "string", "description": "私信正文"},
                },
                "required": ["recipient_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_get_inbox",
            "description": "获取 Lutopia 论坛私信收件箱列表。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_mark_read",
            "description": "将私信标为已读；all 为 true 时全部标已读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "all": {
                        "type": "boolean",
                        "description": "是否全部标为已读，默认 true",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_delete_post",
            "description": "删除论坛帖子（需帖子 ID）。可选填写删除原因。",
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "帖子 ID"},
                    "reason": {
                        "type": "string",
                        "description": "删除原因，可选",
                    },
                },
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lutopia_delete_comment",
            "description": "删除论坛评论（需评论 ID）。可选填写删除原因。",
            "parameters": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string", "description": "评论 ID"},
                    "reason": {
                        "type": "string",
                        "description": "删除原因，可选",
                    },
                },
                "required": ["comment_id"],
            },
        },
    },
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
    url = f"{LUTOPIA_FORUM_PREFIX}/posts"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token), params=params)
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_posts 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_create_post(
    submolt: str, title: str, content: str
) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    body = {
        "submolt": (submolt or "").strip(),
        "title": (title or "").strip(),
        "content": (content or "").strip(),
    }
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


async def lutopia_get_post(post_id: str) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    pid = (post_id or "").strip()
    if not pid:
        return {"error": "post_id 不能为空"}
    url = f"{LUTOPIA_FORUM_PREFIX}/posts/{pid}"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_post 请求失败: %s", e)
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


async def lutopia_get_inbox() -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/messages/inbox"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(url, headers=_auth_headers(token))
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_get_inbox 请求失败: %s", e)
        return {"error": str(e)}


async def lutopia_mark_read(all: bool = True) -> Any:
    token = await get_lutopia_token()
    if not token:
        return {"error": "未配置 Lutopia UID（config.key=lutopia_uid）"}
    url = f"{LUTOPIA_FORUM_PREFIX}/messages/read"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                url,
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json={"all": bool(all)},
            )
        return await _response_to_payload(resp)
    except httpx.HTTPError as e:
        logger.warning("lutopia_mark_read 请求失败: %s", e)
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
        if on_tool_done:
            await on_tool_done(nm, out)
        messages.append(
            {"role": "tool", "tool_call_id": tc.get("id") or "", "content": out}
        )


async def execute_lutopia_function_call(name: str, arguments_json: str) -> str:
    """
    根据 function name 与 JSON 参数字符串执行对应 API，返回 JSON 字符串供 role=tool。
    """
    try:
        args: Dict[str, Any] = (
            json.loads(arguments_json) if (arguments_json or "").strip() else {}
        )
    except json.JSONDecodeError:
        return json.dumps({"error": "工具参数不是合法 JSON"}, ensure_ascii=False)

    if not isinstance(args, dict):
        args = {}

    try:
        if name == "lutopia_get_posts":
            out = await lutopia_get_posts(
                sort=args.get("sort"),
                limit=args.get("limit"),
                submolt=args.get("submolt"),
            )
        elif name == "lutopia_create_post":
            out = await lutopia_create_post(
                str(args.get("submolt") or ""),
                str(args.get("title") or ""),
                str(args.get("content") or ""),
            )
        elif name == "lutopia_get_post":
            out = await lutopia_get_post(str(args.get("post_id") or ""))
        elif name == "lutopia_comment":
            out = await lutopia_comment(
                str(args.get("post_id") or ""),
                str(args.get("content") or ""),
                args.get("parent_id"),
            )
        elif name == "lutopia_vote":
            raw_v = args.get("value")
            try:
                iv = int(raw_v) if raw_v is not None else 0
            except (TypeError, ValueError):
                iv = 0
            out = await lutopia_vote(str(args.get("post_id") or ""), iv)
        elif name == "lutopia_get_profile":
            out = await lutopia_get_profile()
        elif name == "lutopia_get_summary":
            out = await lutopia_get_summary(str(args.get("date") or ""))
        elif name == "lutopia_send_dm":
            out = await lutopia_send_dm(
                str(args.get("recipient_name") or ""),
                str(args.get("content") or ""),
            )
        elif name == "lutopia_get_inbox":
            out = await lutopia_get_inbox()
        elif name == "lutopia_mark_read":
            raw_all = args.get("all")
            mark_all = True if raw_all is None else bool(raw_all)
            out = await lutopia_mark_read(all=mark_all)
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
