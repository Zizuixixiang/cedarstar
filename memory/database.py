"""
短期记忆数据库模块（PostgreSQL / asyncpg 版本）。

使用 asyncpg 连接池操作 PostgreSQL，所有数据库方法均为 async def。
连接池在 MessageDatabase.init_pool() 中创建；全局单例通过 get_database()
获取，startup 阶段需调用一次 await get_database().init_pool(dsn) 或
await initialize_database()。
"""

import asyncpg
import json
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


def _pg_timestamp_naive_utc(dt: _dt.datetime) -> _dt.datetime:
    """
    绑定到 PostgreSQL TIMESTAMP（无时区）列时使用 naive UTC。
    若传入 offset-aware，asyncpg 编码时会触发 naive/aware 混用错误（DataError）。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def _pg_timestamp_naive_shanghai(dt: _dt.datetime) -> _dt.datetime:
    """绑定到业务本地时间 TIMESTAMP 列时使用上海本地 naive datetime。"""
    if dt.tzinfo is None:
        return dt
    tz_sh = _dt.timezone(_dt.timedelta(hours=8))
    return dt.astimezone(tz_sh).replace(tzinfo=None)


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


async def _summaries_ensure_source_column(conn) -> None:
    """为 summaries 补 source / external_events_generated 列（幂等）。"""
    await conn.execute(
        "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS source VARCHAR(32) DEFAULT 'internal'"
    )
    await conn.execute(
        "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS external_events_generated BOOLEAN DEFAULT FALSE"
    )
    logger.debug("summaries.source/external_events_generated 列检查完成")


async def _ensure_mcp_audit_log_table(conn) -> None:
    """创建 mcp_audit_log 表（幂等）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_audit_log (
            id SERIAL PRIMARY KEY,
            token_scope VARCHAR(32),
            tool_name VARCHAR(64),
            arguments JSONB,
            result_status VARCHAR(32),
            error_message TEXT,
            called_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_called_at "
        "ON mcp_audit_log (called_at)"
    )
    logger.debug("mcp_audit_log 表检查完成")


async def _summaries_ensure_archive_columns(conn) -> None:
    """为 summaries 补 daily 归档与收藏字段（幂等）。"""
    await conn.execute(
        "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS archived_by INTEGER"
    )
    await conn.execute(
        "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS is_starred BOOLEAN NOT NULL DEFAULT FALSE"
    )
    await conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'fk_summaries_archived_by'
            ) THEN
                ALTER TABLE summaries
                ADD CONSTRAINT fk_summaries_archived_by
                FOREIGN KEY (archived_by) REFERENCES summaries(id)
                ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
    logger.debug("summaries.archived_by/is_starred 列检查完成")


async def _longterm_memories_ensure_source_columns(conn) -> None:
    """为 longterm_memories 补来源 chunk 与收藏字段（幂等）。"""
    await conn.execute(
        "ALTER TABLE longterm_memories ADD COLUMN IF NOT EXISTS source_chunk_ids JSONB"
    )
    await conn.execute(
        "ALTER TABLE longterm_memories ADD COLUMN IF NOT EXISTS is_starred BOOLEAN NOT NULL DEFAULT FALSE"
    )
    await conn.execute(
        "ALTER TABLE longterm_memories ADD COLUMN IF NOT EXISTS source_date DATE"
    )
    logger.debug("longterm_memories.source_chunk_ids/is_starred/source_date 列检查完成")


async def _daily_batch_log_ensure_step45_columns(conn) -> None:
    """为 daily_batch_log 补 step4/step5 列（幂等）。"""
    await conn.execute(
        "ALTER TABLE daily_batch_log ADD COLUMN IF NOT EXISTS step4_status INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE daily_batch_log ADD COLUMN IF NOT EXISTS step5_status INTEGER DEFAULT 0"
    )
    logger.debug("daily_batch_log step4/step5 列检查完成")


async def _daily_batch_log_ensure_retry_count_column(conn) -> None:
    """为 daily_batch_log 补 retry_count（幂等）。"""
    await conn.execute(
        "ALTER TABLE daily_batch_log ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0"
    )
    logger.debug("daily_batch_log.retry_count 列检查完成")


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


async def _ensure_tool_executions_table(conn) -> None:
    """工具执行记录表：每次 tool call 一行，Context 注入时再按 turn 聚合。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_executions (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT 1,
            tool_name TEXT NOT NULL,
            arguments_json JSONB,
            result_summary TEXT NOT NULL DEFAULT '',
            result_raw TEXT,
            user_message_id INTEGER,
            assistant_message_id INTEGER,
            platform TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_executions_session_created "
        "ON tool_executions (session_id, created_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_executions_turn_seq "
        "ON tool_executions (turn_id, seq)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_executions_user_message "
        "ON tool_executions (user_message_id)"
    )


async def _ensure_token_usage_cache_columns(conn) -> None:
    """为 token_usage 补齐多供应商缓存统计字段（幂等）。"""
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cached_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_hit_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_miss_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_creation_input_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_read_input_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS raw_usage_json JSONB"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS base_url TEXT"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS theoretical_cached_tokens INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS request_type TEXT DEFAULT 'chat'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_usage_request_type "
        "ON token_usage (request_type, created_at)"
    )


async def _ensure_message_platform_file_id_column(conn) -> None:
    """Telegram 图片历史重建：保存可重新下载的 file_id。"""
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS platform_file_id TEXT"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_platform_file_id "
        "ON messages (platform_file_id) WHERE platform_file_id IS NOT NULL"
    )


async def _ensure_summaries_group_column(conn) -> None:
    await conn.execute(
        "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS is_group INTEGER DEFAULT 0"
    )


async def _ensure_group_chat_state_table(conn) -> None:
    """群聊多 Bot：每个 chat 保存连续 bot 互聊轮数。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS group_chat_state (
            chat_id TEXT PRIMARY KEY,
            round_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)


