"""
MCP Memory Server — SSE transport at /mcp/memory/{token}/sse。

工具清单（7 读 + 1 写）：
  search_memories / get_recent_summaries / get_memory_cards
  / get_temporal_states / get_relationship_timeline / get_persona / get_context_trace
  / add_external_chunk

鉴权：URL 内嵌 token（MCP_WEB_READ_TOKEN / MCP_WEB_WRITE_TOKEN）。
  - /mcp/memory/{token}/sse      → SSE 连接
  - /mcp/memory/{token}/messages/ → POST 消息（token 由 MCP server 通过 SSE 自动下发）
审计：所有 call_tool（含鉴权失败）写 mcp_audit_log。
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------

_MCP_WEB_READ_TOKEN = (os.environ.get("MCP_WEB_READ_TOKEN") or "").strip()
_MCP_WEB_WRITE_TOKEN = (os.environ.get("MCP_WEB_WRITE_TOKEN") or "").strip()
_MCP_API_READ_TOKEN = (os.environ.get("MCP_API_READ_TOKEN") or "").strip()
_MCP_API_ADMIN_TOKEN = (os.environ.get("MCP_API_TOKEN") or "").strip()

# ---------------------------------------------------------------------------
# 鉴权 ASGI 中间件（URL 内嵌 token）
# ---------------------------------------------------------------------------

# 匹配完整路径：/mcp/memory/{token}/sse 或 /mcp/memory/{token}/messages/
_TOKEN_PATH_RE = re.compile(r"^/mcp/memory/([^/]{8,256})(/sse|/messages/)$")


class MCPAuthMiddleware:
    """
    URL 内嵌 token 鉴权。
    路径格式：
      - /mcp/memory/{token}/sse      → SSE 连接
      - /mcp/memory/{token}/messages/ → POST 消息
    匹配 read token → read scope；匹配 write token → write scope。
    不匹配 → 404（非 401，避免探测）。

    root_path 设为 /mcp/memory/{token}，使 MCP server 生成的 messages URL
    自动包含 token（/mcp/memory/{token}/messages/?session_id=xxx）。
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # URL 内嵌 token
        m = _TOKEN_PATH_RE.match(path)
        if m:
            token = m.group(1)
            internal_path = m.group(2)  # "/sse" 或 "/messages/"
            resolved = _resolve_scope(token)
            if resolved is None:
                await _audit("__auth__", "__auth__", {"reason": "invalid_url_token"}, "error", "invalid url token")
                return await Response(status_code=404)(scope, receive, send)
            scope["mcp_scope"] = resolved
            scope["mcp_token_hash"] = _hash_token(token)
            # 保留原始路径供 uvicorn access log 使用（含 token，由日志过滤器脱敏）
            scope["mcp_original_path"] = path
            # 重写路径 + root_path，使 MCP server 生成的 messages URL 包含 token
            token_root = f"/mcp/memory/{token}"
            scope["path"] = internal_path
            scope["raw_path"] = internal_path.encode()
            scope["root_path"] = token_root
            return await self.app(scope, receive, send)

        # 路径不匹配 → 404（非 401，避免探测）
        return await Response(status_code=404)(scope, receive, send)


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _resolve_scope(token: str) -> Optional[str]:
    if _MCP_WEB_READ_TOKEN and token == _MCP_WEB_READ_TOKEN:
        return "web_read"
    if _MCP_WEB_WRITE_TOKEN and token == _MCP_WEB_WRITE_TOKEN:
        return "web_write"
    if _MCP_API_READ_TOKEN and token == _MCP_API_READ_TOKEN:
        return "api_read"
    if _MCP_API_ADMIN_TOKEN and token == _MCP_API_ADMIN_TOKEN:
        return "api_admin"
    return None


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

async def _audit(
    scope: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    result_status: str = "success",
    error_message: Optional[str] = None,
    approval_id: Optional[str] = None,
) -> None:
    try:
        from memory.database import insert_mcp_audit_log
        await insert_mcp_audit_log(
            token_scope=scope,
            tool_name=tool_name,
            arguments=arguments,
            result_status=result_status,
            error_message=error_message,
            approval_id=approval_id,
        )
    except Exception as e:
        logger.error("审计日志写入失败: %s", e)


