"""
Context 构建模块。

负责组装发送给 LLM 的完整 prompt，按照优先级从上到下拼装：
1. system prompt：从配置读取，保持原样
2. temporal_states：is_active=1 的全部记录（在记忆卡片之前）
3. memory_cards：查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化后拼入
4. relationship_timeline：条数见 `relationship_timeline_limit`（库内选取），注入 Context 时按 created_at 正序排列
5. 向量检索（长期记忆）：各路 `retrieval_top_k` 条，去重合并，经 SiliconFlow Rerank 精排、阈值过滤、event_type 分级时间衰减、MMR 多样性筛选后注入 `context_max_longterm` 条
6. daily summary：`context_max_daily_summaries`（优先）或环境变量决定最近 N 天，倒序取后翻为正序拼入
7. chunk summary：查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选），附带其来源标识，按时间正序拼入。Telegram 群聊与主用户私聊配对时（`TELEGRAM_MAIN_USER_CHAT_ID` + `TELEGRAM_CONTEXT_GROUP_CHAT_ID`），在 chunk 块内按「对端 chunk + 对端近期原文」与「本侧 chunk」交错顺序注入，便于模型同时看到两侧对话脉络。
8. 最近消息：`short_term_limit`（优先）或环境变量决定条数，再正序排列后拼入

组装完成后返回一个结构，包含 system prompt 和 messages 数组，直接可以传给 LLM API。
"""

import logging
import json
import math
import re
import time
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from datetime import datetime, timedelta