async def _ensure_model_favorites_table(conn) -> None:
    """Mini App API 配置页使用的按供应商收藏模型表。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS model_favorites (
            id SERIAL PRIMARY KEY,
            base_url TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(base_url, model)
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
        ("voice_id", "TEXT"),
    ]:
        await conn.execute(
            f"ALTER TABLE api_configs ADD COLUMN IF NOT EXISTS {col} {col_def}"
        )


async def _ensure_default_search_summary_api_config_row(conn) -> None:
    """若无任意 config_type=search_summary 行，插入占位行（未激活，供 Mini App 填写）。"""
    await ensure_api_configs_schema(conn)
    row = await conn.fetchrow(
        "SELECT 1 FROM api_configs WHERE config_type = $1 LIMIT 1",
        "search_summary",
    )
    if row:
        return
    await conn.execute(
        """
        INSERT INTO api_configs (name, api_key, base_url, model, persona_id, is_active, config_type)
        VALUES ($1, $2, $3, $4, NULL, 0, $5)
        """,
        "搜索摘要模型",
        "",
        "",
        None,
        "search_summary",
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
    await _summaries_ensure_source_column(conn)
    await _summaries_ensure_archive_columns(conn)
    await _longterm_memories_ensure_source_columns(conn)
    await _daily_batch_log_ensure_step45_columns(conn)
    await _daily_batch_log_ensure_retry_count_column(conn)
    await _backfill_daily_batch_step45_legacy_once(conn)
    await _messages_ensure_vision_columns(conn)
    await _ensure_tool_executions_table(conn)
    await _ensure_token_usage_cache_columns(conn)
    await _ensure_message_platform_file_id_column(conn)
    await _ensure_summaries_group_column(conn)
    await _ensure_group_chat_state_table(conn)
    await _ensure_model_favorites_table(conn)
    await _ensure_mcp_audit_log_table(conn)

    await conn.execute(
        "ALTER TABLE meme_pack ADD COLUMN IF NOT EXISTS description TEXT"
    )
    # 清单里短 name 可重复；按 url 幂等，避免重复跑同一链接叠行
    await conn.execute("DROP INDEX IF EXISTS idx_meme_pack_name_unique")
    # 历史多次导入可能叠了相同 url；建唯一索引前保留 id 最小的一条
    dedupe_tag = await conn.execute(
        """
        DELETE FROM meme_pack AS mp1
        USING meme_pack AS mp2
        WHERE mp1.url = mp2.url AND mp1.id > mp2.id
        """
    )
    if dedupe_tag and not str(dedupe_tag).endswith("DELETE 0"):
        logger.info("meme_pack 迁移：按 url 去重 %s", dedupe_tag)
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_meme_pack_url_unique "
        "ON meme_pack (url)"
    )

    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS user_work TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_appearance TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_relationships TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS enable_lutopia INTEGER NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_identity TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_redlines TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_nsfw TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_tools_guide TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS char_offline_mode TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS enable_weather_tool INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS enable_weibo_tool INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS enable_search_tool INTEGER DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS enable_x_tool INTEGER DEFAULT 0"
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
        "CREATE INDEX IF NOT EXISTS idx_summaries_archived_by ON summaries (archived_by)",
        "CREATE INDEX IF NOT EXISTS idx_summaries_is_starred ON summaries (is_starred)",
        "CREATE INDEX IF NOT EXISTS idx_longterm_source_chunks ON longterm_memories USING GIN (source_chunk_ids)",
        "CREATE INDEX IF NOT EXISTS idx_longterm_is_starred ON longterm_memories (is_starred)",
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
        (
            "CREATE INDEX IF NOT EXISTS idx_token_usage_platform_created "
            "ON token_usage (platform, created_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_token_usage_model_created "
            "ON token_usage (model, created_at)"
        ),
    ]
    for sql in index_statements:
        await conn.execute(sql)

    await _config_insert_defaults_if_missing(
        conn,
        [
            ("telegram_max_chars", "50"),
            ("telegram_max_msg", "8"),
            ("send_cot_to_telegram", "1"),
            ("send_cot_in_group_chat", "0"),
            ("telegram_force_recent_images", "0"),
            ("gc_exempt_hits_threshold", "10"),
            ("event_split_max", "8"),
            ("mmr_lambda", "0.75"),
            ("context_archived_daily_limit", "3"),
            ("archived_daily_min_hits", "2"),
            ("starred_boost_factor", "1.2"),
            ("group_chat_silent_mode", "0"),
            ("group_chat_max_rounds", "3"),
            ("group_chat_interject_enabled", "0"),
            ("group_chat_interject_probability", "0.2"),
            ("external_chunk_max_chars", "2000"),
            ("x_daily_read_limit", "100"),
            ("tts_enabled", "false"),
            ("tts_voice_id", ""),
            ("tts_model", "speech-2.8-turbo"),
            ("tts_speed", "0.95"),
            ("tts_vol", "1.0"),
            ("tts_pitch", "0"),
            ("tts_intensity", "0"),
            ("tts_timbre", "0"),
            ("tts_api_key", ""),
        ],
    )

    await _ensure_default_embedding_api_config_row(conn)
    await _ensure_default_search_summary_api_config_row(conn)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_events (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sensor_events_created_at "
        "ON sensor_events(created_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sensor_events_type "
        "ON sensor_events(event_type)"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_diary (
            id SERIAL PRIMARY KEY,
            title TEXT,
            content TEXT NOT NULL,
            trigger_reason TEXT,
            tool_log JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

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
        self.pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=10,
            server_settings={"TimeZone": "Asia/Shanghai"},
        )
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
                        vision_processed INTEGER NOT NULL DEFAULT 1,
                        platform_file_id TEXT
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
                        source_date TIMESTAMP,
                        is_group INTEGER DEFAULT 0,
                        archived_by INTEGER REFERENCES summaries(id) ON DELETE SET NULL,
                        is_starred BOOLEAN NOT NULL DEFAULT FALSE
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
                        retry_count INTEGER NOT NULL DEFAULT 0,
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
                        model TEXT,
                        cached_tokens INTEGER DEFAULT 0,
                        cache_write_tokens INTEGER DEFAULT 0,
                        cache_hit_tokens INTEGER DEFAULT 0,
                        cache_miss_tokens INTEGER DEFAULT 0,
                        cache_creation_input_tokens INTEGER DEFAULT 0,
                        cache_read_input_tokens INTEGER DEFAULT 0,
                        theoretical_cached_tokens INTEGER DEFAULT 0,
                        raw_usage_json JSONB,
                        base_url TEXT,
                        request_type TEXT DEFAULT 'chat'
                    )
                """)

                # tool_executions
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS tool_executions (
                        id SERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        seq INTEGER NOT NULL DEFAULT 1,
                        tool_name TEXT NOT NULL,
                        arguments_json JSONB,
                        result_summary TEXT NOT NULL DEFAULT '',
                        result_raw TEXT,
                        user_message_id INTEGER,
                        assistant_message_id INTEGER,
                        platform TEXT,
                        created_at TIMESTAMP DEFAULT NOW()
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
                        created_at TIMESTAMP DEFAULT NOW(),
                        source_chunk_ids JSONB,
                        is_starred BOOLEAN NOT NULL DEFAULT FALSE
                    )
                """)

                # meme_pack
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS meme_pack (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT,
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
    # tool_executions
    # ------------------------------------------------------------------

    async def save_tool_execution(
        self,
        *,
        session_id: str,
        turn_id: str,
        seq: int,
        tool_name: str,
        arguments_json: Optional[str] = None,
        result_summary: str = "",
        result_raw: Optional[str] = None,
        user_message_id: Optional[int] = None,
        assistant_message_id: Optional[int] = None,
        platform: Optional[str] = None,
    ) -> int:
        """保存一次工具调用记录。raw 只供排查；Context 注入使用 summary。"""
        args_obj: Any = None
        if arguments_json is not None and str(arguments_json).strip():
            try:
                args_obj = json.loads(str(arguments_json))
            except Exception:
                args_obj = {"_raw": str(arguments_json)}
        raw = None if result_raw is None else str(result_raw)
        if raw is not None and len(raw) > 50000:
            raw = raw[:50000] + "\n...(truncated)"
        summary = (result_summary or "").strip()
        if len(summary) > 1200:
            summary = summary[:1200] + "..."
        async with self.pool.acquire() as conn:
            eid = await conn.fetchval(
                """
                INSERT INTO tool_executions (
                    session_id, turn_id, seq, tool_name, arguments_json,
                    result_summary, result_raw, user_message_id,
                    assistant_message_id, platform
                )
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                str(session_id),
                str(turn_id),
                int(seq),
                str(tool_name),
                json.dumps(args_obj, ensure_ascii=False) if args_obj is not None else None,
                summary,
                raw,
                user_message_id,
                assistant_message_id,
                None if platform is None else str(platform),
            )
        logger.debug(
            "保存工具执行记录成功: id=%s session=%s tool=%s",
            eid,
            session_id,
            tool_name,
        )
        return int(eid)

    async def cleanup_tool_executions(self, days: int = 7) -> int:
        """清理超过 N 天的工具执行记录。"""
        async with self.pool.acquire() as conn:
            n = await conn.fetchval(
                "DELETE FROM tool_executions WHERE created_at < NOW() - make_interval(days => $1) RETURNING COUNT(*)",
                int(days),
            )
        if n:
            logger.info("cleanup_tool_executions: 清理 %s 条超过 %d 天的记录", n, days)
        return int(n or 0)

    async def get_recent_tool_executions(
        self,
        session_id: str,
        *,
        limit_turns: int = 3,
        max_rows: int = 20,
    ) -> List[Dict[str, Any]]:
        """取最近若干工具回合的执行记录，按时间/序号正序返回。"""
        async with self.pool.acquire() as conn:
            turn_rows = await conn.fetch(
                """
                SELECT turn_id, MAX(created_at) AS last_at
                FROM tool_executions
                WHERE session_id = $1
                GROUP BY turn_id
                ORDER BY last_at DESC
                LIMIT $2
                """,
                session_id,
                max(1, int(limit_turns)),
            )
            turn_ids = [r["turn_id"] for r in turn_rows]
            if not turn_ids:
                return []
            rows = await conn.fetch(
                """
                WITH recent_rows AS (
                    SELECT id, session_id, turn_id, seq, tool_name, arguments_json,
                           result_summary, user_message_id, assistant_message_id,
                           platform, created_at
                    FROM tool_executions
                    WHERE session_id = $1 AND turn_id = ANY($2::text[])
                    ORDER BY created_at DESC, id DESC
                    LIMIT $3
                )
                SELECT id, session_id, turn_id, seq, tool_name, arguments_json,
                       result_summary, user_message_id, assistant_message_id,
                       platform, created_at
                FROM recent_rows
                ORDER BY created_at ASC, turn_id ASC, seq ASC, id ASC
                """,
                session_id,
                turn_ids,
                max(1, int(max_rows)),
            )
        return [_r(r) for r in rows]

    async def get_tool_executions_for_message_range(
        self,
        session_id: str,
        start_message_id: int,
        end_message_id: int,
    ) -> List[Dict[str, Any]]:
        """取与一段待摘要消息相关的工具记录。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, turn_id, seq, tool_name, arguments_json,
                       result_summary, user_message_id, assistant_message_id,
                       platform, created_at
                FROM tool_executions
                WHERE session_id = $1
                  AND (
                    user_message_id BETWEEN $2 AND $3
                    OR assistant_message_id BETWEEN $2 AND $3
                  )
                ORDER BY created_at ASC, turn_id ASC, seq ASC
                """,
                session_id,
                int(start_message_id),
                int(end_message_id),
            )
        return [_r(r) for r in rows]

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
        platform_file_id: Optional[str] = None,
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
        pfid = None if platform_file_id is None else str(platform_file_id)
        async with self.pool.acquire() as conn:
            new_id = await conn.fetchval(
                """
                INSERT INTO messages (
                    role, content, session_id, user_id, channel_id, message_id,
                    character_id, platform, media_type, image_caption, vision_processed,
                    is_summarized, thinking, platform_file_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                RETURNING id
                """,
                role, content, session_id, uid, cid, mid,
                chrid, plat, mt, image_caption, vp, is_sum, tking, pfid,
            )
        logger.debug(
            "保存消息成功: ID=%s, role=%s, session=%s, platform=%s, "
            "vision_processed=%s, is_summarized=%s, thinking=%s",
            new_id, role, session_id, platform, vp, is_sum, bool(tking),
        )
        return new_id

    async def message_exists(
        self, session_id: str, platform_message_id: str
    ) -> bool:
        """按会话 + 平台消息 ID 判断消息是否已入库，用于 peer relay 幂等。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT 1 FROM messages
                WHERE session_id = $1 AND message_id = $2
                LIMIT 1
                """,
                session_id,
                str(platform_message_id),
            )
        return bool(val)

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

        kw = (keyword or "").strip()
        if kw:
            # ILIKE：不区分大小写；COALESCE 避免 NULL 导致整行无法匹配（用户侧 thinking 常为 NULL）
            conditions.append(
                f"(COALESCE(content, '') ILIKE ${idx} OR COALESCE(thinking, '') ILIKE ${idx})"
            )
            params.append(f"%{kw}%")
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

    async def update_message_by_id(
        self,
        message_id: int,
        *,
        content: Optional[str] = None,
        thinking: Optional[str] = None,
    ) -> bool:
        """按主键更新消息正文和/或思维链；仅更新非 None 的字段。"""
        parts: List[str] = []
        params: List[Any] = []
        idx = 1
        if content is not None:
            parts.append(f"content = ${idx}")
            params.append(content)
            idx += 1
        if thinking is not None:
            parts.append(f"thinking = ${idx}")
            params.append(thinking)
            idx += 1
        if not parts:
            return False
        params.append(message_id)
        sql = f"UPDATE messages SET {', '.join(parts)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            status = await conn.execute(sql, *params)
        return _rowcount(status) > 0

    async def delete_message_by_id(self, message_id: int) -> bool:
        """按主键删除一条消息。"""
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM messages WHERE id = $1",
                message_id,
            )
        return _rowcount(status) > 0

    async def get_logs_filtered(
        self,
        platform: Optional[str] = None,
        level: Optional[str] = None,
        keyword: Optional[str] = None,
        time_from: Optional[_dt.datetime] = None,
        time_to: Optional[_dt.datetime] = None,
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

        if time_from is not None:
            conditions.append(f"created_at >= ${idx}")
            params.append(_pg_timestamp_naive_shanghai(time_from))
            idx += 1

        if time_to is not None:
            conditions.append(f"created_at <= ${idx}")
            params.append(_pg_timestamp_naive_shanghai(time_to))
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
            "platform=%s, level=%s, time_from=%s, time_to=%s",
            total, page, page_size, platform, level, time_from, time_to,
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
                       updated_at, source_message_id, is_active, manual_override
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
        source_date: Optional[date] = None,
        is_group: int = 0,
        source: str = "internal",
        external_events_generated: bool = False,
    ) -> int:
        """保存对话摘要，返回插入 ID。source_date 默认当天；日终跑批传入 batch_date 以便按日历日查询。"""
        if summary_type not in {"chunk", "daily"}:
            raise ValueError(
                f"summary_type '{summary_type}' 不在允许的值中。允许的值: {{'chunk', 'daily'}}"
            )
        sd = (
            source_date
            if source_date is not None
            else _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date()
        )
        async with self.pool.acquire() as conn:
            summary_id = await conn.fetchval(
                """
                INSERT INTO summaries (
                    session_id, summary_text, start_message_id, end_message_id,
                    summary_type, source_date, is_group, source, external_events_generated
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                session_id, summary_text, start_message_id, end_message_id,
                summary_type, sd, int(is_group), source, external_events_generated,
            )
        logger.debug(
            "保存摘要成功: ID=%s, session=%s, type=%s, source=%s", summary_id, session_id, summary_type, source
        )
        return summary_id

    async def get_daily_summary_by_date(
        self, batch_date: str
    ) -> Optional[Dict[str, Any]]:
        """
        按 source_date 日历日 + summary_type=daily 取最新一条（用于日终跑批与 batch_date 对齐）。
        batch_date: YYYY-MM-DD。
        """
        try:
            d = date.fromisoformat(str(batch_date).strip())
        except ValueError:
            logger.warning("get_daily_summary_by_date: 无效日期 %s", batch_date)
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, session_id, summary_text, start_message_id, end_message_id,
                       created_at, summary_type, source_date, archived_by, is_starred
                FROM summaries
                WHERE summary_type = 'daily'
                  AND source_date IS NOT NULL
                  AND source_date::date = $1::date
                ORDER BY created_at DESC
                LIMIT 1
                """,
                d,
            )
        if not row:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "summary_text": row["summary_text"],
            "start_message_id": row["start_message_id"],
            "end_message_id": row["end_message_id"],
            "created_at": _norm(row["created_at"]),
            "summary_type": row["summary_type"],
            "source_date": _norm(row["source_date"]),
            "archived_by": row["archived_by"],
            "is_starred": bool(row["is_starred"]),
        }

    async def get_daily_summaries_by_date(
        self, batch_date: str
    ) -> List[Dict[str, Any]]:
        """按 source_date 日历日取当天所有 daily 摘要，供分会话 daily batch 后续步骤合并使用。"""
        try:
            d = date.fromisoformat(str(batch_date).strip())
        except ValueError:
            logger.warning("get_daily_summaries_by_date: 无效日期 %s", batch_date)
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, summary_text, start_message_id, end_message_id,
                       created_at, summary_type, source_date, is_group, archived_by, is_starred
                FROM summaries
                WHERE summary_type = 'daily'
                  AND source_date IS NOT NULL
                  AND source_date::date = $1::date
                ORDER BY session_id ASC, created_at DESC
                """,
                d,
            )
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "summary_text": r["summary_text"],
                "start_message_id": r["start_message_id"],
                "end_message_id": r["end_message_id"],
                "created_at": _norm(r["created_at"]),
                "summary_type": r["summary_type"],
                "source_date": _norm(r["source_date"]),
                "is_group": r["is_group"],
                "archived_by": r["archived_by"],
                "is_starred": bool(r["is_starred"]),
            }
            for r in rows
        ]

    async def get_summaries_filtered(
        self,
        page: int = 1,
        page_size: int = 20,
        summary_type: Optional[str] = None,
        source_date_from_str: Optional[str] = None,
        source_date_to_str: Optional[str] = None,
        source_filter: Optional[str] = None,
        only_unarchived: bool = False,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        分页查询 summaries；可选 summary_type（chunk/daily）、内容日区间（起止 YYYY-MM-DD，可只填一侧）。
        筛选日使用 COALESCE(source_date::date, created_at::date)，避免 source_date 为空时筛不出。
        排序：source_date DESC（内容所属日，补跑历史也会按该日排在合适位置），空 source_date 置后，再按 created_at DESC。
        返回 (行列表, 总条数)。
        """
        page = max(1, page)
        page_size = max(1, min(int(page_size), 200))
        offset = (page - 1) * page_size

        if summary_type is not None and summary_type not in ("chunk", "daily"):
            raise ValueError("summary_type 须为 chunk、daily 或省略")

        df: Optional[date] = None
        dt: Optional[date] = None
        if source_date_from_str and str(source_date_from_str).strip():
            try:
                df = date.fromisoformat(str(source_date_from_str).strip())
            except ValueError:
                logger.warning(
                    "get_summaries_filtered: 无效 source_date_from %s",
                    source_date_from_str,
                )
        if source_date_to_str and str(source_date_to_str).strip():
            try:
                dt = date.fromisoformat(str(source_date_to_str).strip())
            except ValueError:
                logger.warning(
                    "get_summaries_filtered: 无效 source_date_to %s",
                    source_date_to_str,
                )
        if df is not None and dt is not None and df > dt:
            df, dt = dt, df

        conds: List[str] = []
        params: List[Any] = []

        if summary_type in ("chunk", "daily"):
            params.append(summary_type)
            conds.append(f"summary_type = ${len(params)}")

        if source_filter and source_filter.strip():
            params.append(source_filter.strip())
            conds.append(f"source = ${len(params)}")

        if only_unarchived:
            conds.append("archived_by IS NULL")

        # 筛选日：优先 source_date 日历日；旧数据 source_date 为空时用 created_at::date，否则仅有 source_date 时选不到
        day_expr = "COALESCE(source_date::date, created_at::date)"

        if df is not None:
            params.append(df)
            conds.append(f"{day_expr} >= ${len(params)}::date")
        if dt is not None:
            params.append(dt)
            conds.append(f"{day_expr} <= ${len(params)}::date")

        where_sql = " AND ".join(conds) if conds else "TRUE"

        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM summaries WHERE {where_sql}",
                *params,
            )
            total = int(total or 0)

            params_with_lim = list(params)
            params_with_lim.append(page_size)
            lim_ph = len(params_with_lim)
            params_with_lim.append(offset)
            off_ph = len(params_with_lim)

            rows = await conn.fetch(
                f"""
                SELECT
                    s.id,
                    s.session_id,
                    s.summary_text,
                    s.start_message_id,
                    s.end_message_id,
                    s.created_at,
                    s.summary_type,
                    s.source_date,
                    s.archived_by,
                    s.is_starred,
                    s.source,
                    s.external_events_generated,
                    EXISTS (
                        SELECT 1
                        FROM summaries AS d
                        WHERE d.summary_type = 'daily'
                          AND d.source_date IS NOT NULL
                          AND d.source_date::date = COALESCE(s.source_date::date, s.created_at::date)
                          AND (d.session_id = s.session_id OR d.session_id = 'daily_batch')
                    ) AS has_daily_summary
                FROM summaries AS s
                WHERE {where_sql}
                ORDER BY s.source_date DESC NULLS LAST, s.created_at DESC
                LIMIT ${lim_ph} OFFSET ${off_ph}
                """,
                *params_with_lim,
            )

        items = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "summary_text": r["summary_text"],
                "start_message_id": r["start_message_id"],
                "end_message_id": r["end_message_id"],
                "created_at": _norm(r["created_at"]),
                "summary_type": r["summary_type"],
                "source_date": _norm(r["source_date"]),
                "archived_by": r["archived_by"],
                "is_starred": bool(r["is_starred"]),
                "source": r["source"] or "internal",
                "external_events_generated": bool(r["external_events_generated"]),
                "has_daily_summary": bool(r["has_daily_summary"]),
            }
            for r in rows
        ]
        return items, total

    async def set_summary_starred(self, summary_id: int, is_starred: bool) -> bool:
        """更新 summaries.is_starred；不存在则 False。"""
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE summaries SET is_starred = $1 WHERE id = $2",
                bool(is_starred),
                int(summary_id),
            )
        return _rowcount(status) > 0

    async def recalculate_longterm_starred_for_chunk(self, chunk_id: int) -> List[Dict[str, Any]]:
        """
        查找所有引用 chunk_id 的长期记忆，根据来源 chunk 的收藏状态重新计算 is_starred。
        返回需同步到 Chroma 的 chroma_doc_id/is_starred 列表。
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, chroma_doc_id, source_chunk_ids
                FROM longterm_memories
                WHERE source_chunk_ids @> $1::jsonb
                """,
                json.dumps([int(chunk_id)]),
            )
            changed: List[Dict[str, Any]] = []
            for row in rows:
                raw_ids = row["source_chunk_ids"] or []
                if isinstance(raw_ids, str):
                    try:
                        raw_ids = json.loads(raw_ids)
                    except json.JSONDecodeError:
                        raw_ids = []
                ids = [int(x) for x in raw_ids if str(x).strip().isdigit()]
                if not ids:
                    new_starred = False
                else:
                    new_starred = bool(
                        await conn.fetchval(
                            """
                            SELECT COALESCE(bool_or(is_starred), FALSE)
                            FROM summaries
                            WHERE id = ANY($1::int[])
                            """,
                            ids,
                        )
                    )
                await conn.execute(
                    "UPDATE longterm_memories SET is_starred = $1 WHERE id = $2",
                    new_starred,
                    row["id"],
                )
                if row["chroma_doc_id"]:
                    changed.append(
                        {
                            "chroma_doc_id": row["chroma_doc_id"],
                            "is_starred": new_starred,
                        }
                    )
        return changed

    async def update_summary_by_id(self, summary_id: int, summary_text: str) -> bool:
        """更新 summaries.summary_text；不存在则 False。"""
        text = (summary_text or "").strip()
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE summaries SET summary_text = $1 WHERE id = $2
                """,
                text,
                summary_id,
            )
        return _rowcount(status) > 0

    async def delete_summary_by_id(self, summary_id: int) -> bool:
        """物理删除 summaries 行。"""
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM summaries WHERE id = $1",
                summary_id,
            )
        return _rowcount(status) > 0

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
                           created_at, summary_type, is_group
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
                           created_at, summary_type, is_group
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
                "is_group": r["is_group"],
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
                SELECT id, role, content, created_at, session_id, user_id, channel_id,
                       character_id
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
                "character_id": r["character_id"],
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

    async def get_recent_daily_summaries(
        self, limit: int = 5, session_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch daily summaries from the recent N calendar days; filter by session_id when given."""
        days = max(1, min(int(limit or 1), 100))
        today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date()
        start_day = today - _dt.timedelta(days=days - 1)
        params: List[Any] = [start_day, today]
        where = "WHERE summary_type = 'daily'"
        day_expr = "COALESCE(source_date::date, created_at::date)"
        where += f" AND {day_expr} BETWEEN $1::date AND $2::date"
        if session_id:
            params.append(session_id)
            where += f" AND session_id = ${len(params)}"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, session_id, summary_text, start_message_id, end_message_id,
                       created_at, summary_type, source_date, is_group, is_starred
                FROM summaries
                {where}
                ORDER BY {day_expr} DESC, created_at DESC
                """,
                *params,
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
                "source_date": _norm(r["source_date"]),
                "is_starred": bool(r["is_starred"]),
            }
            for r in rows
        ]
        logger.debug(
            "Fetch recent %s daily-summary days: session=%s count=%s",
            days,
            session_id or "*",
            len(summaries),
        )
        return summaries

    async def get_today_chunk_summaries(
        self, batch_date: Optional[str] = None, include_archived: bool = False
    ) -> List[Dict[str, Any]]:
        """
        获取 chunk 摘要（全局查询，按 created_at 正序）。

        内容日 = COALESCE(source_date::date, created_at::date)。
        - batch_date 为 YYYY-MM-DD：内容日 <= batch_date（日终归档截止当天，补归档之前漏掉的 chunk）。
        - 未传 batch_date：内容日 <= 东八区「今天」（Context / Dashboard；通常仅剩未归档的今日 chunk）。
        """
        day_col = "COALESCE(source_date::date, created_at::date)"
        if batch_date and str(batch_date).strip():
            try:
                d = date.fromisoformat(str(batch_date).strip())
            except ValueError:
                logger.warning("get_today_chunk_summaries: 无效 batch_date %s", batch_date)
                return []
            where_day = f"{day_col} <= $1::date"
            params = (d,)
        else:
            where_day = f"{day_col} <= (now() AT TIME ZONE 'Asia/Shanghai')::date"
            params = ()

        archive_filter = "" if include_archived else "AND archived_by IS NULL"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, session_id, summary_text, start_message_id, end_message_id,
                       created_at, summary_type, source_date, archived_by, is_starred,
                       external_events_generated
                FROM summaries
                WHERE summary_type = 'chunk'
                  AND {where_day}
                  {archive_filter}
                ORDER BY created_at ASC
                """,
                *params,
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
                "source_date": _norm(r["source_date"]),
                "archived_by": r["archived_by"],
                "is_starred": bool(r["is_starred"]),
                "external_events_generated": bool(r["external_events_generated"]),
            }
            for r in rows
        ]
        logger.debug("获取今天的 chunk 摘要: count=%s", len(summaries))
        return summaries

    async def archive_chunk_summaries_by_daily(
        self,
        batch_date: str,
        daily_summary_id: int,
        session_id: Optional[str] = None,
    ) -> int:
        """将指定日期的 chunk 标记为已被 daily 归档，不删除原 chunk。"""
        try:
            d = date.fromisoformat(str(batch_date).strip())
        except ValueError:
            logger.warning("archive_chunk_summaries_by_daily: 无效 batch_date %s", batch_date)
            return 0

        params: List[Any] = [int(daily_summary_id), d]
        session_filter = ""
        if session_id:
            params.append(session_id)
            session_filter = f"AND session_id = ${len(params)}"

        async with self.pool.acquire() as conn:
            status = await conn.execute(
                f"""
                UPDATE summaries
                SET archived_by = $1
                WHERE summary_type = 'chunk'
                  AND COALESCE(source_date::date, created_at::date) <= $2::date
                  {session_filter}
                """,
                *params,
            )
        n = _rowcount(status)
        logger.info(
            "chunk 摘要已归档: date=%s daily_id=%s session=%s count=%s",
            batch_date,
            daily_summary_id,
            session_id or "*",
            n,
        )
        return n

    async def archive_external_chunks_by_daily(
        self,
        batch_date: str,
        daily_summary_id: int,
    ) -> int:
        """将指定日期 external_events_generated=true 的 chunk 回填 archived_by（不删除原 chunk）。"""
        try:
            d = date.fromisoformat(str(batch_date).strip())
        except ValueError:
            logger.warning("archive_external_chunks_by_daily: 无效 batch_date %s", batch_date)
            return 0
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE summaries
                SET archived_by = $1
                WHERE summary_type = 'chunk'
                  AND external_events_generated = TRUE
                  AND COALESCE(source_date::date, created_at::date) <= $2::date
                  AND archived_by IS NULL
                """,
                int(daily_summary_id), d,
            )
        n = _rowcount(status)
        logger.info(
            "external chunk 已回填归档: date=%s daily_id=%s count=%s",
            batch_date, daily_summary_id, n,
        )
        return n

    async def delete_today_chunk_summaries(
        self, batch_date: Optional[str] = None
    ) -> int:
        """
        删除 chunk 记录（规则与 get_today_chunk_summaries 一致）。

        内容日 <= batch_date（或 <= 东八区今天）的全部 chunk 删除，避免滞后写入的积压残留。

        Returns:
            int: 删除的行数。
        """
        day_col = "COALESCE(source_date::date, created_at::date)"
        if batch_date and str(batch_date).strip():
            try:
                d = date.fromisoformat(str(batch_date).strip())
            except ValueError:
                logger.warning("delete_today_chunk_summaries: 无效 batch_date %s", batch_date)
                return 0
            where_day = f"{day_col} <= $1::date"
            params = (d,)
        else:
            where_day = f"{day_col} <= (now() AT TIME ZONE 'Asia/Shanghai')::date"
            params = ()

        async with self.pool.acquire() as conn:
            status = await conn.execute(
                f"""
                DELETE FROM summaries
                WHERE summary_type = 'chunk'
                  AND {where_day}
                """,
                *params,
            )
        try:
            parts = str(status).split()
            n = int(parts[-1]) if parts else 0
        except (ValueError, IndexError):
            n = 0
        if n:
            logger.info("delete_today_chunk_summaries: 已删除 %s 条 chunk 摘要", n)
        return n

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
                       media_type, image_caption, vision_processed, character_id,
                       platform_file_id
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
                "character_id": r["character_id"],
                "platform_file_id": r["platform_file_id"],
            }
            for r in rows
        ]
        messages.reverse()
        logger.debug(
            "获取会话 %s 的最新未摘要消息（正序）: %s 条", session_id, len(messages)
        )
        return messages

    async def get_recent_summarized_messages_desc(
        self, session_id: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """获取指定会话中最新的已摘要消息列表，用于 chunk→正常对话衔接。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at, session_id, user_id, channel_id,
                       media_type, image_caption, vision_processed, character_id,
                       platform_file_id
                FROM messages
                WHERE session_id = $1 AND is_summarized = 1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id, max(1, int(limit)),
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
                "character_id": r["character_id"],
                "platform_file_id": r["platform_file_id"],
            }
            for r in rows
        ]
        messages.reverse()
        logger.debug(
            "获取会话 %s 的最新已摘要消息（正序）: %s 条", session_id, len(messages)
        )
        return messages

    async def get_recent_image_messages(
        self, session_id: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """取近期可重新下载的 Telegram 图片消息。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, created_at, image_caption, platform_file_id
                FROM messages
                WHERE session_id = $1
                  AND platform_file_id IS NOT NULL
                  AND platform_file_id <> ''
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id,
                max(1, int(limit)),
            )
        return [_r(r) for r in rows]

    async def get_group_chat_round_count(self, chat_id: str) -> int:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT round_count FROM group_chat_state WHERE chat_id = $1",
                str(chat_id),
            )
        return int(val or 0)

    async def set_group_chat_round_count(self, chat_id: str, round_count: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO group_chat_state (chat_id, round_count, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                    round_count = EXCLUDED.round_count,
                    updated_at = NOW()
                """,
                str(chat_id),
                max(0, int(round_count)),
            )

    async def increment_group_chat_round_count(self, chat_id: str, delta: int = 1) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO group_chat_state (chat_id, round_count, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                    round_count = group_chat_state.round_count + EXCLUDED.round_count,
                    updated_at = NOW()
                RETURNING round_count
                """,
                str(chat_id),
                max(0, int(delta)),
            )
        return int(row["round_count"] if row else 0)

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
                     step4_status, step5_status, retry_count, error_message, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7, NOW())
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
                       step4_status, step5_status, retry_count, error_message,
                       created_at, updated_at
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
            "retry_count": 0 if row.get("retry_count") is None else int(row["retry_count"]),
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
                       step4_status, step5_status, retry_count, error_message,
                       created_at, updated_at
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
                "retry_count": 0 if r.get("retry_count") is None else int(r["retry_count"]),
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

    async def increment_daily_batch_retry_count(self, batch_date: str) -> int:
        """
        将 retry_count +1；若无该行则插入占位行（五步 0）后视为 1。
        返回更新后的 retry_count。
        """
        dt_val = _dt.datetime.strptime(batch_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO daily_batch_log
                    (batch_date, step1_status, step2_status, step3_status,
                     step4_status, step5_status, retry_count, error_message, updated_at)
                VALUES ($1, 0, 0, 0, 0, 0, 1, NULL, NOW())
                ON CONFLICT (batch_date) DO UPDATE SET
                    retry_count = daily_batch_log.retry_count + 1,
                    updated_at = NOW()
                RETURNING retry_count
                """,
                dt_val,
            )
        rc = int(row["retry_count"]) if row and row.get("retry_count") is not None else 1
        logger.debug("daily_batch_log retry_count=%s date=%s", rc, batch_date)
        return rc

    async def reset_daily_batch_retry_count(self, batch_date: str) -> None:
        """跑批全流程成功后将 retry_count 置 0。"""
        dt_val = _dt.datetime.strptime(batch_date, "%Y-%m-%d").date()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE daily_batch_log
                SET retry_count = 0, updated_at = NOW()
                WHERE batch_date = $1
                """,
                dt_val,
            )

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

    async def update_temporal_state_expire_at(
        self, state_id: str, new_expire_at: _dt.datetime
    ) -> int:
        """更新一条 temporal_states 的 expire_at；返回受影响行数。"""
        sid = (state_id or "").strip()
        if not sid:
            return 0
        exp = _pg_timestamp_naive_utc(new_expire_at)
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE temporal_states SET expire_at = $1 WHERE id = $2",
                exp,
                sid,
            )
        return _rowcount(result)

    async def insert_relationship_timeline_event(
        self,
        event_type: str,
        content: str,
        source_summary_id: Optional[str] = None,
        event_id: Optional[str] = None,
        created_at: Optional[_dt.datetime] = None,
    ) -> str:
        """插入一条 relationship_timeline，返回主键 id（UUID 字符串）。

        created_at 省略时使用库默认值 NOW()；日终 Step 3 可传入 batch 业务日 23:59:59（naive）。
        """
        if event_type not in self.RELATIONSHIP_TIMELINE_EVENT_TYPES:
            raise ValueError(
                f"event_type 无效: {event_type}，允许: {self.RELATIONSHIP_TIMELINE_EVENT_TYPES}"
            )
        eid = event_id or uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            if created_at is None:
                await conn.execute(
                    """
                    INSERT INTO relationship_timeline (id, event_type, content, source_summary_id)
                    VALUES ($1, $2, $3, $4)
                    """,
                    eid, event_type, content, source_summary_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO relationship_timeline (id, event_type, content, source_summary_id, created_at)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    eid, event_type, content, source_summary_id, created_at,
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

    async def update_temporal_state(
        self,
        state_id: str,
        state_content: Optional[str] = None,
        action_rule: Optional[str] = None,
        expire_at: Optional[str] = None,
    ) -> int:
        """更新一条 temporal_states 的 state_content / action_rule / expire_at；返回受影响行数。"""
        sid = (state_id or "").strip()
        if not sid:
            return 0
        sets: List[str] = []
        params: List[Any] = []
        idx = 1
        if state_content is not None:
            sets.append(f"state_content = ${idx}")
            params.append(state_content)
            idx += 1
        if action_rule is not None:
            sets.append(f"action_rule = ${idx}")
            params.append(action_rule)
            idx += 1
        if expire_at is not None:
            if expire_at == "":
                sets.append(f"expire_at = NULL")
            else:
                try:
                    dt_exp = _dt.datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
                    sets.append(f"expire_at = ${idx}")
                    params.append(_pg_timestamp_naive_utc(dt_exp))
                    idx += 1
                except ValueError:
                    logger.warning("update_temporal_state: 解析 expire_at 失败: %s", expire_at)
        if not sets:
            return 0
        params.append(sid)
        sql = f"UPDATE temporal_states SET {', '.join(sets)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            result = await conn.execute(sql, *params)
        return _rowcount(result)

    async def save_temporal_state(
        self,
        id: str,
        state_content: str,
        action_rule: Optional[str],
        expire_at: _dt.datetime,
        is_active: int = 1,
    ) -> None:
        """插入一条 temporal_states；id 冲突则忽略。"""
        exp = _pg_timestamp_naive_utc(expire_at)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO temporal_states (id, state_content, action_rule, expire_at, is_active, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                id,
                state_content,
                action_rule,
                exp,
                is_active,
            )

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

    async def purge_logs_older_than_days(self, days: int = 7) -> int:
        """
        删除 ``logs`` 表中 ``created_at`` 早于 ``NOW() - days`` 的记录。
        供日终跑批等定时任务控制 Mini App「系统日志」体量。
        """
        if days < 1:
            return 0
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM logs
                WHERE created_at < (NOW() - ($1::integer * INTERVAL '1 day'))
                """,
                int(days),
            )
        # asyncpg: "DELETE 42"
        parts = (status or "").split()
        if len(parts) >= 2 and parts[0] == "DELETE":
            try:
                return int(parts[1])
            except ValueError:
                return 0
        return 0

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
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        theoretical_cached_tokens: int = 0,
        raw_usage: Optional[Dict[str, Any]] = None,
        base_url: Optional[str] = None,
        request_type: Optional[str] = None,
    ) -> int:
        """保存 token 使用量，返回插入 ID。"""
        async with self.pool.acquire() as conn:
            usage_id = await conn.fetchval(
                """
                INSERT INTO token_usage (
                    platform, prompt_tokens, completion_tokens, total_tokens, model,
                    cached_tokens, cache_write_tokens, cache_hit_tokens, cache_miss_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens, theoretical_cached_tokens,
                    raw_usage_json, base_url, request_type
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15)
                RETURNING id
                """,
                platform,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                model,
                int(cached_tokens or 0),
                int(cache_write_tokens or 0),
                int(cache_hit_tokens or 0),
                int(cache_miss_tokens or 0),
                int(cache_creation_input_tokens or 0),
                int(cache_read_input_tokens or 0),
                int(theoretical_cached_tokens or 0),
                json.dumps(raw_usage, ensure_ascii=False) if raw_usage is not None else None,
                None if base_url is None else str(base_url),
                request_type or "chat",
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
                SELECT total_tokens, prompt_tokens, completion_tokens, platform,
                       cached_tokens, cache_write_tokens, cache_hit_tokens,
                       cache_miss_tokens, cache_creation_input_tokens,
                       cache_read_input_tokens, model, created_at
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
                    "cached_tokens": 0, "cache_write_tokens": 0,
                    "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                    "call_count": 0, "by_platform": {}
                }
            
            p = row[3] or "unknown"
            return {
                "total_tokens": row[0] or 0,
                "prompt_tokens": row[1] or 0,
                "completion_tokens": row[2] or 0,
                "cached_tokens": row[4] or 0,
                "cache_write_tokens": row[5] or 0,
                "cache_hit_tokens": row[6] or 0,
                "cache_miss_tokens": row[7] or 0,
                "cache_creation_input_tokens": row[8] or 0,
                "cache_read_input_tokens": row[9] or 0,
                "model": row[10],
                "created_at": _norm(row[11]),
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
                SELECT SUM(total_tokens), SUM(prompt_tokens), SUM(completion_tokens),
                       SUM(cached_tokens), SUM(cache_write_tokens),
                       SUM(cache_hit_tokens), SUM(cache_miss_tokens),
                       SUM(cache_creation_input_tokens), SUM(cache_read_input_tokens),
                       COUNT(*)
                FROM token_usage {base_cond}
                """,
                *params,
            )
            total = row[0] or 0
            prompt = row[1] or 0
            completion = row[2] or 0
            cached = row[3] or 0
            cache_write = row[4] or 0
            cache_hit = row[5] or 0
            cache_miss = row[6] or 0
            cache_creation = row[7] or 0
            cache_read = row[8] or 0
            count = row[9] or 0

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
            "cached_tokens": cached,
            "cache_write_tokens": cache_write,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "call_count": count,
            "by_platform": by_platform,
        }

    async def get_token_observability_stats(
        self, start_date, platform: Optional[str] = None
    ) -> Dict[str, Any]:
        """返回 Mini App 观测页使用的 token/cache 聚合数据。"""
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

        sum_sql = f"""
            SELECT SUM(total_tokens), SUM(prompt_tokens), SUM(completion_tokens),
                   SUM(cached_tokens), SUM(cache_write_tokens),
                   SUM(cache_hit_tokens), SUM(cache_miss_tokens),
                   SUM(cache_creation_input_tokens), SUM(cache_read_input_tokens),
                   COUNT(*)
            FROM token_usage {base_cond}
        """
        by_platform_sql = f"""
            SELECT COALESCE(platform, 'unknown') AS platform,
                   SUM(total_tokens) AS total_tokens,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cache_hit_tokens) AS cache_hit_tokens,
                   SUM(cache_miss_tokens) AS cache_miss_tokens,
                   COUNT(*) AS call_count
            FROM token_usage {base_cond}
            GROUP BY COALESCE(platform, 'unknown')
            ORDER BY total_tokens DESC
        """
        by_model_sql = f"""
            SELECT COALESCE(model, 'unknown') AS model,
                   SUM(total_tokens) AS total_tokens,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cache_hit_tokens) AS cache_hit_tokens,
                   SUM(cache_miss_tokens) AS cache_miss_tokens,
                   COUNT(*) AS call_count
            FROM token_usage {base_cond}
            GROUP BY COALESCE(model, 'unknown')
            ORDER BY total_tokens DESC
            LIMIT 20
        """
        by_day_sql = f"""
            SELECT created_at::date AS day,
                   SUM(total_tokens) AS total_tokens,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cache_hit_tokens) AS cache_hit_tokens,
                   SUM(cache_miss_tokens) AS cache_miss_tokens,
                   COUNT(*) AS call_count
            FROM token_usage {base_cond}
            GROUP BY created_at::date
            ORDER BY day DESC
            LIMIT 31
        """
        recent_sql = f"""
            SELECT id, created_at, platform, model, prompt_tokens,
                   completion_tokens, total_tokens, cached_tokens,
                   cache_write_tokens, cache_hit_tokens, cache_miss_tokens,
                   cache_creation_input_tokens, cache_read_input_tokens,
                   raw_usage_json
            FROM token_usage {base_cond}
            ORDER BY created_at DESC
            LIMIT 50
        """

        async with self.pool.acquire() as conn:
            totals = await conn.fetchrow(sum_sql, *params)
            rows_platform = await conn.fetch(by_platform_sql, *params)
            rows_model = await conn.fetch(by_model_sql, *params)
            rows_day = await conn.fetch(by_day_sql, *params)
            recent_rows = await conn.fetch(recent_sql, *params)

        prompt_tokens = (totals[1] or 0) if totals else 0
        cache_write_tokens = (totals[4] or 0) if totals else 0
        cache_hit_tokens = (totals[5] or 0) if totals else 0
        cache_miss_tokens = (totals[6] or 0) if totals else 0
        cache_create_tokens = (totals[7] or 0) if totals else 0
        cache_read_tokens = (totals[8] or 0) if totals else 0
        hit_tokens = max(cache_hit_tokens, cache_read_tokens)
        cache_hit_rate = (
            hit_tokens / prompt_tokens
            if prompt_tokens > 0
            else 0
        )
        return {
            "totals": {
                "total_tokens": (totals[0] or 0) if totals else 0,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": (totals[2] or 0) if totals else 0,
                "cache_write_tokens": cache_write_tokens,
                "cache_hit_tokens": cache_hit_tokens,
                "cache_miss_tokens": cache_miss_tokens,
                "cache_creation_input_tokens": cache_create_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "call_count": (totals[9] or 0) if totals else 0,
                "cache_hit_rate": cache_hit_rate,
            },
            "by_platform": [_r(r) for r in rows_platform],
            "by_model": [_r(r) for r in rows_model],
            "by_day": [_r(r) for r in rows_day],
            "recent": [_r(r) for r in recent_rows],
        }

    async def list_recent_tool_executions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        platform: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按时间倒序列出最近工具调用，供 Mini App 展示。"""
        conditions: List[str] = []
        params: List[Any] = []
        if platform:
            params.append(platform)
            conditions.append(f"platform = ${len(params)}")
        if session_id:
            params.append(session_id)
            conditions.append(f"session_id = ${len(params)}")
        where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
        limit_v = max(1, min(200, int(limit)))
        offset_v = max(0, int(offset))
        count_sql = f"SELECT COUNT(*) FROM tool_executions {where_sql}"
        params_count = list(params)
        params_page = list(params)
        params_page.extend([limit_v, offset_v])
        limit_ref = f"${len(params_page) - 1}"
        offset_ref = f"${len(params_page)}"
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(count_sql, *params_count)
            rows = await conn.fetch(
                f"""
                SELECT id, session_id, turn_id, seq, tool_name, arguments_json,
                       result_summary, result_raw, user_message_id,
                       assistant_message_id, platform, created_at
                FROM tool_executions
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT {limit_ref} OFFSET {offset_ref}
                """,
                *params_page,
            )
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = _r(row)
            raw = item.get("result_raw")
            if raw is not None:
                raw_s = str(raw)
                item["result_raw_length"] = len(raw_s)
                item["result_raw_preview"] = raw_s[:1200]
                item.pop("result_raw", None)
            else:
                item["result_raw_length"] = 0
                item["result_raw_preview"] = ""
            out.append(item)
        return {"items": out, "total": int(total or 0), "limit": limit_v, "offset": offset_v}

    async def list_model_favorites(self, base_url: Optional[str] = None) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = ""
        if base_url:
            params.append(str(base_url).rstrip("/"))
            where = "WHERE base_url = $1"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, base_url, model, created_at
                FROM model_favorites
                {where}
                ORDER BY base_url ASC, model ASC
                """,
                *params,
            )
        return [_r(r) for r in rows]

    async def add_model_favorite(self, base_url: str, model: str) -> int:
        async with self.pool.acquire() as conn:
            fid = await conn.fetchval(
                """
                INSERT INTO model_favorites (base_url, model)
                VALUES ($1, $2)
                ON CONFLICT (base_url, model) DO UPDATE SET model = EXCLUDED.model
                RETURNING id
                """,
                str(base_url).rstrip("/"),
                str(model).strip(),
            )
        return int(fid)

    async def delete_model_favorite(self, favorite_id: int) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM model_favorites WHERE id = $1",
                int(favorite_id),
            )
        return _rowcount(status) > 0

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

    async def get_tts_config(self) -> dict:
        """批量读取 TTS 运行参数，返回带默认值的 dict。
        api_key / voice_id / model 优先从激活的 tts api_configs 行读取；
        速度等调参从 config 表读取。"""
        # 读 config 表的调参
        param_keys = [
            "tts_enabled",
            "tts_speed",
            "tts_vol",
            "tts_pitch",
            "tts_intensity",
            "tts_timbre",
        ]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM config WHERE key = ANY($1::text[])", param_keys
            )
            # 读激活的 tts api_config
            api_row = await conn.fetchrow(
                "SELECT api_key, base_url, model, voice_id FROM api_configs "
                "WHERE config_type = 'tts' AND is_active = 1 LIMIT 1"
            )
        cfg = {r["key"]: r["value"] for r in rows}
        api_key = ""
        voice_id = ""
        model = "speech-2.8-turbo"
        if api_row:
            api_key = api_row["api_key"] or ""
            voice_id = api_row["voice_id"] or ""
            model = api_row["model"] or "speech-2.8-turbo"
        tts_enabled_raw = cfg.get("tts_enabled", "false").lower()
        return {
            "enabled": tts_enabled_raw in ("true", "1"),
            "voice_id": voice_id,
            "model": model,
            "speed": float(cfg.get("tts_speed", "0.95")),
            "vol": float(cfg.get("tts_vol", "1.0")),
            "pitch": int(cfg.get("tts_pitch", "0")),
            "intensity": int(cfg.get("tts_intensity", "0")),
            "timbre": int(cfg.get("tts_timbre", "0")),
            "api_key": api_key,
        }

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
            "name", "char_name", "char_identity", "char_personality", "char_speech_style",
            "char_redlines", "char_appearance", "char_relationships",
            "char_nsfw", "char_tools_guide", "char_offline_mode",
            "user_name", "user_body", "user_work", "user_habits",
            "user_likes_dislikes", "user_values", "user_hobbies", "user_taboos",
            "user_nsfw", "user_other",             "system_rules", "enable_lutopia",
            "enable_weather_tool", "enable_weibo_tool", "enable_search_tool", "enable_x_tool",
        ]
        cols = ", ".join(fields)
        placeholders = ", ".join([f"${i + 1}" for i in range(len(fields))])
        values: List[Any] = []
        for f in fields:
            if f == "enable_lutopia":
                raw = data.get("enable_lutopia", 0)
                try:
                    values.append(1 if int(raw) else 0)
                except (TypeError, ValueError):
                    values.append(0)
            elif f == "enable_weather_tool":
                raw = data.get("enable_weather_tool", 0)
                try:
                    values.append(1 if int(raw) else 0)
                except (TypeError, ValueError):
                    values.append(0)
            elif f == "enable_weibo_tool":
                raw = data.get("enable_weibo_tool", 0)
                try:
                    values.append(1 if int(raw) else 0)
                except (TypeError, ValueError):
                    values.append(0)
            elif f == "enable_search_tool":
                raw = data.get("enable_search_tool", 0)
                try:
                    values.append(1 if int(raw) else 0)
                except (TypeError, ValueError):
                    values.append(0)
            elif f == "enable_x_tool":
                raw = data.get("enable_x_tool", 0)
                try:
                    values.append(1 if int(raw) else 0)
                except (TypeError, ValueError):
                    values.append(0)
            else:
                values.append(data.get(f, ""))
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                f"INSERT INTO persona_configs ({cols}) VALUES ({placeholders}) RETURNING id",
                *values,
            )
        return row_id if row_id is not None else -1

    async def update_persona_config(self, persona_id: int, data: Dict[str, Any]) -> bool:
        """更新人设配置。"""
        allowed = {
            "name", "char_name", "char_identity", "char_personality", "char_speech_style",
            "char_redlines", "char_appearance", "char_relationships",
            "char_nsfw", "char_tools_guide", "char_offline_mode",
            "user_name", "user_body", "user_work", "user_habits",
            "user_likes_dislikes", "user_values", "user_hobbies", "user_taboos",
            "user_nsfw", "user_other",             "system_rules", "enable_lutopia",
            "enable_weather_tool", "enable_weibo_tool", "enable_search_tool", "enable_x_tool",
        }
        update_data = {k: v for k, v in data.items() if k in allowed}
        if "enable_lutopia" in update_data:
            try:
                update_data["enable_lutopia"] = (
                    1 if int(update_data["enable_lutopia"]) else 0
                )
            except (TypeError, ValueError):
                update_data["enable_lutopia"] = 0
        if "enable_weather_tool" in update_data:
            try:
                update_data["enable_weather_tool"] = (
                    1 if int(update_data["enable_weather_tool"]) else 0
                )
            except (TypeError, ValueError):
                update_data["enable_weather_tool"] = 0
        if "enable_weibo_tool" in update_data:
            try:
                update_data["enable_weibo_tool"] = (
                    1 if int(update_data["enable_weibo_tool"]) else 0
                )
            except (TypeError, ValueError):
                update_data["enable_weibo_tool"] = 0
        if "enable_search_tool" in update_data:
            try:
                update_data["enable_search_tool"] = (
                    1 if int(update_data["enable_search_tool"]) else 0
                )
            except (TypeError, ValueError):
                update_data["enable_search_tool"] = 0
        if "enable_x_tool" in update_data:
            try:
                update_data["enable_x_tool"] = (
                    1 if int(update_data["enable_x_tool"]) else 0
                )
            except (TypeError, ValueError):
                update_data["enable_x_tool"] = 0
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
                           a.persona_id, a.is_active, a.config_type, a.voice_id,
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
                           a.persona_id, a.is_active, a.config_type, a.voice_id,
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
                INSERT INTO api_configs (name, api_key, base_url, model, persona_id, config_type, voice_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                data.get("name", ""),
                data.get("api_key", ""),
                data.get("base_url", ""),
                data.get("model"),
                data.get("persona_id"),
                data.get("config_type", "chat"),
                data.get("voice_id"),
            )
        return row_id if row_id is not None else -1

    async def update_api_config(self, config_id: int, data: Dict[str, Any]) -> bool:
        """更新 API 配置。"""
        allowed = {"name", "api_key", "base_url", "model", "persona_id", "config_type", "voice_id"}
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

    async def upsert_longterm_memory_by_chroma_id(
        self,
        content: str,
        chroma_doc_id: str,
        score: int = 5,
        source_chunk_ids: Optional[List[int]] = None,
        is_starred: bool = False,
        source_date: Optional[date] = None,
        theme: Optional[str] = None,
        entities: Optional[List[str]] = None,
        emotion: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> int:
        """按 Chroma doc_id 写入或更新长期记忆镜像记录。"""
        cid = (chroma_doc_id or "").strip()
        if not cid:
            raise ValueError("chroma_doc_id 不能为空")
        chunk_json = json.dumps([int(x) for x in (source_chunk_ids or [])])
        entities_json = json.dumps(entities or [], ensure_ascii=False)
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                UPDATE longterm_memories
                SET content = $1,
                    score = $2,
                    source_chunk_ids = $3::jsonb,
                    is_starred = $4,
                    source_date = $5,
                    theme = $6,
                    entities = $7::jsonb,
                    emotion = $8,
                    event_type = $9
                WHERE chroma_doc_id = $10
                RETURNING id
                """,
                content,
                int(score),
                chunk_json,
                bool(is_starred),
                source_date,
                theme,
                entities_json,
                emotion,
                event_type,
                cid,
            )
            if row_id is not None:
                return int(row_id)
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO longterm_memories (
                        content, chroma_doc_id, score, source_chunk_ids, is_starred, source_date,
                        theme, entities, emotion, event_type
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8::jsonb, $9, $10)
                    RETURNING id
                    """,
                    content,
                    cid,
                    int(score),
                    chunk_json,
                    bool(is_starred),
                    source_date,
                    theme,
                    entities_json,
                    emotion,
                    event_type,
                )
            )

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
                    SELECT id, content, chroma_doc_id, score, created_at,
                           source_chunk_ids, is_starred
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
                    SELECT id, content, chroma_doc_id, score, created_at,
                           source_chunk_ids, is_starred
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
                "source_chunk_ids": r["source_chunk_ids"],
                "is_starred": bool(r["is_starred"]),
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
                SELECT id, content, chroma_doc_id, score, created_at,
                       source_chunk_ids, is_starred
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
            "source_chunk_ids": row["source_chunk_ids"],
            "is_starred": bool(row["is_starred"]),
        }

    async def delete_longterm_memory(self, memory_id: int) -> bool:
        """删除长期记忆镜像记录。"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM longterm_memories WHERE id = $1", memory_id
            )
        return _rowcount(result) > 0

    async def delete_longterm_memory_by_chroma_id(self, chroma_doc_id: str) -> bool:
        """按 Chroma 文档 id 删除 longterm_memories 镜像行（若有）。"""
        cid = (chroma_doc_id or "").strip()
        if not cid:
            return False
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM longterm_memories WHERE chroma_doc_id = $1",
                cid,
            )
        return _rowcount(result) > 0

    # ------------------------------------------------------------------
    # meme_pack
    # ------------------------------------------------------------------

    async def fetch_meme_pack_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """若 ``meme_pack.url`` 已存在则返回该行 ``id/name/description/url/is_animated``，否则 ``None``。"""
        u = (url or "").strip()
        if not u:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, description, url, is_animated "
                "FROM meme_pack WHERE url = $1",
                u,
            )
        return dict(row) if row else None

    async def insert_meme_pack(
        self,
        name: str,
        url: str,
        is_animated: int,
        description: str = "",
    ) -> int:
        """按 url 幂等写入：同 url 已存在则更新 name/description/is_animated，返回该行 id；失败返回 -1。"""
        desc_stripped = (description or "").strip()
        desc_val = desc_stripped if desc_stripped else None
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO meme_pack (name, description, url, is_animated)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (url) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    is_animated = EXCLUDED.is_animated
                RETURNING id
                """,
                (name or "").strip(),
                desc_val,
                (url or "").strip(),
                int(is_animated),
            )
        return int(row_id) if row_id is not None else -1

    async def fetch_all_meme_pack(self) -> List[Dict[str, Any]]:
        """返回 meme_pack 全表行，用于批量重同步 Chroma。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, description, url, is_animated FROM meme_pack ORDER BY id"
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

    # ------------------------------------------------------------------
    # sensor_events / autonomous_diary
    # ------------------------------------------------------------------

    async def save_sensor_event(self, event_type: str, payload: Dict[str, Any]) -> int:
        """写入一条传感器原始事件，返回新行 id。"""
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO sensor_events (event_type, payload)
                VALUES ($1, $2::jsonb)
                RETURNING id
                """,
                event_type,
                payload,
            )
        return int(row_id)

    async def get_sensor_events(
        self, event_type: Optional[str] = None, hours: int = 24
    ) -> List[Dict[str, Any]]:
        """取最近 N 小时内的事件，可选按类型过滤。"""
        async with self.pool.acquire() as conn:
            if event_type:
                rows = await conn.fetch(
                    """
                    SELECT id, event_type, payload, created_at
                    FROM sensor_events
                    WHERE event_type = $1
                      AND created_at >= NOW() - ($2::integer * INTERVAL '1 hour')
                    ORDER BY created_at DESC
                    """,
                    event_type,
                    hours,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, event_type, payload, created_at
                    FROM sensor_events
                    WHERE created_at >= NOW() - ($1::integer * INTERVAL '1 hour')
                    ORDER BY created_at DESC
                    """,
                    hours,
                )
        return [_r(rec) for rec in rows]

    async def get_latest_sensor_by_type(
        self, event_type: str
    ) -> Optional[Dict[str, Any]]:
        """某类传感器最新一条。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, event_type, payload, created_at
                FROM sensor_events
                WHERE event_type = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                event_type,
            )
        return _r(row) if row else None

    async def get_max_sensor_created_at_iso(self) -> Optional[str]:
        """任意传感器事件的最晚时间（ISO 字符串），无数据则 None。"""
        async with self.pool.acquire() as conn:
            ts = await conn.fetchval("SELECT MAX(created_at) FROM sensor_events")
        if ts is None:
            return None
        return str(_norm(ts))

    async def has_recent_sensor_event(
        self, event_type: str, minutes: int = 30
    ) -> bool:
        """最近 N 分钟内是否存在指定类型事件。"""
        async with self.pool.acquire() as conn:
            n = await conn.fetchval(
                """
                SELECT COUNT(*) FROM sensor_events
                WHERE event_type = $1
                  AND created_at >= NOW() - ($2::integer * INTERVAL '1 minute')
                """,
                event_type,
                minutes,
            )
        return int(n or 0) > 0

    async def save_autonomous_diary(
        self,
        title: Optional[str],
        content: str,
        trigger_reason: Optional[str],
        tool_log: Optional[Any] = None,
    ) -> int:
        """写入自主活动日记，返回新行 id。"""
        async with self.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO autonomous_diary
                    (title, content, trigger_reason, tool_log)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id
                """,
                title,
                content,
                trigger_reason,
                tool_log,
            )
        return int(row_id)

    async def get_autonomous_diaries(
        self, page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        """分页日记列表，返回 {total, items}。"""
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        offset = (page - 1) * page_size
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM autonomous_diary")
            rows = await conn.fetch(
                """
                SELECT id, title, content, trigger_reason, tool_log, created_at
                FROM autonomous_diary
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                page_size,
                offset,
            )
        items = [_r(rec) for rec in rows]
        return {"total": int(total or 0), "items": items}

    async def get_autonomous_diary_by_id(
        self, diary_id: int
    ) -> Optional[Dict[str, Any]]:
        """按 id 取单条日记。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, title, content, trigger_reason, tool_log, created_at
                FROM autonomous_diary
                WHERE id = $1
                """,
                diary_id,
            )
        return _r(row) if row else None

    async def purge_old_sensor_events(self, hours: int = 72) -> int:
        """删除超过 N 小时的传感器事件，返回删除行数。"""
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM sensor_events
                WHERE created_at < NOW() - ($1::integer * INTERVAL '1 hour')
                """,
                hours,
            )
        return _rowcount(status)


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
    platform_file_id: Optional[str] = None,
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
        platform_file_id,
    )


