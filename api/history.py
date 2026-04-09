"""
对话历史 API 模块。

提供对话历史的查询、更新与删除接口。
"""
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import date

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


class MessageUpdateBody(BaseModel):
    """部分更新：至少提供 content 或 thinking 之一。"""

    content: Optional[str] = None
    thinking: Optional[str] = None


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

    kw = keyword.strip() if keyword else None
    result = await db.get_messages_filtered(
        platform=platform or None,
        keyword=kw or None,
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


@router.patch("/{message_id}")
async def patch_history_message(message_id: int, body: MessageUpdateBody):
    """更新单条消息的正文和/或思维链。"""
    if body.content is None and body.thinking is None:
        return create_response(False, None, "至少提供 content 或 thinking 之一")

    from memory.database import get_database

    db = get_database()
    ok = await db.update_message_by_id(
        message_id,
        content=body.content,
        thinking=body.thinking,
    )
    if not ok:
        return create_response(False, None, "消息不存在或未修改")
    return create_response(True, {"id": message_id}, "更新成功")


@router.delete("/{message_id}")
async def delete_history_message(message_id: int):
    """删除单条消息。"""
    from memory.database import get_database

    db = get_database()
    ok = await db.delete_message_by_id(message_id)
    if not ok:
        return create_response(False, None, "消息不存在")
    return create_response(True, {"id": message_id}, "删除成功")
