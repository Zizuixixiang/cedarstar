"""
短期记忆数据库模块。

使用 SQLite 存储对话消息，支持短期记忆功能。
"""

import sqlite3
import logging
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

# 设置日志
logger = logging.getLogger(__name__)


class MessageDatabase:
    """
    消息数据库类。
    
    封装 SQLite 操作，提供消息存储和检索功能。
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        初始化消息数据库。
        
        Args:
            db_path: 数据库文件路径，如果为 None 则使用默认路径
        """
        if db_path is None:
            # 使用默认数据库路径
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "cedarstar.db")
        
        self.db_path = db_path
        self._init_database()
        
        logger.info(f"消息数据库初始化完成，路径: {db_path}")
    
    def _init_database(self):
        """初始化数据库，创建所有表（如果不存在）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 创建 messages 表（按新结构）
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        user_id TEXT,
                        channel_id TEXT,
                        message_id TEXT,
                        is_summarized INTEGER DEFAULT 0,
                        character_id TEXT
                    )
                """)
                
                # 创建 memory_cards 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS memory_cards (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        character_id TEXT NOT NULL,
                        dimension TEXT NOT NULL,
                        content TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        source_message_id TEXT,
                        is_active INTEGER DEFAULT 1
                    )
                """)
                
                # 创建 summaries 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS summaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        summary_text TEXT NOT NULL,
                        start_message_id INTEGER NOT NULL,
                        end_message_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        summary_type TEXT DEFAULT 'chunk'
                    )
                """)
                
                # 创建 daily_batch_log 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS daily_batch_log (
                        batch_date DATE PRIMARY KEY,
                        step1_status INTEGER DEFAULT 0,
                        step2_status INTEGER DEFAULT 0,
                        step3_status INTEGER DEFAULT 0,
                        error_message TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 创建索引以提高查询性能
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_messages_session_id 
                    ON messages (session_id, created_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memory_cards_user_character
                    ON memory_cards (user_id, character_id, dimension, updated_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_summaries_session_id
                    ON summaries (session_id, created_at)
                """)
                
                conn.commit()
                
                logger.debug("数据库表初始化完成")
                
        except sqlite3.Error as e:
            logger.error(f"数据库初始化失败: {e}")
            raise
    
    def save_message(self, role: str, content: str, session_id: str, 
                    user_id: Optional[str] = None, channel_id: Optional[str] = None, 
                    message_id: Optional[str] = None, character_id: Optional[str] = None) -> int:
        """
        保存一条消息到数据库。
        
        Args:
            role: 消息角色（'user', 'assistant', 'system' 等）
            content: 消息内容
            session_id: 会话ID，用于区分不同的对话
            user_id: 用户ID（可选）
            channel_id: 频道ID（可选）
            message_id: 消息ID（可选）
            character_id: 角色ID（可选）
            
        Returns:
            int: 插入的消息ID
            
        Raises:
            sqlite3.Error: 数据库操作失败
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO messages (role, content, session_id, user_id, channel_id, message_id, character_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (role, content, session_id, user_id, channel_id, message_id, character_id))
                
                message_id = cursor.lastrowid
                conn.commit()
                
                logger.debug(f"保存消息成功: ID={message_id}, role={role}, session={session_id}")
                return message_id
                
        except sqlite3.Error as e:
            logger.error(f"保存消息失败: {e}")
            raise
    
    def get_recent_messages(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取指定会话的最近 N 条消息。
        
        Args:
            session_id: 会话ID
            limit: 最大消息数量，默认为 20
            
        Returns:
            List[Dict[str, Any]]: 消息列表，每条消息包含 id, role, content, created_at, session_id
            
        Raises:
            sqlite3.Error: 数据库操作失败
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 设置行工厂，返回字典形式的结果
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT id, role, content, created_at, session_id
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (session_id, limit))
                
                rows = cursor.fetchall()
                
                # 转换为字典列表（注意：按时间倒序，可能需要正序返回）
                messages = []
                for row in rows:
                    message = {
                        'id': row['id'],
                        'role': row['role'],
                        'content': row['content'],
                        'created_at': row['created_at'],
                        'session_id': row['session_id']
                    }
                    messages.append(message)
                
                # 按时间正序返回（最旧的消息在前）
                messages.reverse()
                
                logger.debug(f"获取会话 {session_id} 的最近 {len(messages)} 条消息")
                return messages
                
        except sqlite3.Error as e:
            logger.error(f"获取消息失败: {e}")
            raise
    
    def clear_session_messages(self, session_id: str) -> int:
        """
        清除指定会话的所有消息。
        
        Args:
            session_id: 会话ID
            
        Returns:
            int: 删除的消息数量
            
        Raises:
            sqlite3.Error: 数据库操作失败
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    DELETE FROM messages
                    WHERE session_id = ?
                """, (session_id,))
                
                deleted_count = cursor.rowcount
                conn.commit()
                
                logger.info(f"清除会话 {session_id} 的 {deleted_count} 条消息")
                return deleted_count
                
        except sqlite3.Error as e:
            logger.error(f"清除消息失败: {e}")
            raise
    
    def get_session_count(self, session_id: str) -> int:
        """
        获取指定会话的消息数量。
        
        Args:
            session_id: 会话ID
            
        Returns:
            int: 消息数量
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM messages 
                    WHERE session_id = ?
                """, (session_id,))
                
                count = cursor.fetchone()[0]
                return count
                
        except sqlite3.Error as e:
            logger.error(f"获取消息数量失败: {e}")
            return 0
    
    def save_memory_card(self, user_id: str, character_id: str, dimension: str, 
                        content: str, source_message_id: Optional[str] = None) -> int:
        """
        保存记忆卡片到数据库。
        
        Args:
            user_id: 用户ID
            character_id: 角色ID
            dimension: 维度（枚举值：preferences, interaction_patterns, current_status, goals, relationships, key_events, rules）
            content: 记忆内容
            source_message_id: 来源消息ID（可选）
            
        Returns:
            int: 插入的记忆卡片ID
            
        Raises:
            ValueError: 如果维度不在允许的枚举值中
            sqlite3.Error: 数据库操作失败
        """
        # 验证维度值
        allowed_dimensions = {"preferences", "interaction_patterns", "current_status", 
                             "goals", "relationships", "key_events", "rules"}
        if dimension not in allowed_dimensions:
            raise ValueError(f"维度 '{dimension}' 不在允许的枚举值中。允许的值: {allowed_dimensions}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO memory_cards (user_id, character_id, dimension, content, source_message_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, character_id, dimension, content, source_message_id))
                
                card_id = cursor.lastrowid
                conn.commit()
                
                logger.debug(f"保存记忆卡片成功: ID={card_id}, user={user_id}, dimension={dimension}")
                return card_id
                
        except sqlite3.Error as e:
            logger.error(f"保存记忆卡片失败: {e}")
            raise
    
    def get_memory_cards(self, user_id: str, character_id: str, 
                        dimension: Optional[str] = None, 
                        limit: int = 50) -> List[Dict[str, Any]]:
        """
        获取用户的记忆卡片。
        
        Args:
            user_id: 用户ID
            character_id: 角色ID
            dimension: 维度筛选（可选）
            limit: 最大返回数量，默认为 50
            
        Returns:
            List[Dict[str, Any]]: 记忆卡片列表
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                if dimension:
                    cursor.execute("""
                        SELECT id, user_id, character_id, dimension, content, 
                               updated_at, source_message_id, is_active
                        FROM memory_cards
                        WHERE user_id = ? AND character_id = ? AND dimension = ? AND is_active = 1
                        ORDER BY updated_at DESC
                        LIMIT ?
                    """, (user_id, character_id, dimension, limit))
                else:
                    cursor.execute("""
                        SELECT id, user_id, character_id, dimension, content, 
                               updated_at, source_message_id, is_active
                        FROM memory_cards
                        WHERE user_id = ? AND character_id = ? AND is_active = 1
                        ORDER BY updated_at DESC
                        LIMIT ?
                    """, (user_id, character_id, limit))
                
                rows = cursor.fetchall()
                
                cards = []
                for row in rows:
                    card = {
                        'id': row['id'],
                        'user_id': row['user_id'],
                        'character_id': row['character_id'],
                        'dimension': row['dimension'],
                        'content': row['content'],
                        'updated_at': row['updated_at'],
                        'source_message_id': row['source_message_id'],
                        'is_active': bool(row['is_active'])
                    }
                    cards.append(card)
                
                logger.debug(f"获取记忆卡片成功: user={user_id}, count={len(cards)}")
                return cards
                
        except sqlite3.Error as e:
            logger.error(f"获取记忆卡片失败: {e}")
            raise
    
    def update_memory_card(self, card_id: int, content: str, 
                          dimension: Optional[str] = None) -> bool:
        """
        更新记忆卡片。
        
        Args:
            card_id: 记忆卡片ID
            content: 新的记忆内容
            dimension: 新的维度（可选）
            
        Returns:
            bool: 更新是否成功
            
        Raises:
            ValueError: 如果维度不在允许的枚举值中
            sqlite3.Error: 数据库操作失败
        """
        if dimension:
            # 验证维度值
            allowed_dimensions = {"preferences", "interaction_patterns", "current_status", 
                                 "goals", "relationships", "key_events", "rules"}
            if dimension not in allowed_dimensions:
                raise ValueError(f"维度 '{dimension}' 不在允许的枚举值中。允许的值: {allowed_dimensions}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                if dimension:
                    cursor.execute("""
                        UPDATE memory_cards 
                        SET content = ?, dimension = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (content, dimension, card_id))
                else:
                    cursor.execute("""
                        UPDATE memory_cards 
                        SET content = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (content, card_id))
                
                updated = cursor.rowcount > 0
                conn.commit()
                
                if updated:
                    logger.debug(f"更新记忆卡片成功: ID={card_id}")
                else:
                    logger.warning(f"更新记忆卡片失败: ID={card_id} 不存在")
                
                return updated
                
        except sqlite3.Error as e:
            logger.error(f"更新记忆卡片失败: {e}")
            raise
    
    def deactivate_memory_card(self, card_id: int) -> bool:
        """
        停用记忆卡片（软删除）。
        
        Args:
            card_id: 记忆卡片ID
            
        Returns:
            bool: 停用是否成功
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    UPDATE memory_cards 
                    SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (card_id,))
                
                deactivated = cursor.rowcount > 0
                conn.commit()
                
                if deactivated:
                    logger.debug(f"停用记忆卡片成功: ID={card_id}")
                else:
                    logger.warning(f"停用记忆卡片失败: ID={card_id} 不存在")
                
                return deactivated
                
        except sqlite3.Error as e:
            logger.error(f"停用记忆卡片失败: {e}")
            raise
    
    def save_summary(self, session_id: str, summary_text: str, 
                    start_message_id: int, end_message_id: int,
                    summary_type: str = "chunk") -> int:
        """
        保存对话摘要到数据库。
        
        Args:
            session_id: 会话ID
            summary_text: 摘要文本
            start_message_id: 起始消息ID
            end_message_id: 结束消息ID
            summary_type: 摘要类型，默认为 'chunk'，可选 'daily'
            
        Returns:
            int: 插入的摘要ID
            
        Raises:
            ValueError: 如果 summary_type 不在允许的值中
            sqlite3.Error: 数据库操作失败
        """
        # 验证 summary_type 值
        allowed_types = {"chunk", "daily"}
        if summary_type not in allowed_types:
            raise ValueError(f"summary_type '{summary_type}' 不在允许的值中。允许的值: {allowed_types}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO summaries (session_id, summary_text, start_message_id, end_message_id, summary_type)
                    VALUES (?, ?, ?, ?, ?)
                """, (session_id, summary_text, start_message_id, end_message_id, summary_type))
                
                summary_id = cursor.lastrowid
                conn.commit()
                
                logger.debug(f"保存摘要成功: ID={summary_id}, session={session_id}, type={summary_type}")
                return summary_id
                
        except sqlite3.Error as e:
            logger.error(f"保存摘要失败: {e}")
            raise
    
    def get_summaries(self, session_id: str, limit: int = 10, 
                     summary_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取会话的摘要列表。
        
        Args:
            session_id: 会话ID
            limit: 最大返回数量，默认为 10
            summary_type: 摘要类型筛选（可选），'chunk' 或 'daily'
            
        Returns:
            List[Dict[str, Any]]: 摘要列表
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                if summary_type:
                    cursor.execute("""
                        SELECT id, session_id, summary_text, start_message_id, end_message_id, 
                               created_at, summary_type
                        FROM summaries
                        WHERE session_id = ? AND summary_type = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (session_id, summary_type, limit))
                else:
                    cursor.execute("""
                        SELECT id, session_id, summary_text, start_message_id, end_message_id, 
                               created_at, summary_type
                        FROM summaries
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (session_id, limit))
                
                rows = cursor.fetchall()
                
                summaries = []
                for row in rows:
                    summary = {
                        'id': row['id'],
                        'session_id': row['session_id'],
                        'summary_text': row['summary_text'],
                        'start_message_id': row['start_message_id'],
                        'end_message_id': row['end_message_id'],
                        'created_at': row['created_at'],
                        'summary_type': row['summary_type']
                    }
                    summaries.append(summary)
                
                logger.debug(f"获取摘要成功: session={session_id}, count={len(summaries)}, type={summary_type}")
                return summaries
                
        except sqlite3.Error as e:
            logger.error(f"获取摘要失败: {e}")
            raise
    
    def mark_messages_as_summarized(self, start_message_id: int, end_message_id: int) -> int:
        """
        标记消息为已摘要。
        
        Args:
            start_message_id: 起始消息ID
            end_message_id: 结束消息ID
            
        Returns:
            int: 更新的消息数量
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    UPDATE messages 
                    SET is_summarized = 1
                    WHERE id >= ? AND id <= ?
                """, (start_message_id, end_message_id))
                
                updated_count = cursor.rowcount
                conn.commit()
                
                logger.debug(f"标记消息为已摘要: start={start_message_id}, end={end_message_id}, count={updated_count}")
                return updated_count
                
        except sqlite3.Error as e:
            logger.error(f"标记消息为已摘要失败: {e}")
            raise
    
    def mark_messages_as_summarized_by_ids(self, message_ids: List[int]) -> int:
        """
        根据消息ID列表批量标记消息为已摘要。
        
        Args:
            message_ids: 消息ID列表
            
        Returns:
            int: 更新的消息数量
        """
        if not message_ids:
            return 0
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 使用参数化查询，避免 SQL 注入
                placeholders = ','.join(['?' for _ in message_ids])
                cursor.execute(f"""
                    UPDATE messages 
                    SET is_summarized = 1
                    WHERE id IN ({placeholders})
                """, message_ids)
                
                updated_count = cursor.rowcount
                conn.commit()
                
                logger.debug(f"批量标记消息为已摘要: count={updated_count}, ids={message_ids[:5]}...")
                return updated_count
                
        except sqlite3.Error as e:
            logger.error(f"批量标记消息为已摘要失败: {e}")
            raise
    
    def get_unsummarized_count_by_session(self, session_id: str) -> int:
        """
        获取指定会话中未摘要消息的数量。
        
        Args:
            session_id: 会话ID
            
        Returns:
            int: 未摘要消息数量
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM messages 
                    WHERE session_id = ? AND is_summarized = 0
                """, (session_id,))
                
                count = cursor.fetchone()[0]
                logger.debug(f"会话 {session_id} 未摘要消息数量: {count}")
                return count
                
        except sqlite3.Error as e:
            logger.error(f"获取未摘要消息数量失败: {e}")
            return 0
    
    def get_unsummarized_messages_by_session(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        获取指定会话中最早的未摘要消息列表。
        
        按消息创建时间正序排列，返回最早的 limit 条消息。
        
        Args:
            session_id: 会话ID
            limit: 最大返回数量，默认为 50
            
        Returns:
            List[Dict[str, Any]]: 消息列表，每条消息包含 id, role, content, created_at, session_id
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT id, role, content, created_at, session_id, user_id, channel_id
                    FROM messages
                    WHERE session_id = ? AND is_summarized = 0
                    ORDER BY created_at ASC
                    LIMIT ?
                """, (session_id, limit))
                
                rows = cursor.fetchall()
                
                messages = []
                for row in rows:
                    message = {
                        'id': row['id'],
                        'role': row['role'],
                        'content': row['content'],
                        'created_at': row['created_at'],
                        'session_id': row['session_id'],
                        'user_id': row['user_id'],
                        'channel_id': row['channel_id']
                    }
                    messages.append(message)
                
                logger.debug(f"获取会话 {session_id} 的未摘要消息: {len(messages)} 条")
                return messages
                
        except sqlite3.Error as e:
            logger.error(f"获取未摘要消息失败: {e}")
            raise
    
    def save_daily_batch_log(self, batch_date: str, step1_status: int = 0, 
                           step2_status: int = 0, step3_status: int = 0,
                           error_message: Optional[str] = None) -> bool:
        """
        保存或更新每日批处理日志。
        
        Args:
            batch_date: 批处理日期，格式为 'YYYY-MM-DD'
            step1_status: 步骤1状态，0=未开始，1=已完成
            step2_status: 步骤2状态，0=未开始，1=已完成
            step3_status: 步骤3状态，0=未开始，1=已完成
            error_message: 错误信息（可选）
            
        Returns:
            bool: 操作是否成功
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 使用 INSERT OR REPLACE 来处理重复的 batch_date
                cursor.execute("""
                    INSERT OR REPLACE INTO daily_batch_log 
                    (batch_date, step1_status, step2_status, step3_status, error_message, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (batch_date, step1_status, step2_status, step3_status, error_message))
                
                conn.commit()
                
                logger.debug(f"保存每日批处理日志成功: date={batch_date}, "
                           f"step1={step1_status}, step2={step2_status}, step3={step3_status}")
                return True
                
        except sqlite3.Error as e:
            logger.error(f"保存每日批处理日志失败: {e}")
            raise
    
    def get_daily_batch_log(self, batch_date: str) -> Optional[Dict[str, Any]]:
        """
        获取指定日期的批处理日志。
        
        Args:
            batch_date: 批处理日期，格式为 'YYYY-MM-DD'
            
        Returns:
            Optional[Dict[str, Any]]: 批处理日志信息，如果不存在则返回 None
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT batch_date, step1_status, step2_status, step3_status, 
                           error_message, created_at, updated_at
                    FROM daily_batch_log
                    WHERE batch_date = ?
                """, (batch_date,))
                
                row = cursor.fetchone()
                
                if row:
                    log = {
                        'batch_date': row['batch_date'],
                        'step1_status': row['step1_status'],
                        'step2_status': row['step2_status'],
                        'step3_status': row['step3_status'],
                        'error_message': row['error_message'],
                        'created_at': row['created_at'],
                        'updated_at': row['updated_at']
                    }
                    logger.debug(f"获取每日批处理日志成功: date={batch_date}")
                    return log
                else:
                    logger.debug(f"每日批处理日志不存在: date={batch_date}")
                    return None
                
        except sqlite3.Error as e:
            logger.error(f"获取每日批处理日志失败: {e}")
            raise
    
    def get_recent_daily_batch_logs(self, limit: int = 30) -> List[Dict[str, Any]]:
        """
        获取最近的批处理日志列表。
        
        Args:
            limit: 最大返回数量，默认为 30
            
        Returns:
            List[Dict[str, Any]]: 批处理日志列表
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT batch_date, step1_status, step2_status, step3_status, 
                           error_message, created_at, updated_at
                    FROM daily_batch_log
                    ORDER BY batch_date DESC
                    LIMIT ?
                """, (limit,))
                
                rows = cursor.fetchall()
                
                logs = []
                for row in rows:
                    log = {
                        'batch_date': row['batch_date'],
                        'step1_status': row['step1_status'],
                        'step2_status': row['step2_status'],
                        'step3_status': row['step3_status'],
                        'error_message': row['error_message'],
                        'created_at': row['created_at'],
                        'updated_at': row['updated_at']
                    }
                    logs.append(log)
                
                logger.debug(f"获取最近批处理日志成功: count={len(logs)}")
                return logs
                
        except sqlite3.Error as e:
            logger.error(f"获取最近批处理日志失败: {e}")
            raise
    
    def update_daily_batch_step_status(self, batch_date: str, step_number: int, 
                                      status: int, error_message: Optional[str] = None) -> bool:
        """
        更新指定日期的批处理步骤状态。
        
        Args:
            batch_date: 批处理日期，格式为 'YYYY-MM-DD'
            step_number: 步骤编号（1, 2, 3）
            status: 状态，0=未开始，1=已完成
            error_message: 错误信息（可选）
            
        Returns:
            bool: 更新是否成功
            
        Raises:
            ValueError: 如果 step_number 不在 1-3 范围内
        """
        if step_number not in {1, 2, 3}:
            raise ValueError(f"步骤编号 {step_number} 无效，必须是 1, 2 或 3")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 根据步骤编号更新对应的字段
                if step_number == 1:
                    cursor.execute("""
                        UPDATE daily_batch_log 
                        SET step1_status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE batch_date = ?
                    """, (status, error_message, batch_date))
                elif step_number == 2:
                    cursor.execute("""
                        UPDATE daily_batch_log 
                        SET step2_status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE batch_date = ?
                    """, (status, error_message, batch_date))
                else:  # step_number == 3
                    cursor.execute("""
                        UPDATE daily_batch_log 
                        SET step3_status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE batch_date = ?
                    """, (status, error_message, batch_date))
                
                updated = cursor.rowcount > 0
                conn.commit()
                
                if updated:
                    logger.debug(f"更新批处理步骤状态成功: date={batch_date}, step={step_number}, status={status}")
                else:
                    logger.warning(f"更新批处理步骤状态失败: date={batch_date} 不存在")
                
                return updated
                
        except sqlite3.Error as e:
            logger.error(f"更新批处理步骤状态失败: {e}")
            raise


