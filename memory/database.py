"""
短期记忆数据库模块。

使用 SQLite 存储对话消息，支持短期记忆功能。
"""

import sqlite3
import logging
import os
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

# 设置日志
logger = logging.getLogger(__name__)


def _summaries_ensure_source_date_column(cursor: sqlite3.Cursor) -> None:
    """为 summaries 增加 source_date 列（旧库兼容），并按 created_at 日期回填。"""
    cursor.execute("PRAGMA table_info(summaries)")
    columns = {row[1] for row in cursor.fetchall()}
    if "source_date" not in columns:
        cursor.execute("ALTER TABLE summaries ADD COLUMN source_date DATETIME")
        cursor.execute(
            "UPDATE summaries SET source_date = date(created_at) WHERE source_date IS NULL"
        )
        logger.debug("summaries 表添加 source_date 字段并完成历史回填")


def _daily_batch_log_ensure_step45_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(daily_batch_log)")
    cols = {row[1] for row in cursor.fetchall()}
    if "step4_status" not in cols:
        cursor.execute(
            "ALTER TABLE daily_batch_log ADD COLUMN step4_status INTEGER DEFAULT 0"
        )
        logger.debug("daily_batch_log 表添加 step4_status")
    if "step5_status" not in cols:
        cursor.execute(
            "ALTER TABLE daily_batch_log ADD COLUMN step5_status INTEGER DEFAULT 0"
        )
        logger.debug("daily_batch_log 表添加 step5_status")


def _backfill_daily_batch_step45_legacy_once(cursor: sqlite3.Cursor) -> None:
    """
    升级五步流水线前已「三步全完成」的历史行，step4/step5 曾为 0：按用户约定 SQL 一次性补为 1。
    通过 config 键保证全库仅执行一次，避免日后将真实 Step4/5 失败行误标为完成。
    """
    cursor.execute(
        "SELECT 1 FROM config WHERE key = ? LIMIT 1",
        ("backfill_daily_batch_step45_legacy_v1",),
    )
    if cursor.fetchone():
        return
    cursor.execute("""
        UPDATE daily_batch_log
        SET step4_status = 1, step5_status = 1
        WHERE step1_status = 1 AND step2_status = 1 AND step3_status = 1
    """)
    n = cursor.rowcount
    cursor.execute(
        """
        INSERT OR REPLACE INTO config (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        """,
        ("backfill_daily_batch_step45_legacy_v1", "1"),
    )
    logger.info(
        "一次性回填 daily_batch_log：三步已完成行的 step4/step5 已置 1，更新 %s 行",
        n,
    )


