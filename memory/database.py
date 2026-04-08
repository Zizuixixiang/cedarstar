"""
短期记忆数据库模块（PostgreSQL / asyncpg 版本）。

使用 asyncpg 连接池操作 PostgreSQL，所有数据库方法均为 async def。
连接池在 MessageDatabase.init_pool() 中创建；全局单例通过 get_database()
获取，startup 阶段需调用一次 await get_database().init_pool(dsn) 或
await initialize_database()。
"""

import asyncpg
import logging
import datetime as _dt
import uuid
from datetime import date
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块级常量
# ---------------------------------------------------------------------------

VISION_FAIL_CAPTION_SHORT = "[视觉解析失败]"
VISION_FAIL_CAPTION_TIMEOUT = "[系统提示：视觉解析超时失败]"
_IMAGE_CAPTION_FALLBACKS_MARK_SUMMARIZED = frozenset(
    {VISION_FAIL_CAPTION_SHORT, VISION_FAIL_CAPTION_TIMEOUT}
)


# ---------------------------------------------------------------------------
# asyncpg Record → dict 辅助（保持与 SQLite 版返回格式一致）
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Any:
    """将 asyncpg 返回的 datetime/date 对象转为 ISO 字符串，保持上层兼容。"""
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _r(record) -> Dict[str, Any]:
    """asyncpg Record → dict（所有 datetime 值转为字符串）。"""
    return {k: _norm(v) for k, v in dict(record).items()}


def _rows(records) -> List[Dict[str, Any]]:
    """asyncpg Record 列表 → List[dict]。"""
    return [_r(rec) for rec in records]


