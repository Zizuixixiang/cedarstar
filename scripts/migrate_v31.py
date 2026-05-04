"""
v3.1 迁移脚本：历史事件 reembed + metadata 打标。

步骤：
  a) 从 PG longterm_memories 读旧事件（分页，batch=50）
  b) 用 Qwen3-Embedding-8B 重新 embed 旧 summary 文本（原文不变）
  c) 调 LLM 输出 4 字段：theme / entities / emotion / event_type
  d) 写入新 ChromaDB collection（cedarclio_v2）
  e) PG longterm_memories 同步更新 4 字段
  f) 支持断点续跑（metadata 字段为 NULL 判断）
  g) 跑完打印统计

用法：
  python scripts/migrate_v31.py              # 正式跑
  python scripts/migrate_v31.py --dry-run    # 只输出不写库
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

# 添加项目根目录到 Python 路径
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

os.environ.setdefault("DEFAULT_CHARACTER_ID", "1")

from config import config
from memory.vector_store import SiliconFlowEmbedding, _finalize_chroma_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50
NEW_COLLECTION = "cedarclio_v2"

# 4 字段 enum 常量（与 daily_batch.py 保持一致）
EVENT_THEMES = [
    "daily_life", "work_career", "education", "health", "relationship",
    "emotion", "hobby", "travel", "finance", "family", "conflict",
    "milestone", "decision", "other",
]
EVENT_EMOTIONS = [
    "happy", "sad", "angry", "anxious", "excited", "calm", "grateful",
    "nostalgic", "frustrated", "hopeful", "neutral", "other",
]
EVENT_TYPES = [
    "daily_warmth", "decision", "emotional_shift", "milestone",
    "conflict", "routine", "other",
]

TAG_PROMPT_TEMPLATE = """你是一个记忆标注助手。请根据以下事件内容，输出 4 个标签字段。

【事件内容】
{content}

【输出 schema】
{{"theme": "枚举值", "entities": ["实体1", "实体2"], "emotion": "枚举值", "event_type": "枚举值"}}

theme 取值：{themes}
emotion 取值：{emotions}
event_type 取值：{types}

entities: 事件涉及的实体（人名、组织名、产品名等），最多 5 个，必须是有意义的专有名词。禁止填入「今天」「南杉」「东西」等泛指词。无明确实体时返回空数组 []

重要：你只输出这四个字段的 JSON，严禁复述或改写原文，严禁输出其他字段。
只返回 JSON 对象，不要解释、不要 Markdown。"""


def _clamp_enum(value: Any, allowed: List[str], default: str) -> str:
    s = str(value or "").strip().lower()
    return s if s in allowed else default


def _clamp_entities(value: Any, max_items: int = 5) -> List[str]:
    if not isinstance(value, list):
        return []
    seen = set()
    result = []
    for item in value:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        result.append(s)
        if len(result) >= max_items:
            break
    return result


async def fetch_batch(db, offset: int, limit: int) -> List[Dict[str, Any]]:
    """读取一批 longterm_memories（theme IS NULL 表示未迁移）。"""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, chroma_doc_id, score, source_chunk_ids,
                   is_starred, source_date, theme, entities, emotion, event_type
            FROM longterm_memories
            WHERE theme IS NULL
            ORDER BY id
            LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    return [dict(r) for r in rows]


async def count_pending(db) -> int:
    async with db.pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM longterm_memories WHERE theme IS NULL"
        ))


async def count_total(db) -> int:
    async with db.pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM longterm_memories"))


async def update_pg_tags(db, row_id: int, theme: str, entities: List[str],
                         emotion: str, event_type: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE longterm_memories
            SET theme = $1, entities = $2::jsonb, emotion = $3, event_type = $4
            WHERE id = $5
            """,
            theme, json.dumps(entities, ensure_ascii=False), emotion, event_type, row_id,
        )