from config import config
from memory.shanghai_dt import now_shanghai
from tools.lutopia import strip_lutopia_behavior_appendix
from memory.retrieval import (
    chroma_where_longterm_summary_types,
    longterm_allowed_summary_types,
)
from memory.database import (
    get_all_active_memory_cards,
    get_all_active_temporal_states,
    get_active_game_session_id,
    get_database,
    get_game_session,
    get_game_turns,
    get_recent_relationship_timeline,
    get_recent_daily_summaries,
    get_recent_tool_executions,
    get_today_chunk_summaries,
    get_unsummarized_messages_desc,
    get_recent_summarized_messages_desc,
    set_active_game_session_id,
)
from memory.micro_batch import fetch_active_persona_display_names
from memory.shanghai_dt import (
    format_created_at_range_preamble,
    format_shanghai_clock_24h,
    format_shanghai_date_iso,
    format_shanghai_datetime_minutes,
    now_shanghai,
    to_shanghai_datetime,
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

_LAST_CONTEXT_TRACE: Dict[str, Any] = {
    "built_at": None,
    "session_id": None,
    "user_message_preview": "",
    "daily_summary_ids": [],
    "chunk_summary_ids": [],
    "archived_daily_summary_ids": [],
    "longterm_doc_ids": [],
    "memory_card_dimensions": [],
}


def get_last_context_trace() -> Dict[str, Any]:
    """返回最近一次实际构建 context 时注入的记忆/摘要清单。"""
    return {
        "built_at": _LAST_CONTEXT_TRACE.get("built_at"),
        "session_id": _LAST_CONTEXT_TRACE.get("session_id"),
        "user_message_preview": _LAST_CONTEXT_TRACE.get("user_message_preview") or "",
        "daily_summary_ids": list(_LAST_CONTEXT_TRACE.get("daily_summary_ids") or []),
        "chunk_summary_ids": list(_LAST_CONTEXT_TRACE.get("chunk_summary_ids") or []),
        "archived_daily_summary_ids": list(
            _LAST_CONTEXT_TRACE.get("archived_daily_summary_ids") or []
        ),
        "longterm_doc_ids": list(_LAST_CONTEXT_TRACE.get("longterm_doc_ids") or []),
        "memory_card_dimensions": list(
            _LAST_CONTEXT_TRACE.get("memory_card_dimensions") or []
        ),
    }

# OpenAI 兼容路径下启用 tools 时注入 system 固定段（与 Bot 侧 tool_oral_coaching 对齐）
TOOL_ORAL_COACHING_BLOCK = (
    "调用工具前，用一句简短口语告诉用户你要去做什么，"
    "语气自然随意，例如「我去看看××」「我去论坛上搜一下」「我去 rcommunity 翻翻帖子」等。"
    "适用于所有可调用工具（含 Lutopia 论坛、rcommunity 论坛 MCP、记忆类工具、天气、联网检索、X、小红书等）。"
    "工具结果回来后，接着用正常语气继续说。"
    "不要罗列工具名称或 API 名，不要说技术性的话。"
)

TELEGRAM_GROUP_CONTINUATION_DIRECTIVE = (
    "【群聊续话】我在下方近期群聊原文中会看到，每句开头用方括号标出说话人："
    "括号内是**用户**在人设中的称呼；另外两方固定为 **[Clio]** 与 **[Sirius]**，分别指两名不同的助手（不是用户）。"
    "与**我当前使用的助手标签**相一致的那一方只会是 [Clio]、[Sirius] 二者之一；该标签下的发言视为**我**已经说过的话。"
    "若历史中已有这样的发言，我本轮续写须紧接上一条往下推进情节或观点，"
    "不要复述、改写或换一种说法重复同一层意思；避免车轱辘话，让对话向前走。"
)

TELEGRAM_GROUP_IN_CHARACTER_DIRECTIVE = (
    "【群聊】沉浸陪伴，非客服；用人设口吻紧接上文续写，默认中文。"
    "禁止 ready to help、自称 AI 助手、列工具菜单或请用户补充任务。"
    "工具照常可用，需用时一句口语带过即可。"
)

TELEGRAM_GROUP_USER_TURN_HINT = (
    "[系统提示：群聊。人设口吻紧接上文，守分段规定；禁助手寒暄与能力菜单；工具照常。]"
)

GAME_MODE_CONTEXT_BLOCK = """# 当前游戏模式

你正在与{participants}进行「{display_name}」({game_type})。
请按照下方游戏规则进行，同时保持你自己的人设和说话风格。

## 游戏规则
{system_prompt}

## 当前游戏状态
{state_json}

## 游戏历史记录
{game_turns}"""

TELEGRAM_CROSS_CHANNEL_PEER_DIRECTIVE = (
    "【跨会话情境说明】本块除「今日 chunk 摘要」外，另含来自 Telegram 另一会话（私聊或群聊）的近期原文摘录，"
    "供我把握两边话题的连续性；该摘录不是当前输入框里的逐条消息，参与者与语气可能与当前会话不同。"
    "我生成回复时仍须以**当前用户消息所在会话**为准：我在群聊中不要默认对方已看到私聊里才说过的事，除非用户明确提起；"
    "我在私聊中不要把群里的玩笑或多人起哄直接当成对用户个人的承诺。"
    "我可自然呼应另一边的信息，但不要编造另一会话中未出现的细节。"
)


async def _group_transcript_user_bracket_label() -> str:
    """群聊 transcript 中用户行括号标签：来自激活 chat 人设的 user_name。"""
    _, user_name = await fetch_active_persona_display_names()
    un = (user_name or "").strip()
    return un if un else "用户"


def _group_transcript_speaker_bracket(sender_raw: str, *, user_bracket_label: str) -> str:
    """shared_group_messages.sender → transcript 内方括号中的说话人（两助手固定为 Clio / Sirius）。"""
    s = str(sender_raw or "").strip().lower()
    if s == "user":
        ub = (user_bracket_label or "").strip()
        return ub if ub else "用户"
    if s == "clio":
        return "Clio"
    if s == "sirius":
        return "Sirius"
    tail = str(sender_raw or "").strip()
    return tail if tail else "?"


def _prepend_group_reply_to_author(content: str, row: Dict[str, Any]) -> str:
    """把共享群表里的引用作者还原成只进 LLM context 的前缀行。"""
    reply_to_author = str(row.get("reply_to_author") or "").strip()
    body = str(content or "").strip()
    if not reply_to_author:
        return body
    prefix = f"[回复了 {reply_to_author}]"
    return f"{prefix}\n{body}" if body else prefix


async def _telegram_group_chunk_viewpoint_line() -> str:
    """注入「## 群聊摘要」下：第一人称说明方括号标签与「我」在摘要中的指代（与 chunk 摘要任务、人设叙述一致）。"""
    char_name, user_name = await fetch_active_persona_display_names()
    ub = (user_name or "").strip() or "用户"
    cn = (char_name or "").strip() or "助手"
    relay = get_database()._shared_summary_actor()
    me_relay = "Clio" if relay == "clio" else "Sirius"
    peer_relay = "Sirius" if relay == "clio" else "Clio"
    return (
        f"（我在本节群聊摘录中会看到方括号开头的说话人："
        f"**[{ub}]** 指用户；**[{me_relay}]**、**[{peer_relay}]** 固定指两名助手。我在对话中使用的助手标签是 **{me_relay}**；"
        f"我在人设中的名字是 **{cn}**。若摘要正文里出现第一人称「我」，在无特别声明时即指 **{cn}**（我的人设视角），不要误读成另一名助手或用户。）"
    )

TTS_PROMPT_BLOCK = """【TTS语音输出说明】
当前系统已开启语音输出，你可以自由穿插文字和语音消息。

语音标记规则：
- 用 [voice]...[/voice] 标记要发语音的内容
- 语音内容可以出现在回复的任何位置
- 文字和语音内容不要重复，语音是独立的消息
- 语音适合表达语气、情感、口语化内容
- 文字适合陈述、说明、需要阅读的内容
- 语音内容里不要写任何括号（包括（）），括号里的动作描述必须删除
- 只允许在语音里使用 <#秒数#> 停顿标签，不要混用其他格式（例如 [语音]）

语气标签（可嵌入语音内容中）：
(sighs)   — 叹气
(chuckle) — 忍住的轻笑
(laughs)  — 笑出来
(breath)  — 呼吸/停顿感
(emm)     — 犹豫/思考中
(humming) — 低哼
(gasps)   — 轻微惊讶
(groans)  — 低鸣/无奈

停顿控制：
用 <#秒数#> 在语音中插入停顿，单位秒，范围 0.01~99.99

示例：
"你今天怎么样？
[voice](chuckle)看你这表情就知道了[/voice]
有什么想聊的吗？
[voice](sighs)我也有点累了呢[/voice]"
"""

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
    用户消息发往 LLM 时附带的单行时间（东八区 24 小时制），仅写入上下文，不落库。
    ``created_at`` 为 None 时表示「当前时刻」（用于本轮尚未入库的用户输入）。
    """
    dt = to_shanghai_datetime(created_at) if created_at is not None else None
    if dt is None:
        dt = now_shanghai()
    # 时、分之间用全角冒号，与「当前系统时间」块区分表述为「当前时间」
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    line = (
        f"{dt.year}年{dt.month}月{dt.day}日"
        f"{weekdays[dt.weekday()]} "
        f"{dt.hour:02d}：{dt.minute:02d}"
    )
    return f"【当前时间：{line}】（东八区24小时制）"


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


_SHORT_TERM_CONTEXT_HOURS = 48


def _short_term_context_since() -> datetime:
    """群聊近期原文注入的时间下界（东八区墙钟 naive，与库 TIMESTAMP 约定一致）。"""
    return (now_shanghai() - timedelta(hours=_SHORT_TERM_CONTEXT_HOURS)).replace(
        tzinfo=None
    )


async def _summarized_overlap_limit() -> int:
    """已摘要消息重叠条数：优先 config 表 summarized_overlap_limit，否则默认 5。"""
    try:
        raw = await get_database().get_config("summarized_overlap_limit")
        if raw is not None and str(raw).strip() != "":
            return max(0, min(20, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 summarized_overlap_limit 失败，使用默认 5: %s", e)
    return 5


async def _context_max_daily_summaries_limit() -> int:
    """每日小传注入天数：优先 config 表 context_max_daily_summaries，否则环境变量 CONTEXT_MAX_DAILY_SUMMARIES。"""
    try:
        raw = await get_database().get_config("context_max_daily_summaries")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(100, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 context_max_daily_summaries 失败，使用环境变量: %s", e)
    return max(1, min(100, config.CONTEXT_MAX_DAILY_SUMMARIES))


async def _longterm_date_cutoff_iso() -> Optional[str]:
    """长期记忆召回日期上限：排除已由 daily summaries 覆盖的最近 N 天。"""
    try:
        n_days = await _context_max_daily_summaries_limit()
        return (now_shanghai().date() - timedelta(days=n_days)).isoformat()
    except Exception as e:
        logger.debug("计算长期记忆日期 cutoff 失败，跳过日期过滤: %s", e)
        return None


def _longterm_result_before_cutoff(
    result: Dict[str, Any],
    cutoff_date: Optional[str],
) -> bool:
    """BM25 无日期 where，merge 后用 metadata.date 做同口径过滤。"""
    if not cutoff_date:
        return True
    md = result.get("metadata") or {}
    raw = md.get("date")
    if not raw:
        return False
    day = format_shanghai_date_iso(raw) or str(raw)[:10]
    return bool(day and day < cutoff_date)


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
    """双路检索各路 top_k：优先 config 表 retrieval_top_k，默认 30。"""
    try:
        raw = await get_database().get_config("retrieval_top_k")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(50, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 retrieval_top_k 失败，使用默认 30: %s", e)
    return 30


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


async def _starred_boost_factor_value() -> float:
    """收藏长期事件的召回加权系数：优先 config 表 starred_boost_factor，否则默认 1.2。"""
    try:
        raw = await get_database().get_config("starred_boost_factor")
        if raw is not None and str(raw).strip() != "":
            return max(1.0, min(3.0, float(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 starred_boost_factor 失败，使用默认 1.2: %s", e)
    return 1.2


async def _rerank_blend_weight_value() -> float:
    """rerank 语义分在融合公式里的权重，余量给 decay_score；默认 0.7。"""
    try:
        raw = await get_database().get_config("rerank_blend_weight")
        if raw is not None and str(raw).strip() != "":
            return max(0.0, min(1.0, float(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_blend_weight 失败，使用默认 0.7: %s", e)
    return 0.7


async def _context_archived_daily_limit() -> int:
    """远古 daily 补充条数：优先 config 表 context_archived_daily_limit，否则默认 3。"""
    try:
        raw = await get_database().get_config("context_archived_daily_limit")
        if raw is not None and str(raw).strip() != "":
            return max(0, min(20, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 context_archived_daily_limit 失败，使用默认 3: %s", e)
    return 3


async def _archived_daily_min_hits() -> int:
    """触发远古 daily 优先补充的召回事件数阈值，默认 2。"""
    try:
        raw = await get_database().get_config("archived_daily_min_hits")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(20, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 archived_daily_min_hits 失败，使用默认 2: %s", e)
    return 2


# ---------------------------------------------------------------------------
# C3: Rerank 配置读取函数
# ---------------------------------------------------------------------------

async def _rerank_enabled() -> bool:
    """是否启用 rerank：优先 config 表 rerank_enabled，默认 true。"""
    try:
        raw = await get_database().get_config("rerank_enabled")
        if raw is not None:
            return str(raw).strip().lower() in ("true", "1", "yes")
    except Exception:
        pass
    return True


async def _rerank_candidate_size() -> int:
    """rerank 候选集大小上限，默认 50。"""
    try:
        raw = await get_database().get_config("rerank_candidate_size")
        if raw is not None and str(raw).strip() != "":
            return max(10, min(100, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_candidate_size 失败，使用默认 50: %s", e)
    return 50


async def _rerank_score_floor() -> float:
    """非收藏事件的 rerank 分数阈值，默认 0.3。"""
    try:
        raw = await get_database().get_config("rerank_score_floor")
        if raw is not None and str(raw).strip() != "":
            return max(0.0, min(1.0, float(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_score_floor 失败，使用默认 0.3: %s", e)
    return 0.3


async def _rerank_starred_floor() -> float:
    """收藏事件的 rerank 分数阈值，默认 0.15。"""
    try:
        raw = await get_database().get_config("rerank_starred_floor")
        if raw is not None and str(raw).strip() != "":
            return max(0.0, min(1.0, float(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_starred_floor 失败，使用默认 0.15: %s", e)
    return 0.15


async def _rerank_query_max_chars() -> int:
    """rerank query 最大字符数，默认 300。"""
    try:
        raw = await get_database().get_config("rerank_query_max_chars")
        if raw is not None and str(raw).strip() != "":
            return max(50, min(1000, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_query_max_chars 失败，使用默认 300: %s", e)
    return 300


async def _rerank_query_turns() -> int:
    """构建 rerank query 时取最近几轮对话，默认 2。"""
    try:
        raw = await get_database().get_config("rerank_query_turns")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(10, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_query_turns 失败，使用默认 2: %s", e)
    return 2


async def _rerank_timeout_sec() -> float:
    """rerank API 超时秒数，默认 3.0。"""
    try:
        raw = await get_database().get_config("rerank_timeout_sec")
        if raw is not None and str(raw).strip() != "":
            return max(0.5, min(10.0, float(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 rerank_timeout_sec 失败，使用默认 3.0: %s", e)
    return 3.0


async def _half_life_by_event_type(event_type: str) -> int:
    """按 event_type 返回半衰期天数。"""
    et = (event_type or "").strip().lower()
    if et == "milestone":
        key = "half_life_milestone"
        default = 1000
    elif et in ("decision", "emotional_shift"):
        key = "half_life_decision"
        default = 200
    else:
        key = "half_life_default"
        default = 60
    try:
        raw = await get_database().get_config(key)
        if raw is not None and str(raw).strip() != "":
            return max(1, int(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception:
        pass
    return default


async def _build_rerank_query(session_id: str, current_message: str) -> str:
    """
    构建 rerank query：取当前 session 最近 N 轮对话，加角色前缀，截断到 max_chars。
    """
    max_chars = await _rerank_query_max_chars()
    turns = await _rerank_query_turns()

    # 取最近消息（不包含当前轮）
    recent = await get_unsummarized_messages_desc(session_id, limit=turns * 2)
    recent.reverse()  # 正序

    parts = []
    for msg in recent:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            parts.append(f"南杉: {content}")
        elif role == "assistant":
            parts.append(f"小克: {content}")

    # 加当前消息
    if current_message and current_message.strip():
        parts.append(f"南杉: {current_message.strip()}")

    query = "\n".join(parts)
    if len(query) > max_chars:
        query = query[-max_chars:]
    return query


def _time_decay_factor(metadata: Dict[str, Any], half_life_days: int, now_ts: float) -> float:
    """计算时间衰减系数：exp(-ln2 / half_life * age_days)。"""
    age = _memory_age_days(metadata, now_ts)
    if half_life_days <= 0:
        return 1.0
    return math.exp(-math.log(2) / half_life_days * age)


def _source_age_days(metadata: Dict[str, Any], now_ts: float) -> float:
    """按事件发生日计算年龄；优先 source_date/date，缺失时回退 created_at/last_access_ts。"""
    md = metadata or {}
    for key in ("source_date", "date", "created_at"):
        raw = md.get(key)
        if not raw:
            continue
        try:
            s = str(raw).strip()
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                dt = datetime.fromisoformat(f"{s}T00:00:00+08:00")
            else:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return max(0.0, (now_ts - dt.timestamp()) / 86400.0)
        except (TypeError, ValueError):
            continue
    return _memory_age_days(md, now_ts)


def _is_starred(metadata: Dict[str, Any]) -> bool:
    """判断是否收藏。"""
    return metadata.get("is_starred") is True or str(metadata.get("is_starred", "")).lower() == "true"


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
    "禁止在面向用户可见的 assistant 正文开头写「思维链」或「**思维链**」小标题；"
    "推理须走 API 的 reasoning/thinking 通道，或使用 <thinking>…</thinking> 包裹，标签外只保留角色对白。"
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
        "语义完整优先，禁止超长整段 / 句子中间截断 / 机械平均切分。\n"
        "以下情况必须用 ||| 强制分段：\n"
        "- 分点叙述（如「1. 2. 3.」「第一、第二」「首先、其次」）\n"
        "- 列举多个独立要点\n"
        "- 话题转折（如「对了」「另外」「话说回来」）\n"
        "- 长解释后跟独立结论时，结论单独成段\n\n"
        "(3) 表情包：情绪浓度高时写 [meme:中文描述]，自然插入，不必每轮都发。\n"
        "[meme:…] 与 ||| 都是顺序分隔符，从左到右依次发出。\n\n"
        "避免 <blockquote> 包大段聊天内容（思维链的 blockquote 系统自动处理，与此无关）。\n"
        "禁用行首 > 引用语法。"
    )


async def format_telegram_group_segment_directive() -> str:
    """
    群聊：对模型的**硬写作要求**（分段、字数、条数）；数值来自 ``group_chat_max_message_chars``。
    发送端不按字数截断，故必须由模型自律遵守下列规则。
    """
    db = get_database()
    raw = await db.get_config("group_chat_max_message_chars", "600")
    try:
        n = int(str(raw).strip() or "600")
    except ValueError:
        n = 600
    n = max(10, min(3800, n))
    return (
        "\n\n"
        "【Telegram 群聊 · 分段死规定（必须遵守）】\n"
        "以下由你自律执行；系统不会在发送端按字数截断或补省略号，违反会直接表现为超长单条或刷屏。\n\n"
        f"1) **必须分段**：除极短寒暄外，**禁止**把整轮回答写成不换行的一整块。在段落、话题或语气自然停顿处用**单个换行**拆成多条气泡。\n"
        "以下情况必须换行分段：\n"
        "- 分点叙述（如「1. 2. 3.」「第一、第二」「首先、其次」）\n"
        "- 列举多个独立要点\n"
        "- 话题转折（如「对了」「另外」「话说回来」）\n"
        "- 长解释后跟独立结论时，结论单独成段\n"
        f"2) **每段长度**：每一行（换行分隔出的每一段）正文不得超过约 **{n}** 字；若将超出，**必须**在该上限之前主动换行续写，不得硬顶到一句无停顿的长文。\n"
        "3) **条数上限**：同一轮助手回复**最多 3 段**（即正文里**至多使用 2 次换行**拆成三段）；内容再多也**必须**压缩、合并或删减到三段以内发出，不得拆成更多行。\n"
        "4) **禁止**为凑字数在句子中间机械切断；**禁止**用 ||| 在群聊拆成大量短条；群聊以换行为主，||| 仅在确有必要时用。\n"
        "5) **酌情放宽字数**：日常闲聊、接梗、简短回应仍须严守第 2 条每段上限。若本轮确需展开——例如认真回答专业/技术问题，与南杉一起吐槽或拆解分析某件事，或安慰、接住南杉的情绪——可在**仍尽量分段**的前提下，单行可略超上述字数；但总段数仍遵守第 3 条，且不得为凑篇幅写成不换行的超长独白。\n"
        "6) 上文「Telegram 排版」里针对私聊的 ||| 条数/每段字数是另一套规则；**群聊以本段为最高优先级**。"
    )


def _created_at_timestamp_for_sort(created_at: Any) -> float:
    """用于 relationship_timeline 等按时间正序排序；无法解析时置 0（排在最前）。"""
    dt = to_shanghai_datetime(created_at)
    if dt is None:
        return 0.0
    try:
        return float(dt.timestamp())
    except OSError:
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


async def _rerank_success_decay_score(metadata: Dict[str, Any], now_ts: float) -> float:
    """rerank 成功路径的 decay_score：base_score * time_decay * hits_boost。"""
    md = metadata or {}
    try:
        arousal = float(md.get("arousal", 0.1))
    except (TypeError, ValueError):
        arousal = 0.1
    arousal = max(0.0, min(1.0, arousal))

    try:
        raw_hl = md.get("halflife_days")
        halflife_days = float(raw_hl) if raw_hl is not None and str(raw_hl).strip() != "" else 0.0
    except (TypeError, ValueError):
        halflife_days = 0.0
    if halflife_days <= 0:
        halflife_days = float(await _half_life_by_event_type(str(md.get("event_type") or "")))

    effective_hl = max(1.0, float(halflife_days) * (1.0 + arousal))
    if _is_starred(md):
        time_decay = 1.0
    else:
        days_elapsed = _source_age_days(md, now_ts)
        time_decay = math.exp(-math.log(2) * days_elapsed / effective_hl)

    try:
        base_score = float(md.get("base_score", md.get("score", 5.0)))
    except (TypeError, ValueError):
        base_score = 5.0
    try:
        hits = int(md.get("hits", 0))
    except (TypeError, ValueError):
        hits = 0
    hits = max(0, hits)
    hits_boost = 1.0 + 0.35 * math.log(1 + hits)
    return base_score * time_decay * hits_boost


def fuse_rerank_with_time_decay(
    candidates: List[Dict[str, Any]],
    starred_boost_factor: float = 1.2,
) -> List[Dict[str, Any]]:
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
        md = cp.get("metadata") or {}
        if md.get("is_starred") is True or str(md.get("is_starred")).lower() == "true":
            cp["fusion_score"] *= max(1.0, float(starred_boost_factor or 1.0))
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
    exist_lines: List[str] = []
    if cn:
        exist_lines.append(f"你的名字是 {cn}。")
    if ci:
        exist_lines.append(ci)
    if exist_lines:
        sections.append("【存在定义】\n" + "\n".join(exist_lines))

    cpers = _persona_field_str(row, "char_personality")
    ca = _persona_field_str(row, "char_appearance")
    inner_image_parts: List[str] = []
    if cpers:
        inner_image_parts.append(cpers)
    if ca:
        inner_image_parts.append("外在形象：\n" + ca)
    if inner_image_parts:
        sections.append("【内在人格和外在形象】\n" + "\n\n".join(inner_image_parts))

    contract_parts: List[str] = []
    cs = _persona_field_str(row, "char_speech_style")
    cr = _persona_field_str(row, "char_redlines")
    if cs:
        contract_parts.append("说话风格与格式硬规范：\n" + cs)
    if cr:
        contract_parts.append("行为红线与绝对禁忌：\n" + cr)
    if contract_parts:
        sections.append("【表达契约】\n" + "\n\n".join(contract_parts))

    cnsfw = _persona_field_str(row, "char_nsfw")
    if cnsfw:
        sections.append("【成人内容】\n" + cnsfw)

    crels = _persona_field_str(row, "char_relationships")
    if crels:
        sections.append("【机际关系】\n" + crels)

    tools_parts: List[str] = []
    ctg = _persona_field_str(row, "char_tools_guide")
    com = _persona_field_str(row, "char_offline_mode")
    if ctg:
        tools_parts.append("工具使用守则：\n" + ctg)
    if com:
        tools_parts.append("线下模式（在赛博世界接触）：\n" + com)
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


# Telegram 群/私聊交叉上下文：对端会话原文注入 system 时的总长度上限。
_TELEGRAM_PEER_RECENT_MAX_CHARS = 12000


async def _telegram_cross_context_peer_session_id(session_id: str) -> Optional[str]:
    """
    若当前为 Telegram 群聊或（主用户）私聊，返回「另一路」session_id。

    - 群聊 ``telegram_group_*`` → 私聊 ``telegram_{TELEGRAM_MAIN_USER_CHAT_ID}``
    - 私聊 ``telegram_*``（非 group）→ 群聊 ``telegram_group_{gid}``；
      ``gid`` 优先 ``TELEGRAM_CONTEXT_GROUP_CHAT_ID``；未配置时若共享群表仅有
      一个 ``chat_id``，则自动使用该 id（单群部署）。
    """
    sid = str(session_id or "").strip()
    if not sid.startswith("telegram"):
        return None
    if sid.startswith("telegram_group_"):
        main = (config.TELEGRAM_MAIN_USER_CHAT_ID or "").strip()
        if not main:
            return None
        return f"telegram_{main}"
    gid = (config.TELEGRAM_CONTEXT_GROUP_CHAT_ID or "").strip()
    if not gid:
        gid = await get_database().get_unique_shared_group_chat_id_for_context() or ""
        gid = str(gid).strip()
    if not gid:
        return None
    return f"telegram_group_{gid}"


class ContextBuilder:
    """
    Context 构建器类。
    
    负责组装完整的对话上下文，供 LLM 使用。
    """
    
    def __init__(self):
        """
        初始化 Context 构建器。
        """
        self._last_longterm_results: List[Dict[str, Any]] = []
        self._last_daily_summary_ids: List[int] = []
        self._last_chunk_summary_ids: List[int] = []
        self._last_archived_daily_summary_ids: List[int] = []
        self._last_memory_card_dimensions: List[str] = []
        logger.info("Context 构建器初始化完成")

    def _record_context_trace(self, session_id: str, user_message: str) -> None:
        """保存最近一轮真实注入的摘要与长期记忆，供 Mini App 排查使用。"""
        global _LAST_CONTEXT_TRACE
        _LAST_CONTEXT_TRACE = {
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "user_message_preview": (user_message or "").replace("\n", " ")[:160],
            "daily_summary_ids": list(self._last_daily_summary_ids),
            "chunk_summary_ids": list(self._last_chunk_summary_ids),
            "archived_daily_summary_ids": list(self._last_archived_daily_summary_ids),
            "longterm_doc_ids": [
                str(r.get("id") or "")
                for r in self._last_longterm_results
                if r.get("id")
            ],
            "memory_card_dimensions": list(self._last_memory_card_dimensions),
            "rerank_scores": {
                str(r.get("id") or ""): {
                    "rerank_score": r.get("rerank_score"),
                    "fusion_score": float(r.get("fusion_score", 0.0)),
                    "event_type": (r.get("metadata") or {}).get("event_type"),
                }
                for r in self._last_longterm_results
                if r.get("id")
            },
        }

    async def _build_game_persona_prompt(self) -> str:
        """游戏模式只注入核心人设字段，跳过 NSFW 与线下模式。"""
        try:
            db = get_database()
            active = await db.get_active_api_config("chat")
            persona_id = active.get("persona_id") if active else None
            if not persona_id:
                return config.SYSTEM_PROMPT
            row = await db.pool.fetchrow(
                "SELECT * FROM persona_configs WHERE id = $1", int(persona_id)
            )
            if not row:
                return config.SYSTEM_PROMPT
            data = dict(row)

            def _s(key: str) -> str:
                return _persona_field_str(data, key)

            parts: List[str] = []
            exist_lines: List[str] = []
            if _s("char_name"):
                exist_lines.append(f"你的名字是 {_s('char_name')}。")
            if _s("char_identity"):
                exist_lines.append(_s("char_identity"))
            if exist_lines:
                parts.append("【存在定义】\n" + "\n".join(exist_lines))

            image_parts: List[str] = []
            if _s("char_personality"):
                image_parts.append(_s("char_personality"))
            if _s("char_appearance"):
                image_parts.append("外在形象：\n" + _s("char_appearance"))
            if image_parts:
                parts.append("【内在人格和外在形象】\n" + "\n\n".join(image_parts))

            if _s("char_speech_style"):
                parts.append("【表达契约】\n说话风格与格式硬规范：\n" + _s("char_speech_style"))

            return "\n\n".join(parts).strip() or config.SYSTEM_PROMPT
        except Exception as e:
            logger.warning("构建游戏模式人设失败，回退 SYSTEM_PROMPT: %s", e)
            return config.SYSTEM_PROMPT

    async def build_game_context(
        self,
        session_id: str,
        user_message: str,
        game_session: Dict[str, Any],
        images: Optional[List[Dict[str, Any]]] = None,
        llm_user_text: Optional[str] = None,
        telegram_segment_hint: bool = False,
        exclude_message_id: Optional[int] = None,
        short_term_dedup_user_text: Optional[str] = None,
        group_recent_skip_tg_message_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        system_prompt = await self._build_game_persona_prompt()
        memory_cards_section = await self._build_memory_cards_section()

        daily_rows = await get_recent_daily_summaries(limit=1)
        daily_rows.reverse()
        self._last_daily_summary_ids = [
            int(s["id"]) for s in daily_rows if s.get("id") is not None
        ]
        daily_sections: List[str] = []
        for summary in daily_rows:
            raw_date = summary.get("source_date") or summary.get("created_at")
            formatted_date = format_shanghai_date_iso(raw_date) or (
                str(raw_date)[:10] if raw_date else ""
            )
            daily_sections.append(
                f"### {formatted_date or '未知日期'}\n{summary.get('summary_text') or ''}"
            )
        daily_summaries_section = (
            "# 每日摘要\n\n" + "\n\n".join(daily_sections)
            if daily_sections
            else ""
        )

        state = game_session.get("state_json") or {}
        state_text = (
            json.dumps(state, ensure_ascii=False, indent=2)
            if state
            else "新游戏，尚无状态"
        )
        turns = await get_game_turns(game_session.get("id"))
        turn_lines = []
        for turn in turns:
            data = json.dumps(
                turn.get("turn_data") or {}, ensure_ascii=False, separators=(",", ":")
            )
            turn_lines.append(f"第{turn.get('turn_idx')}轮: {data}")
        participants = game_session.get("participants") or []
        if isinstance(participants, list):
            participants_text = "、".join(str(p) for p in participants if str(p).strip())
        else:
            participants_text = str(participants)
        game_block = GAME_MODE_CONTEXT_BLOCK.format(
            participants=participants_text or "玩家",
            display_name=game_session.get("display_name") or game_session.get("game_type") or "未命名游戏",
            game_type=game_session.get("game_type") or "",
            system_prompt=game_session.get("system_prompt") or "未设置",
            state_json=state_text,
            game_turns="\n".join(turn_lines) if turn_lines else "暂无历史记录",
        )
        if game_session.get("state_mode") == "per_turn":
            state_instruction = (
                "每轮回复末尾，你必须输出 [GAME_STATE]{更新后的完整状态JSON}[/GAME_STATE] 块。"
                "只输出变化后的完整状态。该块会被系统解析，不会发送给用户。"
            )
        else:
            state_instruction = (
                "当游戏session结束（用户说停/你判断该存档了）时，在回复末尾输出 "
                "[GAME_STATE]{当前完整状态JSON}[/GAME_STATE] 块。进行中不需要输出该块。"
            )
        turn_instruction = (
            "每轮回复末尾，你还必须输出 [GAME_TURN]{本轮发生事件的JSON摘要}[/GAME_TURN] 块。"
            "该块会被系统解析记录，不会发送给用户。"
        )

        _dedup = (
            short_term_dedup_user_text
            if short_term_dedup_user_text is not None
            and str(short_term_dedup_user_text).strip()
            else user_message
        )
        recent_messages_section = await self._build_recent_messages_section(
            session_id,
            exclude_message_id,
            current_user_text=_dedup,
            group_recent_skip_tg_message_ids=group_recent_skip_tg_message_ids,
        )
        cut = (
            llm_user_text
            if images and (llm_user_text is not None and str(llm_user_text).strip())
            else user_message
        )
        current_user_message = self._build_current_user_message(cut, images)
        blocks: List[Any] = [
            _cache_text_block(
                "\n\n".join(
                    s for s in [system_prompt, memory_cards_section, daily_summaries_section] if s
                ),
                cache=True,
            ),
            _cache_text_block(
                "\n\n".join([game_block, state_instruction, turn_instruction]),
                cache=False,
            ),
        ]
        if telegram_segment_hint:
            blocks.append(_cache_text_block(await format_telegram_reply_segment_hint(), cache=False))
            if str(session_id).startswith("telegram_group_"):
                blocks.append(
                    _cache_text_block(await format_telegram_group_segment_directive(), cache=False)
                )
        messages = self._assemble_messages(
            blocks, recent_messages_section, current_user_message
        )
        logger.info(
            "game context built: session=%s game_session=%s messages_count=%s",
            session_id,
            game_session.get("id"),
            len(messages),
        )
        self._record_context_trace(session_id, user_message)
        return {
            "system_prompt": blocks,
            "messages": messages,
            "cacheable_ratio": 0.0,
            "game_session": game_session,
        }
    
    async def build_context(
        self,
        session_id: str,
        user_message: str,
        images: Optional[List[Dict[str, Any]]] = None,
        llm_user_text: Optional[str] = None,
        telegram_segment_hint: bool = False,
        tool_oral_coaching: bool = False,
        exclude_message_id: Optional[int] = None,
        short_term_dedup_user_text: Optional[str] = None,
        group_recent_skip_tg_message_ids: Optional[Sequence[str]] = None,
        daily_summaries_override: Optional[List[Dict[str, Any]]] = None,
        skip_vector_search: bool = False,
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
            tool_oral_coaching: 为 True 时在 system 末尾追加「工具调用前口播」引导（与启用 OpenAI tools 的请求对齐）
            short_term_dedup_user_text: 若 user_message 含额外系统缀文（如群聊提示），可传与 shared_group_messages.content 一致的文本（Telegram 缓冲路径下即 combined_raw），用于短期历史去重
            group_recent_skip_tg_message_ids: 群聊缓冲合并本轮涉及的 Telegram message_id，用于从短期历史尾部剥离对应 user 行（多句合并时 combined_raw 无法与单行 content 相等）
            
        Returns:
            Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
        """
        try:
            active_game_id = await get_active_game_session_id()
            if active_game_id:
                game_session = await get_game_session(active_game_id)
                if game_session and not game_session.get("ended_at"):
                    return await self.build_game_context(
                        session_id,
                        user_message,
                        game_session,
                        images=images,
                        llm_user_text=llm_user_text,
                        telegram_segment_hint=telegram_segment_hint,
                        exclude_message_id=exclude_message_id,
                        short_term_dedup_user_text=short_term_dedup_user_text,
                        group_recent_skip_tg_message_ids=group_recent_skip_tg_message_ids,
                    )
                await set_active_game_session_id(None)

            # 1. 获取 system prompt
            system_prompt = await self._build_system_prompt()

            temporal_section = await self._build_temporal_states_section()
            
            # 2. 获取 memory cards
            memory_cards_section = await self._build_memory_cards_section()

            relationship_timeline_section = await self._build_relationship_timeline_section()
            
            # 3. 长期记忆（向量）；4. daily；5. chunk（与 _assemble_full_system_prompt 拼接顺序一致）
            if skip_vector_search:
                self._last_longterm_results = []
                self._last_longterm_summary_ids = []
                vector_search_section = ""
            else:
                rerank_enabled = await _rerank_enabled()
                if rerank_enabled:
                    try:
                        vector_search_section = await self._build_vector_search_section_async(
                            user_message,
                            session_id,
                        )
                    except Exception as e:
                        logger.warning("主路径 Rerank 长期记忆召回失败，回退旧路径: %s", e)
                        vector_search_section = await self._build_vector_search_section(
                            session_id,
                            user_message,
                        )
                else:
                    vector_search_section = await self._build_vector_search_section(
                        session_id,
                        user_message,
                    )
            archived_daily_section = await self._build_archived_daily_supplement_section(session_id)
            daily_summaries_section = await self._build_daily_summaries_section(
                session_id,
                daily_summaries_override=daily_summaries_override,
            )
            chunk_summaries_section = await self._build_chunk_summaries_section(session_id)
            recent_tool_section = await self._build_recent_tool_executions_section(session_id)
            logger.info(
                "context chunk section preview: session=%s chunk_section_len=%s tail=%r",
                session_id,
                len(chunk_summaries_section or ""),
                (chunk_summaries_section or "")[-500:],
            )

            # 6. 获取最近消息
            _dedup = (
                short_term_dedup_user_text
                if short_term_dedup_user_text is not None
                and str(short_term_dedup_user_text).strip()
                else user_message
            )
            recent_messages_section = await self._build_recent_messages_section(
                session_id,
                exclude_message_id,
                current_user_text=_dedup,
                group_recent_skip_tg_message_ids=group_recent_skip_tg_message_ids,
            )

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
                archived_daily_section,
                daily_summaries_section,
                chunk_summaries_section,
                recent_tool_section,
                tool_oral_coaching=tool_oral_coaching,
            )
            if telegram_segment_hint:
                full_system_prompt.append(
                    _cache_text_block(await format_telegram_reply_segment_hint(), cache=False)
                )
                if str(session_id).startswith("telegram_group_"):
                    full_system_prompt.append(
                        _cache_text_block(
                            await format_telegram_group_segment_directive(), cache=False
                        )
                    )

            if str(session_id).startswith("telegram_group_"):
                full_system_prompt.append(
                    _cache_text_block(TELEGRAM_GROUP_CONTINUATION_DIRECTIVE, cache=False)
                )
                full_system_prompt.append(
                    _cache_text_block(TELEGRAM_GROUP_IN_CHARACTER_DIRECTIVE, cache=False)
                )

            # TTS 语气标签注入
            tts_enabled = await get_database().get_config("tts_enabled", "false")
            logger.info("[TTS注入] tts_enabled 原始值=%r, 判断结果=%s", tts_enabled, tts_enabled.lower() in ("true", "1"))
            if tts_enabled.lower() in ("true", "1"):
                full_system_prompt.append(
                    _cache_text_block(TTS_PROMPT_BLOCK, cache=False)
                )
                logger.info("[TTS注入] TTS_PROMPT_BLOCK 已追加到 system prompt，当前共 %d 个 block", len(full_system_prompt))

            # 组装 messages 数组
            messages = self._assemble_messages(
                full_system_prompt,
                recent_messages_section,
                current_user_message
            )

            # 用实际组装后的内容计算 cacheable_ratio（包含格式化开销和工具定义）
            # 前缀缓存（SiliconFlow 等）缓存整个稳定前缀，包括 cache_control 标记前的块
            # 统计方式：从第一个块开始，到（含）第一个带 cache_control 的块为止
            system_text_len = 0
            cached_text_len = 0
            found_cache_boundary = False
            if isinstance(full_system_prompt, list):
                for block in full_system_prompt:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blen = len(block.get("text") or "")
                        system_text_len += blen
                        if not found_cache_boundary:
                            cached_text_len += blen
                            if block.get("cache_control"):
                                found_cache_boundary = True
            elif isinstance(full_system_prompt, str):
                system_text_len = len(full_system_prompt)

            messages_text_len = 0
            for m in messages:
                if m.get("role") == "system":
                    continue
                c = m.get("content", "")
                if isinstance(c, str):
                    messages_text_len += len(c)
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            messages_text_len += len(part.get("text") or "")

            total_text_len = system_text_len + messages_text_len
            cacheable_ratio = cached_text_len / total_text_len if total_text_len > 0 else 0.0

            logger.debug(f"Context 构建完成: session={session_id}, system_prompt_length={len(full_system_prompt)}, messages_count={len(messages)}")
            logger.info(
                "context built: session=%s system_prompt_length=%s messages_count=%s daily_section_len=%s chunk_section_len=%s recent_messages=%s cacheable_ratio=%.3f",
                session_id,
                len(full_system_prompt),
                len(messages),
                len(daily_summaries_section or ""),
                len(chunk_summaries_section or ""),
                len(recent_messages_section or []),
                cacheable_ratio,
            )
            self._record_context_trace(session_id, user_message)

            return {
                "system_prompt": full_system_prompt,
                "messages": messages,
                "cacheable_ratio": cacheable_ratio,
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
        short_term_dedup_user_text: Optional[str] = None,
        group_recent_skip_tg_message_ids: Optional[Sequence[str]] = None,
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
            tool_oral_coaching: 为 True 时在 system 末尾追加「工具调用前口播」引导
            
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
            vector_search_section = await self._build_vector_search_section_async(user_message, session_id)
            archived_daily_section = await self._build_archived_daily_supplement_section(session_id)
            daily_summaries_section = await self._build_daily_summaries_section(session_id)
            chunk_summaries_section = await self._build_chunk_summaries_section(session_id)
            recent_tool_section = await self._build_recent_tool_executions_section(session_id)
            logger.info(
                "context chunk section preview async: session=%s chunk_section_len=%s tail=%r",
                session_id,
                len(chunk_summaries_section or ""),
                (chunk_summaries_section or "")[-500:],
            )
            
            # 6. 获取最近消息
            _dedup_async = (
                short_term_dedup_user_text
                if short_term_dedup_user_text is not None
                and str(short_term_dedup_user_text).strip()
                else user_message
            )
            recent_messages_section = await self._build_recent_messages_section(
                session_id,
                exclude_message_id,
                current_user_text=_dedup_async,
                group_recent_skip_tg_message_ids=group_recent_skip_tg_message_ids,
            )

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
                archived_daily_section,
                daily_summaries_section,
                chunk_summaries_section,
                recent_tool_section,
                tool_oral_coaching=tool_oral_coaching,
            )
            if telegram_segment_hint:
                full_system_prompt.append(
                    _cache_text_block(await format_telegram_reply_segment_hint(), cache=False)
                )
                if str(session_id).startswith("telegram_group_"):
                    full_system_prompt.append(
                        _cache_text_block(
                            await format_telegram_group_segment_directive(), cache=False
                        )
                    )

            if str(session_id).startswith("telegram_group_"):
                full_system_prompt.append(
                    _cache_text_block(TELEGRAM_GROUP_CONTINUATION_DIRECTIVE, cache=False)
                )
                full_system_prompt.append(
                    _cache_text_block(TELEGRAM_GROUP_IN_CHARACTER_DIRECTIVE, cache=False)
                )

            # TTS 语气标签注入
            tts_enabled = await get_database().get_config("tts_enabled", "false")
            logger.info("[TTS注入] tts_enabled 原始值=%r, 判断结果=%s", tts_enabled, tts_enabled.lower() in ("true", "1"))
            if tts_enabled.lower() in ("true", "1"):
                full_system_prompt.append(
                    _cache_text_block(TTS_PROMPT_BLOCK, cache=False)
                )
                logger.info("[TTS注入] TTS_PROMPT_BLOCK 已追加到 system prompt，当前共 %d 个 block", len(full_system_prompt))

            # 组装 messages 数组
            messages = self._assemble_messages(
                full_system_prompt,
                recent_messages_section,
                current_user_message
            )

            # 用实际组装后的内容计算 cacheable_ratio（包含格式化开销和工具定义）
            # 前缀缓存（SiliconFlow 等）缓存整个稳定前缀，包括 cache_control 标记前的块
            # 统计方式：从第一个块开始，到（含）第一个带 cache_control 的块为止
            system_text_len = 0
            cached_text_len = 0
            found_cache_boundary = False
            if isinstance(full_system_prompt, list):
                for block in full_system_prompt:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blen = len(block.get("text") or "")
                        system_text_len += blen
                        if not found_cache_boundary:
                            cached_text_len += blen
                            if block.get("cache_control"):
                                found_cache_boundary = True
            elif isinstance(full_system_prompt, str):
                system_text_len = len(full_system_prompt)

            messages_text_len = 0
            for m in messages:
                if m.get("role") == "system":
                    continue
                c = m.get("content", "")
                if isinstance(c, str):
                    messages_text_len += len(c)
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            messages_text_len += len(part.get("text") or "")

            total_text_len = system_text_len + messages_text_len
            cacheable_ratio = cached_text_len / total_text_len if total_text_len > 0 else 0.0

            logger.debug(f"Context 构建完成（异步）: session={session_id}, system_prompt_length={len(full_system_prompt)}, messages_count={len(messages)}")
            logger.info(
                "context built async: session=%s system_prompt_length=%s messages_count=%s daily_section_len=%s chunk_section_len=%s recent_messages=%s cacheable_ratio=%.3f",
                session_id,
                len(full_system_prompt),
                len(messages),
                len(daily_summaries_section or ""),
                len(chunk_summaries_section or ""),
                len(recent_messages_section or []),
                cacheable_ratio,
            )
            self._record_context_trace(session_id, user_message)

            return {
                "system_prompt": full_system_prompt,
                "messages": messages,
                "cacheable_ratio": cacheable_ratio,
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
                created_fmt = format_shanghai_datetime_minutes(created) or (
                    str(created) if created else ""
                )
                if not created_fmt:
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
            self._last_memory_card_dimensions = []
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
                    formatted_time = format_shanghai_datetime_minutes(updated_at) or (
                        str(updated_at) if updated_at else ""
                    )
                    if not formatted_time:
                        formatted_time = "未知时间"
                    
                    section_lines.append(f"- {card['content']} (更新于: {formatted_time})")
                
                sections.append("\n".join(section_lines))
            
            if sections:
                self._last_memory_card_dimensions = list(dimension_groups.keys())
                memory_section = "\n\n".join(sections)
                return f"# 用户记忆卡片\n\n{memory_section}"
            else:
                return ""
                
        except Exception as e:
            logger.warning(f"构建 memory cards 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            self._last_memory_card_dimensions = []
            return ""
    
    async def _build_daily_summaries_section(
        self,
        session_id: Optional[str] = None,
        daily_summaries_override: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        构建 daily summary 部分。
        
        查询 summaries 表中 summary_type='daily'，按内容日取最近 N 天（见 _context_max_daily_summaries_limit），
        然后在代码中将其翻转为正序（按时间从老到新）。
        
        Returns:
            str: daily summary 部分的文本，如果没有则返回空字符串
        """
        try:
            self._last_daily_summary_ids = []
            if daily_summaries_override is not None:
                daily_summaries = list(daily_summaries_override)
            else:
                daily_summaries = await get_recent_daily_summaries(
                    limit=await _context_max_daily_summaries_limit(),
                )

            if not daily_summaries:
                return ""

            # 翻转为正序（最旧的在前）
            daily_summaries.reverse()
            self._last_daily_summary_ids = [
                int(s["id"]) for s in daily_summaries if s.get("id") is not None
            ]

            sections = []
            for summary in daily_summaries:
                # 优先用 source_date（摘要实际代表的日期），兜底 created_at
                raw_date = summary.get('source_date') or summary.get('created_at')
                formatted_date = format_shanghai_date_iso(raw_date) or (
                    str(raw_date)[:10] if raw_date else ""
                )
                if not formatted_date:
                    formatted_date = "未知日期"
                
                sections.append(f"### {formatted_date}\n{summary['summary_text']}")
            
            if sections:
                daily_section = "\n\n".join(sections)
                return f"# 每日摘要\n\n{daily_section}"
            else:
                return ""
                
        except Exception as e:
            logger.warning(f"构建 daily summary 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            self._last_daily_summary_ids = []
            return ""

    async def _build_archived_daily_supplement_section(self, session_id: Optional[str] = None) -> str:
        """
        对长期记忆召回命中的较早日期补充 daily 概况。

        最近 daily 已由常规 daily 通道注入；这里只补召回事件涉及的窗口外日期。
        """
        try:
            self._last_archived_daily_summary_ids = []
            limit = await _context_archived_daily_limit()
            if limit <= 0 or not self._last_longterm_results:
                return ""

            recent_limit = await _context_max_daily_summaries_limit()
            recent_daily = await get_recent_daily_summaries(
                limit=recent_limit,
            )
            recent_dates = set()
            for row in recent_daily:
                raw = row.get("source_date") or row.get("created_at")
                if raw:
                    recent_dates.add(str(raw)[:10])

            grouped: Dict[str, Dict[str, Any]] = {}
            for result in self._last_longterm_results:
                md = result.get("metadata") or {}
                day = str(md.get("source_date") or md.get("date") or "")[:10]
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                    continue
                if day in recent_dates:
                    continue
                score = float(
                    result.get("fusion_score")
                    or result.get("rerank_score")
                    or result.get("score")
                    or 0.0
                )
                bucket = grouped.setdefault(day, {"hits": 0, "max_score": 0.0})
                bucket["hits"] += 1
                bucket["max_score"] = max(float(bucket["max_score"]), score)

            if not grouped:
                return ""

            min_hits = await _archived_daily_min_hits()
            priority_days = sorted(
                (item for item in grouped.items() if item[1]["hits"] >= min_hits),
                key=lambda kv: (kv[1]["hits"], kv[1]["max_score"], kv[0]),
                reverse=True,
            )
            fallback_days = sorted(
                (item for item in grouped.items() if item[1]["hits"] < min_hits),
                key=lambda kv: (kv[1]["max_score"], kv[1]["hits"], kv[0]),
                reverse=True,
            )
            selected_days = [d for d, _ in (priority_days + fallback_days)[:limit]]
            if not selected_days:
                return ""

            db = get_database()
            sections = []
            for day in selected_days:
                rows = await db.get_daily_summaries_by_date(day)
                if not rows:
                    continue
                for row in rows[:1]:
                    if row.get("id") is not None:
                        self._last_archived_daily_summary_ids.append(int(row["id"]))
                    sections.append(f"### {day}\n{row.get('summary_text') or ''}")

            if not sections:
                return ""

            return (
                "# 较早日期概况补充\n\n"
                "以下是长期记忆中涉及到的较早日期的概况补充，仅作为背景，不代表近期发生\n\n"
                + "\n\n".join(sections)
            )
        except Exception as e:
            logger.warning("构建远古 daily 补充失败: %s", e)
            self._last_archived_daily_summary_ids = []
            return ""

    async def _build_telegram_peer_recent_for_system(self, peer_session_id: str) -> str:
        """
        拉取对端 Telegram 会话的近期原文，格式化为 Markdown 列表，供拼入「今日对话摘要」块内。

        与 `_build_recent_messages_section` 使用相同的条数配置，但不剥离当前轮用户正文、
        不处理群聊缓冲 skip_tg（对端会话与本轮触发无关）。
        """
        peer = str(peer_session_id or "").strip()
        if not peer:
            return ""

        limit = await _short_term_recent_message_limit()
        overlap = await _summarized_overlap_limit()
        db = get_database()

        merged: List[Dict[str, Any]] = []
        if peer.startswith("telegram_group_"):
            chat_id = peer[len("telegram_group_") :]
            since = _short_term_context_since()
            recent_unsummarized = await db.get_unsummarized_shared_group_messages(
                chat_id, limit=limit, since=since
            )
            summarized_overlap = await db.get_recent_summarized_shared_group_messages(
                chat_id, limit=overlap, since=since
            )
            seen_ids = set()
            for msg in summarized_overlap + recent_unsummarized:
                mid = msg.get("id")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                merged.append(msg)
            deduped: List[Dict[str, Any]] = []
            for row in merged:
                if deduped:
                    prev = deduped[-1]
                    if (
                        str(prev.get("sender") or "").strip().lower()
                        == str(row.get("sender") or "").strip().lower()
                        and str(prev.get("content") or "").strip()
                        == str(row.get("content") or "").strip()
                    ):
                        continue
                deduped.append(row)
            merged = deduped
        else:
            recent_unsummarized = await get_unsummarized_messages_desc(peer, limit=limit)
            summarized_overlap = await get_recent_summarized_messages_desc(
                peer, limit=overlap
            )
            seen_ids = set()
            for msg in summarized_overlap + recent_unsummarized:
                mid = msg.get("id")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                merged.append(msg)

        if not merged:
            return ""

        def _sort_key(r: Dict[str, Any]) -> Any:
            return r.get("created_at") or ""

        merged.sort(key=_sort_key)

        user_bracket = ""
        if peer.startswith("telegram_group_"):
            user_bracket = await _group_transcript_user_bracket_label()

        lines: List[str] = []
        for row in merged:
            if peer.startswith("telegram_group_"):
                sender = str(row.get("sender") or "").strip().lower()
                speaker = _group_transcript_speaker_bracket(
                    sender, user_bracket_label=user_bracket
                )
                raw = str(row.get("content") or "").strip()
                if sender == "user":
                    text = str(
                        inject_user_sent_at_into_llm_content(
                            raw, row.get("created_at")
                        )
                    )
                else:
                    text = strip_lutopia_behavior_appendix(raw)
                text = _prepend_group_reply_to_author(str(text), row)
            else:
                msg_role = row.get("role")
                role_cn = "用户" if msg_role == "user" else "助手"
                text = format_user_message_for_context(
                    {
                        "role": row.get("role"),
                        "content": row.get("content"),
                        "media_type": row.get("media_type"),
                        "image_caption": row.get("image_caption"),
                    }
                )
                if msg_role == "user":
                    text = str(
                        inject_user_sent_at_into_llm_content(
                            text, row.get("created_at")
                        )
                    )
                    if not str(text).strip():
                        continue
                else:
                    text = strip_lutopia_behavior_appendix(text)
            if not str(text).strip():
                continue
            clock = format_shanghai_clock_24h(row.get("created_at"))
            if not clock:
                clock = "?"
            one = str(text).replace("\r\n", "\n").strip()
            if len(one) > 900:
                one = one[:900] + "…"
            if peer.startswith("telegram_group_"):
                lines.append(f"- **[{speaker}]**（{clock}）：{one}")
            else:
                lines.append(f"- **{role_cn}**（{clock}）：{one}")

        body = "\n".join(lines)
        if len(body) > _TELEGRAM_PEER_RECENT_MAX_CHARS:
            kept: List[str] = []
            total = 0
            for ln in reversed(lines):
                add = len(ln) + (1 if kept else 0)
                if total + add > _TELEGRAM_PEER_RECENT_MAX_CHARS:
                    break
                kept.append(ln)
                total += add
            kept.reverse()
            body = (
                "（以下摘录因长度限制从最早处截断，保留较近对话。）\n"
                + "\n".join(kept)
            )
        return body.strip()
    
    async def _build_chunk_summaries_section(self, session_id: str) -> str:
        """
        构建 chunk summary 部分。
        
        查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选）。
        在拼入时，附带其来源标识（格式如 [来自频道 {session_id}]: 摘要内容），按时间正序拼入。
        
        Returns:
            str: chunk summary 部分的文本，如果没有则返回空字符串
        """
        try:
            self._last_chunk_summary_ids = []
            chunk_summaries = await get_today_chunk_summaries()

            peer_id = await _telegram_cross_context_peer_session_id(session_id)
            peer_text = ""
            if peer_id:
                try:
                    peer_text = await self._build_telegram_peer_recent_for_system(peer_id)
                except Exception as e:
                    logger.warning("构建 Telegram 对端近期原文失败: %s", e)
                    peer_text = ""
            peer_plain = (peer_text or "").strip()

            if not chunk_summaries and not peer_plain:
                return ""

            if not chunk_summaries:
                self._last_chunk_summary_ids = []
                current_is_group_only = str(session_id or "").startswith(
                    "telegram_group_"
                )
                hdr = (
                    "## 私聊近期原文（Telegram 私聊，与当前群聊不同会话，仅作情境参考）"
                    if current_is_group_only
                    else "## 群聊近期原文（Telegram 群聊，与当前私聊不同会话，仅作情境参考）"
                )
                chunk_section = "\n\n".join([hdr, peer_plain])
                today = now_shanghai().strftime("%Y年%m月%d日")
                title = f"# 今日对话摘要（{today}，东八区日历）"
                body = f"{TELEGRAM_CROSS_CHANNEL_PEER_DIRECTIVE}\n\n{chunk_section}"
                return f"{title}\n\n{body}"

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
            self._last_chunk_summary_ids = [
                int(s["id"]) for s in chunk_summaries if s.get("id") is not None
            ]
            
            private_sections: List[str] = []
            group_sections: List[str] = []
            private_summaries: List[Dict[str, Any]] = []
            group_summaries: List[Dict[str, Any]] = []
            for summary in chunk_summaries:
                # 格式化创建时间（东八区 24 小时制，避免与 UTC 串混淆）
                created_at_raw = summary["created_at"]
                formatted_time = format_shanghai_clock_24h(created_at_raw)
                if not formatted_time:
                    formatted_time = "未知时间"

                sum_session_id = summary["session_id"]
                # 简化 session_id 显示
                if "_" in sum_session_id:
                    parts = sum_session_id.split("_")
                    if len(parts) >= 2:
                        display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                    else:
                        display_session = sum_session_id[:20]
                else:
                    display_session = sum_session_id[:20]

                item = (
                    f"### {formatted_time}（东八区） [来自: {display_session}]\n"
                    f"{summary['summary_text']}"
                )
                sid = str(summary.get("session_id") or "")
                if sid.startswith("telegram_group_"):
                    group_sections.append(item)
                    group_summaries.append(summary)
                else:
                    private_sections.append(item)
                    private_summaries.append(summary)

            sections: List[str] = []
            current_is_group = str(session_id or "").startswith("telegram_group_")
            if current_is_group:
                # 群聊会话：把私聊块放前，群聊块贴近末尾；对端私聊原文插在私聊 chunk 与群聊 chunk 之间
                if private_sections:
                    sections.append("## 私聊摘要")
                    sections.append(
                        format_created_at_range_preamble(
                            private_summaries,
                            heading="【私聊 chunk 时间范围】",
                            semantics_note="summaries.created_at 为各 chunk 摘要写入库时间",
                        ).strip()
                    )
                    sections.append("\n\n".join(private_sections))
                if peer_plain:
                    sections.append(
                        "## 私聊近期原文（Telegram 私聊，与当前群聊不同会话，仅作情境参考）\n\n"
                        + peer_plain
                    )
                if group_sections:
                    sections.append("## 群聊摘要")
                    sections.append(await _telegram_group_chunk_viewpoint_line())
                    sections.append(
                        format_created_at_range_preamble(
                            group_summaries,
                            heading="【群聊 chunk 时间范围】",
                            semantics_note="summaries.created_at 为各 chunk 摘要写入库时间",
                        ).strip()
                    )
                    sections.append("\n\n".join(group_sections))
            else:
                # 私聊会话：把群聊块放前，私聊块贴近末尾；对端群聊原文插在群聊 chunk 与私聊 chunk 之间
                if group_sections:
                    sections.append("## 群聊摘要")
                    sections.append(await _telegram_group_chunk_viewpoint_line())
                    sections.append(
                        format_created_at_range_preamble(
                            group_summaries,
                            heading="【群聊 chunk 时间范围】",
                            semantics_note="summaries.created_at 为各 chunk 摘要写入库时间",
                        ).strip()
                    )
                    sections.append("\n\n".join(group_sections))
                if peer_plain:
                    sections.append(
                        "## 群聊近期原文（Telegram 群聊，与当前私聊不同会话，仅作情境参考）\n\n"
                        + peer_plain
                    )
                if private_sections:
                    sections.append("## 私聊摘要")
                    sections.append(
                        format_created_at_range_preamble(
                            private_summaries,
                            heading="【私聊 chunk 时间范围】",
                            semantics_note="summaries.created_at 为各 chunk 摘要写入库时间",
                        ).strip()
                    )
                    sections.append("\n\n".join(private_sections))

            if sections:
                chunk_section = "\n\n".join(sections)
                today = now_shanghai().strftime("%Y年%m月%d日")
                title = f"# 今日对话摘要（{today}，东八区日历）"
                if peer_plain:
                    body = f"{TELEGRAM_CROSS_CHANNEL_PEER_DIRECTIVE}\n\n{chunk_section}"
                else:
                    body = chunk_section
                return f"{title}\n\n{body}"
            else:
                return ""
                
        except Exception as e:
            logger.warning(f"构建 chunk summary 部分失败: {e}")  # 可恢复/已兜底，降为 warning
            self._last_chunk_summary_ids = []
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
                if len(summary) > 150:
                    summary = summary[:150] + "..."
                prefix = f"- {nm}"
                if arg_text:
                    prefix += f"（{arg_text}）"
                lines.append(f"{prefix}：{summary or '已执行，但没有可用摘要'}")
            return "\n".join(lines).strip()
        except Exception as e:
            logger.warning("构建最近工具使用记录失败: %s", e)
            return ""
    
    async def _build_vector_search_section(self, session_id: str, user_message: str) -> str:
        """
        构建向量检索部分（同步，无 Cohere 精排）。
        
        双路融合后经时间衰减融合与 MMR 多样性筛选，注入时每条正文前带 [uid:doc_id]。
        """
        try:
            self._last_longterm_results = []
            if not config.ZHIPU_API_KEY or config.ZHIPU_API_KEY == "your_zhipu_api_key_here":
                logger.warning("ZHIPU_API_KEY 未设置或为默认值，跳过向量检索")
                return ""

            multi_turn_query = await _build_rerank_query(session_id, user_message)
            if multi_turn_query == "":
                return ""

            tk = await _retrieval_top_k()
            cutoff_date = await _longterm_date_cutoff_iso()
            lt_where = chroma_where_longterm_summary_types(user_message)
            lt_types = longterm_allowed_summary_types(user_message)
            vector_results = search_memory(
                multi_turn_query, top_k=tk, where=lt_where
            )
            bm25_results = search_bm25(
                multi_turn_query,
                top_k=tk,
                allowed_summary_types=lt_types,
            )
            all_results = _merge_vector_bm25_dedupe(
                vector_results, bm25_results, max(1, 2 * tk)
            )
            if cutoff_date:
                all_results = [
                    r for r in all_results if _longterm_result_before_cutoff(r, cutoff_date)
                ]
            n_long = await _context_max_longterm_count()
            fused = fuse_rerank_with_time_decay(
                all_results,
                await _starred_boost_factor_value(),
            )
            fused = _hydrate_candidate_embeddings(fused)
            all_results = apply_mmr(fused, await _mmr_lambda_value(), n_long)
            self._last_longterm_results = list(all_results)

            if not all_results:
                logger.debug("双路检索未找到相关记忆")
                self._last_longterm_results = []
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
            self._last_longterm_results = []
            return ""
    
    async def _build_vector_search_section_async(
        self, user_message: str, session_id: str = ""
    ) -> str:
        """
        C3 新链路：并行双路检索 → SiliconFlow Rerank → 阈值过滤 →
        event_type 分级时间衰减 + starred boost → MMR → 取 top N。

        异常降级：rerank 超时或失败时走旧的 fuse_rerank_with_time_decay 路径。
        """
        from memory.reranker import rerank as sf_rerank, RerankFallbackException

        try:
            self._last_longterm_results = []

            tk = await _retrieval_top_k()
            n_long = await _context_max_longterm_count()
            candidate_cap = await _rerank_candidate_size()

            # 1) 构建 rerank query（从最近对话拼接）
            rerank_query = await _build_rerank_query(session_id, user_message)
            if not rerank_query.strip():
                rerank_query = user_message
            logger.debug("rerank query (%d chars): %s", len(rerank_query), rerank_query[:100])

            # 2) 并行双路检索
            import asyncio
            loop = asyncio.get_event_loop()
            cutoff_date = await _longterm_date_cutoff_iso()
            lt_where = chroma_where_longterm_summary_types(user_message)
            lt_types = longterm_allowed_summary_types(user_message)
            vector_future = loop.run_in_executor(
                None, partial(search_memory, rerank_query, tk, lt_where)
            )
            bm25_future = loop.run_in_executor(
                None, partial(search_bm25, rerank_query, tk, lt_types)
            )
            vector_results, bm25_results = await asyncio.gather(vector_future, bm25_future)
            logger.debug(
                "并行检索完成，向量: %d 条，BM25: %d 条",
                len(vector_results), len(bm25_results),
            )

            # 3) 去重合并
            all_results = _merge_vector_bm25_dedupe(
                vector_results, bm25_results, candidate_cap
            )
            if cutoff_date:
                all_results = [
                    r for r in all_results if _longterm_result_before_cutoff(r, cutoff_date)
                ]
            if not all_results:
                logger.debug("双路检索未找到相关记忆")
                return ""

            # 4) 调用 SiliconFlow Rerank
            rerank_ok = False
            timeout = await _rerank_timeout_sec()
            enabled = await _rerank_enabled()

            if enabled:
                try:
                    await sf_rerank(rerank_query, all_results, timeout=timeout)
                    rerank_ok = True
                    logger.debug("Rerank 成功，候选 %d 条有分数", len(all_results))
                except RerankFallbackException as e:
                    logger.warning("Rerank 降级: %s", e)
                except Exception as e:
                    logger.warning("Rerank 异常: %s", e)

            if not rerank_ok:
                # 降级：走旧的 fuse_rerank_with_time_decay
                logger.info("降级到旧的 fuse_rerank_with_time_decay 路径")
                fused = fuse_rerank_with_time_decay(
                    all_results, await _starred_boost_factor_value()
                )
                fused = _hydrate_candidate_embeddings(fused)
                top_results = apply_mmr(fused, await _mmr_lambda_value(), n_long)
                self._last_longterm_results = list(top_results)
                return self._format_longterm_section(top_results, len(all_results), "降级精排")

            # 5) 阈值过滤（用 rerank 纯语义分，不混入加权）
            score_floor = await _rerank_score_floor()
            starred_floor = await _rerank_starred_floor()
            passed = []
            for c in all_results:
                rs = c.get("rerank_score", 0.0)
                if _is_starred(c.get("metadata") or {}):
                    if rs >= starred_floor:
                        passed.append(c)
                else:
                    if rs >= score_floor:
                        passed.append(c)
            logger.debug(
                "阈值过滤: %d → %d (floor=%.2f, starred_floor=%.2f)",
                len(all_results), len(passed), score_floor, starred_floor,
            )
            if not passed:
                logger.debug("阈值过滤后无候选，返回空")
                self._last_longterm_results = []
                return ""

            # 6) 加权阶段：
            # fusion_score = (w * rerank_score + (1-w) * norm_decay_score) * starred_boost
            now_ts = time.time()
            starred_boost = await _starred_boost_factor_value()
            blend_weight = await _rerank_blend_weight_value()
            decay_scores = [
                await _rerank_success_decay_score(c.get("metadata") or {}, now_ts)
                for c in passed
            ]
            dmin, dmax = min(decay_scores), max(decay_scores)

            def norm_decay(i: int) -> float:
                if dmax <= dmin:
                    return 1.0
                return (decay_scores[i] - dmin) / (dmax - dmin)

            for i, c in enumerate(passed):
                md = c.get("metadata") or {}
                rs = max(0.0, min(1.0, float(c.get("rerank_score", 0.0) or 0.0)))
                boost = starred_boost if _is_starred(md) else 1.0
                c["decay_score"] = decay_scores[i]
                c["fusion_score"] = (
                    blend_weight * rs + (1.0 - blend_weight) * norm_decay(i)
                ) * boost

            passed.sort(key=lambda x: x.get("fusion_score", 0.0), reverse=True)

            # 7) MMR 多样性筛选
            passed = _hydrate_candidate_embeddings(passed)
            top_results = apply_mmr(passed, await _mmr_lambda_value(), n_long)
            self._last_longterm_results = list(top_results)

            return self._format_longterm_section(top_results, len(all_results), "Rerank精排")

        except Exception as e:
            logger.warning("构建向量检索部分失败（异步）: %s", e)
            self._last_longterm_results = []
            logger.warning("回退到同步检索")
            return await self._build_vector_search_section(session_id, user_message)

    def _format_longterm_section(
        self, results: List[Dict[str, Any]], total_candidates: int, label: str
    ) -> str:
        """格式化长期记忆注入段落。"""
        if not results:
            return ""

        sections = []
        for i, result in enumerate(results):
            text = (result.get("text") or "").strip()
            doc_id = result.get("id") or ""
            metadata = result.get("metadata") or {}
            fusion = float(result.get("fusion_score", 0.0))
            rerank_sc = result.get("rerank_score")
            retrieval_method = result.get("retrieval_method", "unknown")
            date = metadata.get("date", "未知日期")
            summary_type = metadata.get("summary_type", "未知类型")
            session_id_meta = metadata.get("session_id", "未知会话")
            if "_" in str(session_id_meta):
                parts = str(session_id_meta).split("_")
                if len(parts) >= 2:
                    display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                else:
                    display_session = str(session_id_meta)[:20]
            else:
                display_session = str(session_id_meta)[:20]
            method_label = "向量" if retrieval_method == "vector" else "关键词"
            score_info = f"综合分:{fusion:.4f}"
            if rerank_sc is not None:
                score_info += f" rerank:{rerank_sc:.4f}"
            body = f"[uid:{doc_id}] {text}"
            sections.append(
                f"### 相关记忆 {i+1} ({method_label}检索，{score_info})\n"
                f"日期: {date} | 类型: {summary_type} | 来源: {display_session}\n{body}"
            )

        vector_section = "\n\n".join(sections)
        vector_section += (
            f"\n\n<!-- {label}：自 {total_candidates} 条候选取 {len(results)} 条 -->"
        )
        return f"# 相关长期记忆（{label}结果）\n\n{vector_section}"
    
    async def _build_recent_messages_section(
        self,
        session_id: str,
        exclude_message_id: Optional[int] = None,
        current_user_text: Optional[str] = None,
        group_recent_skip_tg_message_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
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
            if str(session_id).startswith("telegram_group_"):
                chat_id = str(session_id)[len("telegram_group_") :]
                db = get_database()
                since = _short_term_context_since()
                recent_unsummarized = await db.get_unsummarized_shared_group_messages(
                    chat_id,
                    limit=await _short_term_recent_message_limit(),
                    since=since,
                )
                summarized_overlap = await db.get_recent_summarized_shared_group_messages(
                    chat_id,
                    limit=await _summarized_overlap_limit(),
                    since=since,
                )
                merged: List[Dict[str, Any]] = []
                seen_ids = set()
                for msg in summarized_overlap + recent_unsummarized:
                    mid = msg.get("id")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    merged.append(msg)
                # 群聊共享表中若出现近邻重复写入（同 sender + 同 content），在上下文侧做一次稳妥去重。
                deduped: List[Dict[str, Any]] = []
                for row in merged:
                    if deduped:
                        prev = deduped[-1]
                        if (
                            str(prev.get("sender") or "").strip().lower()
                            == str(row.get("sender") or "").strip().lower()
                            and str(prev.get("content") or "").strip()
                            == str(row.get("content") or "").strip()
                        ):
                            continue
                    deduped.append(row)
                merged = deduped
                # 缓冲合并多句时：共享表多行、本轮 combined_raw 一行，无法用整段正文与单行比对。
                # 用本轮涉及的 Telegram message_id 从尾部剥离对应 user 行。
                skip_tg = set()
                if group_recent_skip_tg_message_ids:
                    for _tid in group_recent_skip_tg_message_ids:
                        _s = str(_tid).strip()
                        if _s:
                            skip_tg.add(_s)
                # 不能只从尾部弹：双 bot 时「未摘要」里最后一条常是上一位助手的回复，
                # 本轮用户行在倒数位置，尾部循环会误判为已剥离而保留重复。
                if skip_tg and merged:
                    merged = [
                        row
                        for row in merged
                        if not (
                            str(row.get("sender") or "").strip().lower() == "user"
                            and str(row.get("tg_message_id") or "").strip() in skip_tg
                        )
                    ]
                # 群聊路径避免把当前轮用户输入同时作为「历史」和「当前消息」重复注入。
                # 从尾部连续移除与本轮正文一致的用户行（共享表里偶发多条同文重复时也能收掉）。
                target = str(current_user_text or "").strip()
                if target and merged:
                    while merged:
                        last = merged[-1]
                        if (
                            str(last.get("sender") or "").strip().lower() == "user"
                            and str(last.get("content") or "").strip() == target
                        ):
                            merged.pop()
                        else:
                            break
                if not merged:
                    return []
                user_bracket = await _group_transcript_user_bracket_label()
                out: List[Dict[str, Any]] = []
                for row in merged:
                    sender = str(row.get("sender") or "").strip().lower()
                    content = str(row.get("content") or "")
                    if not content:
                        continue
                    label = _group_transcript_speaker_bracket(
                        sender, user_bracket_label=user_bracket
                    )
                    if sender == "user":
                        body = str(
                            inject_user_sent_at_into_llm_content(
                                content, row.get("created_at")
                            )
                        ).strip()
                    else:
                        body = str(
                            strip_lutopia_behavior_appendix(content)
                        ).strip()
                    body = _prepend_group_reply_to_author(body, row)
                    if not body:
                        continue
                    out.append(
                        {
                            "role": "user",
                            "content": f"[{label}]\n{body}",
                        }
                    )
                return out

            recent_messages = await get_unsummarized_messages_desc(
                session_id,
                limit=await _short_term_recent_message_limit(),
            )
            summarized_overlap = await get_recent_summarized_messages_desc(
                session_id,
                limit=await _summarized_overlap_limit(),
            )

            merged: List[Dict[str, Any]] = []
            seen_ids = set()
            for msg in summarized_overlap + recent_messages:
                mid = msg.get("id")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                merged.append(msg)

            if not merged:
                return []

            # 转换为 LLM 接口期望的格式
            messages = []
            for msg in merged:
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
                if role == "assistant":
                    text = strip_lutopia_behavior_appendix(text)
                else:
                    text = inject_user_sent_at_into_llm_content(
                        text, msg.get("created_at")
                    )
                messages.append({
                    "role": role,
                    "content": text
                })

            logger.debug(
                f"获取最近消息（含已摘要重叠）: session={session_id}, count={len(messages)}"
            )
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
        archived_daily_section: str,
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
        _now = now_shanghai()
        _weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        now_str = f"{_now.year}年{_now.month}月{_now.day}日{_weekdays[_now.weekday()]} {_now.strftime('%H:%M')}"
        time_section = (
            f"【当前系统时间（东八区24小时制）：{now_str}】\n"
            f"(提示：在对话中若关注时间信息，请以此时间为基准；写钟点请用24小时制或写明上午/下午。)"
        )

        fixed_sections = [system_prompt, MEMORY_BLOCK_PRIORITY_DIRECTIVE]
        fixed_sections.append(MEMORY_CITATION_DIRECTIVE)
        fixed_sections.append(THINKING_LANGUAGE_DIRECTIVE)
        if tool_oral_coaching:
            fixed_sections.append(TOOL_ORAL_COACHING_BLOCK)

        blocks: List[Dict[str, Any]] = [
            _cache_text_block("\n\n".join(s for s in fixed_sections if s.strip()), cache=False)
        ]

        # 稳定部分（cache 边界内）：变化频率低，适合前缀缓存
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

        # 向量检索 + 远古 daily 依赖检索结果，每次请求不同，放在 cache 边界之后
        variable_sections: List[str] = []
        if vector_search_section:
            variable_sections.append(vector_search_section)
        if archived_daily_section:
            variable_sections.append(archived_daily_section)
        if variable_sections:
            blocks.append(_cache_text_block("\n\n".join(variable_sections), cache=False))

        if chunk_summaries_section:
            blocks.append(_cache_text_block(chunk_summaries_section, cache=False))

        dynamic_sections = [time_section]
        if recent_tool_section:
            dynamic_sections.append(recent_tool_section)

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
    short_term_dedup_user_text: Optional[str] = None,
    group_recent_skip_tg_message_ids: Optional[Sequence[str]] = None,
    daily_summaries_override: Optional[List[Dict[str, Any]]] = None,
    skip_vector_search: bool = False,
) -> Dict[str, Any]:
    """
    构建对话上下文的便捷函数。
    
    Args:
        session_id: 会话ID
        user_message: 用户当前消息
        images: 当前轮图片 payload（可选）
        llm_user_text: 对话模型用纯文本（可选，有图片时建议传入）
        telegram_segment_hint: 为 True 时追加 Telegram HTML 白名单与 ||| 分段死指令（仅 Telegram 缓冲路径建议开启）
        tool_oral_coaching: 为 True 时追加「工具调用前口播」引导（与启用 OpenAI tools 的请求对齐）
        
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
        short_term_dedup_user_text=short_term_dedup_user_text,
        group_recent_skip_tg_message_ids=group_recent_skip_tg_message_ids,
        daily_summaries_override=daily_summaries_override,
        skip_vector_search=skip_vector_search,
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
