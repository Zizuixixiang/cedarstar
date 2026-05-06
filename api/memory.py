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
import hashlib
import json
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


class TemporalStateUpdate(BaseModel):
    state_content: Optional[str] = None
    action_rule: Optional[str] = None
    expire_at: Optional[str] = None


class SummaryTextPatch(BaseModel):
    summary_text: str


class SummaryStarPatch(BaseModel):
    is_starred: bool


class ApprovalRejectRequest(BaseModel):
    note: str = ""


class ApprovalRequest(BaseModel):
    tool_name: str
    arguments: Dict[str, Any]


def create_response(success: bool, data: Any = None, message: str = "") -> Dict:
    return {"success": success, "data": data, "message": message}


APPROVAL_PERSONA_FIELD_WHITELIST = {
    "char_identity",
    "char_personality",
    "char_speech_style",
    "char_redlines",
    "char_appearance",
    "char_relationships",
    "char_nsfw",
}

APPROVAL_ALLOWED_TOOL_NAMES = {
    "update_memory_card",
    "update_temporal_state",
    "update_relationship_timeline_entry",
    "update_persona_field",
    "update_summary",
    "create_relationship_timeline_entry",
    "create_temporal_state",
}


def _rowcount_from_status(status: str) -> int:
    try:
        return int(str(status).split()[-1])
    except (IndexError, ValueError):
        return 0


def _ensure_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _approval_arg_content(approval: Dict[str, Any]) -> str:
    args = _ensure_dict(approval.get("arguments"))
    after = _ensure_dict(approval.get("after_preview"))
    value = args.get("content")
    if value is None:
        value = after.get("content")
    if value is None:
        value = after.get("state_content")
    if value is None:
        raise ValueError("approval content is missing")
    return str(value)


def _short_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _approval_change_summary(approval: Dict[str, Any]) -> str:
    args = _ensure_dict(approval.get("arguments"))
    tool_name = str(approval.get("tool_name") or "")
    content = _short_text(args.get("content", ""))
    if tool_name == "update_persona_field":
        return f"{args.get('field_name')}: {content}"
    if tool_name == "update_memory_card":
        return f"{args.get('dimension')}: {content}"
    if tool_name == "update_temporal_state":
        return f"{args.get('id')}: {content}"
    if tool_name == "update_relationship_timeline_entry":
        return f"{args.get('id')}: {content}"
    return content


def _approval_notification_text(approval: Dict[str, Any], status_text: str, note: str = "") -> str:
    lines = [
        f"\u5de5\u5177\u540d: {approval.get('tool_name')}",
        f"\u4fee\u6539\u5185\u5bb9\u6458\u8981: {_approval_change_summary(approval)}",
        f"\u72b6\u6001: {status_text}",
    ]
    if note:
        lines.append(f"\u62d2\u7edd\u7406\u7531: {note}")
    return "\n".join(lines)


_TOOL_ACTION_LABELS = {
    "update_memory_card": "更新记忆卡片",
    "update_temporal_state": "更新时效状态",
    "update_relationship_timeline_entry": "更新关系时间线",
    "update_persona_field": "更新人设字段",
    "update_summary": "更新摘要",
    "create_relationship_timeline_entry": "新增关系时间线条目",
    "create_temporal_state": "新增时效状态",
}


def _natural_target_part(approval: Dict[str, Any]) -> str:
    args = _ensure_dict(approval.get("arguments"))
    tool_name = str(approval.get("tool_name") or "")
    if tool_name == "update_memory_card":
        dim = str(args.get("dimension") or "").strip()
        return f"({dim})" if dim else ""
    if tool_name == "update_persona_field":
        field = str(args.get("field_name") or "").strip()
        return f"({field})" if field else ""
    if tool_name == "create_relationship_timeline_entry":
        ev = str(args.get("event_type") or "").strip()
        return f"({ev})" if ev else ""
    return ""


