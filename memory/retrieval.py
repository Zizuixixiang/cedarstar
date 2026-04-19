"""
长期记忆召回辅助：日常白名单与回溯（retrospect）关键词检测。

向量检索与 BM25 共用同一套 summary_type 集合，与 Chroma metadata 对齐。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 回溯类用户消息：命中时在白名单中追加 state_archive
RETROSPECT_KEYWORDS = [
    # 时间回溯
    "以前",
    "之前",
    "当时",
    "那时候",
    "上次",
    "曾经",
    "过去",
    "原来",
    # 变化询问
    "变了",
    "变化",
    "不一样",
    "不同了",
    "不再",
    "不像以前",
    # 对比询问
    "现在和以前",
    "以前不是",
    "之前说过",
    "之前不是",
    # 记忆确认
    "还记得吗",
    "你知道我",
    "我好像",
    "我感觉我变",
]

# 日常召回：不含 state_archive（历史被覆盖片段仅回溯时召出）
LONGTERM_SUMMARY_TYPES_DAILY: List[str] = ["daily", "daily_event", "manual"]

LONGTERM_SUMMARY_TYPES_RETROSPECT: List[str] = [
    "daily",
    "daily_event",
    "manual",
    "state_archive",
]


def is_retrospect_query(user_message: str) -> bool:
    if not user_message:
        return False
    return any(kw in user_message for kw in RETROSPECT_KEYWORDS)


def longterm_allowed_summary_types(user_message: str) -> List[str]:
    """按用户当前消息决定长期记忆允许的 summary_type 列表。"""
    if is_retrospect_query(user_message):
        return list(LONGTERM_SUMMARY_TYPES_RETROSPECT)
    return list(LONGTERM_SUMMARY_TYPES_DAILY)


def chroma_where_longterm_summary_types(user_message: str) -> Dict[str, Any]:
    """Chroma ``where``：按 summary_type 白名单过滤。"""
    types = longterm_allowed_summary_types(user_message)
    return {"summary_type": {"$in": types}}
