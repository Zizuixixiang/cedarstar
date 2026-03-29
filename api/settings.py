"""
设置 API 模块。

提供 API 配置和 Token 消耗统计接口。
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, Dict, Any, List, FrozenSet

router = APIRouter()

ALLOWED_API_CONFIG_TYPES: FrozenSet[str] = frozenset(
    {"chat", "summary", "vision", "stt", "embedding"}
)


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """创建统一格式的响应。"""
    return {"success": success, "data": data, "message": message}


class ApiConfigCreate(BaseModel):
    """API 配置创建模型。"""
    name: str
    api_key: str
    base_url: str
    model: Optional[str] = None
    persona_id: Optional[int] = None
    config_type: Optional[str] = 'chat'  # chat / summary / vision / stt / embedding


class ApiConfigUpdate(BaseModel):
    """API 配置更新模型。"""
    name: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    persona_id: Optional[int] = None
    config_type: Optional[str] = None


@router.get("/api-configs")
async def list_api_configs(config_type: Optional[str] = None):
    """返回所有 API 配置列表（key 字段脱敏，只返回末4位）。可按 config_type 过滤。"""
    from memory.database import get_database
    
    db = get_database()
    configs = db.get_all_api_configs(config_type=config_type)
    
    # 脱敏处理
    for config in configs:
        if config.get('api_key'):
            key = config['api_key']
            if len(key) > 4:
                config['api_key'] = "****" + key[-4:]
            else:
                config['api_key'] = "****"
    
    return create_response(True, configs)


@router.post("/api-configs")
async def create_api_config(config: ApiConfigCreate):
    """新增 API 配置。"""
    from memory.database import get_database

    ct = config.config_type or "chat"
    if ct not in ALLOWED_API_CONFIG_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"无效的 config_type，允许: {', '.join(sorted(ALLOWED_API_CONFIG_TYPES))}",
        )

    db = get_database()
    payload = config.model_dump()
    payload["config_type"] = ct
    config_id = db.save_api_config(payload)
    
    return create_response(True, {"id": config_id}, "创建成功")


@router.put("/api-configs/{config_id}")
async def update_api_config(config_id: int, config: ApiConfigUpdate):
    """更新 API 配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    
    # 只更新非 None 的字段
    update_data = {k: v for k, v in config.model_dump().items() if v is not None}
    if "config_type" in update_data and update_data["config_type"] not in ALLOWED_API_CONFIG_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"无效的 config_type，允许: {', '.join(sorted(ALLOWED_API_CONFIG_TYPES))}",
        )
    db.update_api_config(config_id, update_data)
    
    return create_response(True, None, "更新成功")


@router.delete("/api-configs/{config_id}")
async def delete_api_config(config_id: int):
    """删除 API 配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    
    db.delete_api_config(config_id)
    
    return create_response(True, None, "删除成功")


@router.put("/api-configs/{config_id}/activate")
async def activate_api_config(config_id: int):
    """切换当前激活配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    
    # 激活该配置
    db.activate_api_config(config_id)
    
    return create_response(True, None, "激活成功")


class FetchModelsRequest(BaseModel):
    """获取模型列表请求模型。"""
    api_key: str
    base_url: str


@router.post("/api-configs/fetch-models")
async def fetch_models(req: FetchModelsRequest):
    """调用对应 Base URL 的 /models 端点，返回模型列表。"""
    import requests as http_requests
    
    try:
        base_url = req.base_url.rstrip('/')
        response = http_requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {req.api_key}"},
            timeout=15
        )
        
        if response.status_code == 200:
            data = response.json()
            # 兼容 OpenAI 格式: {"data": [...]} 或直接列表
            models = data.get('data', data) if isinstance(data, dict) else data
            model_ids = [m.get('id', m) if isinstance(m, dict) else m for m in models]
            return create_response(True, model_ids)
        else:
            return create_response(False, None, f"获取模型列表失败: HTTP {response.status_code}")
    except Exception as e:
        return create_response(False, None, f"获取模型列表失败: {str(e)}")


@router.get("/token-usage")
async def get_token_usage(
    period: str = Query("today", description="统计周期：today, week, month"),
    platform: Optional[str] = Query(None, description="平台")
):
    """返回 token 消耗统计。"""
    from memory.database import get_database
    from datetime import datetime, timedelta
    
    db = get_database()
    
    # 计算时间范围
    now = datetime.now()
    if period == "today":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start_date = now - timedelta(days=7)
    elif period == "month":
        start_date = now - timedelta(days=30)
    else:
        raise HTTPException(status_code=400, detail="无效的统计周期")
    
    # 获取统计数据
    stats = db.get_token_usage_stats(start_date, platform)
    
    return create_response(True, stats)