def _compose_approval_resolution_phrase(
    approval: Dict[str, Any],
    decision: str,
    note: str = "",
) -> str:
    """生成自然语言的审批结果短语，用于 Telegram 推送和写入 messages 表。"""
    tool_name = str(approval.get("tool_name") or "")
    action = _TOOL_ACTION_LABELS.get(tool_name, tool_name or "记忆更新")
    label = f"{action}{_natural_target_part(approval)}"
    args = _ensure_dict(approval.get("arguments"))
    content = _short_text(args.get("content"), 120)
    if decision == "approved":
        if content:
            return f"南杉同意了你「{label}」的申请，已生效。\n内容：{content}"
        return f"南杉同意了你「{label}」的申请，已生效。"
    if decision == "rejected":
        note_text = (note or "").strip()
        if note_text:
            return f"南杉拒绝了你「{label}」的申请。\n理由：{_short_text(note_text, 200)}"
        return f"南杉拒绝了你「{label}」的申请。"
    return f"你「{label}」的申请状态：{decision}"


async def _resolve_approval_target() -> Optional[Dict[str, str]]:
    """解析审批结果应该推送到的目标会话。

    优先用 ``.env`` 里的 ``TELEGRAM_MAIN_USER_CHAT_ID``；未配置时回退到 messages 表
    最近一条 telegram 用户消息推断出来的 session_id（CedarClio 单用户场景下足够稳定）。
    返回 ``{"session_id": ..., "chat_id": ..., "platform": "telegram"}`` 或 ``None``。
    """
    try:
        from config import Platform as _Platform, config as _cfg

        raw = _cfg.TELEGRAM_MAIN_USER_CHAT_ID
        chat_id = (raw or "").strip() if raw else ""
        if chat_id:
            return {
                "session_id": f"telegram_{chat_id}",
                "chat_id": chat_id,
                "platform": _Platform.TELEGRAM,
            }

        from memory.database import get_database

        db = get_database()
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_id
                FROM messages
                WHERE platform = 'telegram'
                  AND session_id LIKE 'telegram_%'
                  AND role = 'user'
                ORDER BY id DESC
                LIMIT 1
                """
            )
        if row and row["session_id"]:
            sid = str(row["session_id"])
            if sid.startswith("telegram_"):
                cid = sid[len("telegram_"):]
                if cid:
                    return {
                        "session_id": sid,
                        "chat_id": cid,
                        "platform": _Platform.TELEGRAM,
                    }
    except Exception as e:
        logger.warning("resolve approval target failed: %s", e)
    return None


async def _send_approval_resolution_to_chat(text: str, target: Dict[str, str]) -> None:
    """把审批结果发到目标 telegram chat（直接传 chat_id，不依赖主用户环境变量）。"""
    try:
        from bot.telegram_notify import send_telegram_text_to_chat

        await send_telegram_text_to_chat(target.get("chat_id"), text)
    except Exception as e:
        logger.warning("send approval resolution to chat failed: %s", e)


async def _persist_approval_system_message(text: str, target: Dict[str, str]) -> None:
    """把审批结果作为系统通知写入 messages 表，让 AI 在下一轮上下文里读到。"""
    try:
        from memory.database import save_message

        session_id = target.get("session_id") or ""
        platform = target.get("platform") or "telegram"
        if not session_id:
            logger.debug("approval system message skipped: empty session_id")
            return
        await save_message(
            role="user",
            content=f"[系统通知] {text}",
            session_id=session_id,
            user_id="system",
            platform=platform,
        )
    except Exception as e:
        logger.warning("persist approval system message failed: %s", e)




def _jsonable_approval_value(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _record_to_plain_dict(record: Any) -> Dict[str, Any]:
    if not record:
        return {}
    return {key: _jsonable_approval_value(value) for key, value in dict(record).items()}


def _approval_request_hash(tool_name: str, arguments: Dict[str, Any]) -> str:
    payload = str(tool_name) + json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _build_pending_approval_preview(conn, tool_name: str, arguments: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    args = arguments if isinstance(arguments, dict) else {}
    content = args.get("content")
    if content is None:
        raise ValueError("content is required")
    content = str(content)

    if tool_name == "update_memory_card":
        persona_id = str(args.get("persona_id") or "").strip()
        dimension = str(args.get("dimension") or "").strip()
        if not persona_id or not dimension:
            raise ValueError("persona_id and dimension are required")
        row = await conn.fetchrow(
            """
            SELECT id, user_id, character_id, dimension, content,
                   updated_at, source_message_id, is_active
            FROM memory_cards
            WHERE character_id = $1 AND dimension = $2 AND is_active = 1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            persona_id,
            dimension,
        )
        if not row:
            raise ValueError("memory_card not found")
        before = _record_to_plain_dict(row)
        return before, {**before, "content": content}

    if tool_name == "update_temporal_state":
        state_id = str(args.get("id") or "").strip()
        if not state_id:
            raise ValueError("id is required")
        row = await conn.fetchrow(
            """
            SELECT id, state_content, action_rule, expire_at, is_active, created_at
            FROM temporal_states
            WHERE id = $1
            """,
            state_id,
        )
        if not row:
            raise ValueError("temporal_state not found")
        before = _record_to_plain_dict(row)
        return before, {**before, "state_content": content}

    if tool_name == "update_relationship_timeline_entry":
        entry_id = str(args.get("id") or "").strip()
        if not entry_id:
            raise ValueError("id is required")
        row = await conn.fetchrow(
            """
            SELECT id, created_at, event_type, content, source_summary_id
            FROM relationship_timeline
            WHERE id = $1
            """,
            entry_id,
        )
        if not row:
            raise ValueError("relationship_timeline entry not found")
        before = _record_to_plain_dict(row)
        return before, {**before, "content": content}

    if tool_name == "update_persona_field":
        field = str(args.get("field_name") or "").strip()
        if field not in APPROVAL_PERSONA_FIELD_WHITELIST:
            raise ValueError("field_name not allowed")
        persona_id = args.get("persona_id")
        if persona_id is None or str(persona_id).strip() == "":
            raise ValueError("persona_id is required")
        row = await conn.fetchrow("SELECT * FROM persona_configs WHERE id = $1", int(persona_id))
        if not row:
            raise ValueError("persona not found")
        before = _record_to_plain_dict(row)
        return before, {**before, field: content}

    if tool_name == "update_summary":
        summary_id = args.get("id")
        if summary_id is None:
            raise ValueError("id is required")
        row = await conn.fetchrow(
            "SELECT id, session_id, summary_text, summary_type, source_date FROM summaries WHERE id = $1",
            int(summary_id),
        )
        if not row:
            raise ValueError("summary not found")
        before = _record_to_plain_dict(row)
        return before, {**before, "summary_text": content}

    if tool_name == "create_relationship_timeline_entry":
        event_type = str(args.get("event_type") or "").strip()
        if not event_type:
            raise ValueError("event_type is required")
        before = {}
        after = {"event_type": event_type, "content": content, "source_summary_id": args.get("source_summary_id")}
        return before, after

    if tool_name == "create_temporal_state":
        before = {}
        after = {"state_content": content, "action_rule": args.get("action_rule"), "expire_at": args.get("expire_at")}
        return before, after

    raise ValueError(f"unsupported approval tool: {tool_name}")