def migrate_database_schema(cursor: sqlite3.Cursor) -> None:
    """
    启动时幂等迁移：补齐缺失列与全部约定索引。

    通过 CREATE INDEX IF NOT EXISTS / 列检测实现「不存在则创建、已存在则跳过」。
    """
    _summaries_ensure_source_date_column(cursor)
    _daily_batch_log_ensure_step45_columns(cursor)
    _backfill_daily_batch_step45_legacy_once(cursor)

    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages (session_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_messages_is_summarized ON messages (is_summarized)",
        (
            "CREATE INDEX IF NOT EXISTS idx_messages_session_is_summarized "
            "ON messages (session_id, is_summarized)"
        ),
        "CREATE INDEX IF NOT EXISTS idx_summaries_session_id ON summaries (session_id, created_at)",
        (
            "CREATE INDEX IF NOT EXISTS idx_summaries_session_type_source_date "
            "ON summaries (session_id, summary_type, source_date)"
        ),
        "CREATE INDEX IF NOT EXISTS idx_summaries_source_date ON summaries (source_date)",
        (
            "CREATE INDEX IF NOT EXISTS idx_memory_cards_user_character "
            "ON memory_cards (user_id, character_id, dimension, updated_at)"
        ),
        "CREATE INDEX IF NOT EXISTS idx_memory_cards_user_active ON memory_cards (user_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_memory_cards_is_active ON memory_cards (is_active)",
        (
            "CREATE INDEX IF NOT EXISTS idx_temporal_states_expire_active "
            "ON temporal_states (expire_at, is_active)"
        ),
        "CREATE INDEX IF NOT EXISTS idx_temporal_states_is_active ON temporal_states (is_active)",
        (
            "CREATE INDEX IF NOT EXISTS idx_relationship_timeline_created_at "
            "ON relationship_timeline (created_at)"
        ),
        "CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_token_usage_created_at ON token_usage (created_at)",
    ]
    for sql in index_statements:
        cursor.execute(sql)

    logger.debug("数据库 schema 迁移（索引/列）已执行")


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
                        character_id TEXT,
                        platform TEXT DEFAULT 'discord',
                        thinking TEXT
                    )
                """)
                
                # 检查并添加 thinking 字段（如果不存在）
                cursor.execute("PRAGMA table_info(messages)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'thinking' not in columns:
                    cursor.execute("ALTER TABLE messages ADD COLUMN thinking TEXT")
                    logger.debug("messages 表添加 thinking 字段")
                
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
                        summary_type TEXT DEFAULT 'chunk',
                        source_date DATETIME
                    )
                """)
                
                # 创建 daily_batch_log 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS daily_batch_log (
                        batch_date DATE PRIMARY KEY,
                        step1_status INTEGER DEFAULT 0,
                        step2_status INTEGER DEFAULT 0,
                        step3_status INTEGER DEFAULT 0,
                        step4_status INTEGER DEFAULT 0,
                        step5_status INTEGER DEFAULT 0,
                        error_message TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 创建 logs 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        level TEXT NOT NULL,
                        platform TEXT,
                        message TEXT NOT NULL,
                        stack_trace TEXT
                    )
                """)
                
                # 创建 token_usage 表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        platform TEXT,
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        model TEXT
                    )
                """)
                
                # 创建 config 表（用于存储助手配置）
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 创建 longterm_memories 表（Mini App 展示用镜像表）
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS longterm_memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        chroma_doc_id TEXT,
                        score INTEGER DEFAULT 5,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS temporal_states (
                        id TEXT PRIMARY KEY,
                        state_content TEXT,
                        action_rule TEXT,
                        expire_at DATETIME,
                        is_active INTEGER DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS relationship_timeline (
                        id TEXT PRIMARY KEY,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        event_type TEXT NOT NULL CHECK (
                            event_type IN (
                                'milestone', 'emotional_shift', 'conflict', 'daily_warmth'
                            )
                        ),
                        content TEXT,
                        source_summary_id TEXT
                    )
                """)

                migrate_database_schema(cursor)

                conn.commit()
                
                logger.debug("数据库表初始化完成")
                
        except sqlite3.Error as e:
            logger.error(f"数据库初始化失败: {e}")
            raise
    
    def save_message(self, role: str, content: str, session_id: str, 
                    user_id: Optional[str] = None, channel_id: Optional[str] = None, 
                    message_id: Optional[str] = None, character_id: Optional[str] = None,
                    platform: Optional[str] = None) -> int:
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
            platform: 平台标识（可选），如 'discord', 'telegram'
            
        Returns:
            int: 插入的消息ID
            
        Raises:
            sqlite3.Error: 数据库操作失败
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO messages (role, content, session_id, user_id, channel_id, message_id, character_id, platform)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (role, content, session_id, user_id, channel_id, message_id, character_id, platform))
                
                message_id = cursor.lastrowid
                conn.commit()
                
                logger.debug(f"保存消息成功: ID={message_id}, role={role}, session={session_id}, platform={platform}")
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
    
    def get_all_messages(self) -> List[Dict[str, Any]]:
        """
        获取所有消息（用于历史查询）。
        
        Returns:
            List[Dict[str, Any]]: 消息列表，包含完整字段
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 设置行工厂，返回字典形式的结果
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT id, role, content, created_at, session_id, 
                           user_id, channel_id, message_id, character_id, 
                           platform, thinking, is_summarized
                    FROM messages
                    ORDER BY created_at DESC
                """)
                
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
                        'channel_id': row['channel_id'],
                        'message_id': row['message_id'],
                        'character_id': row['character_id'],
                        'platform': row['platform'],
                        'thinking': row['thinking'],
                        'is_summarized': bool(row['is_summarized'])
                    }
                    messages.append(message)
                
                logger.debug(f"获取所有消息，数量: {len(messages)}")
                return messages
                
        except sqlite3.Error as e:
            logger.error(f"获取所有消息失败: {e}")
            raise
    
    def get_messages_filtered(
        self,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        带过滤条件的消息查询（SQL 层过滤 + LIMIT/OFFSET 分页）。

        Args:
            platform: 平台过滤（'discord' / 'telegram'），None 表示不过滤
            keyword:  关键词，匹配 content 或 thinking 字段，None 表示不过滤
            date_from: 起始日期字符串 'YYYY-MM-DD'（包含），None 表示不限
            date_to:   结束日期字符串 'YYYY-MM-DD'（包含），None 表示不限
            page:      页码（从 1 开始）
            page_size: 每页条数

        Returns:
            {
                "total": int,
                "messages": List[Dict]
            }
        """
        try:
            conditions = []
            params: list = []

            if platform:
                conditions.append("platform = ?")
                params.append(platform)

            if keyword:
                conditions.append("(content LIKE ? OR thinking LIKE ?)")
                like = f"%{keyword}%"
                params.extend([like, like])

            if date_from:
                conditions.append("date(created_at) >= ?")
                params.append(date_from)

            if date_to:
                conditions.append("date(created_at) <= ?")
                params.append(date_to)

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # 查总条数
                cursor.execute(
                    f"SELECT COUNT(*) FROM messages {where_clause}",
                    params,
                )
                total = cursor.fetchone()[0]

                # 查分页数据
                offset = (page - 1) * page_size
                cursor.execute(
                    f"""
                    SELECT id, role, content, thinking, platform, created_at, session_id
                    FROM messages
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [page_size, offset],
                )
                rows = cursor.fetchall()

                messages = [
                    {
                        "id": row["id"],
                        "role": row["role"],
                        "content": row["content"],
                        "thinking": row["thinking"],
                        "platform": row["platform"],
                        "created_at": row["created_at"],
                        "session_id": row["session_id"],
                    }
                    for row in rows
                ]

            logger.debug(
                f"get_messages_filtered: total={total}, page={page}, "
                f"page_size={page_size}, platform={platform}, keyword={keyword}"
            )
            return {"total": total, "messages": messages}

        except sqlite3.Error as e:
            logger.error(f"get_messages_filtered 失败: {e}")
            raise

    def get_logs_filtered(
        self,
        platform: Optional[str] = None,
        level: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        带过滤条件的日志查询（SQL 层过滤 + LIMIT/OFFSET 分页）。

        Args:
            platform: 平台过滤，None 表示不过滤
            level:    日志级别（'INFO'/'WARNING'/'ERROR' 等），None 表示不过滤
            keyword:  关键词，匹配 message 或 stack_trace 字段，None 表示不过滤
            page:     页码（从 1 开始）
            page_size: 每页条数

        Returns:
            {
                "total": int,
                "logs": List[Dict]
            }
        """
        try:
            conditions = []
            params: list = []

            if platform:
                conditions.append("platform = ?")
                params.append(platform)

            if level:
                conditions.append("level = ?")
                params.append(level.upper())

            if keyword:
                conditions.append("(message LIKE ? OR stack_trace LIKE ?)")
                like = f"%{keyword}%"
                params.extend([like, like])

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # 查总条数
                cursor.execute(
                    f"SELECT COUNT(*) FROM logs {where_clause}",
                    params,
                )
                total = cursor.fetchone()[0]

                # 查分页数据
                offset = (page - 1) * page_size
                cursor.execute(
                    f"""
                    SELECT id, created_at, level, platform, message, stack_trace
                    FROM logs
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [page_size, offset],
                )
                rows = cursor.fetchall()

                logs = [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "level": row["level"],
                        "platform": row["platform"],
                        "message": row["message"],
                        "stack_trace": row["stack_trace"],
                    }
                    for row in rows
                ]

            logger.debug(
                f"get_logs_filtered: total={total}, page={page}, "
                f"page_size={page_size}, platform={platform}, level={level}"
            )
            return {"total": total, "logs": logs}

        except sqlite3.Error as e:
            logger.error(f"get_logs_filtered 失败: {e}")
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
                
                source_date = datetime.now().strftime("%Y-%m-%d")
                cursor.execute("""
                    INSERT INTO summaries (
                        session_id, summary_text, start_message_id, end_message_id,
                        summary_type, source_date
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    session_id, summary_text, start_message_id, end_message_id,
                    summary_type, source_date,
                ))
                
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
    
    def get_all_active_memory_cards(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取所有激活的记忆卡片（全局查询）。
        
        Args:
            limit: 最大返回数量，默认为 100
            
        Returns:
            List[Dict[str, Any]]: 记忆卡片列表，按维度和更新时间排序
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT id, user_id, character_id, dimension, content, 
                           updated_at, source_message_id, is_active
                    FROM memory_cards
                    WHERE is_active = 1
                    ORDER BY dimension ASC, updated_at DESC
                    LIMIT ?
                """, (limit,))
                
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
                
                logger.debug(f"获取所有激活记忆卡片: count={len(cards)}")
                return cards
                
        except sqlite3.Error as e:
            logger.error(f"获取所有激活记忆卡片失败: {e}")
            raise
    
    def get_recent_daily_summaries(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        获取最近的每日摘要（全局查询，按 created_at 倒序）。
        
        Args:
            limit: 最大返回数量，默认为 5
            
        Returns:
            List[Dict[str, Any]]: 每日摘要列表，按创建时间倒序
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT id, session_id, summary_text, start_message_id, end_message_id, 
                           created_at, summary_type
                    FROM summaries
                    WHERE summary_type = 'daily'
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))
                
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
                
                logger.debug(f"获取最近每日摘要: count={len(summaries)}")
                return summaries
                
        except sqlite3.Error as e:
            logger.error(f"获取最近每日摘要失败: {e}")
            raise
    
    def get_today_chunk_summaries(self) -> List[Dict[str, Any]]:
        """
        获取今天的所有 chunk 摘要（全局查询，不按 session_id 筛选）。
        
        返回按 created_at 正序排列的结果。
        
        Returns:
            List[Dict[str, Any]]: 今天的 chunk 摘要列表，按创建时间正序
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # 使用 date() 函数获取今天的日期
                cursor.execute("""
                    SELECT id, session_id, summary_text, start_message_id, end_message_id, 
                           created_at, summary_type
                    FROM summaries
                    WHERE summary_type = 'chunk' 
                      AND date(created_at) = date('now', 'localtime')
                    ORDER BY created_at ASC
                """)
                
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
                
                logger.debug(f"获取今天的 chunk 摘要: count={len(summaries)}")
                return summaries
                
        except sqlite3.Error as e:
            logger.error(f"获取今天的 chunk 摘要失败: {e}")
            raise
    
    def get_unsummarized_messages_desc(self, session_id: str, limit: int = 40) -> List[Dict[str, Any]]:
        """
        获取指定会话中最新的未摘要消息列表（用于 context 构建）。
        
        按消息创建时间倒序获取 limit 条，然后返回时翻转为正序。
        
        Args:
            session_id: 会话ID
            limit: 最大返回数量，默认为 40
            
        Returns:
            List[Dict[str, Any]]: 消息列表，按创建时间正序排列（最旧的在前）
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # 先按时间倒序取最新的 limit 条
                cursor.execute("""
                    SELECT id, role, content, created_at, session_id, user_id, channel_id
                    FROM messages
                    WHERE session_id = ? AND is_summarized = 0
                    ORDER BY created_at DESC
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
                
                # 翻转为正序（最旧的在前）
                messages.reverse()
                
                logger.debug(f"获取会话 {session_id} 的最新未摘要消息（正序）: {len(messages)} 条")
                return messages
                
        except sqlite3.Error as e:
            logger.error(f"获取最新未摘要消息失败: {e}")
            raise
    
    def save_daily_batch_log(
        self,
        batch_date: str,
        step1_status: int = 0,
        step2_status: int = 0,
        step3_status: int = 0,
        step4_status: int = 0,
        step5_status: int = 0,
        error_message: Optional[str] = None,
    ) -> bool:
        """
        保存或更新每日批处理日志。
        
        Args:
            batch_date: 批处理日期，格式为 'YYYY-MM-DD'
            step1_status ~ step5_status: 各步状态，0=未开始，1=已完成
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
                    (batch_date, step1_status, step2_status, step3_status, step4_status, step5_status,
                     error_message, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    batch_date, step1_status, step2_status, step3_status,
                    step4_status, step5_status, error_message,
                ))
                
                conn.commit()
                
                logger.debug(
                    f"保存每日批处理日志成功: date={batch_date}, "
                    f"step1={step1_status}, step2={step2_status}, step3={step3_status}, "
                    f"step4={step4_status}, step5={step5_status}"
                )
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
                    SELECT batch_date, step1_status, step2_status, step3_status, step4_status, step5_status,
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
                        'step4_status': (
                            0 if row['step4_status'] is None else int(row['step4_status'])
                        ),
                        'step5_status': (
                            0 if row['step5_status'] is None else int(row['step5_status'])
                        ),
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
                    SELECT batch_date, step1_status, step2_status, step3_status, step4_status, step5_status,
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
                        'step4_status': (
                            0 if row['step4_status'] is None else int(row['step4_status'])
                        ),
                        'step5_status': (
                            0 if row['step5_status'] is None else int(row['step5_status'])
                        ),
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
            ValueError: 如果 step_number 不在 1-5 范围内
        """
        if step_number not in {1, 2, 3, 4, 5}:
            raise ValueError(f"步骤编号 {step_number} 无效，必须是 1 至 5")
        
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
                elif step_number == 3:
                    cursor.execute("""
                        UPDATE daily_batch_log 
                        SET step3_status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE batch_date = ?
                    """, (status, error_message, batch_date))
                elif step_number == 4:
                    cursor.execute("""
                        UPDATE daily_batch_log 
                        SET step4_status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE batch_date = ?
                    """, (status, error_message, batch_date))
                else:
                    cursor.execute("""
                        UPDATE daily_batch_log 
                        SET step5_status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
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

    _DAILY_BATCH_INCOMPLETE_SQL = """(
            IFNULL(step1_status, 0) = 0 OR IFNULL(step2_status, 0) = 0 OR
            IFNULL(step3_status, 0) = 0 OR IFNULL(step4_status, 0) = 0 OR
            IFNULL(step5_status, 0) = 0
        )"""

    def list_incomplete_daily_batch_dates_in_range(
        self, start_date: str, end_date: str
    ) -> List[str]:
        """
        列出 batch_date 在 [start_date, end_date]（含）且五步未全部完成的日期，升序。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT batch_date FROM daily_batch_log
                    WHERE batch_date >= ? AND batch_date <= ?
                      AND {self._DAILY_BATCH_INCOMPLETE_SQL}
                    ORDER BY batch_date ASC
                    """,
                    (start_date, end_date),
                )
                return [str(row[0]) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"查询未完成 daily_batch_log 失败: {e}")
            raise

    def mark_expired_skipped_daily_batch_logs_before(self, before_date: str) -> int:
        """
        batch_date 早于 before_date 且仍有未完成步骤的行：五步均置 1，
        error_message='expired, skipped'。返回更新行数。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    UPDATE daily_batch_log
                    SET step1_status = 1, step2_status = 1, step3_status = 1,
                        step4_status = 1, step5_status = 1,
                        error_message = 'expired, skipped',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE batch_date < ?
                      AND {self._DAILY_BATCH_INCOMPLETE_SQL}
                    """,
                    (before_date,),
                )
                n = cursor.rowcount
                conn.commit()
                if n:
                    logger.info(
                        "已将 %s 条超窗未完成的 daily_batch_log 标记为 expired, skipped",
                        n,
                    )
                return n
        except sqlite3.Error as e:
            logger.error(f"标记过期 daily_batch_log 失败: {e}")
            raise

    RELATIONSHIP_TIMELINE_EVENT_TYPES = frozenset({
        "milestone", "emotional_shift", "conflict", "daily_warmth",
    })

    def list_expired_active_temporal_states(self, as_of_iso: str) -> List[Dict[str, Any]]:
        """
        列出已到期且仍激活的 temporal_states（expire_at <= as_of_iso，is_active=1）。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, state_content, action_rule, expire_at, created_at
                    FROM temporal_states
                    WHERE is_active = 1
                      AND expire_at IS NOT NULL
                      AND datetime(expire_at) <= datetime(?)
                    ORDER BY expire_at ASC
                    """,
                    (as_of_iso,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"查询到期 temporal_states 失败: {e}")
            raise

    def deactivate_temporal_states_by_ids(self, state_ids: List[str]) -> int:
        """将给定 id 的 temporal_states 设为 is_active=0。"""
        if not state_ids:
            return 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                placeholders = ",".join("?" * len(state_ids))
                cursor.execute(
                    f"""
                    UPDATE temporal_states
                    SET is_active = 0
                    WHERE id IN ({placeholders})
                    """,
                    state_ids,
                )
                n = cursor.rowcount
                conn.commit()
                return n
        except sqlite3.Error as e:
            logger.error(f"停用 temporal_states 失败: {e}")
            raise

    def insert_relationship_timeline_event(
        self,
        event_type: str,
        content: str,
        source_summary_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> str:
        """
        插入一条 relationship_timeline。返回主键 id（UUID 字符串）。
        """
        if event_type not in self.RELATIONSHIP_TIMELINE_EVENT_TYPES:
            raise ValueError(
                f"event_type 无效: {event_type}，允许: {self.RELATIONSHIP_TIMELINE_EVENT_TYPES}"
            )
        eid = event_id or uuid.uuid4().hex
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO relationship_timeline (id, event_type, content, source_summary_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (eid, event_type, content, source_summary_id),
                )
                conn.commit()
                logger.debug(
                    "relationship_timeline 插入成功 id=%s type=%s", eid, event_type
                )
                return eid
        except sqlite3.Error as e:
            logger.error(f"插入 relationship_timeline 失败: {e}")
            raise

    def get_all_active_temporal_states(self) -> List[Dict[str, Any]]:
        """
        获取 temporal_states 中 is_active=1 的全部记录（按 created_at 升序，先写入的在前）。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, state_content, action_rule, expire_at, is_active, created_at
                    FROM temporal_states
                    WHERE is_active = 1
                    ORDER BY created_at ASC
                    """
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error(f"查询激活的 temporal_states 失败: {e}")
            return []

    def get_recent_relationship_timeline(self, limit: int = 3) -> List[Dict[str, Any]]:
        """
        按 created_at 倒序取 relationship_timeline 前 limit 条。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, created_at, event_type, content, source_summary_id
                    FROM relationship_timeline
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error(f"查询 relationship_timeline 失败: {e}")
            return []

    def save_log(self, level: str, message: str, platform: Optional[str] = None, 
                stack_trace: Optional[str] = None) -> int:
        """
        保存日志到数据库。
        
        Args:
            level: 日志级别（INFO/WARNING/ERROR）
            message: 日志消息
            platform: 平台标识（可选）
            stack_trace: 堆栈跟踪信息（可选）
            
        Returns:
            int: 插入的日志ID
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO logs (level, platform, message, stack_trace)
                    VALUES (?, ?, ?, ?)
                """, (level, platform, message, stack_trace))
                
                log_id = cursor.lastrowid
                conn.commit()
                
                logger.debug(f"保存日志成功: ID={log_id}, level={level}, platform={platform}")
                return log_id
                
        except sqlite3.Error as e:
            logger.error(f"保存日志失败: {e}")
            raise
    
    def get_all_logs(self) -> List[Dict[str, Any]]:
        """
        获取所有日志（用于日志查询）。
        
        Returns:
            List[Dict[str, Any]]: 日志列表，包含完整字段
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 设置行工厂，返回字典形式的结果
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT id, created_at, level, platform, message, stack_trace
                    FROM logs
                    ORDER BY created_at DESC
                """)
                
                rows = cursor.fetchall()
                
                logs = []
                for row in rows:
                    log = {
                        'id': row['id'],
                        'created_at': row['created_at'],
                        'level': row['level'],
                        'platform': row['platform'],
                        'message': row['message'],
                        'stack_trace': row['stack_trace']
                    }
                    logs.append(log)
                
                logger.debug(f"获取所有日志，数量: {len(logs)}")
                return logs
                
        except sqlite3.Error as e:
            logger.error(f"获取所有日志失败: {e}")
            raise
    
    def save_token_usage(self, prompt_tokens: int, completion_tokens: int, 
                        total_tokens: int, model: str, platform: Optional[str] = None) -> int:
        """
        保存token使用量到数据库。
        
        Args:
            prompt_tokens: 提示token数
            completion_tokens: 完成token数
            total_tokens: 总token数
            model: 模型名称
            platform: 平台标识（可选）
            
        Returns:
            int: 插入的token使用记录ID
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO token_usage (platform, prompt_tokens, completion_tokens, total_tokens, model)
                    VALUES (?, ?, ?, ?, ?)
                """, (platform, prompt_tokens, completion_tokens, total_tokens, model))
                
                usage_id = cursor.lastrowid
                conn.commit()
                
                logger.debug(f"保存token使用量成功: ID={usage_id}, model={model}, total_tokens={total_tokens}")
                return usage_id
                
        except sqlite3.Error as e:
            logger.error(f"保存token使用量失败: {e}")
            raise
    
    def update_message_with_thinking(self, message_id: int, thinking: str) -> bool:
        """
        更新消息的思维链内容。
        
        Args:
            message_id: 消息ID
            thinking: 思维链内容
            
        Returns:
            bool: 更新是否成功
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    UPDATE messages 
                    SET thinking = ?
                    WHERE id = ?
                """, (thinking, message_id))
                
                updated = cursor.rowcount > 0
                conn.commit()
                
                if updated:
                    logger.debug(f"更新消息思维链成功: message_id={message_id}")
                else:
                    logger.warning(f"更新消息思维链失败: message_id={message_id} 不存在")
                
                return updated
                
        except sqlite3.Error as e:
            logger.error(f"更新消息思维链失败: {e}")
            raise
    
    def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        获取配置值。
        
        Args:
            key: 配置键名
            default: 默认值（如果配置不存在）
            
        Returns:
            Optional[str]: 配置值，如果不存在则返回默认值
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT value FROM config WHERE key = ?
                """, (key,))
                
                result = cursor.fetchone()
                if result:
                    return result[0]
                else:
                    return default
                
        except sqlite3.Error as e:
            logger.error(f"获取配置失败: {e}")
            return default
    
    def set_config(self, key: str, value: str) -> bool:
        """
        设置配置值。
        
        Args:
            key: 配置键名
            value: 配置值
            
        Returns:
            bool: 设置是否成功
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT OR REPLACE INTO config (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (key, value))
                
                conn.commit()
                logger.debug(f"设置配置成功: {key}={value}")
                return True
                
        except sqlite3.Error as e:
            logger.error(f"设置配置失败: {e}")
            return False
    
    def get_all_configs(self) -> Dict[str, str]:
        """
        获取所有配置。

        Returns:
            Dict[str, str]: 配置字典，键值对形式
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT key, value FROM config
                """)

                configs = {}
                for row in cursor.fetchall():
                    configs[row[0]] = row[1]

                logger.debug(f"获取所有配置成功: {len(configs)} 条")
                return configs

        except sqlite3.Error as e:
            logger.error(f"获取所有配置失败: {e}")
            return {}

    # ==========================================
    # persona_configs CRUD
    # ==========================================

    def get_all_persona_configs(self) -> List[Dict[str, Any]]:
        """获取所有人设配置列表（仅返回 id, name, created_at, updated_at）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, name, created_at, updated_at FROM persona_configs ORDER BY id ASC"
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"获取人设列表失败: {e}")
            return []

    def get_persona_config(self, persona_id: int) -> Optional[Dict[str, Any]]:
        """获取单个人设配置详情。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM persona_configs WHERE id = ?", (persona_id,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"获取人设详情失败: {e}")
            return None

    def save_persona_config(self, data: Dict[str, Any]) -> int:
        """新增人设配置，返回新插入的 id。"""
        fields = [
            'name', 'char_name', 'char_personality', 'char_speech_style', 
            'user_name', 'user_body', 'user_habits',
            'user_likes_dislikes', 'user_values', 'user_hobbies', 'user_taboos',
            'user_nsfw', 'user_other', 'system_rules'
        ]
        cols = ', '.join(fields)
        placeholders = ', '.join(['?'] * len(fields))
        values = [data.get(f, '') for f in fields]
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"INSERT INTO persona_configs ({cols}) VALUES ({placeholders})",
                    values
                )
                conn.commit()
                return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"新增人设失败: {e}")
            return -1

    def update_persona_config(self, persona_id: int, data: Dict[str, Any]) -> bool:
        """更新人设配置。"""
        allowed = {
            'name', 'char_name', 'char_personality', 'char_speech_style',
            'user_name', 'user_body', 'user_habits',
            'user_likes_dislikes', 'user_values', 'user_hobbies', 'user_taboos',
            'user_nsfw', 'user_other', 'system_rules'
        }
        update_data = {k: v for k, v in data.items() if k in allowed}
        if not update_data:
            return False
        set_clause = ', '.join([f"{k} = ?" for k in update_data.keys()])
        values = list(update_data.values()) + [persona_id]
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"UPDATE persona_configs SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    values
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"更新人设失败: {e}")
            return False

    def delete_persona_config(self, persona_id: int) -> bool:
        """删除人设配置。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM persona_configs WHERE id = ?", (persona_id,))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"删除人设失败: {e}")
            return False

    # ==========================================
    # api_configs CRUD
    # ==========================================

    def _ensure_api_configs_table(self, cursor):
        """确保 api_configs 表存在，并自动补全缺失字段。"""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT,
                persona_id INTEGER,
                is_active INTEGER DEFAULT 0,
                config_type TEXT DEFAULT 'chat',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 检查并补全可能缺失的字段（兼容旧数据库）
        cursor.execute("PRAGMA table_info(api_configs)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        migrations = [
            ("model", "TEXT"),
            ("persona_id", "INTEGER"),
            ("is_active", "INTEGER DEFAULT 0"),
            ("config_type", "TEXT DEFAULT 'chat'"),
        ]
        for col, col_def in migrations:
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE api_configs ADD COLUMN {col} {col_def}")

    def get_all_api_configs(self, config_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有 API 配置列表，带关联人设名称。可按 config_type 过滤。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                if config_type:
                    cursor.execute("""
                        SELECT a.id, a.name, a.api_key, a.base_url, a.model,
                               a.persona_id, a.is_active, a.config_type,
                               a.created_at, a.updated_at,
                               p.name AS persona_name
                        FROM api_configs a
                        LEFT JOIN persona_configs p ON a.persona_id = p.id
                        WHERE a.config_type = ?
                        ORDER BY a.id ASC
                    """, (config_type,))
                else:
                    cursor.execute("""
                        SELECT a.id, a.name, a.api_key, a.base_url, a.model,
                               a.persona_id, a.is_active, a.config_type,
                               a.created_at, a.updated_at,
                               p.name AS persona_name
                        FROM api_configs a
                        LEFT JOIN persona_configs p ON a.persona_id = p.id
                        ORDER BY a.id ASC
                    """)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"获取 API 配置列表失败: {e}")
            return []

    def get_api_config(self, config_id: int) -> Optional[Dict[str, Any]]:
        """获取单个 API 配置。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                cursor.execute("SELECT * FROM api_configs WHERE id = ?", (config_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"获取 API 配置失败: {e}")
            return None

    def save_api_config(self, data: Dict[str, Any]) -> int:
        """新增 API 配置，返回新 id。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                cursor.execute("""
                    INSERT INTO api_configs (name, api_key, base_url, model, persona_id, config_type)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    data.get('name', ''),
                    data.get('api_key', ''),
                    data.get('base_url', ''),
                    data.get('model'),
                    data.get('persona_id'),
                    data.get('config_type', 'chat'),
                ))
                conn.commit()
                return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"新增 API 配置失败: {e}")
            return -1

    def update_api_config(self, config_id: int, data: Dict[str, Any]) -> bool:
        """更新 API 配置。"""
        allowed = {'name', 'api_key', 'base_url', 'model', 'persona_id', 'config_type'}
        update_data = {k: v for k, v in data.items() if k in allowed}
        if not update_data:
            return False
        set_clause = ', '.join([f"{k} = ?" for k in update_data.keys()])
        values = list(update_data.values()) + [config_id]
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                cursor.execute(
                    f"UPDATE api_configs SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    values
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"更新 API 配置失败: {e}")
            return False

    def delete_api_config(self, config_id: int) -> bool:
        """删除 API 配置。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                cursor.execute("DELETE FROM api_configs WHERE id = ?", (config_id,))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"删除 API 配置失败: {e}")
            return False

    def activate_api_config(self, config_id: int) -> bool:
        """激活指定配置（同类型内唯一激活：先清除同类型所有激活，再设置指定条目）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                # 查出该条目的 config_type
                cursor.execute("SELECT config_type FROM api_configs WHERE id = ?", (config_id,))
                row = cursor.fetchone()
                if not row:
                    return False
                cfg_type = row[0] or 'chat'
                # 只清除同类型的激活状态
                cursor.execute(
                    "UPDATE api_configs SET is_active = 0 WHERE config_type = ?",
                    (cfg_type,)
                )
                cursor.execute(
                    "UPDATE api_configs SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (config_id,)
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"激活 API 配置失败: {e}")
            return False

    def get_active_api_config(self, config_type: str = 'chat') -> Optional[Dict[str, Any]]:
        """获取指定类型的激活配置。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                self._ensure_api_configs_table(cursor)
                cursor.execute(
                    "SELECT * FROM api_configs WHERE config_type = ? AND is_active = 1 LIMIT 1",
                    (config_type,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"获取激活 API 配置失败: {e}")
            return None

    # ==========================================
    # longterm_memories CRUD
    # ==========================================

    def create_longterm_memory(self, content: str, chroma_doc_id: Optional[str] = None, score: int = 5) -> int:
        """新增一条长期记忆镜像记录，返回新 id。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO longterm_memories (content, chroma_doc_id, score)
                    VALUES (?, ?, ?)
                """, (content, chroma_doc_id, score))
                conn.commit()
                return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"新增长期记忆失败: {e}")
            raise

    def get_longterm_memories(self, keyword: str = "", page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """查询长期记忆（支持关键词搜索和分页）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                if keyword:
                    count_sql = "SELECT COUNT(*) FROM longterm_memories WHERE content LIKE ?"
                    data_sql = """
                        SELECT id, content, chroma_doc_id, score, created_at
                        FROM longterm_memories WHERE content LIKE ?
                        ORDER BY created_at DESC LIMIT ? OFFSET ?
                    """
                    like = f"%{keyword}%"
                    cursor.execute(count_sql, (like,))
                    total = cursor.fetchone()[0]
                    offset = (page - 1) * page_size
                    cursor.execute(data_sql, (like, page_size, offset))
                else:
                    cursor.execute("SELECT COUNT(*) FROM longterm_memories")
                    total = cursor.fetchone()[0]
                    offset = (page - 1) * page_size
                    cursor.execute("""
                        SELECT id, content, chroma_doc_id, score, created_at
                        FROM longterm_memories ORDER BY created_at DESC LIMIT ? OFFSET ?
                    """, (page_size, offset))

                items = [dict(row) for row in cursor.fetchall()]
                total_pages = max(1, (total + page_size - 1) // page_size)

                return {
                    "items": items,
                    "total_items": total,
                    "total_pages": total_pages,
                    "current_page": page,
                    "page_size": page_size,
                }
        except sqlite3.Error as e:
            logger.error(f"查询长期记忆失败: {e}")
            raise

    def get_longterm_memory(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """获取单条长期记忆（用于删除时获取 chroma_doc_id）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, content, chroma_doc_id, score, created_at FROM longterm_memories WHERE id = ?",
                    (memory_id,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"获取长期记忆失败: {e}")
            return None

    def delete_longterm_memory(self, memory_id: int) -> bool:
        """删除长期记忆镜像记录。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM longterm_memories WHERE id = ?", (memory_id,))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"删除长期记忆失败: {e}")
            return False

    # ==========================================
    # token_usage 统计
    # ==========================================

    def get_token_usage_stats(self, start_date, platform: Optional[str] = None) -> Dict[str, Any]:
        """
        统计从 start_date 开始的 token 使用量。

        Returns:
            {
                total_tokens, prompt_tokens, completion_tokens, call_count,
                by_platform: {telegram: N, discord: N}
            }
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                start_str = start_date.strftime('%Y-%m-%d %H:%M:%S') if hasattr(start_date, 'strftime') else str(start_date)

                base_cond = "WHERE created_at >= ?"
                params = [start_str]
                if platform:
                    base_cond += " AND platform = ?"
                    params.append(platform)

                # 总量
                cursor.execute(f"""
                    SELECT SUM(total_tokens), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*)
                    FROM token_usage {base_cond}
                """, params)
                row = cursor.fetchone()
                total = row[0] or 0
                prompt = row[1] or 0
                completion = row[2] or 0
                count = row[3] or 0

                # 按平台分
                cursor.execute(f"""
                    SELECT platform, SUM(total_tokens)
                    FROM token_usage {base_cond}
                    GROUP BY platform
                """, params)
                by_platform = {}
                for r in cursor.fetchall():
                    if r[0]:
                        by_platform[r[0]] = r[1] or 0

                return {
                    'total_tokens': total,
                    'prompt_tokens': prompt,
                    'completion_tokens': completion,
                    'call_count': count,
                    'by_platform': by_platform,
                }
        except sqlite3.Error as e:
            logger.error(f"获取 token 统计失败: {e}")
            return {
                'total_tokens': 0, 'prompt_tokens': 0,
                'completion_tokens': 0, 'call_count': 0, 'by_platform': {}
            }


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
                message_id: Optional[str] = None, character_id: Optional[str] = None,
                platform: Optional[str] = None) -> int:
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
        platform: 平台标识（可选），如 'discord', 'telegram'
        
    Returns:
        int: 消息ID
    """
    db = get_database()
    return db.save_message(role, content, session_id, user_id, channel_id, message_id, character_id, platform)


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


def save_daily_batch_log(
    batch_date: str,
    step1_status: int = 0,
    step2_status: int = 0,
    step3_status: int = 0,
    step4_status: int = 0,
    step5_status: int = 0,
    error_message: Optional[str] = None,
) -> bool:
    """
    保存或更新每日批处理日志的便捷函数。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'
        step1_status ~ step5_status: 各步状态，0=未开始，1=已完成
        error_message: 错误信息（可选）
        
    Returns:
        bool: 操作是否成功
    """
    db = get_database()
    return db.save_daily_batch_log(
        batch_date, step1_status, step2_status, step3_status,
        step4_status, step5_status, error_message,
    )


def list_expired_active_temporal_states(as_of_iso: str) -> List[Dict[str, Any]]:
    return get_database().list_expired_active_temporal_states(as_of_iso)


def deactivate_temporal_states_by_ids(state_ids: List[str]) -> int:
    return get_database().deactivate_temporal_states_by_ids(state_ids)


def get_all_active_temporal_states() -> List[Dict[str, Any]]:
    """获取 is_active=1 的全部 temporal_states。"""
    return get_database().get_all_active_temporal_states()


def get_recent_relationship_timeline(limit: int = 3) -> List[Dict[str, Any]]:
    """按 created_at 倒序取 relationship_timeline 前 limit 条。"""
    return get_database().get_recent_relationship_timeline(limit)


def insert_relationship_timeline_event(
    event_type: str,
    content: str,
    source_summary_id: Optional[str] = None,
    event_id: Optional[str] = None,
) -> str:
    return get_database().insert_relationship_timeline_event(
        event_type, content, source_summary_id, event_id
    )


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


def list_incomplete_daily_batch_dates_in_range(
    start_date: str, end_date: str,
) -> List[str]:
    return get_database().list_incomplete_daily_batch_dates_in_range(
        start_date, end_date,
    )


def mark_expired_skipped_daily_batch_logs_before(before_date: str) -> int:
    return get_database().mark_expired_skipped_daily_batch_logs_before(before_date)


def update_daily_batch_step_status(batch_date: str, step_number: int, 
                                  status: int, error_message: Optional[str] = None) -> bool:
    """
    更新指定日期的批处理步骤状态的便捷函数。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'
        step_number: 步骤编号（1 至 5）
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


def get_all_active_memory_cards(limit: int = 100) -> List[Dict[str, Any]]:
    """
    获取所有激活的记忆卡片的便捷函数。
    
    Args:
        limit: 最大返回数量，默认为 100
        
    Returns:
        List[Dict[str, Any]]: 记忆卡片列表
    """
    db = get_database()
    return db.get_all_active_memory_cards(limit)


def get_recent_daily_summaries(limit: int = 5) -> List[Dict[str, Any]]:
    """
    获取最近的每日摘要的便捷函数。
    
    Args:
        limit: 最大返回数量，默认为 5
        
    Returns:
        List[Dict[str, Any]]: 每日摘要列表
    """
    db = get_database()
    return db.get_recent_daily_summaries(limit)


def get_today_chunk_summaries() -> List[Dict[str, Any]]:
    """
    获取今天的所有 chunk 摘要的便捷函数。
    
    Returns:
        List[Dict[str, Any]]: 今天的 chunk 摘要列表
    """
    db = get_database()
    return db.get_today_chunk_summaries()


def get_unsummarized_messages_desc(session_id: str, limit: int = 40) -> List[Dict[str, Any]]:
    """
    获取指定会话中最新的未摘要消息列表的便捷函数（用于 context 构建）。
    
    Args:
        session_id: 会话ID
        limit: 最大返回数量，默认为 40
        
    Returns:
        List[Dict[str, Any]]: 消息列表，按创建时间正序排列
    """
    db = get_database()
    return db.get_unsummarized_messages_desc(session_id, limit)


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