"""
日志 API 模块。

提供日志查询接口。
"""
from fastapi import APIRouter, Query
from typing import Optional, Dict, Any

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


@router.get("")
async def get_logs(
    platform: Optional[str] = Query(None, description="平台"),
    level: Optional[str] = Query(None, description="日志级别：DEBUG, INFO, WARNING, ERROR, CRITICAL"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量")
):
    """获取日志列表（SQL 层过滤 + 分页，不做全量加载）。"""
    from memory.database import get_database

    db = get_database()

    result = await db.get_logs_filtered(
        platform=platform or None,
        level=level or None,
        keyword=keyword or None,
        page=page,
        page_size=page_size,
    )

    return create_response(True, {
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "logs": result["logs"],
    })
