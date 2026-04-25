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
    city = str(d.get("city") or name or "\u9ed8\u8ba4\u57ce\u5e02")
    err = d.get("error")
    if err:
        summary = f"{city}\u5929\u6c14\u6682\u65f6\u65e0\u6cd5\u83b7\u53d6\uff1a{err}"
        return json.dumps({"summary": summary, "error": str(err)}, ensure_ascii=False)

    cond = str(d.get("condition") or "\u672a\u77e5")
    temp = str(d.get("temp") or "--")
    feels = str(d.get("feels_like") or "--")
    hum = str(d.get("humidity") or "--")
    wdir = str(d.get("wind_dir") or "")
    wscale = str(d.get("wind_scale") or "")
    hi = str(d.get("high") or "--")
    lo = str(d.get("low") or "--")
    wind = f"{wdir}{wscale}\u7ea7" if (wdir or wscale) else "\u98ce\u529b\u672a\u77e5"
    summary = (
        f"{city}\u5f53\u524d\u5929\u6c14\uff1a{cond}\uff0c{temp}\u00b0C\uff0c\u4f53\u611f{feels}\u00b0C\uff0c\u6e7f\u5ea6{hum}%\uff0c"
        f"{wind}\u3002\u4eca\u65e5\u9ad8\u6e29{hi}\u00b0C\uff0c\u4f4e\u6e29{lo}\u00b0C\u3002"
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
