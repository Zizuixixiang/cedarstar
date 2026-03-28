"""
语音转录（STT）：读取 `stt` 类型 api_config；未配置时回退环境变量 OPENAI_API_KEY + OPENAI_API_BASE
（不复用 chat LLM 配置）。使用 httpx 异步调用 OpenAI 兼容 /audio/transcriptions。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# 转录失败时写入 messages.content 的兜底文案（与 Telegram 等 Bot 一致；落库时须 is_summarized=1）
TRANSCRIBE_FAIL_USER_CONTENT = "[语音] 转录失败"

# 长语音转录可能超过 MessageBuffer 的 buffer_delay 窗口，导致语音与紧邻文字被拆成两轮对话；
# 属物理耗时限制，不是缓冲逻辑 bug。

DEFAULT_STT_MODEL = "whisper-1"
STT_TIMEOUT_SEC = 300.0


def _resolve_stt_credentials() -> Tuple[str, str, str]:
    """返回 (api_key, api_base, model)。无可用 key 时抛出 ValueError。"""
    from memory.database import get_database

    row: Optional[Dict[str, Any]] = None
    try:
        row = get_database().get_active_api_config("stt")
    except Exception as e:
        logger.debug("读取 stt api_config 失败，将尝试环境变量: %s", e)

    if row and (row.get("api_key") or "").strip():
        api_key = str(row["api_key"]).strip()
        base = (row.get("base_url") or "").strip()
        if not base:
            raise ValueError("stt 配置已激活但 base_url 为空")
        model = (row.get("model") or "").strip() or DEFAULT_STT_MODEL
        return api_key, base, model

    from config import config

    env_key = (config.OPENAI_API_KEY or "").strip()
    if not env_key:
        raise ValueError("未配置语音转录：请在 Settings 添加并激活 stt 类型 API，或设置 OPENAI_API_KEY")
    base = (config.OPENAI_API_BASE or "").strip() or "https://api.openai.com/v1"
    return env_key, base, DEFAULT_STT_MODEL


async def transcribe_voice(voice_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    调用 /audio/transcriptions，返回转录正文（不含 [语音] 前缀）。
    失败时抛出异常，由调用方兜底为「转录失败」等文案。
    """
    api_key, api_base, model = _resolve_stt_credentials()
    url = api_base.rstrip("/") + "/audio/transcriptions"

    if mime_type.lower() in ("audio/mpeg", "audio/mp3"):
        filename = "voice.mp3"
    else:
        filename = "voice.ogg"

    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (filename, voice_bytes, mime_type)}
    data = {"model": model, "language": "zh"}

    async with httpx.AsyncClient(timeout=STT_TIMEOUT_SEC) as client:
        resp = await client.post(url, headers=headers, files=files, data=data)

    if resp.status_code != 200:
        body = (resp.text or "")[:500]
        logger.warning("STT HTTP %s: %s", resp.status_code, body)
        raise RuntimeError(f"STT 请求失败 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError("STT 响应非 JSON") from e

    text = (payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("STT 返回空文本")
    return text
