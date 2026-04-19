"""
OpenAI function calling：查询天气（复用 ``api.weather.fetch_weather_cached``）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def fetch_weather(location: Optional[str] = None) -> str:
    """
    返回 **JSON 字符串**（内含对象），供 ``role=tool`` 注入对话。

    与 Lutopia 工具一致，必须是可被解析为 **JSON object** 的文本：经 Gemini 等网关转发时，
    ``function_response`` 要求 ``Struct``，裸自然语言会触发上游 400。
    """
    from api.weather import fetch_weather_cached

    name = (location or "").strip() or None
    d = await fetch_weather_cached(name)
    city = str(d.get("city", ""))
    cond = str(d.get("condition", ""))
    temp = str(d.get("temp", ""))
    feels = str(d.get("feels_like", ""))
    hum = str(d.get("humidity", ""))
    wdir = str(d.get("wind_dir", ""))
    wscale = str(d.get("wind_scale", ""))
    hi = str(d.get("high", ""))
    lo = str(d.get("low", ""))
    summary = (
        f"{city}当前天气：{cond}，{temp}°C，体感{feels}°C，湿度{hum}%，"
        f"{wdir}{wscale}级。今日高温{hi}°C，低温{lo}°C。"
    )
    return json.dumps({"summary": summary}, ensure_ascii=False)


async def execute_weather_function_call(function_name: str, arguments: Any) -> str:
    """
    执行天气工具；arguments 为 dict 或 JSON 字符串。
    """
    if function_name != "get_weather":
        return json.dumps({"error": "未知工具"}, ensure_ascii=False)
    args: Dict[str, Any]
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}
    loc = args.get("location")
    if loc is not None:
        loc = str(loc).strip() or None
    try:
        return await fetch_weather(loc)
    except Exception as e:
        logger.warning("get_weather 执行失败: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
