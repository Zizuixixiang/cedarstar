"""
助手回复中的长期记忆引用：解析 [[used:uid]] 及常见误写、异步更新 Chroma hits、清洗后再存库/发送。
"""

import asyncio
import logging
import re
import unicodedata
from typing import List, Literal, Optional, Set, Tuple

TelegramOrderedSegment = Tuple[Literal["text", "meme", "voice"], str]

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
_VOICE_MARKER_PATTERN = re.compile(r"\[voice\](.*?)\[/voice\]", re.DOTALL)
# 与 `|||` 一起作为顺序分隔：`re.split` 捕获组会保留分隔符
_MEME_OR_TRIPLE_PIPE_SPLIT_RE = re.compile(r"(\[meme:[^\]]*\]|\|\|\|)")
# Telegram 允许的块级标签：仅在闭合块外按换行拆段（单 \\n 与连续空行均切分）
_HTML_BLOCK_TAG_RE = re.compile(
    r"<(/)?\s*(pre|code|blockquote)\b[^>]*>", re.IGNORECASE
)


def _split_by_newline_outside_html_blocks(text: str) -> List[str]:
    """在 HTML 块与 Markdown ``` 代码围栏外按 ``\\n`` 拆行；块内不切。返回非空行。"""
    if not text:
        return []
    parts: List[str] = []
    buf_start = 0
    stack: List[str] = []
    in_code_fence = False
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("```", i):
            in_code_fence = not in_code_fence
            i += 3
            continue
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
        if text[i] == "\n" and not stack and not in_code_fence:
            parts.append(text[buf_start:i])
            buf_start = i + 1
            i += 1
            continue
        i += 1
    parts.append(text[buf_start:])
    return [p for p in parts if (p or "").strip()]


_SENTENCE_END_CHARS = (
    "。",
    "！",
    "？",
    "…",
    "～",
    "~",
    "!",
    "?",
    "♪",
)


def _is_complete_sentence(s: str) -> bool:
    t = (s or "").strip()
    return bool(t) and t.endswith(_SENTENCE_END_CHARS)


# 超长段按句末切分（与 `_is_complete_sentence` 所用集合对齐需求：`。！？…～!?`）
_OVERSIZED_SENTENCE_END_CHARS = frozenset("。！？…～!?")

# 成对符号：仅在「栈空」时允许在句末标点切段，避免把（）、「」、“” 等从中间拆开
_PAIR_OPEN_TO_CLOSE = {
    "（": "）",
    "「": "」",
    "\u201c": "\u201d",  # “ ”
    "\u2018": "\u2019",  # ‘ ’
    "《": "》",
    "【": "】",
    "(": ")",
}


def _pair_stack_update(stack: List[str], c: str) -> None:
    """根据当前字符更新成对符号栈（栈顶为期望出现的闭合符）。"""
    if stack and c == stack[-1]:
        stack.pop()
        return
    if c in _PAIR_OPEN_TO_CLOSE:
        stack.append(_PAIR_OPEN_TO_CLOSE[c])


def _split_oversized_chunk(chunk: str, max_chars: int) -> List[str]:
    """单段超过 ``max_chars`` 时按句末标点切分；标点留在前段末尾。
    句末切分仅在**成对括号/引号已平衡**且**不在 ASCII 双引号成对内**时进行，避免把（）、「」、“” 等从中间拆开。
    若无句末标点或某句仍超长，整段保留不切（宁可单条气泡偏长）。"""
    if max_chars < 1:
        max_chars = 1
    t = (chunk or "").strip()
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    parts: List[str] = []
    buf: List[str] = []
    stack: List[str] = []
    ascii_dq = 0  # ASCII " 成对内为 1，避免 `他说"一句。话"` 在内部句号处误切
    in_code_fence = False

    i = 0
    while i < len(t):
        if t.startswith("```", i):
            in_code_fence = not in_code_fence
            buf.append("```")
            i += 3
            continue
        c = t[i]
        buf.append(c)
        if c == '"':
            ascii_dq ^= 1
        else:
            _pair_stack_update(stack, c)

        if (
            c in _OVERSIZED_SENTENCE_END_CHARS
            and not stack
            and ascii_dq == 0
            and not in_code_fence
        ):
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    if not parts:
        return [t]
    return [x for x in parts if (x or "").strip()]


