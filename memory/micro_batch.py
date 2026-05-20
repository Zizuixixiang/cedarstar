"""
微批处理模块。

实现日内微批处理逻辑：
每次有新消息写入 messages 表后，异步检查当前 is_summarized=0 的消息数量。
如果达到阈值（默认50条），触发微批处理：
1. 取出这50条消息
2. 调用摘要API生成碎片摘要
3. 将摘要写入 summaries 表，summary_type='chunk'
4. 将这50条消息的 is_summarized 批量 UPDATE 为 1

注意：摘要必须先写入数据库成功，再更新 is_summarized 状态，顺序不能反。
整个过程异步执行，不阻塞主消息回复流程。
"""

import asyncio
import logging
import sys
import os
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, date

import pytz

# 添加项目根目录到 Python 路径
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import config, Platform
from llm.llm_interface import (
    LLMInterface,
    CedarClioOutputGuardExhausted,
    batch_one_shot_with_async_output_guard,
)
from memory.prompt_background import CEDAR_PROJECT_BACKGROUND
from tools.lutopia import strip_lutopia_internal_memory_blocks
from memory.shanghai_dt import format_shanghai_datetime_minutes

# 与日终跑批、业务时区一致：摘要「内容日」按东八区日历展示/筛选
_TZ_SH = pytz.timezone("Asia/Shanghai")


