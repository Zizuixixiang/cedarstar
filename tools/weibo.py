"""
OpenAI function calling：微博实时热搜（官方 ``weibo.com/ajax/side/hotSearch``）。

反爬场景需在环境变量 ``WEIBO_COOKIE`` 中配置从浏览器复制的完整 Cookie，见 ``config.Config.WEIBO_COOKIE``。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from config import config

logger = logging.getLogger(__name__)

_HOTSEARCH_URL = "https://weibo.com/ajax/side/hotSearch"
_CACHE_TTL_SEC = 300.0
_FAIL_SUMMARY = "暂时无法获取热搜"


def _weibo_headers() -> Dict[str, str]:
    h: Dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
    }
    ck = config.WEIBO_COOKIE
    if ck:
        h["Cookie"] = ck
    return h


_cache_lock = asyncio.Lock()
_cache_expires_monotonic: float = 0.0
_cached_summary: str = ""


def _extract_titles(payload: Dict[str, Any]) -> List[str]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rt = data.get("realtime")
    if not isinstance(rt, list):
        return []
    out: List[str] = []
    for item in rt:
        if len(out) >= 10:
            break
        if not isinstance(item, dict):
            continue
        if item.get("topic_ad") == 1:
            continue
        w = item.get("word") or item.get("note") or ""
        w = str(w).strip()
        if w:
            out.append(w)
    return out


def _format_top10(words: List[str]) -> str:
    parts = [f"{i + 1}. {words[i]}" for i in range(len(words))]
    inner = " ".join(parts)
    if len(words) >= 10:
        head = "当前微博热搜前10："
    else:
        head = f"当前微博热搜（共{len(words)}条）："
    return f"「{head}{inner}」"


async def _fetch_hotsearch_once() -> Optional[List[str]]:
    if not config.WEIBO_COOKIE:
        logger.warning(
            "微博热搜：未设置 WEIBO_COOKIE，官方接口易被 403；请在 .env 中配置浏览器 Cookie"
        )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
        ) as client:
            r = await client.get(_HOTSEARCH_URL, headers=_weibo_headers())
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        logger.warning("微博 hotSearch 请求或解析失败: %s", e)
        return None
    if not isinstance(body, dict) or body.get("ok") != 1:
        logger.warning("微博 hotSearch 返回非 ok: %s", body.get("ok"))
        return None
    titles = _extract_titles(body)
    return titles if titles else None


async def fetch_weibo_hot_summary_text() -> str:
    """
    返回热搜文案；失败时返回 ``_FAIL_SUMMARY``（不抛异常）。
    仅缓存成功拉取到的文案，TTL 5 分钟。
    """
    global _cache_expires_monotonic, _cached_summary
    now = time.monotonic()
    async with _cache_lock:
        if _cached_summary and now < _cache_expires_monotonic:
            return _cached_summary

    titles = await _fetch_hotsearch_once()
    if not titles:
        return _FAIL_SUMMARY

    summary = _format_top10(titles)
    async with _cache_lock:
        _cached_summary = summary
        _cache_expires_monotonic = time.monotonic() + _CACHE_TTL_SEC
    return summary


async def execute_weibo_function_call(function_name: str, arguments: Any) -> str:
    """
    执行微博热搜工具；arguments 为 dict 或 JSON 字符串（可无参）。
    返回 **JSON 字符串**（内含 ``summary``），与天气工具一致，便于网关 ``Struct`` 校验。
    """
    _ = arguments
    if function_name != "get_weibo_hot":
        return json.dumps({"error": "未知工具"}, ensure_ascii=False)
    try:
        text = await fetch_weibo_hot_summary_text()
        return json.dumps({"summary": text}, ensure_ascii=False)
    except Exception as e:
        logger.warning("get_weibo_hot 执行失败: %s", e)
        return json.dumps({"summary": _FAIL_SUMMARY}, ensure_ascii=False)