# 创建全局数据库实例
_db_instance: Optional[MessageDatabase] = None


def get_database() -> MessageDatabase:
    """
    获取数据库实例（单例模式）。
    
    Returns:
        MessageDatabase: 数据库实例
    """
    global _db_instance
    
    if _db_instance is None:
        # 从配置获取数据库路径
        try:
            from config import config
            db_url = config.DATABASE_URL
            if db_url and db_url.startswith("sqlite:///"):
                # 提取 SQLite 文件路径
                # sqlite:///./cedarstar.db -> ./cedarstar.db
                db_path = db_url.replace("sqlite:///", "")
                # 处理相对路径
                if db_path.startswith("./"):
                    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    db_path = os.path.join(current_dir, db_path[2:])
            else:
                # 使用默认路径
                current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                db_path = os.path.join(current_dir, "cedarstar.db")
            
            _db_instance = MessageDatabase(db_path)
            
        except ImportError:
            # 如果无法导入 config，使用默认路径
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "cedarstar.db")
            _db_instance = MessageDatabase(db_path)
    
    return _db_instance


# 便捷函数
def save_message(role: str, content: str, session_id: str, 
                user_id: Optional[str] = None, channel_id: Optional[str] = None, 
                message_id: Optional[str] = None, character_id: Optional[str] = None) -> int:
    """
    保存消息的便捷函数。
    
    Args:
        role: 消息角色
        content: 消息内容
        session_id: 会话ID
        user_id: 用户ID（可选）
        channel_id: 频道ID（可选）
        message_id: 消息ID（可选）
        character_id: 角色ID（可选）
        
    Returns:
        int: 消息ID
    """
    db = get_database()
    return db.save_message(role, content, session_id, user_id, channel_id, message_id, character_id)


