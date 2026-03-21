"""
向量存储模块。

封装 ChromaDB 操作，提供长期记忆向量检索功能。
使用智谱 embedding-3 模型生成向量，本地 ChromaDB 存储。
"""

import os
import logging
import json
import time
from typing import List, Dict, Any, Optional, Tuple, Set
from datetime import datetime

import chromadb
from chromadb.config import Settings
import jieba
import requests
from config import config

# 设置日志
logger = logging.getLogger(__name__)

# 未显式传入时的半衰期默认值（天）
_DEFAULT_MEMORY_HALFLIFE_DAYS = 30


def build_daily_summary_doc_id(batch_date: str) -> str:
    """日终主文档 doc_id：`daily_{batch_date}`。"""
    return f"daily_{batch_date}"


def build_daily_event_doc_id(batch_date: str, event_index: int) -> str:
    """日终事件片段 doc_id：`daily_{batch_date}_event_0` 递增。"""
    return f"daily_{batch_date}_event_{event_index}"


def _coerce_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _coerce_float(val: Any, default: float) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _finalize_chroma_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    写入 Chroma 前补齐必选 metadata：base_score、halflife_days、hits、last_access_ts。
    新写入 hits 恒为 0；last_access_ts 为当前 Unix 时间戳（float）。
    """
    out = dict(metadata)
    if out.get("parent_id") is not None:
        out["parent_id"] = str(out["parent_id"])
    if "base_score" in out:
        out["base_score"] = _coerce_float(out["base_score"], 5.0)
    elif "score" in out:
        out["base_score"] = _coerce_float(out["score"], 5.0)
    else:
        out["base_score"] = 5.0
    out["halflife_days"] = _coerce_int(
        out.get("halflife_days"), _DEFAULT_MEMORY_HALFLIFE_DAYS
    )
    out["hits"] = 0
    out["last_access_ts"] = float(time.time())
    return out


def decayed_memory_strength(
    base_score: float,
    halflife_days: int,
    last_access_ts: float,
    now_ts: float,
) -> float:
    """
    半衰期衰减：将 base_score（通常 1–10）归一化到 [0,1] 后按半衰期衰减。
    strength = (base_score/10) * 0.5 ** (elapsed_days / halflife_days)
    """
    b = max(0.0, min(1.0, float(base_score) / 10.0))
    hl = max(1, int(halflife_days))
    elapsed_days = max(0.0, (float(now_ts) - float(last_access_ts)) / 86400.0)
    return b * (0.5 ** (elapsed_days / hl))


class ZhipuEmbedding:
    """
    智谱 AI Embedding 客户端。
    
    调用智谱 embedding-3 模型生成文本向量。
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        初始化智谱 Embedding 客户端。
        
        Args:
            api_key: 智谱 API 密钥，如果为 None 则从配置读取
        """
        self.api_key = api_key or config.ZHIPU_API_KEY
        if not self.api_key:
            raise ValueError("ZHIPU_API_KEY 未设置，请检查 .env 文件")
        
        self.base_url = "https://open.bigmodel.cn/api/paas/v4/embeddings"
        self.model = "embedding-3"
        
        # 设置代理
        self.proxies = config.proxy_dict
        
        logger.info(f"智谱 Embedding 客户端初始化完成，模型: {self.model}")
    
    def get_embedding(self, text: str) -> List[float]:
        """
        获取文本的向量表示。
        
        Args:
            text: 要向量化的文本
            
        Returns:
            List[float]: 文本向量（1024维）
            
        Raises:
            Exception: 如果 API 调用失败
        """
        try:
            # 预处理文本：分词并限制长度
            words = jieba.lcut(text)
            # 限制 token 数量，避免超出 API 限制
            if len(words) > 512:
                words = words[:512]
            processed_text = " ".join(words)
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            # embedding-3 默认 2048 维；与 Chroma / 全库约定一致须显式指定 1024
            data = {
                "model": self.model,
                "input": processed_text,
                "dimensions": 1024,
            }
            
            response = requests.post(
                self.base_url,
                headers=headers,
                json=data,
                proxies=self.proxies,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if "data" in result and len(result["data"]) > 0:
                    embedding = result["data"][0]["embedding"]
                    logger.debug(f"成功获取文本向量，长度: {len(embedding)}")
                    return embedding
                else:
                    raise ValueError(f"API 响应格式错误: {result}")
            else:
                error_msg = f"智谱 Embedding API 调用失败: {response.status_code} - {response.text}"
                logger.error(error_msg)
                raise Exception(error_msg)
                
        except Exception as e:
            logger.error(f"获取文本向量失败: {e}")
            raise
    
    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        批量获取文本向量。
        
        Args:
            texts: 要向量化的文本列表
            
        Returns:
            List[List[float]]: 文本向量列表
            
        Raises:
            Exception: 如果 API 调用失败
        """
        embeddings = []
        for text in texts:
            try:
                embedding = self.get_embedding(text)
                embeddings.append(embedding)
            except Exception as e:
                logger.error(f"批量获取向量失败，文本: {text[:50]}..., 错误: {e}")
                # 返回零向量作为占位
                embeddings.append([0.0] * 1024)
        
        return embeddings


