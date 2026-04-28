"""
记忆管理 API 模块。
提供记忆卡片管理接口和长期记忆库接口。
长期记忆：创建时先写 ChromaDB（成功后再写 SQLite）；删除时先删 SQLite 再删 ChromaDB。
GET /longterm 从 ChromaDB 分页全量列出；SQLite 镜像仅对手动条目维护，列表以向量库为准。
"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Dict, Any, List, Optional
import datetime
import logging
import uuid

logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic 模型
class MemoryCardCreate(BaseModel):
    user_id: str
    character_id: str
    dimension: str
    content: str
    source_message_id: Optional[str] = None


class MemoryCardUpdate(BaseModel):
    content: str
    dimension: Optional[str] = None


class LongTermMemoryCreate(BaseModel):
    content: str
    score: Optional[int] = 5
    halflife_days: Optional[int] = 30


class LongTermMetadataPatch(BaseModel):
    halflife_days: Optional[int] = None
    arousal: Optional[float] = None


class TemporalStateCreate(BaseModel):
    state_content: str
    action_rule: Optional[str] = None
    expire_at: Optional[str] = None


class SummaryTextPatch(BaseModel):
    summary_text: str


class SummaryStarPatch(BaseModel):
    is_starred: bool


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    return {"success": success, "data": data, "message": message}


def _is_chroma_doc_id_missing(chroma_doc_id: Any) -> bool:
    """chroma_doc_id 为空、仅空白或历史异常值时视为无向量关联（孤儿行）。"""
    if chroma_doc_id is None:
        return True
    if isinstance(chroma_doc_id, str) and not chroma_doc_id.strip():
        return True
    return False


def _annotate_longterm_query_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """为列表中每条长期记忆增加 is_orphan（chroma_doc_id 缺失时为 True）。"""
    items_out: List[Dict[str, Any]] = []
    for row in result.get("items", []):
        d = dict(row) if isinstance(row, dict) else row
        cid = d.get("chroma_doc_id")
        d = {**d, "is_orphan": _is_chroma_doc_id_missing(cid)}
        items_out.append(d)
    return {**result, "items": items_out}


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _annotate_longterm_chroma_stats(result: Dict[str, Any]) -> Dict[str, Any]:
    """为每条长期记忆附加 Chroma 元数据：hits、halflife_days、last_access_ts（孤儿行为 null）。"""
    from memory.vector_store import get_memory_metadatas_by_doc_ids

    items = result.get("items", [])
    doc_ids = [
        i.get("chroma_doc_id")
        for i in items
        if not _is_chroma_doc_id_missing(i.get("chroma_doc_id"))
    ]
    meta_by_id = get_memory_metadatas_by_doc_ids(doc_ids) if doc_ids else {}
    items_out: List[Dict[str, Any]] = []
    for row in items:
        d = dict(row) if isinstance(row, dict) else row
        cid = d.get("chroma_doc_id")
        if _is_chroma_doc_id_missing(cid):
            d = {
                **d,
                "hits": None,
                "halflife_days": None,
                "last_access_ts": None,
                "arousal": None,
            }
        else:
            md = meta_by_id.get(cid) or {}
            lat = md.get("last_access_ts")
            last_ts: Optional[float] = None
            if lat is not None:
                try:
                    last_ts = float(lat)
                except (TypeError, ValueError):
                    last_ts = None
            raw_arousal = md.get("arousal")
            arousal_val: Optional[float] = None
            if raw_arousal is not None:
                try:
                    arousal_val = float(raw_arousal)
                except (TypeError, ValueError):
                    arousal_val = None
            d = {
                **d,
                "hits": _safe_int(md.get("hits"), 0),
                "halflife_days": _safe_int(md.get("halflife_days"), 30),
                "last_access_ts": last_ts,
                "arousal": arousal_val,
            }
        items_out.append(d)
    return {**result, "items": items_out}


# ==========================================
# 记忆卡片接口（读取真实 memory_cards 表）
# ==========================================

@router.get("/cards")
async def get_memory_cards(
    user_id: Optional[str] = None,
    character_id: Optional[str] = None,
    dimension: Optional[str] = None,
    limit: int = 50
):
    """获取记忆卡片列表（从真实数据库读取）。"""
    from memory.database import get_database
    
    try:
        db = get_database()
        
        if user_id and character_id:
            cards = await db.get_memory_cards(user_id, character_id, dimension, limit)
        else:
            # 获取所有激活的卡片
            cards = await db.get_all_active_memory_cards(limit=limit)
        
        return create_response(True, cards, "获取记忆卡片成功")
    except Exception as e:
        logger.error(f"获取记忆卡片失败: {e}")
        return create_response(False, None, f"获取记忆卡片失败: {str(e)}")


@router.post("/cards")
async def create_memory_card(card_data: MemoryCardCreate):
    """创建记忆卡片。"""
    from memory.database import save_memory_card
    
    try:
        card_id = await save_memory_card(
            card_data.user_id,
            card_data.character_id,
            card_data.dimension,
            card_data.content,
            card_data.source_message_id
        )
        return create_response(True, {"card_id": card_id}, "创建记忆卡片成功")
    except Exception as e:
        logger.error(f"创建记忆卡片失败: {e}")
        return create_response(False, None, f"创建记忆卡片失败: {str(e)}")


@router.put("/cards/{card_id}")
async def update_memory_card(card_id: int, body: MemoryCardUpdate):
    """更新记忆卡片。"""
    from memory.database import update_memory_card
    
    try:
        updated = await update_memory_card(card_id, body.content, body.dimension)
        if updated:
            return create_response(True, {"card_id": card_id}, "更新记忆卡片成功")
        else:
            return create_response(False, None, "卡片不存在")
    except Exception as e:
        logger.error(f"更新记忆卡片失败: {e}")
        return create_response(False, None, f"更新记忆卡片失败: {str(e)}")


@router.delete("/cards/{card_id}")
async def deactivate_memory_card(card_id: int):
    """停用记忆卡片（软删除）。"""
    from memory.database import deactivate_memory_card
    
    try:
        deactivated = await deactivate_memory_card(card_id)
        if deactivated:
            return create_response(True, {"card_id": card_id}, "停用记忆卡片成功")
        else:
            return create_response(False, None, "卡片不存在")
    except Exception as e:
        logger.error(f"停用记忆卡片失败: {e}")
        return create_response(False, None, f"停用记忆卡片失败: {str(e)}")


# ==========================================
# summaries 表（chunk / daily 摘要管理）
# ==========================================


@router.get("/summaries")
async def list_summaries(
    summary_type: Optional[str] = None,
    source_date_from: Optional[str] = None,
    source_date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    """分页列出 summaries；可选按 summary_type、source_date 区间（起止 YYYY-MM-DD，可只填一侧）过滤。"""
    from memory.database import get_summaries_filtered

    st = (summary_type or "").strip() or None
    if st is not None and st not in ("chunk", "daily"):
        return create_response(False, None, "summary_type 须为 chunk 或 daily")

    d_from = (source_date_from or "").strip() or None
    d_to = (source_date_to or "").strip() or None

    try:
        items, total = await get_summaries_filtered(
            page=page,
            page_size=page_size,
            summary_type=st,
            source_date_from=d_from,
            source_date_to=d_to,
        )
    except ValueError as e:
        return create_response(False, None, str(e))
    except Exception as e:
        logger.error(f"列出 summaries 失败: {e}")
        return create_response(False, None, f"查询失败: {str(e)}")

    payload = {
        "items": items,
        "total": total,
        "page": max(1, page),
        "page_size": max(1, min(page_size, 200)),
    }
    return create_response(True, payload, "获取摘要列表成功")


@router.patch("/summaries/{summary_id}")
async def patch_summary(summary_id: int, body: SummaryTextPatch):
    """更新单条 summary 正文。"""
    from memory.database import update_summary_by_id

    text = (body.summary_text or "").strip()
    if not text:
        return create_response(False, None, "summary_text 不能为空")

    try:
        ok = await update_summary_by_id(summary_id, text)
        if ok:
            return create_response(True, {"id": summary_id}, "更新成功")
        return create_response(False, None, "记录不存在")
    except Exception as e:
        logger.error(f"更新 summary 失败 id={summary_id}: {e}")
        return create_response(False, None, f"更新失败: {str(e)}")


@router.patch("/summaries/{summary_id}/star")
async def patch_summary_star(summary_id: int, body: SummaryStarPatch):
    """收藏/取消收藏 chunk summary，并同步引用它的长期事件与 Chroma metadata。"""
    from memory.database import (
        set_summary_starred,
        recalculate_longterm_starred_for_chunk,
    )
    from memory.vector_store import update_memory_metadata_fields

    try:
        ok = await set_summary_starred(summary_id, bool(body.is_starred))
        if not ok:
            return create_response(False, None, "记录不存在")

        changed = await recalculate_longterm_starred_for_chunk(summary_id)
        updates = {
            row["chroma_doc_id"]: {"is_starred": bool(row["is_starred"])}
            for row in changed
            if row.get("chroma_doc_id")
        }
        chroma_updated = update_memory_metadata_fields(updates) if updates else 0
        return create_response(
            True,
            {
                "id": summary_id,
                "is_starred": bool(body.is_starred),
                "longterm_updated": len(changed),
                "chroma_updated": chroma_updated,
            },
            "收藏状态已更新",
        )
    except Exception as e:
        logger.error("更新 summary 收藏失败 id=%s: %s", summary_id, e)
        return create_response(False, None, f"更新失败: {str(e)}")


@router.delete("/summaries/{summary_id}")
async def delete_summary(summary_id: int):
    """物理删除单条 summary。"""
    from memory.database import delete_summary_by_id

    try:
        ok = await delete_summary_by_id(summary_id)
        if ok:
            return create_response(True, {"id": summary_id}, "删除成功")
        return create_response(False, None, "记录不存在")
    except Exception as e:
        logger.error(f"删除 summary 失败 id={summary_id}: {e}")
        return create_response(False, None, f"删除失败: {str(e)}")


# ==========================================
# 长期记忆接口（ChromaDB 与 SQLite 镜像表，创建/删除顺序见各端点文档字符串）
# ==========================================

@router.get("/longterm")
async def get_longterm_memories(
    page: int = 1,
    page_size: int = 20,
    summary_type: Optional[str] = None,
):
    """从 ChromaDB 分页拉取长期记忆全量；可选按 metadata.summary_type 过滤。"""
    from memory.vector_store import get_vector_store

    try:
        vs = get_vector_store()
        st = (summary_type or "").strip() or None
        where = {"summary_type": st} if st else None

        if where:
            filt = vs.collection.get(where=where, include=["metadatas"])
            total = len(filt.get("ids") or [])
        else:
            total = vs.collection.count()

        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size

        items: List[Dict[str, Any]] = []
        if total > 0 and offset < total:
            result = vs.collection.get(
                limit=page_size,
                offset=offset,
                where=where,
                include=["documents", "metadatas"],
            )
            ids = result.get("ids") or []
            docs = result.get("documents") or []
            metas = result.get("metadatas") or []
            for i, doc_id in enumerate(ids):
                meta = metas[i] if i < len(metas) else None
                meta = dict(meta or {})
                doc = docs[i] if i < len(docs) else ""
                lat = meta.get("last_access_ts")
                last_ts: Optional[float] = None
                if lat is not None:
                    try:
                        last_ts = float(lat)
                    except (TypeError, ValueError):
                        last_ts = None
                raw_arousal = meta.get("arousal")
                arousal_val: Optional[float] = None
                if raw_arousal is not None:
                    try:
                        arousal_val = float(raw_arousal)
                    except (TypeError, ValueError):
                        arousal_val = None
                raw_base = meta.get("base_score")
                base_score = 5.0
                if raw_base is not None:
                    try:
                        base_score = float(raw_base)
                    except (TypeError, ValueError):
                        base_score = 5.0
                items.append(
                    {
                        "chroma_doc_id": doc_id,
                        "content": doc or "",
                        "is_manual": str(doc_id).startswith("manual_"),
                        "summary_type": meta.get("summary_type"),
                        "date": meta.get("date"),
                        "hits": _safe_int(meta.get("hits"), 0),
                        "halflife_days": _safe_int(meta.get("halflife_days"), 30),
                        "arousal": arousal_val,
                        "last_access_ts": last_ts,
                        "base_score": base_score,
                        "is_starred": bool(meta.get("is_starred")),
                        "source_chunk_ids": meta.get("source_chunk_ids"),
                    }
                )

        payload = {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
        return create_response(True, payload, "获取长期记忆成功")
    except Exception as e:
        logger.error(f"获取长期记忆失败: {e}")
        return create_response(False, None, f"获取长期记忆失败: {str(e)}")


@router.post("/longterm")
async def create_longterm_memory(body: LongTermMemoryCreate):
    """
    新增长期记忆。
    先写入 ChromaDB（成功后再写 SQLite，避免 chroma 失败产生无向量关联行）。
    """
    from memory.database import get_database
    from memory.vector_store import get_vector_store
    
    content = body.content.strip()
    if not content:
        return create_response(False, None, "内容不能为空")
    
    db = get_database()
    chroma_doc_id = f"manual_{uuid.uuid4().hex}"
    today = datetime.date.today().isoformat()
    store = get_vector_store()
    
    try:
        chroma_ok = store.add_memory(
            doc_id=chroma_doc_id,
            text=content,
            metadata={
                "date": today,
                "session_id": "manual",
                "summary_type": "manual",
                "source": "miniapp_manual",
                "base_score": float(body.score if body.score is not None else 5),
                "halflife_days": int(body.halflife_days if body.halflife_days is not None else 30),
            }
        )
    except Exception as e:
        logger.error(f"写入 ChromaDB 失败，已跳过 SQLite: {e}")
        return create_response(False, None, f"创建长期记忆失败：ChromaDB 异常: {str(e)}")
    
    if not chroma_ok:
        logger.error("写入 ChromaDB 返回失败，已跳过 SQLite")
        return create_response(False, None, "创建长期记忆失败：ChromaDB 写入未成功")
    
    try:
        memory_id = await db.create_longterm_memory(
            content=content, chroma_doc_id=chroma_doc_id, score=(body.score if body.score is not None else 5)
        )
    except Exception as e:
        logger.error(f"ChromaDB 已成功但写入 SQLite 长期记忆失败: {e}")
        try:
            rolled_back = store.delete_memory(chroma_doc_id)
            if not rolled_back:
                logger.error(f"回滚 ChromaDB 文档未成功 doc_id={chroma_doc_id}")
        except Exception as cleanup_e:
            logger.error(f"回滚 ChromaDB 文档异常 doc_id={chroma_doc_id}: {cleanup_e}")
        return create_response(False, None, f"创建长期记忆失败: {str(e)}")
    
    new_memory = {
        "id": memory_id,
        "content": content,
        "chroma_doc_id": chroma_doc_id,
        "score": body.score if body.score is not None else 5,
        "created_at": datetime.datetime.now().isoformat(),
        "is_orphan": False,
    }
    return create_response(True, {"memory": new_memory}, "长期记忆创建成功")


@router.patch("/longterm/{chroma_doc_id}/metadata")
async def update_longterm_metadata(chroma_doc_id: str, body: LongTermMetadataPatch):
    """更新 Chroma 元数据（halflife_days、arousal）；合并写入，避免覆盖其他 metadata 字段。"""
    from memory.vector_store import get_vector_store

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return create_response(False, None, "无可更新字段")

    vs = get_vector_store()
    try:
        got = vs.collection.get(ids=[chroma_doc_id], include=["metadatas"])
        if not got.get("ids"):
            return create_response(False, None, "记录不存在")
        md = dict((got["metadatas"] or [None])[0] or {})
        if "halflife_days" in patch:
            md["halflife_days"] = int(patch["halflife_days"])
        if "arousal" in patch:
            md["arousal"] = float(patch["arousal"])
        vs.collection.update(ids=[chroma_doc_id], metadatas=[md])
        return create_response(True, {"chroma_doc_id": chroma_doc_id}, "更新成功")
    except Exception as e:
        logger.error(f"更新长期记忆元数据失败 doc_id={chroma_doc_id}: {e}")
        return create_response(False, None, f"更新失败: {str(e)}")


@router.delete("/longterm/{chroma_doc_id}")
async def delete_longterm_memory(chroma_doc_id: str):
    """
    按 Chroma doc_id 删除；仅允许 manual_ 前缀（Mini App 手动新增）。
    先删 ChromaDB，再尝试删除 longterm_memories 镜像行。
    """
    if not chroma_doc_id.startswith("manual_"):
        return create_response(False, None, "日终归档记忆不允许删除")

    from memory.database import get_database
    from memory.vector_store import get_vector_store

    vs = get_vector_store()
    try:
        chroma_ok = vs.delete_memory(chroma_doc_id)
        if not chroma_ok:
            return create_response(False, None, "删除失败")
    except Exception as e:
        logger.error(f"ChromaDB 删除长期记忆失败 doc_id={chroma_doc_id}: {e}")
        return create_response(False, None, f"删除失败: {str(e)}")

    db = get_database()
    try:
        await db.delete_longterm_memory_by_chroma_id(chroma_doc_id)
    except Exception as e:
        logger.warning(
            "Chroma 已删除但镜像表删除失败 chroma_doc_id=%s: %s",
            chroma_doc_id,
            e,
        )

    return create_response(True, {"chroma_doc_id": chroma_doc_id}, "长期记忆删除成功")


# ==========================================
# 时效状态 temporal_states（管理端）
# ==========================================


@router.get("/temporal-states")
async def list_temporal_states():
    """列出全部 temporal_states（含已停用），按 created_at 倒序。"""
    from memory.database import list_temporal_states_all

    try:
        rows = await list_temporal_states_all()
        return create_response(True, rows, "获取时效状态成功")
    except Exception as e:
        logger.error(f"获取时效状态失败: {e}")
        return create_response(False, None, f"获取时效状态失败: {str(e)}")


@router.post("/temporal-states")
async def create_temporal_state(body: TemporalStateCreate):
    """新增一条 temporal_states（is_active=1）。"""
    from memory.database import insert_temporal_state

    content = (body.state_content or "").strip()
    if not content:
        return create_response(False, None, "state_content 不能为空")
    try:
        eid = await insert_temporal_state(
            state_content=content,
            action_rule=(body.action_rule or "").strip() or None,
            expire_at=(body.expire_at or "").strip() or None,
        )
        return create_response(True, {"id": eid}, "创建时效状态成功")
    except Exception as e:
        logger.error(f"创建时效状态失败: {e}")
        return create_response(False, None, f"创建时效状态失败: {str(e)}")


@router.delete("/temporal-states/{state_id}")
async def soft_delete_temporal_state(state_id: str):
    """手动软删除：将 is_active 置 0。"""
    from memory.database import deactivate_temporal_states_by_ids

    try:
        n = await deactivate_temporal_states_by_ids([state_id])
        if n:
            return create_response(True, {"id": state_id}, "已停用该时效状态")
        return create_response(False, None, "记录不存在或已停用")
    except Exception as e:
        logger.error(f"停用时效状态失败: {e}")
        return create_response(False, None, f"停用时效状态失败: {str(e)}")


# ==========================================
# 关系时间线 relationship_timeline（只读全表）
# ==========================================


@router.get("/relationship-timeline")
async def list_relationship_timeline_all():
    """全部关系时间线，按 created_at 倒序。"""
    from memory.database import list_relationship_timeline_all_desc

    try:
        rows = await list_relationship_timeline_all_desc()
        return create_response(True, rows, "获取关系时间线成功")
    except Exception as e:
        logger.error(f"获取关系时间线失败: {e}")
        return create_response(False, None, f"获取关系时间线失败: {str(e)}")
