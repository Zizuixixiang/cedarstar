"""
Current weather via QWeather API, cached in-process per location for 10 minutes.
Shared by HTTP `/api/weather/current` and the AI `get_weather` tool.
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

_WEATHER_UNAVAILABLE = "\u5929\u6c14\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528"
_CONDITION_UNAVAILABLE = "\u6682\u65f6\u4e0d\u53ef\u7528"
_NO_API_KEY = "\u672a\u914d\u7f6e HEFENG_API_KEY"
_CITY_NOT_RESOLVED = "\u57ce\u5e02\u672a\u89e3\u6790\u5230 LocationID"


def _qweather_url(path: str) -> str:
    host = config.HEFENG_API_HOST
    path = "/" + (path or "").lstrip("/")
    return f"https://{host}{path}"


def _qweather_headers() -> Dict[str, str]:
    return {"X-QW-Api-Key": config.HEFENG_API_KEY, "Accept": "application/json"}


def _unavailable_payload(city_label: Optional[str] = None, reason: str = _WEATHER_UNAVAILABLE) -> Dict[str, Any]:
    now = datetime.now().replace(microsecond=0)
    city = (city_label or "").strip() or config.HEFENG_CITY
    return {
        "city": city,
        "temp": None,
        "feels_like": None,
        "condition": _CONDITION_UNAVAILABLE,
        "icon": "",
        "humidity": None,
        "wind_dir": "",
        "wind_scale": "",
        "high": None,
        "low": None,
        "updated_at": now.isoformat(),
        "error": reason,
    }


async def lookup_city_location_id(city_name: str) -> Optional[str]:
    """Return QWeather LocationID for a city name, or None when lookup fails."""
    name = (city_name or "").strip()
    key = config.HEFENG_API_KEY
    if not name or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=_qweather_headers()) as client:
            r = await client.get(
                _qweather_url("/geo/v2/city/lookup"),
                params={"location": name, "number": "1", "range": "cn"},
            )
        if r.status_code != 200:
            logger.warning("QWeather city lookup HTTP error: status=%s body=%s", r.status_code, r.text[:200])
            return None
        data = r.json()
    except Exception as e:
        logger.warning("QWeather city lookup failed: %s", e)
        return None
    if str(data.get("code")) != "200":
        logger.warning("QWeather city lookup error: code=%s city=%s", data.get("code"), name)
        return None
    locs = data.get("location") or []
    if not locs or not isinstance(locs, list):
        return None
    first = locs[0]
    if isinstance(first, dict) and first.get("id"):
        return str(first["id"]).strip()
    return None


async def _fetch_hefeng_for_location_id(location_id: str, city_display: Optional[str] = None) -> Optional[Dict[str, Any]]:
    key = config.HEFENG_API_KEY
    if not key:
        return None
    loc = (location_id or "").strip() or config.HEFENG_LOCATION
    params = {"location": loc}
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_qweather_headers()) as client:
            r_now = await client.get(_qweather_url("/v7/weather/now"), params=params)
            r_3d = await client.get(_qweather_url("/v7/weather/3d"), params=params)
        if r_now.status_code != 200:
            logger.warning("QWeather now HTTP error: status=%s body=%s", r_now.status_code, r_now.text[:200])
            return None
        now_j = r_now.json()
        d3_j = r_3d.json() if r_3d.status_code == 200 else {}
    except Exception as e:
        logger.warning("QWeather weather request failed: %s", e)
        return None

    if str(now_j.get("code")) != "200" or "now" not in now_j:
        logger.warning("QWeather now error: code=%s", now_j.get("code"))
        return None

    now = now_j["now"]
    high, low = None, None
    if str(d3_j.get("code")) == "200":
        daily = d3_j.get("daily") or []
        if daily:
            high = daily[0].get("tempMax")
            low = daily[0].get("tempMin")
    elif d3_j:
        logger.warning("QWeather 3d error: code=%s", d3_j.get("code"))

    update_raw = now_j.get("updateTime") or now.get("obsTime") or ""
    if isinstance(update_raw, str) and len(update_raw) >= 19:
        updated_at = update_raw[:19]
    else:
        updated_at = datetime.now().replace(microsecond=0).isoformat()

    wind_scale = now.get("windScale", "")
    if isinstance(wind_scale, str) and "-" in wind_scale:
        wind_scale = wind_scale.split("-")[0].strip()

    return {
        "city": (city_display or "").strip() or config.HEFENG_CITY,
        "temp": str(now.get("temp", "")),
        "feels_like": str(now.get("feelsLike", now.get("feels_like", ""))),
        "condition": str(now.get("text", "")),
        "icon": str(now.get("icon", "")),
        "humidity": str(now.get("humidity", "")),
        "wind_dir": str(now.get("windDir", "")),
        "wind_scale": str(wind_scale),
        "high": None if high is None else str(high),
        "low": None if low is None else str(low),
        "updated_at": updated_at,
    }


async def fetch_weather_cached(location_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Return the same shape as GET /api/weather/current.
    Empty location_name uses HEFENG_LOCATION; otherwise GeoAPI resolves a LocationID.
    """
    loc_id = config.HEFENG_LOCATION
    display: Optional[str] = None
    name = (location_name or "").strip()

    if not config.HEFENG_API_KEY:
        return _unavailable_payload(name or None, _NO_API_KEY)

    if name:
        resolved = await lookup_city_location_id(name)
        if not resolved:
            logger.info("QWeather city did not resolve to LocationID: %s", name)
            return _unavailable_payload(name, _CITY_NOT_RESOLVED)
        loc_id = resolved
        display = name

    now_ts = time.monotonic()
    ent = _WEATHER_BY_LOC.get(loc_id)
    if ent is not None and (now_ts - float(ent["ts"])) < _CACHE_TTL_SEC:
        body = dict(ent["body"])
        if display:
            body["city"] = display
        return body

    fresh = await _fetch_hefeng_for_location_id(loc_id, display)
    if fresh is None:
        return _unavailable_payload(display or name or None)

    _WEATHER_BY_LOC[loc_id] = {"ts": now_ts, "body": dict(fresh)}
    return dict(fresh)


@router.get("/current")
async def current_weather():
    return await fetch_weather_cached(None)