async def _create_pending_approval_from_request(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    from memory.database import (
        find_duplicate_pending,
        get_database,
        insert_mcp_audit_log,
        insert_pending_approval,
    )

    name = str(tool_name or "").strip()
    if name not in APPROVAL_ALLOWED_TOOL_NAMES:
        logger.error(
            "approval rejected: tool_name not allowed, tool_name=%s, allowed=%s",
            name,
            sorted(APPROVAL_ALLOWED_TOOL_NAMES),
        )
        raise ValueError("tool_name not allowed")
    args = arguments if isinstance(arguments, dict) else {}
    arg_hash = _approval_request_hash(name, args)
    duplicate = await find_duplicate_pending(name, arg_hash)
    if duplicate:
        return {
            "status": "pending",
            "approval_id": duplicate.get("id"),
            "expires_at": duplicate.get("expires_at"),
            "duplicate": True,
        }

    db = get_database()
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
    async with db.pool.acquire() as conn:
        before, after = await _build_pending_approval_preview(conn, name, args)
    approval_id = await insert_pending_approval(
        tool_name=name,
        arguments=args,
        arguments_hash=arg_hash,
        before_snapshot=before,
        after_preview=after,
        requested_by_token_hash="internal_ai_tool",
        expires_at=expires_at,
    )
    await insert_mcp_audit_log(
        token_scope="internal_ai_tool",
        tool_name=name,
        arguments=args,
        result_status="pending",
        approval_id=approval_id,
    )
    return {
        "status": "pending",
        "approval_id": approval_id,
        "expires_at": expires_at.isoformat(),
        "duplicate": False,
    }

async def _send_approval_notification(text: str) -> None:
    try:
        from bot.telegram_notify import send_telegram_main_user_text

        await send_telegram_main_user_text(text)
    except Exception as e:
        logger.warning("approval telegram notification failed: %s", e)


async def _apply_approved_update(conn, approval: Dict[str, Any]) -> Dict[str, Any]:
    tool_name = str(approval.get("tool_name") or "")
    args = _ensure_dict(approval.get("arguments"))
    before = _ensure_dict(approval.get("before_snapshot"))
    content = _approval_arg_content(approval)

    if tool_name == "update_memory_card":
        card_id = before.get("id")
        if card_id is None:
            raise ValueError("memory card id is missing")
        status = await conn.execute(
            "UPDATE memory_cards SET content = $1, updated_at = NOW() WHERE id = $2",
            content,
            int(card_id),
        )
        rows = _rowcount_from_status(status)
        if rows <= 0:
            raise ValueError("memory card not found")
        return {"rows": rows, "target": "memory_cards", "id": card_id}

    if tool_name == "update_temporal_state":
        state_id = args.get("id") or before.get("id")
        if not state_id:
            raise ValueError("temporal state id is missing")
        status = await conn.execute(
            "UPDATE temporal_states SET state_content = $1 WHERE id = $2",
            content,
            str(state_id),
        )
        rows = _rowcount_from_status(status)
        if rows <= 0:
            raise ValueError("temporal state not found")
        return {"rows": rows, "target": "temporal_states", "id": str(state_id)}

    if tool_name == "update_relationship_timeline_entry":
        entry_id = args.get("id") or before.get("id")
        if not entry_id:
            raise ValueError("relationship timeline id is missing")
        status = await conn.execute(
            "UPDATE relationship_timeline SET content = $1 WHERE id = $2",
            content,
            str(entry_id),
        )
        rows = _rowcount_from_status(status)
        if rows <= 0:
            raise ValueError("relationship timeline entry not found")
        return {"rows": rows, "target": "relationship_timeline", "id": str(entry_id)}

    if tool_name == "update_persona_field":
        field = str(args.get("field_name") or "").strip()
        if field not in APPROVAL_PERSONA_FIELD_WHITELIST:
            raise ValueError("field_name not allowed")
        persona_id = args.get("persona_id") or before.get("id")
        if persona_id is None:
            raise ValueError("persona id is missing")
        status = await conn.execute(
            f"UPDATE persona_configs SET {field} = $1, updated_at = NOW() WHERE id = $2",
            content,
            int(persona_id),
        )
        rows = _rowcount_from_status(status)
        if rows <= 0:
            raise ValueError("persona not found")
        return {"rows": rows, "target": "persona_configs", "id": int(persona_id), "field": field}

    if tool_name == "update_summary":
        summary_id = args.get("id") or before.get("id")
        if summary_id is None:
            raise ValueError("summary id is missing")
        from memory.database import update_summary_by_id
        ok = await update_summary_by_id(int(summary_id), content)
        if not ok:
            raise ValueError("summary not found")
        return {"rows": 1, "target": "summaries", "id": int(summary_id)}

    if tool_name == "create_relationship_timeline_entry":
        from memory.database import insert_relationship_timeline_event
        event_type = str(args.get("event_type") or "").strip()
        if not event_type:
            raise ValueError("event_type is required")
        entry_id = await insert_relationship_timeline_event(
            event_type=event_type,
            content=content,
            source_summary_id=str(args.get("source_summary_id") or "").strip() or None,
        )
        return {"rows": 1, "target": "relationship_timeline", "id": entry_id}

    if tool_name == "create_temporal_state":
        from memory.database import insert_temporal_state
        state_id = await insert_temporal_state(
            state_content=content,
            action_rule=str(args.get("action_rule") or "").strip() or None,
            expire_at=str(args.get("expire_at") or "").strip() or None,
        )
        return {"rows": 1, "target": "temporal_states", "id": state_id}

    raise ValueError(f"unsupported approval tool: {tool_name}")


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


@router.get("/context-trace")
async def get_context_trace():
    """最近一次 context 构建时实际注入的摘要和长期记忆清单。"""
    from memory.context_builder import get_last_context_trace

    try:
        return create_response(True, get_last_context_trace(), "获取本轮记忆标记成功")
    except Exception as e:
        logger.error(f"获取 context trace 失败: {e}")
        return create_response(False, None, f"获取失败: {str(e)}")


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
    days: Optional[int] = None,
    context_only: bool = False,
    starred_only: bool = False,
    page: int = 1,
    page_size: int = 20,
):
    """分页列出 summaries；可选按 summary_type、source_date 区间或最近 N 天过滤。"""
    from memory.database import get_summaries_filtered

    st = (summary_type or "").strip() or None
    if st is not None and st not in ("chunk", "daily"):
        return create_response(False, None, "summary_type 须为 chunk 或 daily")

    d_from = (source_date_from or "").strip() or None
    d_to = (source_date_to or "").strip() or None
    if days and days > 0 and not d_from and not d_to:
        from datetime import date as _date, timedelta
        d_from = (_date.today() - timedelta(days=days - 1)).isoformat()

    try:
        if context_only:
            from memory.context_builder import get_last_context_trace
            from memory.database import get_database

            trace = get_last_context_trace()
            ids = [
                int(x)
                for x in (
                    (trace.get("daily_summary_ids") or [])
                    + (trace.get("chunk_summary_ids") or [])
                    + (trace.get("archived_daily_summary_ids") or [])
                )
                if str(x).isdigit()
            ]
            if ids:
                db = get_database()
                cond = "s.id = ANY($1::int[])"
                params: List[Any] = [ids]
                if st:
                    params.append(st)
                    cond += f" AND s.summary_type = ${len(params)}"
                async with db.pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"""
                        SELECT
                            s.id, s.session_id, s.summary_text, s.start_message_id,
                            s.end_message_id, s.created_at, s.summary_type, s.source_date,
                            s.archived_by, s.is_starred,
                            EXISTS (
                                SELECT 1
                                FROM summaries AS d
                                WHERE d.summary_type = 'daily'
                                  AND d.source_date IS NOT NULL
                                  AND d.source_date::date = COALESCE(s.source_date::date, s.created_at::date)
                                  AND (d.session_id = s.session_id OR d.session_id = 'daily_batch')
                            ) AS has_daily_summary
                        FROM summaries AS s
                        WHERE {cond}
                        ORDER BY array_position($1::int[], s.id)
                        """,
                        *params,
                    )
                items = [
                    {
                        "id": r["id"],
                        "session_id": r["session_id"],
                        "summary_text": r["summary_text"],
                        "start_message_id": r["start_message_id"],
                        "end_message_id": r["end_message_id"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        "summary_type": r["summary_type"],
                        "source_date": r["source_date"].isoformat() if r["source_date"] else None,
                        "archived_by": r["archived_by"],
                        "is_starred": bool(r["is_starred"]),
                        "has_daily_summary": bool(r["has_daily_summary"]),
                    }
                    for r in rows
                ]
                total = len(items)
                page = 1
            else:
                items, total, page = [], 0, 1
        else:
            items, total = await get_summaries_filtered(
                page=page,
                page_size=page_size,
                summary_type=st,
                source_date_from=d_from,
                source_date_to=d_to,
                starred_only=starred_only,
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
    context_only: bool = False,
    query: Optional[str] = None,
    top_k: int = 5,
):
    """List long-term memories; run vector search when query is provided."""
    from memory.vector_store import get_vector_store, search_memory

    try:
        vs = get_vector_store()
        st = (summary_type or "").strip() or None
        where = {"summary_type": st} if st else None
        q = (query or "").strip()
        if q:
            k = max(1, min(int(top_k or page_size or 5), 20))
            rows = search_memory(q, top_k=k, where=where)
            items = []
            for r in rows:
                meta = dict(r.get("metadata") or {})
                items.append(
                    {
                        "chroma_doc_id": r.get("id"),
                        "content": r.get("text") or "",
                        "score": r.get("score"),
                        "summary_type": meta.get("summary_type"),
                        "date": meta.get("date"),
                        "source": meta.get("source"),
                        "is_starred": bool(meta.get("is_starred")),
                        "source_chunk_ids": meta.get("source_chunk_ids"),
                    }
                )
            return create_response(
                True,
                {"items": items, "total": len(items), "query": q, "top_k": k},
                "memory search completed",
            )

        trace_ids: List[str] = []
        if context_only:
            from memory.context_builder import get_last_context_trace

            trace = get_last_context_trace()
            raw_trace_ids = [str(x) for x in (trace.get("longterm_doc_ids") or []) if str(x).strip()]
            if st and raw_trace_ids:
                trace_result = vs.collection.get(ids=raw_trace_ids, include=["metadatas"])
                trace_result_ids = trace_result.get("ids") or []
                trace_metas = trace_result.get("metadatas") or []
                meta_by_id = {
                    doc_id: dict(trace_metas[i] or {}) if i < len(trace_metas) else {}
                    for i, doc_id in enumerate(trace_result_ids)
                }
                trace_ids = [
                    doc_id
                    for doc_id in raw_trace_ids
                    if meta_by_id.get(doc_id, {}).get("summary_type") == st
                ]
            else:
                trace_ids = raw_trace_ids
            total = len(trace_ids)
        elif where:
            filt = vs.collection.get(where=where, include=["metadatas"])
            total = len(filt.get("ids") or [])
        else:
            total = vs.collection.count()

        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size

        items: List[Dict[str, Any]] = []
        if context_only:
            page_ids = trace_ids[offset : offset + page_size]
            if page_ids:
                result = vs.collection.get(
                    ids=page_ids,
                    include=["documents", "metadatas"],
                )
                ids = result.get("ids") or []
                docs = result.get("documents") or []
                metas = result.get("metadatas") or []
                ordered = []
                by_id = {
                    doc_id: (
                        docs[i] if i < len(docs) else "",
                        dict(metas[i] or {}) if i < len(metas) else {},
                    )
                    for i, doc_id in enumerate(ids)
                }
                for doc_id in page_ids:
                    if doc_id in by_id:
                        ordered.append((doc_id, *by_id[doc_id]))
                for doc_id, doc, meta in ordered:
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
        elif total > 0 and offset < total:
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
async def list_temporal_states(days: Optional[int] = None):
    """列出全部 temporal_states（含已停用），按 created_at 倒序。可选 days 过滤最近 N 天。"""
    from memory.database import list_temporal_states_all

    try:
        rows = await list_temporal_states_all(days=days)
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


@router.patch("/temporal-states/{state_id}")
async def update_temporal_state(state_id: str, body: TemporalStateUpdate):
    """更新一条 temporal_states 的 state_content / action_rule / expire_at。"""
    from memory.database import update_temporal_state as db_update

    has_any = (
        body.state_content is not None
        or body.action_rule is not None
        or body.expire_at is not None
    )
    if not has_any:
        return create_response(False, None, "至少提供一个可更新字段")
    try:
        n = await db_update(
            state_id,
            state_content=body.state_content,
            action_rule=body.action_rule,
            expire_at=body.expire_at,
        )
        if n:
            return create_response(True, {"id": state_id}, "更新成功")
        return create_response(False, None, "记录不存在")
    except Exception as e:
        logger.error(f"更新时效状态失败: {e}")
        return create_response(False, None, f"更新时效状态失败: {str(e)}")


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
async def list_relationship_timeline_all(days: Optional[int] = None):
    """全部关系时间线，按 created_at 倒序。可选 days 过滤最近 N 天。"""
    from memory.database import list_relationship_timeline_all_desc

    try:
        rows = await list_relationship_timeline_all_desc(days=days)
        return create_response(True, rows, "获取关系时间线成功")
    except Exception as e:
        logger.error(f"获取关系时间线失败: {e}")
        return create_response(False, None, f"获取关系时间线失败: {str(e)}")

# ==========================================
# Pending approvals
# ==========================================


@router.post("/approvals/request")
async def request_approval(body: ApprovalRequest):
    """Create a pending memory update approval from the internal AI tool loop."""
    try:
        args = body.arguments if isinstance(body.arguments, dict) else {}
        logger.info(
            "approval request received tool_name=%s arg_keys=%s arguments=%s",
            body.tool_name,
            sorted(args.keys()),
            args,
        )
        data = await _create_pending_approval_from_request(body.tool_name, body.arguments or {})
        if not data.get("duplicate"):
            await _send_approval_notification(
                "\u5de5\u5177\u540d: "
                + str(body.tool_name)
                + "\n\u72b6\u6001: \u5f85\u5ba1\u6279\napproval_id: "
                + str(data.get("approval_id"))
            )
        return create_response(
            True,
            {
                "status": data.get("status", "pending"),
                "approval_id": data.get("approval_id"),
                "expires_at": data.get("expires_at"),
            },
            "approval requested",
        )
    except Exception as e:
        logger.error("request approval failed: %s", e)
        return create_response(False, None, f"request approval failed: {str(e)}")



@router.get("/approvals")
async def list_approvals(status: Optional[str] = None, limit: Optional[int] = None):
    """List approval records, optionally filtered by status and capped by limit.

    省略 ``limit`` 时返回全部（保持 Mini App 一次拉满的行为）；传 ``limit`` 时按
    ``created_at DESC`` 截断，最大 100。
    """
    from memory.database import expire_stale_approvals, list_pending_approvals

    capped: Optional[int] = None
    if limit is not None:
        try:
            capped = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            capped = None
    try:
        await expire_stale_approvals()
        rows = await list_pending_approvals(status=status, limit=capped)
        return create_response(True, rows, "approvals loaded")
    except Exception as e:
        logger.error("list approvals failed: %s", e)
        return create_response(False, None, f"list approvals failed: {str(e)}")


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str):
    """Return a single approval record by id."""
    from memory.database import expire_stale_approvals, get_pending_approval

    try:
        await expire_stale_approvals()
        row = await get_pending_approval(approval_id)
        if not row:
            return create_response(False, None, "approval not found")
        return create_response(True, row, "approval loaded")
    except Exception as e:
        logger.error("get approval failed approval_id=%s: %s", approval_id, e)
        return create_response(False, None, f"get approval failed: {str(e)}")


