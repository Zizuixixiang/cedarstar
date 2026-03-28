"""
将模型输出的 Markdown 转为 Telegram parse_mode=HTML 可用的安全片段：
markdown → HTML → bleach 白名单（strip 非法标签，保留内部文本）。
"""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

import bleach
import markdown

logger = logging.getLogger(__name__)

# Telegram Bot API HTML 不支持 <br>；nl2br 产生的换行在送入 bleach 前转为 \n
_TELEGRAM_HTML_TAGS = frozenset(
    {"b", "i", "u", "s", "code", "pre", "blockquote", "a"}
)
_BR_TAG_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)
_TELEGRAM_HTML_ATTRS = {
    "a": ["href"],
    "blockquote": ["expandable"],
}
_TELEGRAM_PROTOCOLS = frozenset({"http", "https", "mailto", "tg"})


def _plain_text_fallback_html(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _markdown_emphasis_tags_to_telegram(html: str) -> str:
    """Markdown 常输出 strong/em/del/ins/strike，映射到 Telegram 支持的 b/i/s/u。"""
    if not html:
        return ""
    h = html
    h = re.sub(r"<\s*strong\s*>", "<b>", h, flags=re.I)
    h = re.sub(r"<\s*/\s*strong\s*>", "</b>", h, flags=re.I)
    h = re.sub(r"<\s*em\s*>", "<i>", h, flags=re.I)
    h = re.sub(r"<\s*/\s*em\s*>", "</i>", h, flags=re.I)
    h = re.sub(r"<\s*del\s*>", "<s>", h, flags=re.I)
    h = re.sub(r"<\s*/\s*del\s*>", "</s>", h, flags=re.I)
    h = re.sub(r"<\s*strike\s*>", "<s>", h, flags=re.I)
    h = re.sub(r"<\s*/\s*strike\s*>", "</s>", h, flags=re.I)
    h = re.sub(r"<\s*ins\s*>", "<u>", h, flags=re.I)
    h = re.sub(r"<\s*/\s*ins\s*>", "</u>", h, flags=re.I)
    return h


def _html_br_to_newlines(html: str) -> str:
    """Telegram parse_mode=HTML 不接受 <br>，用换行符表现断行。"""
    if not html:
        return ""
    return _BR_TAG_RE.sub("\n", html)


def markdown_to_telegram_safe_html(text: str) -> str:
    """
    Markdown / 混排文本 → Telegram 安全 HTML。
    非法标签由 bleach 剥离，保留内部纯文本；链接仅保留安全协议。
    """
    if text is None:
        return ""
    src = str(text)
    if not src.strip():
        return ""

    try:
        raw_html = markdown.markdown(
            src,
            extensions=["extra", "nl2br", "sane_lists"],
            output_format="html",
        )
    except Exception as e:
        logger.warning("markdown 解析失败，回退为纯文本转义: %s", e)
        return _plain_text_fallback_html(src)

    normalized = _markdown_emphasis_tags_to_telegram(raw_html)
    normalized = _html_br_to_newlines(normalized)
    try:
        cleaned = bleach.clean(
            normalized,
            tags=_TELEGRAM_HTML_TAGS,
            attributes=_TELEGRAM_HTML_ATTRS,
            protocols=_TELEGRAM_PROTOCOLS,
            strip=True,
            strip_comments=True,
        )
    except Exception as e:
        logger.warning("bleach 清洗失败，回退为纯文本转义: %s", e)
        return _plain_text_fallback_html(src)

    return cleaned.strip()


def prefix_safe_html_by_max_len(html: str, max_len: int) -> Tuple[str, str]:
    """在已净化的 HTML 上切前缀，使 len(prefix) <= max_len，尽量在 `>` 后断开。"""
    if max_len < 1:
        return "", html
    if not html:
        return "", ""
    if len(html) <= max_len:
        return html, ""
    window = html[:max_len]
    gt = window.rfind(">")
    if gt > max_len // 12:
        cut = gt + 1
    else:
        cut = max_len
    return html[:cut], html[cut:]


def split_safe_html_telegram_chunks(html: str, max_len: int = 4096) -> List[str]:
    """将已净化 HTML 切成多段，每段不超过 max_len（Telegram 上限）。"""
    if not (html or "").strip():
        return []
    s = html.strip()
    if len(s) <= max_len:
        return [s]
    out: List[str] = []
    rest = s
    guard = 0
    while rest:
        guard += 1
        if guard > len(s) + 20:
            out.append(rest)
            break
        if len(rest) <= max_len:
            out.append(rest)
            break
        window = rest[:max_len]
        gt = window.rfind(">")
        if gt > max_len // 12:
            cut = gt + 1
        else:
            cut = max_len
        if cut < 1:
            cut = min(len(rest), 1)
        piece = rest[:cut]
        out.append(piece)
        rest = rest[cut:]
    return out