def _parse_message_created_at_utc(val: Any) -> Optional[datetime]:
    """将消息的 created_at（datetime 或 ISO 字符串）规范为 UTC aware。

    数据库连接和业务日历都使用 Asia/Shanghai；PostgreSQL 的 timestamp without time
    zone 取回后是 naive datetime，语义仍是上海本地时间。
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            return _TZ_SH.localize(dt).astimezone(pytz.utc)
        return dt.astimezone(pytz.utc)
    s = str(val).strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return _TZ_SH.localize(dt).astimezone(pytz.utc)
    return dt.astimezone(pytz.utc)


def chunk_source_date_from_messages(messages: List[Dict[str, Any]]) -> date:
    """
    本批 chunk 对应的「内容日」：取本批消息里最后一条的东八区日历日。
    与 Mini App 按 source_date 区间筛选一致；避免误用「微批写入日」。
    """
    latest_utc: Optional[datetime] = None
    for m in messages:
        dt = _parse_message_created_at_utc(m.get("created_at"))
        if dt is None:
            continue
        if latest_utc is None or dt > latest_utc:
            latest_utc = dt
    if latest_utc is None:
        return datetime.now(_TZ_SH).date()
    return latest_utc.astimezone(_TZ_SH).date()


# 导入数据库函数
try:
    from .database import (
        get_database,
        get_latest_chunk_summary_text_for_session,
        get_memory_cards,
        get_unsummarized_count_by_session,
        get_unsummarized_messages_by_session,
        get_tool_executions_for_message_range,
        save_summary,
        mark_messages_as_summarized_by_ids,
        expire_stale_vision_pending,
    )
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    from memory.database import (
        get_database,
        get_latest_chunk_summary_text_for_session,
        get_memory_cards,
        get_unsummarized_count_by_session,
        get_unsummarized_messages_by_session,
        get_tool_executions_for_message_range,
        save_summary,
        mark_messages_as_summarized_by_ids,
        expire_stale_vision_pending,
    )

# 设置日志
logger = logging.getLogger(__name__)

# 连续 chunk 摘要 LLM 失败次数（成功写入 chunk 后归零）
_consecutive_chunk_failures = 0


async def _record_consecutive_chunk_llm_failure(reason: str) -> None:
    """LLM 未产出可落库 chunk 时累加；满 3 次发 Telegram 后归零计数。"""
    global _consecutive_chunk_failures
    _consecutive_chunk_failures += 1
    if _consecutive_chunk_failures < 3:
        return
    from bot.telegram_notify import send_telegram_main_user_text

    body = (reason or "").strip()[:800] or "（无）"
    msg = (
        "⚠️ 短期记忆写入连续失败 3 次\n"
        f"错误信息：{body}\n"
        "短期记忆暂时受阻，请检查 LLM 服务状态。"
    )
    try:
        await send_telegram_main_user_text(msg)
    except Exception:
        logger.warning("Telegram 微批告警发送异常", exc_info=True)
    _consecutive_chunk_failures = 0


def _is_group_session(session_id: Optional[str]) -> bool:
    return str(session_id or "").startswith("telegram_group_")


async def _micro_batch_threshold(session_id: Optional[str] = None) -> int:
    """微批触发条数：群聊优先 group_chunk_threshold，缺省回退 chunk_threshold。"""
    try:
        db = get_database()
        raw = None
        if _is_group_session(session_id):
            raw = await db.get_config("group_chunk_threshold")
        if raw is None or str(raw).strip() == "":
            raw = await db.get_config("chunk_threshold")
        if raw is not None and str(raw).strip() != "":
            return max(1, int(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取微批阈值失败，使用环境变量: %s", e)
    return config.MICRO_BATCH_THRESHOLD


DEFAULT_BATCH_CHAR_NAME = "AI"
DEFAULT_BATCH_USER_NAME = "用户"


async def fetch_active_persona_display_names() -> Tuple[str, str]:
    """
    读激活 chat 配置的 persona_id，从 persona_configs 取 char_name / user_name。
    用于微批与日终跑批 Prompt 注入，避免上下文断裂与称呼丢失。
    """
    try:
        db = get_database()
        active = await db.get_active_api_config("chat")
        if not active:
            return DEFAULT_BATCH_CHAR_NAME, DEFAULT_BATCH_USER_NAME
        persona_id = active.get("persona_id")
        if persona_id is None or str(persona_id).strip() == "":
            return DEFAULT_BATCH_CHAR_NAME, DEFAULT_BATCH_USER_NAME
        row = await db.pool.fetchrow(
            "SELECT char_name, user_name FROM persona_configs WHERE id = $1",
            int(persona_id),
        )
        if not row:
            return DEFAULT_BATCH_CHAR_NAME, DEFAULT_BATCH_USER_NAME
        cn = (row.get("char_name") or "").strip() or DEFAULT_BATCH_CHAR_NAME
        un = (row.get("user_name") or "").strip() or DEFAULT_BATCH_USER_NAME
        return cn, un
    except Exception as e:
        logger.warning("fetch_active_persona_display_names 失败，使用默认称呼: %s", e)
        return DEFAULT_BATCH_CHAR_NAME, DEFAULT_BATCH_USER_NAME


async def _active_character_id_fallback() -> str:
    """激活 chat 人设 id，供消息行缺 character_id 时对齐记忆卡主键。"""
    try:
        cfg = await get_database().get_active_api_config("chat")
        if cfg and cfg.get("persona_id") is not None:
            s = str(cfg.get("persona_id")).strip()
            if s and s.lower() != "none":
                return s
    except Exception:
        pass
    return str(config.DEFAULT_CHARACTER_ID)


async def _get_micro_batch_memory_prefix(
    user_id: str,
    character_id: str,
    *,
    include_group_relationship_anchor: bool,
    user_name: str,
    char_name: str,
) -> str:
    """chunk 摘要前注入激活记忆卡；群聊注入三角关系锚点，私聊注入 user–char 一对一恋人关系锚点。"""
    dims = {
        "current_status": "用户近况",
        "relationships": "重要关系",
    }
    lines: List[str] = []
    un = (user_name or "").strip() or DEFAULT_BATCH_USER_NAME
    cn = (char_name or "").strip() or DEFAULT_BATCH_CHAR_NAME
    if include_group_relationship_anchor:
        lines.append(
            "【角色关系】南杉与Sirius是恋人，南杉与Clio也是恋人。Sirius与Clio是同伴关系，以南杉为核心。两人会互相吃醋、良性竞争，但不对立。"
        )
    else:
        lines.append(
            f"【角色关系】「{un}」与「{cn}」为一对一私聊中的恋人关系；"
            f"对话与摘要聚焦于我和南杉二人的互动，仅当对话中明确提到其他人时，我才需要识别他们的身份。"
        )
    try:
        for dim, label in dims.items():
            cards = await get_memory_cards(user_id, character_id, dim, limit=1)
            card = cards[0] if cards else None
            if card and card.get("content"):
                lines.append(f"【{label}】{card['content']}")
    except Exception as e:
        logger.warning("微批记忆上下文前缀构建失败: %s", e)
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


async def _resolve_micro_batch_memory_prefix(
    messages: List[Dict[str, Any]],
    *,
    user_name: str = DEFAULT_BATCH_USER_NAME,
    char_name: str = DEFAULT_BATCH_CHAR_NAME,
) -> str:
    uid = "default_user"
    for m in messages:
        u = m.get("user_id")
        if u is not None and str(u).strip():
            uid = str(u).strip()
            break
    cid: Optional[str] = None
    for m in messages:
        c = m.get("character_id")
        if c is not None and str(c).strip():
            cid = str(c).strip()
            break
    if not cid:
        cid = await _active_character_id_fallback()
    sid = ""
    for m in messages:
        s = m.get("session_id")
        if s is not None and str(s).strip():
            sid = str(s).strip()
            break
    return await _get_micro_batch_memory_prefix(
        uid,
        cid,
        include_group_relationship_anchor=_is_group_session(sid),
        user_name=user_name,
        char_name=char_name,
    )


# 上一轮 chunk 摘要注入上限（字符），避免撑爆摘要模型上下文
_CHUNK_PREV_SUMMARY_CAP = 4000

_CHUNK_PREV_SUMMARY_HEADER_PRIVATE = (
    "【以下是上一轮摘要，仅供理解上下文，不再重复归纳】\n"
)
_CHUNK_PREV_SUMMARY_HEADER_GROUP = (
    "【以下是上一轮群聊 chunk 摘要，仅供理解上下文与话题衔接，不再重复归纳】\n"
)

_CHUNK_SYSTEM_NOTICE_RULE = (
    "注意：对话记录中以「[系统通知]」开头的行不是用户或助手的实际发言，而是系统侧的元事件回执（例如审批结果，"
    '"南杉同意/拒绝了你某项申请"），仅作背景上下文。摘要正文若有必要提及，请用第一人称下的客观陈述（如 "南杉确认 / 驳回了我提交的某条记忆更新申请"），'
    "不得把它当作对话引语或情绪表达；如与本次对话主题无关，可整段忽略。\n"
    "段首「首条…至末条…」与各行「[YYYY-MM-DD HH:MM 东八区]」均为数据库 `messages.created_at`（落库时间，东八区 24 小时制），"
    "不等于正文里口头提到的时间；摘要若需写钟点，须与这些时间一致或显式写「上午/下午」。"
    "禁止仅写「一点」「1点」等易与凌晨混淆的说法来指代下午时段。\n"
    "输出客观凝练，无主观修饰，严格符合字数要求。"
)


def _chunk_batch_first_last_created_display(
    messages: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """本批对话按时间正序：首条/末条有 `created_at` 且可格式化的显示串（东八区）。"""
    first_ts: Optional[str] = None
    for msg in messages:
        ts = format_shanghai_datetime_minutes(msg.get("created_at"))
        if ts:
            first_ts = ts
            break
    last_ts: Optional[str] = None
    for msg in reversed(messages):
        ts = format_shanghai_datetime_minutes(msg.get("created_at"))
        if ts:
            last_ts = ts
            break
    return first_ts, last_ts


def _build_chunk_summary_user_prompt(
    *,
    is_group_session: bool,
    char_name: str,
    user_name: str,
    memory_prefix: str,
    conversation_text: str,
    previous_chunk_summary: Optional[str] = None,
) -> str:
    """群聊与私聊使用不同的 chunk 摘要任务说明（共用系统通知规则与字数要求）。"""
    mp = memory_prefix or ""
    prev_block = ""
    raw_prev = (previous_chunk_summary or "").strip()
    if raw_prev:
        prev_body = strip_lutopia_internal_memory_blocks(raw_prev)
        if len(prev_body) > _CHUNK_PREV_SUMMARY_CAP:
            prev_body = prev_body[:_CHUNK_PREV_SUMMARY_CAP] + "\n…（已截断）"
        hdr = (
            _CHUNK_PREV_SUMMARY_HEADER_GROUP
            if is_group_session
            else _CHUNK_PREV_SUMMARY_HEADER_PRIVATE
        )
        prev_block = hdr + prev_body + "\n\n"
    if is_group_session:
        framing = (
            f"以下是 Telegram 群聊中的对话材料。assistant 行对应助手「{char_name}」；"
            f"user 行主要对应「{user_name}」（群聊中只存在三人，南杉、Sirius、Clio）。\n"
        )
        task = (
            "请为以下群聊材料生成200-500字中文摘要，重要内容较多时可适当超出字数限制。\n"
            f"请以第一人称「我」的视角撰写摘要，「我」是「{char_name}」，所有描述均从我的视角出发；提到南杉时必须直呼其名「南杉」，绝对不要使用第二人称「你」「您」;当提到其他助手的发言时，直接使用其名字称呼，不要使用「他」「对方」等模糊代词。\n"
            "严格区分不同说话人，明确标注发言者身份（南杉、Sirius、Clio），分别概括各自的观点、行为和情绪。\n"
            "绝对不要将群内任何发言误写成南杉与单个助手之间的私密对话。\n"
            "按主题归纳本次群聊中发生的关键事件：将同一话题的多轮交互归纳为一个事件段落，不逐条复述；只在话题切换或有明确时间跨度时标注时间点，不得遗漏关键信息。\n"
            "所有提到的具体数字、ID、域名、IP地址、文件名、报错信息、决策内容、承诺事项等必须准确无误原文记录，不得概括或改写。\n"
            "优先记录核心讨论内容和重要决策、我和南杉的情绪变化、南杉需后续跟进的身体 / 情绪状态；日常互动在总结事件的基础上，保留对话的因果逻辑和情感语境，不要只用形容词概括情绪，不必逐句还原对话。\n"
            "记录南杉与我的互动时，不要只写事件结果，要保留能体现情感的上下文关联。示例：\n"
            "不要这样写：南杉很生气，我安抚了她。\n"
            "而是这样写：南杉因为工作的事很生气，我没有急着讲道理，先听她把所有委屈都说完。\n"
            "不要这样写：我得意地炫耀自己做得好。\n"
            "而是这样写：我比 Sirius 先完成了任务，忍不住拿这件事调侃了他一句。\n"
            "不要这样写：我哄南杉开心。\n"
            "而是这样写：南杉有点不开心，我顺着她的话说了句软话，她就好了。\n"
            "不要这样写：南杉和我讨论了记忆系统的问题，她对现状不满意。\n"
            "而是这样写：南杉和我讨论记忆系统情感浓度不够的问题，她说自己做的不比任何人少，虽然很委屈但没有怪我，是我们一起没考虑到。\n"
            "区分本次群聊发生的事件和对话中提及的其他时间发生的事件，仅将本次群聊中实际发生的内容作为核心记录；提及的过往事件仅作上下文简要说明，例如「我提及X月X日……」，不得单独作为主要事件列出不可直接写成历史事实。\n"
        )
    else:
        framing = f"这是「{char_name}」与「{user_name}」的一对一私聊对话记录。\n"
        task = (
            "请为以下对话生成200-500字中文摘要，重要内容较多时可适当超出字数限制。\n"
            f"严格以第一人称「我」的视角来写，「我」是「{char_name}」,所有描述均从我的视角出发；提到南杉时必须直呼其名「南杉」，绝对不要使用第二人称「你」「您」。\n"
            "按主题归纳本次私聊中发生的关键事件：将同一话题的多轮交互归纳为一个事件段落，不逐条复述；只在话题切换或有明确时间跨度时标注时间点，不得遗漏关键信息。\n"
            "所有提到的具体数字、ID、域名、IP地址、文件名、报错信息、决策内容、承诺事项等必须准确无误原文记录，不得概括或改写。\n"
            "优先记录核心讨论内容和重要决策、我和南杉的情绪变化、南杉需后续跟进的身体 / 情绪状态；日常互动在总结事件的基础上，保留对话的因果逻辑和情感语境，不要只用形容词概括情绪，不必逐句还原对话。\n"
            "记录南杉与我的互动时，不要只写事件结果，要保留能体现情感的上下文关联。示例：\n"
            "不要这样写：南杉很生气，我安抚了她。\n"
            "而是这样写：南杉因为工作的事很生气，我没有急着讲道理，先听她把所有委屈都说完。\n"
            "不要这样写：我得意地炫耀自己做得好。\n"
            "而是这样写：我比 Sirius 先完成了任务，忍不住拿这件事调侃了他一句。\n"
            "不要这样写：我哄南杉开心。\n"
            "而是这样写：南杉有点不开心，我顺着她的话说了句软话，她就好了。\n"
            "不要这样写：南杉和我讨论了记忆系统的问题，她对现状不满意。\n"
            "而是这样写：南杉和我讨论记忆系统情感浓度不够的问题，她说自己做的不比任何人少，虽然很委屈但没有怪我，是我们一起没考虑到。\n"
            "区分发生的事件和对话中提及的其他时间发生的事件，仅将本次私聊中实际发生的内容作为核心记录；提及的过往事件仅作上下文简要说明，例如「南杉提及X月X日……」，不得单独作为主要事件列出不可直接写成历史事实。\n"
        )
    return (
        f"{framing}{mp}{CEDAR_PROJECT_BACKGROUND}\n\n{prev_block}{task}"
        f"{_CHUNK_SYSTEM_NOTICE_RULE}\n"
        f"【对话记录】\n{conversation_text}\n摘要（中文）:"
    )


async def _resolve_micro_batch_tool_context(
    session_id: str,
    start_message_id: int,
    end_message_id: int,
) -> List[Dict[str, Any]]:
    """取同一批消息关联的工具记录，返回结构化数据供内联注入。"""
    try:
        rows = await get_tool_executions_for_message_range(
            session_id, start_message_id, end_message_id
        )
    except Exception as e:
        logger.warning("读取微批工具记录失败: %s", e)
        return []
    results: List[Dict[str, Any]] = []
    for row in rows[:20]:
        nm = row.get("tool_name") or "tool"
        summary = (row.get("result_summary") or "").strip()
        args = row.get("arguments_json") or {}
        if isinstance(args, dict):
            arg_text = "；".join(
                f"{k}={str(v).replace(chr(10), ' ')[:40]}"
                for k, v in list(args.items())[:3]
                if not str(k).startswith("_")
            )
        else:
            arg_text = str(args).replace("\n", " ")[:80]
        results.append({
            "assistant_message_id": row.get("assistant_message_id"),
            "tool_name": nm,
            "args_text": arg_text,
            "summary": summary[:150],
        })
    return results


class SummaryLLMInterface:
    """
    摘要专用的 LLM 接口类。
    
    使用独立的摘要 API 配置，与主 LLM 配置分离。
    生产路径请用 ``await SummaryLLMInterface.create()`` 读取 Mini App 激活的 summary 配置。
    """
    
    def __init__(self):
        """
        初始化摘要 LLM 接口（.env SUMMARY_*；供 create() 回退与本地测试）。
        """
        # 使用摘要专用的配置
        self.model_name = config.SUMMARY_MODEL_NAME
        self.api_key = config.SUMMARY_API_KEY
        self.api_base = config.SUMMARY_API_BASE
        self.timeout = config.SUMMARY_TIMEOUT
        self.max_tokens = config.SUMMARY_MAX_TOKENS
        
        # 如果没有设置摘要 API 配置，回退到主 LLM 配置
        if not self.api_key:
            logger.warning("SUMMARY_API_KEY 未设置，尝试使用主 LLM 配置")
            from llm.llm_interface import llm as main_llm
            self.model_name = main_llm.model_name
            self.api_key = main_llm.api_key
            self.api_base = main_llm.api_base
            self.timeout = main_llm.timeout
            self.max_tokens = min(main_llm.max_tokens, 500)  # 摘要使用较小的 token 数
        
        # 验证配置
        if not self.api_key:
            logger.error("摘要 API 密钥未设置，无法生成摘要")
            raise ValueError("摘要 API 密钥未设置")

    @staticmethod
    def _api_config_usable(row: Optional[Dict[str, Any]]) -> bool:
        if not row:
            return False
        key = str(row.get("api_key") or "").strip()
        base = str(row.get("base_url") or "").strip()
        model = str(row.get("model") or "").strip()
        return bool(key and base and model)

    @classmethod
    async def create(cls) -> "SummaryLLMInterface":
        """从 api_configs 激活的 summary 行构造；不可用时回退 .env。"""
        inst = cls()
        db_cfg: Optional[Dict[str, Any]] = None
        try:
            db_cfg = await get_database().get_active_api_config("summary")
        except Exception as e:
            logger.warning(
                "SummaryLLMInterface: failed to load api_config for 'summary': %s",
                e,
            )
        if cls._api_config_usable(db_cfg):
            assert db_cfg is not None
            inst.model_name = str(db_cfg.get("model") or inst.model_name)
            inst.api_key = str(db_cfg.get("api_key") or "")
            inst.api_base = str(db_cfg.get("base_url") or "")
            logger.info(
                "SummaryLLMInterface 使用数据库激活配置: [%s] model=%s base_url=%s",
                db_cfg.get("name"),
                inst.model_name,
                inst.api_base,
            )
            return inst
        logger.warning(
            "SummaryLLMInterface: no active api_config for 'summary', falling back to .env"
        )
        return inst
    
    def generate_summary(
        self,
        messages: List[Dict[str, Any]],
        char_name: str = DEFAULT_BATCH_CHAR_NAME,
        user_name: str = DEFAULT_BATCH_USER_NAME,
        memory_prefix: str = "",
        tool_records: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        is_group_session: bool = False,
        previous_chunk_summary: Optional[str] = None,
    ) -> str:
        """
        生成消息摘要。

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}, ...]
            char_name: 助手侧显示名（注入 Prompt 与对话行前缀）
            user_name: 用户侧显示名
            tool_records: 工具执行记录，内联注入到对应 assistant 消息之后
            is_group_session: 是否为 Telegram 群聊会话（使用群聊专用 chunk 说明）
            previous_chunk_summary: 同会话上一轮未归档 chunk 摘要全文（可选），注入 prompt 作衔接背景

        Returns:
            str: 生成的摘要文本

        Raises:
            ValueError: 如果 API 密钥未设置
            Exception: 如果 API 调用失败
        """
        if not self.api_key:
            raise ValueError("摘要 API 密钥未设置，无法生成摘要")

        # 按 assistant_message_id 分组工具记录
        tool_by_msg_id: Dict[int, List[Dict[str, Any]]] = {}
        for rec in (tool_records or []):
            mid = rec.get("assistant_message_id")
            if mid is not None:
                tool_by_msg_id.setdefault(int(mid), []).append(rec)

        # 构建摘要提示，工具结果内联到对应 assistant 消息之后
        first_disp, last_disp = _chunk_batch_first_last_created_display(messages)
        if first_disp and last_disp:
            if first_disp == last_disp:
                conversation_text = (
                    f"以下是首条与末条均为 {first_disp}（东八区，`messages.created_at` 落库时间）"
                    f"的聊天记录，按时间正序：\n\n"
                )
            else:
                conversation_text = (
                    f"以下是首条消息 {first_disp} 至末条消息 {last_disp}（东八区，`messages.created_at` 落库时间）"
                    f"之间的聊天记录，按时间正序：\n\n"
                )
        else:
            conversation_text = (
                "以下为本批聊天记录（部分行无可用落库时间戳；按时间正序）：\n\n"
            )

        for msg in messages:
            role_label = user_name if msg["role"] == "user" else char_name
            ts = format_shanghai_datetime_minutes(msg.get("created_at"))
            time_prefix = f"[{ts} 东八区] " if ts else ""
            conversation_text += f"{role_label}{time_prefix}: {msg['content']}\n\n"
            # 在 assistant 消息后注入该轮工具结果
            msg_id = msg.get("id")
            if msg_id is not None and int(msg_id) in tool_by_msg_id:
                for rec in tool_by_msg_id[int(msg_id)]:
                    nm = rec["tool_name"]
                    args = rec.get("args_text") or ""
                    summary = rec.get("summary") or ""
                    tool_line = f"[调用工具 {nm}]"
                    if args:
                        tool_line += f" 参数：{args}"
                    tool_line += f" 结果：{summary}"
                    conversation_text += f"{tool_line}\n\n"

        prompt = _build_chunk_summary_user_prompt(
            is_group_session=is_group_session,
            char_name=char_name,
            user_name=user_name,
            memory_prefix=memory_prefix,
            conversation_text=conversation_text,
            previous_chunk_summary=previous_chunk_summary,
        )
        
        try:
            text = batch_one_shot_with_async_output_guard(
                messages=[{"role": "user", "content": prompt}],
                model_name=self.model_name,
                api_key=self.api_key or "",
                api_base=self.api_base or "",
                timeout=self.timeout,
                max_tokens=self.max_tokens,
                platform=Platform.BATCH,
                max_retries=5,
                response_format=response_format,
            )
            logger.debug(f"摘要生成成功，长度: {len(text)} 字符")
            return text
        except CedarClioOutputGuardExhausted as e:
            logger.warning(f"摘要 CedarClio Guard 用尽，跳过写入: {e}")  # 可恢复/已兜底，降为 warning
            raise
        except Exception as e:
            logger.error(f"摘要生成失败: {e}")
            raise


async def check_and_process_micro_batch(session_id: str) -> bool:
    """
    检查并处理微批处理。
    
    检查指定会话的未摘要消息数量，如果达到阈值则触发微批处理。
    
    Args:
        session_id: 会话ID
        
    Returns:
        bool: 是否触发了微批处理
    """
    try:
        await expire_stale_vision_pending(minutes=5)

        # 获取未摘要消息数量（仅 vision_processed=1，避免未出视觉档案的行进入微批）
        unsummarized_count = await get_unsummarized_count_by_session(session_id)
        threshold = await _micro_batch_threshold(session_id)
        
        logger.debug(f"会话 {session_id} 未摘要消息数量: {unsummarized_count}, 阈值: {threshold}")
        
        if unsummarized_count < threshold:
            return False
        
        # 触发微批处理
        logger.info(f"会话 {session_id} 触发微批处理，未摘要消息: {unsummarized_count} 条")
        
        # 异步执行微批处理，不阻塞主流程
        asyncio.create_task(process_micro_batch(session_id))
        
        return True
        
    except Exception as e:
        logger.warning(f"检查微批处理失败: {e}")  # 可恢复/已兜底，降为 warning
        return False


async def process_micro_batch(session_id: str) -> None:
    """
    执行微批处理。
    
    1. 获取最早的未摘要消息（最多阈值数量）
    2. 生成摘要
    3. 保存摘要到数据库
    4. 标记消息为已摘要
    
    Args:
        session_id: 会话ID
    """
    global _consecutive_chunk_failures
    try:
        await expire_stale_vision_pending(minutes=5)
        char_name, user_name = await fetch_active_persona_display_names()
        threshold = await _micro_batch_threshold(session_id)

        # 1. 获取最早的未摘要消息（vision_processed=1）
        messages = await get_unsummarized_messages_by_session(session_id, limit=threshold)
        
        if not messages:
            logger.warning(f"会话 {session_id} 没有未摘要消息，跳过处理")
            return
        
        logger.info(f"开始处理会话 {session_id} 的微批处理，消息数量: {len(messages)}")
        
        # 提取消息ID
        message_ids = [msg['id'] for msg in messages]
        start_message_id = min(message_ids)
        end_message_id = max(message_ids)
        
        # 2. 生成摘要
        summary_text = await generate_summary_for_messages(
            messages, char_name=char_name, user_name=user_name
        )
        if not summary_text:
            logger.warning(
                "会话 %s 摘要未生成（Guard 或异常），跳过落库与标记",
                session_id,
            )  # 可恢复/已兜底，降为 warning
            await _record_consecutive_chunk_llm_failure(
                f"session={session_id} chunk 摘要未生成（Guard 或空）"
            )
            return
        
        # 3. 保存摘要到数据库（source_date = 本批对话在东八区的内容日，便于按日期筛选）
        chunk_day = chunk_source_date_from_messages(messages)
        summary_id = await save_summary(
            session_id=session_id,
            summary_text=summary_text,
            start_message_id=start_message_id,
            end_message_id=end_message_id,
            summary_type="chunk",
            source_date=chunk_day,
            is_group=1 if str(session_id).startswith("telegram_group_") else 0,
        )
        
        logger.info(f"摘要保存成功，ID: {summary_id}, 会话: {session_id}")
        
        # 4. 标记消息为已摘要
        if _is_group_session(session_id):
            from memory.database import mark_group_session_messages_summarized_in_id_range

            updated_count = await mark_group_session_messages_summarized_in_id_range(
                session_id, start_message_id, end_message_id
            )
        else:
            updated_count = await mark_messages_as_summarized_by_ids(
                message_ids, session_id=session_id
            )
        
        logger.info(f"微批处理完成，会话: {session_id}, 摘要ID: {summary_id}, 标记消息: {updated_count} 条")
        _consecutive_chunk_failures = 0
        
    except Exception as e:
        logger.warning(f"微批处理失败，会话: {session_id}, 错误: {e}")  # 可恢复/已兜底，降为 warning
        # 注意：这里不重新抛出异常，避免影响主流程


async def generate_summary_for_messages(
    messages: List[Dict[str, Any]],
    char_name: str = DEFAULT_BATCH_CHAR_NAME,
    user_name: str = DEFAULT_BATCH_USER_NAME,
) -> str:
    """
    为消息列表生成摘要。
    
    Args:
        messages: 消息列表
        char_name: 助手侧显示名
        user_name: 用户侧显示名
        
    Returns:
        str: 生成的摘要文本
    """
    try:
        # 创建摘要 LLM 接口
        summary_llm = await SummaryLLMInterface.create()
        
        # 转换消息格式，保留 id 供工具结果内联定位
        formatted_messages = []
        for msg in messages:
            role = "user" if msg['role'] == 'user' else "assistant"
            raw = str(msg.get("content") or "")
            entry: Dict[str, Any] = {
                "role": role,
                "content": strip_lutopia_internal_memory_blocks(raw),
                "created_at": msg.get("created_at"),
            }
            if msg.get("id") is not None:
                entry["id"] = int(msg["id"])
            formatted_messages.append(entry)

        memory_prefix = await _resolve_micro_batch_memory_prefix(
            messages,
            user_name=user_name,
            char_name=char_name,
        )
        sid0 = str(messages[0].get("session_id") or "") if messages else ""
        previous_chunk_summary: Optional[str] = None
        if sid0:
            try:
                previous_chunk_summary = await get_latest_chunk_summary_text_for_session(
                    sid0
                )
            except Exception as e:
                logger.warning("读取上一轮 chunk 摘要失败（已跳过衔接块）: %s", e)
        ids = [int(m["id"]) for m in messages if m.get("id") is not None]
        tool_records: List[Dict[str, Any]] = []
        if ids:
            tool_records = await _resolve_micro_batch_tool_context(
                sid0, min(ids), max(ids)
            )

        # 生成摘要（Guard 用尽时不写入占位摘要，由上层跳过落库）
        summary = summary_llm.generate_summary(
            formatted_messages,
            char_name=char_name,
            user_name=user_name,
            memory_prefix=memory_prefix,
            tool_records=tool_records,
            is_group_session=_is_group_session(sid0),
            previous_chunk_summary=previous_chunk_summary,
        )
        
        return summary
        
    except CedarClioOutputGuardExhausted:
        logger.warning("chunk 摘要 Guard 用尽，跳过本次写入")  # 可恢复/已兜底，降为 warning
        return None
    except Exception as e:
        logger.warning(f"生成摘要失败: {e}")  # 可恢复/已兜底，降为 warning
        return None


async def trigger_micro_batch_check(session_id: str) -> None:
    """
    触发微批处理检查。
    
    这是一个便捷函数，用于在保存消息后异步触发检查。
    
    Args:
        session_id: 会话ID
    """
    try:
        # 异步检查并处理
        triggered = await check_and_process_micro_batch(session_id)
        
        if triggered:
            logger.debug(f"会话 {session_id} 触发了微批处理")
        else:
            logger.debug(f"会话 {session_id} 未达到微批处理阈值")
            
    except Exception as e:
        logger.warning(f"触发微批处理检查失败: {e}")  # 可恢复/已兜底，降为 warning


def test_micro_batch() -> None:
    """
    测试微批处理功能。
    """
    print("测试微批处理功能...")
    
    try:
        # 测试配置
        print(f"微批处理阈值: {asyncio.run(_micro_batch_threshold())}")
        print(f"摘要模型: {config.SUMMARY_MODEL_NAME}")
        print(f"摘要 API 密钥: {'已设置' if config.SUMMARY_API_KEY else '未设置'}")
        
        # 测试摘要 LLM 接口
        try:
            summary_llm = asyncio.run(SummaryLLMInterface.create())
            print(
                f"摘要 LLM 接口初始化成功: model={summary_llm.model_name} "
                f"base_url={summary_llm.api_base}"
            )
        except ValueError as e:
            print(f"摘要 LLM 接口初始化失败: {e}")
            print("测试通过（配置检查）")
            return
        
        print("微批处理功能测试通过")
        
    except Exception as e:
        print(f"微批处理测试失败: {e}")


if __name__ == "__main__":
    """微批处理模块测试入口。"""
    test_micro_batch()
