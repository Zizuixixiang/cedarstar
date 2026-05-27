"""Global prompt management API for Mini App."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from memory.database import get_database
from memory.prompt_registry import get_prompt_definition, list_prompt_definitions

router = APIRouter()


class PromptUpdate(BaseModel):
    override_text: str


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


def _serialize_prompt(defn, override_row: Dict[str, Any] | None = None) -> Dict[str, Any]:
    override_text = ""
    updated_at = None
    if override_row:
        override_text = str(override_row.get("override_text") or "")
        updated_at = override_row.get("updated_at")
    return {
        "key": defn.key,
        "title": defn.title,
        "description": defn.description,
        "default_text": defn.default_text,
        "override_text": override_text,
        "effective_text": override_text.strip() or defn.default_text,
        "has_override": bool(override_text.strip()),
        "updated_at": updated_at,
    }


@router.get("")
async def list_prompts():
    db = get_database()
    overrides = await db.get_prompt_overrides()
    data = [
        _serialize_prompt(defn, overrides.get(defn.key))
        for defn in list_prompt_definitions()
    ]
    return create_response(True, data)


@router.get("/{key}")
async def get_prompt(key: str):
    defn = get_prompt_definition(key)
    if defn is None:
        raise HTTPException(status_code=404, detail="prompt key not found")
    db = get_database()
    overrides = await db.get_prompt_overrides()
    return create_response(True, _serialize_prompt(defn, overrides.get(defn.key)))


@router.put("/{key}")
async def update_prompt(key: str, body: PromptUpdate):
    defn = get_prompt_definition(key)
    if defn is None:
        raise HTTPException(status_code=404, detail="prompt key not found")
    text = str(body.override_text or "").strip()
    if not text:
        return create_response(False, None, "override_text 不能为空；如需恢复默认请使用 reset")
    db = get_database()
    await db.set_prompt_override(defn.key, text)
    overrides = await db.get_prompt_overrides()
    return create_response(True, _serialize_prompt(defn, overrides.get(defn.key)), "保存成功")


@router.post("/{key}/reset")
async def reset_prompt(key: str):
    defn = get_prompt_definition(key)
    if defn is None:
        raise HTTPException(status_code=404, detail="prompt key not found")
    db = get_database()
    await db.delete_prompt_override(defn.key)
    return create_response(True, _serialize_prompt(defn), "已恢复默认")