def get_recent_messages(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    获取最近消息的便捷函数。
    
    Args:
        session_id: 会话ID
        limit: 最大消息数量
        
    Returns:
        List[Dict[str, Any]]: 消息列表
    """
    db = get_database()
    return db.get_recent_messages(session_id, limit)


def clear_session_messages(session_id: str) -> int:
    """
    清除会话消息的便捷函数。
    
    Args:
        session_id: 会话ID
        
    Returns:
        int: 删除的消息数量
    """
    db = get_database()
    return db.clear_session_messages(session_id)


def save_memory_card(user_id: str, character_id: str, dimension: str, 
                    content: str, source_message_id: Optional[str] = None) -> int:
    """
    保存记忆卡片的便捷函数。
    
    Args:
        user_id: 用户ID
        character_id: 角色ID
        dimension: 维度（枚举值：preferences, interaction_patterns, current_status, goals, relationships, key_events, rules）
        content: 记忆内容
        source_message_id: 来源消息ID（可选）
        
    Returns:
        int: 插入的记忆卡片ID
    """
    db = get_database()
    return db.save_memory_card(user_id, character_id, dimension, content, source_message_id)


def get_memory_cards(user_id: str, character_id: str, 
                    dimension: Optional[str] = None, 
                    limit: int = 50) -> List[Dict[str, Any]]:
    """
    获取记忆卡片的便捷函数。
    
    Args:
        user_id: 用户ID
        character_id: 角色ID
        dimension: 维度筛选（可选）
        limit: 最大返回数量，默认为 50
        
    Returns:
        List[Dict[str, Any]]: 记忆卡片列表
    """
    db = get_database()
    return db.get_memory_cards(user_id, character_id, dimension, limit)


def update_memory_card(card_id: int, content: str, 
                      dimension: Optional[str] = None) -> bool:
    """
    更新记忆卡片的便捷函数。
    
    Args:
        card_id: 记忆卡片ID
        content: 新的记忆内容
        dimension: 新的维度（可选）
        
    Returns:
        bool: 更新是否成功
    """
    db = get_database()
    return db.update_memory_card(card_id, content, dimension)


def deactivate_memory_card(card_id: int) -> bool:
    """
    停用记忆卡片的便捷函数。
    
    Args:
        card_id: 记忆卡片ID
        
    Returns:
        bool: 停用是否成功
    """
    db = get_database()
    return db.deactivate_memory_card(card_id)


def save_summary(session_id: str, summary_text: str, 
                start_message_id: int, end_message_id: int,
                summary_type: str = "chunk") -> int:
    """
    保存摘要的便捷函数。
    
    Args:
        session_id: 会话ID
        summary_text: 摘要文本
        start_message_id: 起始消息ID
        end_message_id: 结束消息ID
        summary_type: 摘要类型，默认为 'chunk'，可选 'daily'
        
    Returns:
        int: 插入的摘要ID
    """
    db = get_database()
    return db.save_summary(session_id, summary_text, start_message_id, end_message_id, summary_type)


def get_summaries(session_id: str, limit: int = 10, 
                 summary_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    获取摘要的便捷函数。
    
    Args:
        session_id: 会话ID
        limit: 最大返回数量，默认为 10
        summary_type: 摘要类型筛选（可选），'chunk' 或 'daily'
        
    Returns:
        List[Dict[str, Any]]: 摘要列表
    """
    db = get_database()
    return db.get_summaries(session_id, limit, summary_type)


def mark_messages_as_summarized(start_message_id: int, end_message_id: int) -> int:
    """
    标记消息为已摘要的便捷函数。
    
    Args:
        start_message_id: 起始消息ID
        end_message_id: 结束消息ID
        
    Returns:
        int: 更新的消息数量
    """
    db = get_database()
    return db.mark_messages_as_summarized(start_message_id, end_message_id)


def save_daily_batch_log(batch_date: str, step1_status: int = 0, 
                        step2_status: int = 0, step3_status: int = 0,
                        error_message: Optional[str] = None) -> bool:
    """
    保存或更新每日批处理日志的便捷函数。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'
        step1_status: 步骤1状态，0=未开始，1=已完成
        step2_status: 步骤2状态，0=未开始，1=已完成
        step3_status: 步骤3状态，0=未开始，1=已完成
        error_message: 错误信息（可选）
        
    Returns:
        bool: 操作是否成功
    """
    db = get_database()
    return db.save_daily_batch_log(batch_date, step1_status, step2_status, step3_status, error_message)


def get_daily_batch_log(batch_date: str) -> Optional[Dict[str, Any]]:
    """
    获取指定日期的批处理日志的便捷函数。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'
        
    Returns:
        Optional[Dict[str, Any]]: 批处理日志信息，如果不存在则返回 None
    """
    db = get_database()
    return db.get_daily_batch_log(batch_date)


def get_recent_daily_batch_logs(limit: int = 30) -> List[Dict[str, Any]]:
    """
    获取最近的批处理日志列表的便捷函数。
    
    Args:
        limit: 最大返回数量，默认为 30
        
    Returns:
        List[Dict[str, Any]]: 批处理日志列表
    """
    db = get_database()
    return db.get_recent_daily_batch_logs(limit)


def update_daily_batch_step_status(batch_date: str, step_number: int, 
                                  status: int, error_message: Optional[str] = None) -> bool:
    """
    更新指定日期的批处理步骤状态的便捷函数。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'
        step_number: 步骤编号（1, 2, 3）
        status: 状态，0=未开始，1=已完成
        error_message: 错误信息（可选）
        
    Returns:
        bool: 更新是否成功
    """
    db = get_database()
    return db.update_daily_batch_step_status(batch_date, step_number, status, error_message)


def mark_messages_as_summarized_by_ids(message_ids: List[int]) -> int:
    """
    根据消息ID列表批量标记消息为已摘要的便捷函数。
    
    Args:
        message_ids: 消息ID列表
        
    Returns:
        int: 更新的消息数量
    """
    db = get_database()
    return db.mark_messages_as_summarized_by_ids(message_ids)


def get_unsummarized_count_by_session(session_id: str) -> int:
    """
    获取指定会话中未摘要消息数量的便捷函数。
    
    Args:
        session_id: 会话ID
        
    Returns:
        int: 未摘要消息数量
    """
    db = get_database()
    return db.get_unsummarized_count_by_session(session_id)


def get_unsummarized_messages_by_session(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    获取指定会话中最早的未摘要消息列表的便捷函数。
    
    Args:
        session_id: 会话ID
        limit: 最大返回数量，默认为 50
        
    Returns:
        List[Dict[str, Any]]: 消息列表
    """
    db = get_database()
    return db.get_unsummarized_messages_by_session(session_id, limit)


if __name__ == "__main__":
    """数据库模块测试入口。"""
    import sys
    
    # 添加项目根目录到 Python 路径
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("测试消息数据库...")
    
    try:
        # 获取数据库实例
        db = get_database()
        print(f"数据库路径: {db.db_path}")
        
        # 测试保存消息
        test_session = "test_session_123"
        msg_id1 = db.save_message("user", "你好，这是一个测试消息", test_session)
        print(f"保存用户消息成功，ID: {msg_id1}")
        
        msg_id2 = db.save_message("assistant", "你好！我是AI助手，很高兴为您服务。", test_session)
        print(f"保存助手消息成功，ID: {msg_id2}")
        
        # 测试获取消息
        messages = db.get_recent_messages(test_session, limit=10)
        print(f"获取到 {len(messages)} 条消息:")
        for msg in messages:
            print(f"  [{msg['role']}] {msg['content'][:50]}...")
        
        # 测试消息计数
        count = db.get_session_count(test_session)
        print(f"会话消息数量: {count}")
        
        # 清理测试数据
        deleted = db.clear_session_messages(test_session)
        print(f"清理测试数据，删除 {deleted} 条消息")
        
        print("数据库测试完成！")
        
    except Exception as e:
        print(f"数据库测试失败: {e}")
        import traceback
        traceback.print_exc()