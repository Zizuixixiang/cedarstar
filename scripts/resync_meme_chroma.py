"""
将 SQLite meme_pack 全表按当前 name 重新嵌入并 upsert 到 Chroma meme_pack。

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

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    from memory.database import get_database
    from memory.meme_store import get_meme_store

    rows = get_database().fetch_all_meme_pack()
    if not rows:
        logger.info("meme_pack 表为空，无需同步")
        return 0

    store = get_meme_store()
    ok = 0
    for r in rows:
        rid = r["id"]
        name = (r.get("name") or "").strip()
        url = (r.get("url") or "").strip()
        isa = int(r.get("is_animated") or 0)
        if not name:
            logger.warning("跳过 id=%s：name 为空", rid)
            continue
        try:
            store.upsert_meme(str(rid), name, url, isa, document_text=name)
            ok += 1
            logger.info("OK id=%s %s", rid, name[:50])
        except Exception as e:
            logger.error("id=%s 失败: %s", rid, e)
            return 1

    logger.info("完成：%s / %s 条已写入 Chroma", ok, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