def _is_punctuation_or_symbol_only_fragment(s: str) -> bool:
    """strip 后除空白外仅含 Unicode 标点/符号（如引号、括号），无字母数字等正文时视为应并入前一片。"""
    t = (s or "").strip()
    if not t:
        return True
    for ch in t:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] not in ("P", "S"):
            return False
    return True


def _collapse_punctuation_only_into_previous(merged: List[str]) -> List[str]:
    """将仅标点/符号的切片用换行并入前一片（首片无法并入则保留）。"""
    out: List[str] = []
    for cur in merged:
        if _is_punctuation_or_symbol_only_fragment(cur) and out:
            out[-1] = out[-1] + "\n" + cur
        else:
            out.append(cur)
    return out


def _merge_short_text_chunks(chunks: List[str], min_chars: int = 15) -> List[str]:
    """strip 后过短的切片与相邻片用换行合并（先与后一片合并，末片过短再并回前一片）。
    以句末标点结尾的短句视为完整句，不合并。
    仅标点/符号的切片先并入前一片（首片无法并入则保留），且不与下一段正文做「过短合并」。"""
    if not chunks:
        return []
    chunks = _collapse_punctuation_only_into_previous(list(chunks))
    i = 0
    merged: List[str] = []
    while i < len(chunks):
        cur = chunks[i]
        if (
            len(cur.strip()) < min_chars
            and i + 1 < len(chunks)
            and not _is_complete_sentence(cur)
            and not _is_punctuation_or_symbol_only_fragment(cur)
            and cur.count("\n") < 2
        ):
            merged.append(cur + "\n" + chunks[i + 1])
            i += 2
        else:
            merged.append(cur)
            i += 1
    if (
        len(merged) >= 2
        and len(merged[-1].strip()) < min_chars
        and not _is_complete_sentence(merged[-1])
        and not _is_punctuation_or_symbol_only_fragment(merged[-1])
    ):
        merged[-2] = merged[-2] + "\n" + merged[-1]
        merged.pop()
    return _collapse_punctuation_only_into_previous(merged)


def _enforce_max_msg_segments(
    segments: List[TelegramOrderedSegment], max_msg: int
) -> List[TelegramOrderedSegment]:
    """总气泡数（含 meme）超过 max_msg 时，优先合并「合并后总长最短」的相邻 text 对；若无相邻 text 对则回退为从后往前合并 text（meme 不删）。"""
    out: List[TelegramOrderedSegment] = list(segments)
    while len(out) > max_msg:
        best_i: Optional[int] = None
        best_len: Optional[int] = None
        for i in range(len(out) - 1):
            if out[i][0] == "text" and out[i + 1][0] == "text":
                a = out[i][1]
                b = out[i + 1][1]
                merged_len = len(a) + len(b) + 1
                if best_len is None or merged_len < best_len:
                    best_len = merged_len
                    best_i = i
        if best_i is not None:
            a = out[best_i][1]
            b = out[best_i + 1][1]
            out[best_i] = ("text", a + "\n" + b)
            del out[best_i + 1]
            continue
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
        out[prev_text_idx] = ("text", a + "\n" + b)
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


async def telegram_max_chars_from_config() -> int:
    """与 ``context_builder._telegram_segment_limits_from_db`` 中 MAX_CHARS 范围一致（10–1000，步长 10）。"""
    db = get_database()
    raw = await db.get_config("telegram_max_chars")
    if raw is None or not str(raw).strip():
        return 50
    try:
        v = int(str(raw).strip())
    except ValueError:
        return 50
    v = max(10, min(1000, round(v / 10) * 10))
    return v


