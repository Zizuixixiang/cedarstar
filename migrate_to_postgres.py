"""
SQLite → PostgreSQL 数据迁移脚本
只迁移三张配置表：persona_configs、api_configs、config
用法：python migrate_to_postgres.py --sqlite 本地sqlite文件路径
"""
import sys
import asyncio
import sqlite3
import argparse
import logging
from dotenv import load_dotenv

load_dotenv()

import asyncpg
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def migrate(sqlite_path: str):
    if not config.DATABASE_URL:
        logger.error("DATABASE_URL 未配置，请检查 .env")
        sys.exit(1)

    # 连接 SQLite 源库
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    cursor = src.cursor()

    # 连接 PostgreSQL 目标库
    dst = await asyncpg.connect(config.DATABASE_URL)

    try:
        # ── 1. persona_configs ──────────────────────────────────────
        cursor.execute("SELECT * FROM persona_configs")
        rows = cursor.fetchall()
        logger.info(f"persona_configs: {len(rows)} 条")

        for row in rows:
            await dst.execute("""
                INSERT INTO persona_configs (
                    id, name, char_name, char_personality, char_speech_style,
                    user_name, user_body, user_habits, user_likes_dislikes,
                    user_values, user_hobbies, user_taboos, user_nsfw,
                    user_other, system_rules, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name,
                    char_name=EXCLUDED.char_name,
                    char_personality=EXCLUDED.char_personality,
                    char_speech_style=EXCLUDED.char_speech_style,
                    user_name=EXCLUDED.user_name,
                    user_body=EXCLUDED.user_body,
                    user_habits=EXCLUDED.user_habits,
                    user_likes_dislikes=EXCLUDED.user_likes_dislikes,
                    user_values=EXCLUDED.user_values,
                    user_hobbies=EXCLUDED.user_hobbies,
                    user_taboos=EXCLUDED.user_taboos,
                    user_nsfw=EXCLUDED.user_nsfw,
                    user_other=EXCLUDED.user_other,
                    system_rules=EXCLUDED.system_rules,
                    updated_at=EXCLUDED.updated_at
            """,
            row["id"], row["name"], row["char_name"], row["char_personality"],
            row["char_speech_style"], row["user_name"], row["user_body"],
            row["user_habits"], row["user_likes_dislikes"], row["user_values"],
            row["user_hobbies"], row["user_taboos"], row["user_nsfw"],
            row["user_other"], row["system_rules"],
            row["created_at"], row["updated_at"])

        logger.info("persona_configs 迁移完成")

        # ── 2. api_configs ──────────────────────────────────────────
        cursor.execute("SELECT * FROM api_configs")
        rows = cursor.fetchall()
        logger.info(f"api_configs: {len(rows)} 条")

        for row in rows:
            await dst.execute("""
                INSERT INTO api_configs (
                    id, name, api_key, base_url, model, persona_id,
                    is_active, config_type, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name,
                    api_key=EXCLUDED.api_key,
                    base_url=EXCLUDED.base_url,
                    model=EXCLUDED.model,
                    persona_id=EXCLUDED.persona_id,
                    is_active=EXCLUDED.is_active,
                    config_type=EXCLUDED.config_type,
                    updated_at=EXCLUDED.updated_at
            """,
            row["id"], row["name"], row["api_key"], row["base_url"],
            row["model"], row["persona_id"], row["is_active"],
            row["config_type"], row["created_at"], row["updated_at"])

        logger.info("api_configs 迁移完成")

        # ── 3. config ───────────────────────────────────────────────
        cursor.execute("SELECT * FROM config")
        rows = cursor.fetchall()
        logger.info(f"config: {len(rows)} 条")

        for row in rows:
            await dst.execute("""
                INSERT INTO config (key, value, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (key) DO UPDATE SET
                    value=EXCLUDED.value,
                    updated_at=EXCLUDED.updated_at
            """, row["key"], row["value"], row["updated_at"])

        logger.info("config 迁移完成")

        # SERIAL 序列重置（防止新插入 id 冲突）
        await dst.execute("""
            SELECT setval('persona_configs_id_seq',
                COALESCE((SELECT MAX(id) FROM persona_configs), 0))
        """)
        await dst.execute("""
            SELECT setval('api_configs_id_seq',
                COALESCE((SELECT MAX(id) FROM api_configs), 0))
        """)
        logger.info("SERIAL 序列已重置")

    finally:
        src.close()
        await dst.close()

    logger.info("全部迁移完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="SQLite 文件路径")
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite))
