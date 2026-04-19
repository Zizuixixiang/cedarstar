"""
自主活动日记与手动触发 API（统一鉴权由 main.py 对 /api 注入）。
"""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


def _ok(data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": True, "data": data, "message": message}


@router.get("/diary")
async def list_autonomous_diary(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    from memory.database import get_database

    db = get_database()
    result = await db.get_autonomous_diaries(page=page, page_size=page_size)
    return _ok(
        {
            "total": result["total"],
            "items": result["items"],
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/diary/{diary_id}")
async def get_autonomous_diary_detail(diary_id: int):
    from memory.database import get_database

    db = get_database()
    row = await db.get_autonomous_diary_by_id(diary_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return _ok(row)


@router.post("/trigger")
async def trigger_autonomous():
    return {"success": True, "message": "已触发，功能开发中"}
