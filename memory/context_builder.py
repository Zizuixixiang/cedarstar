"""
Context 构建模块。

负责组装发送给 LLM 的完整 prompt，按照优先级从上到下拼装：
1. system prompt：从配置读取，保持原样
2. temporal_states：is_active=1 的全部记录（在记忆卡片之前）
3. memory_cards：查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化后拼入
4. relationship_timeline：条数见 `relationship_timeline_limit`（库内选取），注入 Context 时按 created_at 正序排列
5. 向量检索（长期记忆）：各路 `retrieval_top_k` 条，去重合并，经精排、MMR 多样性筛选后注入 `context_max_longterm` 条
6. daily summary：`context_max_daily_summaries`（优先）或环境变量决定条数，倒序取后翻为正序拼入
7. chunk summary：查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选），附带其来源标识，按时间正序拼入
8. 最近消息：`short_term_limit`（优先）或环境变量决定条数，再正序排列后拼入

组装完成后返回一个结构，包含 system prompt 和 messages 数组，直接可以传给 LLM API。
"""

import logging
import math
import re
import time
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from datetime import datetime

from config import config
from tools.lutopia import strip_lutopia_behavior_appendix
from memory.retrieval import (
    chroma_where_longterm_summary_types,
    longterm_allowed_summary_types,
)
from memory.database import (
    get_all_active_memory_cards,
    get_all_active_temporal_states,
    get_database,
    get_recent_relationship_timeline,
    get_recent_daily_summaries,
    get_recent_tool_executions,
    get_today_chunk_summaries,
    get_unsummarized_messages_desc,
)

# 导入向量存储函数
try:
    from .vector_store import search_memory, get_embeddings_by_doc_ids
except ImportError:
    from memory.vector_store import search_memory, get_embeddings_by_doc_ids

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

# OpenAI 兼容路径下启用 Lutopia tools 时注入 system 末尾（与 tools 是否传入由调用方 flag 对齐）
TOOL_ORAL_COACHING_BLOCK = (
    "调用工具前，用一句简短口语告诉用户你要去做什么，"
    "语气自然随意，比如「我去看看xxxx」。"
    "工具结果回来后，接着用正常语气继续说。"
    "不要罗列工具名称，不要说技术性的话。"
)

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


def format_user_context_sent_at_line(created_at: Optional[Any] = None) -> str:
    """
    用户消息发往 LLM 时附带的单行时间（东八区），仅写入上下文，不落库。
    ``created_at`` 为 None 时表示「当前时刻」（用于本轮尚未入库的用户输入）。
    """
    from datetime import datetime, timezone, timedelta

    tz_sh = timezone(timedelta(hours=8))
    dt: Optional[datetime] = None
    if created_at is not None:
        try:
            if isinstance(created_at, datetime):
                d = created_at
            else:
                s = str(created_at).strip()
                if not s:
                    raise ValueError("empty created_at")
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            dt = d.astimezone(tz_sh)
        except Exception:
            dt = None
    if dt is None:
        dt = datetime.now(tz_sh)
    # 时、分之间用全角冒号，与「当前系统时间」块区分表述为「当前时间」
    line = (
        f"{dt.year}年{dt.month}月{dt.day}日 "
        f"{dt.hour:02d}：{dt.minute:02d}"
    )
    return f"【当前时间：{line}】"