async def message_exists(session_id: str, platform_message_id: str) -> bool:
    return await get_database().message_exists(session_id, platform_message_id)


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
    source_date: Optional[date] = None,
    is_group: int = 0,
    source: str = "internal",
    external_events_generated: bool = False,
) -> int:
    return await get_database().save_summary(
        session_id,
        summary_text,
        start_message_id,
        end_message_id,
        summary_type,
        source_date,
        is_group,
        source,
        external_events_generated,
    )


async def get_daily_summary_by_date(
    batch_date: str,
) -> Optional[Dict[str, Any]]:
    return await get_database().get_daily_summary_by_date(batch_date)


async def get_daily_summaries_by_date(batch_date: str) -> List[Dict[str, Any]]:
    return await get_database().get_daily_summaries_by_date(batch_date)


async def get_summaries_filtered(
    page: int = 1,
    page_size: int = 20,
    summary_type: Optional[str] = None,
    source_date_from: Optional[str] = None,
    source_date_to: Optional[str] = None,
    source_filter: Optional[str] = None,
    only_unarchived: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    return await get_database().get_summaries_filtered(
        page=page,
        page_size=page_size,
        summary_type=summary_type,
        source_date_from_str=source_date_from,
        source_date_to_str=source_date_to,
        source_filter=source_filter,
        only_unarchived=only_unarchived,
    )


async def update_summary_by_id(summary_id: int, summary_text: str) -> bool:
    return await get_database().update_summary_by_id(summary_id, summary_text)


async def set_summary_starred(summary_id: int, is_starred: bool) -> bool:
    return await get_database().set_summary_starred(summary_id, is_starred)


async def recalculate_longterm_starred_for_chunk(chunk_id: int) -> List[Dict[str, Any]]:
    return await get_database().recalculate_longterm_starred_for_chunk(chunk_id)


async def delete_summary_by_id(summary_id: int) -> bool:
    return await get_database().delete_summary_by_id(summary_id)


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


async def update_temporal_state_expire_at(
    state_id: str, new_expire_at: _dt.datetime
) -> int:
    return await get_database().update_temporal_state_expire_at(
        state_id, new_expire_at
    )


async def get_all_active_temporal_states() -> List[Dict[str, Any]]:
    return await get_database().get_all_active_temporal_states()


async def get_recent_relationship_timeline(limit: int = 3) -> List[Dict[str, Any]]:
    return await get_database().get_recent_relationship_timeline(limit)


async def list_temporal_states_all() -> List[Dict[str, Any]]:
    return await get_database().list_temporal_states_all()


async def update_temporal_state(
    state_id: str,
    state_content: Optional[str] = None,
    action_rule: Optional[str] = None,
    expire_at: Optional[str] = None,
) -> int:
    return await get_database().update_temporal_state(
        state_id, state_content=state_content, action_rule=action_rule, expire_at=expire_at
    )


async def insert_temporal_state(
    state_content: str,
    action_rule: Optional[str] = None,
    expire_at: Optional[str] = None,
) -> str:
    return await get_database().insert_temporal_state(
        state_content, action_rule, expire_at
    )


async def save_temporal_state(
    id: str,
    state_content: str,
    action_rule: Optional[str],
    expire_at: _dt.datetime,
    is_active: int = 1,
) -> None:
    return await get_database().save_temporal_state(
        id, state_content, action_rule, expire_at, is_active
    )


async def list_relationship_timeline_all_desc() -> List[Dict[str, Any]]:
    return await get_database().list_relationship_timeline_all_desc()


async def insert_relationship_timeline_event(
    event_type: str,
    content: str,
    source_summary_id: Optional[str] = None,
    event_id: Optional[str] = None,
    created_at: Optional[_dt.datetime] = None,
) -> str:
    return await get_database().insert_relationship_timeline_event(
        event_type, content, source_summary_id, event_id, created_at
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


async def purge_logs_older_than_days(days: int = 7) -> int:
    return await get_database().purge_logs_older_than_days(days)


async def update_daily_batch_step_status(
    batch_date: str,
    step_number: int,
    status: int,
    error_message: Optional[str] = None,
) -> bool:
    return await get_database().update_daily_batch_step_status(
        batch_date, step_number, status, error_message
    )


async def increment_daily_batch_retry_count(batch_date: str) -> int:
    return await get_database().increment_daily_batch_retry_count(batch_date)


async def reset_daily_batch_retry_count(batch_date: str) -> None:
    await get_database().reset_daily_batch_retry_count(batch_date)


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


async def get_recent_daily_summaries(
    limit: int = 5, session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    return await get_database().get_recent_daily_summaries(limit, session_id)


async def get_today_chunk_summaries(
    batch_date: Optional[str] = None,
    include_archived: bool = False,
) -> List[Dict[str, Any]]:
    return await get_database().get_today_chunk_summaries(batch_date, include_archived)


async def archive_chunk_summaries_by_daily(
    batch_date: str,
    daily_summary_id: int,
    session_id: Optional[str] = None,
) -> int:
    return await get_database().archive_chunk_summaries_by_daily(
        batch_date,
        daily_summary_id,
        session_id,
    )


async def archive_external_chunks_by_daily(
    batch_date: str,
    daily_summary_id: int,
) -> int:
    return await get_database().archive_external_chunks_by_daily(
        batch_date, daily_summary_id,
    )


async def delete_today_chunk_summaries(
    batch_date: Optional[str] = None,
) -> int:
    return await get_database().delete_today_chunk_summaries(batch_date)


async def get_today_user_character_pairs(batch_date: str) -> List[Dict[str, Any]]:
    return await get_database().get_today_user_character_pairs(batch_date)


async def get_unsummarized_messages_desc(
    session_id: str, limit: int = 40
) -> List[Dict[str, Any]]:
    return await get_database().get_unsummarized_messages_desc(session_id, limit)


async def get_recent_summarized_messages_desc(
    session_id: str, limit: int = 5
) -> List[Dict[str, Any]]:
    return await get_database().get_recent_summarized_messages_desc(session_id, limit)


async def save_tool_execution(**kwargs) -> int:
    return await get_database().save_tool_execution(**kwargs)


async def get_recent_tool_executions(
    session_id: str, *, limit_turns: int = 3, max_rows: int = 20
) -> List[Dict[str, Any]]:
    return await get_database().get_recent_tool_executions(
        session_id, limit_turns=limit_turns, max_rows=max_rows
    )


async def get_tool_executions_for_message_range(
    session_id: str, start_message_id: int, end_message_id: int
) -> List[Dict[str, Any]]:
    return await get_database().get_tool_executions_for_message_range(
        session_id, start_message_id, end_message_id
    )


async def cleanup_tool_executions(days: int = 7) -> int:
    return await get_database().cleanup_tool_executions(days)


async def insert_mcp_audit_log(
    token_scope: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    result_status: str = "success",
    error_message: Optional[str] = None,
) -> int:
    """写入 MCP 审计日志，返回插入 ID。"""
    db = get_database()
    async with db.pool.acquire() as conn:
        row_id = await conn.fetchval(
            """
            INSERT INTO mcp_audit_log (token_scope, tool_name, arguments, result_status, error_message)
            VALUES ($1, $2, $3::jsonb, $4, $5)
            RETURNING id
            """,
            token_scope,
            tool_name,
            json.dumps(arguments, ensure_ascii=False) if arguments else None,
            result_status,
            error_message,
        )
    return int(row_id)


async def get_token_observability_stats(
    start_date, platform: Optional[str] = None
) -> Dict[str, Any]:
    return await get_database().get_token_observability_stats(start_date, platform)


async def list_recent_tool_executions(
    *, limit: int = 50, platform: Optional[str] = None, session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    return await get_database().list_recent_tool_executions(
        limit=limit, platform=platform, session_id=session_id
    )


async def get_recent_image_messages(session_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    return await get_database().get_recent_image_messages(session_id, limit)


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
