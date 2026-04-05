"""
Discord / Telegram 共用的消息缓冲实现。

同一 session 在 buffer_delay 秒内合并多条消息，超时后通过回调交给各平台
完成「正在输入 / 生成 / 发送」等平台相关逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# 贴纸识图 / 语音转录等在入队前可能远超 buffer_delay；flush 前最多再等这么久（秒）
_BUFFER_HEAVY_WAIT_CAP_S = 180.0
_BUFFER_HEAVY_POLL_S = 0.25

logger = logging.getLogger(__name__)


def aggregate_buffer_entries(
    buffer_messages: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]], str]:
    """
    合并缓冲条目：生成落库用 combined_content、image_payload 列表、以及多模态请求用纯文本。
    """
    text_parts: List[str] = []
    images: List[Dict[str, Any]] = []

    for e in buffer_messages:
        raw = (e.get("content") or "").strip()
        if raw:
            text_parts.append(raw)
        ips = list(e.get("image_payloads") or [])
        if e.get("image_payload"):
            ips.append(e["image_payload"])
        for ip in ips:
            images.append(ip)
            cap = (ip.get("caption") or "").strip()
            if cap:
                text_parts.append(cap)

    if images:
        body = "\n".join(text_parts).strip()
        combined = f"[发送了{len(images)}张图片]" + (f" {body}" if body else "")
    else:
        combined = "\n".join(text_parts).strip()

    text_for_llm = "\n".join(text_parts).strip()
    if images and not text_for_llm:
        text_for_llm = "请结合用户发送的图片进行理解和回复。"

    return combined, images, text_for_llm


def ordered_media_type_from_buffer(buffer_messages: List[Dict[str, Any]]) -> Optional[str]:
    """
    按 buffer 条目顺序遍历，收集 image / sticker / voice，去重且保留首次出现顺序。
    reaction 等不入此字符串，由平台单独落库。
    """
    media_types: List[str] = []
    for e in buffer_messages:
        ips = list(e.get("image_payloads") or [])
        if e.get("image_payload"):
            ips.append(e["image_payload"])
        has_img = bool(ips)
        if has_img and "image" not in media_types:
            media_types.append("image")
        if e.get("from_sticker") and "sticker" not in media_types:
            media_types.append("sticker")
        if e.get("from_voice") and "voice" not in media_types:
            media_types.append("voice")
    return ",".join(media_types) if media_types else None


# (session_id, combined_content, images, buffer_messages, text_for_llm) -> None
BufferFlushCallback = Callable[
    [str, str, List[Dict[str, Any]], List[Dict[str, Any]], str],
    Awaitable[None],
]


class MessageBuffer:
    """
    消息缓冲区：按 session_id 聚合条目，防抖定时器到期后调用 flush_callback。
    """

    def __init__(
        self,
        flush_callback: BufferFlushCallback,
        *,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._flush_callback = flush_callback
        self._log = log or logger
        self.message_buffers: Dict[str, List[Dict[str, Any]]] = {}
        self.buffer_timers: Dict[str, asyncio.Task] = {}
        self.buffer_locks: Dict[str, asyncio.Lock] = {}
        # 同一 session 上「入队前慢处理」计数（贴纸 vision、语音 STT、图片下载等）
        self._heavy_pending: Dict[str, int] = {}

    def begin_heavy(self, session_id: str) -> None:
        """慢处理开始前调用，flush 会等到对应 ``end_heavy`` 再取队列（有上限）。"""
        self._heavy_pending[session_id] = self._heavy_pending.get(session_id, 0) + 1

    def end_heavy(self, session_id: str) -> None:
        """与 ``begin_heavy`` 成对；须在 ``add_to_buffer`` 完成之后调用。"""
        n = max(0, self._heavy_pending.get(session_id, 0) - 1)
        if n == 0:
            self._heavy_pending.pop(session_id, None)
        else:
            self._heavy_pending[session_id] = n

    async def _wait_heavy_done(self, session_id: str) -> None:
        """buffer_delay 已到后，若仍有慢处理，短暂轮询等待以免只 flush 到先入库的图片。"""
        t0 = time.monotonic()
        while self._heavy_pending.get(session_id, 0) > 0:
            if time.monotonic() - t0 > _BUFFER_HEAVY_WAIT_CAP_S:
                self._log.warning(
                    "缓冲等待慢处理超时 session=%s cap=%ss，可能仍拆分本轮 flush",
                    session_id,
                    int(_BUFFER_HEAVY_WAIT_CAP_S),
                )
                break
            await asyncio.sleep(_BUFFER_HEAVY_POLL_S)

    async def add_to_buffer(self, session_id: str, entry: Dict[str, Any]) -> None:
        """追加一条缓冲条目（``content`` 与/或 ``image_payload`` / ``image_payloads``），并启动/重置定时任务。"""
        if session_id not in self.buffer_locks:
            self.buffer_locks[session_id] = asyncio.Lock()

        async with self.buffer_locks[session_id]:
            if session_id not in self.message_buffers:
                self.message_buffers[session_id] = []

            self.message_buffers[session_id].append(entry)

            self._log.debug(
                "消息添加到缓冲区: session_id=%s, 缓冲区大小=%s",
                session_id,
                len(self.message_buffers[session_id]),
            )

            if session_id in self.buffer_timers:
                self.buffer_timers[session_id].cancel()
                self._log.debug("取消现有定时器: session_id=%s", session_id)

            self.buffer_timers[session_id] = asyncio.create_task(
                self._process_buffer(session_id)
            )

    async def _process_buffer(self, session_id: str) -> None:
        """等待 buffer_delay 后取出缓冲区，合并 content 并调用 flush_callback。"""
        try:
            from memory.database import get_database

            db = get_database()
            buffer_delay_str = await db.get_config("buffer_delay", "5")
            try:
                buffer_delay = int(buffer_delay_str)
            except ValueError:
                buffer_delay = 5

            self._log.debug(
                "开始缓冲等待: session_id=%s, 延迟=%s秒", session_id, buffer_delay
            )
            await asyncio.sleep(buffer_delay)
            await self._wait_heavy_done(session_id)

            if session_id not in self.buffer_locks:
                self.buffer_locks[session_id] = asyncio.Lock()

            async with self.buffer_locks[session_id]:
                if (
                    session_id not in self.message_buffers
                    or not self.message_buffers[session_id]
                ):
                    self._log.debug("缓冲区为空，跳过处理: session_id=%s", session_id)
                    return

                buffer_messages = self.message_buffers[session_id]
                self.message_buffers[session_id] = []

                if session_id in self.buffer_timers:
                    del self.buffer_timers[session_id]

                self._log.info(
                    "处理缓冲区消息: session_id=%s, 消息数量=%s",
                    session_id,
                    len(buffer_messages),
                )

                combined_content, images, text_for_llm = aggregate_buffer_entries(
                    buffer_messages
                )

                await self._flush_callback(
                    session_id,
                    combined_content,
                    images,
                    buffer_messages,
                    text_for_llm,
                )

        except asyncio.CancelledError:
            self._log.debug("缓冲定时器被取消: session_id=%s", session_id)
        except Exception as e:
            self._log.error("处理缓冲区时出错: session_id=%s, 错误=%s", session_id, e)
            self._log.exception(e)
