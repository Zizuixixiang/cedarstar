"""
业务 SSE 通道（/api/stream）。

注意：本模块与 MCP SSE（/mcp/memory/...）完全独立。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()


class EventType(Enum):
    STATUS_UPDATE = "status_update"
    CONNECTION_UPDATE = "connection_update"
    CHAT_MSG = "chat_msg"
    TOOL_PENDING_APPROVAL = "tool_pending_approval"


_subscribers: set[asyncio.Queue[Dict[str, Any]]] = set()
_subscribers_lock = asyncio.Lock()

_status_state_lock = asyncio.Lock()
_status_state: Dict[str, Any] = {
    "pocketMoney": None,
}


async def _safe_get_current_pocket_money() -> float:
    try:
        from memory.database import get_current_pocket_money_balance

        return float(await get_current_pocket_money_balance())
    except Exception as e:
        logger.warning("读取当前 pocketMoney 失败，回退 0.0: %s", e)
        return 0.0


async def _build_status_update_payload(partial_payload: Dict[str, Any]) -> Dict[str, Any]:
    async with _status_state_lock:
        if "pocketMoney" in partial_payload:
            _status_state["pocketMoney"] = float(partial_payload["pocketMoney"])
        elif _status_state.get("pocketMoney") is None:
            _status_state["pocketMoney"] = await _safe_get_current_pocket_money()

        # TODO: 待全局状态源接入后替换占位实现，这里暂时固定为 neutral。
        emotion = str(partial_payload.get("emotion", "neutral"))
        # TODO: 待全局状态源接入后替换占位实现，这里暂时固定为 default。
        current_mode = str(partial_payload.get("currentMode", "default"))
        pocket_money = float(_status_state.get("pocketMoney") or 0.0)

    return {
        "pocketMoney": pocket_money,
        "emotion": emotion,
        "currentMode": current_mode,
    }


async def publish_event(event_type: EventType, partial_payload: Dict[str, Any]) -> None:
    payload = partial_payload
    if event_type is EventType.STATUS_UPDATE:
        payload = await _build_status_update_payload(partial_payload)
    else:
        # TODO: 预留其他事件类型的 payload 规范与聚合逻辑。
        payload = dict(partial_payload or {})

    event = {
        "eventType": event_type.value,
        "payload": payload,
        "timestamp": datetime.utcnow().isoformat(),
    }

    async with _subscribers_lock:
        queues = list(_subscribers)
    for queue in queues:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)


async def _subscribe() -> asyncio.Queue[Dict[str, Any]]:
    queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=100)
    async with _subscribers_lock:
        _subscribers.add(queue)
    return queue


async def _unsubscribe(queue: asyncio.Queue[Dict[str, Any]]) -> None:
    async with _subscribers_lock:
        _subscribers.discard(queue)


@router.get("")
async def stream_events():
    queue = await _subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {item['eventType']}\n"
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            await _unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
