"""
Context 构建模块。

负责组装发送给 LLM 的完整 prompt，按照优先级从上到下拼装：
1. system prompt：从配置读取，保持原样
2. memory_cards：查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化后拼入
3. daily summary：查询 summaries 表中 summary_type='daily'，按 created_at 倒序取最近 5 条，然后翻转为正序（按时间从老到新）后拼入
4. chunk summary：查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选），附带其来源标识，按时间正序拼入
5. 最近消息：查询当前 session_id 下 is_summarized=0 的消息，按 created_at 倒序取 40 条，再正序排列后拼入

组装完成后返回一个结构，包含 system prompt 和 messages 数组，直接可以传给 LLM API。
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

from config import config
from memory.database import (
    get_all_active_memory_cards,
    get_recent_daily_summaries,
    get_today_chunk_summaries,
    get_unsummarized_messages_desc
)

# 设置日志
logger = logging.getLogger(__name__)


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
        2. memory_cards
        3. daily summary
        4. chunk summary
        5. 最近消息
        
        Args:
            session_id: 会话ID
            user_message: 用户当前消息
            
        Returns:
            Dict[str, Any]: 包含 system prompt 和 messages 数组的结构
        """
        try:
            # 1. 获取 system prompt
            system_prompt = self._build_system_prompt()
            
            # 2. 获取 memory cards
            memory_cards_section = self._build_memory_cards_section()
            
            # 3. 获取 daily summaries
            daily_summaries_section = self._build_daily_summaries_section()
            
            # 4. 获取 today's chunk summaries
            chunk_summaries_section = self._build_chunk_summaries_section()
            
            # 5. 获取最近消息
            recent_messages_section = self._build_recent_messages_section(session_id)
            
            # 6. 添加当前用户消息
            current_user_message = self._build_current_user_message(user_message)
            
            # 组装完整的 system prompt
            full_system_prompt = self._assemble_full_system_prompt(
                system_prompt,
                memory_cards_section,
                daily_summaries_section,
                chunk_summaries_section
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
    
    def _build_system_prompt(self) -> str:
        """
        构建基础 system prompt。
        
        Returns:
            str: 基础 system prompt
        """
        return config.SYSTEM_PROMPT
    
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
    
    def _assemble_full_system_prompt(self, system_prompt: str, 
                                    memory_cards_section: str,
                                    daily_summaries_section: str,
                                    chunk_summaries_section: str) -> str:
        """
        组装完整的 system prompt。
        
        Args:
            system_prompt: 基础 system prompt
            memory_cards_section: memory cards 部分
            daily_summaries_section: daily summary 部分
            chunk_summaries_section: chunk summary 部分
            
        Returns:
            str: 完整的 system prompt
        """
        sections = [system_prompt]
        
        if memory_cards_section:
            sections.append(memory_cards_section)
        
        if daily_summaries_section:
            sections.append(daily_summaries_section)
        
        if chunk_summaries_section:
            sections.append(chunk_summaries_section)
        
        # 添加分隔线和指令
        if len(sections) > 1:  # 除了基础 system prompt 外还有其他部分
            sections.append("---")
            sections.append("以上是历史信息和用户记忆，请基于这些信息进行对话。")
        
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