def _rowcount(status: str) -> int:
    """解析 asyncpg execute() 返回的状态字符串，提取受影响行数。
    示例：'UPDATE 3' → 3，'DELETE 0' → 0，'INSERT 0 1' → 1。
    """
    try:
        return int(status.split()[-1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Schema 迁移帮助函数（异步，接受 asyncpg connection）
# ---------------------------------------------------------------------------

async def _summaries_ensure_source_date_column(conn) -> None:
    """为 summaries 补 source_date 列并回填（幂等）。"""
    await conn.execute(
        "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS source_date TIMESTAMP"
    )
    await conn.execute(
        "UPDATE summaries SET source_date = created_at::date WHERE source_date IS NULL"
    )
    logger.debug("summaries.source_date 列检查/回填完成")


async def _daily_batch_log_ensure_step45_columns(conn) -> None:
    """为 daily_batch_log 补 step4/step5 列（幂等）。"""
    await conn.execute(
        "ALTER TABLE daily_batch_log ADD COLUMN IF NOT EXISTS step4_status INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE daily_batch_log ADD COLUMN IF NOT EXISTS step5_status INTEGER DEFAULT 0"
    )
    logger.debug("daily_batch_log step4/step5 列检查完成")


async def _backfill_daily_batch_step45_legacy_once(conn) -> None:
    """
    升级五步流水线前已「三步全完成」的历史行，step4/step5 为 0：按约定一次性补为 1。
    通过 config 键保证全库仅执行一次。
    """
    row = await conn.fetchrow(
        "SELECT 1 FROM config WHERE key = $1 LIMIT 1",
        "backfill_daily_batch_step45_legacy_v1",
    )
    if row:
        return
    result = await conn.execute("""
        UPDATE daily_batch_log
        SET step4_status = 1, step5_status = 1
        WHERE step1_status = 1 AND step2_status = 1 AND step3_status = 1
    """)
    n = _rowcount(result)
    await conn.execute(
        """
        INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        "backfill_daily_batch_step45_legacy_v1",
        "1",
    )
    logger.info(
        "一次性回填 daily_batch_log：三步已完成行的 step4/step5 已置 1，更新 %s 行", n
    )


async def _messages_ensure_vision_columns(conn) -> None:
    """为 messages 补图片/视觉相关列（幂等）。"""
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS image_caption TEXT"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS vision_processed INTEGER NOT NULL DEFAULT 1"
    )
    logger.debug("messages 视觉相关列检查完成")


async def _ensure_sticker_cache_table(conn) -> None:
    """Telegram 贴纸缓存表：CREATE IF NOT EXISTS（幂等）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sticker_cache (
            file_unique_id TEXT PRIMARY KEY,
            emoji TEXT,
            sticker_set_name TEXT,
            description TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)


async def _config_insert_defaults_if_missing(
    conn, defaults: List[Tuple[str, str]]
) -> None:
    """为 config 表补默认行（ON CONFLICT DO NOTHING，不覆盖用户已改值）。"""
    for key, val in defaults:
        await conn.execute(
            """
            INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT DO NOTHING
            """,
            key,
            str(val),
        )


async def ensure_api_configs_schema(conn) -> None:
    """创建 api_configs 表并补全缺失列（与 MessageDatabase._ensure_api_configs_table 一致）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS api_configs (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            api_key TEXT NOT NULL,
            base_url TEXT NOT NULL,
            model TEXT,
            persona_id INTEGER,
            is_active INTEGER DEFAULT 0,
            config_type TEXT DEFAULT 'chat',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    for col, col_def in [
        ("model", "TEXT"),
        ("persona_id", "INTEGER"),
        ("is_active", "INTEGER DEFAULT 0"),
        ("config_type", "TEXT DEFAULT 'chat'"),
    ]:
        await conn.execute(
            f"ALTER TABLE api_configs ADD COLUMN IF NOT EXISTS {col} {col_def}"
        )


async def _ensure_default_embedding_api_config_row(conn) -> None:
    """若无任意 config_type=embedding 行，插入默认硅基流动 bge-m3（api_key 空，用户自填）并激活。"""
    await ensure_api_configs_schema(conn)
    row = await conn.fetchrow(
        "SELECT 1 FROM api_configs WHERE config_type = $1 LIMIT 1",
        "embedding",
    )
    if row:
        return
    await conn.execute(
        """
        INSERT INTO api_configs (name, api_key, base_url, model, persona_id, is_active, config_type)
        VALUES ($1, $2, $3, $4, NULL, 1, $5)
        """,
        "硅基流动 bge-m3",
        "",
        "https://api.siliconflow.cn/v1",
        "BAAI/bge-m3",
        "embedding",
    )


async def migrate_database_schema(conn) -> None:
    """
    启动时幂等迁移：补齐缺失列与全部约定索引。
    通过 CREATE INDEX IF NOT EXISTS / ADD COLUMN IF NOT EXISTS 实现「不存在则创建、已存在则跳过」。
    """
    await _ensure_sticker_cache_table(conn)
    await _summaries_ensure_source_date_column(conn)
    await _daily_batch_log_ensure_step45_columns(conn)
    await _backfill_daily_batch_step45_legacy_once(conn)
    await _messages_ensure_vision_columns(conn)

    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS user_work TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_appearance TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_relationships TEXT DEFAULT ''"
    )

    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages (session_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_messages_is_summarized ON messages (is_summarized)",
        (
            "CREATE INDEX IF NOT EXISTS idx_messages_session_is_summarized "
            "ON messages (session_id, is_summarized)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_messages_vision_batch "
            "ON messages (is_summarized, vision_processed)"
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
        await conn.execute(sql)

    await _config_insert_defaults_if_missing(
        conn,
        [
            ("telegram_max_chars", "50"),
            ("telegram_max_msg", "8"),
            ("gc_exempt_hits_threshold", "10"),
        ],
    )

    await _ensure_default_embedding_api_config_row(conn)

    logger.info("数据库 schema 迁移（索引/列）已执行")


# ---------------------------------------------------------------------------
# MessageDatabase 类
# ---------------------------------------------------------------------------

class MessageDatabase:
    """
    消息数据库类（PostgreSQL / asyncpg 版本）。

    使用方式：
        db = get_database()
        await db.init_pool(dsn)   # 应用启动时调用一次
        # 之后所有方法均 await 调用
    """

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def init_pool(self, dsn: str) -> None:
        """
        创建 asyncpg 连接池，并建表 / 运行 schema 迁移。

        Args:
            dsn: PostgreSQL 连接字符串，来自 config.DATABASE_URL
        """
        self.pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
        await self.create_tables()
        logger.info("PostgreSQL 连接池初始化完成")

    async def create_tables(self) -> None:
        """创建所有核心表（幂等），并运行 schema 迁移。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # messages
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        user_id TEXT,
                        channel_id TEXT,
                        message_id TEXT,
                        is_summarized INTEGER DEFAULT 0,
                        character_id TEXT,
                        platform TEXT DEFAULT 'discord',
                        thinking TEXT,
                        media_type TEXT,
                        image_caption TEXT,
                        vision_processed INTEGER NOT NULL DEFAULT 1
                    )
                """)

                # memory_cards
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_cards (
                        id SERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        character_id TEXT NOT NULL,
                        dimension TEXT NOT NULL,
                        content TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW(),
                        source_message_id TEXT,
                        is_active INTEGER DEFAULT 1
                    )
                """)

                # summaries
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS summaries (
                        id SERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        summary_text TEXT NOT NULL,
                        start_message_id INTEGER NOT NULL,
                        end_message_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        summary_type TEXT DEFAULT 'chunk',
                        source_date TIMESTAMP
                    )
                """)

                # daily_batch_log
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_batch_log (
                        batch_date DATE PRIMARY KEY,
                        step1_status INTEGER DEFAULT 0,
                        step2_status INTEGER DEFAULT 0,
                        step3_status INTEGER DEFAULT 0,
                        step4_status INTEGER DEFAULT 0,
                        step5_status INTEGER DEFAULT 0,
                        error_message TEXT,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # logs
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT NOW(),
                        level TEXT NOT NULL,
                        platform TEXT,
                        message TEXT NOT NULL,
                        stack_trace TEXT
                    )
                """)

                # token_usage
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT NOW(),
                        platform TEXT,
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        model TEXT
                    )
                """)

                # config
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # longterm_memories
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS longterm_memories (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        chroma_doc_id TEXT,
                        score INTEGER DEFAULT 5,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # meme_pack
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS meme_pack (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        url TEXT NOT NULL,
                        is_animated INTEGER NOT NULL DEFAULT 0
                    )
                """)

                # temporal_states
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS temporal_states (
                        id TEXT PRIMARY KEY,
                        state_content TEXT,
                        action_rule TEXT,
                        expire_at TIMESTAMP,
                        is_active INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # relationship_timeline
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS relationship_timeline (
                        id TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT NOW(),
                        event_type TEXT NOT NULL CHECK (
                            event_type IN (
                                'milestone', 'emotional_shift', 'conflict', 'daily_warmth'
                            )
                        ),
                        content TEXT,
                        source_summary_id TEXT
                    )
                """)

                # persona_configs
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS persona_configs (
                        id SERIAL PRIMARY KEY,
                        name TEXT,
                        char_name TEXT,
                        char_personality TEXT,
                        char_speech_style TEXT,
                        char_appearance TEXT,
                        char_relationships TEXT,
                        user_name TEXT,
                        user_body TEXT,
                        user_work TEXT,
                        user_habits TEXT,
                        user_likes_dislikes TEXT,
                        user_values TEXT,
                        user_hobbies TEXT,
                        user_taboos TEXT,
                        user_nsfw TEXT,
                        user_other TEXT,
                        system_rules TEXT,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                await migrate_database_schema(conn)

        logger.debug("数据库表初始化完成")

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    async def save_message(
        self,
        role: str,
        content: str,
        session_id: str,
        user_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        message_id: Optional[str] = None,
        character_id: Optional[str] = None,
        platform: Optional[str] = None,
        media_type: Optional[str] = None,
        image_caption: Optional[str] = None,
        vision_processed: Optional[int] = None,
        is_summarized: int = 0,
        thinking: Optional[str] = None,
    ) -> int:
        """
        保存一条消息到数据库。

        Returns:
            int: 插入的消息 ID
        """
        vp = 1 if vision_processed is None else int(vision_processed)
        is_sum = int(is_summarized)
        # asyncpg 绑定 TEXT 列须为 str；Telegram/上游可能传入 int（如 user_id）
        uid = None if user_id is None else str(user_id)
        cid = None if channel_id is None else str(channel_id)
        mid = None if message_id is None else str(message_id)
        chrid = None if character_id is None else str(character_id)
        plat = None if platform is None else str(platform)
        mt = None if media_type is None else str(media_type)
        tking = None if thinking is None else str(thinking)
        async with self.pool.acquire() as conn:
            new_id = await conn.fetchval(
                """
                INSERT INTO messages (
                    role, content, session_id, user_id, channel_id, message_id,
                    character_id, platform, media_type, image_caption, vision_processed,
                    is_summarized, thinking
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING id
                """,
                role, content, session_id, uid, cid, mid,
                chrid, plat, mt, image_caption, vp, is_sum, tking,
            )
        logger.debug(
            "保存消息成功: ID=%s, role=%s, session=%s, platform=%s, "
            "vision_processed=%s, is_summarized=%s, thinking=%s",
            new_id, role, session_id, platform, vp, is_sum, bool(tking),
        )
        return new_id

    async def get_assistant_content_for_platform_message_id(
        self, session_id: str, platform_message_id: str
    ) -> Optional[str]:
        """
        按会话 + 平台消息 ID 查找助手消息正文（用于 Telegram 反应等）。
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content FROM messages
                WHERE session_id = $1 AND role = 'assistant' AND message_id = $2
                LIMIT 1
                """,
                session_id,
                str(platform_message_id),
            )
        return str(row["content"]) if row and row["content"] is not None else None

    async def update_message_vision_result(
        self,
        message_row_id: int,
        image_caption: str,
        vision_processed: int = 1,
    ) -> bool:
        """更新消息的 image_caption 与 vision_processed（视觉异步任务回调）。"""
        async with self.pool.acquire() as conn:
            if image_caption in _IMAGE_CAPTION_FALLBACKS_MARK_SUMMARIZED:
                result = await conn.execute(
                    """
                    UPDATE messages
                    SET image_caption = $1, vision_processed = $2, is_summarized = 1
                    WHERE id = $3
                    """,
                    image_caption, int(vision_processed), int(message_row_id),
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE messages
                    SET image_caption = $1, vision_processed = $2
                    WHERE id = $3
                    """,
                    image_caption, int(vision_processed), int(message_row_id),
                )
        return _rowcount(result) > 0

    async def expire_stale_vision_pending(self, minutes: int = 5) -> int:
        """
        将长时间仍处于 vision_processed=0 的行标记为失败（微批/检查前兜底）。
        """
        caption = VISION_FAIL_CAPTION_TIMEOUT
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE messages
                SET vision_processed = 1,
                    image_caption = $1,
                    is_summarized = 1
                WHERE vision_processed = 0
                  AND created_at <= NOW() - $2 * INTERVAL '1 minute'
                """,
                caption,
                int(minutes),
            )
        n = _rowcount(result)
        if n:
            logger.info("expire_stale_vision_pending: 更新 %s 行", n)
        return n

    async def get_recent_messages(
        self, session_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """获取指定会话的最近 N 条消息（正序）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at, session_id
                FROM messages
                WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id,
                limit,
            )
        messages = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": _norm(r["created_at"]),
                "session_id": r["session_id"],
            }
            for r in rows
        ]
        messages.reverse()
        logger.debug("获取会话 %s 的最近 %s 条消息", session_id, len(messages))
        return messages

    async def get_all_messages(self) -> List[Dict[str, Any]]:
        """获取所有消息（用于历史查询）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at, session_id,
                       user_id, channel_id, message_id, character_id,
                       platform, thinking, is_summarized
                FROM messages
                ORDER BY created_at DESC
                """
            )
        messages = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": _norm(r["created_at"]),
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "channel_id": r["channel_id"],
                "message_id": r["message_id"],
                "character_id": r["character_id"],
                "platform": r["platform"],
                "thinking": r["thinking"],
                "is_summarized": bool(r["is_summarized"]),
            }
            for r in rows
        ]
        logger.debug("获取所有消息，数量: %s", len(messages))
        return messages

    async def get_messages_filtered(
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

        Returns:
            {"total": int, "messages": List[Dict]}
        """
        if isinstance(date_from, str):
            date_from = date.fromisoformat(date_from)
        if isinstance(date_to, str):
            date_to = date.fromisoformat(date_to)

        conditions: List[str] = []
        params: List[Any] = []
        idx = 1

        if platform:
            conditions.append(f"platform = ${idx}")
            params.append(platform)
            idx += 1

        if keyword:
            conditions.append(f"(content LIKE ${idx} OR thinking LIKE ${idx})")
            params.append(f"%{keyword}%")
            idx += 1

        if date_from:
            conditions.append(f"created_at::date >= ${idx}")
            params.append(date_from)
            idx += 1

        if date_to:
            conditions.append(f"created_at::date <= ${idx}")
            params.append(date_to)
            idx += 1

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM messages {where_clause}", *params
            )
            offset = (page - 1) * page_size
            rows = await conn.fetch(
                f"""
                SELECT id, role, content, thinking, platform, created_at, session_id
                FROM messages
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
                page_size,
                offset,
            )

        messages = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "thinking": r["thinking"],
                "platform": r["platform"],
                "created_at": _norm(r["created_at"]),
                "session_id": r["session_id"],
            }
            for r in rows
        ]
        logger.debug(
            "get_messages_filtered: total=%s, page=%s, page_size=%s, "
            "platform=%s, keyword=%s",
            total, page, page_size, platform, keyword,
        )
        return {"total": total, "messages": messages}

    async def get_logs_filtered(
        self,
        platform: Optional[str] = None,
        level: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        带过滤条件的日志查询（SQL 层过滤 + LIMIT/OFFSET 分页）。

        Returns:
            {"total": int, "logs": List[Dict]}
        """
        conditions: List[str] = []
        params: List[Any] = []
        idx = 1

        if platform:
            conditions.append(f"platform = ${idx}")
            params.append(platform)
            idx += 1

        if level:
            conditions.append(f"level = ${idx}")
            params.append(level.upper())
            idx += 1

        if keyword:
            conditions.append(f"(message LIKE ${idx} OR stack_trace LIKE ${idx})")
            params.append(f"%{keyword}%")
            idx += 1

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM logs {where_clause}", *params
            )
            offset = (page - 1) * page_size
            rows = await conn.fetch(
                f"""
                SELECT id, created_at, level, platform, message, stack_trace
                FROM logs
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
                page_size,
                offset,
            )

        logs = [
            {
                "id": r["id"],
                "created_at": _norm(r["created_at"]),
                "level": r["level"],
                "platform": r["platform"],
                "message": r["message"],
                "stack_trace": r["stack_trace"],
            }
            for r in rows
        ]
        logger.debug(
            "get_logs_filtered: total=%s, page=%s, page_size=%s, "
            "platform=%s, level=%s",
            total, page, page_size, platform, level,
        )
        return {"total": total, "logs": logs}

    async def clear_session_messages(self, session_id: str) -> int:
        """清除指定会话的所有消息，返回删除数量。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM messages WHERE session_id = $1", session_id
            )
        deleted_count = _rowcount(result)
        logger.info("清除会话 %s 的 %s 条消息", session_id, deleted_count)
        return deleted_count

    async def get_session_count(self, session_id: str) -> int:
        """获取指定会话的消息数量。"""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE session_id = $1", session_id
            )
        return count or 0

    # ------------------------------------------------------------------
    # memory_cards
    # ------------------------------------------------------------------

    async def save_memory_card(
        self,
        user_id: str,
        character_id: str,
        dimension: str,
        content: str,
        source_message_id: Optional[str] = None,
    ) -> int:
        """保存记忆卡片，返回插入 ID。"""
        _validate_dimension(dimension)
        async with self.pool.acquire() as conn:
            card_id = await conn.fetchval(
                """
                INSERT INTO memory_cards (user_id, character_id, dimension, content, source_message_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                user_id, character_id, dimension, content, source_message_id,
            )
        logger.debug("保存记忆卡片成功: ID=%s, user=%s, dimension=%s", card_id, user_id, dimension)
        return card_id

    async def get_memory_cards(
        self,
        user_id: str,
        character_id: str,
        dimension: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """获取用户的记忆卡片（仅 is_active=1）。"""
        async with self.pool.acquire() as conn:
            if dimension:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, character_id, dimension, content,
                           updated_at, source_message_id, is_active
                    FROM memory_cards
                    WHERE user_id = $1 AND character_id = $2 AND dimension = $3 AND is_active = 1
                    ORDER BY updated_at DESC
                    LIMIT $4
                    """,
                    user_id, character_id, dimension, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, character_id, dimension, content,
                           updated_at, source_message_id, is_active
                    FROM memory_cards
                    WHERE user_id = $1 AND character_id = $2 AND is_active = 1
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    user_id, character_id, limit,
                )
        cards = [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "character_id": r["character_id"],
                "dimension": r["dimension"],
                "content": r["content"],
                "updated_at": _norm(r["updated_at"]),
                "source_message_id": r["source_message_id"],
                "is_active": bool(r["is_active"]),
            }
            for r in rows
        ]
        logger.debug("获取记忆卡片成功: user=%s, count=%s", user_id, len(cards))
        return cards

    async def get_latest_memory_card_for_dimension(
        self,
        user_id: str,
        character_id: str,
        dimension: str,
    ) -> Optional[Dict[str, Any]]:
        """
        按用户、角色、维度取最近一条记忆卡片（不筛选 is_active）。
        供日终 Step 3 Upsert：批量软删后仍能更新同一行并重新激活。
        """
        _validate_dimension(dimension)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, character_id, dimension, content,
                       updated_at, source_message_id, is_active
                FROM memory_cards
                WHERE user_id = $1 AND character_id = $2 AND dimension = $3
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                user_id, character_id, dimension,
            )
        if not row:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "character_id": row["character_id"],
            "dimension": row["dimension"],
            "content": row["content"],
            "updated_at": _norm(row["updated_at"]),
            "source_message_id": row["source_message_id"],
            "is_active": bool(row["is_active"]),
        }

    async def update_memory_card(
        self,
        card_id: int,
        content: str,
        dimension: Optional[str] = None,
        reactivate: bool = False,
    ) -> bool:
        """更新记忆卡片内容；reactivate=True 时同时将 is_active 置 1。"""
        if dimension:
            _validate_dimension(dimension)
        async with self.pool.acquire() as conn:
            if dimension and reactivate:
                result = await conn.execute(
                    """
                    UPDATE memory_cards
                    SET content = $1, dimension = $2, updated_at = NOW(), is_active = 1
                    WHERE id = $3
                    """,
                    content, dimension, card_id,
                )
            elif dimension:
                result = await conn.execute(
                    """
                    UPDATE memory_cards
                    SET content = $1, dimension = $2, updated_at = NOW()
                    WHERE id = $3
                    """,
                    content, dimension, card_id,
                )
            elif reactivate:
                result = await conn.execute(
                    """
                    UPDATE memory_cards
                    SET content = $1, updated_at = NOW(), is_active = 1
                    WHERE id = $2
                    """,
                    content, card_id,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE memory_cards
                    SET content = $1, updated_at = NOW()
                    WHERE id = $2
                    """,
                    content, card_id,
                )
        updated = _rowcount(result) > 0
        if updated:
            logger.debug("更新记忆卡片成功: ID=%s", card_id)
        else:
            logger.warning("更新记忆卡片失败: ID=%s 不存在", card_id)
        return updated

    async def deactivate_memory_card(self, card_id: int) -> bool:
        """停用记忆卡片（软删除）。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE memory_cards
                SET is_active = 0, updated_at = NOW()
                WHERE id = $1
                """,
                card_id,
            )
        deactivated = _rowcount(result) > 0
        if deactivated:
            logger.debug("停用记忆卡片成功: ID=%s", card_id)
        else:
            logger.warning("停用记忆卡片失败: ID=%s 不存在", card_id)
        return deactivated

    # ------------------------------------------------------------------
    # summaries
    # ------------------------------------------------------------------

    async def save_summary(
        self,
        session_id: str,
        summary_text: str,
        start_message_id: int,
        end_message_id: int,
        summary_type: str = "chunk",
    ) -> int:
        """保存对话摘要，返回插入 ID。"""
        if summary_type not in {"chunk", "daily"}:
            raise ValueError(
                f"summary_type '{summary_type}' 不在允许的值中。允许的值: {{'chunk', 'daily'}}"
            )
        source_date = _dt.datetime.now().date()
        async with self.pool.acquire() as conn:
            summary_id = await conn.fetchval(
                """
                INSERT INTO summaries (
                    session_id, summary_text, start_message_id, end_message_id,
                    summary_type, source_date
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                session_id, summary_text, start_message_id, end_message_id,
                summary_type, source_date,
            )
        logger.debug(
            "保存摘要成功: ID=%s, session=%s, type=%s", summary_id, session_id, summary_type
        )
        return summary_id

    async def get_summaries(
        self,
        session_id: str,
        limit: int = 10,
        summary_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取会话的摘要列表。"""
        async with self.pool.acquire() as conn:
            if summary_type:
                rows = await conn.fetch(
                    """
                    SELECT id, session_id, summary_text, start_message_id, end_message_id,
                           created_at, summary_type
                    FROM summaries
                    WHERE session_id = $1 AND summary_type = $2
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    session_id, summary_type, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, session_id, summary_text, start_message_id, end_message_id,
                           created_at, summary_type
                    FROM summaries
                    WHERE session_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    session_id, limit,
                )
        summaries = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "summary_text": r["summary_text"],
                "start_message_id": r["start_message_id"],
                "end_message_id": r["end_message_id"],
                "created_at": _norm(r["created_at"]),
                "summary_type": r["summary_type"],
            }
            for r in rows
        ]
        logger.debug(
            "获取摘要成功: session=%s, count=%s, type=%s",
            session_id, len(summaries), summary_type,
        )
        return summaries

    async def mark_messages_as_summarized(
        self, start_message_id: int, end_message_id: int
    ) -> int:
        """标记消息范围为已摘要。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE messages
                SET is_summarized = 1
                WHERE id >= $1 AND id <= $2
                """,
                start_message_id, end_message_id,
            )
        updated_count = _rowcount(result)
        logger.debug(
            "标记消息为已摘要: start=%s, end=%s, count=%s",
            start_message_id, end_message_id, updated_count,
        )
        return updated_count

    async def mark_messages_as_summarized_by_ids(self, message_ids: List[int]) -> int:
        """根据消息 ID 列表批量标记消息为已摘要。"""
        if not message_ids:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE messages SET is_summarized = 1 WHERE id = ANY($1::int[])",
                message_ids,
            )
        updated_count = _rowcount(result)
        logger.debug(
            "批量标记消息为已摘要: count=%s, ids=%s...", updated_count, message_ids[:5]
        )
        return updated_count

    async def get_unsummarized_count_by_session(self, session_id: str) -> int:
        """获取指定会话中未摘要且视觉已处理的消息数量。"""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM messages
                WHERE session_id = $1 AND is_summarized = 0 AND vision_processed = 1
                """,
                session_id,
            )
        count = count or 0
        logger.debug("会话 %s 未摘要消息数量: %s", session_id, count)
        return count

    async def get_unsummarized_messages_by_session(
        self, session_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取指定会话中最早的未摘要消息列表（vision_processed=1）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at, session_id, user_id, channel_id
                FROM messages
                WHERE session_id = $1 AND is_summarized = 0 AND vision_processed = 1
                ORDER BY created_at ASC
                LIMIT $2
                """,
                session_id, limit,
            )
        messages = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": _norm(r["created_at"]),
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "channel_id": r["channel_id"],
            }
            for r in rows
        ]
        logger.debug("获取会话 %s 的未摘要消息: %s 条", session_id, len(messages))
        return messages

    async def get_all_active_memory_cards(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取所有激活的记忆卡片（全局查询）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, character_id, dimension, content,
                       updated_at, source_message_id, is_active
                FROM memory_cards
                WHERE is_active = 1
                ORDER BY dimension ASC, updated_at DESC
                LIMIT $1
                """,
                limit,
            )
        cards = [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "character_id": r["character_id"],
                "dimension": r["dimension"],
                "content": r["content"],
                "updated_at": _norm(r["updated_at"]),
                "source_message_id": r["source_message_id"],
                "is_active": bool(r["is_active"]),
            }
            for r in rows
        ]
        logger.debug("获取所有激活记忆卡片: count=%s", len(cards))
        return cards

    async def get_recent_daily_summaries(self, limit: int = 5) -> List[Dict[str, Any]]:
        """获取最近的每日摘要（全局查询，按 created_at 倒序）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, summary_text, start_message_id, end_message_id,
                       created_at, summary_type
                FROM summaries
                WHERE summary_type = 'daily'
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        summaries = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "summary_text": r["summary_text"],
                "start_message_id": r["start_message_id"],
                "end_message_id": r["end_message_id"],
                "created_at": _norm(r["created_at"]),
                "summary_type": r["summary_type"],
            }
            for r in rows
        ]
        logger.debug("获取最近每日摘要: count=%s", len(summaries))
        return summaries

    async def get_today_chunk_summaries(self) -> List[Dict[str, Any]]:
        """
        获取今天的所有 chunk 摘要（全局查询，按 created_at 正序）。

        注意：使用 PostgreSQL 的 CURRENT_DATE，依赖数据库服务器时区配置。
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, summary_text, start_message_id, end_message_id,
                       created_at, summary_type
                FROM summaries
                WHERE summary_type = 'chunk'
                  AND created_at::date = CURRENT_DATE
                ORDER BY created_at ASC
                """
            )
        summaries = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "summary_text": r["summary_text"],
                "start_message_id": r["start_message_id"],
                "end_message_id": r["end_message_id"],
                "created_at": _norm(r["created_at"]),
                "summary_type": r["summary_type"],
            }
            for r in rows
        ]
        logger.debug("获取今天的 chunk 摘要: count=%s", len(summaries))
        return summaries

    async def get_today_user_character_pairs(self, batch_date: str) -> List[Dict[str, Any]]:
        """
        查询指定日期有过对话的用户列表（按 created_at::date 过滤）。

        Args:
            batch_date: 日期字符串，格式 'YYYY-MM-DD'

        Returns:
            List of dicts with keys 'user_id' and 'character_id'.
        """
        dt_val = _dt.datetime.strptime(batch_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT user_id, character_id
                FROM messages
                WHERE created_at::date = $1::date
                  AND user_id IS NOT NULL
                  AND user_id != ''
                  AND role = 'user'
                """,
                dt_val,
            )
        return [dict(r) for r in rows]

    async def get_unsummarized_messages_desc(
        self, session_id: str, limit: int = 40
    ) -> List[Dict[str, Any]]:
        """
        获取指定会话中最新的未摘要消息列表（用于 context 构建）。
        按时间倒序取 limit 条，返回时翻转为正序（最旧在前）。
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at, session_id, user_id, channel_id,
                       media_type, image_caption, vision_processed
                FROM messages
                WHERE session_id = $1 AND is_summarized = 0
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id, limit,
            )
        messages = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": _norm(r["created_at"]),
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "channel_id": r["channel_id"],
                "media_type": r["media_type"],
                "image_caption": r["image_caption"],
                "vision_processed": r["vision_processed"],
            }
            for r in rows
        ]
        messages.reverse()
        logger.debug(
            "获取会话 %s 的最新未摘要消息（正序）: %s 条", session_id, len(messages)
        )
        return messages

    # ------------------------------------------------------------------
    # daily_batch_log
    # ------------------------------------------------------------------

    async def save_daily_batch_log(
        self,
        batch_date: str,
        step1_status: int = 0,
        step2_status: int = 0,
        step3_status: int = 0,
        step4_status: int = 0,
        step5_status: int = 0,
        error_message: Optional[str] = None,
    ) -> bool:
        """保存或更新每日批处理日志。"""
        dt_val = _dt.datetime.strptime(batch_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_batch_log
                    (batch_date, step1_status, step2_status, step3_status,
                     step4_status, step5_status, error_message, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT (batch_date) DO UPDATE SET
                    step1_status = EXCLUDED.step1_status,
                    step2_status = EXCLUDED.step2_status,
                    step3_status = EXCLUDED.step3_status,
                    step4_status = EXCLUDED.step4_status,
                    step5_status = EXCLUDED.step5_status,
                    error_message = EXCLUDED.error_message,
                    updated_at = NOW()
                """,
                dt_val,
                step1_status, step2_status, step3_status,
                step4_status, step5_status,
                error_message,
            )
        logger.debug(
            "保存每日批处理日志成功: date=%s, step1=%s, step2=%s, step3=%s, step4=%s, step5=%s",
            batch_date,
            step1_status, step2_status, step3_status, step4_status, step5_status,
        )
        return True

    async def get_daily_batch_log(self, batch_date: str) -> Optional[Dict[str, Any]]:
        """获取指定日期的批处理日志。"""
        dt_val = _dt.datetime.strptime(batch_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT batch_date, step1_status, step2_status, step3_status,
                       step4_status, step5_status, error_message, created_at, updated_at
                FROM daily_batch_log
                WHERE batch_date = $1
                """,
                dt_val,
            )
        if not row:
            logger.debug("每日批处理日志不存在: date=%s", batch_date)
            return None
        log = {
            "batch_date": _norm(row["batch_date"]),
            "step1_status": row["step1_status"],
            "step2_status": row["step2_status"],
            "step3_status": row["step3_status"],
            "step4_status": 0 if row["step4_status"] is None else int(row["step4_status"]),
            "step5_status": 0 if row["step5_status"] is None else int(row["step5_status"]),
            "error_message": row["error_message"],
            "created_at": _norm(row["created_at"]),
            "updated_at": _norm(row["updated_at"]),
        }
        logger.debug("获取每日批处理日志成功: date=%s", batch_date)
        return log

    async def get_recent_daily_batch_logs(self, limit: int = 30) -> List[Dict[str, Any]]:
        """获取最近的批处理日志列表。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT batch_date, step1_status, step2_status, step3_status,
                       step4_status, step5_status, error_message, created_at, updated_at
                FROM daily_batch_log
                ORDER BY batch_date DESC
                LIMIT $1
                """,
                limit,
            )
        logs = [
            {
                "batch_date": _norm(r["batch_date"]),
                "step1_status": r["step1_status"],
                "step2_status": r["step2_status"],
                "step3_status": r["step3_status"],
                "step4_status": 0 if r["step4_status"] is None else int(r["step4_status"]),
                "step5_status": 0 if r["step5_status"] is None else int(r["step5_status"]),
                "error_message": r["error_message"],
                "created_at": _norm(r["created_at"]),
                "updated_at": _norm(r["updated_at"]),
            }
            for r in rows
        ]
        logger.debug("获取最近批处理日志成功: count=%s", len(logs))
        return logs

    async def update_daily_batch_step_status(
        self,
        batch_date: str,
        step_number: int,
        status: int,
        error_message: Optional[str] = None,
    ) -> bool:
        """更新指定日期的批处理步骤状态。"""
        if step_number not in {1, 2, 3, 4, 5}:
            raise ValueError(f"步骤编号 {step_number} 无效，必须是 1 至 5")
        col = f"step{step_number}_status"
        dt_val = _dt.datetime.strptime(batch_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE daily_batch_log
                SET {col} = $1, error_message = $2, updated_at = NOW()
                WHERE batch_date = $3
                """,
                status, error_message, dt_val,
            )
        updated = _rowcount(result) > 0
        if updated:
            logger.debug(
                "更新批处理步骤状态成功: date=%s, step=%s, status=%s",
                batch_date, step_number, status,
            )
        else:
            logger.warning("更新批处理步骤状态失败: date=%s 不存在", batch_date)
        return updated

    _DAILY_BATCH_INCOMPLETE_SQL = """(
            COALESCE(step1_status, 0) = 0 OR COALESCE(step2_status, 0) = 0 OR
            COALESCE(step3_status, 0) = 0 OR COALESCE(step4_status, 0) = 0 OR
            COALESCE(step5_status, 0) = 0
        )"""

    async def list_incomplete_daily_batch_dates_in_range(
        self, start_date: str, end_date: str
    ) -> List[str]:
        """列出 batch_date 在范围内且五步未全部完成的日期，升序。"""
        dt_start = _dt.datetime.strptime(start_date, "%Y-%m-%d").date()
        dt_end = _dt.datetime.strptime(end_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT batch_date FROM daily_batch_log
                WHERE batch_date >= $1 AND batch_date <= $2
                  AND {self._DAILY_BATCH_INCOMPLETE_SQL}
                ORDER BY batch_date ASC
                """,
                dt_start, dt_end,
            )
        return [_norm(r["batch_date"]) for r in rows]

    async def mark_expired_skipped_daily_batch_logs_before(
        self, before_date: str
    ) -> int:
        """
        batch_date 早于 before_date 且仍有未完成步骤的行：五步均置 1，
        error_message='expired, skipped'。返回更新行数。
        """
        dt_before = _dt.datetime.strptime(before_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE daily_batch_log
                SET step1_status = 1, step2_status = 1, step3_status = 1,
                    step4_status = 1, step5_status = 1,
                    error_message = 'expired, skipped',
                    updated_at = NOW()
                WHERE batch_date < $1
                  AND {self._DAILY_BATCH_INCOMPLETE_SQL}
                """,
                dt_before,
            )
        n = _rowcount(result)
        if n:
            logger.info(
                "已将 %s 条超窗未完成的 daily_batch_log 标记为 expired, skipped", n
            )
        return n

    # ------------------------------------------------------------------
    # temporal_states / relationship_timeline
    # ------------------------------------------------------------------

    RELATIONSHIP_TIMELINE_EVENT_TYPES = frozenset({
        "milestone", "emotional_shift", "conflict", "daily_warmth",
    })

    async def list_expired_active_temporal_states(
        self, as_of_iso: str
    ) -> List[Dict[str, Any]]:
        """列出已到期且仍激活的 temporal_states（expire_at <= as_of_iso，is_active=1）。"""
        dt_as_of = _dt.datetime.strptime(as_of_iso, "%Y-%m-%d %H:%M:%S")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, state_content, action_rule, expire_at, created_at
                FROM temporal_states
                WHERE is_active = 1
                  AND expire_at IS NOT NULL
                  AND expire_at <= $1::timestamp
                ORDER BY expire_at ASC
                """,
                dt_as_of,
            )
        return [
            {
                "id": r["id"],
                "state_content": r["state_content"],
                "action_rule": r["action_rule"],
                "expire_at": _norm(r["expire_at"]),
                "created_at": _norm(r["created_at"]),
            }
            for r in rows
        ]

    async def deactivate_temporal_states_by_ids(self, state_ids: List[str]) -> int:
        """将给定 id 的 temporal_states 设为 is_active=0。"""
        if not state_ids:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE temporal_states SET is_active = 0 WHERE id = ANY($1::text[])",
                state_ids,
            )
        return _rowcount(result)

    async def insert_relationship_timeline_event(
        self,
        event_type: str,
        content: str,
        source_summary_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> str:
        """插入一条 relationship_timeline，返回主键 id（UUID 字符串）。"""
        if event_type not in self.RELATIONSHIP_TIMELINE_EVENT_TYPES:
            raise ValueError(
                f"event_type 无效: {event_type}，允许: {self.RELATIONSHIP_TIMELINE_EVENT_TYPES}"
            )
        eid = event_id or uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO relationship_timeline (id, event_type, content, source_summary_id)
                VALUES ($1, $2, $3, $4)
                """,
                eid, event_type, content, source_summary_id,
            )
        logger.debug(
            "relationship_timeline 插入成功 id=%s type=%s", eid, event_type
        )
        return eid

    async def get_all_active_temporal_states(self) -> List[Dict[str, Any]]:
        """获取 temporal_states 中 is_active=1 的全部记录（按 created_at 升序）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, state_content, action_rule, expire_at, is_active, created_at
                FROM temporal_states
                WHERE is_active = 1
                ORDER BY created_at ASC
                """
            )
        return [
            {
                "id": r["id"],
                "state_content": r["state_content"],
                "action_rule": r["action_rule"],
                "expire_at": _norm(r["expire_at"]),
                "is_active": r["is_active"],
                "created_at": _norm(r["created_at"]),
            }
            for r in rows
        ]

    async def get_recent_relationship_timeline(
        self, limit: int = 3
    ) -> List[Dict[str, Any]]:
        """按 created_at 倒序取 relationship_timeline 前 limit 条。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_at, event_type, content, source_summary_id
                FROM relationship_timeline
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "id": r["id"],
                "created_at": _norm(r["created_at"]),
                "event_type": r["event_type"],
                "content": r["content"],
                "source_summary_id": r["source_summary_id"],
            }
            for r in rows
        ]

    async def list_temporal_states_all(self) -> List[Dict[str, Any]]:
        """全部 temporal_states，按 created_at 倒序（管理端）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, state_content, action_rule, expire_at, is_active, created_at
                FROM temporal_states
                ORDER BY created_at DESC
                """
            )
        return [
            {
                "id": r["id"],
                "state_content": r["state_content"],
                "action_rule": r["action_rule"],
                "expire_at": _norm(r["expire_at"]),
                "is_active": r["is_active"],
                "created_at": _norm(r["created_at"]),
            }
            for r in rows
        ]

    async def insert_temporal_state(
        self,
        state_content: str,
        action_rule: Optional[str] = None,
        expire_at: Optional[str] = None,
    ) -> str:
        """插入一条 temporal_states，返回主键 id（UUID hex）。"""
        dt_expire = None
        if expire_at:
            try:
                dt_expire = _dt.datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("解析 expire_at 失败: %s", expire_at)
        eid = uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO temporal_states (id, state_content, action_rule, expire_at, is_active)
                VALUES ($1, $2, $3, $4, 1)
                """,
                eid, state_content, action_rule or "", dt_expire,
            )
        return eid

    async def list_relationship_timeline_all_desc(self) -> List[Dict[str, Any]]:
        """全部 relationship_timeline，按 created_at 倒序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_at, event_type, content, source_summary_id
                FROM relationship_timeline
                ORDER BY created_at DESC
                """
            )
        return [
            {
                "id": r["id"],
                "created_at": _norm(r["created_at"]),
                "event_type": r["event_type"],
                "content": r["content"],
                "source_summary_id": r["source_summary_id"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    async def save_log(
        self,
        level: str,
        message: str,
        platform: Optional[str] = None,
        stack_trace: Optional[str] = None,
    ) -> int:
        """保存日志到数据库，返回插入 ID。"""
        async with self.pool.acquire() as conn:
            log_id = await conn.fetchval(
                """
                INSERT INTO logs (level, platform, message, stack_trace)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                level, platform, message, stack_trace,
            )
        logger.debug("保存日志成功: ID=%s, level=%s, platform=%s", log_id, level, platform)
        return log_id

    async def get_all_logs(self) -> List[Dict[str, Any]]:
        """获取所有日志（用于日志查询）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_at, level, platform, message, stack_trace
                FROM logs
                ORDER BY created_at DESC
                """
            )
        logs = [
            {
                "id": r["id"],
                "created_at": _norm(r["created_at"]),
                "level": r["level"],
                "platform": r["platform"],
                "message": r["message"],
                "stack_trace": r["stack_trace"],
            }
            for r in rows
        ]
        logger.debug("获取所有日志，数量: %s", len(logs))
        return logs

    # ------------------------------------------------------------------
    # token_usage
    # ------------------------------------------------------------------

    async def save_token_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        model: str,
        platform: Optional[str] = None,
    ) -> int:
        """保存 token 使用量，返回插入 ID。"""
        async with self.pool.acquire() as conn:
            usage_id = await conn.fetchval(
                """
                INSERT INTO token_usage (platform, prompt_tokens, completion_tokens, total_tokens, model)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                platform, prompt_tokens, completion_tokens, total_tokens, model,
            )
        logger.debug(
            "保存 token 使用量成功: ID=%s, model=%s, total_tokens=%s",
            usage_id, model, total_tokens,
        )
        return usage_id

    async def get_latest_token_usage_stats(self, platform: Optional[str] = None) -> Dict[str, Any]:
        """获取最新一次调用的 token 使用量。"""
        idx = 1
        base_cond = ""
        params: List[Any] = []
        if platform:
            base_cond = "WHERE platform = $1"
            params.append(platform)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT total_tokens, prompt_tokens, completion_tokens, platform
                FROM token_usage
                {base_cond}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                *params,
            )
            if not row:
                return {
                    "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "call_count": 0, "by_platform": {}
                }
            
            p = row[3] or "unknown"
            return {
                "total_tokens": row[0] or 0,
                "prompt_tokens": row[1] or 0,
                "completion_tokens": row[2] or 0,
                "call_count": 1,
                "by_platform": {p: row[0] or 0},
            }

    async def get_token_usage_stats(
        self, start_date, platform: Optional[str] = None
    ) -> Dict[str, Any]:
        """统计从 start_date 开始的 token 使用量。"""
        # asyncpg 要求传 datetime 对象，不能传字符串（否则 DataError）
        dt_start: _dt.datetime = (
            start_date if isinstance(start_date, _dt.datetime)
            else _dt.datetime.fromisoformat(str(start_date))
        )
        idx = 2
        base_cond = "WHERE created_at >= $1"
        params: List[Any] = [dt_start]
        if platform:
            base_cond += f" AND platform = ${idx}"
            params.append(platform)
            idx += 1

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT SUM(total_tokens), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*)
                FROM token_usage {base_cond}
                """,
                *params,
            )
            total = row[0] or 0
            prompt = row[1] or 0
            completion = row[2] or 0
            count = row[3] or 0

            rows_bp = await conn.fetch(
                f"""
                SELECT platform, SUM(total_tokens)
                FROM token_usage {base_cond}
                GROUP BY platform
                """,
                *params,
            )
        by_platform = {}
        for r in rows_bp:
            if r[0]:
                by_platform[r[0]] = r[1] or 0

        return {
            "total_tokens": total,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "call_count": count,
            "by_platform": by_platform,
        }

    # ------------------------------------------------------------------
    # messages: thinking
    # ------------------------------------------------------------------

    async def update_message_with_thinking(self, message_id: int, thinking: str) -> bool:
        """更新消息的思维链内容。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE messages SET thinking = $1 WHERE id = $2",
                thinking, message_id,
            )
        updated = _rowcount(result) > 0
        if updated:
            logger.debug("更新消息思维链成功: message_id=%s", message_id)
        else:
            logger.warning("更新消息思维链失败: message_id=%s 不存在", message_id)
        return updated

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    async def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """获取配置值，不存在时返回 default。"""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT value FROM config WHERE key = $1", key
            )
        return result if result is not None else default

    async def set_config(self, key: str, value: str) -> bool:
        """设置配置值（存在则更新，不存在则插入）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                key, value,
            )
        logger.debug("设置配置成功: %s=%s", key, value)
        return True

    async def get_all_configs(self) -> Dict[str, str]:
        """获取所有配置，返回字典。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM config")
        configs = {r["key"]: r["value"] for r in rows}
        logger.debug("获取所有配置成功: %s 条", len(configs))
        return configs

    async def get_config_max_updated_at_for_keys(
        self, keys: List[str]
    ) -> Optional[str]:
        """
        返回 config 表中给定 key 列表里最新的 updated_at。
        供助手配置 API 在响应中附带「最近持久化时间」。
        """
        if not keys:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT MAX(updated_at) FROM config WHERE key = ANY($1::text[])",
                keys,
            )
        if row and row[0] is not None:
            return _norm(row[0])
        return None

    async def toggle_offline_mode(self, enable: bool) -> bool:
        """
        开启或关闭线下极速模式的“影子备份”。
        开启时备份当前的 buffer_delay、telegram_max_chars、telegram_max_msg 到 backup_* 键，
        并将这三个键设置为 0.1、800、1，最后写入 offline_mode_active=1。
        关闭时从 backup_* 键还原（若没有则使用系统默认值），并置 offline_mode_active=0。
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if enable:
                    # 备份
                    for key in ["buffer_delay", "telegram_max_chars", "telegram_max_msg"]:
                        val = await conn.fetchval("SELECT value FROM config WHERE key = $1", key)
                        if val is not None:
                            await conn.execute(
                                """
                                INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
                                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                                """,
                                f"backup_{key}", val
                            )
                    
                    # 覆写极速参数
                    fast_values = {
                        "buffer_delay": "1",
                        "telegram_max_chars": "800",
                        "telegram_max_msg": "1",
                        "offline_mode_active": "1"
                    }
                    for k, v in fast_values.items():
                        await conn.execute(
                            """
                            INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                            """,
                            k, v
                        )
                else:
                    # 还原参数
                    fallback_defaults = {
                        "buffer_delay": "5",
                        "telegram_max_chars": "50",
                        "telegram_max_msg": "8",
                    }
                    for key in ["buffer_delay", "telegram_max_chars", "telegram_max_msg"]:
                        backup_val = await conn.fetchval(
                            "SELECT value FROM config WHERE key = $1", f"backup_{key}"
                        )
                        restore_val = backup_val if backup_val is not None else fallback_defaults[key]
                        await conn.execute(
                            """
                            INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                            """,
                            key, restore_val
                        )
                    
                    # 取消激活状态
                    await conn.execute(
                        """
                        INSERT INTO config (key, value, updated_at) VALUES ($1, $2, NOW())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                        """,
                        "offline_mode_active", "0"
                    )
        logger.info("线下模式已%s", "开启" if enable else "关闭")
        return True

    # ------------------------------------------------------------------
    # persona_configs CRUD
    # ------------------------------------------------------------------

    async def get_all_persona_configs(self) -> List[Dict[str, Any]]:
        """获取所有人设配置列表（仅返回 id, name, created_at, updated_at）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, created_at, updated_at FROM persona_configs ORDER BY id ASC"
            )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "created_at": _norm(r["created_at"]),
                "updated_at": _norm(r["updated_at"]),
            }
            for r in rows
        ]

    async def get_persona_config(self, persona_id: int) -> Optional[Dict[str, Any]]:
        """获取单个人设配置详情。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM persona_configs WHERE id = $1", persona_id
            )
        return _r(row) if row else None

    async def save_persona_config(self, data: Dict[str, Any]) -> int:
        """新增人设配置，返回新插入的 id。"""
        fields = [
            "name", "char_name", "char_personality", "char_speech_style",
            "char_appearance", "char_relationships",
            "user_name", "user_body", "user_work", "user_habits",
            "user_likes_dislikes", "user_values", "user_hobbies", "user_taboos",
            "user_nsfw", "user_other", "system_rules",
        ]
        cols = ", ".join(fields)
        placeholders = ", ".join([f"${i + 1}" for i in range(len(fields))])
        values = [data.get(f, "") for f in fields]
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                f"INSERT INTO persona_configs ({cols}) VALUES ({placeholders}) RETURNING id",
                *values,
            )
        return row_id if row_id is not None else -1

    async def update_persona_config(self, persona_id: int, data: Dict[str, Any]) -> bool:
        """更新人设配置。"""
        allowed = {
            "name", "char_name", "char_personality", "char_speech_style",
            "char_appearance", "char_relationships",
            "user_name", "user_body", "user_work", "user_habits",
            "user_likes_dislikes", "user_values", "user_hobbies", "user_taboos",
            "user_nsfw", "user_other", "system_rules",
        }
        update_data = {k: v for k, v in data.items() if k in allowed}
        if not update_data:
            return False
        set_parts = [f"{k} = ${i + 1}" for i, k in enumerate(update_data.keys())]
        set_clause = ", ".join(set_parts)
        last_idx = len(update_data) + 1
        values = list(update_data.values()) + [persona_id]
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE persona_configs SET {set_clause}, updated_at = NOW() WHERE id = ${last_idx}",
                *values,
            )
        return _rowcount(result) > 0

    async def delete_persona_config(self, persona_id: int) -> bool:
        """删除人设配置。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM persona_configs WHERE id = $1", persona_id
            )
        return _rowcount(result) > 0

    # ------------------------------------------------------------------
    # api_configs CRUD
    # ------------------------------------------------------------------

    async def _ensure_api_configs_table(self, conn) -> None:
        """确保 api_configs 表存在，并自动补全缺失字段。"""
        await ensure_api_configs_schema(conn)

    async def get_all_api_configs(
        self, config_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """获取所有 API 配置列表（带关联人设名称），可按 config_type 过滤。"""
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            if config_type:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.name, a.api_key, a.base_url, a.model,
                           a.persona_id, a.is_active, a.config_type,
                           a.created_at, a.updated_at,
                           p.name AS persona_name
                    FROM api_configs a
                    LEFT JOIN persona_configs p ON a.persona_id = p.id
                    WHERE a.config_type = $1
                    ORDER BY a.id ASC
                    """,
                    config_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.name, a.api_key, a.base_url, a.model,
                           a.persona_id, a.is_active, a.config_type,
                           a.created_at, a.updated_at,
                           p.name AS persona_name
                    FROM api_configs a
                    LEFT JOIN persona_configs p ON a.persona_id = p.id
                    ORDER BY a.id ASC
                    """
                )
        return [_r(r) for r in rows]

    async def get_api_config(self, config_id: int) -> Optional[Dict[str, Any]]:
        """获取单个 API 配置。"""
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            row = await conn.fetchrow(
                "SELECT * FROM api_configs WHERE id = $1", config_id
            )
        return _r(row) if row else None

    async def save_api_config(self, data: Dict[str, Any]) -> int:
        """新增 API 配置，返回新 id。"""
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            row_id = await conn.fetchval(
                """
                INSERT INTO api_configs (name, api_key, base_url, model, persona_id, config_type)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                data.get("name", ""),
                data.get("api_key", ""),
                data.get("base_url", ""),
                data.get("model"),
                data.get("persona_id"),
                data.get("config_type", "chat"),
            )
        return row_id if row_id is not None else -1

    async def update_api_config(self, config_id: int, data: Dict[str, Any]) -> bool:
        """更新 API 配置。"""
        allowed = {"name", "api_key", "base_url", "model", "persona_id", "config_type"}
        update_data = {k: v for k, v in data.items() if k in allowed}
        if not update_data:
            return False
        set_parts = [f"{k} = ${i + 1}" for i, k in enumerate(update_data.keys())]
        set_clause = ", ".join(set_parts)
        last_idx = len(update_data) + 1
        values = list(update_data.values()) + [config_id]
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            result = await conn.execute(
                f"UPDATE api_configs SET {set_clause}, updated_at = NOW() WHERE id = ${last_idx}",
                *values,
            )
        return _rowcount(result) > 0

    async def delete_api_config(self, config_id: int) -> bool:
        """删除 API 配置。"""
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            result = await conn.execute(
                "DELETE FROM api_configs WHERE id = $1", config_id
            )
        return _rowcount(result) > 0

    async def activate_api_config(self, config_id: int) -> bool:
        """激活指定配置（同类型内唯一激活：先清除同类型所有激活，再设置指定条目）。"""
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            row = await conn.fetchrow(
                "SELECT config_type FROM api_configs WHERE id = $1", config_id
            )
            if not row:
                return False
            cfg_type = row["config_type"] or "chat"
            async with conn.transaction():
                await conn.execute(
                    "UPDATE api_configs SET is_active = 0 WHERE config_type = $1",
                    cfg_type,
                )
                await conn.execute(
                    "UPDATE api_configs SET is_active = 1, updated_at = NOW() WHERE id = $1",
                    config_id,
                )
        return True

    async def get_active_api_config(
        self, config_type: str = "chat"
    ) -> Optional[Dict[str, Any]]:
        """获取指定类型的激活配置。"""
        async with self.pool.acquire() as conn:
            await self._ensure_api_configs_table(conn)
            row = await conn.fetchrow(
                "SELECT * FROM api_configs WHERE config_type = $1 AND is_active = 1 LIMIT 1",
                config_type,
            )
        return _r(row) if row else None

    # ------------------------------------------------------------------
    # longterm_memories CRUD
    # ------------------------------------------------------------------

    async def create_longterm_memory(
        self,
        content: str,
        chroma_doc_id: Optional[str] = None,
        score: int = 5,
    ) -> int:
        """新增一条长期记忆镜像记录，返回新 id。"""
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO longterm_memories (content, chroma_doc_id, score)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                content, chroma_doc_id, score,
            )
        return row_id

    async def get_longterm_memories(
        self, keyword: str = "", page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        """查询长期记忆（支持关键词搜索和分页）。"""
        async with self.pool.acquire() as conn:
            if keyword:
                like = f"%{keyword}%"
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM longterm_memories WHERE content LIKE $1", like
                )
                offset = (page - 1) * page_size
                rows = await conn.fetch(
                    """
                    SELECT id, content, chroma_doc_id, score, created_at
                    FROM longterm_memories
                    WHERE content LIKE $1
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    like, page_size, offset,
                )
            else:
                total = await conn.fetchval("SELECT COUNT(*) FROM longterm_memories")
                offset = (page - 1) * page_size
                rows = await conn.fetch(
                    """
                    SELECT id, content, chroma_doc_id, score, created_at
                    FROM longterm_memories
                    ORDER BY created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    page_size, offset,
                )
        items = [
            {
                "id": r["id"],
                "content": r["content"],
                "chroma_doc_id": r["chroma_doc_id"],
                "score": r["score"],
                "created_at": _norm(r["created_at"]),
            }
            for r in rows
        ]
        total_pages = max(1, (total + page_size - 1) // page_size)
        return {
            "items": items,
            "total_items": total,
            "total_pages": total_pages,
            "current_page": page,
            "page_size": page_size,
        }

    async def get_longterm_memory(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """获取单条长期记忆（用于删除时获取 chroma_doc_id）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, content, chroma_doc_id, score, created_at
                FROM longterm_memories
                WHERE id = $1
                """,
                memory_id,
            )
        if not row:
            return None
        return {
            "id": row["id"],
            "content": row["content"],
            "chroma_doc_id": row["chroma_doc_id"],
            "score": row["score"],
            "created_at": _norm(row["created_at"]),
        }

    async def delete_longterm_memory(self, memory_id: int) -> bool:
        """删除长期记忆镜像记录。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM longterm_memories WHERE id = $1", memory_id
            )
        return _rowcount(result) > 0

    # ------------------------------------------------------------------
    # meme_pack
    # ------------------------------------------------------------------

    async def insert_meme_pack(self, name: str, url: str, is_animated: int) -> int:
        """插入 meme_pack 行，返回新自增 id；失败返回 -1。"""
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO meme_pack (name, url, is_animated)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                (name or "").strip(),
                (url or "").strip(),
                int(is_animated),
            )
        return int(row_id) if row_id is not None else -1

    async def fetch_all_meme_pack(self) -> List[Dict[str, Any]]:
        """返回 meme_pack 全表行，用于批量重同步 Chroma。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, url, is_animated FROM meme_pack ORDER BY id"
            )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # sticker_cache
    # ------------------------------------------------------------------

    async def get_sticker_cache(
        self, file_unique_id: str
    ) -> Optional[Dict[str, Any]]:
        """按 file_unique_id 读取贴纸缓存。"""
        if not file_unique_id:
            return None
        async with self.pool.acquire() as conn:
            await _ensure_sticker_cache_table(conn)
            row = await conn.fetchrow(
                """
                SELECT file_unique_id, emoji, sticker_set_name, description, created_at
                FROM sticker_cache
                WHERE file_unique_id = $1
                """,
                file_unique_id,
            )
        if not row:
            return None
        return {
            "file_unique_id": row["file_unique_id"],
            "emoji": row["emoji"],
            "sticker_set_name": row["sticker_set_name"],
            "description": row["description"],
            "created_at": _norm(row["created_at"]),
        }

    async def save_sticker_cache(
        self,
        file_unique_id: str,
        emoji: Optional[str],
        sticker_set_name: Optional[str],
        description: str,
    ) -> None:
        """写入或覆盖贴纸缓存（失败仅打日志）。"""
        if not file_unique_id:
            return
        desc = (description or "").strip() or "（贴纸）"
        try:
            async with self.pool.acquire() as conn:
                await _ensure_sticker_cache_table(conn)
                await conn.execute(
                    """
                    INSERT INTO sticker_cache
                        (file_unique_id, emoji, sticker_set_name, description, created_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (file_unique_id) DO UPDATE SET
                        emoji = EXCLUDED.emoji,
                        sticker_set_name = EXCLUDED.sticker_set_name,
                        description = EXCLUDED.description,
                        created_at = NOW()
                    """,
                    file_unique_id,
                    emoji or "",
                    sticker_set_name or "",
                    desc,
                )
        except Exception as e:
            logger.error("写入 sticker_cache 失败: %s", e)

    async def delete_sticker_cache(self, file_unique_id: str) -> None:
        """按 file_unique_id 删除贴纸缓存（用于 /rescanpic 等强制重识别）。"""
        if not file_unique_id:
            return
        try:
            async with self.pool.acquire() as conn:
                await _ensure_sticker_cache_table(conn)
                await conn.execute(
                    "DELETE FROM sticker_cache WHERE file_unique_id = $1",
                    file_unique_id,
                )
        except Exception as e:
            logger.error("删除 sticker_cache 失败: %s", e)


# ---------------------------------------------------------------------------
# 维度枚举校验（模块级，供多处复用）
# ---------------------------------------------------------------------------

_ALLOWED_DIMENSIONS = frozenset({
    "preferences", "interaction_patterns", "current_status",
    "goals", "relationships", "key_events", "rules",
})


def _validate_dimension(dimension: str) -> None:
    if dimension not in _ALLOWED_DIMENSIONS:
        raise ValueError(
            f"维度 '{dimension}' 不在允许的枚举值中。允许的值: {_ALLOWED_DIMENSIONS}"
        )


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_db_instance: Optional[MessageDatabase] = None


def get_database() -> MessageDatabase:
    """
    获取数据库实例（单例模式）。

    注意：首次使用前必须调用 await get_database().init_pool(dsn) 或
    await initialize_database()，否则 pool 为 None 导致所有方法抛出错误。
    """
    global _db_instance
    if _db_instance is None:
        _db_instance = MessageDatabase()
    return _db_instance


async def initialize_database() -> MessageDatabase:
    """
    初始化并返回数据库单例（应在应用启动时 await 调用一次）。
    自动从 config.DATABASE_URL 读取 DSN。
    """
    from config import config as _cfg
    db = get_database()
    if db.pool is None:
        await db.init_pool(_cfg.DATABASE_URL)
    return db


# ---------------------------------------------------------------------------
# 便捷函数（与 MessageDatabase 方法一一对应，保持上层调用签名不变）
# ---------------------------------------------------------------------------

async def save_message(
    role: str,
    content: str,
    session_id: str,
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
    character_id: Optional[str] = None,
    platform: Optional[str] = None,
    media_type: Optional[str] = None,
    image_caption: Optional[str] = None,
    vision_processed: Optional[int] = None,
    is_summarized: int = 0,
    thinking: Optional[str] = None,
) -> int:
    # 与 MessageDatabase.save_message 一致：asyncpg TEXT 绑定必须为 str
    uid = None if user_id is None else str(user_id)
    cid = None if channel_id is None else str(channel_id)
    mid = None if message_id is None else str(message_id)
    chrid = None if character_id is None else str(character_id)
    plat = None if platform is None else str(platform)
    mt = None if media_type is None else str(media_type)
    return await get_database().save_message(
        role,
        content,
        session_id,
        uid,
        cid,
        mid,
        chrid,
        plat,
        mt,
        image_caption,
        vision_processed,
        is_summarized,
        thinking,
    )


async def get_assistant_content_for_platform_message_id(
    session_id: str, platform_message_id: str
) -> Optional[str]:
    return await get_database().get_assistant_content_for_platform_message_id(
        session_id, platform_message_id
    )


async def update_message_vision_result(
    message_row_id: int,
    image_caption: str,
    vision_processed: int = 1,
) -> bool:
    return await get_database().update_message_vision_result(
        message_row_id, image_caption, vision_processed
    )


async def expire_stale_vision_pending(minutes: int = 5) -> int:
    return await get_database().expire_stale_vision_pending(minutes=minutes)


async def get_sticker_cache_row(file_unique_id: str) -> Optional[Dict[str, Any]]:
    return await get_database().get_sticker_cache(file_unique_id)


async def save_sticker_cache_row(
    file_unique_id: str,
    emoji: Optional[str],
    sticker_set_name: Optional[str],
    description: str,
) -> None:
    await get_database().save_sticker_cache(
        file_unique_id, emoji, sticker_set_name, description
    )


async def delete_sticker_cache_row(file_unique_id: str) -> None:
    await get_database().delete_sticker_cache(file_unique_id)


async def get_recent_messages(
    session_id: str, limit: int = 20
) -> List[Dict[str, Any]]:
    return await get_database().get_recent_messages(session_id, limit)


async def clear_session_messages(session_id: str) -> int:
    return await get_database().clear_session_messages(session_id)


async def save_memory_card(
    user_id: str,
    character_id: str,
    dimension: str,
    content: str,
    source_message_id: Optional[str] = None,
) -> int:
    return await get_database().save_memory_card(
        user_id, character_id, dimension, content, source_message_id
    )


async def get_memory_cards(
    user_id: str,
    character_id: str,
    dimension: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    return await get_database().get_memory_cards(user_id, character_id, dimension, limit)


async def get_latest_memory_card_for_dimension(
    user_id: str,
    character_id: str,
    dimension: str,
) -> Optional[Dict[str, Any]]:
    return await get_database().get_latest_memory_card_for_dimension(
        user_id, character_id, dimension
    )


async def update_memory_card(
    card_id: int,
    content: str,
    dimension: Optional[str] = None,
    reactivate: bool = False,
) -> bool:
    return await get_database().update_memory_card(card_id, content, dimension, reactivate)


async def deactivate_memory_card(card_id: int) -> bool:
    return await get_database().deactivate_memory_card(card_id)


async def save_summary(
    session_id: str,
    summary_text: str,
    start_message_id: int,
    end_message_id: int,
    summary_type: str = "chunk",
) -> int:
    return await get_database().save_summary(
        session_id, summary_text, start_message_id, end_message_id, summary_type
    )


async def get_summaries(
    session_id: str, limit: int = 10, summary_type: Optional[str] = None
) -> List[Dict[str, Any]]:
    return await get_database().get_summaries(session_id, limit, summary_type)


async def mark_messages_as_summarized(
    start_message_id: int, end_message_id: int
) -> int:
    return await get_database().mark_messages_as_summarized(
        start_message_id, end_message_id
    )


async def save_daily_batch_log(
    batch_date: str,
    step1_status: int = 0,
    step2_status: int = 0,
    step3_status: int = 0,
    step4_status: int = 0,
    step5_status: int = 0,
    error_message: Optional[str] = None,
) -> bool:
    return await get_database().save_daily_batch_log(
        batch_date,
        step1_status, step2_status, step3_status,
        step4_status, step5_status,
        error_message,
    )


async def list_expired_active_temporal_states(
    as_of_iso: str,
) -> List[Dict[str, Any]]:
    return await get_database().list_expired_active_temporal_states(as_of_iso)


async def deactivate_temporal_states_by_ids(state_ids: List[str]) -> int:
    return await get_database().deactivate_temporal_states_by_ids(state_ids)


async def get_all_active_temporal_states() -> List[Dict[str, Any]]:
    return await get_database().get_all_active_temporal_states()


async def get_recent_relationship_timeline(limit: int = 3) -> List[Dict[str, Any]]:
    return await get_database().get_recent_relationship_timeline(limit)


async def list_temporal_states_all() -> List[Dict[str, Any]]:
    return await get_database().list_temporal_states_all()


async def insert_temporal_state(
    state_content: str,
    action_rule: Optional[str] = None,
    expire_at: Optional[str] = None,
) -> str:
    return await get_database().insert_temporal_state(
        state_content, action_rule, expire_at
    )


async def list_relationship_timeline_all_desc() -> List[Dict[str, Any]]:
    return await get_database().list_relationship_timeline_all_desc()


async def insert_relationship_timeline_event(
    event_type: str,
    content: str,
    source_summary_id: Optional[str] = None,
    event_id: Optional[str] = None,
) -> str:
    return await get_database().insert_relationship_timeline_event(
        event_type, content, source_summary_id, event_id
    )


async def get_daily_batch_log(batch_date: str) -> Optional[Dict[str, Any]]:
    return await get_database().get_daily_batch_log(batch_date)


async def get_recent_daily_batch_logs(limit: int = 30) -> List[Dict[str, Any]]:
    return await get_database().get_recent_daily_batch_logs(limit)


async def list_incomplete_daily_batch_dates_in_range(
    start_date: str, end_date: str
) -> List[str]:
    return await get_database().list_incomplete_daily_batch_dates_in_range(
        start_date, end_date
    )


async def mark_expired_skipped_daily_batch_logs_before(before_date: str) -> int:
    return await get_database().mark_expired_skipped_daily_batch_logs_before(before_date)


async def update_daily_batch_step_status(
    batch_date: str,
    step_number: int,
    status: int,
    error_message: Optional[str] = None,
) -> bool:
    return await get_database().update_daily_batch_step_status(
        batch_date, step_number, status, error_message
    )


async def mark_messages_as_summarized_by_ids(message_ids: List[int]) -> int:
    return await get_database().mark_messages_as_summarized_by_ids(message_ids)


async def get_unsummarized_count_by_session(session_id: str) -> int:
    return await get_database().get_unsummarized_count_by_session(session_id)


async def get_unsummarized_messages_by_session(
    session_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    return await get_database().get_unsummarized_messages_by_session(session_id, limit)


async def get_all_active_memory_cards(limit: int = 100) -> List[Dict[str, Any]]:
    return await get_database().get_all_active_memory_cards(limit)


async def get_recent_daily_summaries(limit: int = 5) -> List[Dict[str, Any]]:
    return await get_database().get_recent_daily_summaries(limit)


async def get_today_chunk_summaries() -> List[Dict[str, Any]]:
    return await get_database().get_today_chunk_summaries()


async def get_today_user_character_pairs(batch_date: str) -> List[Dict[str, Any]]:
    return await get_database().get_today_user_character_pairs(batch_date)


async def get_unsummarized_messages_desc(
    session_id: str, limit: int = 40
) -> List[Dict[str, Any]]:
    return await get_database().get_unsummarized_messages_desc(session_id, limit)


# ---------------------------------------------------------------------------
# MIGRATION NOTES
# ---------------------------------------------------------------------------
#
# 本次迁移：SQLite + sqlite3（同步） → PostgreSQL + asyncpg（异步）
#
# ── 替换的 SQL 语句 ──────────────────────────────────────────────────────────
#
# 1. datetime('now', '-{N} minutes')
#    → NOW() - $N * INTERVAL '1 minute'
#    位置：expire_stale_vision_pending
#
# 2. date(created_at) = date('now', 'localtime')
#    → created_at::date = CURRENT_DATE
#    位置：get_today_chunk_summaries
#    注意：CURRENT_DATE 使用数据库服务器的时区；建议将 PostgreSQL 时区设置为
#          Asia/Shanghai（东八区），与原 SQLite localtime 行为保持一致。
#
# 3. datetime(expire_at) <= datetime(?)
#    → expire_at <= $1::timestamp
#    位置：list_expired_active_temporal_states
#
# 4. ORDER BY datetime(created_at) DESC
#    → ORDER BY created_at DESC
#    位置：list_temporal_states_all（PostgreSQL TIMESTAMP 列无需 datetime() 包装）
#
# 5. INSERT OR REPLACE INTO daily_batch_log ...
#    → INSERT INTO daily_batch_log ... ON CONFLICT (batch_date) DO UPDATE SET ...
#    位置：save_daily_batch_log
#
# 6. INSERT OR REPLACE INTO config ...
#    → INSERT INTO config ... ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, ...
#    位置：set_config、_backfill_daily_batch_step45_legacy_once
#
# 7. INSERT OR IGNORE INTO config ...
#    → INSERT INTO config ... ON CONFLICT DO NOTHING
#    位置：_config_insert_defaults_if_missing
#
# 8. INSERT OR REPLACE INTO sticker_cache ...
#    → INSERT INTO sticker_cache ... ON CONFLICT (file_unique_id) DO UPDATE SET ...
#    位置：save_sticker_cache
#
# 9. PRAGMA journal_mode=WAL（删除）
#    → 不适用于 PostgreSQL，已删除
#
# 10. PRAGMA table_info(table_name)（删除）
#     → 替换为 ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...（幂等，无需先查列名）
#     位置：各 _ensure_*_column 函数
#
# 11. ? 占位符（全部替换）
#     → $1, $2, $3, ... 位置参数
#
# 12. IN ({placeholders})（如 IN (?,?,?) 动态列表）
#     → = ANY($1::int[]) 或 = ANY($1::text[])
#     位置：mark_messages_as_summarized_by_ids、deactivate_temporal_states_by_ids、
#            get_config_max_updated_at_for_keys
#
# 13. cursor.lastrowid
#     → RETURNING id + conn.fetchval(...)
#     位置：所有 INSERT 语句
#
# 14. INTEGER PRIMARY KEY AUTOINCREMENT
#     → SERIAL PRIMARY KEY
#     位置：所有建表语句
#
# 15. DATETIME / TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#     → TIMESTAMP DEFAULT NOW()
#     位置：所有建表语句
#
# 16. IFNULL(...)（_DAILY_BATCH_INCOMPLETE_SQL）
#     → COALESCE(...)（PostgreSQL 等价函数）
#
# ── 其他注意事项 ─────────────────────────────────────────────────────────────
#
# A. 启动流程变更：
#    main.py 需在启动 Bot 之前增加：
#      from memory.database import initialize_database
#      await initialize_database()
#    或：
#      await get_database().init_pool(config.DATABASE_URL)
#
# B. asyncpg 时间戳返回 datetime 对象，_norm()/_r() 自动转为 ISO 字符串，
#    与原 SQLite 版字符串格式保持兼容。
#
# C. asyncpg 参数传递为 positional（*args），与 sqlite3 的 tuple 形式不同。
#
# D. asyncpg 默认 autocommit；多语句原子操作已用 async with conn.transaction(): 包裹
#    （位置：create_tables、activate_api_config）。
#
# E. get_today_chunk_summaries 中的日期范围依赖服务器时区，推荐在 PostgreSQL 中设置：
#    ALTER DATABASE <dbname> SET timezone TO 'Asia/Shanghai';
#
# F. 需安装：pip install asyncpg
#
# G. persona_configs 表已纳入 create_tables()，与原版通过外部脚本建表对齐，
#    保证自包含初始化。
