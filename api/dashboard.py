"""
控制台 API 模块。

提供系统状态概览接口，所有数据均从数据库真实读取。
"""
from fastapi import APIRouter
from typing import Dict, Any

router = APIRouter()

# ── Bot 在线状态共享标志 ──────────────────────────────────────────────
# 由各 bot 的 on_ready / on_disconnect 事件写入
_bot_status = {
    "discord": False,
    "telegram": False,
}

def set_bot_online(platform: str, online: bool):
    """由 bot 模块调用，更新在线状态。"""
    _bot_status[platform] = online
# ─────────────────────────────────────────────────────────────────────


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    return {"success": success, "data": data, "message": message}


@router.get("/status")
async def get_status():
    """
    获取 bot 在线状态、当前「对话 API」激活配置名、模型名。
    - bot_online：由 discord_bot / telegram_bot 的 on_ready 写入共享标志
    - active_api_config / model_name：`config_type='chat'` 且 is_active=1 的一条（与 Bot / LLMInterface 对话路径一致，非摘要 API）
    """
    from memory.database import get_database

    db = get_database()

    active_config_name = "未设置"
    model_name = "未设置"

    try:
        active = await db.get_active_api_config("chat")
        if active:
            active_config_name = active.get("name", "未设置")
            model_name = active.get("model") or "未设置"
    except Exception:
        pass

    return create_response(True, {
        "discord_online": _bot_status["discord"],
        "telegram_online": _bot_status["telegram"],
        "bot_online": _bot_status["discord"] or _bot_status["telegram"],
        "active_api_config": active_config_name,
        "model_name": model_name,
    })


@router.get("/batch-log")
async def get_batch_log():
    """
    获取最近 7 天 daily_batch_log 记录。
    返回字段：batch_date, step1_status … step5_status, error_message, created_at
    前端用 batch_date 匹配日期，step*_status 判断成功/失败。
    """
    from memory.database import get_database

    db = get_database()
    logs = await db.get_recent_daily_batch_logs(limit=7)
    return create_response(True, logs)


@router.get("/memory-overview")
async def get_memory_overview():
    """
    获取记忆概览：
    - chromadb_count：从 longterm_memories 表 COUNT(*)
    - short_term_limit：从 config 表读取
    - dimension_status：从 memory_cards 表查 is_active=1 的维度
    - chunk_summary_count：今日 chunk 摘要数量
    - latest_daily_summary_time：summaries 表最新 daily 记录的 created_at
    """
    from memory.database import get_database

    db = get_database()

    # 1. longterm_memories 条数（复用分页查询的 total_items，不加载全表）
    longterm_count = 0
    try:
        lt = await db.get_longterm_memories(keyword="", page=1, page_size=1)
        longterm_count = int(lt.get("total_items") or 0)
    except Exception:
        pass

    # 2. 配置参数
    short_term_limit = 40
    try:
        val2 = await db.get_config('short_term_limit')
        if val2 is not None:
            short_term_limit = int(val2)
    except Exception:
        pass

    # 3. 维度卡片状态（7 个维度）
    dimensions = [
        'preferences', 'interaction_patterns', 'current_status',
        'goals', 'relationships', 'key_events', 'rules'
    ]
    dimension_status = {d: False for d in dimensions}
    try:
        cards = await db.get_all_active_memory_cards()
        for card in cards:
            dim = card.get('dimension')
            if dim in dimension_status:
                dimension_status[dim] = True
    except Exception:
        pass

    # 4. 今日 chunk 摘要数量
    chunk_count = 0
    try:
        chunk_summaries = await db.get_today_chunk_summaries()
        chunk_count = len(chunk_summaries)
    except Exception:
        pass

    # 5. 最近 daily 摘要时间
    latest_daily_time = None
    try:
        daily_summaries = await db.get_recent_daily_summaries(limit=1)
        if daily_summaries:
            latest_daily_time = daily_summaries[0].get('created_at')
    except Exception:
        pass

    return create_response(True, {
        "chromadb_count": longterm_count,
        "short_term_limit": short_term_limit,
        "dimension_status": dimension_status,
        "chunk_summary_count": chunk_count,
        "latest_daily_summary_time": latest_daily_time,
    })
