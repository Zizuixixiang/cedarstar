"""
将 PostgreSQL ``meme_pack`` 全表按 ``description``（非空优先）否则 ``name`` 重新嵌入并 upsert 到 Chroma ``meme_pack``。

用法：在 DB 工具里改完描述后，在项目根目录执行：
  python scripts/resync_meme_chroma.py

依赖与 import_memes 相同：Embedding API（核心设置或 SILICONFLOW_API_KEY）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    from memory.database import initialize_database
    from memory.meme_store import get_meme_store

    db = await initialize_database()
    rows = await db.fetch_all_meme_pack()
    if not rows:
        logger.info("meme_pack 表为空，无需同步")
        return 0

    store = get_meme_store()
    ok = 0
    for r in rows:
        rid = r["id"]
        name = (r.get("name") or "").strip()
        desc = (r.get("description") or "").strip()
        url = (r.get("url") or "").strip()
        isa = int(r.get("is_animated") or 0)
        if not name:
            logger.warning("跳过 id=%s：name 为空", rid)
            continue
        embed_text = desc if desc else name
        try:
            await store.upsert_meme_async(
                str(rid), name, url, isa, document_text=embed_text
            )
            ok += 1
            logger.info("OK id=%s %s", rid, name[:50])
        except Exception as e:
            logger.error("id=%s 失败: %s", rid, e)
            return 1

    logger.info("✅ 全部完成：%s / %s 条已写入 Chroma", ok, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
