"""
Context 构建模块。

负责组装发送给 LLM 的完整 prompt，按照优先级从上到下拼装：
1. system prompt：从配置读取，保持原样
2. temporal_states：is_active=1 的全部记录（在记忆卡片之前）
3. memory_cards：查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化后拼入
4. relationship_timeline：条数见 `relationship_timeline_limit`（库内选取），注入 Context 时按 created_at 正序排列
5. daily summary：`context_max_daily_summaries`（优先）或环境变量决定条数，倒序取后翻为正序拼入
6. chunk summary：查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选），附带其来源标识，按时间正序拼入
7. 向量检索：各路 `retrieval_top_k` 条，去重合并，父子折叠后注入 `context_max_longterm` 条（异步路径经精排后再截断）
8. 最近消息：`short_term_limit`（优先）或环境变量决定条数，再正序排列后拼入

组装完成后返回一个结构，包含 system prompt 和 messages 数组，直接可以传给 LLM API。
"""

import logging
import math
import re
import time
from functools import partial
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

from config import config
from memory.database import (
    get_all_active_memory_cards,
    get_all_active_temporal_states,
    get_database,
    get_recent_relationship_timeline,
    get_recent_daily_summaries,
    get_today_chunk_summaries,
    get_unsummarized_messages_desc,
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

_USER_IMAGE_CONTENT_RE = re.compile(
    r"^\[发送了(\d+)张图片\]\s*(.*)$", re.DOTALL
)


def _is_user_media_marker_line(stripped_line: str) -> bool:
    if stripped_line.startswith("[贴纸]"):
        return True
    if stripped_line.startswith("[语音]"):
        return True
    if stripped_line.startswith("[发送了") and "张图片]" in stripped_line:
        return True
    return False


def _extract_plain_user_text(content: str) -> str:
    """去掉图片/贴纸/语音结构行后的用户纯文字（放格式化结果最前）。"""
    lines = content.split("\n")
    kept: List[str] = []
    for line in lines:
        st = line.strip()
        if _is_user_media_marker_line(st):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _infer_media_type_order_from_content(content: str) -> List[str]:
    """旧行无 media_type 时，按正文出现顺序推断 image / sticker / voice。"""
    order: List[str] = []
    for line in content.split("\n"):
        st = line.strip()
        if st.startswith("[发送了") and "张图片]" in st and "image" not in order:
            order.append("image")
        if st.startswith("[贴纸]") and "sticker" not in order:
            order.append("sticker")
        if st.startswith("[语音]") and "voice" not in order:
            order.append("voice")
    return order


def _format_reaction_part(msg: Dict[str, Any]) -> str:
    """反应类消息：content 已在 Bot 层拼好，原样注入上下文。"""
    return msg.get("content") or ""


def _format_image_part(msg: Dict[str, Any]) -> str:
    """
    图片块：用户配文 + 可选系统视觉档案。
    未来多图可升级为 JSON 数组，当前 image_caption 按字符串处理。
    """
    content = (msg.get("content") or "").strip()
    cap = (msg.get("image_caption") or "").strip()
    m = _USER_IMAGE_CONTENT_RE.match(content)
    if m:
        n = m.group(1)
        ucap = (m.group(2) or "").strip()
    else:
        if not cap and "[发送了" not in content:
            return ""
        n = "1"
        ucap = _extract_plain_user_text(content) or content
    line1 = f"[用户发送了{n}张图片]：{ucap}" if ucap else f"[用户发送了{n}张图片]："
    if cap:
        return f"{line1}\n[系统视觉档案]：{cap}"
    return line1


def _format_sticker_part(msg: Dict[str, Any]) -> str:
    lines = (msg.get("content") or "").split("\n")
    out: List[str] = []
    prefix = "[贴纸]"
    for line in lines:
        s = line.strip()
        if not s.startswith(prefix):
            continue
        rest = s[len(prefix) :].lstrip()
        sp = rest.find(" ")
        desc = rest[sp + 1 :].lstrip() if sp != -1 else rest
        out.append(f"[用户发送了一个贴纸]：{desc}")
    return "\n".join(out)


def _format_voice_part(msg: Dict[str, Any]) -> str:
    text = msg.get("content") or ""
    lines = text.split("\n")
    out: List[str] = []
    prefix = "[语音]"
    for line in lines:
        s = line.strip()
        if not s.startswith(prefix):
            continue
        inner = s[len(prefix) :].lstrip()
        out.append(f"[用户发送了一条语音]：{inner}")
    if not out and text.strip().startswith(prefix):
        inner = text.strip()[len(prefix) :].lstrip()
        out.append(f"[用户发送了一条语音]：{inner}")
    return "\n".join(out)


def format_user_message_for_context(msg: Dict[str, Any]) -> str:
    """
    用户消息按 media_type 逗号顺序路由到各段格式化函数；纯文字先于媒体段。
    reaction 单独走 _format_reaction_part（不参与复合 media_type 拼接）。
    """
    role = msg.get("role")
    content = msg.get("content") or ""
    if role != "user":
        return content
    media_raw = (msg.get("media_type") or "").strip()
    if media_raw.lower() == "reaction":
        return _format_reaction_part(msg)

    plain = _extract_plain_user_text(content)
    type_order = [x.strip().lower() for x in media_raw.split(",") if x.strip()]
    if not type_order:
        type_order = _infer_media_type_order_from_content(content)

    chunks: List[str] = []
    if plain:
        chunks.append(plain)
    for m in type_order:
        if m == "image":
            part = _format_image_part(msg)
            if part:
                chunks.append(part)
        elif m == "sticker":
            part = _format_sticker_part(msg)
            if part:
                chunks.append(part)
        elif m == "voice":
            part = _format_voice_part(msg)
            if part:
                chunks.append(part)
    if not chunks:
        return content
    return "\n\n".join(chunks)


def _short_term_recent_message_limit() -> int:
    """最近原文条数：优先 config 表 short_term_limit，否则环境变量 CONTEXT_MAX_RECENT_MESSAGES。"""
    try:
        raw = get_database().get_config("short_term_limit")
        if raw is not None and str(raw).strip() != "":
            return max(1, int(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 short_term_limit 失败，使用环境变量: %s", e)
    return config.CONTEXT_MAX_RECENT_MESSAGES


def _context_max_daily_summaries_limit() -> int:
    """每日小传注入条数：优先 config 表 context_max_daily_summaries，否则环境变量 CONTEXT_MAX_DAILY_SUMMARIES。"""
    try:
        raw = get_database().get_config("context_max_daily_summaries")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(100, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 context_max_daily_summaries 失败，使用环境变量: %s", e)
    return max(1, min(100, config.CONTEXT_MAX_DAILY_SUMMARIES))


def _context_max_longterm_count() -> int:
    """长期记忆注入 Top N：优先 config 表 context_max_longterm，否则默认 3。"""
    try:
        raw = get_database().get_config("context_max_longterm")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(20, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 context_max_longterm 失败，使用默认 3: %s", e)
    return 3


def _relationship_timeline_limit() -> int:
    """关系时间线条数：优先 config 表 relationship_timeline_limit，否则默认 3。"""
    try:
        raw = get_database().get_config("relationship_timeline_limit")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(50, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 relationship_timeline_limit 失败，使用默认 3: %s", e)
    return 3


def _retrieval_top_k() -> int:
    """双路检索各路 top_k：优先 config 表 retrieval_top_k，否则默认 5。"""
    try:
        raw = get_database().get_config("retrieval_top_k")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(30, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 retrieval_top_k 失败，使用默认 5: %s", e)
    return 5


MEMORY_CITATION_DIRECTIVE = (
    "如果你在生成回复时参考了上述历史记忆，必须在回复文本末尾标注引用，格式为 [[used:uid]]（半角方括号、双括号），可以有多个。"
    "禁止使用单括号 [used:…]、中文书名号【used:…】，否则无法被系统正确识别。"
)

THINKING_LANGUAGE_DIRECTIVE = (
    "你的思维链（thinking / reasoning）必须使用中文。"
)

def _telegram_segment_limits_from_db() -> Tuple[int, int]:
    """读取 config 表中的 Telegram 分段参数（与 api.config DEFAULT 及校验范围一致）。"""
    db = get_database()

    def _int_key(name: str, default: int, lo: int, hi: int) -> int:
        raw = db.get_config(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            v = int(str(raw).strip())
        except ValueError:
            return default
        return max(lo, min(hi, v))

    max_chars = _int_key("telegram_max_chars", 50, 10, 1000)
    max_chars = max(10, min(1000, round(max_chars / 10) * 10))
    max_msg = _int_key("telegram_max_msg", 8, 1, 20)
    return max_chars, max_msg


def format_telegram_reply_segment_hint() -> str:
    """Telegram 缓冲回复：追加于 system 末尾；MAX_CHARS / MAX_MSG 来自数据库。"""
    max_chars, max_msg = _telegram_segment_limits_from_db()
    return (
        "\n\n"
        "【Telegram 排版与分段指令】\n\n"
        "(1) 标签限制\n"
        "仅使用：<b> <i> <u> <s> <code> <pre> <blockquote> <a>，禁用其他所有标签。\n\n"
        "(2) 分段规则（最高优先级）\n"
        "思维链 / 思考过程中禁止使用 |||；||| 仅用于最终对用户的正文。\n"
        f"可调变量：MAX_CHARS = {max_chars}，MAX_MSG = {max_msg}\n"
        "- 每段约 10～MAX_CHARS 字，总段数 ≤ MAX_MSG\n"
        "- 按语气 / 停顿 / 情绪切分，用 ||| 分隔分段，禁止在句子中间截断\n"
        "- 每段只表达一个小点，允许极短句（嗯 / 好的 / 亲亲）\n\n"
        "(3) 聊天感\n"
        "像真人发消息，不像写文章：可用语气词；可有停顿和转折；避免长段解释。\n\n"
        "(4) 内容过多时\n"
        "优先压缩表达 → 只说重点 → 不突破段数上限。\n\n"
        "(5) 禁止\n"
        "超长整段 / 机械平均切分 / 每段结构完全一致。\n\n"
        "(5.1) 不要用 Markdown 引用语法「行首 >」把很多行都写成引用块，"
        "也不要用整段 <blockquote>；在 Telegram 里会显示成一条条带竖线的引用样式，像公告一样很难看。\n"
        "普通聊天用正常段落即可，需要强调用 <b> 等标签。\n\n"
        "(6) 表情包\n"
        "把表情包当作真人聊天里随手甩图，自然融入对话：配合当前话题、语气与情绪选用，"
        "在调侃、吐槽、安慰、附和、庆祝等时机点缀即可；不必每轮回复都发，一两张放在语气停顿或包袱之后，"
        "比一串无关堆砌更合适；也可以什么表情包都不发；描述要写此刻真正想传达的情绪或梗，避免与正文脱节。\n"
        "如果你想发表情包，在回复里写 [meme:描述]，描述用中文写你想要的情绪或内容，\n"
        "例如：[meme:开心大笑] 或 [meme:无语翻白眼]。\n"
        "[meme:…] 与 ||| 一样都是「顺序分隔符」：机器人会按从左到右依次发出每一段文字和每一条表情包，\n"
        "写在前面的先发。可把表情包插在多段文字之间，例如：第一段||| [meme:开心] 第二段。"
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
    try:
        arousal = float(md.get("arousal") or 0.1)
        arousal = max(0.0, min(1.0, arousal))
    except (TypeError, ValueError):
        arousal = 0.1
    effective_hl = float(hl) * (1.0 + arousal)
    exp_part = math.exp(-math.log(2) / effective_hl * age_days)
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
    
    def build_context(
        self,
        session_id: str,
        user_message: str,
        images: Optional[List[Dict[str, Any]]] = None,
        llm_user_text: Optional[str] = None,
        telegram_segment_hint: bool = False,
    ) -> Dict[str, Any]:
        """
        构建完整的对话上下文。
        
        按照优先级从上到下拼装：
        1. system prompt
        2. temporal_states（is_active=1）
        3. memory_cards
        4. relationship_timeline（条数见库内配置，created_at 正序注入）
        5. daily summary
        6. chunk summary
        7. 向量检索（折叠 + [uid:doc_id]）
        8. 最近消息
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息（与落库 content 一致）
            images: 当前轮次多模态图片（可选）
            llm_user_text: 对话模型用纯文本（有图片时建议传入）
            telegram_segment_hint: 为 True 时在 system 末尾追加 Telegram HTML 白名单与 ||| 分段死指令（仅 Telegram 缓冲路径）
            
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
            cut = (
                llm_user_text
                if images and (llm_user_text is not None and str(llm_user_text).strip())
                else user_message
            )
            current_user_message = self._build_current_user_message(cut, images)
            
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
            if telegram_segment_hint:
                full_system_prompt = full_system_prompt + format_telegram_reply_segment_hint()
            
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
    
    async def build_context_async(
        self,
        session_id: str,
        user_message: str,
        images: Optional[List[Dict[str, Any]]] = None,
        llm_user_text: Optional[str] = None,
        telegram_segment_hint: bool = False,
    ) -> Dict[str, Any]:
        """
        异步构建完整的对话上下文（支持 Reranker）。
        
        按照优先级从上到下拼装：
        1. system prompt
        2. temporal_states（is_active=1）
        3. memory_cards
        4. relationship_timeline（条数见库内配置，created_at 正序注入）
        5. daily summary
        6. chunk summary
        7. 向量检索（折叠 → Cohere 全候选打分 → 语义×0.8+衰减×0.2 → top N，[uid:doc_id]）
        8. 最近消息
        
        使用 asyncio.gather 并行执行向量检索和 BM25 检索，
        合并去重并父子折叠后，对全量候选 await rerank()，再按融合分排序取 top N。
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息
            images: 当前轮次图片 payload（可选）
            llm_user_text: 对话模型用纯文本（可选）
            telegram_segment_hint: 为 True 时在 system 末尾追加 Telegram HTML 白名单与 ||| 分段死指令
            
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
            cut = (
                llm_user_text
                if images and (llm_user_text is not None and str(llm_user_text).strip())
                else user_message
            )
            current_user_message = self._build_current_user_message(cut, images)
            
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
            if telegram_segment_hint:
                full_system_prompt = full_system_prompt + format_telegram_reply_segment_hint()
            
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
        """relationship_timeline：库内取最近若干条（见 _relationship_timeline_limit），拼入前按 created_at 升序排列。"""
        type_labels = {
            "milestone": "里程碑",
            "emotional_shift": "情绪转折",
            "conflict": "冲突",
            "daily_warmth": "日常温情",
        }
        try:
            rows = get_recent_relationship_timeline(
                limit=_relationship_timeline_limit(),
            )
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
        
        查询 summaries 表中 summary_type='daily'，按 created_at 倒序取若干条（见 _context_max_daily_summaries_limit），
        然后在代码中将其翻转为正序（按时间从老到新）。
        
        Returns:
            str: daily summary 部分的文本，如果没有则返回空字符串
        """
        try:
            daily_summaries = get_recent_daily_summaries(
                limit=_context_max_daily_summaries_limit(),
            )
            
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

            tk = _retrieval_top_k()
            vector_results = search_memory(user_message, top_k=tk)
            bm25_results = search_bm25(user_message, top_k=tk)
            all_results = _merge_vector_bm25_dedupe(
                vector_results, bm25_results, max(1, 2 * tk)
            )
            all_results = collapse_longterm_by_parent_id(all_results)
            n_long = _context_max_longterm_count()
            all_results = all_results[:n_long]

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
        语义归一化×0.8 + 时间衰减复活分归一化×0.2 综合排序 → 取 top N（见 _context_max_longterm_count）；
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

            tk = _retrieval_top_k()
            n_long = _context_max_longterm_count()
            logger.debug(f"开始并行检索，查询: '{user_message[:50]}...'")
            loop = asyncio.get_event_loop()
            vector_future = loop.run_in_executor(
                None, partial(search_memory, user_message, tk)
            )
            bm25_future = loop.run_in_executor(
                None, partial(search_bm25, user_message, tk)
            )
            vector_results, bm25_results = await asyncio.gather(vector_future, bm25_future)

            logger.debug(
                f"并行检索完成，向量结果: {len(vector_results)} 条，BM25 结果: {len(bm25_results)} 条"
            )

            all_results = _merge_vector_bm25_dedupe(
                vector_results, bm25_results, max(1, 2 * tk)
            )
            all_results = collapse_longterm_by_parent_id(all_results)

            if not all_results:
                logger.debug("双路检索未找到相关记忆")
                return ""

            logger.debug(f"调用 Reranker（全量候选语义分），文档: {len(all_results)} 条")
            reranked_results = await rerank(
                user_message, all_results, top_n=len(all_results)
            )
            if not reranked_results:
                logger.debug("Reranker 未返回结果，使用折叠后候选前 %s 条", n_long)
                reranked_results = all_results[:n_long]

            fused = fuse_rerank_with_time_decay(reranked_results)
            top_results = fused[:n_long]

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
    
    def _build_recent_messages_section(self, session_id: str) -> List[Dict[str, Any]]:
        """
        构建最近消息部分。
        
        查询当前 session_id 下 is_summarized=0 的消息，按 created_at 倒序取若干条（见 short_term_limit），
        再正序排列后返回。
        
        Args:
            session_id: 会话ID
            
        Returns:
            List[Dict[str, Any]]: 消息列表，每条消息包含 role 和 content（纯文本）
        """
        try:
            recent_messages = get_unsummarized_messages_desc(
                session_id,
                limit=_short_term_recent_message_limit(),
            )
            
            if not recent_messages:
                return []
            
            # 转换为 LLM 接口期望的格式
            messages = []
            for msg in recent_messages:
                role = "user" if msg['role'] == "user" else "assistant"
                text = format_user_message_for_context(
                    {
                        "role": msg["role"],
                        "content": msg["content"],
                        "media_type": msg.get("media_type"),
                        "image_caption": msg.get("image_caption"),
                    }
                )
                messages.append({
                    "role": role,
                    "content": text
                })
            
            logger.debug(f"获取最近消息: session={session_id}, count={len(messages)}")
            return messages
            
        except Exception as e:
            logger.error(f"构建最近消息部分失败: {e}")
            return []
    
    def _build_current_user_message(
        self,
        user_message: str,
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        构建当前用户消息。
        
        Args:
            user_message: 用户当前消息
            images: 多模态图片列表（可选）
            
        Returns:
            Dict[str, Any]: 当前用户消息
        """
        if images:
            from llm.llm_interface import LLMInterface, build_user_multimodal_content

            llm = LLMInterface()
            content = build_user_multimodal_content(
                llm.api_base, llm.model_name, user_message, images
            )
            return {"role": "user", "content": content}
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
        → daily → chunk → 长期记忆检索；末尾注入引用死命令与思维链语言要求。
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
        sections.append(THINKING_LANGUAGE_DIRECTIVE)

        return "\n\n".join(sections)
    
    def _assemble_messages(self, full_system_prompt: str,
                          recent_messages: List[Dict[str, Any]],
                          current_user_message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        组装完整的 messages 数组。
        
        Args:
            full_system_prompt: 完整的 system prompt
            recent_messages: 最近消息列表
            current_user_message: 当前用户消息
            
        Returns:
            List[Dict[str, Any]]: 完整的 messages 数组
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
def build_context(
    session_id: str,
    user_message: str,
    images: Optional[List[Dict[str, Any]]] = None,
    llm_user_text: Optional[str] = None,
    telegram_segment_hint: bool = False,
) -> Dict[str, Any]:
    """
    构建对话上下文的便捷函数。
    
    Args:
        session_id: 会话ID
        user_message: 用户当前消息
        images: 当前轮图片 payload（可选）
        llm_user_text: 对话模型用纯文本（可选，有图片时建议传入）
        telegram_segment_hint: 为 True 时追加 Telegram HTML 白名单与 ||| 分段死指令（仅 Telegram 缓冲路径建议开启）
        
    Returns:
        Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
    """
    builder = ContextBuilder()
    return builder.build_context(
        session_id,
        user_message,
        images=images,
        llm_user_text=llm_user_text,
        telegram_segment_hint=telegram_segment_hint,
    )


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
