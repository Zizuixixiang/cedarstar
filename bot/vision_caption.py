"""
异步视觉描述：用户图片消息落库后后台生成 image_caption，不阻塞主对话流程。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _caption_user_prompt(caption_text: str) -> str:
    base = (
        "请用中文详细描述图片内容，包含颜色、人物表情、画面中的文字、构图与氛围等细节。"
        "输出为一段连贯文字，不要分条列表。"
    )
    if caption_text and caption_text.strip():
        return f"{base}\n\n用户配文：{caption_text.strip()}"
    return base


def schedule_generate_image_caption(
    message_row_id: int,
    images: List[Dict[str, Any]],
    caption_text: str,
    platform: Optional[str] = None,
) -> None:
    """在事件循环中调度异步视觉描述任务（吞掉异常，失败时写库标记）。"""
    try:
        asyncio.create_task(
            generate_image_caption(message_row_id, images, caption_text, platform=platform)
        )
    except RuntimeError:
        # 无运行中的 loop（极少）；同步降级为 fire-and-forget
        logger.warning("schedule_generate_image_caption: 无事件循环，跳过视觉描述")


async def generate_image_caption(
    message_row_id: int,
    images: List[Dict[str, Any]],
    caption_text: str,
    platform: Optional[str] = None,
) -> None:
    from llm.llm_interface import LLMInterface, build_user_multimodal_content
    from memory.database import VISION_FAIL_CAPTION_SHORT, update_message_vision_result

    if not images:
        return

    prompt = _caption_user_prompt(caption_text)
    fail_caption = VISION_FAIL_CAPTION_SHORT

    try:
        llm = LLMInterface(config_type="vision")
        content = build_user_multimodal_content(
            llm.api_base,
            llm.model_name,
            prompt,
            images,
        )
        messages: List[Dict[str, Any]] = [{"role": "user", "content": content}]

        loop = asyncio.get_running_loop()
        def _call() -> str:
            raw, _thinking = llm.generate_with_context_and_tracking(
                messages, platform=platform
            )
            return raw

        text = await loop.run_in_executor(None, _call)
        text = (text or "").strip() or fail_caption
        update_message_vision_result(message_row_id, text, 1)
    except Exception as e:
        logger.exception("视觉描述任务失败 message_id=%s: %s", message_row_id, e)
        try:
            update_message_vision_result(message_row_id, fail_caption, 1)
        except Exception as db_e:
            logger.error("写入视觉失败标记失败: %s", db_e)