@router.post("/approvals/{approval_id}/approve")
async def approve_approval(approval_id: str):
    """Approve a pending MCP memory update and apply it in one transaction."""
    from memory.database import (
        get_database,
        get_pending_approval,
        insert_mcp_audit_log,
        resolve_approval,
    )

    db = get_database()
    approval: Optional[Dict[str, Any]] = None
    applied: Optional[Dict[str, Any]] = None
    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                approval = await get_pending_approval(approval_id, conn=conn)
                if not approval:
                    return create_response(False, None, "approval not found")
                if approval.get("status") != "pending":
                    return create_response(False, approval, "approval is not pending")
                applied = await _apply_approved_update(conn, approval)
                await resolve_approval(approval_id, "approved", conn=conn)
                await insert_mcp_audit_log(
                    token_scope="approval_api",
                    tool_name=str(approval.get("tool_name") or ""),
                    arguments=_ensure_dict(approval.get("arguments")),
                    result_status="success",
                    approval_id=approval_id,
                    conn=conn,
                )
    except Exception as e:
        logger.error("approve approval failed approval_id=%s: %s", approval_id, e)
        return create_response(False, None, f"approve failed: {str(e)}")

    phrase = _compose_approval_resolution_phrase(approval or {}, "approved")
    target = await _resolve_approval_target()
    if target:
        await _send_approval_resolution_to_chat(phrase, target)
        await _persist_approval_system_message(phrase, target)
    else:
        logger.warning("approval approved but no target chat resolved, phrase=%s", phrase)
    return create_response(
        True,
        {"approval_id": approval_id, "status": "approved", "applied": applied},
        "approval approved",
    )


