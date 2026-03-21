"""
Context 构建模块。

负责组装发送给 LLM 的完整 prompt，按照优先级从上到下拼装：
1. system prompt：从配置读取，保持原样
2. temporal_states：is_active=1 的全部记录（在记忆卡片之前）
3. memory_cards：查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化后拼入
4. relationship_timeline：取最近 3 条（库内按倒序选取），注入 Context 时按 created_at 正序排列
5. daily summary：查询 summaries 表中 summary_type='daily'，按 created_at 倒序取最近 5 条，然后翻转为正序（按时间从老到新）后拼入
6. chunk summary：查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选），附带其来源标识，按时间正序拼入
7. 向量检索：Chroma top5 + BM25 top5 去重；长期记忆在进入精排前按 parent_id 父子折叠；注入时每条带 [uid:doc_id] 前缀
8. 最近消息：查询当前 session_id 下 is_summarized=0 的消息，按 created_at 倒序取 40 条，再正序排列后拼入

组装完成后返回一个结构，包含 system prompt 和 messages 数组，直接可以传给 LLM API。
"""

import logging
import math
import time
from typing import Dict, List, Any, Optional
from datetime import datetime

from config import config
from memory.database import (
    get_all_active_memory_cards,
    get_all_active_temporal_states,
    get_recent_relationship_timeline,
    get_recent_daily_summaries,
    get_today_chunk_summaries,
    get_unsummarized_messages_desc
)

# 导入向量存储函数
try:
    from .vector_store import search_memory
except ImportError:
    from memory.vector_store import search_memory

# 导入 BM25 检索函数
try:
    from .bm25_retriever import search_bm25
except ImportError:
    from memory.bm25_retriever import search_bm25

# 导入 Reranker 函数
try:
    from .reranker import rerank
except ImportError:
    from memory.reranker import rerank

# 设置日志
logger = logging.getLogger(__name__)

MEMORY_CITATION_DIRECTIVE = (
    "如果你在生成回复时参考了上述历史记忆，必须在回复文本末尾标注引用，格式为 [[used:uid]]，可以有多个。"
)


def _created_at_timestamp_for_sort(created_at: Any) -> float:
    """用于 relationship_timeline 等按时间正序排序；无法解析时置 0（排在最前）。"""
    if not created_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return float(dt.timestamp())
    except (TypeError, ValueError, OSError):
        return 0.0


def _parent_group_key(result: Dict[str, Any]) -> str:
    md = result.get("metadata") or {}
    pid = md.get("parent_id")
    if pid is not None and str(pid).strip():
        return str(pid).strip()
    rid = result.get("id")
    return str(rid) if rid is not None else ""


def _semantic_similarity_for_collapse(result: Dict[str, Any], bm25_max: float) -> float:
    """组内比较用：向量用 score；BM25 分数按批次最大值缩放到约 [0,1]。"""
    if result.get("retrieval_method") == "vector":
        return float(result.get("score") or 0.0)
    bm = float(result.get("score") or 0.0)
    denom = bm25_max if bm25_max > 0 else 1.0
    return min(1.0, bm / denom)


