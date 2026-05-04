"""
语音转录（STT）：读取 `stt` 类型 api_config；未配置时回退环境变量 OPENAI_API_KEY + OPENAI_API_BASE
（不复用 chat LLM 配置）。使用 httpx 异步调用 OpenAI 兼容 /audio/transcriptions。
火山引擎走 Seed-ASR 2.0 BigModel 异步 submit/query 接口。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# 转录失败时写入 messages.content 的兜底文案（与 Telegram 等 Bot 一致；落库时须 is_summarized=1）
TRANSCRIBE_FAIL_USER_CONTENT = "[语音] 转录失败"

# 长语音转录可能超过 MessageBuffer 的 buffer_delay 窗口，导致语音与紧邻文字被拆成两轮对话；
# 属物理耗时限制，不是缓冲逻辑 bug。

DEFAULT_STT_MODEL = "whisper-1"
STT_TIMEOUT_SEC = 300.0
VOLCENGINE_ASR_RESOURCE_ID = "volc.seedasr.auc"
VOLCENGINE_POLL_INTERVAL_SEC = 0.5
VOLCENGINE_POLL_MAX_ATTEMPTS = 120  # 0.5s * 120 = 60s max


def _is_volcengine_stt_config(api_base: str, model: str) -> bool:
    """通过 base_url/model 约定识别火山引擎原生 ASR 配置。"""
    base = (api_base or "").lower()
    m = (model or "").lower()
    return (
        "volcengine" in base
        or "volces" in base
        or "openspeech" in base
        or "byte" in base and "speech" in base
        or m.startswith("volc:")
        or m.startswith("volcengine:")
    )


async def _resolve_stt_config_async() -> Dict[str, str]:
    """返回 STT 配置 dict。无可用 key 时抛出 ValueError。"""
    from memory.database import get_database

    row: Optional[Dict[str, Any]] = None
    try:
        row = await get_database().get_active_api_config("stt")
    except Exception as e:
        logger.debug("读取 stt api_config 失败，将尝试环境变量: %s", e)

    if row and (row.get("api_key") or "").strip():
        api_key = str(row["api_key"]).strip()
        base = (row.get("base_url") or "").strip()
        if not base:
            raise ValueError("stt 配置已激活但 base_url 为空")
        model = (row.get("model") or "").strip() or DEFAULT_STT_MODEL
        provider = "volcengine" if _is_volcengine_stt_config(base, model) else "openai_compatible"
        return {"provider": provider, "api_key": api_key, "api_base": base, "model": model}

    from config import config

    env_key = (config.OPENAI_API_KEY or "").strip()
    if not env_key:
        raise ValueError("未配置语音转录：请在 Settings 添加并激活 stt 类型 API，或设置 OPENAI_API_KEY")
    base = (config.OPENAI_API_BASE or "").strip() or "https://api.openai.com/v1"
    return {"provider": "openai_compatible", "api_key": env_key, "api_base": base, "model": DEFAULT_STT_MODEL}


async def _transcribe_openai_compatible(
    voice_bytes: bytes,
    *,
    api_key: str,
    api_base: str,
    model: str,
    mime_type: str,
) -> str:
    """调用 OpenAI 兼容 /audio/transcriptions，返回转录正文。"""
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


# ---------------------------------------------------------------------------
# 火山引擎 Seed-ASR 2.0 BigModel
# ---------------------------------------------------------------------------

def _volcengine_audio_format_from_mime(mime_type: str) -> str:
    mt = (mime_type or "").lower()
    if "mpeg" in mt or "mp3" in mt:
        return "mp3"
    if "wav" in mt or "wave" in mt:
        return "wav"
    if "pcm" in mt:
        return "raw"
    if "ogg" in mt or "opus" in mt:
        return "ogg"
    return "ogg"


def _parse_volcengine_credentials(api_key: str) -> Dict[str, str]:
    """
    解析火山引擎 STT 凭证，返回构建请求头所需的 dict。
    支持两种格式：
    - 新版单 key（UUID 格式）：直接作为 x-api-key
    - 旧版 appid:access_token：拆为 X-Api-App-Key + X-Api-Access-Key
    """
    key = (api_key or "").strip()
    if ":" in key:
        parts = key.split(":", 1)
        app_id = parts[0].strip()
        access_token = parts[1].strip()
        if app_id and access_token:
            return {"X-Api-App-Key": app_id, "X-Api-Access-Key": access_token}
    # 新版单 key 或无冒号的旧 token
    return {"x-api-key": key}


def _volcengine_build_headers(cred_headers: Dict[str, str], request_id: str, *, submit: bool = True) -> Dict[str, str]:
    """构建火山 Seed-ASR 2.0 请求头。"""
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": VOLCENGINE_ASR_RESOURCE_ID,
        "X-Api-Request-Id": request_id,
    }
    headers.update(cred_headers)
    if submit:
        headers["X-Api-Sequence"] = "-1"
    return headers


async def _transcribe_volcengine(
    voice_bytes: bytes,
    *,
    api_key: str,
    api_base: str,
    model: str,
    mime_type: str,
) -> str:
    """调用火山引擎 Seed-ASR 2.0 BigModel 异步 submit/query 接口。"""
    del model  # 新 API 固定使用 bigmodel，不再需要 cluster/workflow

    # 解析凭证
    cred_headers = _parse_volcengine_credentials(api_key)

    # 构造 base URL
    base = api_base.rstrip("/")
    # 如果用户填的是完整 submit URL，取其前缀
    if base.endswith("/submit"):
        base = base[: -len("/submit")]
    elif base.endswith("/query"):
        base = base[: -len("/query")]
    # 确保路径为 /api/v3/auc/bigmodel
    if "/auc/bigmodel" not in base:
        # 兼容只填了 openspeech.bytedance.com 的情况
        if "openspeech.bytedance.com" in base and "/api/" not in base:
            base = base.rstrip("/") + "/api/v3/auc/bigmodel"
    submit_url = base + "/submit" if not base.endswith("/submit") else base
    query_url = base.rsplit("/submit", 1)[0] + "/query"

    # 音频编码
    audio_format = _volcengine_audio_format_from_mime(mime_type)
    audio_b64 = base64.b64encode(voice_bytes).decode("ascii")
    request_id = str(uuid.uuid4())

    # --- Step 1: Submit ---
    submit_headers = _volcengine_build_headers(cred_headers, request_id, submit=True)
    submit_body = {
        "user": {"uid": "cedarclio"},
        "audio": {
            "data": audio_b64,
            "format": audio_format,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
        },
    }

    async with httpx.AsyncClient(timeout=STT_TIMEOUT_SEC) as client:
        resp = await client.post(submit_url, headers=submit_headers, json=submit_body)

        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            logger.warning("火山 STT submit HTTP %s: %s", resp.status_code, body)
            raise RuntimeError(f"火山 STT submit 失败 HTTP {resp.status_code}")

        status_code = resp.headers.get("X-Api-Status-Code", "")
        if status_code not in ("20000000", "20000001", "20000002"):
            msg = resp.headers.get("X-Api-Message", "")
            raise RuntimeError(f"火山 STT submit 异常: status={status_code} {msg}")

        # --- Step 2: Poll query (submit 20000000 仅表示已接受，结果需通过 query 获取) ---
        query_headers = _volcengine_build_headers(cred_headers, request_id, submit=False)
        for _ in range(VOLCENGINE_POLL_MAX_ATTEMPTS):
            await asyncio.sleep(VOLCENGINE_POLL_INTERVAL_SEC)
            resp = await client.post(query_url, headers=query_headers, json={})

            if resp.status_code != 200:
                body = (resp.text or "")[:500]
                logger.warning("火山 STT query HTTP %s: %s", resp.status_code, body)
                raise RuntimeError(f"火山 STT query 失败 HTTP {resp.status_code}")

            status_code = resp.headers.get("X-Api-Status-Code", "")
            if status_code == "20000000":
                return _extract_volcengine_text(resp.json())
            if status_code == "20000003":
                raise RuntimeError("火山 STT: 音频静音，无内容")
            if status_code not in ("20000001", "20000002"):
                msg = resp.headers.get("X-Api-Message", "")
                raise RuntimeError(f"火山 STT query 异常: status={status_code} {msg}")

        raise RuntimeError("火山 STT 轮询超时")


def _extract_volcengine_text(data: dict) -> str:
    """从火山 Seed-ASR 2.0 响应中提取转录文本。"""
    result = data.get("result") if isinstance(data, dict) else None
    text = ""
    if isinstance(result, dict):
        text = (result.get("text") or "").strip()
        if not text and isinstance(result.get("utterances"), list):
            text = "".join(
                str(u.get("text") or "")
                for u in result["utterances"]
                if isinstance(u, dict)
            ).strip()
    if not text:
        text = (data.get("text") or "").strip()
    if not text:
        raise RuntimeError("火山 STT 返回空文本")
    return text


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def transcribe_voice(voice_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    返回转录正文（不含 [语音] 前缀）。
    - stt 配置识别为火山引擎时，走火山 Seed-ASR 2.0 BigModel 分支；
    - 其他情况保留原 OpenAI 兼容 /audio/transcriptions 逻辑。
    失败时抛出异常，由调用方兜底为「转录失败」等文案。
    """
    stt_config = await _resolve_stt_config_async()
    if stt_config["provider"] == "volcengine":
        return await _transcribe_volcengine(
            voice_bytes,
            api_key=stt_config["api_key"],
            api_base=stt_config["api_base"],
            model=stt_config["model"],
            mime_type=mime_type,
        )
    return await _transcribe_openai_compatible(
        voice_bytes,
        api_key=stt_config["api_key"],
        api_base=stt_config["api_base"],
        model=stt_config["model"],
        mime_type=mime_type,
    )
