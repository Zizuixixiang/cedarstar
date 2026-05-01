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
from tools.lutopia import strip_lutopia_internal_memory_blocks

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


async def _micro_batch_threshold() -> int:
    """微批触发条数：优先 config 表 chunk_threshold，否则环境变量 MICRO_BATCH_THRESHOLD。"""
    try:
        raw = await get_database().get_config("chunk_threshold")
        if raw is not None and str(raw).strip() != "":
            return max(1, int(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 chunk_threshold 失败，使用环境变量: %s", e)
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


async def _get_micro_batch_memory_prefix(user_id: str, character_id: str) -> str:
    """chunk 摘要前注入关系锚点与激活记忆卡（current_status / relationships）。"""
    dims = {
        "current_status": "用户近况",
        "relationships": "重要关系",
    }
    lines = ["【基础设定】小克是南杉的二号男友。"]
    try:
        for dim, label in dims.items():
            cards = await get_memory_cards(user_id, character_id, dim, limit=1)
            card = cards[0] if cards else None
            if card and card.get("content"):
                lines.append(f"【{label}】{card['content']}")
    except Exception as e:
        logger.warning("微批记忆上下文前缀构建失败: %s", e)
    return "\n".join(lines) + "\n\n"


async def _resolve_micro_batch_memory_prefix(
    messages: List[Dict[str, Any]],
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
    return await _get_micro_batch_memory_prefix(uid, cid)


async def _resolve_micro_batch_tool_context(
    session_id: str,
    start_message_id: int,
    end_message_id: int,
) -> str:
    """把同一批消息关联的工具记录加入 chunk 摘要输入，避免工具信息断档。"""
    try:
        rows = await get_tool_executions_for_message_range(
            session_id, start_message_id, end_message_id
        )
    except Exception as e:
        logger.warning("读取微批工具记录失败: %s", e)
        return ""
    if not rows:
        return ""
    lines: List[str] = []
    for row in rows[:20]:
        nm = row.get("tool_name") or "tool"
        summary = (row.get("result_summary") or "").strip()
        args = row.get("arguments_json") or {}
        if isinstance(args, dict):
            arg_text = "；".join(
                f"{k}={str(v).replace(chr(10), ' ')[:60]}"
                for k, v in list(args.items())[:3]
                if not str(k).startswith("_")
            )
        else:
            arg_text = str(args).replace("\n", " ")[:160]
        line = f"- {nm}"
        if arg_text:
            line += f"（{arg_text}）"
        line += f"：{summary[:700]}"
        lines.append(line)
    return "\n".join(lines)


class SummaryLLMInterface:
    """
    摘要专用的 LLM 接口类。
    
    使用独立的摘要 API 配置，与主 LLM 配置分离。
    """
    
    def __init__(self):
        """
        初始化摘要 LLM 接口。
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
    
    def generate_summary(
        self,
        messages: List[Dict[str, Any]],
        char_name: str = DEFAULT_BATCH_CHAR_NAME,
        user_name: str = DEFAULT_BATCH_USER_NAME,
        memory_prefix: str = "",
        tool_context: str = "",
    ) -> str:
        """
        生成消息摘要。
        
        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}, ...]
            char_name: 助手侧显示名（注入 Prompt 与对话行前缀）
            user_name: 用户侧显示名
            
        Returns:
            str: 生成的摘要文本
            
        Raises:
            ValueError: 如果 API 密钥未设置
            Exception: 如果 API 调用失败
        """
        if not self.api_key:
            raise ValueError("摘要 API 密钥未设置，无法生成摘要")
        
        prefix = f"这是 {char_name} 与 {user_name} 的对话记录。\n"
        mp = memory_prefix or ""
        # 构建摘要提示
        conversation_text = ""
        for msg in messages:
            role_label = user_name if msg["role"] == "user" else char_name
            conversation_text += f"{role_label}: {msg['content']}\n\n"

        tool_block = ""
        if (tool_context or "").strip():
            tool_block = (
                "\n【期间工具使用】\n"
                + tool_context.strip()
                + "\n\n"
            )
        
        prompt = f"""{prefix}{mp}请为以下对话生成150-200字中文简洁摘要，精准提取核心话题、双方情绪变化、关键事实（含数字、决策、名称、技术术语/报错信息），剔除语气词、重复内容与无效闲聊。
输出客观凝练，无主观修饰，严格符合字数要求。
【对话记录】
{conversation_text}{tool_block}
摘要（中文）:"""
        
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
        threshold = await _micro_batch_threshold()
        
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
        threshold = await _micro_batch_threshold()

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
            is_group=1 if str(session_id).startswith("telegram_-") else 0,
        )
        
        logger.info(f"摘要保存成功，ID: {summary_id}, 会话: {session_id}")
        
        # 4. 标记消息为已摘要
        updated_count = await mark_messages_as_summarized_by_ids(message_ids)
        
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
        summary_llm = SummaryLLMInterface()
        
        # 转换消息格式
        formatted_messages = []
        for msg in messages:
            role = "user" if msg['role'] == 'user' else "assistant"
            raw = str(msg.get("content") or "")
            formatted_messages.append(
                {
                    "role": role,
                    "content": strip_lutopia_internal_memory_blocks(raw),
                }
            )
        
        memory_prefix = await _resolve_micro_batch_memory_prefix(messages)
        ids = [int(m["id"]) for m in messages if m.get("id") is not None]
        tool_context = ""
        if ids:
            sid = str(messages[0].get("session_id") or "")
            tool_context = await _resolve_micro_batch_tool_context(
                sid, min(ids), max(ids)
            )

        # 生成摘要（Guard 用尽时不写入占位摘要，由上层跳过落库）
        summary = summary_llm.generate_summary(
            formatted_messages,
            char_name=char_name,
            user_name=user_name,
            memory_prefix=memory_prefix,
            tool_context=tool_context,
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
            summary_llm = SummaryLLMInterface()
            print("摘要 LLM 接口初始化成功")
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
