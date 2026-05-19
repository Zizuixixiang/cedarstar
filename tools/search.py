"""
OpenAI function calling：Tavily 网页检索，返回原始标题、链接与摘要拼接文本。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_MAX_RESULTS = 10


async def _fetch_tavily_snippets(query: str) -> str:
    from config import config as app_config

    api_key = (app_config.TAVILY_API_KEY or "").strip()
    if not api_key:
        return ""
    q = (query or "").strip()
    if not q:
        return ""
    payload = {
        "api_key": api_key,
        "query": q,
        "max_results": TAVILY_MAX_RESULTS,
        "include_raw_content": False,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TAVILY_SEARCH_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    results = data.get("results")
    if not isinstance(results, list):
        return ""
    lines: List[str] = []
    for i, item in enumerate(results[:TAVILY_MAX_RESULTS], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        body = str(
            item.get("content") or item.get("snippet") or ""
        ).strip()
        lines.append(f"[{i}] {title}\nURL: {url}\n{body}")
    return "\n\n".join(lines).strip()


def _fail_payload() -> str:
    return "暂时无法搜索"


async def execute_search_function_call(function_name: str, arguments: Any) -> str:
    """
    执行 ``web_search``：Tavily 取前 10 条，直接返回标题、链接与摘要原文拼接文本。
    """
    if function_name != "web_search":
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
    query = args.get("query")
    if query is None or not str(query).strip():
        return _fail_payload()
    try:
        raw = await _fetch_tavily_snippets(str(query))
        if not raw:
            return _fail_payload()
        return raw
    except Exception as e:
        logger.warning("web_search 执行失败: %s", e)
        return _fail_payload()
