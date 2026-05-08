"""
消息记录 API。

统一查询私聊与群聊消息记录（分页 + 日期区间 + 关键词）。
"""
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


@router.get("")
async def get_messages(
    type: str = Query(..., description="消息类型：private/group"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    keyword: Optional[str] = Query(None, description="content 关键词搜索"),
    date_from: Optional[date] = Query(None, description="开始日期"),
    date_to: Optional[date] = Query(None, description="结束日期"),
    session_id: Optional[str] = Query(None, description="私聊会话ID（type=private 必填）"),
    chat_id: Optional[str] = Query(None, description="群聊 chat_id（type=group 必填）"),
):
    from memory.database import get_database

    mt = (type or "").strip().lower()
    if mt not in {"private", "group"}:
        return create_response(False, None, "type 仅支持 private 或 group")
    db = get_database()
    try:
        result = await db.get_messages_by_type(
            message_type=mt,
            page=page,
            page_size=page_size,
            keyword=(keyword or "").strip() or None,
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None,
            session_id=(session_id or "").strip() or None,
            chat_id=(chat_id or "").strip() or None,
        )
    except ValueError as e:
        return create_response(False, None, str(e))

    return create_response(
        True,
        {
            "type": mt,
            "total": result.get("total", 0),
            "page": page,
            "page_size": page_size,
            "items": result.get("items", []),
        },
    )
