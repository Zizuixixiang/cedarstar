"""
MiniMax T2A v2 TTS 客户端
文档：https://platform.minimax.io/docs/api-reference/speech-t2a-http
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TTS_ENDPOINT = "https://api.minimaxi.com/v1/t2a_v2"


async def minimax_tts(
    text: str,
    api_key: str,
    voice_id: str,
    model: str = "speech-2.8-turbo",
    speed: float = 0.95,
    vol: float = 1.0,
    pitch: int = 0,
    intensity: int = 0,
    timbre: int = 0,
) -> Optional[bytes]:
    """
    调用 MiniMax TTS，返回 mp3 bytes，失败返回 None（不抛异常，调用方降级）。
    text 长度不超过 10000 字符；超长请调用方截断后分批调用。
    """
    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "output_format": "hex",
        "voice_setting": {
            "voice_id": voice_id,
            "speed": speed,
            "vol": vol,
            "pitch": pitch,
        },
        "voice_modify": {
            "pitch": pitch,
            "intensity": intensity,
            "timbre": timbre,
        },
        "audio_setting": {
            "format": "mp3",
            "sample_rate": 32000,
            "bitrate": 128000,
            "channel": 1,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                TTS_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            hex_audio = data.get("data", {}).get("audio", "")
            if not hex_audio:
                logger.error("TTS response missing audio field: %s", data)
                return None
            return bytes.fromhex(hex_audio)
    except Exception as e:
        logger.error("MiniMax TTS failed: %s", e)
        return None
