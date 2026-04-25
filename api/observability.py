"""LLM 调用观测 API：token/cache 用量与工具执行记录。"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


def _period_start(period: str) -> datetime:
    now = datetime.now()
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
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


@router.get("/usage")
async def usage_observability(
    period: str = Query("today", description="today / week / month"),
    platform: Optional[str] = Query(None, description="按平台过滤"),
):
    """返回 token 与缓存观测聚合，不内置价格表。"""
    from memory.database import get_database

    db = get_database()
    stats = await db.get_token_observability_stats(_period_start(period), platform)
    stats["period"] = period
    stats["platform"] = platform
    return create_response(True, stats)


@router.get("/tool-executions")
async def recent_tool_executions(
    limit: int = Query(50, ge=1, le=200),
    platform: Optional[str] = Query(None, description="按平台过滤"),
    session_id: Optional[str] = Query(None, description="按 session_id 过滤"),
):
    """返回最近工具执行，raw 只给截断预览。"""
    from memory.database import get_database

    db = get_database()
    rows = await db.list_recent_tool_executions(
        limit=limit,
        platform=platform,
        session_id=session_id,
    )
    return create_response(True, rows)
