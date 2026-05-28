"""
日摘要按天压缩（进程内缓存），供自主活动 Context 与 MCP 外部读取共用。

完整原文规则：库内按内容日倒序的第一条 daily 所在日期（最新一日）合并全文；
其余日期仅返回压缩摘要。不按东八区「今天」固定。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from config import Platform
from llm.llm_interface import batch_one_shot_with_async_output_guard
from memory.database import get_recent_daily_summaries
from memory.micro_batch import SummaryLLMInterface
from memory.shanghai_dt import format_shanghai_date_iso

logger = logging.getLogger(__name__)

_DAILY_SUMMARY_COMPRESS_CACHE: dict[str, str] = {}
_DAILY_SUMMARY_COMPRESS_PROMPT = """以下是多天的对话日摘要，请按天独立压缩，保留每天的日期标题、关键事件、情绪节点和对话主题，去掉细节和重复内容。每天压缩后不超过300字，格式保持"YYYY-MM-DD：……"。

{daily_summaries_text}
"""


def daily_summary_date(summary: dict[str, Any]) -> str:
    raw_date = summary.get("source_date") or summary.get("created_at")
    formatted = format_shanghai_date_iso(raw_date)
    if formatted:
        return formatted
    raw = str(raw_date or "").strip()
    return raw[:10] if raw else "未知日期"


def _is_valid_date_key(date_key: str) -> bool:
    return bool(date_key and date_key != "未知日期" and re.match(r"^\d{4}-\d{2}-\d{2}$", date_key))


def _join_daily_summary_text(rows: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        str(row.get("summary_text") or "").strip()
        for row in rows
        if str(row.get("summary_text") or "").strip()
    )


def _group_summaries_by_date(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        date_key = daily_summary_date(row)
        grouped.setdefault(date_key, []).append(row)
    return grouped


def latest_daily_date_key(rows: list[dict[str, Any]]) -> Optional[str]:
    """与 get_recent_daily_summaries 倒序一致：第一条记录的内容日即为「最新一日」。"""
    if not rows:
        return None
    return daily_summary_date(rows[0])


def parse_compressed_daily_summaries(text: str) -> dict[str, str]:
    parsed: dict[str, list[str]] = {}
    current_date: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.match(r"^(?:#{1,6}\s*)?(\d{4}-\d{2}-\d{2})[：:]\s*(.*)$", line)
        if m:
            current_date = m.group(1)
            parsed[current_date] = [m.group(2).strip()]
            continue
        if current_date:
            parsed[current_date].append(line)
    return {
        date_key: "\n".join(part for part in parts if part).strip()
        for date_key, parts in parsed.items()
        if any(part.strip() for part in parts)
    }


async def _compress_daily_dates(
    grouped: dict[str, list[dict[str, Any]]],
    date_keys: list[str],
    *,
    log_prefix: str,
) -> bool:
    """将 date_keys 对应日期的原文压缩并写入进程内缓存。成功返回 True。"""
    uncached = [dk for dk in date_keys if dk not in _DAILY_SUMMARY_COMPRESS_CACHE]
    if not uncached:
        return True

    daily_summaries_text = "\n\n".join(
        f"{date_key}：\n" + _join_daily_summary_text(grouped[date_key])
        for date_key in uncached
        if _join_daily_summary_text(grouped[date_key]).strip()
    )
    if not daily_summaries_text.strip():
        return True

    try:
        summary_llm = await SummaryLLMInterface.create()
        base_tokens = int(getattr(summary_llm, "max_tokens", 500) or 500)
        compressed_text = batch_one_shot_with_async_output_guard(
            messages=[
                {
                    "role": "user",
                    "content": _DAILY_SUMMARY_COMPRESS_PROMPT.format(
                        daily_summaries_text=daily_summaries_text
                    ),
                }
            ],
            model_name=summary_llm.model_name,
            api_key=summary_llm.api_key or "",
            api_base=summary_llm.api_base or "",
            timeout=summary_llm.timeout,
            max_tokens=min(4096, max(base_tokens, 1800)),
            platform=Platform.BATCH,
            max_retries=5,
        )
        parsed = parse_compressed_daily_summaries(compressed_text)
        for date_key in uncached:
            compressed = (parsed.get(date_key) or "").strip()
            if compressed:
                _DAILY_SUMMARY_COMPRESS_CACHE[date_key] = compressed
            else:
                _DAILY_SUMMARY_COMPRESS_CACHE[date_key] = _join_daily_summary_text(
                    grouped[date_key]
                )
        logger.info(
            "%s daily 预压缩完成: requested=%s parsed=%s cache_size=%s",
            log_prefix,
            len(uncached),
            len(parsed),
            len(_DAILY_SUMMARY_COMPRESS_CACHE),
        )
        return True
    except Exception as e:
        logger.warning("%s daily 预压缩失败: %s", log_prefix, e)
        return False


def _older_dates_for_compress(
    grouped: dict[str, list[dict[str, Any]]],
    latest_date: str,
    *,
    max_older_days: int,
) -> list[str]:
    older = sorted(
        (
            dk
            for dk in grouped
            if dk != latest_date and _is_valid_date_key(dk)
        ),
        reverse=True,
    )
    return older[:max(0, max_older_days)]


def _make_summary_row_for_date(
    grouped: dict[str, list[dict[str, Any]]],
    date_key: str,
    summary_text: str,
) -> dict[str, Any]:
    row = dict(grouped[date_key][0])
    row["summary_text"] = summary_text
    row["source_date"] = date_key
    return row


async def build_idle_daily_summaries_override() -> Optional[list[dict[str, Any]]]:
    """自主活动：16 日历日窗口内，最新一日 daily 合并原文，更早最多 15 日压缩。"""
    try:
        rows = await get_recent_daily_summaries(limit=16)
    except Exception as e:
        logger.warning("[idle] 拉取 daily summary 失败，使用 ContextBuilder 默认逻辑: %s", e)
        return None

    if not rows:
        return None

    grouped = _group_summaries_by_date(rows)
    latest_date = latest_daily_date_key(rows)
    if not latest_date:
        return None

    latest_row = _make_summary_row_for_date(
        grouped,
        latest_date,
        _join_daily_summary_text(grouped[latest_date]),
    )

    older_dates = _older_dates_for_compress(grouped, latest_date, max_older_days=15)
    if not older_dates:
        return [latest_row]

    if not await _compress_daily_dates(grouped, older_dates, log_prefix="[idle]"):
        return [latest_row] + [
            row for date_key in older_dates for row in grouped[date_key]
        ]

    compressed_rows = [
        _make_summary_row_for_date(
            grouped,
            date_key,
            _DAILY_SUMMARY_COMPRESS_CACHE.get(date_key)
            or _join_daily_summary_text(grouped[date_key]),
        )
        for date_key in older_dates
    ]
    return [latest_row] + compressed_rows


async def get_recent_daily_digest(days: int = 7) -> dict[str, Any]:
    """
    最近 N 个日历日窗口：库内最新一日 daily 合并原文，其余日期仅压缩摘要。
    items 按日期升序。
    """
    window_days = max(1, min(int(days or 7), 30))

    try:
        rows = await get_recent_daily_summaries(limit=window_days)
    except Exception as e:
        logger.error("[mcp] 拉取 daily digest 失败: %s", e)
        return {"success": False, "error": str(e)}

    latest_date = latest_daily_date_key(rows) if rows else None
    if not rows or not latest_date:
        return {
            "success": True,
            "items": [],
            "days": window_days,
            "latest_date": latest_date,
        }

    grouped = _group_summaries_by_date(rows)
    older_dates = _older_dates_for_compress(
        grouped,
        latest_date,
        max_older_days=max(0, window_days - 1),
    )

    compress_ok = True
    if older_dates:
        compress_ok = await _compress_daily_dates(
            grouped, older_dates, log_prefix="[mcp]"
        )

    date_order = sorted(grouped.keys())
    items: list[dict[str, Any]] = []
    for date_key in date_order:
        if date_key == latest_date:
            items.append(
                {
                    "date": date_key,
                    "text": _join_daily_summary_text(grouped[date_key]),
                    "compressed": False,
                }
            )
            continue
        if compress_ok:
            text = (_DAILY_SUMMARY_COMPRESS_CACHE.get(date_key) or "").strip()
        else:
            text = ""
        items.append(
            {
                "date": date_key,
                "text": text,
                "compressed": True,
                **({"compress_failed": True} if not compress_ok else {}),
            }
        )

    return {
        "success": True,
        "items": items,
        "days": window_days,
        "latest_date": latest_date,
    }