class VectorStore:
    """
    向量存储管理器。
    
    封装 ChromaDB 操作，提供增删改查功能。
    """
    
    def __init__(self, persist_directory: Optional[str] = None):
        """
        初始化向量存储管理器。
        
        Args:
            persist_directory: ChromaDB 持久化目录，如果为 None 则从配置读取
        """
        # 确定持久化目录
        if persist_directory is None:
            persist_directory = config.CHROMADB_PERSIST_DIR
        
        # 确保目录存在
        os.makedirs(persist_directory, exist_ok=True)
        
        # 初始化 ChromaDB 客户端
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True
            )
        )
        
        # 初始化智谱 Embedding 客户端
        self.embedding_client = ZhipuEmbedding()
        
        # 获取或创建集合
        self.collection = self.client.get_or_create_collection(
            name="cedarstar_memories",
            metadata={"description": "CedarStar 长期记忆存储"}
        )
        
        logger.info(f"向量存储管理器初始化完成，目录: {persist_directory}")
    
    def add_memory(self, doc_id: str, text: str, metadata: Dict[str, Any]) -> bool:
        """
        添加记忆到向量数据库。
        
        Args:
            doc_id: 文档ID。日终主文档建议 `daily_{batch_date}`；事件片段建议
                `daily_{batch_date}_event_0`、`_event_1`…
            text: 记忆文本内容
            metadata: 元数据，必须包含 date、session_id、summary_type；
                写入 Chroma 时会自动补齐 base_score(float)、halflife_days(int)、
                hits(0)、last_access_ts(float)。
            
        Returns:
            bool: 是否添加成功
        """
        try:
            # 验证元数据
            required_fields = ["date", "session_id", "summary_type"]
            for field in required_fields:
                if field not in metadata:
                    raise ValueError(f"元数据缺少必要字段: {field}")
            
            # 获取文本向量
            embedding = self.embedding_client.get_embedding(text)
            
            chroma_meta = _finalize_chroma_metadata(metadata)
            chroma_meta["created_at"] = datetime.now().isoformat()
            
            # 添加到 ChromaDB
            self.collection.add(
                ids=[doc_id],
                embeddings=[embedding],
                metadatas=[chroma_meta],
                documents=[text]
            )
            
            logger.info(
                f"记忆添加成功，ID: {doc_id}, 类型: {chroma_meta.get('summary_type')}"
            )
            return True
            
        except Exception as e:
            logger.error(f"添加记忆失败，ID: {doc_id}, 错误: {e}")
            return False

    def update_memory_hits(self, uid_list: List[str]) -> int:
        """
        按 doc_id 批量更新：hits+1，last_access_ts 为当前时间戳。

        使用 collection.get(ids=...) 读取现有 metadata 后 collection.update() 写回，
        不使用 where / metadata 过滤查询。
        """
        if not uid_list:
            return 0
        unique_ids = list(dict.fromkeys(uid_list))
        try:
            got = self.collection.get(ids=unique_ids, include=["metadatas"])
            ids_out = got.get("ids") or []
            metas = got.get("metadatas") or []
            if not ids_out:
                logger.debug("update_memory_hits: 未找到任何匹配的 doc_id")
                return 0
            now_ts = float(time.time())
            upd_ids: List[str] = []
            upd_meta: List[Dict[str, Any]] = []
            for i, uid in enumerate(ids_out):
                md = dict(metas[i] or {})
                prev = _coerce_int(md.get("hits"), 0)
                md["hits"] = prev + 1
                md["last_access_ts"] = now_ts
                upd_ids.append(uid)
                upd_meta.append(md)
            self.collection.update(ids=upd_ids, metadatas=upd_meta)
            found_set = set(ids_out)
            missing = [u for u in unique_ids if u not in found_set]
            if missing:
                logger.warning(
                    "update_memory_hits: 以下 doc_id 不存在，已跳过（最多列 10 个）: %s",
                    missing[:10],
                )
            return len(upd_ids)
        except Exception as e:
            logger.error(f"update_memory_hits 失败: {e}")
            return 0

    def get_metadatas_by_doc_ids(self, doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """按 doc_id 批量读取 Chroma metadata，缺失的 id 不会出现在结果中。"""
        if not doc_ids:
            return {}
        unique_ids = list(dict.fromkeys(doc_ids))
        try:
            got = self.collection.get(ids=unique_ids, include=["metadatas"])
            ids_out = got.get("ids") or []
            metas = got.get("metadatas") or []
            out: Dict[str, Dict[str, Any]] = {}
            for i, uid in enumerate(ids_out):
                out[uid] = dict(metas[i] or {})
            return out
        except Exception as e:
            logger.error(f"get_metadatas_by_doc_ids 失败: {e}")
            return {}
    
    def search_memory(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        搜索相似记忆。
        
        Args:
            query: 查询文本
            top_k: 返回结果数量
            
        Returns:
            List[Dict[str, Any]]: 搜索结果列表，每个结果包含 id、text、metadata、distance
        """
        try:
            # 获取查询向量
            query_embedding = self.embedding_client.get_embedding(query)
            
            # 在 ChromaDB 中搜索
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                include=["metadatas", "documents", "distances"]
            )
            
            # 格式化结果
            formatted_results = []
            if results["ids"] and len(results["ids"]) > 0:
                for i in range(len(results["ids"][0])):
                    doc_id = results["ids"][0][i]
                    text = results["documents"][0][i]
                    metadata = results["metadatas"][0][i]
                    distance = results["distances"][0][i]
                    
                    formatted_results.append({
                        "id": doc_id,
                        "text": text,
                        "metadata": metadata,
                        "distance": distance,
                        "score": 1.0 - distance  # 将距离转换为相似度分数
                    })
            
            logger.debug(f"记忆搜索完成，查询: {query[:50]}..., 结果数量: {len(formatted_results)}")
            return formatted_results
            
        except Exception as e:
            logger.error(f"搜索记忆失败，查询: {query[:50]}..., 错误: {e}")
            return []
    
    def delete_memory(self, doc_id: str) -> bool:
        """
        删除记忆。
        
        Args:
            doc_id: 要删除的文档ID
            
        Returns:
            bool: 是否删除成功
        """
        try:
            self.collection.delete(ids=[doc_id])
            logger.info(f"记忆删除成功，ID: {doc_id}")
            return True
            
        except Exception as e:
            logger.error(f"删除记忆失败，ID: {doc_id}, 错误: {e}")
            return False
    
    def get_memory_count(self) -> int:
        """
        获取记忆总数。
        
        Returns:
            int: 记忆总数
        """
        try:
            count = self.collection.count()
            logger.debug(f"记忆总数: {count}")
            return count
        except Exception as e:
            logger.error(f"获取记忆总数失败: {e}")
            return 0
    
    def clear_all_memories(self) -> bool:
        """
        清空所有记忆。
        
        Returns:
            bool: 是否清空成功
        """
        try:
            self.collection.delete(where={})  # 删除所有文档
            logger.info("所有记忆已清空")
            return True
        except Exception as e:
            logger.error(f"清空记忆失败: {e}")
            return False
    
    def get_all_memories(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        获取所有记忆文档。
        
        用于 BM25 检索器构建索引。
        
        Args:
            limit: 最大返回数量
            
        Returns:
            List[Dict[str, Any]]: 所有记忆文档列表，每个文档包含 id、text、metadata
        """
        try:
            # 使用一个虚拟查询来获取所有文档
            # ChromaDB 没有直接的获取所有文档的 API，我们可以使用一个空向量查询
            # 或者使用一个简单的查询来获取所有文档
            
            # 方法：使用一个零向量进行查询
            zero_embedding = [0.0] * 1024  # embedding-3 模型是 1024 维
            
            results = self.collection.query(
                query_embeddings=[zero_embedding],
                n_results=limit,
                include=["metadatas", "documents"]
            )
            
            # 格式化结果
            memories = []
            if results["ids"] and len(results["ids"]) > 0:
                for i in range(len(results["ids"][0])):
                    doc_id = results["ids"][0][i]
                    text = results["documents"][0][i]
                    metadata = results["metadatas"][0][i]
                    
                    memories.append({
                        "id": doc_id,
                        "text": text,
                        "metadata": metadata
                    })
            
            logger.debug(f"获取所有记忆文档完成，数量: {len(memories)}")
            return memories
            
        except Exception as e:
            logger.error(f"获取所有记忆文档失败: {e}")
            return []

    def garbage_collect_stale_memories(
        self,
        idle_days_threshold: float = 90.0,
        strength_threshold: float = 0.05,
        scan_limit: int = 10000,
    ) -> int:
        """
        回收长期未访问且衰减后强度极低的向量记忆。

        仅当同时满足：距 last_access_ts 已满 idle_days_threshold 天、
        decayed_memory_strength < strength_threshold、且不存在其他文档以本 id 为 parent_id 时，
        才物理删除（collection.delete(ids=...)）。
        """
        now_ts = time.time()
        memories = self.get_all_memories(limit=scan_limit)
        if not memories:
            return 0

        parents_with_children: Set[str] = set()
        for m in memories:
            md = m.get("metadata") or {}
            pid = md.get("parent_id")
            if pid is not None and str(pid).strip() != "":
                parents_with_children.add(str(pid))

        deleted = 0
        for m in memories:
            doc_id = m["id"]
            md = m.get("metadata") or {}
            try:
                last_ts = float(md.get("last_access_ts", 0))
            except (TypeError, ValueError):
                continue
            idle_days = (now_ts - last_ts) / 86400.0
            if idle_days < idle_days_threshold:
                continue
            base = _coerce_float(md.get("base_score"), 5.0)
            hl = _coerce_int(md.get("halflife_days"), _DEFAULT_MEMORY_HALFLIFE_DAYS)
            strength = decayed_memory_strength(base, hl, last_ts, now_ts)
            if strength >= strength_threshold:
                continue
            if doc_id in parents_with_children:
                continue
            if self.delete_memory(doc_id):
                deleted += 1
        if deleted:
            logger.info("Chroma 记忆 GC 完成，删除 %s 条", deleted)
        return deleted


# 全局向量存储实例
_vector_store_instance = None


def get_vector_store() -> VectorStore:
    """
    获取全局向量存储实例。
    
    Returns:
        VectorStore: 向量存储实例
    """
    global _vector_store_instance
    if _vector_store_instance is None:
        _vector_store_instance = VectorStore()
    return _vector_store_instance


def add_memory(doc_id: str, text: str, metadata: Dict[str, Any]) -> bool:
    """
    添加记忆的便捷函数。
    
    Args:
        doc_id: 文档ID
        text: 记忆文本内容
        metadata: 元数据
        
    Returns:
        bool: 是否添加成功
    """
    store = get_vector_store()
    return store.add_memory(doc_id, text, metadata)


def search_memory(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    搜索记忆的便捷函数。
    
    Args:
        query: 查询文本
        top_k: 返回结果数量
        
    Returns:
        List[Dict[str, Any]]: 搜索结果
    """
    store = get_vector_store()
    return store.search_memory(query, top_k)


def delete_memory(doc_id: str) -> bool:
    """
    删除记忆的便捷函数。
    
    Args:
        doc_id: 要删除的文档ID
        
    Returns:
        bool: 是否删除成功
    """
    store = get_vector_store()
    return store.delete_memory(doc_id)


def update_memory_hits(uid_list: List[str]) -> int:
    """对给定 doc_id 列表执行 hits+1 并刷新 last_access_ts，返回成功更新条数。"""
    store = get_vector_store()
    return store.update_memory_hits(uid_list)


def get_memory_metadatas_by_doc_ids(doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量读取长期记忆 Chroma 元数据（hits、halflife_days、last_access_ts 等）。"""
    store = get_vector_store()
    return store.get_metadatas_by_doc_ids(doc_ids)


def garbage_collect_stale_memories(
    idle_days_threshold: float = 90.0,
    strength_threshold: float = 0.05,
    scan_limit: int = 10000,
) -> int:
    store = get_vector_store()
    return store.garbage_collect_stale_memories(
        idle_days_threshold, strength_threshold, scan_limit
    )


def test_vector_store() -> None:
    """
    测试向量存储功能。
    """
    print("测试向量存储功能...")
    
    try:
        # 检查配置
        if not config.ZHIPU_API_KEY or config.ZHIPU_API_KEY == "your_zhipu_api_key_here":
            print("警告: ZHIPU_API_KEY 未设置或为默认值，向量化功能可能无法正常工作")
            print("请在 .env 文件中设置有效的 ZHIPU_API_KEY")
            return
        
        # 初始化向量存储
        store = VectorStore()
        print("向量存储初始化成功")
        
        # 测试添加记忆
        test_doc_id = "test_memory_001"
        test_text = "这是一个测试记忆，用于验证向量存储功能是否正常工作。"
        test_metadata = {
            "date": "2026-03-14",
            "session_id": "test_session",
            "summary_type": "test"
        }
        
        success = store.add_memory(test_doc_id, test_text, test_metadata)
        if success:
            print(f"测试记忆添加成功，ID: {test_doc_id}")
        else:
            print("测试记忆添加失败")
            return
        
        # 测试搜索记忆
        query = "测试记忆验证功能"
        results = store.search_memory(query, top_k=3)
        print(f"记忆搜索完成，查询: '{query}'，找到 {len(results)} 条结果")
        
        if results:
            for i, result in enumerate(results):
                print(f"  结果 {i+1}: ID={result['id']}, 分数={result['score']:.4f}")
                print(f"      文本: {result['text'][:50]}...")
        
        # 测试获取记忆总数
        count = store.get_memory_count()
        print(f"记忆总数: {count}")
        
        # 测试删除记忆
        success = store.delete_memory(test_doc_id)
        if success:
            print(f"测试记忆删除成功，ID: {test_doc_id}")
        
        # 最终计数
        final_count = store.get_memory_count()
        print(f"最终记忆总数: {final_count}")
        
        print("向量存储测试完成！")
        
    except Exception as e:
        print(f"向量存储测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    """向量存储模块测试入口。"""
    import sys
    import os
    
    # 添加项目根目录到 Python 路径
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    test_vector_store()