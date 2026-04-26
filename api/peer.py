"""
Peer-to-peer runtime hooks between CedarStar/CedarClio instances.

Routes are mounted under /api and inherit the same X-Cedarstar-Token auth.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class PeerGroupMessage(BaseModel):
    sender_app_id: str
    sender_bot_id: Optional[str] = None
    sender_name: Optional[str] = None
    chat_id: str
    message_id: str
    content: str
    character_id: Optional[str] = None
    thinking: Optional[str] = None


@router.post("/group-message")
async def receive_peer_group_message(payload: PeerGroupMessage) -> Dict[str, Any]:
    from bot.telegram_bot import handle_peer_group_message

    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    result = await handle_peer_group_message(data)
    return {"success": True, "data": result, "message": "ok"}
