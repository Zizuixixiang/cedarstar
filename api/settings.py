"""
设置 API 模块。

提供 API 配置和 Token 消耗统计接口。
"""
import asyncio
import json
import logging
import time
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, Dict, Any, List, FrozenSet

logger = logging.getLogger(__name__)

router = APIRouter()


def _east8_month_start_naive():
    """东八区自然月：当月 1 日 00:00:00 起，按 PG 上海本地 naive timestamp 比较。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
    start_cn = now_cn.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_cn.replace(tzinfo=None)


def _east8_week_start_naive():
    """东八区自然周：周一 00:00:00 起，按 PG 上海本地 naive timestamp 比较。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
    start_cn = (
        now_cn - timedelta(days=now_cn.weekday())
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_cn.replace(tzinfo=None)


ALLOWED_API_CONFIG_TYPES: FrozenSet[str] = frozenset(
    {"chat", "summary", "vision", "stt", "tts", "embedding", "search_summary", "analysis"}
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
    config_type: Optional[str] = 'chat'
    voice_id: Optional[str] = None


class ApiConfigUpdate(BaseModel):
    """API 配置更新模型。"""
    name: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    persona_id: Optional[int] = None
    config_type: Optional[str] = None
    voice_id: Optional[str] = None


@router.get("/api-configs")
async def list_api_configs(config_type: Optional[str] = None):
    """返回所有 API 配置列表（key 字段脱敏，只返回末4位）。可按 config_type 过滤。"""
    from memory.database import get_database
    
    db = get_database()
    configs = await db.get_all_api_configs(config_type=config_type)
    
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
    config_id = await db.save_api_config(payload)
    
    return create_response(True, {"id": config_id}, "创建成功")


@router.put("/api-configs/{config_id}")
async def update_api_config(config_id: int, config: ApiConfigUpdate):
    """更新 API 配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = await db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    
    # 只更新非 None 的字段
    update_data = {k: v for k, v in config.model_dump().items() if v is not None}
    if "config_type" in update_data and update_data["config_type"] not in ALLOWED_API_CONFIG_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"无效的 config_type，允许: {', '.join(sorted(ALLOWED_API_CONFIG_TYPES))}",
        )
    await db.update_api_config(config_id, update_data)
    
    return create_response(True, None, "更新成功")


@router.delete("/api-configs/{config_id}")
async def delete_api_config(config_id: int):
    """删除 API 配置。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = await db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    
    await db.delete_api_config(config_id)
    
    return create_response(True, None, "删除成功")


@router.put("/api-configs/{config_id}/activate")
async def activate_api_config(config_id: int):
    """将配置加入激活池（同 config_type 可多条同时激活，按 id 顺序故障转移）。"""
    from memory.database import get_database
    
    db = get_database()
    
    # 检查是否存在
    existing = await db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    
    await db.activate_api_config(config_id)
    
    return create_response(True, None, "已加入激活池")


@router.put("/api-configs/{config_id}/deactivate")
async def deactivate_api_config(config_id: int):
    """从激活池移除指定配置（不激活其他条目）。"""
    from memory.database import get_database

    db = get_database()
    existing = await db.get_api_config(config_id)
    if not existing:
        raise HTTPException(status_code=404, detail="API 配置不存在")
    await db.deactivate_api_config(config_id)
    return create_response(True, None, "已取消激活")


class FetchModelsRequest(BaseModel):
    """获取模型列表请求模型。"""
    base_url: str
    api_key: Optional[str] = ""
    config_id: Optional[int] = None


def _extract_model_ids(payload: Any) -> List[str]:
    """从 OpenAI 兼容 /models 响应中解析模型 id 列表。"""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, list):
            items = inner
        elif isinstance(inner, dict) and isinstance(inner.get("data"), list):
            items = inner["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
        else:
            items = []
    else:
        items = []

    ids: List[str] = []
    seen: set = set()
    for m in items:
        mid: Optional[str] = None
        if isinstance(m, str):
            mid = m.strip()
        elif isinstance(m, dict):
            raw = m.get("id") or m.get("name") or m.get("model")
            mid = str(raw).strip() if raw else None
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    return ids


class ModelFavoriteCreate(BaseModel):
    """模型收藏请求。"""
    base_url: str
    model: str


@router.post("/api-configs/fetch-models")
async def fetch_models(req: FetchModelsRequest):
    """调用对应 Base URL 的 /models 端点，返回模型列表。"""
    import httpx
    from memory.database import get_database

    base_url = (req.base_url or "").strip().rstrip("/")
    if not base_url:
        return create_response(False, None, "请先填写 Base URL")

    api_key = (req.api_key or "").strip()
    if not api_key and req.config_id:
        existing = await get_database().get_api_config(int(req.config_id))
        if not existing:
            return create_response(False, None, "配置不存在")
        api_key = (existing.get("api_key") or "").strip()
    if not api_key:
        return create_response(False, None, "请先填写 API Key，或在编辑已保存配置时留空以使用库内 Key")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )

        if response.status_code == 200:
            data = response.json()
            model_ids = _extract_model_ids(data)
            if not model_ids:
                return create_response(False, None, "供应商返回了空的模型列表")
            return create_response(True, model_ids, f"获取到 {len(model_ids)} 个模型")
        body_snip = (response.text or "")[:200]
        return create_response(
            False,
            None,
            f"获取模型列表失败: HTTP {response.status_code}" + (f" — {body_snip}" if body_snip else ""),
        )
    except Exception as e:
        return create_response(False, None, f"获取模型列表失败: {str(e)}")


@router.get("/model-favorites")
async def list_model_favorites(base_url: Optional[str] = None):
    """按供应商 Base URL 返回收藏模型。"""
    from memory.database import get_database

    rows = await get_database().list_model_favorites(base_url)
    return create_response(True, rows)


@router.post("/model-favorites")
async def add_model_favorite(req: ModelFavoriteCreate):
    """收藏某供应商下的模型名。"""
    from memory.database import get_database

    if not req.base_url.strip() or not req.model.strip():
        raise HTTPException(status_code=400, detail="base_url 和 model 不能为空")
    fid = await get_database().add_model_favorite(req.base_url, req.model)
    return create_response(True, {"id": fid})


@router.delete("/model-favorites/{favorite_id}")
async def delete_model_favorite(favorite_id: int):
    """删除收藏模型。"""
    from memory.database import get_database

    ok = await get_database().delete_model_favorite(favorite_id)
    if not ok:
        raise HTTPException(status_code=404, detail="收藏模型不存在")
    return create_response(True, None, "已取消收藏")


def _ui_pref_key_group_order(config_type: str) -> str:
    return f"miniapp_settings_group_order_{config_type or 'chat'}"


def _ui_pref_key_favorite_order() -> str:
    return "miniapp_settings_favorite_model_order"


class UiPreferencesBody(BaseModel):
    """Mini App 核心设置 UI 偏好（存 config 表，不改 schema）。"""
    config_type: str = "chat"
    group_order: Optional[List[str]] = None
    favorite_model_order: Optional[Dict[str, List[str]]] = None


CHAT_LIKE_CONFIG_TYPES = frozenset(
    {"chat", "summary", "vision", "search_summary", "analysis"}
)

API_CONFIG_TEST_FIXED_KEY = "api_config_test_fixed_context_v1"
API_CONFIG_TEST_TARGET_CHARS = 20_000

CONFIG_TEST_SYSTEM = (
    "你是 CedarStar 的 API 配置测试助手。以下用户消息内含固定的历史对话抽样（约两万字），"
    "仅用于长上下文连通性压测，无需真正续写对话。"
)

CONFIG_TEST_TAIL_USER = (
    "【API 配置连通性测试】以上是固定的历史对话抽样（约两万字）。"
    "请只回复「收到」两个字，不要解释、不要续写、不要调用任何工具。"
)


def _api_config_test_fixed_storage_key() -> str:
    return API_CONFIG_TEST_FIXED_KEY


async def _sample_fixed_context_from_messages(db) -> tuple[str, int]:
    """从 messages 表倒序取样，按时间正序拼接至约两万字。"""
    target = API_CONFIG_TEST_TARGET_CHARS
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content
            FROM messages
            WHERE content IS NOT NULL AND TRIM(content) <> ''
            ORDER BY id DESC
            LIMIT 1500
            """
        )
    if not rows:
        placeholder = "（无历史消息，占位文本用于 API 配置长上下文测试。）\n"
        repeat = max(1, target // len(placeholder))
        return (placeholder * repeat)[:target], 0

    parts: List[str] = []
    total = 0
    msg_count = 0
    for row in reversed(rows):
        role = str(row["role"] or "user").strip()
        content = str(row["content"] or "").strip()
        if not content:
            continue
        block = f"[{role}] {content}\n\n"
        remain = target - total
        if remain <= 0:
            break
        if len(block) > remain:
            block = block[:remain]
        parts.append(block)
        total += len(block)
        msg_count += 1
    return "".join(parts), msg_count


async def _load_cached_fixed_context(db) -> tuple[str, int, bool]:
    """读取已缓存的固定测试文本；有则永远复用，不自动重抽。"""
    key = _api_config_test_fixed_storage_key()
    raw = await db.get_config(key)
    if not raw:
        return "", 0, False
    try:
        stored = json.loads(raw)
        if isinstance(stored, dict):
            text = str(stored.get("context_text") or "").strip()
            if text:
                return text, int(stored.get("source_message_count") or 0), True
    except json.JSONDecodeError:
        logger.warning("invalid cached api config test context")
    return "", 0, False


async def _persist_fixed_test_context(db, context_text: str, msg_count: int) -> None:
    await db.set_config(
        _api_config_test_fixed_storage_key(),
        json.dumps(
            {
                "context_text": context_text,
                "char_count": len(context_text),
                "source_message_count": msg_count,
            },
            ensure_ascii=False,
        ),
    )


async def build_fixed_test_context_from_db(db) -> Dict[str, Any]:
    """从 messages 表抽样约两万字并写入 config（仅在手動/首次建库时调用）。"""
    context_text, msg_count = await _sample_fixed_context_from_messages(db)
    await _persist_fixed_test_context(db, context_text, msg_count)
    logger.info(
        "built fixed api config test context: %s chars from %s messages",
        len(context_text),
        msg_count,
    )
    return {
        "char_count": len(context_text),
        "source_message_count": msg_count,
    }


async def _get_or_create_fixed_test_messages(
    db,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """测试时只读缓存；仅当从未抽样过时才从 DB 建一份。"""
    meta: Dict[str, Any] = {
        "used_fixed_context": False,
        "context_char_count": 0,
        "source_message_count": 0,
        "context_cached": False,
    }

    context_text, msg_count, cached = await _load_cached_fixed_context(db)
    if cached:
        meta["source_message_count"] = msg_count
        meta["context_cached"] = True
    else:
        built = await build_fixed_test_context_from_db(db)
        context_text, _ = await _load_cached_fixed_context(db)
        meta["source_message_count"] = built.get("source_message_count", 0)
        meta["context_cached"] = False

    meta["context_char_count"] = len(context_text)
    meta["used_fixed_context"] = bool(context_text)

    user_content = (
        f"{context_text}\n\n---\n\n{CONFIG_TEST_TAIL_USER}"
        if context_text
        else CONFIG_TEST_TAIL_USER
    )
    messages = [
        {"role": "system", "content": CONFIG_TEST_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    return messages, meta


@router.get("/ui-preferences")
async def get_ui_preferences(
    config_type: str = Query("chat", description="与 API 配置 Tab 一致"),
):
    from memory.database import get_database

    db = get_database()
    ct = (config_type or "chat").strip() or "chat"
    go_raw = await db.get_config(_ui_pref_key_group_order(ct), "[]")
    fo_raw = await db.get_config(_ui_pref_key_favorite_order(), "{}")
    try:
        group_order = json.loads(go_raw or "[]")
        if not isinstance(group_order, list):
            group_order = []
    except json.JSONDecodeError:
        group_order = []
    try:
        favorite_model_order = json.loads(fo_raw or "{}")
        if not isinstance(favorite_model_order, dict):
            favorite_model_order = {}
    except json.JSONDecodeError:
        favorite_model_order = {}
    return create_response(
        True,
        {"group_order": group_order, "favorite_model_order": favorite_model_order},
    )


@router.put("/ui-preferences")
async def put_ui_preferences(body: UiPreferencesBody):
    from memory.database import get_database

    db = get_database()
    ct = (body.config_type or "chat").strip() or "chat"
    if body.group_order is not None:
        await db.set_config(_ui_pref_key_group_order(ct), json.dumps(body.group_order, ensure_ascii=False))
    if body.favorite_model_order is not None:
        await db.set_config(
            _ui_pref_key_favorite_order(),
            json.dumps(body.favorite_model_order, ensure_ascii=False),
        )
    return create_response(True, None, "已保存")


def _extract_chat_reply(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)[:2000]
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    for key in ("output_text", "text", "content"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return json.dumps(data, ensure_ascii=False)[:2000]


@router.post("/api-configs/{config_id}/test")
async def test_api_config(config_id: int):
    """用固定约两万字的抽样长文本压测配置（首次从 messages 表生成并缓存）。"""
    import httpx
    from llm.llm_interface import LLMInterface
    from memory.database import get_database

    db = get_database()
    cfg = await db.get_api_config(config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="API 配置不存在")

    api_key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    config_type = (cfg.get("config_type") or "chat").strip() or "chat"

    if not api_key or not base_url:
        return create_response(False, None, "配置缺少 API Key 或 Base URL")
    if not model and config_type in CHAT_LIKE_CONFIG_TYPES:
        return create_response(False, None, "请先填写模型名再测试")

    if config_type == "stt":
        return create_response(False, None, "语音转录请上传音频文件测试，暂不支持一键探测")
    if config_type == "tts":
        return create_response(False, None, "语音合成暂不支持一键探测，请在对话中试听")

    try:
        if config_type == "embedding":
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "input": "CedarStar 配置连通性测试"}
            url = f"{base_url}/embeddings"
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                raw: Any
                try:
                    raw = response.json()
                except Exception:
                    raw = {"_raw_text": (response.text or "")[:4000]}
                if response.status_code >= 400:
                    detail = raw if isinstance(raw, dict) else {"_raw_text": str(raw)}
                    return create_response(
                        False,
                        {"http_status": response.status_code, "raw": detail},
                        f"测试失败: HTTP {response.status_code}",
                    )
                reply = _extract_chat_reply(raw)
                emb = raw.get("data") if isinstance(raw, dict) else None
                if isinstance(emb, list) and emb:
                    vec = emb[0].get("embedding") if isinstance(emb[0], dict) else None
                    if isinstance(vec, list):
                        reply = f"embedding 维度 {len(vec)}"
                return create_response(
                    True,
                    {
                        "reply": reply,
                        "raw": raw,
                        "http_status": response.status_code,
                        "message_count": 1,
                    },
                    "测试成功",
                )

        t_ctx = time.perf_counter()
        messages, ctx_meta = await _get_or_create_fixed_test_messages(db)
        context_build_ms = int((time.perf_counter() - t_ctx) * 1000)

        llm = LLMInterface(config_type=config_type, _db_cfg=cfg)
        prev_max_tokens = llm.max_tokens
        llm.max_tokens = 32
        t_llm = time.perf_counter()
        try:
            reply = await asyncio.to_thread(llm.generate_with_context, messages)
        finally:
            llm.max_tokens = prev_max_tokens
        llm_ms = int((time.perf_counter() - t_llm) * 1000)

        reply_text = (reply or "").strip() or "(无文本回复)"
        return create_response(
            True,
            {
                "reply": reply_text,
                "message_count": len(messages),
                "used_fixed_context": bool(ctx_meta.get("used_fixed_context")),
                "context_char_count": ctx_meta.get("context_char_count"),
                "source_message_count": ctx_meta.get("source_message_count"),
                "context_cached": bool(ctx_meta.get("context_cached")),
                "context_build_ms": context_build_ms,
                "llm_ms": llm_ms,
                "config_name": cfg.get("name"),
                "model": model,
            },
            "测试成功",
        )
    except Exception as e:
        logger.exception("api config test failed: config_id=%s", config_id)
        return create_response(False, None, f"测试请求失败: {str(e)}")


@router.get("/token-usage")
async def get_token_usage(
    period: str = Query(
        "today",
        description="统计周期：today（本日0点起，服务器本地）/ week（东八区自然周）/ month（东八区自然月月初至今）",
    ),
    platform: Optional[str] = Query(None, description="平台"),
):
    """返回 token 消耗统计。"""
    from memory.database import get_database
    from datetime import datetime, timedelta
    
    db = get_database()
    
    # 计算时间范围
    now = datetime.now()
    if period == "latest":
        stats = await db.get_latest_token_usage_stats(platform)
        return create_response(True, stats)
    elif period == "today":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start_date = _east8_week_start_naive()
    elif period == "month":
        start_date = _east8_month_start_naive()
    else:
        raise HTTPException(status_code=400, detail="无效的统计周期")
    
    # 获取统计数据
    stats = await db.get_token_usage_stats(start_date, platform)
    
    return create_response(True, stats)
