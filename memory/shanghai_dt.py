"""
东八区时间展示（24 小时制）。

与库内 TIMESTAMP naive = 上海墙钟的约定一致；带时区的时刻会先换算到东八区再格式化。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Mapping, Optional, Sequence

import pytz

_TZ_SH = pytz.timezone("Asia/Shanghai")


def now_shanghai() -> datetime:
    """当前时刻（东八区 aware）。"""
    return datetime.now(_TZ_SH)


def to_shanghai_datetime(val: Any) -> Optional[datetime]:
    """
    将 asyncpg 常见的 created_at（datetime / date / ISO 字符串）转为东八区 aware datetime。

    - naive ``datetime``：按 Asia/Shanghai 解释。
    - aware：换算到 Asia/Shanghai。
    - ``date``：视为该日 00:00 上海本地。
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            return _TZ_SH.localize(dt)
        return dt.astimezone(_TZ_SH)
    if isinstance(val, date):
        return _TZ_SH.localize(datetime.combine(val, time(0, 0)))
    s = str(val).strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return _TZ_SH.localize(dt)
    return dt.astimezone(_TZ_SH)


def format_shanghai_clock_24h(val: Any) -> Optional[str]:
    """HH:MM，24 小时制（东八区）。不可解析时返回 None。"""
    dt = to_shanghai_datetime(val)
    if dt is None:
        return None
    return dt.strftime("%H:%M")


def format_shanghai_datetime_minutes(val: Any) -> Optional[str]:
    """YYYY-MM-DD HH:MM，24 小时制（东八区）。"""
    dt = to_shanghai_datetime(val)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


def format_shanghai_date_iso(val: Any) -> Optional[str]:
    """YYYY-MM-DD，按东八区日历日。"""
    dt = to_shanghai_datetime(val)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d")


def format_created_at_range_preamble(
    records: Sequence[Mapping[str, Any]],
    *,
    heading: str,
    field: str = "created_at",
    semantics_note: str = "",
) -> str:
    """
    按 ``records`` 的顺序，取第一条与最后一条可解析的 ``field``，拼成「首条～末条」说明（东八区）。

    适用于列表本身已是时间正序（如 ``ORDER BY created_at ASC``）的材料。
    """
    suffix = f"，{semantics_note}" if semantics_note else ""
    first_ts: Optional[str] = None
    for rec in records:
        ts = format_shanghai_datetime_minutes(rec.get(field))
        if ts:
            first_ts = ts
            break
    last_ts: Optional[str] = None
    for rec in reversed(records):
        ts = format_shanghai_datetime_minutes(rec.get(field))
        if ts:
            last_ts = ts
            break
    if not first_ts or not last_ts:
        return ""
    if first_ts == last_ts:
        return f"{heading}首条与末条均为 {first_ts}（东八区{suffix}）。\n\n"
    return f"{heading}首条 {first_ts} 至末条 {last_ts}（东八区{suffix}）。\n\n"


def format_created_at_span_minmax_preamble(
    records: Sequence[Mapping[str, Any]],
    *,
    heading: str,
    field: str = "created_at",
    semantics_note: str = "",
) -> str:
    """
    在 ``records`` 全体中，取 ``field`` 在东八区格式化后的字典序最小与最大，拼成「最早～最晚」说明。

    适用于材料块被重新分组、顺序不再是时间轴时的提示。
    """
    suffix = f"，{semantics_note}" if semantics_note else ""
    ts_list: list[str] = []
    for rec in records:
        ts = format_shanghai_datetime_minutes(rec.get(field))
        if ts:
            ts_list.append(ts)
    if not ts_list:
        return ""
    earliest = min(ts_list)
    latest = max(ts_list)
    if earliest == latest:
        return f"{heading}涉及记录均为 {earliest}（东八区{suffix}）。\n\n"
    return f"{heading}最早 {earliest}、最晚 {latest}（东八区{suffix}）。\n\n"
