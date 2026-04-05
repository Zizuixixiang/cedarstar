"""
SQLite → PostgreSQL 数据迁移脚本
只迁移 api_configs 表
用法：python migrate_to_postgres.py --sqlite 本地sqlite文件路径
"""
import sys
from pathlib import Path

# 独立脚本运行时，部分环境开启 safe_path（sys.flags.safe_path），不会自动把项目根加入 sys.path
_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import asyncio
import sqlite3
import argparse
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import asyncpg
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _sqlite_ts(val):
    """SQLite 常返回 TEXT 时间戳；asyncpg 的 TIMESTAMP 绑定需要 datetime。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(s.replace(" ", "T", 1))


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
            row["config_type"], _sqlite_ts(row["created_at"]),
            _sqlite_ts(row["updated_at"]))

        logger.info("api_configs 迁移完成")

        await dst.execute("""
            SELECT setval('api_configs_id_seq',
                COALESCE((SELECT MAX(id) FROM api_configs), 0))
        """)
        logger.info("api_configs_id_seq 已重置")

    finally:
        src.close()
        await dst.close()

    logger.info("迁移完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="SQLite 文件路径")
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite))
