"""
向量存储模块。

封装 ChromaDB 操作，提供长期记忆向量检索功能。
使用智谱 embedding-3 模型生成向量，本地 ChromaDB 存储。
"""

import os
import logging
import json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

import chromadb
from chromadb.config import Settings
import jieba
import requests
from config import config

# 设置日志
logger = logging.getLogger(__name__)


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
            
            data = {
                "model": self.model,
                "input": processed_text
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
            doc_id: 文档ID，格式如 daily_2026-03-14
            text: 记忆文本内容
            metadata: 元数据，必须包含 date、session_id、summary_type
            
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
            
            # 添加元数据时间戳
            metadata["created_at"] = datetime.now().isoformat()
            
            # 添加到 ChromaDB
            self.collection.add(
                ids=[doc_id],
                embeddings=[embedding],
                metadatas=[metadata],
                documents=[text]
            )
            
            logger.info(f"记忆添加成功，ID: {doc_id}, 类型: {metadata.get('summary_type')}")
            return True
            
        except Exception as e:
            logger.error(f"添加记忆失败，ID: {doc_id}, 错误: {e}")
            return False
    
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