def collapse_longterm_by_parent_id(all_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    按 parent_id 分组：事件片段与父 daily 同组；组内仅保留语义相似度（向量 score 或归一化 BM25）最高的一条。
    输出顺序与首次出现的组顺序一致。
    """
    if not all_results:
        return []
    bm25_scores = [
        float(r.get("score") or 0.0)
        for r in all_results
        if r.get("retrieval_method") == "bm25"
    ]
    bm25_max = max(bm25_scores) if bm25_scores else 1.0

    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for r in all_results:
        key = _parent_group_key(r)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    collapsed: List[Dict[str, Any]] = []
    for key in order:
        items = groups[key]
        best = max(
            items,
            key=lambda x: _semantic_similarity_for_collapse(x, bm25_max),
        )
        collapsed.append(best)
    return collapsed


def _merge_vector_bm25_dedupe(
    vector_results: List[Dict[str, Any]],
    bm25_results: List[Dict[str, Any]],
    max_total: int = 10,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for result in vector_results:
        doc_id = result.get("id")
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            r = dict(result)
            r["retrieval_method"] = "vector"
            merged.append(r)
    for result in bm25_results:
        doc_id = result.get("id")
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            r = dict(result)
            r["retrieval_method"] = "bm25"
            merged.append(r)
    return merged[:max_total]


def _memory_age_days(metadata: Dict[str, Any], now_ts: float) -> float:
    md = metadata or {}
    created = md.get("created_at")
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            return max(0.0, (now_ts - dt.timestamp()) / 86400.0)
        except (TypeError, ValueError):
            pass
    try:
        lt = float(md.get("last_access_ts", now_ts))
    except (TypeError, ValueError):
        lt = now_ts
    return max(0.0, (now_ts - lt) / 86400.0)


def _decay_resurrection_raw(metadata: Dict[str, Any], age_days: float) -> float:
    md = metadata or {}
    try:
        base = float(md.get("base_score", md.get("score", 5.0)))
    except (TypeError, ValueError):
        base = 5.0
    try:
        hl = int(md.get("halflife_days") or 30)
    except (TypeError, ValueError):
        hl = 30
    hl = max(1, hl)
    try:
        hits = int(md.get("hits") or 0)
    except (TypeError, ValueError):
        hits = 0
    hits = max(0, hits)
    exp_part = math.exp(-math.log(2) / float(hl) * age_days)
    return base * exp_part * (1.0 + 0.35 * math.log(1 + hits))


def fuse_rerank_with_time_decay(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    精排综合分：0.8×语义(归一化) + 0.2×时间衰减复活分(归一化)。
    语义分优先用 Cohere rerank_score，否则用检索 score。
    """
    if not candidates:
        return []
    now_ts = time.time()
    sem_raw: List[float] = []
    for c in candidates:
        if c.get("rerank_score") is not None:
            sem_raw.append(float(c["rerank_score"]))
        else:
            sem_raw.append(float(c.get("score") or 0.0))
    smin, smax = min(sem_raw), max(sem_raw)

    def norm_sem(i: int) -> float:
        if smax <= smin:
            return 1.0
        return (sem_raw[i] - smin) / (smax - smin)

    decay_raw: List[float] = []
    for c in candidates:
        md = c.get("metadata") or {}
        age = _memory_age_days(md, now_ts)
        decay_raw.append(_decay_resurrection_raw(md, age))
    dmin, dmax = min(decay_raw), max(decay_raw)

    def norm_dec(i: int) -> float:
        if dmax <= dmin:
            return 1.0
        return (decay_raw[i] - dmin) / (dmax - dmin)

    scored: List[Dict[str, Any]] = []
    for i, c in enumerate(candidates):
        cp = dict(c)
        cp["fusion_score"] = 0.8 * norm_sem(i) + 0.2 * norm_dec(i)
        scored.append(cp)
    scored.sort(key=lambda x: x["fusion_score"], reverse=True)
    return scored


class ContextBuilder:
    """
    Context 构建器类。
    
    负责组装完整的对话上下文，供 LLM 使用。
    """
    
    def __init__(self):
        """
        初始化 Context 构建器。
        """
        logger.info("Context 构建器初始化完成")
    
    def build_context(self, session_id: str, user_message: str) -> Dict[str, Any]:
        """
        构建完整的对话上下文。
        
        按照优先级从上到下拼装：
        1. system prompt
        2. temporal_states（is_active=1）
        3. memory_cards
        4. relationship_timeline（最近 3 条，created_at 正序注入）
        5. daily summary
        6. chunk summary
        7. 向量检索（折叠 + [uid:doc_id]）
        8. 最近消息
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息
            
        Returns:
            Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
        """
        try:
            # 1. 获取 system prompt
            system_prompt = self._build_system_prompt()

            temporal_section = self._build_temporal_states_section()
            
            # 2. 获取 memory cards
            memory_cards_section = self._build_memory_cards_section()

            relationship_timeline_section = self._build_relationship_timeline_section()
            
            # 3. 获取 daily summaries
            daily_summaries_section = self._build_daily_summaries_section()
            
            # 4. 获取 today's chunk summaries
            chunk_summaries_section = self._build_chunk_summaries_section()
            
            # 5. 获取向量检索结果
            vector_search_section = self._build_vector_search_section(user_message)
            
            # 6. 获取最近消息
            recent_messages_section = self._build_recent_messages_section(session_id)
            
            # 7. 添加当前用户消息
            current_user_message = self._build_current_user_message(user_message)
            
            # 组装完整的 system prompt
            full_system_prompt = self._assemble_full_system_prompt(
                system_prompt,
                temporal_section,
                memory_cards_section,
                relationship_timeline_section,
                daily_summaries_section,
                chunk_summaries_section,
                vector_search_section
            )
            
            # 组装 messages 数组
            messages = self._assemble_messages(
                full_system_prompt,
                recent_messages_section,
                current_user_message
            )
            
            logger.debug(f"Context 构建完成: session={session_id}, system_prompt_length={len(full_system_prompt)}, messages_count={len(messages)}")
            
            return {
                "system_prompt": full_system_prompt,
                "messages": messages
            }
            
        except Exception as e:
            logger.error(f"构建 context 失败: {e}")
            # 返回最小化的 context
            return {
                "system_prompt": config.SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": user_message}
                ]
            }
    
    async def build_context_async(self, session_id: str, user_message: str) -> Dict[str, Any]:
        """
        异步构建完整的对话上下文（支持 Reranker）。
        
        按照优先级从上到下拼装：
        1. system prompt
        2. temporal_states（is_active=1）
        3. memory_cards
        4. relationship_timeline（最近 3 条，created_at 正序注入）
        5. daily summary
        6. chunk summary
        7. 向量检索（折叠 → Cohere 全候选打分 → 语义×0.8+衰减×0.2 → top 2，[uid:doc_id]）
        8. 最近消息
        
        使用 asyncio.gather 并行执行向量检索和 BM25 检索，
        合并去重并父子折叠后，对全量候选 await rerank()，再按融合分排序取 top 2。
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息
            
        Returns:
            Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
        """
        try:
            # 1. 获取 system prompt
            system_prompt = self._build_system_prompt()

            temporal_section = self._build_temporal_states_section()
            
            # 2. 获取 memory cards
            memory_cards_section = self._build_memory_cards_section()

            relationship_timeline_section = self._build_relationship_timeline_section()
            
            # 3. 获取 daily summaries
            daily_summaries_section = self._build_daily_summaries_section()
            
            # 4. 获取 today's chunk summaries
            chunk_summaries_section = self._build_chunk_summaries_section()
            
            # 5. 获取向量检索结果（带 Reranker）
            vector_search_section = await self._build_vector_search_section_async(user_message)
            
            # 6. 获取最近消息
            recent_messages_section = self._build_recent_messages_section(session_id)
            
            # 7. 添加当前用户消息
            current_user_message = self._build_current_user_message(user_message)
            
            # 组装完整的 system prompt
            full_system_prompt = self._assemble_full_system_prompt(
                system_prompt,
                temporal_section,
                memory_cards_section,
                relationship_timeline_section,
                daily_summaries_section,
                chunk_summaries_section,
                vector_search_section
            )
            
            # 组装 messages 数组
            messages = self._assemble_messages(
                full_system_prompt,
                recent_messages_section,
                current_user_message
            )
            
            logger.debug(f"Context 构建完成（异步）: session={session_id}, system_prompt_length={len(full_system_prompt)}, messages_count={len(messages)}")
            
            return {
                "system_prompt": full_system_prompt,
                "messages": messages
            }
            
        except Exception as e:
            logger.error(f"构建 context 失败（异步）: {e}")
            # 返回最小化的 context
            return {
                "system_prompt": config.SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": user_message}
                ]
            }
    
    def _build_system_prompt(self) -> str:
        """
        构建基础 system prompt。
        
        Returns:
            str: 基础 system prompt
        """
        return config.SYSTEM_PROMPT

    def _build_temporal_states_section(self) -> str:
        """temporal_states 中 is_active=1 的全部记录（置于记忆卡片之前）。"""
        try:
            rows = get_all_active_temporal_states()
            if not rows:
                return ""
            lines: List[str] = ["# 时效状态（进行中）", ""]
            for row in rows:
                expire_at = row.get("expire_at") or "未设置"
                content = (row.get("state_content") or "").strip()
                rule = (row.get("action_rule") or "").strip()
                sid = row.get("id", "")
                chunk = f"- **{sid}**（至 {expire_at}）\n  - 状态：{content}"
                if rule:
                    chunk += f"\n  - 行为规则：{rule}"
                lines.append(chunk)
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"构建 temporal_states 部分失败: {e}")
            return ""

    def _build_relationship_timeline_section(self) -> str:
        """relationship_timeline：库内取最近 3 条，拼入前按 created_at 升序排列。"""
        type_labels = {
            "milestone": "里程碑",
            "emotional_shift": "情绪转折",
            "conflict": "冲突",
            "daily_warmth": "日常温情",
        }
        try:
            rows = get_recent_relationship_timeline(limit=3)
            if not rows:
                return ""
            rows = sorted(
                rows,
                key=lambda r: _created_at_timestamp_for_sort(r.get("created_at")),
            )
            lines: List[str] = ["# 关系时间线（最近）", ""]
            for row in rows:
                et = row.get("event_type") or ""
                label = type_labels.get(et, et)
                created = row.get("created_at") or ""
                if created:
                    try:
                        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                        created_fmt = dt.strftime("%Y-%m-%d %H:%M")
                    except (TypeError, ValueError):
                        created_fmt = str(created)
                else:
                    created_fmt = "未知时间"
                content = (row.get("content") or "").strip()
                lines.append(f"- **{label}**（{created_fmt}）\n  {content}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"构建 relationship_timeline 部分失败: {e}")
            return ""
    
    def _build_memory_cards_section(self) -> str:
        """
        构建 memory cards 部分。
        
        查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化。
        
        Returns:
            str: memory cards 部分的文本，如果没有则返回空字符串
        """
        try:
            memory_cards = get_all_active_memory_cards(limit=100)
            
            if not memory_cards:
                return ""
            
            # 按维度分组
            dimension_groups = {}
            for card in memory_cards:
                dimension = card['dimension']
                if dimension not in dimension_groups:
                    dimension_groups[dimension] = []
                dimension_groups[dimension].append(card)
            
            # 构建格式化文本
            sections = []
            for dimension, cards in dimension_groups.items():
                # 维度名称映射
                dimension_names = {
                    "preferences": "偏好与喜恶",
                    "interaction_patterns": "相处模式",
                    "current_status": "近况与生活动态",
                    "goals": "目标与计划",
                    "relationships": "重要关系",
                    "key_events": "重要事件",
                    "rules": "相处规则与禁区"
                }
                
                dimension_name = dimension_names.get(dimension, dimension)
                section_lines = [f"## {dimension_name}"]
                
                for card in cards:
                    # 格式化更新时间
                    updated_at = card['updated_at']
                    if updated_at:
                        try:
                            dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                            formatted_time = dt.strftime("%Y-%m-%d %H:%M")
                        except:
                            formatted_time = updated_at
                    else:
                        formatted_time = "未知时间"
                    
                    section_lines.append(f"- {card['content']} (更新于: {formatted_time})")
                
                sections.append("\n".join(section_lines))
            
            if sections:
                memory_section = "\n\n".join(sections)
                return f"# 用户记忆卡片\n\n{memory_section}"
            else:
                return ""
                
        except Exception as e:
            logger.error(f"构建 memory cards 部分失败: {e}")
            return ""
    
    def _build_daily_summaries_section(self) -> str:
        """
        构建 daily summary 部分。
        
        查询 summaries 表中 summary_type='daily'，按 created_at 倒序取最近 5 条，
        然后在代码中将其翻转为正序（按时间从老到新）。
        
        Returns:
            str: daily summary 部分的文本，如果没有则返回空字符串
        """
        try:
            daily_summaries = get_recent_daily_summaries(limit=config.CONTEXT_MAX_DAILY_SUMMARIES)
            
            if not daily_summaries:
                return ""
            
            # 翻转为正序（最旧的在前）
            daily_summaries.reverse()
            
            sections = []
            for summary in daily_summaries:
                # 格式化创建时间
                created_at = summary['created_at']
                if created_at:
                    try:
                        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        formatted_date = dt.strftime("%Y-%m-%d")
                    except:
                        formatted_date = created_at.split(' ')[0] if ' ' in created_at else created_at
                else:
                    formatted_date = "未知日期"
                
                sections.append(f"### {formatted_date}\n{summary['summary_text']}")
            
            if sections:
                daily_section = "\n\n".join(sections)
                return f"# 每日摘要\n\n{daily_section}"
            else:
                return ""
                
        except Exception as e:
            logger.error(f"构建 daily summary 部分失败: {e}")
            return ""
    
    def _build_chunk_summaries_section(self) -> str:
        """
        构建 chunk summary 部分。
        
        查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选）。
        在拼入时，附带其来源标识（格式如 [来自频道 {session_id}]: 摘要内容），按时间正序拼入。
        
        Returns:
            str: chunk summary 部分的文本，如果没有则返回空字符串
        """
        try:
            chunk_summaries = get_today_chunk_summaries()
            
            if not chunk_summaries:
                return ""
            
            sections = []
            for summary in chunk_summaries:
                # 格式化创建时间
                created_at = summary['created_at']
                if created_at:
                    try:
                        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        formatted_time = dt.strftime("%H:%M")
                    except:
                        formatted_time = created_at.split(' ')[1] if ' ' in created_at else created_at
                else:
                    formatted_time = "未知时间"
                
                session_id = summary['session_id']
                # 简化 session_id 显示
                if '_' in session_id:
                    parts = session_id.split('_')
                    if len(parts) >= 2:
                        display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                    else:
                        display_session = session_id[:20]
                else:
                    display_session = session_id[:20]
                
                sections.append(f"### {formatted_time} [来自: {display_session}]\n{summary['summary_text']}")
            
            if sections:
                chunk_section = "\n\n".join(sections)
                return f"# 今日对话摘要\n\n{chunk_section}"
            else:
                return ""
                
        except Exception as e:
            logger.error(f"构建 chunk summary 部分失败: {e}")
            return ""
    
    def _build_vector_search_section(self, user_message: str) -> str:
        """
        构建向量检索部分（同步，无 Cohere 精排）。
        
        双路融合后按 parent_id 父子折叠，注入时每条正文前带 [uid:doc_id]。
        """
        try:
            if not config.ZHIPU_API_KEY or config.ZHIPU_API_KEY == "your_zhipu_api_key_here":
                logger.warning("ZHIPU_API_KEY 未设置或为默认值，跳过向量检索")
                return ""

            vector_results = search_memory(user_message, top_k=5)
            bm25_results = search_bm25(user_message, top_k=5)
            all_results = _merge_vector_bm25_dedupe(vector_results, bm25_results, 10)
            all_results = collapse_longterm_by_parent_id(all_results)

            if not all_results:
                logger.debug("双路检索未找到相关记忆")
                return ""

            sections = []
            for i, result in enumerate(all_results):
                text = (result.get("text") or "").strip()
                doc_id = result.get("id") or ""
                metadata = result.get("metadata") or {}
                score = float(result.get("score") or 0.0)
                retrieval_method = result.get("retrieval_method", "unknown")
                date = metadata.get("date", "未知日期")
                summary_type = metadata.get("summary_type", "未知类型")
                session_id = metadata.get("session_id", "未知会话")
                if "_" in str(session_id):
                    parts = str(session_id).split("_")
                    if len(parts) >= 2:
                        display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                    else:
                        display_session = str(session_id)[:20]
                else:
                    display_session = str(session_id)[:20]
                method_label = "向量" if retrieval_method == "vector" else "关键词"
                body = f"[uid:{doc_id}] {text}"
                sections.append(
                    f"### 相关记忆 {i+1} ({method_label}检索，分数: {score:.2f})\n"
                    f"日期: {date} | 类型: {summary_type} | 来源: {display_session}\n{body}"
                )

            vector_section = "\n\n".join(sections)
            vector_section += "\n\n<!-- 以上是双路检索结果（已父子折叠）；异步路径下由 Reranker 精排 -->"
            return f"# 相关长期记忆（双路检索结果）\n\n{vector_section}"

        except Exception as e:
            logger.error(f"构建向量检索部分失败: {e}")
            return ""
    
    async def _build_vector_search_section_async(self, user_message: str) -> str:
        """
        异步构建向量检索部分：并行双路检索 → 父子折叠 → Cohere 打分 →
        语义归一化×0.8 + 时间衰减复活分归一化×0.2 综合排序 → 取 top 2；
        每条正文前带 [uid:doc_id]。
        """
        try:
            if not config.ZHIPU_API_KEY or config.ZHIPU_API_KEY == "your_zhipu_api_key_here":
                logger.warning("ZHIPU_API_KEY 未设置或为默认值，跳过向量检索")
                return ""

            if not config.COHERE_API_KEY or config.COHERE_API_KEY == "your_cohere_api_key_here":
                logger.warning("COHERE_API_KEY 未设置或为默认值，使用普通双路检索")
                return self._build_vector_search_section(user_message)

            import asyncio

            logger.debug(f"开始并行检索，查询: '{user_message[:50]}...'")
            loop = asyncio.get_event_loop()
            vector_future = loop.run_in_executor(None, search_memory, user_message, 5)
            bm25_future = loop.run_in_executor(None, search_bm25, user_message, 5)
            vector_results, bm25_results = await asyncio.gather(vector_future, bm25_future)

            logger.debug(
                f"并行检索完成，向量结果: {len(vector_results)} 条，BM25 结果: {len(bm25_results)} 条"
            )

            all_results = _merge_vector_bm25_dedupe(vector_results, bm25_results, 10)
            all_results = collapse_longterm_by_parent_id(all_results)

            if not all_results:
                logger.debug("双路检索未找到相关记忆")
                return ""

            logger.debug(f"调用 Reranker（全量候选语义分），文档: {len(all_results)} 条")
            reranked_results = await rerank(
                user_message, all_results, top_n=len(all_results)
            )
            if not reranked_results:
                logger.debug("Reranker 未返回结果，使用折叠后候选前 2 条")
                reranked_results = all_results[:2]

            fused = fuse_rerank_with_time_decay(reranked_results)
            top_results = fused[:2]

            sections = []
            for i, result in enumerate(top_results):
                text = (result.get("text") or "").strip()
                doc_id = result.get("id") or ""
                metadata = result.get("metadata") or {}
                fusion = float(result.get("fusion_score", 0.0))
                retrieval_method = result.get("retrieval_method", "unknown")
                date = metadata.get("date", "未知日期")
                summary_type = metadata.get("summary_type", "未知类型")
                session_id = metadata.get("session_id", "未知会话")
                if "_" in str(session_id):
                    parts = str(session_id).split("_")
                    if len(parts) >= 2:
                        display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                    else:
                        display_session = str(session_id)[:20]
                else:
                    display_session = str(session_id)[:20]
                method_label = "向量" if retrieval_method == "vector" else "关键词"
                body = f"[uid:{doc_id}] {text}"
                sections.append(
                    f"### 相关记忆 {i+1} ({method_label}检索，综合分: {fusion:.4f})\n"
                    f"日期: {date} | 类型: {summary_type} | 来源: {display_session}\n{body}"
                )

            vector_section = "\n\n".join(sections)
            vector_section += (
                f"\n\n<!-- 精排：语义×0.8+时间衰减×0.2，自 {len(all_results)} 条折叠后候选取 {len(top_results)} 条 -->"
            )
            return f"# 相关长期记忆（精排结果）\n\n{vector_section}"

        except Exception as e:
            logger.error(f"构建向量检索部分失败（异步）: {e}")
            logger.warning("异步检索失败，回退到同步检索")
            return self._build_vector_search_section(user_message)
    
    def _build_recent_messages_section(self, session_id: str) -> List[Dict[str, str]]:
        """
        构建最近消息部分。
        
        查询当前 session_id 下 is_summarized=0 的消息，按 created_at 倒序取 40 条，
        再正序排列后返回。
        
        Args:
            session_id: 会话ID
            
        Returns:
            List[Dict[str, str]]: 消息列表，每条消息包含 role 和 content
        """
        try:
            recent_messages = get_unsummarized_messages_desc(
                session_id, 
                limit=config.CONTEXT_MAX_RECENT_MESSAGES
            )
            
            if not recent_messages:
                return []
            
            # 转换为 LLM 接口期望的格式
            messages = []
            for msg in recent_messages:
                role = "user" if msg['role'] == "user" else "assistant"
                messages.append({
                    "role": role,
                    "content": msg['content']
                })
            
            logger.debug(f"获取最近消息: session={session_id}, count={len(messages)}")
            return messages
            
        except Exception as e:
            logger.error(f"构建最近消息部分失败: {e}")
            return []
    
    def _build_current_user_message(self, user_message: str) -> Dict[str, str]:
        """
        构建当前用户消息。
        
        Args:
            user_message: 用户当前消息
            
        Returns:
            Dict[str, str]: 当前用户消息
        """
        return {
            "role": "user",
            "content": user_message
        }
    
    def _assemble_full_system_prompt(
        self,
        system_prompt: str,
        temporal_states_section: str,
        memory_cards_section: str,
        relationship_timeline_section: str,
        daily_summaries_section: str,
        chunk_summaries_section: str,
        vector_search_section: str,
    ) -> str:
        """
        组装完整的 system prompt。

        顺序：system → temporal_states → memory_cards → relationship_timeline
        → daily → chunk → 长期记忆检索；末尾注入引用死命令。
        """
        sections = [system_prompt]

        if temporal_states_section:
            sections.append(temporal_states_section)

        if memory_cards_section:
            sections.append(memory_cards_section)

        if relationship_timeline_section:
            sections.append(relationship_timeline_section)

        if daily_summaries_section:
            sections.append(daily_summaries_section)

        if chunk_summaries_section:
            sections.append(chunk_summaries_section)

        if vector_search_section:
            sections.append(vector_search_section)

        if len(sections) > 1:
            sections.append("---")
            sections.append("以上是历史信息和用户记忆，请基于这些信息进行对话。")

        sections.append(MEMORY_CITATION_DIRECTIVE)

        return "\n\n".join(sections)
    
    def _assemble_messages(self, full_system_prompt: str,
                          recent_messages: List[Dict[str, str]],
                          current_user_message: Dict[str, str]) -> List[Dict[str, str]]:
        """
        组装完整的 messages 数组。
        
        Args:
            full_system_prompt: 完整的 system prompt
            recent_messages: 最近消息列表
            current_user_message: 当前用户消息
            
        Returns:
            List[Dict[str, str]]: 完整的 messages 数组
        """
        messages = []
        
        # 添加 system prompt
        if full_system_prompt:
            messages.append({
                "role": "system",
                "content": full_system_prompt
            })
        
        # 添加历史消息
        messages.extend(recent_messages)
        
        # 添加当前用户消息
        messages.append(current_user_message)
        
        return messages


# 便捷函数
def build_context(session_id: str, user_message: str) -> Dict[str, Any]:
    """
    构建对话上下文的便捷函数。
    
    Args:
        session_id: 会话ID
        user_message: 用户当前消息
        
    Returns:
        Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
    """
    builder = ContextBuilder()
    return builder.build_context(session_id, user_message)


if __name__ == "__main__":
    """Context 构建模块测试入口。"""
    import sys
    import os
    
    # 添加项目根目录到 Python 路径
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("测试 Context 构建器...")
    
    try:
        # 创建测试数据
        from memory.database import get_database
        db = get_database()
        
        test_session = "context_builder_test_session"
        
        # 清理测试数据
        db.clear_session_messages(test_session)
        
        # 保存测试消息
        for i in range(10):
            db.save_message("user", f"测试用户消息 {i+1}", test_session)
            db.save_message("assistant", f"测试助手回复 {i+1}", test_session)
        
        # 测试构建 context
        builder = ContextBuilder()
        context = builder.build_context(test_session, "你好，这是一个测试消息")
        
        print(f"Context 构建成功:")
        print(f"System Prompt 长度: {len(context['system_prompt'])}")
        print(f"Messages 数量: {len(context['messages'])}")
        
        # 显示结构
        print("\nSystem Prompt 预览:")
        print(context['system_prompt'][:200] + "..." if len(context['system_prompt']) > 200 else context['system_prompt'])
        
        print("\nMessages 结构:")
        for i, msg in enumerate(context['messages']):
            role = msg['role']
            content_preview = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
            print(f"  [{i}] {role}: {content_preview}")
        
        # 清理测试数据
        db.clear_session_messages(test_session)
        print("\nContext 构建器测试完成！")
        
    except Exception as e:
        print(f"Context 构建器测试失败: {e}")
        import traceback
        traceback.print_exc()
