"""
配置 API 模块。

提供助手配置参数的获取和更新接口。
"""
from fastapi import APIRouter
from typing import Dict, Any, Optional
from memory.database import get_database

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    """
    创建统一格式的响应。
    
    Args:
        success: 是否成功
        data: 响应数据
        message: 消息说明
        
    Returns:
        统一格式的响应字典
    """
    return {
        "success": success,
        "data": data,
        "message": message
    }


# 默认配置值（使用后端期望的字段名）
DEFAULT_CONFIG = {
    "short_term_limit": 40,
    "buffer_delay": 5,
    "chunk_threshold": 50,
    "context_max_daily_summaries": 5,
    "context_max_longterm": 3,
    "daily_batch_hour": 23,
    "relationship_timeline_limit": 3,
    "gc_stale_days": 180,
    "gc_exempt_hits_threshold": 10,
    "retrieval_top_k": 5,
    "telegram_max_chars": 50,
    "telegram_max_msg": 8,
    "send_cot_to_telegram": 1,
}


def _normalize_telegram_config_value(key: str, value: Any) -> Optional[int]:
    """将 telegram_* 配置规范为合法整数；非法则返回 None。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if key == "telegram_max_chars":
        v = max(10, min(1000, round(v / 10) * 10))
        return v
    if key == "telegram_max_msg":
        return max(1, min(20, v))
    return None

# 内存缓存配置（从数据库加载）
_config = None


async def _load_config_from_db() -> Dict[str, Any]:
    """
    从数据库加载配置。
    
    Returns:
        Dict[str, Any]: 配置字典
    """
    db = get_database()
    db_configs = await db.get_all_configs()
    
    # 合并默认配置和数据库配置
    config = DEFAULT_CONFIG.copy()
    
    # 将数据库中的字符串值转换为适当类型
    for key in config.keys():
        if key in db_configs:
            value = db_configs[key]
            # 根据默认值的类型进行转换
            if isinstance(config[key], int):
                try:
                    config[key] = int(value)
                except (ValueError, TypeError):
                    pass  # 保持默认值
            elif isinstance(config[key], float):
                try:
                    config[key] = float(value)
                except (ValueError, TypeError):
                    pass  # 保持默认值
    
    return config


async def _save_config_to_db(config: Dict[str, Any]) -> bool:
    """
    保存配置到数据库。
    
    Args:
        config: 配置字典
        
    Returns:
        bool: 保存是否成功
    """
    db = get_database()
    success = True
    
    for key, value in config.items():
        if key in DEFAULT_CONFIG:
            if not await db.set_config(key, str(value)):
                success = False
    
    return success


async def _get_config() -> Dict[str, Any]:
    """
    获取配置（每次从数据库加载，确保获取最新值）。
    
    Returns:
        Dict[str, Any]: 配置字典
    """
    global _config
    
    # 每次调用都从数据库重新加载，确保获取最新配置
    _config = await _load_config_from_db()
    
    return _config


async def _payload_with_meta(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """在配置字典上附加 _meta.updated_at（助手相关 key 在库中的最近更新时间）。"""
    db = get_database()
    ts = await db.get_config_max_updated_at_for_keys(list(DEFAULT_CONFIG.keys()))
    out = dict(config_dict)
    out["_meta"] = {"updated_at": ts}
    return out


@router.get("/config")
async def get_config():
    """
    获取当前所有配置参数。
    """
    config = await _get_config()
    return create_response(True, await _payload_with_meta(config))


@router.put("/config")
async def update_config(new_config: Dict[str, Any]):
    """
    批量更新配置参数，热更新生效。
    
    Args:
        new_config: 新的配置字典
        
    Returns:
        更新后的配置
    """
    global _config
    
    # 获取当前配置
    config = await _get_config()
    
    # 验证并更新配置
    updated = False
    for key, value in new_config.items():
        if key in config:
            if key in ("telegram_max_chars", "telegram_max_msg"):
                norm = _normalize_telegram_config_value(key, value)
                if norm is not None and config[key] != norm:
                    config[key] = norm
                    updated = True
                continue
            # 根据默认值的类型进行转换
            if isinstance(config[key], int):
                try:
                    config[key] = int(value)
                    updated = True
                except (ValueError, TypeError):
                    pass  # 忽略无效值
            elif isinstance(config[key], float):
                try:
                    config[key] = float(value)
                    updated = True
                except (ValueError, TypeError):
                    pass  # 忽略无效值
            else:
                config[key] = value
                updated = True
    
    if updated:
        # 保存到数据库
        if await _save_config_to_db(config):
            # _get_config() 会刷新全局缓存；附带 _meta 供前端展示真实落库时间
            fresh = await _get_config()
            return create_response(True, await _payload_with_meta(fresh), "配置更新成功")
        else:
            return create_response(False, None, "配置保存到数据库失败")
    else:
        return create_response(False, None, "没有有效的配置更新")
