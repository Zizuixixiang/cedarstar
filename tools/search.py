"""
OpenAI function calling：Tavily 网页检索 + 小模型压缩为高密度摘要（供主模型阅读）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SEARCH_COMPRESS_MAX_OUTPUT_TOKENS = 800

_COMPRESS_SYSTEM = (
    "你是给下游主模型用的信息压缩器。输出是给 AI 看的摘要：信息密度拉满，去掉情绪与废话，"
    "只保留可验证的事实与要点；可用短句或条目，不要寒暄与自我评价。"
    f"输出正文不超过约 {SEARCH_COMPRESS_MAX_OUTPUT_TOKENS} tokens，使用简体中文为主。"
)


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
        "max_results": 5,
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
    for i, item in enumerate(results[:5], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        body = str(
            item.get("content") or item.get("snippet") or ""
        ).strip()
        lines.append(f"[{i}] {title}\nURL: {url}\n{body}")
    return "\n\n".join(lines).strip()


async def _active_compress_api_config() -> Optional[Dict[str, Any]]:
    from memory.database import get_database

    db = get_database()

    def _usable(row: Optional[Dict[str, Any]]) -> bool:
        if not row:
            return False
        key = str(row.get("api_key") or "").strip()
        base = str(row.get("base_url") or "").strip()
        return bool(key and base)

    try:
        ss = await db.get_active_api_config("search_summary")
    except Exception as e:
        logger.warning("读取 search_summary 激活配置失败: %s", e)
        ss = None
    if _usable(ss):
        return ss
    try:
        su = await db.get_active_api_config("summary")
    except Exception as e:
        logger.warning("读取 summary 激活配置失败: %s", e)
        su = None
    return su if _usable(su) else None


async def _compress_snippets(raw_snippets: str) -> str:
    if not (raw_snippets or "").strip():
        return ""
    db_cfg = await _active_compress_api_config()
    if not db_cfg:
        return ""

    user_block = (
        "以下是搜索引擎返回的若干条结果的标题、链接与摘要原文。"
        "请按要求输出一段压缩摘要。\n\n"
        + raw_snippets.strip()
    )

    def _run() -> str:
        from llm.llm_interface import LLMInterface

        llm = LLMInterface(config_type="summary", _db_cfg=db_cfg)
        llm.max_tokens = SEARCH_COMPRESS_MAX_OUTPUT_TOKENS
        try:
            llm.temperature = min(float(llm.temperature), 0.35)
        except (TypeError, ValueError):
            llm.temperature = 0.2
        messages = [
            {"role": "system", "content": _COMPRESS_SYSTEM},
            {"role": "user", "content": user_block},
        ]
        return llm.generate_with_context(messages)

    try:
        return (await asyncio.to_thread(_run)).strip()
    except Exception as e:
        logger.warning("搜索摘要压缩 LLM 失败: %s", e)
        return ""


def _fail_payload() -> str:
    return json.dumps({"summary": "暂时无法搜索"}, ensure_ascii=False)


async def execute_search_function_call(function_name: str, arguments: Any) -> str:
    """
    执行 ``web_search``：Tavily 取前 5 条 → 小模型压成高密度摘要。
    返回可被解析为 JSON object 的字符串（与天气工具一致，便于网关 Struct）。
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
        compressed = await _compress_snippets(raw)
        if not compressed:
            return _fail_payload()
        return json.dumps({"summary": compressed}, ensure_ascii=False)
    except Exception as e:
        logger.warning("web_search 执行失败: %s", e)
        return _fail_payload()
