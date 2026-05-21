"""REST API for CedarStar custom MCP server management."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from memory.database import (
    create_mcp_server,
    delete_mcp_server,
    get_mcp_server,
    get_mcp_tool,
    list_mcp_servers,
    list_mcp_tools,
    toggle_mcp_server,
    toggle_mcp_tool_approval,
    toggle_mcp_tool_enabled,
    update_mcp_server,
)
from tools.custom_mcp import sync_tools_from_server

router = APIRouter()

_TRANSPORTS = {"sse", "streamable_http"}


class McpServerCreate(BaseModel):
    name: str
    transport: str
    url: str
    headers: str = ""
    enabled: int = 1
    trigger_keywords: List[str] = []
    allow_idle: bool = False


class McpServerUpdate(BaseModel):
    name: Optional[str] = None
    transport: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[str] = None
    enabled: Optional[int] = None
    trigger_keywords: Optional[List[str]] = None
    allow_idle: Optional[bool] = None


def _validate_transport(value: str) -> str:
    transport = (value or "").strip().lower()
    if transport not in _TRANSPORTS:
        raise HTTPException(status_code=400, detail="transport must be sse or streamable_http")
    return transport


def _normalize_headers_for_store(value: Optional[str], *, allow_empty: bool = True) -> Optional[str]:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return "" if allow_empty else None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="headers must be a JSON object string")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="headers must be a JSON object string")
    return json.dumps(parsed, ensure_ascii=False)


def _normalize_keywords_for_store(value: Optional[List[str]]) -> Optional[str]:
    if not value:
        return None
    out: List[str] = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return json.dumps(out, ensure_ascii=False) if out else None


def _keywords_from_store(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(str(value))
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _mask_server(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["headers"] = ""
    out["trigger_keywords"] = _keywords_from_store(out.get("trigger_keywords"))
    out["allow_idle"] = bool(int(out.get("allow_idle") or 0))
    return out


@router.get("/servers")
async def api_list_mcp_servers():
    rows = await list_mcp_servers(enabled_only=False)
    return [_mask_server(r) for r in rows]


@router.post("/servers")
async def api_create_mcp_server(payload: McpServerCreate):
    name = (payload.name or "").strip()
    url = (payload.url or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    row = await create_mcp_server(
        name=name,
        transport=_validate_transport(payload.transport),
        url=url,
        headers=_normalize_headers_for_store(payload.headers),
        enabled=1 if int(payload.enabled or 0) else 0,
        trigger_keywords=_normalize_keywords_for_store(payload.trigger_keywords),
        allow_idle=1 if payload.allow_idle else 0,
    )
    return _mask_server(row)


@router.put("/servers/{server_id}")
async def api_update_mcp_server(server_id: str, payload: McpServerUpdate):
    existing = await get_mcp_server(server_id)
    if not existing:
        raise HTTPException(status_code=404, detail="server not found")

    update: Dict[str, Any] = {"update_headers": False}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        update["name"] = name
    if payload.transport is not None:
        update["transport"] = _validate_transport(payload.transport)
    if payload.url is not None:
        url = payload.url.strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        update["url"] = url
    if payload.headers is not None and payload.headers.strip():
        update["headers"] = _normalize_headers_for_store(payload.headers)
        update["update_headers"] = True
    if payload.enabled is not None:
        update["enabled"] = 1 if int(payload.enabled or 0) else 0
    if payload.trigger_keywords is not None:
        update["trigger_keywords"] = _normalize_keywords_for_store(payload.trigger_keywords)
        update["update_trigger_keywords"] = True
    if payload.allow_idle is not None:
        update["allow_idle"] = 1 if payload.allow_idle else 0

    row = await update_mcp_server(server_id, **update)
    if not row:
        raise HTTPException(status_code=404, detail="server not found")
    return _mask_server(row)


@router.delete("/servers/{server_id}")
async def api_delete_mcp_server(server_id: str):
    deleted = await delete_mcp_server(server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="server not found")
    return {"ok": True}


@router.patch("/servers/{server_id}/toggle")
async def api_toggle_mcp_server(server_id: str):
    row = await toggle_mcp_server(server_id)
    if not row:
        raise HTTPException(status_code=404, detail="server not found")
    return _mask_server(row)


@router.post("/servers/{server_id}/sync")
async def api_sync_mcp_server(server_id: str):
    try:
        rows = await sync_tools_from_server(server_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"sync failed: {e}")
    return {"ok": True, "tools": rows, "count": len(rows)}


@router.get("/servers/{server_id}/tools")
async def api_list_mcp_server_tools(server_id: str):
    if not await get_mcp_server(server_id):
        raise HTTPException(status_code=404, detail="server not found")
    return await list_mcp_tools(server_id=server_id, enabled_only=False)


@router.patch("/tools/{tool_id}/toggle")
async def api_toggle_mcp_tool(tool_id: str):
    if not await get_mcp_tool(tool_id):
        raise HTTPException(status_code=404, detail="tool not found")
    row = await toggle_mcp_tool_enabled(tool_id)
    return row


@router.patch("/tools/{tool_id}/approval")
async def api_toggle_mcp_tool_approval(tool_id: str):
    if not await get_mcp_tool(tool_id):
        raise HTTPException(status_code=404, detail="tool not found")
    row = await toggle_mcp_tool_approval(tool_id)
    return row