def inject_user_sent_at_into_llm_content(
    content: Union[str, List[Dict[str, Any]]],
    created_at: Optional[Any] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """在 user 消息的 content 首行注入 ``format_user_context_sent_at_line``（多模态则写入首个 text 段）。"""
    label = format_user_context_sent_at_line(created_at)
    prefix = label + "\n"
    if isinstance(content, str):
        return prefix + content if (content or "").strip() else label
    if isinstance(content, list):
        out: List[Dict[str, Any]] = []
        injected = False
        for part in content:
            if (
                not injected
                and isinstance(part, dict)
                and part.get("type") == "text"
            ):
                t = part.get("text") or ""
                np = dict(part)
                np["text"] = prefix + t if t.strip() else label
                out.append(np)
                injected = True
            else:
                out.append(part)
        if not injected:
            out.insert(0, {"type": "text", "text": label})
        return out
    return content


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


async def _short_term_recent_message_limit() -> int:
    """最近原文条数：优先 config 表 short_term_limit，否则环境变量 CONTEXT_MAX_RECENT_MESSAGES。"""
    try:
        raw = await get_database().get_config("short_term_limit")
        if raw is not None and str(raw).strip() != "":
            return max(1, int(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 short_term_limit 失败，使用环境变量: %s", e)
    return config.CONTEXT_MAX_RECENT_MESSAGES


async def _context_max_daily_summaries_limit() -> int:
    """每日小传注入条数：优先 config 表 context_max_daily_summaries，否则环境变量 CONTEXT_MAX_DAILY_SUMMARIES。"""
    try:
        raw = await get_database().get_config("context_max_daily_summaries")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(100, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 context_max_daily_summaries 失败，使用环境变量: %s", e)
    return max(1, min(100, config.CONTEXT_MAX_DAILY_SUMMARIES))


async def _context_max_chunk_summaries_limit() -> int:
    try:
        raw = await get_database().get_config("context_max_chunk_summaries")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(100, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("璇诲彇 context_max_chunk_summaries 澶辫触锛屼娇鐢ㄧ幆澧冨彉閲? %s", e)
    return max(1, min(100, config.CONTEXT_MAX_CHUNK_SUMMARIES))


async def _context_max_longterm_count() -> int:
    """长期记忆注入 Top N：优先 config 表 context_max_longterm，否则默认 3。"""
    try:
        raw = await get_database().get_config("context_max_longterm")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(20, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 context_max_longterm 失败，使用默认 3: %s", e)
    return 3


async def _relationship_timeline_limit() -> int:
    """关系时间线条数：优先 config 表 relationship_timeline_limit，否则默认 3。"""
    try:
        raw = await get_database().get_config("relationship_timeline_limit")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(50, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 relationship_timeline_limit 失败，使用默认 3: %s", e)
    return 3


async def _retrieval_top_k() -> int:
    """双路检索各路 top_k：优先 config 表 retrieval_top_k，否则默认 5。"""
    try:
        raw = await get_database().get_config("retrieval_top_k")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(30, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 retrieval_top_k 失败，使用默认 5: %s", e)
    return 5


async def _mmr_lambda_value() -> float:
    """MMR 相关性权重：优先 config 表 mmr_lambda，否则默认 0.75。"""
    try:
        raw = await get_database().get_config("mmr_lambda")
        if raw is not None and str(raw).strip() != "":
            return max(0.5, min(1.0, float(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 mmr_lambda 失败，使用默认 0.75: %s", e)
    return 0.75


MEMORY_CITATION_DIRECTIVE = (
    "注入的历史记忆块格式为 [uid:xxx]，其中 xxx 即为你引用时需填入的标识。"
    "若生成回复时参考了上述历史记忆，必须在文本末尾标注引用，格式为 [[used:uid]]（半角方括号、双括号），可同时标注多个。"
    "禁止使用单括号 [used:…]、中文书名号【used:…】等错误格式，否则系统无法正确识别。"
)

# 主对话 system 末尾：引用指令之前的优先级说明（与 MEMORY_CITATION_DIRECTIVE / THINKING_LANGUAGE_DIRECTIVE 用 \\n\\n 分隔）
MEMORY_BLOCK_PRIORITY_DIRECTIVE = (
    "近期消息 > chunk碎片摘要 > 时效状态 > 记忆卡片 = 关系时间线 > 每日小传 > 长期记忆\n"
    "同类型块内以日期更近的条目为准\n"
    "时效状态的 action_rule 与其他块通常不直接冲突，上述优先级主要作用于状态描述层面；若近期消息明确提及某时效状态描述已发生变化，以近期消息为准"
)

THINKING_LANGUAGE_DIRECTIVE = (
    "所有思维链、推理过程必须使用中文；思考时统一称呼我为南杉，严禁出现用户、user 字样。"
)

ANTHROPIC_CACHE_CONTROL_1H = {
    "type": "ephemeral",
    "ttl": "1h",
}


def _cache_text_block(text: str, *, cache: bool = False) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = dict(ANTHROPIC_CACHE_CONTROL_1H)
    return block


def flatten_text_content(content: Any) -> str:
    """将 Anthropic text blocks / 普通字符串统一压成 OpenAI 兼容字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "\n\n".join(p for p in parts if p.strip())
    return str(content) if content is not None else ""

async def _telegram_segment_limits_from_db() -> Tuple[int, int]:
    """读取 config 表中的 Telegram 分段参数（与 api.config DEFAULT 及校验范围一致）。"""
    db = get_database()

    async def _int_key(name: str, default: int, lo: int, hi: int) -> int:
        raw = await db.get_config(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            v = int(str(raw).strip())
        except ValueError:
            return default
        return max(lo, min(hi, v))

    max_chars = await _int_key("telegram_max_chars", 50, 10, 1000)
    max_chars = max(10, min(1000, round(max_chars / 10) * 10))
    max_msg = await _int_key("telegram_max_msg", 8, 1, 20)
    return max_chars, max_msg


async def format_telegram_reply_segment_hint() -> str:
    """Telegram 缓冲回复：追加于 system 末尾；MAX_CHARS / MAX_MSG 来自数据库。"""
    max_chars, max_msg = await _telegram_segment_limits_from_db()
    return (
        "\n\n"
        "【Telegram 排版】\n\n"
        "(1) 标签：仅用 <b> <i> <u> <s> <code> <pre> <blockquote> <a>，标签规范闭合，禁用其余格式。\n\n"
        "(2) 分段：像真人发消息，每条气泡之间用空行隔开（即两次换行）。\n"
        f"想强制分割时用 |||；总段数 ≤ {max_msg}，每段 ≤ {max_chars} 字。\n"
        "语义完整优先，禁止超长整段 / 句子中间截断 / 机械平均切分。\n\n"
        "(3) 表情包：情绪浓度高时写 [meme:中文描述]，自然插入，不必每轮都发。\n"
        "[meme:…] 与 ||| 都是顺序分隔符，从左到右依次发出。\n\n"
        "避免 <blockquote> 包大段聊天内容（思维链的 blockquote 系统自动处理，与此无关）。\n"
        "禁用行首 > 引用语法。"
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


def _coerce_embedding_vector(value: Any) -> Optional[List[float]]:
    """把 Chroma 返回的 list / tuple / ndarray embedding 规范成 float list。"""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return [float(x) for x in value]
    except (TypeError, ValueError):
        return None


def _cosine_similarity(a: Any, b: Any) -> float:
    va = _coerce_embedding_vector(a)
    vb = _coerce_embedding_vector(b)
    if not va or not vb:
        return 0.0
    n = min(len(va), len(vb))
    if n <= 0:
        return 0.0
    dot = sum(va[i] * vb[i] for i in range(n))
    na = math.sqrt(sum(va[i] * va[i] for i in range(n)))
    nb = math.sqrt(sum(vb[i] * vb[i] for i in range(n)))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _hydrate_candidate_embeddings(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为 BM25 等缺失向量的候选按 doc_id 补取 Chroma 已存 embedding。"""
    if not candidates:
        return []
    missing_ids = [
        str(c.get("id"))
        for c in candidates
        if c.get("id") is not None and _coerce_embedding_vector(c.get("embedding")) is None
    ]
    emb_map = get_embeddings_by_doc_ids(missing_ids) if missing_ids else {}
    hydrated: List[Dict[str, Any]] = []
    for c in candidates:
        cp = dict(c)
        doc_id = str(cp.get("id")) if cp.get("id") is not None else ""
        if _coerce_embedding_vector(cp.get("embedding")) is None and doc_id in emb_map:
            cp["embedding"] = emb_map[doc_id]
        hydrated.append(cp)
    return hydrated


def apply_mmr(
    candidates: List[Dict[str, Any]],
    lambda_param: float,
    top_n: int,
) -> List[Dict[str, Any]]:
    """
    Maximal Marginal Relevance：在融合得分后加入多样性惩罚。

    candidates 已按 fusion_score 降序；第一轮直接选择最高分，其后按
    λ × normalized(fusion_score) - (1-λ) × max(cos_sim(d, selected)) 选择。
    """
    if not candidates:
        return []
    top_n = max(1, int(top_n))
    if len(candidates) <= top_n:
        return list(candidates)

    lambda_param = max(0.5, min(1.0, float(lambda_param)))
    max_score = max(float(c.get("fusion_score", 0.0) or 0.0) for c in candidates)
    denom = max_score if max_score > 0.0 else 1.0

    selected: List[Dict[str, Any]] = [candidates[0]]
    remaining: List[Dict[str, Any]] = list(candidates[1:])

    while remaining and len(selected) < top_n:
        best_idx = 0
        best_score = float("-inf")
        for idx, cand in enumerate(remaining):
            relevance = float(cand.get("fusion_score", 0.0) or 0.0) / denom
            diversity_penalty = max(
                _cosine_similarity(cand.get("embedding"), item.get("embedding"))
                for item in selected
            )
            mmr_score = lambda_param * relevance - (1.0 - lambda_param) * diversity_penalty
            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = idx
        selected.append(remaining.pop(best_idx))

    return selected


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
    """用于衰减复活分：优先以 last_access_ts 计龄；仅当缺失或无法解析时用 created_at 兜底。"""
    md = metadata or {}
    last_raw = md.get("last_access_ts")
    if last_raw is not None and str(last_raw).strip() != "":
        try:
            lt = float(last_raw)
            return max(0.0, (now_ts - lt) / 86400.0)
        except (TypeError, ValueError):
            pass
    created = md.get("created_at")
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            return max(0.0, (now_ts - dt.timestamp()) / 86400.0)
        except (TypeError, ValueError):
            pass
    return 0.0


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


def _persona_field_str(row: Mapping[str, Any], key: str) -> str:
    v = row.get(key)
    return (v or "").strip() if v is not None else ""


def build_char_persona_prompt_sections(row: Mapping[str, Any]) -> List[str]:
    """Char 段：与 miniapp Persona.jsx buildPreview 一致。"""
    sections: List[str] = []
    cn = _persona_field_str(row, "char_name")
    ci = _persona_field_str(row, "char_identity")
    ca = _persona_field_str(row, "char_appearance")
    exist_lines: List[str] = []
    if cn:
        exist_lines.append(f"你的名字是 {cn}。")
    if ci:
        exist_lines.append(ci)
    if ca:
        exist_lines.append(ca)
    if exist_lines:
        sections.append("【存在定义】\n" + "\n".join(exist_lines))

    cpers = _persona_field_str(row, "char_personality")
    if cpers:
        sections.append("【内在人格】\n" + cpers)

    contract_parts: List[str] = []
    cs = _persona_field_str(row, "char_speech_style")
    cr = _persona_field_str(row, "char_redlines")
    if cs:
        contract_parts.append("说话风格与格式硬规范：\n" + cs)
    if cr:
        contract_parts.append("行为红线与绝对禁忌：\n" + cr)
    if contract_parts:
        sections.append("【表达契约】\n" + "\n\n".join(contract_parts))

    crels = _persona_field_str(row, "char_relationships")
    if crels:
        sections.append("【关系与形象】\n" + crels)

    cnsfw = _persona_field_str(row, "char_nsfw")
    if cnsfw:
        sections.append("【成人内容】\n" + cnsfw)

    tools_parts: List[str] = []
    ctg = _persona_field_str(row, "char_tools_guide")
    com = _persona_field_str(row, "char_offline_mode")
    if ctg:
        tools_parts.append("工具使用守则：\n" + ctg)
    if com:
        tools_parts.append("线下模式：\n" + com)
    if tools_parts:
        sections.append("【工具与场景】\n" + "\n\n".join(tools_parts))

    return sections


def build_persona_config_system_body(row: Mapping[str, Any]) -> str:
    """persona_configs 一行 → 基础 system 文本（系统规则 + Char + User），供预览与 _build_system_prompt。"""
    parts: List[str] = []

    def _s(key: str) -> str:
        return _persona_field_str(row, key)

    if _s("system_rules"):
        parts.append(f"【系统规则】\n{_s('system_rules')}")

    parts.extend(build_char_persona_prompt_sections(row))

    user_lines: List[str] = []
    if _s("user_name"):
        user_lines.append(f"姓名：{_s('user_name')}")
    if _s("user_body"):
        user_lines.append(f"身体特征：{_s('user_body')}")
    if _s("user_work"):
        user_lines.append(f"工作：{_s('user_work')}")
    if _s("user_habits"):
        user_lines.append(f"生活习惯：{_s('user_habits')}")
    if _s("user_likes_dislikes"):
        user_lines.append(f"喜恶：{_s('user_likes_dislikes')}")
    if _s("user_values"):
        user_lines.append(f"价值观与世界观：{_s('user_values')}")
    if _s("user_hobbies"):
        user_lines.append(f"兴趣娱乐：{_s('user_hobbies')}")
    if _s("user_taboos"):
        user_lines.append(f"禁忌：{_s('user_taboos')}")
    if _s("user_nsfw"):
        user_lines.append(f"NSFW 偏好：{_s('user_nsfw')}")
    if _s("user_other"):
        user_lines.append(f"其他：{_s('user_other')}")

    if user_lines:
        parts.append("【User 的人设】\n" + "\n".join(user_lines))

    return "\n\n".join(parts).strip()


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
    
    async def build_context(
        self,
        session_id: str,
        user_message: str,
        images: Optional[List[Dict[str, Any]]] = None,
        llm_user_text: Optional[str] = None,
        telegram_segment_hint: bool = False,
        tool_oral_coaching: bool = False,
        exclude_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        构建完整的对话上下文。
        
        按照优先级从上到下拼装：
        1. system prompt
        2. temporal_states（is_active=1）
        3. memory_cards
        4. relationship_timeline（条数见库内配置，created_at 正序注入）
        5. 向量检索（融合打分 + MMR + [uid:doc_id]）
        6. daily summary
        7. chunk summary
        8. 最近消息
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息（与落库 content 一致）
            images: 当前轮次多模态图片（可选）
            llm_user_text: 对话模型用纯文本（有图片时建议传入）
            telegram_segment_hint: 为 True 时在 system 末尾追加 Telegram HTML 白名单与 ||| 分段死指令（仅 Telegram 缓冲路径）
            tool_oral_coaching: 为 True 时在 system 末尾追加 Lutopia 工具「口播」引导（与启用 tools 的请求对齐）
            
        Returns:
            Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
        """
        try:
            # 1. 获取 system prompt
            system_prompt = await self._build_system_prompt()

            temporal_section = await self._build_temporal_states_section()
            
            # 2. 获取 memory cards
            memory_cards_section = await self._build_memory_cards_section()

            relationship_timeline_section = await self._build_relationship_timeline_section()
            
            # 3. 长期记忆（向量）；4. daily；5. chunk（与 _assemble_full_system_prompt 拼接顺序一致）
            vector_search_section = await self._build_vector_search_section(user_message)
            daily_summaries_section = await self._build_daily_summaries_section(session_id)
            chunk_summaries_section = await self._build_chunk_summaries_section()
            recent_tool_section = await self._build_recent_tool_executions_section(session_id)
            logger.info(
                "context chunk section preview: session=%s chunk_section_len=%s tail=%r",
                session_id,
                len(chunk_summaries_section or ""),
                (chunk_summaries_section or "")[-500:],
            )
            
            # 6. 获取最近消息
            recent_messages_section = await self._build_recent_messages_section(session_id, exclude_message_id)
            
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
                vector_search_section,
                daily_summaries_section,
                chunk_summaries_section,
                recent_tool_section,
                tool_oral_coaching=tool_oral_coaching,
            )
            if telegram_segment_hint:
                full_system_prompt.append(
                    _cache_text_block(await format_telegram_reply_segment_hint(), cache=False)
                )
            
            # 组装 messages 数组
            messages = self._assemble_messages(
                full_system_prompt,
                recent_messages_section,
                current_user_message
            )
            
            logger.debug(f"Context 构建完成: session={session_id}, system_prompt_length={len(full_system_prompt)}, messages_count={len(messages)}")
            logger.info(
                "context built: session=%s system_prompt_length=%s messages_count=%s daily_section_len=%s chunk_section_len=%s recent_messages=%s",
                session_id,
                len(full_system_prompt),
                len(messages),
                len(daily_summaries_section or ""),
                len(chunk_summaries_section or ""),
                len(recent_messages_section or []),
            )
            
            return {
                "system_prompt": full_system_prompt,
                "messages": messages
            }
            
        except Exception as e:
            logger.warning(f"构建 context 失败: {e}")  # 可恢复/已兜底，降为 warning
            # 返回最小化的 context
            return {
                "system_prompt": config.SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": inject_user_sent_at_into_llm_content(
                            user_message, None
                        ),
                    }
                ],
            }
    
    async def build_context_async(
        self,
        session_id: str,
        user_message: str,
        images: Optional[List[Dict[str, Any]]] = None,
        llm_user_text: Optional[str] = None,
        telegram_segment_hint: bool = False,
        tool_oral_coaching: bool = False,
        exclude_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        异步构建完整的对话上下文（支持 Reranker）。
        
        按照优先级从上到下拼装：
        1. system prompt
        2. temporal_states（is_active=1）
        3. memory_cards
        4. relationship_timeline（条数见库内配置，created_at 正序注入）
        5. 向量检索（Cohere 全候选打分 → 语义×0.8+衰减×0.2 → MMR → top N，[uid:doc_id]）
        6. daily summary
        7. chunk summary
        8. 最近消息
        
        使用 asyncio.gather 并行执行向量检索和 BM25 检索，
        合并去重后，对全量候选 await rerank()，再按融合分排序并经 MMR 取 top N。
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息
            images: 当前轮次图片 payload（可选）
            llm_user_text: 对话模型用纯文本（可选）
            telegram_segment_hint: 为 True 时在 system 末尾追加 Telegram HTML 白名单与 ||| 分段死指令
            tool_oral_coaching: 为 True 时在 system 末尾追加 Lutopia 工具「口播」引导
            
        Returns:
            Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
        """
        try:
            # 1. 获取 system prompt
            system_prompt = await self._build_system_prompt()

            temporal_section = await self._build_temporal_states_section()
            
            # 2. 获取 memory cards
            memory_cards_section = await self._build_memory_cards_section()

            relationship_timeline_section = await self._build_relationship_timeline_section()
            
            # 3. 长期记忆（向量）；4. daily；5. chunk（与 _assemble_full_system_prompt 拼接顺序一致）
            vector_search_section = await self._build_vector_search_section_async(user_message)
            daily_summaries_section = await self._build_daily_summaries_section(session_id)
            chunk_summaries_section = await self._build_chunk_summaries_section()
            recent_tool_section = await self._build_recent_tool_executions_section(session_id)
            logger.info(
                "context chunk section preview async: session=%s chunk_section_len=%s tail=%r",
                session_id,
                len(chunk_summaries_section or ""),
                (chunk_summaries_section or "")[-500:],
            )
            
            # 6. 获取最近消息
            recent_messages_section = await self._build_recent_messages_section(session_id, exclude_message_id)
            
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
                vector_search_section,
                daily_summaries_section,
                chunk_summaries_section,
                recent_tool_section,
                tool_oral_coaching=tool_oral_coaching,
            )
            if telegram_segment_hint:
                full_system_prompt.append(
                    _cache_text_block(await format_telegram_reply_segment_hint(), cache=False)
                )
            
            # 组装 messages 数组
            messages = self._assemble_messages(
                full_system_prompt,
                recent_messages_section,
                current_user_message
            )
            
            logger.debug(f"Context 构建完成（异步）: session={session_id}, system_prompt_length={len(full_system_prompt)}, messages_count={len(messages)}")
            logger.info(
                "context built async: session=%s system_prompt_length=%s messages_count=%s daily_section_len=%s chunk_section_len=%s recent_messages=%s",
                session_id,
                len(full_system_prompt),
                len(messages),
                len(daily_summaries_section or ""),
                len(chunk_summaries_section or ""),
                len(recent_messages_section or []),
            )
            
            return {
                "system_prompt": full_system_prompt,
                "messages": messages
            }
            
        except Exception as e:
            logger.warning(f"构建 context 失败（异步）: {e}")  # 可恢复/已兜底，降为 warning
            # 返回最小化的 context
            return {
                "system_prompt": config.SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": inject_user_sent_at_into_llm_content(
                            user_message, None
                        ),
                    }
                ],
            }
    
    async def _build_system_prompt(self) -> str:
        """
        从激活的 persona_configs 行组装 system prompt。
        无法读取时回退到 config.SYSTEM_PROMPT。
        """
        try:
            db = get_database()
            # 读激活的 chat api_config，拿 persona_id
            active = await db.get_active_api_config('chat')
            persona_id = active.get('persona_id') if active else None
            if not persona_id:
                return config.SYSTEM_PROMPT

            row = await db.pool.fetchrow(
                'SELECT * FROM persona_configs WHERE id = $1', int(persona_id)
            )
            if not row:
                return config.SYSTEM_PROMPT

            assembled = build_persona_config_system_body(dict(row))
            return assembled if assembled else config.SYSTEM_PROMPT

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"读取 persona 失败，回退 SYSTEM_PROMPT: {e}")
            return config.SYSTEM_PROMPT

    async def _build_temporal_states_section(self) -> str:
        """temporal_states 中 is_active=1 的全部记录（置于记忆卡片之前）。"""
        try:
            rows = await get_all_active_temporal_states()
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
            logger.warning(f"构建 temporal_states 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            return ""

    async def _build_relationship_timeline_section(self) -> str:
        """relationship_timeline：库内取最近若干条（见 _relationship_timeline_limit），拼入前按 created_at 升序排列。"""
        type_labels = {
            "milestone": "里程碑",
            "emotional_shift": "情绪转折",
            "conflict": "冲突",
            "daily_warmth": "日常温情",
        }
        try:
            rows = await get_recent_relationship_timeline(
                limit=await _relationship_timeline_limit(),
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
            logger.warning(f"构建 relationship_timeline 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            return ""
    
    async def _build_memory_cards_section(self) -> str:
        """
        构建 memory cards 部分。
        
        查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化。
        
        Returns:
            str: memory cards 部分的文本，如果没有则返回空字符串
        """
        try:
            memory_cards = await get_all_active_memory_cards(limit=100)
            
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
            logger.warning(f"构建 memory cards 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            return ""
    
    async def _build_daily_summaries_section(self, session_id: Optional[str] = None) -> str:
        """
        构建 daily summary 部分。
        
        查询 summaries 表中 summary_type='daily'，按 created_at 倒序取若干条（见 _context_max_daily_summaries_limit），
        然后在代码中将其翻转为正序（按时间从老到新）。
        
        Returns:
            str: daily summary 部分的文本，如果没有则返回空字符串
        """
        try:
            daily_summaries = await get_recent_daily_summaries(
                limit=await _context_max_daily_summaries_limit(),
                session_id=session_id,
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
            logger.warning(f"构建 daily summary 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            return ""
    
    async def _build_chunk_summaries_section(self) -> str:
        """
        构建 chunk summary 部分。
        
        查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选）。
        在拼入时，附带其来源标识（格式如 [来自频道 {session_id}]: 摘要内容），按时间正序拼入。
        
        Returns:
            str: chunk summary 部分的文本，如果没有则返回空字符串
        """
        try:
            chunk_summaries = await get_today_chunk_summaries()
            
            if not chunk_summaries:
                return ""

            total_chunk_summaries = len(chunk_summaries)
            chunk_limit = await _context_max_chunk_summaries_limit()
            if total_chunk_summaries > chunk_limit:
                # 优先保留最新 chunk，避免较早摘要把尾部的新摘要挤出模型关注范围。
                chunk_summaries = chunk_summaries[-chunk_limit:]
                logger.debug(
                    "chunk 摘要注入已截断: total=%s, keep_latest=%s",
                    total_chunk_summaries,
                    len(chunk_summaries),
                )
            
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
            logger.warning(f"构建 chunk summary 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            return ""

    async def _build_recent_tool_executions_section(self, session_id: str) -> str:
        """最近工具使用记录：动态信息，放在缓存断点之后。"""
        try:
            rows = await get_recent_tool_executions(
                session_id,
                limit_turns=5,
                max_rows=24,
            )
            if not rows:
                return ""
            lines = ["# 最近工具使用记录", ""]
            lines.append(
                "以下是最近几轮对话中已经执行过的工具结果摘要；它们用于保持连续性，不表示本轮刚刚调用。"
                "如果用户追问刚才查看的帖子、评论、私信或搜索结果，优先使用本节内容回答。"
            )
            current_turn = None
            for row in rows:
                turn_id = str(row.get("turn_id") or "")
                if turn_id != current_turn:
                    current_turn = turn_id
                    created = row.get("created_at") or ""
                    lines.append("")
                    lines.append(f"## 工具回合 {turn_id[:8] or 'unknown'}（{created}）")
                nm = row.get("tool_name") or "tool"
                args = row.get("arguments_json") or {}
                if isinstance(args, dict):
                    arg_bits = []
                    for k, v in list(args.items())[:3]:
                        if k.startswith("_"):
                            continue
                        sv = str(v).replace("\n", " ").strip()
                        if sv:
                            arg_bits.append(f"{k}={sv[:80]}")
                    arg_text = "；".join(arg_bits)
                else:
                    arg_text = str(args).replace("\n", " ").strip()[:160]
                summary = (row.get("result_summary") or "").strip()
                if len(summary) > 1800:
                    summary = summary[:1800] + "..."
                prefix = f"- {nm}"
                if arg_text:
                    prefix += f"（{arg_text}）"
                lines.append(f"{prefix}：{summary or '已执行，但没有可用摘要'}")
            return "\n".join(lines).strip()
        except Exception as e:
            logger.warning("构建最近工具使用记录失败: %s", e)
            return ""
    
    async def _build_vector_search_section(self, user_message: str) -> str:
        """
        构建向量检索部分（同步，无 Cohere 精排）。
        
        双路融合后经时间衰减融合与 MMR 多样性筛选，注入时每条正文前带 [uid:doc_id]。
        """
        try:
            if not config.ZHIPU_API_KEY or config.ZHIPU_API_KEY == "your_zhipu_api_key_here":
                logger.warning("ZHIPU_API_KEY 未设置或为默认值，跳过向量检索")
                return ""

            tk = await _retrieval_top_k()
            lt_where = chroma_where_longterm_summary_types(user_message)
            lt_types = longterm_allowed_summary_types(user_message)
            vector_results = search_memory(
                user_message, top_k=tk, where=lt_where
            )
            bm25_results = search_bm25(
                user_message,
                top_k=tk,
                allowed_summary_types=lt_types,
            )
            all_results = _merge_vector_bm25_dedupe(
                vector_results, bm25_results, max(1, 2 * tk)
            )
            n_long = await _context_max_longterm_count()
            fused = fuse_rerank_with_time_decay(all_results)
            fused = _hydrate_candidate_embeddings(fused)
            all_results = apply_mmr(fused, await _mmr_lambda_value(), n_long)

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
            vector_section += "\n\n<!-- 以上是双路检索结果（融合时间衰减并经 MMR 多样性筛选）；异步路径下由 Reranker 提供语义分 -->"
            return f"# 相关长期记忆（双路检索结果）\n\n{vector_section}"

        except Exception as e:
            logger.warning(f"构建向量检索部分失败: {e}")  # 可恢复/已兜底，降为 warning
            return ""
    
    async def _build_vector_search_section_async(self, user_message: str) -> str:
        """
        异步构建向量检索部分：并行双路检索 → Cohere 打分 →
        语义归一化×0.8 + 时间衰减复活分归一化×0.2 综合排序 → MMR → 取 top N（见 _context_max_longterm_count）；
        每条正文前带 [uid:doc_id]。
        """
        try:
            if not config.ZHIPU_API_KEY or config.ZHIPU_API_KEY == "your_zhipu_api_key_here":
                logger.warning("ZHIPU_API_KEY 未设置或为默认值，跳过向量检索")
                return ""

            if not config.COHERE_API_KEY or config.COHERE_API_KEY == "your_cohere_api_key_here":
                logger.warning("COHERE_API_KEY 未设置或为默认值，使用普通双路检索")
                return await self._build_vector_search_section(user_message)

            import asyncio

            tk = await _retrieval_top_k()
            n_long = await _context_max_longterm_count()
            logger.debug(f"开始并行检索，查询: '{user_message[:50]}...'")
            loop = asyncio.get_event_loop()
            lt_where = chroma_where_longterm_summary_types(user_message)
            lt_types = longterm_allowed_summary_types(user_message)
            vector_future = loop.run_in_executor(
                None, partial(search_memory, user_message, tk, lt_where)
            )
            bm25_future = loop.run_in_executor(
                None,
                partial(
                    search_bm25,
                    user_message,
                    tk,
                    lt_types,
                ),
            )
            vector_results, bm25_results = await asyncio.gather(vector_future, bm25_future)

            logger.debug(
                f"并行检索完成，向量结果: {len(vector_results)} 条，BM25 结果: {len(bm25_results)} 条"
            )

            all_results = _merge_vector_bm25_dedupe(
                vector_results, bm25_results, max(1, 2 * tk)
            )

            if not all_results:
                logger.debug("双路检索未找到相关记忆")
                return ""

            logger.debug(f"调用 Reranker（全量候选语义分），文档: {len(all_results)} 条")
            reranked_results = await rerank(
                user_message, all_results, top_n=len(all_results)
            )
            if not reranked_results:
                logger.debug("Reranker 未返回结果，使用双路候选进入融合与 MMR")
                reranked_results = all_results

            fused = fuse_rerank_with_time_decay(reranked_results)
            fused = _hydrate_candidate_embeddings(fused)
            top_results = apply_mmr(fused, await _mmr_lambda_value(), n_long)

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
                f"\n\n<!-- 精排：语义×0.8+时间衰减×0.2 后经 MMR，自 {len(all_results)} 条候选取 {len(top_results)} 条 -->"
            )
            return f"# 相关长期记忆（精排结果）\n\n{vector_section}"

        except Exception as e:
            logger.warning(f"构建向量检索部分失败（异步）: {e}")  # 可恢复/已兜底，降为 warning
            logger.warning("异步检索失败，回退到同步检索")
            return await self._build_vector_search_section(user_message)
    
    async def _build_recent_messages_section(self, session_id: str, exclude_message_id: Optional[int] = None) -> List[Dict[str, Any]]:
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
            recent_messages = await get_unsummarized_messages_desc(
                session_id,
                limit=await _short_term_recent_message_limit(),
            )
            
            if not recent_messages:
                return []
            
            # 转换为 LLM 接口期望的格式
            messages = []
            for msg in recent_messages:
                if exclude_message_id and msg.get("id") == exclude_message_id:
                    continue
                msg_role = msg.get("role")
                role = "user" if msg_role == "user" else "assistant"
                text = format_user_message_for_context(
                    {
                        "role": msg["role"],
                        "content": msg["content"],
                        "media_type": msg.get("media_type"),
                        "image_caption": msg.get("image_caption"),
                    }
                )
                if msg_role == "assistant_other":
                    other_name = (msg.get("character_id") or "未知助手")
                    text = strip_lutopia_behavior_appendix(text)
                    text = f"[另一名助手 {other_name} 的发言]：{text}"
                elif role == "assistant":
                    text = strip_lutopia_behavior_appendix(text)
                else:
                    text = inject_user_sent_at_into_llm_content(
                        text, msg.get("created_at")
                    )
                messages.append({
                    "role": role,
                    "content": text
                })
            
            logger.debug(f"获取最近消息: session={session_id}, count={len(messages)}")
            return messages
            
        except Exception as e:
            logger.warning(f"构建最近消息部分失败: {e}")  # 可恢复/已兜底，降为 warning
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
            content = inject_user_sent_at_into_llm_content(content, None)
            return {"role": "user", "content": content}
        return {
            "role": "user",
            "content": inject_user_sent_at_into_llm_content(user_message, None),
        }
    
    def _assemble_full_system_prompt(
        self,
        system_prompt: str,
        temporal_states_section: str,
        memory_cards_section: str,
        relationship_timeline_section: str,
        vector_search_section: str,
        daily_summaries_section: str,
        chunk_summaries_section: str,
        recent_tool_section: str,
        *,
        tool_oral_coaching: bool = False,
        exclude_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        组装完整的 system prompt。

        Anthropic 路径使用 text blocks + 1h cache_control；OpenAI 路径在 LLM 层压回字符串。
        易变的当前时间、长期检索和工具记录放在缓存块之后，避免破坏稳定前缀。
        """
        from datetime import datetime, timezone, timedelta
        tz_utc_8 = timezone(timedelta(hours=8))
        now_str = datetime.now(tz_utc_8).strftime("%Y年%m月%d日 %H:%M")
        time_section = f"【当前系统时间（东八区）：{now_str}】\n(提示：在对话中若关注时间信息，请以此时间为基准！)"

        fixed_sections = [system_prompt, MEMORY_BLOCK_PRIORITY_DIRECTIVE]
        fixed_sections.append(MEMORY_CITATION_DIRECTIVE)
        fixed_sections.append(THINKING_LANGUAGE_DIRECTIVE)
        if tool_oral_coaching:
            fixed_sections.append(TOOL_ORAL_COACHING_BLOCK)

        blocks: List[Dict[str, Any]] = [
            _cache_text_block("\n\n".join(s for s in fixed_sections if s.strip()), cache=True)
        ]

        slow_sections: List[str] = []
        if temporal_states_section:
            slow_sections.append(temporal_states_section)

        if memory_cards_section:
            slow_sections.append(memory_cards_section)

        if relationship_timeline_section:
            slow_sections.append(relationship_timeline_section)

        if daily_summaries_section:
            slow_sections.append(daily_summaries_section)

        if slow_sections:
            blocks.append(_cache_text_block("\n\n".join(slow_sections), cache=True))

        if chunk_summaries_section:
            blocks.append(_cache_text_block(chunk_summaries_section, cache=True))

        dynamic_sections = [time_section]
        if recent_tool_section:
            dynamic_sections.append(recent_tool_section)

        if vector_search_section:
            dynamic_sections.append(
                "# 本轮召回的相关长期记忆\n"
                "以下记忆可能来自过去日期，不代表今天发生；请以条目日期为准。\n\n"
                + vector_search_section
            )

        dynamic_sections.append("---\n以上是历史信息和用户记忆，请基于这些信息进行对话。")

        blocks.append(_cache_text_block("\n\n".join(dynamic_sections), cache=False))
        return blocks
    
    def _assemble_messages(self, full_system_prompt: Any,
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
        recent_out = [dict(m) for m in recent_messages]
        if len(recent_out) > 2:
            idx = len(recent_out) - 3
            c = recent_out[idx].get("content")
            if isinstance(c, str) and c.strip():
                recent_out[idx]["content"] = [_cache_text_block(c, cache=True)]
        messages.extend(recent_out)
        
        # 添加当前用户消息
        messages.append(current_user_message)
        
        return messages


# 便捷函数
async def build_context(
    session_id: str,
    user_message: str,
    images: Optional[List[Dict[str, Any]]] = None,
    llm_user_text: Optional[str] = None,
    telegram_segment_hint: bool = False,
    tool_oral_coaching: bool = False,
        exclude_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    构建对话上下文的便捷函数。
    
    Args:
        session_id: 会话ID
        user_message: 用户当前消息
        images: 当前轮图片 payload（可选）
        llm_user_text: 对话模型用纯文本（可选，有图片时建议传入）
        telegram_segment_hint: 为 True 时追加 Telegram HTML 白名单与 ||| 分段死指令（仅 Telegram 缓冲路径建议开启）
        tool_oral_coaching: 为 True 时追加 Lutopia 工具口播引导（与启用 tools 的请求对齐）
        
    Returns:
        Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
    """
    builder = ContextBuilder()
    return await builder.build_context(
        session_id,
        user_message,
        images=images,
        llm_user_text=llm_user_text,
        telegram_segment_hint=telegram_segment_hint,
        tool_oral_coaching=tool_oral_coaching,
        exclude_message_id=exclude_message_id,
    )


if __name__ == "__main__":
    """Context 构建模块测试入口。"""
    import asyncio
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

    async def _main_test() -> None:
        from memory.database import (
            clear_session_messages,
            initialize_database,
            save_message,
        )

        await initialize_database()

        test_session = "context_builder_test_session"
        await clear_session_messages(test_session)

        for i in range(10):
            await save_message("user", f"测试用户消息 {i+1}", test_session)
            await save_message("assistant", f"测试助手回复 {i+1}", test_session)

        builder = ContextBuilder()
        context = await builder.build_context(
            test_session, "你好，这是一个测试消息"
        )

        print("Context 构建成功:")
        print(f"System Prompt 长度: {len(context['system_prompt'])}")
        print(f"Messages 数量: {len(context['messages'])}")

        print("\nSystem Prompt 预览:")
        sp = context["system_prompt"]
        print(sp[:200] + "..." if len(sp) > 200 else sp)

        print("\nMessages 结构:")
        for i, msg in enumerate(context["messages"]):
            role = msg["role"]
            c = msg["content"]
            content_preview = c[:50] + "..." if len(c) > 50 else c
            print(f"  [{i}] {role}: {content_preview}")

        await clear_session_messages(test_session)
        print("\nContext 构建器测试完成！")

    print("测试 Context 构建器...")
    try:
        asyncio.run(_main_test())
    except Exception as e:
        print(f"Context 构建器测试失败: {e}")
        import traceback

        traceback.print_exc()
