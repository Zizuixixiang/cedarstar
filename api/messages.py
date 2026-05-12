"""
消息记录 API。

统一查询私聊与群聊消息记录（分页 + 日期区间 + 关键词）。
"""
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


class MessageUpdateBody(BaseModel):
    """部分更新：至少提供 content 或 thinking 之一。"""

    content: Optional[str] = None
    thinking: Optional[str] = None


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


@router.patch("/{message_id}")
async def patch_group_message(message_id: int, body: MessageUpdateBody):
    """更新单条群聊共享消息的正文和/或思维链。"""
    if body.content is None and body.thinking is None:
        return create_response(False, None, "至少提供 content 或 thinking 之一")

    from memory.database import get_database

    db = get_database()
    try:
        ok = await db.update_shared_group_message_by_id(
            message_id,
            content=body.content,
            thinking=body.thinking,
        )
    except ValueError as e:
        return create_response(False, None, str(e))
    if not ok:
        return create_response(False, None, "消息不存在或未修改")
    return create_response(True, {"id": message_id}, "更新成功")


@router.delete("/{message_id}")
async def delete_group_message(message_id: int):
    """删除单条群聊共享消息。"""
    from memory.database import get_database

    db = get_database()
    try:
        ok = await db.delete_shared_group_message_by_id(message_id)
    except ValueError as e:
        return create_response(False, None, str(e))
    if not ok:
        return create_response(False, None, "消息不存在")
    return create_response(True, {"id": message_id}, "删除成功")
