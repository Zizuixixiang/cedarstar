"""
Peer-to-peer runtime hooks between CedarStar/CedarClio instances.

Routes are mounted under /api and inherit the same X-Cedarstar-Token auth.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class PeerGroupMessage(BaseModel):
    sender_app_id: str
    chat_id: str
    round_count: Optional[int] = None
    tg_message_id: Optional[str] = None


@router.post("/group-message")
async def receive_peer_group_message(payload: PeerGroupMessage) -> Dict[str, Any]:
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()

    async def _run() -> None:
        from bot.telegram_bot import handle_peer_group_message

        try:
            result = await handle_peer_group_message(data)
            logger.info("peer relay processed: %s", result)
        except Exception as e:
            logger.warning("peer relay processing failed: %s", e)

    asyncio.create_task(_run())
    return {"success": True, "data": {"status": "accepted"}, "message": "queued"}