async def call_llm_for_tags(llm, content: str) -> Dict[str, Any]:
    """调 LLM 获取 4 字段标签。"""
    import re
    prompt = TAG_PROMPT_TEMPLATE.format(
        content=content,
        themes=" / ".join(EVENT_THEMES),
        emotions=" / ".join(EVENT_EMOTIONS),
        types=" / ".join(EVENT_TYPES),
    )
    resp = llm.generate_with_context_and_tracking(
        [{"role": "user", "content": prompt}],
        timeout_override_seconds=120,
    )
    raw = (resp.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise ValueError(f"LLM tag output parse error: {raw[:200]}")
        parsed = json.loads(m.group())
    if not isinstance(parsed, dict):
        raise ValueError("LLM tag output is not a dict")
    return {
        "theme": _clamp_enum(parsed.get("theme"), EVENT_THEMES, "other"),
        "entities": _clamp_entities(parsed.get("entities"), max_items=5),
        "emotion": _clamp_enum(parsed.get("emotion"), EVENT_EMOTIONS, "neutral"),
        "event_type": _clamp_enum(parsed.get("event_type"), EVENT_TYPES, "other"),
    }


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只输出不写库")
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        logger.info("=== DRY RUN 模式 ===")

    from memory.database import get_database
    db = get_database()
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        logger.error("DATABASE_URL 未设置")
        return
    await db.init_pool(dsn)

    # 检查 SILICONFLOW_API_KEY
    sf_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not sf_key and not dry_run:
        logger.error("SILICONFLOW_API_KEY 未设置，无法执行 reembed")
        return

    # 初始化 embedding 客户端
    embedding_client = SiliconFlowEmbedding() if not dry_run else None

    # 初始化 LLM（用于打标签）
    from llm.llm_interface import LLMInterface
    try:
        llm = await LLMInterface.create(config_type="analysis")
    except Exception:
        llm = await LLMInterface.create(config_type="summary")

    # 初始化新 ChromaDB collection
    if not dry_run:
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(
            path=config.CHROMADB_PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        new_collection = client.get_or_create_collection(
            name=NEW_COLLECTION,
            metadata={"description": f"{config.APP_NAME} v3.1 长期记忆存储"},
        )

    total = await count_total(db)
    pending = await count_pending(db)
    logger.info("longterm_memories 总数: %d，待迁移: %d", total, pending)

    if pending == 0:
        logger.info("无需迁移，退出")
        return

    stats = {"success": 0, "failed": 0, "skipped": 0}
    offset = 0

    while True:
        batch = await fetch_batch(db, offset, BATCH_SIZE)
        if not batch:
            break

        logger.info("处理 batch offset=%d, size=%d", offset, len(batch))

        for row in batch:
            row_id = row["id"]
            content = row["content"] or ""
            chroma_doc_id = row["chroma_doc_id"] or ""
            if not content or not chroma_doc_id:
                logger.warning("跳过 id=%d: content 或 chroma_doc_id 为空", row_id)
                stats["skipped"] += 1
                continue

            try:
                # b) reembed
                if not dry_run:
                    embedding = embedding_client.get_embedding(content)
                else:
                    embedding = [0.0] * 1024

                # c) LLM 打标签
                tags = await call_llm_for_tags(llm, content)

                if dry_run:
                    logger.info(
                        "[DRY] id=%d theme=%s emotion=%s event_type=%s entities=%s | content=%s",
                        row_id, tags["theme"], tags["emotion"], tags["event_type"],
                        tags["entities"], content[:80],
                    )
                    stats["success"] += 1
                    continue

                # d) 写入新 ChromaDB collection
                sd = row.get("source_date")
                meta = {
                    "date": sd.isoformat() if sd else "",
                    "session_id": "migrated",
                    "summary_type": "daily_event",
                    "score": int(row.get("score") or 5),
                    "base_score": float(row.get("score") or 5),
                    "halflife_days": 30,
                    "hits": 0,
                    "last_access_ts": float(time.time()),
                    "is_starred": bool(row.get("is_starred")),
                    "arousal": 0.1,
                    "theme": tags["theme"],
                    "entities": "|".join(tags["entities"]) if tags["entities"] else "",
                    "emotion": tags["emotion"],
                    "event_type": tags["event_type"],
                }
                meta = _finalize_chroma_metadata(meta)
                new_collection.add(
                    ids=[chroma_doc_id],
                    embeddings=[embedding],
                    metadatas=[meta],
                    documents=[content],
                )

                # e) PG 更新 4 字段
                await update_pg_tags(
                    db, row_id,
                    tags["theme"], tags["entities"], tags["emotion"], tags["event_type"],
                )

                stats["success"] += 1
                if stats["success"] % 10 == 0:
                    logger.info("进度: %d/%d", stats["success"], pending)

            except Exception as e:
                logger.error("迁移失败 id=%d: %s", row_id, e)
                stats["failed"] += 1

        offset += BATCH_SIZE

    logger.info("=== 迁移完成 ===")
    logger.info("总数: %d, 成功: %d, 失败: %d, 跳过: %d", total, stats["success"], stats["failed"], stats["skipped"])


if __name__ == "__main__":
    asyncio.run(main())
