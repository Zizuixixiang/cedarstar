"""
表情包向量集合（ChromaDB `meme_pack`）。与主记忆集合隔离；写入时使用显式 embeddings。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

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
    同步路径（脚本 / Chroma upsert）：仅用 .env，不访问数据库（避免无事件循环处 coroutine 未 await）。
    运行时检索请用 ``await _resolve_embedding_api_async()``。
    """
    base = _DEFAULT_EMBEDDING_BASE.rstrip("/")
    model = _DEFAULT_EMBEDDING_MODEL
    key = (config.SILICONFLOW_API_KEY or "").strip()
    return key, base, model


async def _resolve_embedding_api_async() -> tuple[str, str, str]:
    """
    使用 api_configs 中 config_type=embedding 的激活行提供 base_url / model / api_key；
    api_key 若库内为空则回退 config.SILICONFLOW_API_KEY。
    """
    db = get_database()
    row = await db.get_active_api_config("embedding")
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


async def siliconflow_embed_text_async(text: str) -> List[float]:
    """异步路径：读取库内 Embedding 配置后调用 /v1/embeddings。"""
    key, base, model = await _resolve_embedding_api_async()
    if not key:
        row = await get_database().get_active_api_config("embedding")
        if row is None:
            hint = (
                "当前 PostgreSQL（.env 的 DATABASE_URL）中，没有 "
                "config_type='embedding' 且 is_active=1 的 API 配置。"
                "Embedding 与 Chat/Vision 分开激活：请到 Mini App「设置 → Embedding」"
                "为该类型新增一条配置并点「激活」。"
                "若脚本与线上后端连的不是同一数据库，也会出现此提示。"
            )
        else:
            nm = (row.get("name") or "").strip() or "(未命名)"
            hint = (
                f"已有激活的 Embedding 配置「{nm}」，但 api_key 在库中为空。"
                "请在该条配置上重新填写 API Key 并保存，或在 .env 设置 SILICONFLOW_API_KEY 兜底。"
            )
        logger.warning("表情包向量无法调用 /embeddings：%s", hint)
        raise ValueError("未配置表情包向量：" + hint)
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

    def has_meme_id(self, meme_id: str) -> bool:
        """Chroma 集合中是否已有以 ``meme_id`` 为 id 的文档（与 PG ``meme_pack.id`` 对应）。"""
        mid = str(meme_id).strip()
        if not mid:
            return False
        r = self.collection.get(ids=[mid])
        ids = r.get("ids") if isinstance(r, dict) else None
        return bool(ids)

    @staticmethod
    def _meme_doc_and_metadata(
        meme_id: str,
        name: str,
        url: str,
        is_animated: int,
        document_text: Optional[str],
    ) -> Tuple[str, Dict[str, Any]]:
        doc = (document_text if document_text is not None else name) or ""
        doc = doc.strip()
        if not doc:
            raise ValueError("document_text / name 不能为空")
        meta: Dict[str, Any] = {
            "name": name,
            "description": doc,
            "url": url,
            "is_animated": int(is_animated),
            "sqlite_id": str(meme_id),
        }
        return doc, meta

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
        嵌入请求走 **同步** ``siliconflow_embed_text``（仅 .env ``SILICONFLOW_API_KEY``，无事件循环场景）。
        若在 PostgreSQL 里改过 ``meme_pack.name`` / ``description``，须调用本方法（或跑 ``scripts/resync_meme_chroma.py``）才会与向量检索一致。
        """
        doc, meta = self._meme_doc_and_metadata(
            meme_id, name, url, is_animated, document_text
        )
        emb = siliconflow_embed_text(doc)
        self.collection.upsert(
            ids=[str(meme_id)],
            embeddings=[emb],
            documents=[doc],
            metadatas=[meta],
        )

    async def upsert_meme_async(
        self,
        meme_id: str,
        name: str,
        url: str,
        is_animated: int,
        document_text: Optional[str] = None,
    ) -> None:
        """
        与 ``upsert_meme`` 相同，但嵌入走 ``siliconflow_embed_text_async``：
        使用 ``api_configs`` 中激活的 ``embedding`` 行，key 为空时回退 ``SILICONFLOW_API_KEY``。
        """
        doc, meta = self._meme_doc_and_metadata(
            meme_id, name, url, is_animated, document_text
        )
        emb = await siliconflow_embed_text_async(doc)
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

    async def add_meme_async(
        self,
        meme_id: str,
        name: str,
        url: str,
        is_animated: int,
        document_text: Optional[str] = None,
    ) -> None:
        await self.upsert_meme_async(
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
