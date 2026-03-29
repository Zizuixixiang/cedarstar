"""
表情包向量集合（ChromaDB `meme_pack`）。与主记忆集合隔离；写入时使用显式 embeddings。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import chromadb
import requests
from chromadb.config import Settings

from config import config
from memory.database import get_database

logger = logging.getLogger(__name__)

MEME_COLLECTION_NAME = "meme_pack"
_DEFAULT_EMBEDDING_BASE = "https://api.siliconflow.cn/v1"
_DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"

_meme_store_singleton: Optional["MemeStore"] = None


def _resolve_embedding_api() -> tuple[str, str, str]:
    """
    仅使用 api_configs 中 config_type=embedding 的激活行提供 base_url / model / api_key；
    api_key 若库内为空则回退 config.SILICONFLOW_API_KEY（.env，优先级低于库内已填 key）。
    """
    db = get_database()
    row = db.get_active_api_config("embedding")
    base = _DEFAULT_EMBEDDING_BASE.rstrip("/")
    model = _DEFAULT_EMBEDDING_MODEL
    key = ""
    if row:
        bu = (row.get("base_url") or "").strip()
        if bu:
            base = bu.rstrip("/")
        m = (row.get("model") or "").strip()
        if m:
            model = m
        key = (row.get("api_key") or "").strip()
    if not key:
        key = (config.SILICONFLOW_API_KEY or "").strip()
    return key, base, model


def siliconflow_embed_text(text: str) -> List[float]:
    """调用 OpenAI 兼容 /v1/embeddings。"""
    key, base, model = _resolve_embedding_api()
    if not key:
        raise ValueError(
            "未配置表情包向量：请在核心设置 → API 配置 → Embedding 填写 API Key 并激活，"
            "或在 .env 设置 SILICONFLOW_API_KEY 作为兜底"
        )
    t = (text or "").strip()
    if not t:
        raise ValueError("向量化文本不能为空")
    url = f"{base}/embeddings"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "CedarStar/import_memes",
        },
        json={"model": model, "input": t},
        timeout=60,
        proxies=config.proxy_dict,
    )
    resp.raise_for_status()
    data = resp.json()
    arr = data.get("data") or []
    if not arr:
        raise ValueError("embeddings 响应无 data")
    emb = arr[0].get("embedding")
    if not isinstance(emb, list):
        raise ValueError("embedding 格式无效")
    return [float(x) for x in emb]


class MemeStore:
    def __init__(self, persist_directory: Optional[str] = None) -> None:
        if persist_directory is None:
            persist_directory = config.CHROMADB_PERSIST_DIR
        os.makedirs(persist_directory, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self.collection = self.client.get_or_create_collection(
            name=MEME_COLLECTION_NAME,
            metadata={"description": "CedarStar 表情包检索"},
        )

    def upsert_meme(
        self,
        meme_id: str,
        name: str,
        url: str,
        is_animated: int,
        document_text: Optional[str] = None,
    ) -> None:
        """
        用当前描述重新算 embedding 并写入 Chroma（同 id 则覆盖）。
        若在 SQLite 里改过 meme_pack.name，须调用本方法（或跑 scripts/resync_meme_chroma.py）才会与向量检索一致。
        """
        doc = (document_text if document_text is not None else name) or ""
        if not doc.strip():
            raise ValueError("document_text / name 不能为空")
        emb = siliconflow_embed_text(doc)
        meta: Dict[str, Any] = {
            "name": name,
            "url": url,
            "is_animated": int(is_animated),
            "sqlite_id": str(meme_id),
        }
        self.collection.upsert(
            ids=[str(meme_id)],
            embeddings=[emb],
            documents=[doc],
            metadatas=[meta],
        )

    def add_meme(
        self,
        meme_id: str,
        name: str,
        url: str,
        is_animated: int,
        document_text: Optional[str] = None,
    ) -> None:
        self.upsert_meme(
            meme_id, name, url, is_animated, document_text=document_text
        )

    def search_by_vector(
        self, vector: List[float], top_k: int = 3
    ) -> List[Dict[str, Any]]:
        n = max(1, int(top_k))
        res = self.collection.query(
            query_embeddings=[vector],
            n_results=n,
            include=["metadatas", "distances"],
        )
        metas = (res.get("metadatas") or [[]])[0]
        out: List[Dict[str, Any]] = []
        for m in metas:
            if not isinstance(m, dict):
                continue
            row = dict(m)
            sid = row.get("sqlite_id")
            try:
                row["id"] = int(sid) if sid is not None else None
            except (TypeError, ValueError):
                row["id"] = sid
            out.append(row)
        return out


def get_meme_store() -> MemeStore:
    global _meme_store_singleton
    if _meme_store_singleton is None:
        _meme_store_singleton = MemeStore()
    return _meme_store_singleton
