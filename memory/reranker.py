"""
Reranker 重排模块。

使用 Cohere Rerank API 对双路检索结果进行重排序。
支持异步网络调用，使用 cohere.AsyncClient 确保不阻塞事件循环。
"""

import logging
import asyncio
from typing import List, Dict, Any, Optional
import cohere
from config import config

# 设置日志
logger = logging.getLogger(__name__)


class Reranker:
    """
    Reranker 重排器类。
    
    使用 Cohere Rerank API 对检索结果进行重排序。
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        初始化 Reranker。
        
        Args:
            api_key: Cohere API 密钥，如果为 None 则从配置读取
        """
        self.api_key = api_key or config.COHERE_API_KEY
        if not self.api_key:
            raise ValueError("COHERE_API_KEY 未设置，请检查 .env 文件")
        
        # 创建异步客户端
        self.client = cohere.AsyncClient(
            api_key=self.api_key,
            timeout=30,
            max_retries=3
        )
        
        # 模型配置
        self.model = "rerank-multilingual-v3.0"
        
        logger.info(f"Reranker 初始化完成，模型: {self.model}")
    
    async def rerank(self, query: str, candidates: List[Dict[str, Any]], top_n: int = 2) -> List[Dict[str, Any]]:
        """
        对候选文档进行重排序。
        
        接收用户查询和双路检索返回的候选列表（最多 10 条），
        调用 Cohere Rerank API 进行重排序，返回得分最高的 top_n 条。
        
        Args:
            query: 用户查询文本
            candidates: 候选文档列表，每个文档包含 text 字段
            top_n: 返回结果数量
            
        Returns:
            List[Dict[str, Any]]: 重排后的文档列表，包含原始文档信息和 rerank_score
        """
        try:
            # 检查候选列表
            if not candidates:
                logger.debug("候选列表为空，跳过重排序")
                return []
            
            # 提取文档文本
            documents = [candidate.get('text', '') for candidate in candidates]
            
            # 过滤空文档
            valid_docs = []
            valid_candidates = []
            for doc, candidate in zip(documents, candidates):
                if doc and doc.strip():
                    valid_docs.append(doc)
                    valid_candidates.append(candidate)
            
            if not valid_docs:
                logger.debug("没有有效的文档文本，跳过重排序")
                return []
            
            # 调用 Cohere Rerank API
            logger.debug(f"调用 Cohere Rerank API，查询: '{query[:50]}...'，文档数量: {len(valid_docs)}")
            
            response = await self.client.rerank(
                model=self.model,
                query=query,
                documents=valid_docs,
                top_n=min(top_n, len(valid_docs)),
                return_documents=True
            )
            
            # 处理重排结果
            reranked_results = []
            for result in response.results:
                index = result.index
                if 0 <= index < len(valid_candidates):
                    candidate = valid_candidates[index].copy()
                    candidate['rerank_score'] = result.relevance_score
                    candidate['rerank_rank'] = len(reranked_results) + 1
                    reranked_results.append(candidate)
            
            logger.debug(f"Rerank 完成，查询: '{query[:50]}...'，返回 {len(reranked_results)} 条结果")
            return reranked_results
            
        except Exception as e:
            logger.error(f"Rerank 失败: {e}")
            # 如果重排失败，返回原始候选列表的前 top_n 条
            logger.warning(f"Rerank 失败，返回原始候选列表的前 {top_n} 条")
            return candidates[:top_n]
    
    async def close(self):
        """
        关闭 Cohere 客户端连接。
        """
        try:
            await self.client.close()
            logger.debug("Cohere 客户端已关闭")
        except Exception as e:
            logger.error(f"关闭 Cohere 客户端失败: {e}")


# 全局 Reranker 实例
_reranker_instance = None


async def get_reranker() -> Reranker:
    """
    获取全局 Reranker 实例。
    
    Returns:
        Reranker: Reranker 实例
    """
    global _reranker_instance
    if _reranker_instance is None:
        _reranker_instance = Reranker()
    return _reranker_instance


async def rerank(query: str, candidates: List[Dict[str, Any]], top_n: int = 2) -> List[Dict[str, Any]]:
    """
    Rerank 的便捷函数。
    
    Args:
        query: 用户查询文本
        candidates: 候选文档列表
        top_n: 返回结果数量
        
    Returns:
        List[Dict[str, Any]]: 重排后的文档列表
    """
    reranker = await get_reranker()
    return await reranker.rerank(query, candidates, top_n)


async def close_reranker():
    """
    关闭 Reranker 的便捷函数。
    """
    global _reranker_instance
    if _reranker_instance is not None:
        await _reranker_instance.close()
        _reranker_instance = None


async def test_reranker() -> None:
    """
    测试 Reranker 功能。
    """
    print("测试 Reranker 功能...")
    
    try:
        # 检查配置
        if not config.COHERE_API_KEY or config.COHERE_API_KEY == "your_cohere_api_key_here":
            print("警告: COHERE_API_KEY 未设置或为默认值，Rerank 功能可能无法正常工作")
            print("请在 .env 文件中设置有效的 COHERE_API_KEY")
            return
        
        # 初始化 Reranker
        reranker = Reranker()
        print("Reranker 初始化成功")
        
        # 创建测试数据
        test_query = "测试查询：人工智能的发展"
        test_candidates = [
            {
                "id": "test_001",
                "text": "人工智能是计算机科学的一个分支，旨在创造能够执行通常需要人类智能的任务的机器。",
                "metadata": {"date": "2026-03-14", "type": "test"},
                "score": 0.85,
                "retrieval_method": "vector"
            },
            {
                "id": "test_002",
                "text": "机器学习是人工智能的一个子领域，它使计算机能够在没有明确编程的情况下学习。",
                "metadata": {"date": "2026-03-14", "type": "test"},
                "score": 0.78,
                "retrieval_method": "bm25"
            },
            {
                "id": "test_003",
                "text": "深度学习是机器学习的一个分支，它使用神经网络来模拟人脑的工作方式。",
                "metadata": {"date": "2026-03-14", "type": "test"},
                "score": 0.72,
                "retrieval_method": "vector"
            }
        ]
        
        print(f"测试查询: '{test_query}'")
        print(f"候选文档数量: {len(test_candidates)}")
        
        # 测试重排序
        results = await reranker.rerank(test_query, test_candidates, top_n=2)
        
        print(f"Rerank 完成，返回 {len(results)} 条结果:")
        for i, result in enumerate(results):
            print(f"  结果 {i+1}: ID={result['id']}, Rerank分数={result.get('rerank_score', 0.0):.4f}")
            print(f"      文本: {result['text'][:50]}...")
        
        # 关闭客户端
        await reranker.close()
        print("Reranker 测试完成！")
        
    except Exception as e:
        print(f"Reranker 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    """Reranker 模块测试入口。"""
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
    
    # 运行异步测试
    asyncio.run(test_reranker())