async def parse_telegram_segments_with_memes_async(
    reply_text: str,
) -> Tuple[List[TelegramOrderedSegment], str]:
    """读取 config 表 ``telegram_max_chars`` / ``telegram_max_msg`` 后调用 `parse_telegram_segments_with_memes`。"""
    cleaned = reply_text or ""
    logger.info(f"[segment_debug] cleaned前200: {repr(cleaned[:200])}")
    max_chars = await telegram_max_chars_from_config()
    max_msg = await telegram_max_msg_from_config()
    segments, body_for_db = parse_telegram_segments_with_memes(
        reply_text, max_msg=max_msg, max_chars=max_chars
    )
    logger.info(
        f"[segment_debug] 分段结果 共{len(segments)}段: "
        f"{[(k, repr((c or '')[:50])) for k, c in segments]}"
    )
    return segments, body_for_db


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
    reply_text: str, *, max_msg: int = 8, max_chars: int = 50
) -> Tuple[List[TelegramOrderedSegment], str]:
    """
    一级：将 `|||`、`[meme:描述]` 与 `[voice]...[/voice]` 视为同级顺序分隔符，拆成 (text|meme|voice)* 序列。

    二级（仅当正文中 **不出现** ``|||`` 时执行；``[meme:…]`` 不算「AI 已分段」）：
    对每个 text 段在 `<pre>` / `<code>` / `<blockquote>` 块外按换行（``\\n``）拆行，
    再对超长切片按句末标点切分（`_split_oversized_chunk`：成对 ``（）``「」、“” 等未闭合时不在句末切开），再合并过短段（strip 后 < 15 字），
    最后若总段数超过 ``max_msg`` 则优先均匀合并相邻 text 段。

    若正文中含至少一处 ``|||``（全角 ``｜｜｜`` 会先归一为 ASCII），则 **只** 按一级顺序切分，
    不再做上述二级强行走分割 / ``max_msg`` 合并；发送侧仍会对单条 HTML 做 Telegram 4096 限长处理。
    须在 schedule_update_memory_hits_and_clean_reply 之后调用。

    Returns:
        segments: 按出现顺序的 ``("text", 片段)`` / ``("meme", 描述)`` / ``("voice", 语音内容)``；描述可为空（仍占位，发送时跳过）。
        body_for_db: 仅所有 text 片段按顺序用换行拼接（供 messages 落库，不含 meme/voice 标记）。
    """
    if not isinstance(reply_text, str):
        return [], ""
    raw = reply_text.replace("｜｜｜", "|||")
    if not raw.strip():
        return [], ""

    # 提取 [voice]...[/voice] 标签，替换为占位符
    voice_segments: List[Tuple[int, str]] = []  # (位置, 语音内容)
    def _voice_replacer(m):
        voice_segments.append((m.start(), (m.group(1) or "").strip()))
        return f"\x00VOICE_{len(voice_segments) - 1}\x00"
    raw = _VOICE_MARKER_PATTERN.sub(_voice_replacer, raw)

    # 仅 ||| 视为模型显式分段；仅有 [meme:…] 时仍走强行走分割二级逻辑。
    has_ai_pipe_split = "|||" in raw

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
        if has_ai_pipe_split:
            t = (piece or "").strip()
            if t:
                segments.append(("text", t))
            continue
        sub = _split_by_newline_outside_html_blocks(piece)
        expanded: List[str] = []
        for line in sub:
            expanded.extend(_split_oversized_chunk(line, max_chars))
        sub = _merge_short_text_chunks(expanded, 15)
        for s in sub:
            if (s or "").strip():
                segments.append(("text", s))

    if not has_ai_pipe_split:
        segments = _enforce_max_msg_segments(segments, max_msg)

    # 还原 voice 占位符
    final_segments: List[TelegramOrderedSegment] = []
    for kind, s in segments:
        if kind == "text" and "\x00VOICE_" in s:
            # 包含 voice 占位符，需要拆分
            parts = re.split(r"(\x00VOICE_\d+\x00)", s)
            for part in parts:
                if part is None or part == "":
                    continue
                vm = re.match(r"\x00VOICE_(\d+)\x00", part)
                if vm:
                    idx = int(vm.group(1))
                    if idx < len(voice_segments):
                        final_segments.append(("voice", voice_segments[idx][1]))
                else:
                    t = part.strip()
                    if t:
                        final_segments.append(("text", t))
        else:
            final_segments.append((kind, s))

    segments = final_segments

    text_lines: List[str] = []
    for kind, s in segments:
        if kind == "text" and (s or "").strip():
            text_lines.append((s or "").strip())
        elif kind == "voice" and (s or "").strip():
            text_lines.append(f"[语音]{s}")
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
