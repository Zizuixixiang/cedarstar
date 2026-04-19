"""
人设配置 API 模块。

提供人设配置的增删改查接口。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

from memory.context_builder import build_persona_config_system_body

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


class PersonaCreate(BaseModel):
    """人设配置创建模型。"""
    name: str
    char_name: Optional[str] = ""
    char_identity: Optional[str] = ""
    char_personality: Optional[str] = ""
    char_speech_style: Optional[str] = ""
    char_redlines: Optional[str] = ""
    char_appearance: Optional[str] = ""
    char_relationships: Optional[str] = ""
    char_nsfw: Optional[str] = ""
    char_tools_guide: Optional[str] = ""
    char_offline_mode: Optional[str] = ""
    user_name: Optional[str] = ""
    user_body: Optional[str] = ""
    user_work: Optional[str] = ""
    user_habits: Optional[str] = ""
    user_likes_dislikes: Optional[str] = ""
    user_values: Optional[str] = ""
    user_hobbies: Optional[str] = ""
    user_taboos: Optional[str] = ""
    user_nsfw: Optional[str] = ""
    user_other: Optional[str] = ""
    system_rules: Optional[str] = ""
    enable_lutopia: Optional[int] = 0
    enable_weather_tool: Optional[int] = 0
    enable_weibo_tool: Optional[int] = 0
    enable_search_tool: Optional[int] = 0


class PersonaUpdate(BaseModel):
    """人设配置更新模型。"""
    name: Optional[str] = None
    char_name: Optional[str] = None
    char_identity: Optional[str] = None
    char_personality: Optional[str] = None
    char_speech_style: Optional[str] = None
    char_redlines: Optional[str] = None
    char_appearance: Optional[str] = None
    char_relationships: Optional[str] = None
    char_nsfw: Optional[str] = None
    char_tools_guide: Optional[str] = None
    char_offline_mode: Optional[str] = None
    user_name: Optional[str] = None
    user_body: Optional[str] = None
    user_work: Optional[str] = None
    user_habits: Optional[str] = None
    user_likes_dislikes: Optional[str] = None
    user_values: Optional[str] = None
    user_hobbies: Optional[str] = None
    user_taboos: Optional[str] = None
    user_nsfw: Optional[str] = None
    user_other: Optional[str] = None
    system_rules: Optional[str] = None
    enable_lutopia: Optional[int] = None
    enable_weather_tool: Optional[int] = None
    enable_weibo_tool: Optional[int] = None
    enable_search_tool: Optional[int] = None


class PersonaResponse(BaseModel):
    """人设配置详情（与 persona_configs 行一致，供 OpenAPI / 类型参考）。"""
    id: int
    name: Optional[str] = None
    char_name: Optional[str] = ""
    char_identity: Optional[str] = ""
    char_personality: Optional[str] = ""
    char_speech_style: Optional[str] = ""
    char_redlines: Optional[str] = ""
    char_appearance: Optional[str] = ""
    char_relationships: Optional[str] = ""
    char_nsfw: Optional[str] = ""
    char_tools_guide: Optional[str] = ""
    char_offline_mode: Optional[str] = ""
    user_name: Optional[str] = ""
    user_body: Optional[str] = ""
    user_work: Optional[str] = ""
    user_habits: Optional[str] = ""
    user_likes_dislikes: Optional[str] = ""
    user_values: Optional[str] = ""
    user_hobbies: Optional[str] = ""
    user_taboos: Optional[str] = ""
    user_nsfw: Optional[str] = ""
    user_other: Optional[str] = ""
    system_rules: Optional[str] = ""
    enable_lutopia: Optional[int] = 0
    enable_weather_tool: Optional[int] = 0
    enable_weibo_tool: Optional[int] = 0
    enable_search_tool: Optional[int] = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@router.get("")
async def list_personas():
    """获取所有人设配置列表。"""
    from memory.database import get_database
    
    db = get_database()
    personas = await db.get_all_persona_configs()
    
    return create_response(True, personas)


@router.get("/{persona_id}")
async def get_persona(persona_id: int):
    """获取单个人设配置详情。"""
    from memory.database import get_database
    
    db = get_database()
    persona = await db.get_persona_config(persona_id)
    
    if not persona:
        raise HTTPException(status_code=404, detail="人设配置不存在")
    
    return create_response(True, persona)


@router.post("")
async def create_persona(persona: PersonaCreate):
    """新增人设配置。"""
    from memory.database import get_database
    
    db = get_database()
    persona_id = await db.save_persona_config(persona.model_dump())
    
    return create_response(True, {"id": persona_id}, "创建成功")


@router.put("/{persona_id}")
async def update_persona(persona_id: int, persona: PersonaUpdate):
    """更新人设配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = await db.get_persona_config(persona_id)
    if not existing:
        raise HTTPException(status_code=404, detail="人设配置不存在")
    
    # 只更新非 None 的字段
    update_data = {k: v for k, v in persona.model_dump().items() if v is not None}
    await db.update_persona_config(persona_id, update_data)
    
    return create_response(True, None, "更新成功")


@router.delete("/{persona_id}")
async def delete_persona(persona_id: int):
    """删除人设配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = await db.get_persona_config(persona_id)
    if not existing:
        raise HTTPException(status_code=404, detail="人设配置不存在")
    
    await db.delete_persona_config(persona_id)
    
    return create_response(True, None, "删除成功")


@router.get("/{persona_id}/preview")
async def preview_persona(persona_id: int):
    """获取拼接后的完整 system prompt 预览文本（与 context_builder / Mini App 预览一致）。"""
    from memory.database import get_database
    
    db = get_database()
    persona = await db.get_persona_config(persona_id)
    
    if not persona:
        raise HTTPException(status_code=404, detail="人设配置不存在")
    
    preview = build_persona_config_system_body(persona)
    
    return create_response(True, {"preview": preview})
