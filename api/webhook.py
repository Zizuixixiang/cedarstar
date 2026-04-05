"""
Telegram Bot API webhook：供公网 HTTPS 接收更新，不经 /api 前缀（便于独立 Access 策略）。
"""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from config import config

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != config.TELEGRAM_WEBHOOK_SECRET:
        logger.warning(
            "Telegram webhook 拒绝：Secret-Token 与 TELEGRAM_WEBHOOK_SECRET 不一致或未配置"
        )
        raise HTTPException(status_code=401, detail="Unauthorized")
    update_data = await request.json()
    uid = update_data.get("update_id")
    logger.info("Telegram webhook 已接收 update_id=%s", uid)
    background_tasks.add_task(process_update_task, update_data)
    return {"ok": True}


async def process_update_task(update_data: dict) -> None:
    from bot.telegram_bot import process_update

    await process_update(update_data)
