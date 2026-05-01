"""
日志 API 模块。

提供日志查询接口。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from typing import Optional, Dict, Any

router = APIRouter()


def _parse_log_time_param(value: Optional[str]) -> Optional[datetime]:
    """
    解析查询串中的时间（ISO8601 / datetime-local 经前端 toISOString 后常见格式）。
    返回上海本地 naive datetime，与 PostgreSQL `logs.created_at TIMESTAMP`
    在 Asia/Shanghai 连接时区下写入的语义一致。
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"时间参数格式无效: {value!r}") from e
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return dt


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


@router.get("")
async def get_logs(
    platform: Optional[str] = Query(None, description="平台"),
    level: Optional[str] = Query(None, description="日志级别：DEBUG, INFO, WARNING, ERROR, CRITICAL"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    time_from: Optional[str] = Query(
        None, description="起始时间（含），ISO8601 字符串"
    ),
    time_to: Optional[str] = Query(
        None, description="结束时间（含），ISO8601 字符串"
    ),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量")
):
    """获取日志列表（SQL 层过滤 + 分页，不做全量加载）。"""
    from memory.database import get_database

    db = get_database()

    tf = _parse_log_time_param(time_from)
    tt = _parse_log_time_param(time_to)

    result = await db.get_logs_filtered(
        platform=platform or None,
        level=level or None,
        keyword=keyword or None,
        time_from=tf,
        time_to=tt,
        page=page,
        page_size=page_size,
    )

    return create_response(True, {
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "logs": result["logs"],
    })
