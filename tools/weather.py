"""
OpenAI function calling：查询天气（复用 ``api.weather`` 缓存接口）。
支持 mode="now"（实时）与 mode="forecast"（未来7天）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def fetch_weather(location: Optional[str] = None) -> str:
    """
    返回 **JSON 字符串**（内含对象），供 ``role=tool`` 注入对话。
    """
    from api.weather import fetch_weather_cached

    name = (location or "").strip() or None
    d = await fetch_weather_cached(name)
    city = str(d.get("city") or name or "默认城市")
    err = d.get("error")
    if err:
        summary = f"{city}天气暂时无法获取：{err}"
        return json.dumps({"summary": summary, "error": str(err)}, ensure_ascii=False)

    cond = str(d.get("condition") or "未知")
    temp = str(d.get("temp") or "--")
    feels = str(d.get("feels_like") or "--")
    hum = str(d.get("humidity") or "--")
    wdir = str(d.get("wind_dir") or "")
    wscale = str(d.get("wind_scale") or "")
    hi = str(d.get("high") or "--")
    lo = str(d.get("low") or "--")
    wind = f"{wdir}{wscale}级" if (wdir or wscale) else "风力未知"
    summary = (
        f"{city}当前天气：{cond}，{temp}°C，体感{feels}°C，湿度{hum}%，"
        f"{wind}。今日高温{hi}°C，低温{lo}°C。"
    )
    return json.dumps({"summary": summary}, ensure_ascii=False)


async def fetch_forecast(location: Optional[str] = None) -> str:
    """返回 7 天预报的 JSON 字符串。"""
    from api.weather import fetch_forecast_cached

    name = (location or "").strip() or None
    d = await fetch_forecast_cached(name)
    city = str(d.get("city") or name or "默认城市")
    err = d.get("error")
    if err:
        summary = f"{city}天气预报暂时无法获取：{err}"
        return json.dumps({"summary": summary, "error": str(err)}, ensure_ascii=False)

    days = d.get("forecast") or []
    if not days:
        return json.dumps({"summary": f"{city}暂无预报数据"}, ensure_ascii=False)

    lines = [f"{city}未来{len(days)}天天气预报："]
    for day in days:
        date = day.get("date", "")
        cond_day = day.get("condition_day", "")
        cond_night = day.get("condition_night", "")
        hi = day.get("high", "--")
        lo = day.get("low", "--")
        hum = day.get("humidity", "--")
        wdir = day.get("wind_dir", "")
        wscale = day.get("wind_scale", "")
        wind = f"{wdir}{wscale}级" if (wdir or wscale) else ""
        cond = cond_day if cond_day == cond_night else f"{cond_day}转{cond_night}"
        line = f"{date} {cond} {lo}~{hi}°C 湿度{hum}% {wind}".rstrip()
        lines.append(line)

    summary = "\n".join(lines)
    return json.dumps({"summary": summary}, ensure_ascii=False)


async def execute_weather_function_call(function_name: str, arguments: Any) -> str:
    """
    执行天气工具；arguments 为 dict 或 JSON 字符串。
    支持 mode 参数: "now"（默认，实时天气）或 "forecast"（7天预报）。
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
    mode = str(args.get("mode") or "now").strip().lower()
    try:
        if mode == "forecast":
            return await fetch_forecast(loc)
        return await fetch_weather(loc)
    except Exception as e:
        logger.warning("get_weather 执行失败: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
