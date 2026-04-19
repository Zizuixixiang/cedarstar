"""
传感器事件上报与聚合摘要 API。
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class SensorPushBody(BaseModel):
    event_type: str = Field(..., min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)


@router.post("")
async def post_sensor_event(body: SensorPushBody):
    from memory.database import get_database

    db = get_database()
    await db.save_sensor_event(body.event_type, body.payload)
    await db.purge_old_sensor_events(72)
    return {"success": True}


@router.get("/summary")
async def get_sensor_summary():
    from memory.database import get_database

    db = get_database()
    last_seen = await db.get_max_sensor_created_at_iso()
    is_active = await db.has_recent_sensor_event("screen", minutes=30)

    battery_row = await db.get_latest_sensor_by_type("battery")
    health_row = await db.get_latest_sensor_by_type("health")
    screen_row = await db.get_latest_sensor_by_type("screen")

    def _payload(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        p = row.get("payload")
        if isinstance(p, dict):
            return p
        return None

    return {
        "last_seen": last_seen,
        "is_active": is_active,
        "battery": _payload(battery_row),
        "health": _payload(health_row),
        "screen": _payload(screen_row),
    }
