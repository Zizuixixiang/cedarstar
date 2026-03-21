"""
记忆管理 API 模块。
提供记忆卡片管理接口和长期记忆库接口。
长期记忆：创建时先写 ChromaDB（成功后再写 SQLite）；删除时先删 SQLite 再删 ChromaDB。
列表接口对 chroma_doc_id 为空的记录附带 is_orphan 标记。
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
            cards = db.get_memory_cards(user_id, character_id, dimension, limit)
        else:
            # 获取所有激活的卡片
            cards = db.get_all_active_memory_cards(limit=limit)
        
        return create_response(True, cards, "获取记忆卡片成功")
    except Exception as e:
        logger.error(f"获取记忆卡片失败: {e}")
        return create_response(False, None, f"获取记忆卡片失败: {str(e)}")


@router.post("/cards")
async def create_memory_card(card_data: MemoryCardCreate):
    """创建记忆卡片。"""
    from memory.database import save_memory_card
    
    try:
        card_id = save_memory_card(
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
        updated = update_memory_card(card_id, body.content, body.dimension)
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
        deactivated = deactivate_memory_card(card_id)
        if deactivated:
            return create_response(True, {"card_id": card_id}, "停用记忆卡片成功")
        else:
            return create_response(False, None, "卡片不存在")
    except Exception as e:
        logger.error(f"停用记忆卡片失败: {e}")
        return create_response(False, None, f"停用记忆卡片失败: {str(e)}")


# ==========================================
# 长期记忆接口（ChromaDB 与 SQLite 镜像表，创建/删除顺序见各端点文档字符串）
# ==========================================

@router.get("/longterm")
async def get_longterm_memories(
    keyword: str = "",
    page: int = 1,
    page_size: int = 20
):
    """获取长期记忆列表（带搜索和分页，从真实数据库读取）。"""
    from memory.database import get_database
    
    try:
        db = get_database()
        result = db.get_longterm_memories(keyword=keyword, page=page, page_size=page_size)
        result = _annotate_longterm_query_result(result)
        return create_response(True, result, "获取长期记忆成功")
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
                "base_score": 5.0,
            }
        )
    except Exception as e:
        logger.error(f"写入 ChromaDB 失败，已跳过 SQLite: {e}")
        return create_response(False, None, f"创建长期记忆失败：ChromaDB 异常: {str(e)}")
    
    if not chroma_ok:
        logger.error("写入 ChromaDB 返回失败，已跳过 SQLite")
        return create_response(False, None, "创建长期记忆失败：ChromaDB 写入未成功")
    
    try:
        memory_id = db.create_longterm_memory(
            content=content, chroma_doc_id=chroma_doc_id, score=5
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
        "score": 5,
        "created_at": datetime.datetime.now().isoformat(),
        "is_orphan": False,
    }
    return create_response(True, {"memory": new_memory}, "长期记忆创建成功")


@router.delete("/longterm/{memory_id}")
async def delete_longterm_memory(memory_id: int):
    """
    删除长期记忆。
    先删 SQLite，再删 ChromaDB。SQLite 失败返回删除失败；Chroma 失败仅记日志，接口仍返回成功。
    """
    from memory.database import get_database
    from memory.vector_store import get_vector_store
    
    db = get_database()
    
    record = db.get_longterm_memory(memory_id)
    if not record:
        return create_response(False, None, "记忆不存在")
    
    chroma_doc_id = record.get("chroma_doc_id")
    
    sqlite_deleted = False
    try:
        sqlite_deleted = db.delete_longterm_memory(memory_id)
    except Exception as e:
        logger.error(f"从 SQLite 删除长期记忆失败 memory_id={memory_id}: {e}")
    
    if not sqlite_deleted:
        return create_response(False, None, "删除失败")
    
    if chroma_doc_id and not _is_chroma_doc_id_missing(chroma_doc_id):
        try:
            store = get_vector_store()
            chroma_ok = store.delete_memory(chroma_doc_id)
            if not chroma_ok:
                logger.warning(
                    f"SQLite 已删除但 ChromaDB 删除未成功 memory_id={memory_id} "
                    f"doc_id={chroma_doc_id}"
                )
        except Exception as e:
            logger.warning(
                f"SQLite 已删除但 ChromaDB 删除异常 memory_id={memory_id} "
                f"doc_id={chroma_doc_id}: {e}"
            )
    
    return create_response(True, {"memory_id": memory_id}, "长期记忆删除成功")