# ---------------------------------------------------------------------------
# FastMCP 实例 + 工具注册
# ---------------------------------------------------------------------------

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "memory",
    instructions=(
        "CedarClio 记忆系统 MCP Server。"
        "读工具可用于查询长期记忆、摘要、记忆卡片、时效状态、关系时间线和人设。"
        "add_external_chunk 仅在用户明确要求「整理这个窗口」「写进记忆库」「存进去」等时调用。"
        "api_admin scope 下的 update_memory_card / update_temporal_state / update_relationship_timeline_entry / update_persona_field "
        "都不会直接修改数据，只会创建 pending approval。"
        "调用 update_* 前，先用读工具确认精确目标：id / persona_id / dimension / field_name。"
        "update_temporal_state 只改 content，不要传 expires_at。"
        "update_persona_field 的 field_name 只能是 char_identity / char_personality / char_speech_style / char_redlines / char_appearance / char_relationships / char_nsfw。"
        "看到 pending 返回后，应告诉用户 approval_id，并说明需要在 Mini App 待审批页或 Telegram 通知中审批后才会生效。"
    ),
)
# ---------------------------------------------------------------------------
# Scope and approval helpers
# ---------------------------------------------------------------------------

READ_TOOL_SCOPES = ["web_read", "web_write", "api_read", "api_admin"]
WEB_WRITE_SCOPES = ["web_write"]
API_ADMIN_SCOPES = ["api_admin"]
UPDATE_PERSONA_FIELD_WHITELIST = {
    "char_identity",
    "char_personality",
    "char_speech_style",
    "char_redlines",
    "char_appearance",
    "char_relationships",
    "char_nsfw",
}
PENDING_APPROVAL_MESSAGE = (
    "\u7b49\u5f85\u5ba1\u6279\uff0c\u5ba1\u6279\u7ed3\u679c\u5c06\u901a\u8fc7 Telegram \u901a\u77e5"
)


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _record_to_dict(record: Any) -> Dict[str, Any]:
    if not record:
        return {}
    return {key: _jsonable_value(value) for key, value in dict(record).items()}


def _current_request_scope() -> Dict[str, Any]:
    try:
        request = mcp.get_context().request_context.request
        return getattr(request, "scope", {}) or {}
    except Exception:
        return {}


def _current_mcp_scope() -> Optional[str]:
    return _current_request_scope().get("mcp_scope")


def _current_token_hash() -> Optional[str]:
    return _current_request_scope().get("mcp_token_hash")


def _required_scopes_for_tool_info(tool_info: Any) -> List[str]:
    fn = getattr(tool_info, "fn", None)
    required = getattr(fn, "_required_scopes", None)
    if required is None:
        return API_ADMIN_SCOPES
    return list(required)


def _tool_allowed_for_scope(tool_info: Any, scope: Optional[str]) -> bool:
    return bool(scope and scope in _required_scopes_for_tool_info(tool_info))


def _arguments_hash(tool_name: str, arguments: Dict[str, Any]) -> str:
    payload = tool_name + json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _queue_pending_update(
    tool_name: str,
    arguments: Dict[str, Any],
    before_snapshot: Dict[str, Any],
    after_preview: Dict[str, Any],
) -> str:
    from memory.database import (
        find_duplicate_pending,
        insert_mcp_audit_log,
        insert_pending_approval,
    )

    arg_hash = _arguments_hash(tool_name, arguments)
    duplicate = await find_duplicate_pending(tool_name, arg_hash)
    if duplicate:
        return _json({
            "status": "pending",
            "approval_id": duplicate.get("id"),
            "expires_at": duplicate.get("expires_at"),
            "message": PENDING_APPROVAL_MESSAGE,
        })

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    approval_id = await insert_pending_approval(
        tool_name=tool_name,
        arguments=arguments,
        arguments_hash=arg_hash,
        before_snapshot=before_snapshot,
        after_preview=after_preview,
        requested_by_token_hash=_current_token_hash(),
        expires_at=expires_at,
    )
    await insert_mcp_audit_log(
        token_scope=_current_mcp_scope() or "unknown",
        tool_name=tool_name,
        arguments=arguments,
        result_status="pending",
        approval_id=approval_id,
    )
    return _json({
        "status": "pending",
        "approval_id": approval_id,
        "expires_at": expires_at.isoformat(),
        "message": PENDING_APPROVAL_MESSAGE,
    })



