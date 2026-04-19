"""
后台告警：使用与主流程相同的 Bot Token 与代理，向主用户私聊发送文本。
不依赖 Application 生命周期；发送失败仅记 log，不向外抛。
"""

from __future__ import annotations

import logging

from config import config

logger = logging.getLogger(__name__)


async def send_telegram_main_user_text(text: str) -> None:
    """
    向 ``TELEGRAM_MAIN_USER_CHAT_ID`` 发送纯文本（无 parse_mode）。
    未配置 token 或 chat_id 时静默跳过。
    """
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    raw_chat = config.TELEGRAM_MAIN_USER_CHAT_ID
    chat_id_s = (raw_chat or "").strip() if raw_chat else ""
    if not token or not chat_id_s:
        logger.debug("跳过 Telegram 告警：未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_MAIN_USER_CHAT_ID")
        return
    try:
        chat_id = int(chat_id_s)
    except ValueError:
        logger.warning("TELEGRAM_MAIN_USER_CHAT_ID 非法，跳过告警: %r", chat_id_s)
        return

    try:
        from telegram import Bot
        from telegram.request import HTTPXRequest

        def _req() -> HTTPXRequest:
            return HTTPXRequest(
                connect_timeout=25.0,
                read_timeout=60.0,
                write_timeout=60.0,
                proxy=config.TELEGRAM_PROXY,
                httpx_kwargs={"trust_env": False},
            )

        bot = Bot(token=token, request=_req())
        await bot.initialize()
        try:
            await bot.send_message(chat_id=chat_id, text=text[:4096])
        finally:
            await bot.shutdown()
    except Exception as e:
        logger.warning("Telegram 告警发送失败: %s", e)
