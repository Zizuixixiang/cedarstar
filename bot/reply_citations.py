"""
助手回复中的长期记忆引用：解析 [[used:uid]] 及常见误写、异步更新 Chroma hits、清洗后再存库/发送。
"""

import asyncio
import logging
import re
from typing import List, Literal, Optional, Set, Tuple

TelegramOrderedSegment = Tuple[Literal["text", "meme"], str]

from bot.logutil import exc_detail
from memory.database import get_database
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
# Telegram 允许的块级标签：仅在闭合块外按 \n\n 拆段
_HTML_BLOCK_TAG_RE = re.compile(
    r"<(/)?\s*(pre|code|blockquote)\b[^>]*>", re.IGNORECASE
)


def _split_by_double_newline_outside_html_blocks(text: str) -> List[str]:
    """在 `<pre>` / `<code>` / `<blockquote>` 闭合块外按 ``\\n\\n`` 拆段；块内不切。"""
    if not text:
        return []
    parts: List[str] = []
    buf_start = 0
    stack: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = _HTML_BLOCK_TAG_RE.match(text, i)
        if m:
            is_close = m.group(1) == "/"
            tag = m.group(2).lower()
            if is_close:
                if stack and stack[-1] == tag:
                    stack.pop()
            else:
                stack.append(tag)
            i = m.end()
            continue
        if i + 1 < n and text[i] == "\n" and text[i + 1] == "\n" and not stack:
            parts.append(text[buf_start:i])
            buf_start = i + 2
            i += 2
            continue
        i += 1
    parts.append(text[buf_start:])
    return parts


def _merge_short_text_chunks(chunks: List[str], min_chars: int = 15) -> List[str]:
    """strip 后过短的切片与相邻片用 ``\\n\\n`` 合并（先与后一片合并，末片过短再并回前一片）。"""
    if not chunks:
        return []
    i = 0
    merged: List[str] = []
    while i < len(chunks):
        cur = chunks[i]
        if len(cur.strip()) < min_chars and i + 1 < len(chunks):
            merged.append(cur + "\n\n" + chunks[i + 1])
            i += 2
        else:
            merged.append(cur)
            i += 1
    if len(merged) >= 2 and len(merged[-1].strip()) < min_chars:
        merged[-2] = merged[-2] + "\n\n" + merged[-1]
        merged.pop()
    return merged


def _enforce_max_msg_segments(
    segments: List[TelegramOrderedSegment], max_msg: int
) -> List[TelegramOrderedSegment]:
    """总气泡数（含 meme）超过 max_msg 时，从后往前将相邻 text 合并，直至 ≤ max_msg。"""
    out: List[TelegramOrderedSegment] = list(segments)
    while len(out) > max_msg:
        last_text_idx: Optional[int] = None
        for idx in range(len(out) - 1, -1, -1):
            if out[idx][0] == "text":
                last_text_idx = idx
                break
        if last_text_idx is None or last_text_idx == 0:
            break
        prev_text_idx: Optional[int] = None
        for j in range(last_text_idx - 1, -1, -1):
            if out[j][0] == "text":
                prev_text_idx = j
                break
        if prev_text_idx is None:
            break
        a = out[prev_text_idx][1]
        b = out[last_text_idx][1]
        out[prev_text_idx] = ("text", a + "\n\n" + b)
        del out[last_text_idx]
    return out


async def telegram_max_msg_from_config() -> int:
    """与 ``context_builder._telegram_segment_limits_from_db`` 中 MAX_MSG 范围一致。"""
    db = get_database()
    raw = await db.get_config("telegram_max_msg")
    if raw is None or not str(raw).strip():
        return 8
    try:
        v = int(str(raw).strip())
    except ValueError:
        return 8
    return max(1, min(20, v))


async def parse_telegram_segments_with_memes_async(
    reply_text: str,
) -> Tuple[List[TelegramOrderedSegment], str]:
    """读取 config 表 ``telegram_max_msg`` 后调用 `parse_telegram_segments_with_memes`。"""
    max_msg = await telegram_max_msg_from_config()
    return parse_telegram_segments_with_memes(reply_text, max_msg=max_msg)


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


def parse_telegram_segments_with_memes(
    reply_text: str, *, max_msg: int = 8
) -> Tuple[List[TelegramOrderedSegment], str]:
    """
    一级：将 `|||` 与 `[meme:描述]` 视为同级顺序分隔符，拆成 (text|meme)* 序列。
    二级：对每个 text 段在 `<pre>` / `<code>` / `<blockquote>` 块外按 ``\\n\\n`` 拆段，
    再合并过短段（strip 后 < 15 字），最后若总段数超过 ``max_msg`` 则从后往前合并 text 段。
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
    primary: List[TelegramOrderedSegment] = []
    for piece in parts:
        if piece is None or piece == "":
            continue
        if piece == "|||":
            continue
        stripped = piece.strip()
        mm = _MEME_MARKER_PATTERN.fullmatch(stripped)
        if mm is not None:
            primary.append(("meme", (mm.group(1) or "").strip()))
            continue
        primary.append(("text", piece))

    segments: List[TelegramOrderedSegment] = []
    for kind, piece in primary:
        if kind == "meme":
            segments.append(("meme", piece))
            continue
        sub = _split_by_double_newline_outside_html_blocks(piece)
        sub = _merge_short_text_chunks(sub, 15)
        for s in sub:
            if (s or "").strip():
                segments.append(("text", s))

    segments = _enforce_max_msg_segments(segments, max_msg)

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
