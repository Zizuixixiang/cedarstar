"""LLM 调用观测 API：token/cache 用量与工具执行记录。"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


def _period_start(period: str) -> datetime:
    if period == "today":
        now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
        start_cn = now_cn.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_cn.astimezone(timezone.utc).replace(tzinfo=None)
    if period == "week":
        now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
        start_cn = (
            now_cn - timedelta(days=now_cn.weekday())
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        return start_cn.astimezone(timezone.utc).replace(tzinfo=None)
    if period == "month":
        now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
        start_cn = now_cn.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start_cn.astimezone(timezone.utc).replace(tzinfo=None)
    raise HTTPException(status_code=400, detail="无效的统计周期")


def _norm_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row_dict(row: Any) -> Dict[str, Any]:
    return {key: _norm_value(value) for key, value in dict(row).items()}


def _int_value(row: Dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _provider_cache_hit_tokens(row: Dict[str, Any]) -> int:
    explicit = row.get("provider_cache_hit_tokens")
    if explicit is not None:
        try:
            return int(explicit or 0)
        except (TypeError, ValueError):
            pass
    return max(
        _int_value(row, "cache_hit_tokens"),
        _int_value(row, "cache_read_input_tokens"),
        _int_value(row, "cached_tokens"),
    )


async def _latest_usage_stats(db: Any, platform: Optional[str] = None) -> Dict[str, Any]:
    conditions = []
    params = []
    if platform:
        params.append(platform)
        conditions.append(f"tu.platform = ${len(params)}")
    where_sql = "AND " + " AND ".join(conditions) if conditions else ""
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT tu.id, tu.created_at, tu.platform, tu.model, tu.prompt_tokens,
                   tu.completion_tokens, tu.total_tokens, tu.cached_tokens,
                   tu.cache_write_tokens, tu.cache_hit_tokens, tu.cache_miss_tokens,
                   tu.cache_creation_input_tokens, tu.cache_read_input_tokens,
                   tu.raw_usage_json, tu.base_url,
                   GREATEST(
                       COALESCE(tu.cache_hit_tokens, 0),
                       COALESCE(tu.cache_read_input_tokens, 0),
                       COALESCE(tu.cached_tokens, 0)
                   ) AS provider_cache_hit_tokens
            FROM token_usage tu
            WHERE TRUE
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            *params,
        )
    if not row:
        empty_totals = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "provider_cache_hit_tokens": 0,
            "theoretical_cached_tokens": 0,
            "call_count": 0,
            "cache_hit_rate": 0,
            "theoretical_cache_hit_rate": 0,
        }
        return {"totals": empty_totals, "by_platform": [], "by_model": [], "by_day": [], "recent": []}

    item = _row_dict(row)
    prompt_tokens = _int_value(item, "prompt_tokens")
    hit_tokens = _provider_cache_hit_tokens(item)
    hit_rate = (hit_tokens / prompt_tokens) if prompt_tokens else 0
    totals = {
        "total_tokens": _int_value(item, "total_tokens"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": _int_value(item, "completion_tokens"),
        "cached_tokens": _int_value(item, "cached_tokens"),
        "cache_write_tokens": _int_value(item, "cache_write_tokens"),
        "cache_hit_tokens": _int_value(item, "cache_hit_tokens"),
        "cache_miss_tokens": _int_value(item, "cache_miss_tokens"),
        "cache_creation_input_tokens": _int_value(item, "cache_creation_input_tokens"),
        "cache_read_input_tokens": _int_value(item, "cache_read_input_tokens"),
        "provider_cache_hit_tokens": hit_tokens,
        "theoretical_cached_tokens": hit_tokens,
        "call_count": 1,
        "cache_hit_rate": hit_rate,
        "theoretical_cache_hit_rate": hit_rate,
    }
    platform_row = dict(totals)
    platform_row["platform"] = item.get("platform") or "unknown"
    model_row = dict(platform_row)
    model_row["model"] = item.get("model") or "unknown"
    return {
        "totals": totals,
        "by_platform": [platform_row],
        "by_model": [model_row],
        "by_day": [],
        "recent": [item],
    }


async def _range_usage_stats(db: Any, start_date: datetime, platform: Optional[str] = None) -> Dict[str, Any]:
    conditions = ["tu.created_at >= $1"]
    params = [start_date]
    if platform:
        params.append(platform)
        conditions.append(f"tu.platform = ${len(params)}")
    where_sql = "WHERE " + " AND ".join(conditions)
    provider_hit_expr = (
        "GREATEST("
        "COALESCE(tu.cache_hit_tokens, 0), "
        "COALESCE(tu.cache_read_input_tokens, 0), "
        "COALESCE(tu.cached_tokens, 0)"
        ")"
    )
    sum_sql = f"""
        SELECT SUM(tu.total_tokens), SUM(tu.prompt_tokens), SUM(tu.completion_tokens),
               SUM(tu.cached_tokens), SUM(tu.cache_write_tokens),
               SUM(tu.cache_hit_tokens), SUM(tu.cache_miss_tokens),
               SUM(tu.cache_creation_input_tokens), SUM(tu.cache_read_input_tokens),
               SUM({provider_hit_expr}), COUNT(*)
        FROM token_usage tu {where_sql}
    """
    by_platform_sql = f"""
        SELECT COALESCE(tu.platform, 'unknown') AS platform,
               SUM(tu.total_tokens) AS total_tokens,
               SUM(tu.prompt_tokens) AS prompt_tokens,
               SUM(tu.completion_tokens) AS completion_tokens,
               SUM(tu.cached_tokens) AS cached_tokens,
               SUM(tu.cache_write_tokens) AS cache_write_tokens,
               SUM(tu.cache_hit_tokens) AS cache_hit_tokens,
               SUM(tu.cache_miss_tokens) AS cache_miss_tokens,
               SUM(tu.cache_creation_input_tokens) AS cache_creation_input_tokens,
               SUM(tu.cache_read_input_tokens) AS cache_read_input_tokens,
               SUM({provider_hit_expr}) AS provider_cache_hit_tokens,
               COUNT(*) AS call_count
        FROM token_usage tu {where_sql}
        GROUP BY COALESCE(tu.platform, 'unknown')
        ORDER BY total_tokens DESC
    """
    by_model_sql = f"""
        SELECT COALESCE(tu.model, 'unknown') AS model,
               SUM(tu.total_tokens) AS total_tokens,
               SUM(tu.prompt_tokens) AS prompt_tokens,
               SUM(tu.completion_tokens) AS completion_tokens,
               SUM(tu.cached_tokens) AS cached_tokens,
               SUM(tu.cache_write_tokens) AS cache_write_tokens,
               SUM(tu.cache_hit_tokens) AS cache_hit_tokens,
               SUM(tu.cache_miss_tokens) AS cache_miss_tokens,
               SUM(tu.cache_creation_input_tokens) AS cache_creation_input_tokens,
               SUM(tu.cache_read_input_tokens) AS cache_read_input_tokens,
               SUM({provider_hit_expr}) AS provider_cache_hit_tokens,
               COUNT(*) AS call_count
        FROM token_usage tu {where_sql}
        GROUP BY COALESCE(tu.model, 'unknown')
        ORDER BY total_tokens DESC
        LIMIT 20
    """
    by_day_sql = f"""
        SELECT tu.created_at::date AS day,
               SUM(tu.total_tokens) AS total_tokens,
               SUM(tu.prompt_tokens) AS prompt_tokens,
               SUM(tu.completion_tokens) AS completion_tokens,
               SUM(tu.cached_tokens) AS cached_tokens,
               SUM(tu.cache_write_tokens) AS cache_write_tokens,
               SUM(tu.cache_hit_tokens) AS cache_hit_tokens,
               SUM(tu.cache_miss_tokens) AS cache_miss_tokens,
               SUM(tu.cache_creation_input_tokens) AS cache_creation_input_tokens,
               SUM(tu.cache_read_input_tokens) AS cache_read_input_tokens,
               SUM({provider_hit_expr}) AS provider_cache_hit_tokens,
               COUNT(*) AS call_count
        FROM token_usage tu {where_sql}
        GROUP BY tu.created_at::date
        ORDER BY day DESC
        LIMIT 31
    """
    recent_sql = f"""
        SELECT tu.id, tu.created_at, tu.platform, tu.model, tu.prompt_tokens,
               tu.completion_tokens, tu.total_tokens, tu.cached_tokens,
               tu.cache_write_tokens, tu.cache_hit_tokens, tu.cache_miss_tokens,
               tu.cache_creation_input_tokens, tu.cache_read_input_tokens,
               {provider_hit_expr} AS provider_cache_hit_tokens,
               tu.raw_usage_json
        FROM token_usage tu {where_sql}
        ORDER BY tu.created_at DESC, tu.id DESC
        LIMIT 50
    """
    async with db.pool.acquire() as conn:
        totals = await conn.fetchrow(sum_sql, *params)
        rows_platform = await conn.fetch(by_platform_sql, *params)
        rows_model = await conn.fetch(by_model_sql, *params)
        rows_day = await conn.fetch(by_day_sql, *params)
        recent_rows = await conn.fetch(recent_sql, *params)

    prompt_tokens = (totals[1] or 0) if totals else 0
    hit_tokens = (totals[9] or 0) if totals else 0
    hit_rate = (hit_tokens / prompt_tokens) if prompt_tokens else 0
    return {
        "totals": {
            "total_tokens": (totals[0] or 0) if totals else 0,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": (totals[2] or 0) if totals else 0,
            "cached_tokens": (totals[3] or 0) if totals else 0,
            "cache_write_tokens": (totals[4] or 0) if totals else 0,
            "cache_hit_tokens": (totals[5] or 0) if totals else 0,
            "cache_miss_tokens": (totals[6] or 0) if totals else 0,
            "cache_creation_input_tokens": (totals[7] or 0) if totals else 0,
            "cache_read_input_tokens": (totals[8] or 0) if totals else 0,
            "provider_cache_hit_tokens": hit_tokens,
            "theoretical_cached_tokens": hit_tokens,
            "call_count": (totals[10] or 0) if totals else 0,
            "cache_hit_rate": hit_rate,
            "theoretical_cache_hit_rate": hit_rate,
        },
        "by_platform": [_row_dict(r) for r in rows_platform],
        "by_model": [_row_dict(r) for r in rows_model],
        "by_day": [_row_dict(r) for r in rows_day],
        "recent": [_row_dict(r) for r in recent_rows],
    }


@router.get("/usage")
async def usage_observability(
    period: str = Query("today", description="current / today / week / month"),
    platform: Optional[str] = Query(None, description="按平台过滤"),
):
    """返回 token 与缓存观测聚合，不内置价格表。"""
    from memory.database import get_database

    db = get_database()
    if period == "current":
        stats = await _latest_usage_stats(db, platform)
    else:
        stats = await _range_usage_stats(db, _period_start(period), platform)
    stats["period"] = period
    stats["platform"] = platform
    return create_response(True, stats)


@router.get("/tool-executions")
async def recent_tool_executions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    platform: Optional[str] = Query(None, description="按平台过滤"),
    session_id: Optional[str] = Query(None, description="按 session_id 过滤"),
):
    """返回最近工具执行，raw 只给截断预览。"""
    from memory.database import get_database

    db = get_database()
    rows = await db.list_recent_tool_executions(
        limit=limit,
        offset=offset,
        platform=platform,
        session_id=session_id,
    )
    return create_response(True, rows)
