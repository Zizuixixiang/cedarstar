"""
助手回复中的长期记忆引用：解析 [[used:uid]]、异步更新 Chroma hits、清洗后再存库/发送。
"""

import asyncio
import logging
import re
from typing import Set

from memory.vector_store import update_memory_hits

logger = logging.getLogger(__name__)

_USED_UID_PATTERN = re.compile(r"\[\[used:(.*?)\]\]")


def collect_used_memory_uids(reply_text: str) -> Set[str]:
    """用正则提取所有 uid，去重（空串不计）。"""
    out: Set[str] = set()
    if not isinstance(reply_text, str):
        return out
    for m in _USED_UID_PATTERN.finditer(reply_text):
        uid = (m.group(1) or "").strip()
        if uid:
            out.add(uid)
    return out


def strip_used_memory_tags(reply_text: str) -> str:
    """移除 [[used:...]] 标记。"""
    if not isinstance(reply_text, str):
        return reply_text  # type: ignore[return-value]
    return re.sub(r"\[\[used:.*?\]\]", "", reply_text)


def schedule_update_memory_hits_and_clean_reply(reply_text: str) -> str:
    """
    须在已有运行中的 asyncio 事件循环内调用。
    若提取到 uid，则 fire-and-forget 在线程池中执行 update_memory_hits；
    返回去掉引用标记后的正文（用于存库与发送）。
    """
    if not isinstance(reply_text, str):
        return reply_text
    uids = collect_used_memory_uids(reply_text)
    if uids:
        uid_list = list(uids)

        async def _run() -> None:
            try:
                await asyncio.to_thread(update_memory_hits, uid_list)
            except Exception as e:
                logger.warning("update_memory_hits 异步执行失败: %s", e)

        asyncio.create_task(_run())
    return strip_used_memory_tags(reply_text)
