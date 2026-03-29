"""
助手回复中的长期记忆引用：解析 [[used:uid]] 及常见误写、异步更新 Chroma hits、清洗后再存库/发送。
"""

import asyncio
import logging
import re
from typing import List, Literal, Set, Tuple

TelegramOrderedSegment = Tuple[Literal["text", "meme"], str]

from bot.logutil import exc_detail
from memory.vector_store import update_memory_hits

logger = logging.getLogger(__name__)

_USED_UID_PATTERN = re.compile(r"\[\[used:(.*?)\]\]")
# 模型常误用全角书名号【】；须同样计 hit 并剥离
_USED_UID_FWC_PATTERN = re.compile(r"【used:([^】]*)】")
# 单括号 [used:uid]（勿与 [[used:…]] 重叠：后者整段先被另一规则去掉）
_USED_UID_SINGLE_RE = re.compile(r"(?<!\[)\[used:([^\]]+)\](?!\])")
_MEME_MARKER_PATTERN = re.compile(r"\[meme:([^\]]*)\]")
# 与 `|||` 一起作为顺序分隔：`re.split` 捕获组会保留分隔符
_MEME_OR_TRIPLE_PIPE_SPLIT_RE = re.compile(r"(\[meme:[^\]]*\]|\|\|\|)")


def collect_used_memory_uids(reply_text: str) -> Set[str]:
    """用正则提取所有 uid，去重（空串不计）。"""
    out: Set[str] = set()
    if not isinstance(reply_text, str):
        return out
    for m in _USED_UID_PATTERN.finditer(reply_text):
        uid = (m.group(1) or "").strip()
        if uid:
            out.add(uid)
    for m in _USED_UID_FWC_PATTERN.finditer(reply_text):
        uid = (m.group(1) or "").strip()
        if uid:
            out.add(uid)
    for m in _USED_UID_SINGLE_RE.finditer(reply_text):
        uid = (m.group(1) or "").strip()
        if uid:
            out.add(uid)
    return out


def strip_used_memory_tags(reply_text: str) -> str:
    """移除 [[used:...]]、【used:...】与单括号 [used:...] 标记。"""
    if not isinstance(reply_text, str):
        return reply_text  # type: ignore[return-value]
    s = re.sub(r"\[\[used:.*?\]\]", "", reply_text)
    s = _USED_UID_FWC_PATTERN.sub("", s)
    s = _USED_UID_SINGLE_RE.sub("", s)
    return s


def parse_telegram_segments_with_memes(reply_text: str) -> Tuple[List[TelegramOrderedSegment], str]:
    """
    将 `|||` 与 `[meme:描述]` 视为同级顺序分隔符，拆成 (text|meme)* 序列。
    须在 schedule_update_memory_hits_and_clean_reply 之后调用。

    Returns:
        segments: 按出现顺序的 ``("text", 片段)`` / ``("meme", 描述)``；描述可为空（仍占位，发送时跳过）。
        body_for_db: 仅所有 text 片段按顺序用换行拼接（供 messages 落库，不含 meme 标记）。
    """
    if not isinstance(reply_text, str):
        return [], ""
    raw = reply_text.replace("｜｜｜", "|||")
    if not raw.strip():
        return [], ""

    parts = _MEME_OR_TRIPLE_PIPE_SPLIT_RE.split(raw)
    segments: List[TelegramOrderedSegment] = []
    for piece in parts:
        if piece is None or piece == "":
            continue
        if piece == "|||":
            continue
        stripped = piece.strip()
        mm = _MEME_MARKER_PATTERN.fullmatch(stripped)
        if mm is not None:
            segments.append(("meme", (mm.group(1) or "").strip()))
            continue
        segments.append(("text", piece))

    text_lines: List[str] = []
    for kind, s in segments:
        if kind == "text" and (s or "").strip():
            text_lines.append((s or "").strip())
    body_for_db = "\n".join(text_lines)
    return segments, body_for_db


def strip_meme_markers_and_queries(reply_text: str) -> Tuple[str, List[str]]:
    """
    按出现顺序提取 [meme:描述]，并从正文移除这些标记（不解析 ||| 顺序）。
    发送侧请优先用 ``parse_telegram_segments_with_memes``。
    """
    if not isinstance(reply_text, str):
        return "", []
    queries: List[str] = []
    for m in _MEME_MARKER_PATTERN.finditer(reply_text):
        q = (m.group(1) or "").strip()
        if q:
            queries.append(q)
    cleaned = _MEME_MARKER_PATTERN.sub("", reply_text)
    return cleaned, queries


def schedule_update_memory_hits_and_clean_reply(reply_text: str) -> str:
    """
    须在已有运行中的 asyncio 事件循环内调用。
    若提取到 uid，则 fire-and-forget 在线程池中执行 update_memory_hits；
    返回去掉引用标记后的正文（用于存库与发送）。
    """
    if not isinstance(reply_text, str):
        return reply_text
    uids = collect_used_memory_uids(reply_text)
    if uids:
        uid_list = list(uids)

        async def _run() -> None:
            try:
                await asyncio.to_thread(update_memory_hits, uid_list)
            except Exception as e:
                logger.warning(
                    "update_memory_hits 异步执行失败: %s", exc_detail(e)
                )

        asyncio.create_task(_run())
    return strip_used_memory_tags(reply_text)
