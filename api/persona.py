"""
人设配置 API 模块。

提供人设配置的增删改查接口。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


class PersonaCreate(BaseModel):
    """人设配置创建模型。"""
    name: str
    char_name: Optional[str] = ""
    char_personality: Optional[str] = ""
    char_speech_style: Optional[str] = ""
    user_name: Optional[str] = ""
    user_body: Optional[str] = ""
    user_habits: Optional[str] = ""
    user_likes_dislikes: Optional[str] = ""
    user_values: Optional[str] = ""
    user_hobbies: Optional[str] = ""
    user_taboos: Optional[str] = ""
    user_nsfw: Optional[str] = ""
    user_other: Optional[str] = ""
    system_rules: Optional[str] = ""


class PersonaUpdate(BaseModel):
    """人设配置更新模型。"""
    name: Optional[str] = None
    char_name: Optional[str] = None
    char_personality: Optional[str] = None
    char_speech_style: Optional[str] = None
    user_name: Optional[str] = None
    user_body: Optional[str] = None
    user_habits: Optional[str] = None
    user_likes_dislikes: Optional[str] = None
    user_values: Optional[str] = None
    user_hobbies: Optional[str] = None
    user_taboos: Optional[str] = None
    user_nsfw: Optional[str] = None
    user_other: Optional[str] = None
    system_rules: Optional[str] = None


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
    """获取拼接后的完整 system prompt 预览文本。"""
    from memory.database import get_database
    
    db = get_database()
    persona = await db.get_persona_config(persona_id)
    
    if not persona:
        raise HTTPException(status_code=404, detail="人设配置不存在")
    
    # 拼接完整的 system prompt
    parts = []
    
    # Char 人设部分
    if persona.get('char_name') or persona.get('char_personality') or persona.get('char_speech_style'):
        char_parts = []
        if persona.get('char_name'):
            char_parts.append(f"姓名：{persona['char_name']}")
        if persona.get('char_personality'):
            char_parts.append(f"性格：{persona['char_personality']}")
        if persona.get('char_speech_style'):
            char_parts.append(f"说话方式：{persona['char_speech_style']}")
        if char_parts:
            parts.append("【Char 人设】\n" + "\n".join(char_parts))
    
    if persona.get('user_name'):
        parts.append(f"【用户名称】\n{persona['user_name']}")
    
    if persona.get('user_body'):
        parts.append(f"【用户身体特征】\n{persona['user_body']}")
    
    if persona.get('user_habits'):
        parts.append(f"【用户习惯】\n{persona['user_habits']}")
    
    if persona.get('user_likes_dislikes'):
        parts.append(f"【用户喜恶】\n{persona['user_likes_dislikes']}")
    
    if persona.get('user_values'):
        parts.append(f"【用户价值观】\n{persona['user_values']}")
    
    if persona.get('user_hobbies'):
        parts.append(f"【用户爱好】\n{persona['user_hobbies']}")
    
    if persona.get('user_taboos'):
        parts.append(f"【用户禁忌】\n{persona['user_taboos']}")
    
    if persona.get('user_nsfw'):
        parts.append(f"【NSFW 设置】\n{persona['user_nsfw']}")
    
    if persona.get('user_other'):
        parts.append(f"【其他信息】\n{persona['user_other']}")
    
    if persona.get('system_rules'):
        parts.append(f"【系统规则】\n{persona['system_rules']}")
    
    preview = "\n\n".join(parts)
    
    return create_response(True, {"preview": preview})
