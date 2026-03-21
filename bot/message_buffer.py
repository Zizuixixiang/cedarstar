"""
Discord / Telegram 共用的消息缓冲实现。

同一 session 在 buffer_delay 秒内合并多条消息，超时后通过回调交给各平台
完成「正在输入 / 生成 / 发送」等平台相关逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# (session_id, combined_content, buffer_messages) -> None
BufferFlushCallback = Callable[
    [str, str, List[Dict[str, Any]]],
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

    async def add_to_buffer(self, session_id: str, entry: Dict[str, Any]) -> None:
        """追加一条缓冲条目（须含 ``content`` 字段供合并），并启动/重置该 session 的定时任务。"""
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
            buffer_delay_str = db.get_config("buffer_delay", "5")
            try:
                buffer_delay = int(buffer_delay_str)
            except ValueError:
                buffer_delay = 5

            self._log.debug(
                "开始缓冲等待: session_id=%s, 延迟=%s秒", session_id, buffer_delay
            )
            await asyncio.sleep(buffer_delay)

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

                combined_content = "\n".join(msg["content"] for msg in buffer_messages)

                await self._flush_callback(session_id, combined_content, buffer_messages)

        except asyncio.CancelledError:
            self._log.debug("缓冲定时器被取消: session_id=%s", session_id)
        except Exception as e:
            self._log.error("处理缓冲区时出错: session_id=%s, 错误=%s", session_id, e)
            self._log.exception(e)
