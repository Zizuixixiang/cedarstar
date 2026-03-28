"""
Telegram parse_mode=HTML 正文：模型 Markdown → HTML（markdown 库）→ bleach 白名单；
非法标签剥离并保留内文；按净化后长度切分 4096。
"""

from __future__ import annotations

from typing import List

from bot.markdown_telegram_html import (
    markdown_to_telegram_safe_html,
    split_safe_html_telegram_chunks,
)


def sanitize_telegram_body_html(text: str) -> str:
    """Markdown / 混排 → Telegram 安全 HTML（与 markdown_telegram_html 一致）。"""
    return markdown_to_telegram_safe_html(text or "")


def split_body_into_html_chunks(raw: str, max_len: int = 4096) -> List[str]:
    """整段先做一次 Markdown + bleach，再按 max_len 切净化后的 HTML（不在 raw 前缀上重复解析）。"""
    raw = raw or ""
    if not raw.strip():
        return []
    full = markdown_to_telegram_safe_html(raw)
    if not (full or "").strip():
        return []
    return split_safe_html_telegram_chunks(full, max_len)
