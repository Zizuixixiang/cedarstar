"""
表情包：向量检索与 Telegram 发送。

send_meme 需传入 Telegram Bot 与 chat_id；调度由 bot 层在解析 [meme:…] 标记后调用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from memory.meme_store import (
    get_meme_store,
    siliconflow_embed_text,
    siliconflow_embed_text_async,
)

logger = logging.getLogger(__name__)


def search_meme(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """
    将 query 经硅基流动 BAAI/bge-m3 向量化后，在 Chroma `meme_pack` 中检索。

    Returns:
        [{id, name, url, is_animated}, ...]
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        k = max(1, int(top_k))
    except (TypeError, ValueError):
        k = 3
    try:
        vec = siliconflow_embed_text(q)
        rows = get_meme_store().search_by_vector(vec, top_k=k)
    except Exception as e:
        logger.warning("search_meme 失败: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            isa = int(row.get("is_animated", 0))
        except (TypeError, ValueError):
            isa = 0
        rid = row.get("id")
        out.append(
            {
                "id": rid,
                "name": row.get("name") or "",
                "url": row.get("url") or "",
                "is_animated": isa,
            }
        )
    return out


async def search_meme_async(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """
    异步检索表情包（在主事件循环中调用，可正确 await 库内 Embedding 配置）。
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        k = max(1, int(top_k))
    except (TypeError, ValueError):
        k = 3
    try:
        vec = await siliconflow_embed_text_async(q)
        rows = get_meme_store().search_by_vector(vec, top_k=k)
    except Exception as e:
        logger.warning("search_meme_async 失败: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            isa = int(row.get("is_animated", 0))
        except (TypeError, ValueError):
            isa = 0
        rid = row.get("id")
        out.append(
            {
                "id": rid,
                "name": row.get("name") or "",
                "url": row.get("url") or "",
                "is_animated": isa,
            }
        )
    return out


async def send_meme(url: str, is_animated: int, bot: Any, chat_id: int) -> Any:
    """
    is_animated=1 → send_animation；0 → send_photo（python-telegram-bot 风格 Bot）。
    """
    u = (url or "").strip()
    if not u:
        raise ValueError("url 不能为空")
    try:
        isa = int(is_animated)
    except (TypeError, ValueError):
        isa = 0
    if isa == 1:
        return await bot.send_animation(chat_id=chat_id, animation=u)
    return await bot.send_photo(chat_id=chat_id, photo=u)