@mcp.tool()
async def search_memories(
    query: str,
    top_k: int = 10,
    type_filter: Optional[List[str]] = None,
    source_filter: Optional[str] = None,
) -> str:
    """搜索长期记忆（向量 + BM25 双路召回，无时间衰减/MMR/星标加权）。
    type_filter: 可选值 daily_event / manual / state_archive，默认 ['daily_event', 'manual']。
    source_filter: 可选，按 Chroma metadata.source 过滤（如 claude_web）。
    """
    try:
        from memory.vector_store import search_memory
        from memory.bm25_retriever import search_bm25
        from memory.context_builder import _merge_vector_bm25_dedupe

        allowed_types = type_filter if type_filter else ["daily_event", "manual", "app_event"]

        where: Optional[Dict[str, Any]] = None
        if allowed_types:
            where = {"summary_type": {"$in": allowed_types}}
        if source_filter:
            src_cond = {"source": source_filter}
            where = {"$and": [where, src_cond]} if where else src_cond

        vector_results = search_memory(query, top_k=top_k, where=where)
        bm25_results = search_bm25(query, top_k=top_k, allowed_summary_types=allowed_types)
        merged = _merge_vector_bm25_dedupe(vector_results, bm25_results, max_total=top_k)

        results = []
        for r in merged:
            meta = r.get("metadata") or {}
            results.append({
                "id": r.get("id"),
                "text": r.get("text", "")[:500],
                "score": round(float(r.get("score", 0)), 3),
                "retrieval_method": r.get("retrieval_method", "unknown"),
                "summary_type": meta.get("summary_type"),
                "date": meta.get("date"),
                "source": meta.get("source"),
            })
        return json.dumps({"success": True, "results": results, "count": len(results)}, ensure_ascii=False)
    except Exception as e:
        logger.error("search_memories 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_recent_summaries(
    date: Optional[str] = None,
    days: Optional[int] = None,
    summary_type: Optional[str] = None,
    only_unarchived: bool = False,
    source_filter: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> str:
    """分页列出 summaries。date: 具体日期 YYYY-MM-DD；days: 最近 N 天。summary_type: chunk/daily/省略=全部。
    only_unarchived: 仅未归档。source_filter: internal/claude_web/省略=全部。
    """
    try:
        from memory.database import get_summaries_filtered

        source_date_from = None
        source_date_to = None
        if date:
            source_date_from = date
            source_date_to = date
        elif days is not None and days > 0:
            source_date_from = (datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=days - 1)).isoformat()

        items, total = await get_summaries_filtered(
            page=page,
            page_size=page_size,
            summary_type=summary_type,
            source_date_from=source_date_from,
            source_date_to=source_date_to,
            source_filter=source_filter,
            only_unarchived=only_unarchived,
        )
        return json.dumps({
            "success": True,
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_recent_summaries 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_memory_cards(
    user_id: Optional[str] = None,
    character_id: Optional[str] = None,
    dimension: Optional[str] = None,
    limit: int = 50,
) -> str:
    """获取记忆卡片列表。不传 user_id/character_id 时返回全部激活卡片。"""
    try:
        from memory.database import get_database
        db = get_database()
        if user_id and character_id:
            cards = await db.get_memory_cards(user_id, character_id, dimension, limit)
        else:
            cards = await db.get_all_active_memory_cards(limit=limit)
        return json.dumps({"success": True, "cards": cards}, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_memory_cards 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_temporal_states() -> str:
    """列出全部 temporal_states（含已停用），按 created_at 倒序。"""
    try:
        from memory.database import list_temporal_states_all
        rows = await list_temporal_states_all()
        return json.dumps({"success": True, "states": rows}, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_temporal_states 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_relationship_timeline() -> str:
    """全部关系时间线，按 created_at 倒序。"""
    try:
        from memory.database import list_relationship_timeline_all_desc
        rows = await list_relationship_timeline_all_desc()
        return json.dumps({"success": True, "timeline": rows}, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_relationship_timeline 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_persona(persona_id: int) -> str:
    """获取单个人设配置详情。"""
    try:
        from memory.database import get_database
        db = get_database()
        persona = await db.get_persona_config(persona_id)
        if not persona:
            return json.dumps({"success": False, "error": "人设配置不存在"}, ensure_ascii=False)
        return json.dumps({"success": True, "persona": persona}, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_persona 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_context_trace() -> str:
    """最近一次 context 构建时实际注入的摘要和长期记忆清单。"""
    try:
        from memory.context_builder import get_last_context_trace
        trace = get_last_context_trace()
        return json.dumps({"success": True, "trace": trace}, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_context_trace 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def add_external_chunk(content: str, as_of_date: Optional[str] = None) -> str:
    """从网页端 Claude 整理的对话摘要写入记忆库。

    仅在用户明确说出「整理这个窗口」「写进记忆库」「存进去」等显式指令时调用。
    不要主动判断对话是否值得整理，不要在对话中途调用。
    一次会话最多调用一次。
    content 应是当前完整对话的摘要总结，不是单条消息。

    as_of_date 用于补录历史窗口对话。补录后需在服务器执行 `python run_daily_batch.py YYYY-MM-DD` 手动重跑该日期的 daily 摘要，
    重跑时会把新 chunk 标记为已归档（archived_by 回填），避免被当晚自动跑批误吃。
    一天可补录多条，重跑时会拼接全部 chunk。

    流程：LLM 拆分事件 → summaries 写 chunk 留底 → longterm_memories 逐条写事件 → ChromaDB embedding → BM25。
    """
    _TZ_SH = timezone(timedelta(hours=8))

    try:
        from memory.database import get_database, save_summary
        from llm.llm_interface import LLMInterface
        from memory.vector_store import add_memory
        from memory.bm25_retriever import add_document_to_bm25, refresh_bm25_index

        # 0. 日期校验
        today = datetime.now(_TZ_SH).date()
        if as_of_date is not None and str(as_of_date).strip():
            raw_date = str(as_of_date).strip()
            try:
                resolved_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                return json.dumps({
                    "success": False,
                    "error": f"as_of_date 格式错误，须为 YYYY-MM-DD: {raw_date}",
                }, ensure_ascii=False)
            delta = (today - resolved_date).days
            if delta < 0:
                return json.dumps({
                    "success": False,
                    "error": f"as_of_date 不允许未来日期: {raw_date}",
                }, ensure_ascii=False)
        else:
            resolved_date = today

        # 1. 字数校验
        db = get_database()
        max_chars_str = await db.get_config("external_chunk_max_chars", "2000")
        max_chars = int(max_chars_str or "2000")
        content = (content or "").strip()
        if not content:
            return json.dumps({"success": False, "error": "content 不能为空"}, ensure_ascii=False)
        if len(content) > max_chars:
            return json.dumps({
                "success": False,
                "error": f"content 超过 {max_chars} 字限制（当前 {len(content)} 字）",
            }, ensure_ascii=False)

        # 2. LLM 拆分事件
        try:
            llm = await LLMInterface.create(config_type="analysis")
        except Exception:
            llm = await LLMInterface.create(config_type="summary")

        prompt = (
            "以下是网页端 Claude 与用户的一段对话摘要总结。"
            "请将其拆分为独立的事件/话题片段，每个事件给出 score（1-10，重要程度）和 arousal（0-1，情绪强度）。\n\n"
            f"【输入】\n{content}\n\n"
            "【输出 schema】\n"
            '[{"summary": "事件描述（50-200字，不得少于50字）", "score": 5, "arousal": 0.1}, ...]\n'
            "只输出 JSON 数组，不要解释、不要 Markdown。\n"
            "注意：每个事件的 summary 必须不少于 50 个字符，过短的片段应合并到相邻事件中。"
        )

        events_parsed = None
        last_raw = ""
        last_exc = None
        for attempt in range(1, 4):
            try:
                resp = llm.generate_with_context_and_tracking(
                    [{"role": "user", "content": prompt}],
                    timeout_override_seconds=120,
                )
                last_raw = (resp.content or "").strip()
            except Exception as e:
                last_exc = e
                logger.warning("add_external_chunk LLM 拆分第 %s/3 次失败: %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(2)
                continue

            # JSON 解析
            try:
                events_parsed = json.loads(last_raw)
            except json.JSONDecodeError:
                m = re.search(r"\[[\s\S]*\]", last_raw)
                if m:
                    try:
                        events_parsed = json.loads(m.group())
                    except json.JSONDecodeError:
                        pass
            if isinstance(events_parsed, list) and events_parsed:
                break
            logger.warning("add_external_chunk JSON 解析第 %s/3 次失败，原始输出: %s", attempt, last_raw[:200])
            events_parsed = None
            if attempt < 3:
                await asyncio.sleep(2)

        if not isinstance(events_parsed, list) or not events_parsed:
            return json.dumps({
                "success": False,
                "error": f"LLM 拆分 3 次重试全失败（{last_exc or 'JSON 解析失败'}），原始输出: {last_raw[:300]}",
            }, ensure_ascii=False)

        # 3. PG: summaries 写 chunk 留底
        chunk_id = await save_summary(
            session_id="mcp_external",
            summary_text=f"[APP端] {content}",
            start_message_id=0,
            end_message_id=0,
            summary_type="chunk",
            source_date=resolved_date,
            source="claude_web",
            external_events_generated=True,
        )

        # 4. 逐条事件写入
        written_events = []
        for idx, ev in enumerate(events_parsed):
            if not isinstance(ev, dict):
                continue
            frag = str(ev.get("summary") or "").strip()
            if not frag or len(frag) < 50:
                logger.warning("add_external_chunk 跳过过短事件 idx=%s len=%s", idx, len(frag))
                continue
            score = max(1, min(10, int(float(ev.get("score", 5)))))
            arousal = max(0.0, min(1.0, float(ev.get("arousal", 0.1))))
            doc_id = f"mcp_external_{chunk_id}_{idx}"
            metadata = {
                "date": resolved_date.isoformat(),
                "session_id": "mcp_external",
                "summary_type": "app_event",
                "source": "claude_web",
                "base_score": float(score),
                "halflife_days": max(1, score * 3),
                "arousal": arousal,
                "source_chunk_ids": json.dumps([chunk_id]),
            }

            # ChromaDB
            chroma_ok = add_memory(doc_id, frag, metadata)
            if not chroma_ok:
                logger.error("add_external_chunk ChromaDB 写入失败 doc_id=%s", doc_id)
                continue

            # PG longterm_memories 镜像
            try:
                await db.upsert_longterm_memory_by_chroma_id(
                    content=frag,
                    chroma_doc_id=doc_id,
                    score=score,
                    source_chunk_ids=[chunk_id],
                    source_date=resolved_date,
                )
            except Exception as e:
                logger.error("add_external_chunk longterm_memories 写入失败 doc_id=%s: %s", doc_id, e)

            # BM25
            try:
                if not add_document_to_bm25(doc_id, frag, dict(metadata)):
                    refresh_bm25_index()
            except Exception as e:
                logger.warning("add_external_chunk BM25 失败 doc_id=%s: %s", doc_id, e)

            written_events.append({"doc_id": doc_id, "summary": frag[:200], "score": score, "arousal": arousal})

        return json.dumps({
            "success": True,
            "chunk_id": chunk_id,
            "event_count": len(written_events),
            "events": written_events,
        }, ensure_ascii=False, default=str)

    except Exception as e:
        logger.error("add_external_chunk 失败: %s", e)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 构建挂载用的 ASGI app：auth middleware + MCP SSE
# 导出给 main.py: app.mount("/mcp/memory", mcp_sse_app)
# ---------------------------------------------------------------------------


@mcp.tool()
async def update_memory_card(persona_id: str, dimension: str, content: str) -> str:
    """提交记忆卡片内容更新审批。

    使用时机：需要改写某个角色/persona 的某个记忆维度时。
    persona_id 按现有数据库映射为 memory_cards.character_id；dimension 必须与已有记忆卡片维度匹配。
    本工具只创建 pending approval，不会立刻生效。返回 approval_id 后等待 Mini App/Telegram 审批。
    """
    try:
        from memory.database import get_database

        db = get_database()
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, character_id, dimension, content,
                       updated_at, source_message_id, is_active
                FROM memory_cards
                WHERE character_id = $1 AND dimension = $2 AND is_active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                str(persona_id),
                str(dimension),
            )
        if not row:
            return _json({"success": False, "error": "memory_card not found"})
        before = _record_to_dict(row)
        after = {**before, "content": str(content)}
        return await _queue_pending_update(
            "update_memory_card",
            {"persona_id": str(persona_id), "dimension": str(dimension), "content": str(content)},
            before,
            after,
        )
    except Exception as e:
        logger.error("update_memory_card failed: %s", e)
        return _json({"success": False, "error": str(e)})


@mcp.tool()
async def update_temporal_state(id: str, content: str) -> str:
    """提交时效状态内容更新审批。

    参数 id 是 temporal_states.id；content 会写入 state_content。
    仅允许修改 content/state_content，不接受、不应尝试传 expires_at。
    本工具只创建 pending approval，审批通过后才生效。
    """
    try:
        from memory.database import get_database

        db = get_database()
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, state_content, action_rule, expire_at, is_active, created_at
                FROM temporal_states
                WHERE id = $1
                """,
                str(id),
            )
        if not row:
            return _json({"success": False, "error": "temporal_state not found"})
        before = _record_to_dict(row)
        after = {**before, "state_content": str(content)}
        return await _queue_pending_update(
            "update_temporal_state",
            {"id": str(id), "content": str(content)},
            before,
            after,
        )
    except Exception as e:
        logger.error("update_temporal_state failed: %s", e)
        return _json({"success": False, "error": str(e)})


@mcp.tool()
async def update_relationship_timeline_entry(id: str, content: str) -> str:
    """提交关系时间线条目内容更新审批。

    参数 id 是 relationship_timeline.id；content 会写入 relationship_timeline.content。
    调用前应先用 get_relationship_timeline 找到精确条目。
    本工具只创建 pending approval，审批通过后才生效。
    """
    try:
        from memory.database import get_database

        db = get_database()
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, created_at, event_type, content, source_summary_id
                FROM relationship_timeline
                WHERE id = $1
                """,
                str(id),
            )
        if not row:
            return _json({"success": False, "error": "relationship_timeline entry not found"})
        before = _record_to_dict(row)
        after = {**before, "content": str(content)}
        return await _queue_pending_update(
            "update_relationship_timeline_entry",
            {"id": str(id), "content": str(content)},
            before,
            after,
        )
    except Exception as e:
        logger.error("update_relationship_timeline_entry failed: %s", e)
        return _json({"success": False, "error": str(e)})


@mcp.tool()
async def update_persona_field(persona_id: int, field_name: str, content: str) -> str:
    """提交人设字段更新审批。

    persona_id 是 persona_configs.id。field_name 只允许：
    char_identity / char_personality / char_speech_style / char_redlines / char_appearance / char_relationships / char_nsfw。
    field_name 不在白名单时会直接返回 error。
    本工具只创建 pending approval，审批通过后才生效。
    """
    try:
        field = str(field_name or "").strip()
        if field not in UPDATE_PERSONA_FIELD_WHITELIST:
            return _json({"success": False, "error": "field_name not allowed"})

        from memory.database import get_database

        db = get_database()
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM persona_configs WHERE id = $1", int(persona_id))
        if not row:
            return _json({"success": False, "error": "persona not found"})
        before = _record_to_dict(row)
        after = {**before, field: str(content)}
        return await _queue_pending_update(
            "update_persona_field",
            {"persona_id": int(persona_id), "field_name": field, "content": str(content)},
            before,
            after,
        )
    except Exception as e:
        logger.error("update_persona_field failed: %s", e)
        return _json({"success": False, "error": str(e)})


def _assign_tool_scopes() -> None:
    for fn in (
        search_memories,
        get_recent_summaries,
        get_memory_cards,
        get_temporal_states,
        get_relationship_timeline,
        get_persona,
        get_context_trace,
    ):
        fn._required_scopes = READ_TOOL_SCOPES
    add_external_chunk._required_scopes = WEB_WRITE_SCOPES
    for fn in (
        update_memory_card,
        update_temporal_state,
        update_relationship_timeline_entry,
        update_persona_field,
    ):
        fn._required_scopes = API_ADMIN_SCOPES


async def _filtered_list_tools() -> List[mcp_types.Tool]:
    current_scope = _current_mcp_scope()
    tools: List[mcp_types.Tool] = []
    for info in mcp._tool_manager.list_tools():
        if not _tool_allowed_for_scope(info, current_scope):
            continue
        tools.append(
            mcp_types.Tool(
                name=info.name,
                title=info.title,
                description=info.description,
                inputSchema=info.parameters,
                outputSchema=info.output_schema,
                annotations=info.annotations,
                icons=info.icons,
                _meta=info.meta,
            )
        )
    return tools


async def _filtered_call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    tool_info = mcp._tool_manager.get_tool(name)
    current_scope = _current_mcp_scope()
    if tool_info is not None and not _tool_allowed_for_scope(tool_info, current_scope):
        payload = {"error": "forbidden"}
        await _audit(current_scope or "unknown", name, arguments, "error", "forbidden")
        return ([mcp_types.TextContent(type="text", text=_json(payload))], payload)
    return await mcp.call_tool(name, arguments or {})


_assign_tool_scopes()
mcp._mcp_server.list_tools()(_filtered_list_tools)
mcp._mcp_server.call_tool(validate_input=False)(_filtered_call_tool)

mcp_sse_app = MCPAuthMiddleware(mcp.sse_app())
