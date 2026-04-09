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
from datetime import datetime

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

# 导入数据库函数
try:
    from .database import (
        get_database,
        get_unsummarized_count_by_session,
        get_unsummarized_messages_by_session,
        save_summary,
        mark_messages_as_summarized_by_ids,
        expire_stale_vision_pending,
    )
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    from memory.database import (
        get_database,
        get_unsummarized_count_by_session,
        get_unsummarized_messages_by_session,
        save_summary,
        mark_messages_as_summarized_by_ids,
        expire_stale_vision_pending,
    )

# 设置日志
logger = logging.getLogger(__name__)


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
        # 构建摘要提示
        conversation_text = ""
        for msg in messages:
            role_label = user_name if msg["role"] == "user" else char_name
            conversation_text += f"{role_label}: {msg['content']}\n\n"
        
        prompt = f"""{prefix}请为以下对话生成一段简洁摘要（约150-200字），重点提取主要话题、双方的情绪起伏和关键信息（包括具体数字、决定、名称等细节），去除无意义的语气词和重复内容。
若是技术讨论或代码问题，必须保留核心的名词或报错关键语。
【对话记录】
{conversation_text}
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
            logger.error(f"摘要 CedarClio Guard 用尽，跳过写入: {e}")
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
        logger.error(f"检查微批处理失败: {e}")
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
            logger.error(
                "会话 %s 摘要未生成（Guard 或异常），跳过落库与标记",
                session_id,
            )
            return
        
        # 3. 保存摘要到数据库
        summary_id = await save_summary(
            session_id=session_id,
            summary_text=summary_text,
            start_message_id=start_message_id,
            end_message_id=end_message_id,
            summary_type="chunk"
        )
        
        logger.info(f"摘要保存成功，ID: {summary_id}, 会话: {session_id}")
        
        # 4. 标记消息为已摘要
        updated_count = await mark_messages_as_summarized_by_ids(message_ids)
        
        logger.info(f"微批处理完成，会话: {session_id}, 摘要ID: {summary_id}, 标记消息: {updated_count} 条")
        
    except Exception as e:
        logger.error(f"微批处理失败，会话: {session_id}, 错误: {e}")
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
            formatted_messages.append({
                "role": role,
                "content": msg['content']
            })
        
        # 生成摘要（Guard 用尽时不写入占位摘要，由上层跳过落库）
        summary = summary_llm.generate_summary(
            formatted_messages, char_name=char_name, user_name=user_name
        )
        
        return summary
        
    except CedarClioOutputGuardExhausted:
        logger.error("chunk 摘要 Guard 用尽，跳过本次写入")
        return None
    except Exception as e:
        logger.error(f"生成摘要失败: {e}")
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
        logger.error(f"触发微批处理检查失败: {e}")


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
