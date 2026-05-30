"""Internal mail tools exposed to the OpenAI-compatible tool loop."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from tools.memory_tools import _api_get, _headers, _json_text, MEMORY_API_BASE_URL, MEMORY_TOOL_TIMEOUT

import httpx

logger = logging.getLogger(__name__)


OPENAI_MAIL_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_mail_contacts",
            "description": "查看笔友列表，返回每位笔友的名字、邮箱和备注。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_mail",
            "description": (
                "读取邮件往来。可传 contact_email 查看某个笔友；不传则返回所有往来。"
                "返回按时间升序合并的收件箱/发件箱记录，最近 recent_n 封含原文，更早只含摘要。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_email": {
                        "type": "string",
                        "description": "笔友邮箱，可省略",
                    },
                    "recent_n": {
                        "type": "integer",
                        "description": "最近多少封返回原文，默认 3",
                        "default": 3,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_mail",
            "description": (
                "写一封邮件并提交给南杉审批。审批通过后才会通过 Resend 发出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_addr": {"type": "string", "description": "收件人邮箱"},
                    "to_name": {"type": "string", "description": "收件人名字，可省略"},
                    "subject": {"type": "string", "description": "邮件主题"},
                    "body": {"type": "string", "description": "邮件正文"},
                },
                "required": ["to_addr", "body"],
            },
        },
    },
]


async def execute_list_mail_contacts(arguments: Dict[str, Any]) -> str:
    try:
        raw = await _api_get("/mail/contacts")
        return raw
    except Exception as e:
        logger.warning("list_mail_contacts failed: %s", e)
        return _json_text({"error": str(e)})


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_MAIL_RESULT_MAX_CHARS = 10000

async def execute_read_mail(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    recent_n = max(0, min(_safe_int(args.get("recent_n"), 3), 20))
    params: Dict[str, Any] = {"limit": 500}
    contact = str(args.get("contact_email") or "").strip()
    if contact:
        params["contact_email"] = contact
    raw = await _api_get("/mail/thread", params)
    try:
        payload = json.loads(raw)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return raw
        # 按时间升序（旧→新），从最新的往前遍历
        rows_sorted = sorted(
            [r for r in rows if isinstance(r, dict)],
            key=lambda r: str(r.get("happened_at") or ""),
        )
        total = len(rows_sorted)
        items = []
        char_count = 0
        for i in range(total - 1, -1, -1):
            row = rows_sorted[i]
            idx_from_end = total - 1 - i  # 0 = 最新
            item: Dict[str, Any] = {
                "id": row.get("id"),
                "direction": row.get("direction"),
                "contact_addr": row.get("contact_addr"),
                "contact_name": row.get("contact_name"),
                "subject": row.get("subject"),
                "time": row.get("happened_at"),
            }
            if idx_from_end < recent_n:
                item["body"] = row.get("body") or ""
                item["summary"] = row.get("summary") or ""
            else:
                item["summary"] = row.get("summary") or ""
            item_chars = len(str(item.get("body") or "")) + len(str(item.get("summary") or ""))
            if char_count + item_chars > _MAIL_RESULT_MAX_CHARS and items:
                break
            char_count += item_chars
            items.append(item)
        # 反转为时间升序（旧→新）
        items.reverse()
        return _json_text({"success": True, "data": items})
    except Exception as e:
        logger.warning("read_mail result shaping failed: %s", e)
        return raw


async def execute_send_mail(arguments: Dict[str, Any]) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    to_addr = str(args.get("to_addr") or "").strip()
    body = str(args.get("body") or "").strip()
    if not to_addr:
        return _json_text({"error": "to_addr 不能为空"})
    if not body:
        return _json_text({"error": "body 不能为空"})
    payload = {
        "to_addr": to_addr,
        "to_name": str(args.get("to_name") or "").strip() or None,
        "subject": str(args.get("subject") or "").strip(),
        "body": body,
    }
    try:
        async with httpx.AsyncClient(timeout=MEMORY_TOOL_TIMEOUT) as client:
            resp = await client.post(
                f"{MEMORY_API_BASE_URL}/mail/outbox",
                headers={**_headers(), "Content-Type": "application/json"},
                json=payload,
            )
        if not resp.is_success:
            return _json_text({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"})
        return _json_text(resp.json())
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.warning("send_mail failed: %s", e)
        return _json_text({"error": str(e)})
