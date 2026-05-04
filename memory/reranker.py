"""
Reranker 重排模块（SiliconFlow Qwen3-Reranker-4B 版本）。

使用 SiliconFlow Rerank API 对双路检索结果进行重排序。
支持异步网络调用，超时抛出 RerankFallbackException 由调用方降级处理。
"""

import logging
import asyncio
import aiohttp
import os
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# SiliconFlow Rerank API endpoint
RERANK_API_URL = "https://api.siliconflow.cn/v1/rerank"
RERANK_MODEL = "Qwen/Qwen3-Reranker-4B"


class RerankFallbackException(Exception):
    """Rerank API 调用失败，调用方应降级到旧的 fuse_rerank_with_time_decay 路径。"""
    pass


class Reranker:
    """
    Reranker 重排器类（SiliconFlow 版本）。

    使用 SiliconFlow Qwen3-Reranker-4B API 对检索结果进行重排序。
    """

    def __init__(self, api_key: Optional[str] = None, timeout: float = 3.0):
        self.api_key = api_key or os.getenv("SILICONFLOW_API_KEY", "")
        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未设置，请检查 .env 文件")
        self.timeout = timeout
        logger.info("Reranker 初始化完成，模型: %s, timeout: %.1fs", RERANK_MODEL, self.timeout)

    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        调用 SiliconFlow Rerank API 对文档列表重排序。

        Args:
            query: 查询文本
            documents: 文档文本列表
            top_n: 返回条数（None 则返回全部）

        Returns:
            按 relevance_score 降序排列的列表，每项 {index, relevance_score}

        Raises:
            RerankFallbackException: API 超时或调用失败
        """
        if not documents:
            return []
        if not query or not query.strip():
            raise RerankFallbackException("query 为空，跳过 rerank")

        payload: Dict[str, Any] = {
            "model": RERANK_MODEL,
            "query": query,
            "documents": documents,
            "return_documents": False,
        }
        if top_n is not None:
            payload["top_n"] = min(top_n, len(documents))

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            timeout_obj = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout_obj) as session:
                async with session.post(
                    RERANK_API_URL, json=payload, headers=headers
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RerankFallbackException(
                            f"SiliconFlow Rerank HTTP {resp.status}: {body[:500]}"
                        )
                    result = await resp.json()

            # SiliconFlow rerank response follows Cohere-compatible format:
            # {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
            results = result.get("results", [])
            logger.debug(
                "SiliconFlow Rerank 完成，query=%s, docs=%d, results=%d",
                query[:50], len(documents), len(results),
            )
            return results

        except asyncio.TimeoutError:
            raise RerankFallbackException(
                f"SiliconFlow Rerank 超时 ({self.timeout}s)"
            )
        except RerankFallbackException:
            raise
        except Exception as e:
            raise RerankFallbackException(f"SiliconFlow Rerank 异常: {e}")


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_reranker_instance: Optional[Reranker] = None


async def get_reranker() -> Reranker:
    """获取全局 Reranker 单例。"""
    global _reranker_instance
    if _reranker_instance is None:
        _reranker_instance = Reranker()
    return _reranker_instance


async def rerank(
    query: str,
    candidates: List[Dict[str, Any]],
    top_n: Optional[int] = None,
    timeout: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    便捷 rerank 函数。

    接收候选列表（每项需有 'text' 字段），调用 rerank API，
    将 relevance_score 写回每条候选的 metadata 里，按分数降序返回。

    Args:
        query: 查询文本
        candidates: 候选文档列表，每项需有 'text' 和 'metadata' 字段
        top_n: 返回条数
        timeout: 超时秒数（覆盖默认值）

    Returns:
        按 relevance_score 降序排列的候选列表，每项 metadata 里多了 rerank_score

    Raises:
        RerankFallbackException: API 调用失败
    """
    if not candidates:
        return []

    # 提取文档文本
    docs = []
    valid_indices = []
    for i, c in enumerate(candidates):
        text = (c.get("text") or "").strip()
        if text:
            docs.append(text)
            valid_indices.append(i)

    if not docs:
        return candidates

    # 调用 API
    reranker = await get_reranker()
    if timeout is not None:
        reranker.timeout = timeout
    raw_results = await reranker.rerank(query, docs, top_n=top_n)

    # 映射回候选列表
    result_map = {r["index"]: r["relevance_score"] for r in raw_results}
    for local_idx, global_idx in enumerate(valid_indices):
        score = result_map.get(local_idx, 0.0)
        candidates[global_idx]["rerank_score"] = score
        if "metadata" in candidates[global_idx] and candidates[global_idx]["metadata"]:
            candidates[global_idx]["metadata"]["rerank_score"] = score

    # 按 rerank_score 降序
    candidates.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)

    if top_n is not None:
        return candidates[:top_n]
    return candidates


async def close_reranker():
    """关闭全局 Reranker 实例。"""
    global _reranker_instance
    _reranker_instance = None


# ---------------------------------------------------------------------------
# 测试入口
# ---------------------------------------------------------------------------

async def test_reranker() -> None:
    """测试 SiliconFlow Rerank API。"""
    print("测试 SiliconFlow Reranker...")

    try:
        reranker = Reranker()
        print(f"Reranker 初始化成功，模型: {RERANK_MODEL}")

        test_query = "南杉的记忆系统修改"
        test_docs = [
            "Clio 的模型过去由小克进行了记忆系统修改。",
            "南杉因Cursor中GPT模型将页面清空而沮丧愤怒。",
            "创建一条测试条目，用于验证add_external_chunk功能。",
        ]

        print(f"查询: '{test_query}'")
        print(f"文档数量: {len(test_docs)}")

        results = await reranker.rerank(test_query, test_docs)
        print(f"Rerank 完成，返回 {len(results)} 条结果:")
        for r in results:
            print(f"  index={r['index']}, score={r['relevance_score']:.4f}, doc={test_docs[r['index']][:60]}...")

    except RerankFallbackException as e:
        print(f"Rerank 降级异常: {e}")
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import sys
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(test_reranker())
