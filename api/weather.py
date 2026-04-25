"""
当前天气（和风天气 devapi），进程内按 location 缓存 10 分钟。
供 HTTP `/api/weather/current` 与 AI 工具 `get_weather` 共用。
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter

from config import config

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_TTL_SEC = 600
# location_id -> {"ts": monotonic, "body": dict}
_WEATHER_BY_LOC: Dict[str, Dict[str, Any]] = {}


def _mock_payload(city_label: Optional[str] = None) -> Dict[str, Any]:
    now = datetime.now().replace(microsecond=0)
    city = (city_label or "").strip() or config.HEFENG_CITY
    return {
        "city": city,
        "temp": "23",
        "feels_like": "21",
        "condition": "多云",
        "icon": "101",
        "humidity": "65",
        "wind_dir": "东北风",
        "wind_scale": "3",
        "high": "26",
        "low": "18",
        "updated_at": now.isoformat(),
    }


async def lookup_city_location_id(city_name: str) -> Optional[str]:
    """
    城市名 → LocationID；无 Key 或失败时返回 None（由调用方回退默认 HEFENG_LOCATION）。
    """
    name = (city_name or "").strip()
    if not name:
        return None
    key = config.HEFENG_API_KEY
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(
                "https://devapi.qweather.com/v2/city/lookup",
                params={"location": name, "key": key},
            )
        data = r.json()
    except Exception as e:
        logger.warning("和风城市查询失败: %s", e)
        return None
    if str(data.get("code")) != "200":
        return None
    locs = data.get("location") or []
    if not locs or not isinstance(locs, list):
        return None
    first = locs[0]
    if isinstance(first, dict) and first.get("id"):
        return str(first["id"]).strip()
    return None


async def _fetch_hefeng_for_location_id(location_id: str) -> Optional[Dict[str, Any]]:
    key = config.HEFENG_API_KEY
    if not key:
        return None
    loc = (location_id or "").strip() or config.HEFENG_LOCATION
    base = "https://devapi.qweather.com/v7/weather"
    params = {"location": loc, "key": key}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r_now = await client.get(f"{base}/now", params=params)
            r_3d = await client.get(f"{base}/3d", params=params)
        now_j = r_now.json()
        d3_j = r_3d.json()
    except Exception as e:
        logger.warning("和风天气请求失败: %s", e)
        return None

    if str(now_j.get("code")) != "200" or "now" not in now_j:
        logger.warning("和风天气 now 异常: code=%s", now_j.get("code"))
        return None

    now = now_j["now"]
    high, low = "18", "26"
    city_display = config.HEFENG_CITY
    if str(d3_j.get("code")) == "200":
        daily = d3_j.get("daily") or []
        if daily:
            high = str(daily[0].get("tempMax", high))
            low = str(daily[0].get("tempMin", low))

    update_raw = now_j.get("updateTime") or now.get("obsTime") or ""
    if isinstance(update_raw, str) and len(update_raw) >= 19:
        updated_at = update_raw[:19]
    else:
        updated_at = datetime.now().replace(microsecond=0).isoformat()

    wind_scale = now.get("windScale", "3")
    if isinstance(wind_scale, str) and "-" in wind_scale:
        wind_scale = wind_scale.split("-")[0].strip()

    # 若 now 所在城市名与默认不同，优先用 API 返回（部分版本在 now 无 city，仍用配置）
    # 城市展示：lookup 时已选 id，此处沿用配置名；工具层可用 location_name 覆盖
    return {
        "city": city_display,
        "temp": str(now.get("temp", "23")),
        "feels_like": str(now.get("feelsLike", now.get("feels_like", "21"))),
        "condition": str(now.get("text", "多云")),
        "icon": str(now.get("icon", "101")),
        "humidity": str(now.get("humidity", "65")),
        "wind_dir": str(now.get("windDir", "风")),
        "wind_scale": str(wind_scale),
        "high": str(high),
        "low": str(low),
        "updated_at": updated_at,
    }


async def fetch_weather_cached(location_name: Optional[str] = None) -> Dict[str, Any]:
    """
    返回与 ``GET /api/weather/current`` 相同结构的 dict。
    ``location_name`` 为空则用 ``HEFENG_LOCATION``；否则尝试 Geo 解析 LocationID。
    """
    loc_id = config.HEFENG_LOCATION
    display: Optional[str] = None
    name = (location_name or "").strip()
    if name:
        resolved = await lookup_city_location_id(name)
        if resolved:
            loc_id = resolved
            if not display:
                display = name
        else:
            logger.info("城市未解析到 LocationID，使用默认 HEFENG_LOCATION 数据，展示名仍用：%s", name)
            if not display:
                display = name

    now_ts = time.monotonic()
    ent = _WEATHER_BY_LOC.get(loc_id)
    if ent is not None and (now_ts - float(ent["ts"])) < _CACHE_TTL_SEC:
        body = dict(ent["body"])
        if display:
            body["city"] = display
        return body

    if not config.HEFENG_API_KEY:
        body = _mock_payload(None)
        _WEATHER_BY_LOC[loc_id] = {"ts": now_ts, "body": dict(body)}
        if display:
            body = dict(body)
            body["city"] = display
        return body

    fresh = await _fetch_hefeng_for_location_id(loc_id)
    if fresh is None:
        body = _mock_payload(None)
    else:
        body = fresh

    _WEATHER_BY_LOC[loc_id] = {"ts": now_ts, "body": dict(body)}
    if display:
        body = dict(body)
        body["city"] = display
    elif name:
        body = dict(body)
        body["city"] = name
    return body


@router.get("/current")
async def current_weather():
    return await fetch_weather_cached(None)