@router.post("/approvals/{approval_id}/reject")
async def reject_approval(approval_id: str, body: ApprovalRejectRequest):
    """Reject a pending MCP memory update."""
    from memory.database import (
        get_database,
        get_pending_approval,
        insert_mcp_audit_log,
        resolve_approval,
    )

    note = str(body.note or "").strip()
    db = get_database()
    approval: Optional[Dict[str, Any]] = None
    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                approval = await get_pending_approval(approval_id, conn=conn)
                if not approval:
                    return create_response(False, None, "approval not found")
                if approval.get("status") != "pending":
                    return create_response(False, approval, "approval is not pending")
                await resolve_approval(approval_id, "rejected", note=note, conn=conn)
                await insert_mcp_audit_log(
                    token_scope="approval_api",
                    tool_name=str(approval.get("tool_name") or ""),
                    arguments=_ensure_dict(approval.get("arguments")),
                    result_status="rejected",
                    error_message=note or None,
                    approval_id=approval_id,
                    conn=conn,
                )
    except Exception as e:
        logger.error("reject approval failed approval_id=%s: %s", approval_id, e)
        return create_response(False, None, f"reject failed: {str(e)}")

    phrase = _compose_approval_resolution_phrase(approval or {}, "rejected", note)
    target = await _resolve_approval_target()
    if target:
        await _send_approval_resolution_to_chat(phrase, target)
        await _persist_approval_system_message(phrase, target)
    else:
        logger.warning("approval rejected but no target chat resolved, phrase=%s", phrase)
    return create_response(
        True,
        {"approval_id": approval_id, "status": "rejected"},
        "approval rejected",
    )

