"""
对话历史 API 模块。

提供对话历史的查询接口。
"""
from fastapi import APIRouter, Query
from typing import Optional, Dict, Any
from datetime import date

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


@router.get("")
async def get_history(
    platform: Optional[str] = Query(None, description="平台：discord, telegram"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    date_from: Optional[date] = Query(None, description="开始日期"),
    date_to: Optional[date] = Query(None, description="结束日期"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量")
):
    """获取对话历史（SQL 层过滤 + 分页，不做全量加载）。"""
    from memory.database import get_database

    db = get_database()

    # 将 date 对象转为字符串（SQLite date() 函数接受 'YYYY-MM-DD'）
    date_from_str = date_from.isoformat() if date_from else None
    date_to_str = date_to.isoformat() if date_to else None

    result = await db.get_messages_filtered(
        platform=platform or None,
        keyword=keyword or None,
        date_from=date_from_str,
        date_to=date_to_str,
        page=page,
        page_size=page_size,
    )

    return create_response(True, {
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "messages": result["messages"],
    })
