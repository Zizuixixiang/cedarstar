"""
BM25 关键词检索模块。

使用 jieba 分词 + rank_bm25 实现 BM25 检索，检索数据来源是 ChromaDB 里存的全部记忆文本。
实现缓存机制：模块加载时构建一次索引，提供 refresh_index() 方法更新索引。
"""

import logging
import jieba
from typing import List, Dict, Any, Optional, Tuple
from rank_bm25 import BM25Okapi

# 导入向量存储函数
try:
    from .vector_store import get_vector_store
except ImportError:
    from memory.vector_store import get_vector_store

# 设置日志
logger = logging.getLogger(__name__)


class BM25Retriever:
    """
    BM25 检索器类。
    
    使用 jieba 分词 + rank_bm25 实现 BM25 检索，支持内存缓存索引。
    """
    
    def __init__(self):
        """
        初始化 BM25 检索器。
        
        模块加载时自动构建一次索引。
        """
        self.documents: List[str] = []  # 文档文本列表
        self.doc_metadata: List[Dict[str, Any]] = []  # 文档元数据列表
        self.doc_ids: List[str] = []  # 文档ID列表
        self.bm25: Optional[BM25Okapi] = None
        
        # 初始化时构建索引
        self._build_index()
        
        logger.info("BM25 检索器初始化完成")
    
    def _build_index(self) -> None:
        """
        构建 BM25 索引。

        启动时自动从 ChromaDB 加载全量文档并建立索引。
        若 ChromaDB 为空或连接失败，则优雅降级为空索引，不阻断服务启动。
        """
        try:
            store = get_vector_store()
            memories = store.get_all_memories(limit=1000)

            if not memories:
                logger.info("ChromaDB 中暂无文档，BM25 索引初始化为空（后续可通过 refresh_index() 更新）")
                self.bm25 = None
                return

            for memory in memories:
                self.doc_ids.append(memory["id"])
                self.documents.append(memory["text"])
                self.doc_metadata.append(memory["metadata"])

            tokenized_docs = [self._tokenize(doc) for doc in self.documents]
            self.bm25 = BM25Okapi(tokenized_docs)

            logger.info(f"BM25 索引初始化完成，已从 ChromaDB 加载 {len(self.documents)} 条文档")

        except Exception as e:
            logger.warning(f"BM25 索引初始化失败，降级为空索引（不影响服务启动）: {e}")
            self.documents = []
            self.doc_metadata = []
            self.doc_ids = []
            self.bm25 = None
    
    def refresh_index(self) -> bool:
        """
        刷新 BM25 索引。
        
        重新从 ChromaDB 拉取全量文档并重建索引。
        
        Returns:
            bool: 是否刷新成功
        """
        try:
            logger.info("开始刷新 BM25 索引...")
            
            # 获取向量存储实例
            store = get_vector_store()
            
            # 清空现有数据
            self.documents = []
            self.doc_metadata = []
            self.doc_ids = []
            
            # 从 ChromaDB 获取所有文档
            memories = store.get_all_memories(limit=1000)
            
            if not memories:
                logger.info("ChromaDB 中没有文档，BM25 索引为空")
                self.bm25 = None
                return True
            
            # 提取文档信息
            for memory in memories:
                self.doc_ids.append(memory["id"])
                self.documents.append(memory["text"])
                self.doc_metadata.append(memory["metadata"])
            
            # 分词处理并构建 BM25 索引
            tokenized_docs = [self._tokenize(doc) for doc in self.documents]
            self.bm25 = BM25Okapi(tokenized_docs)
            
            logger.info(f"BM25 索引刷新完成，文档数量: {len(self.documents)}")
            return True
            
        except Exception as e:
            logger.error(f"刷新 BM25 索引失败: {e}")
            return False
    
    def _tokenize(self, text: str) -> List[str]:
        """
        对文本进行分词处理。
        
        Args:
            text: 要分词的文本
            
        Returns:
            List[str]: 分词结果
        """
        # 使用 jieba 分词
        words = jieba.lcut(text)
        # 过滤空字符串和过短的词
        words = [word.strip() for word in words if len(word.strip()) > 1]
        return words
    
    def search_bm25(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        BM25 检索。
        
        对用户输入分词后检索，返回 top k 个结果。
        
        Args:
            query: 查询文本
            top_k: 返回结果数量
            
        Returns:
            List[Dict[str, Any]]: 检索结果列表，每个结果包含 id、text、metadata、score
        """
        try:
            # 检查索引是否已构建
            if self.bm25 is None or len(self.documents) == 0:
                logger.warning("BM25 索引未构建或为空，返回空结果")
                return []
            
            # 对查询进行分词
            tokenized_query = self._tokenize(query)
            
            if not tokenized_query:
                logger.debug("查询分词结果为空，返回空结果")
                return []
            
            # 执行 BM25 检索
            scores = self.bm25.get_scores(tokenized_query)
            
            # 获取 top k 个结果
            top_indices = sorted(
                range(len(scores)),
                key=lambda i: scores[i],
                reverse=True
            )[:top_k]
            
            # 构建结果列表
            results = []
            for idx in top_indices:
                if scores[idx] > 0:  # 只返回分数大于0的结果
                    results.append({
                        "id": self.doc_ids[idx],
                        "text": self.documents[idx],
                        "metadata": self.doc_metadata[idx],
                        "score": float(scores[idx]),
                        "retrieval_method": "bm25"
                    })
            
            logger.debug(f"BM25 检索完成，查询: '{query[:50]}...'，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"BM25 检索失败: {e}")
            return []
    
    def add_document(self, doc_id: str, text: str, metadata: Dict[str, Any]) -> bool:
        """
        添加文档到索引。
        
        用于在内存中维护文档索引，避免每次都从 ChromaDB 重新加载。
        
        Args:
            doc_id: 文档ID
            text: 文档文本
            metadata: 文档元数据
            
        Returns:
            bool: 是否添加成功
        """
        try:
            # 添加到文档列表
            self.documents.append(text)
            self.doc_metadata.append(metadata)
            self.doc_ids.append(doc_id)
            
            # 重新构建 BM25 索引
            if self.documents:
                tokenized_docs = [self._tokenize(doc) for doc in self.documents]
                self.bm25 = BM25Okapi(tokenized_docs)
            
            logger.debug(f"文档添加到 BM25 索引，ID: {doc_id}")
            return True
            
        except Exception as e:
            logger.error(f"添加文档到 BM25 索引失败: {e}")
            return False
    
    def get_document_count(self) -> int:
        """
        获取文档数量。
        
        Returns:
            int: 文档数量
        """
        return len(self.documents)


# 全局 BM25 检索器实例
_bm25_retriever_instance = None


def get_bm25_retriever() -> BM25Retriever:
    """
    获取全局 BM25 检索器实例。
    
    Returns:
        BM25Retriever: BM25 检索器实例
    """
    global _bm25_retriever_instance
    if _bm25_retriever_instance is None:
        _bm25_retriever_instance = BM25Retriever()
    return _bm25_retriever_instance


def refresh_bm25_index() -> bool:
    """
    刷新 BM25 索引的便捷函数。
    
    Returns:
        bool: 是否刷新成功
    """
    retriever = get_bm25_retriever()
    return retriever.refresh_index()


def search_bm25(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    BM25 检索的便捷函数。
    
    Args:
        query: 查询文本
        top_k: 返回结果数量
        
    Returns:
        List[Dict[str, Any]]: 检索结果
    """
    retriever = get_bm25_retriever()
    return retriever.search_bm25(query, top_k)


def add_document_to_bm25(doc_id: str, text: str, metadata: Dict[str, Any]) -> bool:
    """
    添加文档到 BM25 索引的便捷函数。
    
    Args:
        doc_id: 文档ID
        text: 文档文本
        metadata: 文档元数据
        
    Returns:
        bool: 是否添加成功
    """
    retriever = get_bm25_retriever()
    return retriever.add_document(doc_id, text, metadata)


def test_bm25_retriever() -> None:
    """
    测试 BM25 检索器功能。
    """
    print("测试 BM25 检索器功能...")
    
    try:
        # 初始化检索器
        retriever = BM25Retriever()
        print("BM25 检索器初始化成功")
        
        # 测试添加文档
        test_doc_id = "test_bm25_001"
        test_text = "这是一个测试文档，用于验证 BM25 检索功能是否正常工作。"
        test_metadata = {
            "date": "2026-03-14",
            "session_id": "test_session",
            "summary_type": "test"
        }
        
        success = retriever.add_document(test_doc_id, test_text, test_metadata)
        if success:
            print(f"测试文档添加成功，ID: {test_doc_id}")
        else:
            print("测试文档添加失败")
            return
        
        # 测试检索
        query = "测试文档验证功能"
        results = retriever.search_bm25(query, top_k=3)
        print(f"BM25 检索完成，查询: '{query}'，找到 {len(results)} 条结果")
        
        if results:
            for i, result in enumerate(results):
                print(f"  结果 {i+1}: ID={result['id']}, 分数={result['score']:.4f}")
                print(f"      文本: {result['text'][:50]}...")
        
        # 测试获取文档数量
        count = retriever.get_document_count()
        print(f"文档总数: {count}")
        
        # 测试刷新索引
        print("测试刷新索引...")
        success = retriever.refresh_index()
        if success:
            print("索引刷新成功")
        else:
            print("索引刷新失败")
        
        print("BM25 检索器测试完成！")
        
    except Exception as e:
        print(f"BM25 检索器测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    """BM25 检索模块测试入口。"""
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
    
    test_bm25_retriever()