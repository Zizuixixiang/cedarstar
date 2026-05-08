"""星露谷自主模式：`stardew_autoplay` 配置读写（存于全局 `config` 表）。"""

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

CONFIG_KEY = "stardew_autoplay"


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _ok(data=None, message: str = "") -> Dict[str, Any]:
    return {"success": True, "data": data, "message": message}


class StardewAutoplayBody(BaseModel):
    enabled: bool


@router.get("/autoplay")
async def get_stardew_autoplay():
    from memory.database import get_database

    raw = await get_database().get_config(CONFIG_KEY, "false")
    return _ok({"enabled": _truthy(raw)})


@router.post("/autoplay")
async def set_stardew_autoplay(body: StardewAutoplayBody):
    from memory.database import get_database

    await get_database().set_config(CONFIG_KEY, "true" if body.enabled else "false")
    return _ok({"enabled": body.enabled}, "已保存")
