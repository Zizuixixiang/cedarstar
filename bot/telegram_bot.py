"""
Telegram 机器人模块。

接收 Telegram 消息，调用 LLM 接口生成回复，并发送回 Telegram。
复用现有的消息缓冲逻辑，与 Discord 实现解耦。
"""

import os
import sys
import io
import time
import asyncio
import base64
import logging
import threading
import requests
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

# 添加当前目录到 Python 路径，确保可以导入本地模块
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    Update,
)
from telegram.error import NetworkError as TelegramNetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from bot.message_buffer import MessageBuffer, ordered_media_type_from_buffer
from bot.markdown_telegram_html import (
    markdown_to_telegram_safe_html,
    prefix_safe_html_by_max_len,
    split_safe_html_telegram_chunks,
)
from bot.telegram_html_sanitize import split_body_into_html_chunks
from bot.reply_citations import schedule_update_memory_hits_and_clean_reply
from bot.stt_client import TRANSCRIBE_FAIL_USER_CONTENT, transcribe_voice
from bot.vision_caption import schedule_generate_image_caption
from config import config, validate_config, Platform
from llm.llm_interface import LLMInterface, build_user_multimodal_content
from memory.database import (
    VISION_FAIL_CAPTION_SHORT,
    VISION_FAIL_CAPTION_TIMEOUT,
    get_assistant_content_for_platform_message_id,
    get_database,
    save_message,
)
from memory.micro_batch import trigger_micro_batch_check
from memory.context_builder import build_context


# 设置日志
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


_TELEGRAM_THINK_PLACEHOLDER = "…"
_TELEGRAM_STREAM_GENERIC_ERROR = "抱歉，生成回复时出错了，请稍后再试。"


def _normalize_telegram_reply_segment_markers(text: str) -> str:
    """全角竖线 ｜｜｜ (U+FF5C) 归一为 ASCII |||，避免模型用错符号导致无法分段。"""
    if not text:
        return ""
    return text.replace("｜｜｜", "|||")


def _split_telegram_body_parts(text: str) -> List[str]:
    """按 ||| 切正文段（先归一化全角分隔符）。仅用于 Telegram 缓冲回复。"""
    norm = _normalize_telegram_reply_segment_markers(text or "")
    return [p.strip() for p in norm.split("|||") if p.strip()]


class _BufferGenResult(NamedTuple):
    """缓冲生成结果：是否已落库用户行决定是否再写助手行。"""

    reply: str
    character_id: Optional[str]
    persist_assistant: bool
    thinking: Optional[str] = None
    assistant_message_id: Optional[str] = None


class _TelegramStreamOutcome(NamedTuple):
    """缓冲流式生成结束状态（用户行是否落库、正文入库串、首条正文 Telegram message_id）。"""

    body_for_db: str
    assistant_message_id: Optional[str]
    thinking: Optional[str]
    save_user: bool


def _telegram_reaction_emoji_label(rt: Any) -> Optional[str]:
    if isinstance(rt, ReactionTypeEmoji):
        return rt.emoji
    if isinstance(rt, ReactionTypeCustomEmoji):
        return rt.custom_emoji_id
    return None


def _character_id_for_reaction_save() -> str:
    """激活 chat 行 `persona_id`；否则 `DEFAULT_CHARACTER_ID`（不经 LLMInterface）。"""
    cfg = get_database().get_active_api_config("chat")
    if cfg:
        pid = cfg.get("persona_id")
        if pid is not None:
            s = str(pid).strip()
            if s and s.lower() != "none":
                return s
    return config.DEFAULT_CHARACTER_ID


def _telegram_user_content_error_fallback_is_summarized(content: str) -> int:
    """语音/贴纸等占位正文落库时置 1，避免计入微批未摘要条数。"""
    c = (content or "").strip()
    if not c:
        return 0
    if TRANSCRIBE_FAIL_USER_CONTENT in c:
        return 1
    if "[贴纸]" in c and "（贴纸）" in c:
        return 1
    if VISION_FAIL_CAPTION_SHORT in c or VISION_FAIL_CAPTION_TIMEOUT in c:
        return 1
    return 0


# Telegram 贴纸：同 file_unique_id 并发解析去重（等待方最多轮询库 3 秒）
processing_stickers: Set[str] = set()
_sticker_coord_lock = asyncio.Lock()

# /rescanpic：等待下一条贴纸以删缓存并重跑 vision；60s 超时或非贴纸消息则取消
pending_rescan: Set[str] = set()
_rescan_timeout_tasks: Dict[str, asyncio.Task] = {}


def _cancel_rescan_timeout_task(session_id: str) -> None:
    t = _rescan_timeout_tasks.pop(session_id, None)
    if t and not t.done():
        t.cancel()


async def _schedule_rescan_timeout(bot, session_id: str, chat_id: int) -> None:
    _cancel_rescan_timeout_task(session_id)

    async def _run() -> None:
        try:
            await asyncio.sleep(60.0)
            if session_id in pending_rescan:
                pending_rescan.discard(session_id)
                try:
                    await bot.send_message(chat_id=chat_id, text="已取消")
                except Exception as e:
                    logger.warning("贴纸重扫超时回复失败: %s", e)
        finally:
            _rescan_timeout_tasks.pop(session_id, None)

    _rescan_timeout_tasks[session_id] = asyncio.create_task(_run())


def _sync_describe_sticker_vision(b64: str, mime_type: str) -> str:
    """同步调用 vision 配置，供 asyncio.to_thread 使用。"""
    prompt = (
        "请用40字以内描述这张贴纸的含义和情绪，\n"
        "如果图片中有文字请原样引用，不要描述技术细节"
    )
    llm = LLMInterface(config_type="vision")
    imgs = [{"type": "image", "data": b64, "mime_type": mime_type}]
    content = build_user_multimodal_content(
        llm.api_base, llm.model_name, prompt, imgs
    )
    out, _ = llm.generate_with_context_and_tracking(
        [{"role": "user", "content": content}], platform=Platform.TELEGRAM
    )
    t = (out or "").strip()
    if len(t) > 160:
        t = t[:160]
    return t


def _sticker_mime_from_path(file_path: str) -> str:
    p = (file_path or "").lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    return "image/webp"


class TelegramBot:
    """
    Telegram 机器人类。
    
    处理 Telegram 事件，调用 LLM 生成回复。
    复用现有的消息缓冲逻辑，与平台对象解耦。
    """
    
    def __init__(self):
        """
        初始化 Telegram 机器人。
        """
        # 注意：LLMInterface 不在这里固化，而是每次请求时动态创建，以支持热更新
        self._message_buffer = MessageBuffer(
            flush_callback=self._flush_buffered_messages,
            log=logger,
        )
        
        logger.info("Telegram 机器人初始化完成")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /start 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        welcome_text = (
            "🤖 欢迎使用 CedarStar AI 助手！\n\n"
            "我是一个智能助手，可以与你进行对话。\n"
            "直接发送消息即可开始聊天。\n\n"
            "可用命令：\n"
            "/start - 显示此欢迎信息\n"
            "/help - 显示帮助信息\n"
            "/model - 显示当前模型信息\n"
            "/clear - 清除当前对话历史\n"
            "/rescanpic - 重新识别贴纸图片\n"
        )
        await update.message.reply_text(welcome_text)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /help 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        help_text = (
            "🤖 CedarStar Telegram 机器人帮助\n\n"
            "基本用法：\n"
            "- 直接发送消息，我会回复你\n\n"
            "可用命令：\n"
            "/start - 显示欢迎信息\n"
            "/help - 显示此帮助信息\n"
            "/model - 显示当前模型信息\n"
            "/clear - 清除当前对话历史\n"
            "/rescanpic - 重新识别贴纸图片\n\n"
            "注意事项：\n"
            "- 对话历史会保存在数据库中\n"
            "- 回复可能因模型配置而有所不同"
        )
        await update.message.reply_text(help_text)
    
    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /model 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        # 动态创建以读取最新激活配置
        current_llm = LLMInterface()
        model_info = (
            f"🤖 当前模型: {current_llm.model_name}\n"
            f"📊 最大 token: {current_llm.max_tokens}\n"
            f"🌡️ 温度: {current_llm.temperature}\n"
            f"⏱️ 超时: {current_llm.timeout}秒"
        )
        await update.message.reply_text(model_info)
    
    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /clear 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        # 获取会话ID
        chat_id = update.effective_chat.id
        session_id = f"telegram_{chat_id}"
        
        # 清除对话历史（在数据库中标记为已摘要）
        from memory.database import get_database
        db = get_database()
        db.clear_session_messages(session_id)
        
        await update.message.reply_text("✅ 对话历史已清除")

    async def rescanpic_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """下一条贴纸将删除 sticker_cache 后重新走视觉识别。"""
        if not update.message:
            return
        chat_id = update.effective_chat.id
        session_id = f"telegram_{chat_id}"
        await update.message.reply_text("好的，请发送需要重新识别的贴纸")
        pending_rescan.add(session_id)
        await _schedule_rescan_timeout(context.bot, session_id, chat_id)

    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    # 语音：Bot 侧拒绝下载超过 50MB（Telegram 侧上限）；下载后超过 25MB 再拦截以符合 Whisper API 限制
    MAX_VOICE_DOWNLOAD_BYTES = 50 * 1024 * 1024
    WHISPER_MAX_VOICE_BYTES = 25 * 1024 * 1024
    MAX_STICKER_BYTES = 10 * 1024 * 1024

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理文本或图片消息。
        
        使用消息缓冲逻辑：收到消息后等待 buffer_delay 配置的时间（默认5秒），
        期间如果同一 session 有新消息进来就重置计时器，
        超时后才将缓冲区内所有消息合并成一条处理。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        if not update.message:
            return
        message = update.message
        session_id = f"telegram_{message.chat.id}"
        if session_id in pending_rescan and not message.sticker:
            _cancel_rescan_timeout_task(session_id)
            pending_rescan.discard(session_id)
            await message.reply_text("未检测到贴纸，已取消")
        if message.voice:
            await self._handle_voice_message(update, context, message)
            return
        if message.sticker:
            await self._handle_sticker_message(update, context, message)
            return
        if message.photo:
            await self._handle_photo_message(update, context, message)
            return
        if not message.text:
            return
        
        # 获取消息信息（纯文本）
        chat_id = message.chat.id
        user_id = message.from_user.id
        message_id = message.message_id
        content = message.text

        logger.info(f"收到 Telegram 消息: chat_id={chat_id}, user_id={user_id}, 内容长度={len(content)}")
        
        # 将消息添加到缓冲区
        await self._add_to_buffer(update, context, session_id, message, content, user_id, message_id)

    async def _handle_photo_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message,
    ) -> None:
        """处理带图消息：取最大尺寸、校验大小、下载转 Base64 后入缓冲。"""
        chat_id = message.chat.id
        user_id = message.from_user.id
        message_id = message.message_id
        session_id = f"telegram_{chat_id}"
        self._message_buffer.begin_heavy(session_id)
        photo = message.photo[-1]
        caption = (message.caption or "").strip()

        try:
            if photo.file_size and photo.file_size > self.MAX_IMAGE_BYTES:
                skip = "[发送了1张图片（文件过大，已跳过视觉解析）]"
                await self._add_to_buffer(
                    update, context, session_id, message, skip, user_id, message_id
                )
                return

            try:
                tg_file = await context.bot.get_file(photo.file_id)
                try:
                    data = await tg_file.download_as_bytearray()
                except AttributeError:
                    bio = io.BytesIO()
                    await tg_file.download_to_memory(bio)
                    data = bio.getvalue()
                b64 = base64.b64encode(bytes(data)).decode("ascii")
                path = (tg_file.file_path or "").lower()
                mime = "image/jpeg"
                if path.endswith(".png"):
                    mime = "image/png"
                elif path.endswith(".webp"):
                    mime = "image/webp"
                elif path.endswith(".gif"):
                    mime = "image/gif"
                image_payload: Dict[str, Any] = {
                    "type": "image",
                    "data": b64,
                    "caption": caption,
                    "mime_type": mime,
                }
                await self._message_buffer.add_to_buffer(
                    session_id,
                    {
                        "update": update,
                        "context": context,
                        "message": message,
                        "content": "",
                        "image_payload": image_payload,
                        "user_id": user_id,
                        "message_id": message_id,
                        "timestamp": asyncio.get_event_loop().time(),
                    },
                )
            except Exception as e:
                logger.exception("Telegram 图片下载失败: %s", e)
                skip = "[发送了1张图片（文件过大，已跳过视觉解析）]"
                await self._add_to_buffer(
                    update, context, session_id, message, skip, user_id, message_id
                )
        finally:
            self._message_buffer.end_heavy(session_id)

    async def _handle_voice_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message,
    ) -> None:
        """
        Telegram 语音（Opus .ogg）：入 Buffer 前同步转录。
        与贴纸/图片同轮时由 MessageBuffer heavy 等待减少误拆分；跨轮仍可能单独 flush。
        """
        chat_id = message.chat.id
        user_id = message.from_user.id
        message_id = message.message_id
        session_id = f"telegram_{chat_id}"
        voice = message.voice
        oversized = "[语音] 文件过大，跳过转录"
        fail = TRANSCRIBE_FAIL_USER_CONTENT

        self._message_buffer.begin_heavy(session_id)
        try:
            fs = getattr(voice, "file_size", None) or 0
            if fs and fs > self.MAX_VOICE_DOWNLOAD_BYTES:
                await self._add_to_buffer(
                    update,
                    context,
                    session_id,
                    message,
                    oversized,
                    user_id,
                    message_id,
                    from_voice=True,
                )
                return

            try:
                tg_file = await context.bot.get_file(voice.file_id)
                try:
                    data = await tg_file.download_as_bytearray()
                except AttributeError:
                    bio = io.BytesIO()
                    await tg_file.download_to_memory(bio)
                    data = bio.getvalue()
                raw = bytes(data)
            except Exception as e:
                logger.exception("Telegram 语音下载失败: %s", e)
                await self._add_to_buffer(
                    update,
                    context,
                    session_id,
                    message,
                    fail,
                    user_id,
                    message_id,
                    from_voice=True,
                )
                return

            if len(raw) > self.WHISPER_MAX_VOICE_BYTES:
                await self._add_to_buffer(
                    update,
                    context,
                    session_id,
                    message,
                    oversized,
                    user_id,
                    message_id,
                    from_voice=True,
                )
                return

            try:
                text = await transcribe_voice(raw, mime_type="audio/ogg")
                content = f"[语音] {text}"
            except Exception as e:
                logger.warning("Telegram 语音转录失败: %s", e)
                content = fail

            await self._add_to_buffer(
                update,
                context,
                session_id,
                message,
                content,
                user_id,
                message_id,
                from_voice=True,
            )
        finally:
            self._message_buffer.end_heavy(session_id)

    async def _resolve_sticker_description(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        sticker,
    ) -> str:
        """查库 / 并发去重 / 下载 + vision；失败与占位均写入 sticker_cache。"""
        db = get_database()
        fid = sticker.file_unique_id
        emoji = (sticker.emoji or "").strip()
        set_name = (getattr(sticker, "set_name", None) or "").strip() or ""

        fallback = "（贴纸）"

        for _ in range(32):
            row = db.get_sticker_cache(fid)
            if row and (row.get("description") or "").strip():
                return str(row["description"]).strip()

            if fid in processing_stickers:
                t0 = time.monotonic()
                while fid in processing_stickers and time.monotonic() - t0 < 3.0:
                    await asyncio.sleep(0.1)
                    row = db.get_sticker_cache(fid)
                    if row and (row.get("description") or "").strip():
                        return str(row["description"]).strip()
                row = db.get_sticker_cache(fid)
                if row and (row.get("description") or "").strip():
                    return str(row["description"]).strip()
                if fid in processing_stickers:
                    return fallback
                await asyncio.sleep(0.05)
                continue

            async with _sticker_coord_lock:
                row = db.get_sticker_cache(fid)
                if row and (row.get("description") or "").strip():
                    return str(row["description"]).strip()
                if fid in processing_stickers:
                    continue
                processing_stickers.add(fid)

            desc = fallback
            try:
                tg_file = await context.bot.get_file(sticker.file_id)
                path = (tg_file.file_path or "").lower()
                if path.endswith(".tgs") or path.endswith(".webm"):
                    raise ValueError("unsupported sticker format for vision")
                fs = getattr(sticker, "file_size", None) or 0
                if fs and fs > self.MAX_STICKER_BYTES:
                    raise ValueError("sticker file too large")
                try:
                    data = await tg_file.download_as_bytearray()
                except AttributeError:
                    bio = io.BytesIO()
                    await tg_file.download_to_memory(bio)
                    data = bio.getvalue()
                raw = bytes(data)
                if len(raw) > self.MAX_STICKER_BYTES:
                    raise ValueError("sticker file too large")
                mime = _sticker_mime_from_path(tg_file.file_path or "")
                b64 = base64.b64encode(raw).decode("ascii")
                text = await asyncio.to_thread(
                    _sync_describe_sticker_vision, b64, mime
                )
                if text:
                    desc = text
            except Exception as e:
                logger.warning("贴纸视觉解析失败 fid=%s: %s", fid, e)
                desc = fallback
            finally:
                db.save_sticker_cache(fid, emoji, set_name, desc)
                async with _sticker_coord_lock:
                    processing_stickers.discard(fid)
            return desc

        return fallback

    async def _handle_sticker_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message,
    ) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id
        message_id = message.message_id
        session_id = f"telegram_{chat_id}"
        sticker = message.sticker
        fid = sticker.file_unique_id

        self._message_buffer.begin_heavy(session_id)
        try:
            if session_id in pending_rescan:
                db = get_database()
                db.delete_sticker_cache(fid)
                processing_stickers.discard(fid)
                _cancel_rescan_timeout_task(session_id)
                pending_rescan.discard(session_id)
            desc = await self._resolve_sticker_description(context, sticker)
            emoji = (sticker.emoji or "").strip()
            if emoji:
                content = f"[贴纸] {emoji} {desc}"
            else:
                content = f"[贴纸] {desc}"

            await self._add_to_buffer(
                update,
                context,
                session_id,
                message,
                content,
                user_id,
                message_id,
                from_sticker=True,
            )
        finally:
            self._message_buffer.end_heavy(session_id)

    def _split_message(self, message: str, max_length: int = 4096) -> list:
        """
        将长消息分割成多个部分。
        
        Args:
            message: 原始消息
            max_length: 每个部分的最大长度
            
        Returns:
            list: 分割后的消息列表
        """
        if len(message) <= max_length:
            return [message]
        
        parts = []
        while message:
            if len(message) <= max_length:
                parts.append(message)
                break
            
            # 查找最后一个换行符或句号作为分割点
            split_index = max_length
            for i in range(max_length - 1, max_length - 100, -1):
                if i < len(message) and message[i] in ('\n', '。', '.', '!', '?'):
                    split_index = i + 1
                    break
            
            parts.append(message[:split_index])
            message = message[split_index:]
        
        return parts

    @staticmethod
    def _escape_telegram_html(text: str) -> str:
        if not text:
            return ""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _telegram_html_body_chunks(
        self, text: str, max_html_len: int = 4096
    ) -> List[str]:
        """正文：白名单净化后按 Telegram 4096 限长切分。"""
        return split_body_into_html_chunks(text or "", max_html_len)

    @staticmethod
    def _think_display_trunc(think_e: str, max_len: int, trunc_marker: str) -> str:
        if max_len <= 0:
            return ""
        if len(think_e) <= max_len:
            return think_e
        avail = max_len - len(trunc_marker)
        if avail <= 0:
            return trunc_marker[:max_len]
        return think_e[:avail] + trunc_marker

    async def _telegram_safe_edit_text(
        self,
        bot,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
    ) -> None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.debug("Telegram edit_message_text 失败: %s", e)

    def _telegram_thinking_blockquote_html(self, think_plain: str) -> str:
        """思维链定稿：可折叠 blockquote（仅流式结束后的最后一次编辑使用）。"""
        esc = self._escape_telegram_html(think_plain)
        head = "<blockquote expandable>🧠 思维链\n"
        tail = "</blockquote>"
        max_len = 4096
        inner_max = max_len - len(head) - len(tail)
        if inner_max < 1:
            return head + self._escape_telegram_html("…") + tail
        if len(esc) <= inner_max:
            return head + esc + tail
        trunc_m = "…（已截断）"
        esc_t = self._think_display_trunc(esc, inner_max, trunc_m)
        return head + esc_t + tail

    async def _telegram_send_body_segments(
        self, base_message, cleaned_with_separators: str
    ) -> Tuple[str, Optional[str]]:
        """Citation 已清洗的正文（可含 |||）。返回 (入库正文不含 |||, 首条正文 message_id)。"""
        parts = _split_telegram_body_parts(cleaned_with_separators)
        logger.debug("Telegram 正文分段: 非空段数=%s", len(parts))
        body_for_db = "\n".join(parts)
        out_chunks: List[str] = []
        for seg in parts:
            out_chunks.extend(self._telegram_html_body_chunks(seg))
        first_mid: Optional[str] = None
        for i, chunk in enumerate(out_chunks):
            sent = await base_message.reply_text(chunk, parse_mode="HTML")
            if first_mid is None:
                first_mid = str(sent.message_id)
            if i + 1 < len(out_chunks):
                await asyncio.sleep(0.5)
        return body_for_db, first_mid

    async def _telegram_stream_thinking_and_reply(
        self,
        llm: LLMInterface,
        messages: List[Dict[str, Any]],
        base_message,
        bot,
    ) -> _TelegramStreamOutcome:
        chat_id = base_message.chat.id
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def run_stream() -> None:
            co_list: List[str] = []
            th_list: List[str] = []
            try:
                gen = llm.generate_stream(messages, platform=Platform.TELEGRAM)
                while True:
                    try:
                        kind, chunk = next(gen)
                    except StopIteration as e:
                        fin = e.value
                        if not isinstance(fin, dict):
                            fin = {}
                        c_body = fin.get("content")
                        if not isinstance(c_body, str):
                            c_body = ""
                        if not c_body:
                            c_body = "".join(co_list)
                        t_raw = fin.get("thinking")
                        if isinstance(t_raw, str) and t_raw.strip():
                            t_norm: Optional[str] = t_raw.strip()
                        else:
                            merged = "".join(th_list).strip()
                            t_norm = merged if merged else None
                        asyncio.run_coroutine_threadsafe(
                            q.put(
                                (
                                    "done",
                                    {
                                        "content": c_body,
                                        "thinking": t_norm,
                                        "usage": fin.get("usage"),
                                    },
                                )
                            ),
                            loop,
                        ).result(timeout=600)
                        return
                    if kind == "thinking":
                        th_list.append(chunk)
                        asyncio.run_coroutine_threadsafe(
                            q.put(("delta_th", chunk)), loop
                        ).result(timeout=600)
                    else:
                        co_list.append(chunk)
            except Exception as ex:
                logger.exception("Telegram LLM 流式线程异常: %s", ex)
                asyncio.run_coroutine_threadsafe(
                    q.put(("err", ex, "".join(co_list), "".join(th_list))), loop
                ).result(timeout=60)

        stream_thread = threading.Thread(target=run_stream, daemon=True)
        stream_thread.start()

        thinking_parts: List[str] = []
        thinking_msg_id: Optional[int] = None
        last_think_edit = 0.0
        done_payload: Optional[Dict[str, Any]] = None
        err_pack: Optional[Tuple[Any, str, str]] = None

        while True:
            item = await q.get()
            tag = item[0]
            if tag == "delta_th":
                thinking_parts.append(item[1])
                cur = "".join(thinking_parts)
                if thinking_msg_id is None:
                    # 阶段一：纯文本占位，流式全程展开可见（无 parse_mode）
                    sent = await base_message.reply_text(_TELEGRAM_THINK_PLACEHOLDER)
                    thinking_msg_id = sent.message_id
                now = time.monotonic()
                if now - last_think_edit >= 1.0:
                    plain = cur or _TELEGRAM_THINK_PLACEHOLDER
                    if len(plain) > 4096:
                        plain = plain[:4096]
                    await self._telegram_safe_edit_text(
                        bot,
                        chat_id,
                        thinking_msg_id,
                        plain,
                    )
                    last_think_edit = now
            elif tag == "done":
                done_payload = item[1]
                break
            elif tag == "err":
                err_pack = (item[1], item[2], item[3])
                break

        stream_thread.join(timeout=2.0)

        think_from_delta = "".join(thinking_parts).strip()
        if done_payload is not None:
            raw_content = done_payload.get("content") or ""
            if not isinstance(raw_content, str):
                raw_content = str(raw_content)
            t_api = done_payload.get("thinking")
            if isinstance(t_api, str) and t_api.strip():
                think_plain = t_api.strip()
            else:
                think_plain = think_from_delta
            interrupted = False
            thinking_stored: Optional[str] = done_payload.get("thinking")
            if isinstance(thinking_stored, str) and thinking_stored.strip():
                thinking_stored = thinking_stored.strip()
            elif think_from_delta:
                thinking_stored = think_from_delta
            else:
                thinking_stored = None
        elif err_pack is not None:
            _ex, c_partial, t_partial = err_pack
            raw_content = c_partial or ""
            think_plain = think_from_delta or (t_partial or "").strip()
            interrupted = True
            thinking_stored = think_from_delta or ((t_partial or "").strip() or None)
        else:
            raw_content = ""
            think_plain = think_from_delta
            interrupted = False
            thinking_stored = think_from_delta or None

        if interrupted and think_plain and "…（已中断）" not in think_plain:
            think_plain_show = think_plain + "\n…（已中断）"
        else:
            think_plain_show = think_plain

        if thinking_msg_id is not None:
            if think_plain_show.strip():
                # 阶段二：定稿为可折叠 HTML blockquote
                html_th = self._telegram_thinking_blockquote_html(think_plain_show)
                await self._telegram_safe_edit_text(
                    bot, chat_id, thinking_msg_id, html_th, parse_mode="HTML"
                )
            else:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=thinking_msg_id)
                except Exception:
                    pass
                thinking_msg_id = None
        elif think_plain_show.strip():
            html_th = self._telegram_thinking_blockquote_html(think_plain_show)
            sent_th = await base_message.reply_text(html_th, parse_mode="HTML")
            thinking_msg_id = sent_th.message_id

        cleaned = schedule_update_memory_hits_and_clean_reply(raw_content)
        body_parts = _split_telegram_body_parts(cleaned)
        parts_exist = bool(body_parts)
        logger.debug(
            "Telegram 流式结束: 正文段数=%s (按 |||；全角｜｜｜已归一)",
            len(body_parts),
        )
        assistant_message_id: Optional[str] = None
        body_for_db = ""
        if parts_exist:
            body_for_db, assistant_message_id = await self._telegram_send_body_segments(
                base_message, cleaned
            )

        sent_something = bool(parts_exist or think_plain_show.strip())
        if done_payload is not None and not sent_something:
            await base_message.reply_text(
                _TELEGRAM_STREAM_GENERIC_ERROR, parse_mode=None
            )
        if err_pack is not None and not parts_exist and not think_plain.strip():
            await base_message.reply_text(
                _TELEGRAM_STREAM_GENERIC_ERROR, parse_mode=None
            )

        if done_payload is not None:
            save_user = True
        elif err_pack is not None:
            save_user = bool(parts_exist or bool(think_plain.strip()))
        else:
            save_user = False

        return _TelegramStreamOutcome(
            body_for_db=body_for_db,
            assistant_message_id=assistant_message_id,
            thinking=thinking_stored,
            save_user=save_user,
        )

    def _assistant_outgoing_chunks(
        self, reply: str, thinking: Optional[str]
    ) -> List[Tuple[str, Optional[str]]]:
        """
        组装发往 Telegram 的 (text, parse_mode)。
        思维链为定稿形态：expandable blockquote + 转义内文（缓冲主路径见流式两阶段）。
        正文：Markdown → bleach 安全 HTML（见 markdown_telegram_html / telegram_html_sanitize）。
        """
        max_len = 4096
        head = "<blockquote expandable>🧠 思维链\n"
        tail = "</blockquote>\n"
        trunc_m = "…（已截断）"
        reply = reply or ""
        th_raw = (thinking or "").strip()

        if not th_raw:
            chunks = self._telegram_html_body_chunks(reply)
            return [(c, "HTML") for c in chunks] if chunks else []

        think_e = self._escape_telegram_html(th_raw)
        body_full_html = markdown_to_telegram_safe_html(reply)

        def packed(th_part: str, body_html: str) -> str:
            return head + th_part + tail + body_html

        one = packed(think_e, body_full_html)
        if len(one) <= max_len:
            return [(one, "HTML")]

        overhead = len(head) + len(tail) + len(body_full_html)
        max_th = max_len - overhead
        if max_th > 0:
            td = self._think_display_trunc(think_e, max_th, trunc_m)
            w = packed(td, body_full_html)
            if len(w) <= max_len:
                return [(w, "HTML")]

        remaining_body = body_full_html
        out: List[Tuple[str, Optional[str]]] = []
        for max_tl in range(min(len(think_e), max_len), -1, -1):
            td = self._think_display_trunc(think_e, max_tl, trunc_m)
            max_body = max_len - len(head) - len(tail) - len(td)
            if max_body < 1:
                continue
            fb_html, suf = prefix_safe_html_by_max_len(remaining_body, max_body)
            if not fb_html:
                continue
            if td:
                msg = packed(td, fb_html)
                if len(msg) <= max_len:
                    out.append((msg, "HTML"))
                    remaining_body = suf
                    break
            else:
                out.append((fb_html, "HTML"))
                remaining_body = suf
                break

        if not out:
            return [
                (c, "HTML")
                for c in split_safe_html_telegram_chunks(body_full_html, max_len)
            ]

        if remaining_body:
            out.extend(
                (c, "HTML")
                for c in split_safe_html_telegram_chunks(remaining_body, max_len)
            )
        return out

    async def _generate_reply_from_buffer(
        self,
        session_id: str,
        combined_content: str,
        user_id: str,
        chat_id: str,
        message_id: str,
        buffer_messages: List[Dict[str, Any]],
        images: Optional[List[Dict[str, Any]]] = None,
        text_for_llm: Optional[str] = None,
        base_message=None,
        bot=None,
    ) -> _BufferGenResult:
        """从缓冲区合并的消息流式生成回复（思维链 + 正文 ||| 分条），并视结果落库用户消息。"""
        try:
            context = build_context(
                session_id,
                combined_content,
                images=images or None,
                llm_user_text=text_for_llm or None,
                telegram_segment_hint=True,
            )
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            if not messages:
                messages = [{"role": "user", "content": combined_content}]

            llm = LLMInterface()

            logger.info(
                "为缓冲区生成回复（流式）: session_id=%s, context 消息数量=%s",
                session_id,
                len(messages),
            )
            logger.debug("System prompt 长度: %s", len(system_prompt))

            if base_message is None or bot is None:
                return _BufferGenResult(
                    "抱歉，内部错误：缺少消息上下文。", None, False
                )

            outcome = await self._telegram_stream_thinking_and_reply(
                llm, messages, base_message, bot
            )

            has_img = bool(images)
            media_t = ordered_media_type_from_buffer(buffer_messages)
            user_row_id = None
            if outcome.save_user:
                user_row_id = save_message(
                    session_id=session_id,
                    role="user",
                    content=combined_content,
                    user_id=user_id,
                    channel_id=chat_id,
                    message_id=message_id,
                    character_id=llm.character_id,
                    platform=Platform.TELEGRAM,
                    media_type=media_t,
                    image_caption=None,
                    vision_processed=0 if has_img else 1,
                    is_summarized=_telegram_user_content_error_fallback_is_summarized(
                        combined_content
                    ),
                )
                if has_img and user_row_id:
                    schedule_generate_image_caption(
                        user_row_id,
                        images or [],
                        (text_for_llm or "").strip(),
                        platform=Platform.TELEGRAM,
                    )

            asyncio.create_task(trigger_micro_batch_check(session_id))

            persist = bool(outcome.body_for_db.strip())
            return _BufferGenResult(
                outcome.body_for_db,
                llm.character_id,
                persist,
                thinking=outcome.thinking,
                assistant_message_id=outcome.assistant_message_id,
            )

        except ValueError as e:
            logger.error("LLM 配置错误: %s", e)
            return _BufferGenResult(
                "抱歉，LLM 配置有问题，请检查 API 密钥设置。", None, False
            )
        except requests.exceptions.Timeout:
            if images:
                logger.error(
                    "LLM 请求超时（本轮含图片/多模态，可调 LLM_VISION_TIMEOUT / LLM_TIMEOUT）"
                )
                return _BufferGenResult(
                    "抱歉，模型响应超时。带图请求往往比纯文字慢很多；"
                    "若仍失败请在 .env 提高 LLM_VISION_TIMEOUT（默认 180 秒）或 LLM_TIMEOUT。",
                    None,
                    False,
                )
            logger.error(
                "LLM 请求超时（主对话无多模态图片 payload；贴纸等已以文本进上下文；"
                "上下文长或上游慢时可调 LLM_TIMEOUT，默认 60 秒）"
            )
            return _BufferGenResult(
                "抱歉，模型响应超时。若对话上下文很长或上游较慢，"
                "请在 .env 提高 LLM_TIMEOUT（默认 60 秒）。",
                None,
                False,
            )
        except Exception as e:
            logger.error("生成回复时出错: %s", e)
            logger.exception(e)
            return _BufferGenResult("抱歉，生成回复时出错了，请稍后再试。", None, False)

    async def _add_to_buffer(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session_id: str,
        message,
        content: str,
        user_id,
        message_id,
        *,
        from_voice: bool = False,
        from_sticker: bool = False,
    ):
        """
        将消息添加到缓冲区，并启动/重置缓冲定时器。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
            session_id: 会话ID
            message: 消息对象
            content: 消息内容
            user_id: 用户ID
            message_id: 消息ID
        """
        await self._message_buffer.add_to_buffer(
            session_id,
            {
                "update": update,
                "context": context,
                "message": message,
                "content": content,
                "user_id": user_id,
                "message_id": message_id,
                "from_voice": from_voice,
                "from_sticker": from_sticker,
                "timestamp": asyncio.get_event_loop().time(),
            },
        )

    async def _flush_buffered_messages(
        self,
        session_id: str,
        combined_content: str,
        images: List[Dict[str, Any]],
        buffer_messages: List[Dict[str, Any]],
        text_for_llm: str,
    ) -> None:
        """缓冲到期后由 MessageBuffer 调用：typing、流式生成、思维链消息与正文 ||| 分条回复。"""
        base_message = buffer_messages[0]["message"]
        base_context = buffer_messages[0]["context"]
        base_user_id = buffer_messages[0]["user_id"]
        base_message_id = buffer_messages[0]["message_id"]

        try:
            await base_context.bot.send_chat_action(
                chat_id=base_message.chat.id, action="typing"
            )
        except TelegramNetworkError as e:
            logger.warning(
                "send_chat_action 失败（略过「正在输入」提示，仍继续生成）: %s",
                e,
            )

        gen = await self._generate_reply_from_buffer(
            session_id=session_id,
            combined_content=combined_content,
            user_id=base_user_id,
            chat_id=str(base_message.chat.id),
            message_id=base_message_id,
            buffer_messages=buffer_messages,
            images=images,
            text_for_llm=text_for_llm,
            base_message=base_message,
            bot=base_context.bot,
        )
        if gen.assistant_message_id and gen.persist_assistant and gen.reply.strip():
            save_message(
                session_id=session_id,
                role="assistant",
                content=gen.reply,
                user_id=base_user_id,
                channel_id=str(base_message.chat.id),
                message_id=gen.assistant_message_id,
                character_id=gen.character_id,
                platform=Platform.TELEGRAM,
            )
        elif gen.reply and not gen.assistant_message_id:
            await base_message.reply_text(gen.reply, parse_mode=None)
    
    async def _generate_reply(self, session_id: str, content: str, 
                            user_id: str, chat_id: str, message_id: str) -> Optional[str]:
        """
        生成回复消息。
        
        复用现有的消息缓冲逻辑，与平台对象解耦。
        
        Args:
            session_id: 会话ID（格式：telegram_{chat_id}）
            content: 消息内容
            user_id: 用户ID
            chat_id: 聊天ID
            message_id: 消息ID
            
        Returns:
            Optional[str]: 生成的回复，如果生成失败则返回 None
        """
        try:
            # 使用 context builder 构建完整的对话上下文
            context = build_context(session_id, content)
            
            # 提取 system prompt 和 messages
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            
            # 如果没有构建出有效的 messages，使用最小化版本
            if not messages:
                messages = [{"role": "user", "content": content}]
            
            # 每次动态创建 LLMInterface，以读取最新激活配置（支持热更新）
            llm = LLMInterface()
            reply, _thinking = llm.generate_with_context_and_tracking(
                messages, platform=Platform.TELEGRAM
            )
            reply = schedule_update_memory_hits_and_clean_reply(reply)
            
            # 保存用户消息到数据库
            save_message(
                session_id=session_id,
                role="user",
                content=content,
                user_id=user_id,
                channel_id=chat_id,
                message_id=message_id,
                character_id=llm.character_id,
                platform=Platform.TELEGRAM,
                is_summarized=_telegram_user_content_error_fallback_is_summarized(
                    content
                ),
            )
            
            # 保存AI回复到数据库
            save_message(
                session_id=session_id,
                role="assistant",
                content=reply,
                user_id=user_id,
                channel_id=chat_id,
                message_id=f"ai_{message_id}",
                character_id=llm.character_id,
                platform=Platform.TELEGRAM
            )
            
            logger.info(f"为 Telegram 用户 {user_id} 生成回复，context 消息数量: {len(messages)}")
            logger.debug(f"System prompt 长度: {len(system_prompt)}")
            
            # 异步触发微批处理检查
            asyncio.create_task(trigger_micro_batch_check(session_id))
            
            return reply
            
        except ValueError as e:
            logger.error(f"LLM 配置错误: {e}")
            return "抱歉，LLM 配置有问题，请检查 API 密钥设置。"
        except requests.exceptions.Timeout:
            logger.error("LLM 请求超时（可调 LLM_TIMEOUT，默认 60 秒）")
            return (
                "抱歉，模型响应超时。可在 .env 提高 LLM_TIMEOUT（默认 60 秒）。"
            )
        except Exception as e:
            logger.error(f"生成回复时出错: {e}")
            logger.exception(e)  # 记录完整异常堆栈
            return "抱歉，生成回复时出错了，请稍后再试。"

    async def handle_message_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """用户对消息点赞/表情：不入缓冲，直接落库为一条 user + media_type=reaction。"""
        mr = update.message_reaction
        if not mr or not mr.chat:
            return
        new_r = mr.new_reaction or ()
        if not new_r:
            return
        label: Optional[str] = None
        for rt in new_r:
            label = _telegram_reaction_emoji_label(rt)
            if label:
                break
        if not label:
            return
        chat_id = mr.chat.id
        session_id = f"telegram_{chat_id}"
        raw = get_assistant_content_for_platform_message_id(
            session_id, str(mr.message_id)
        )
        if raw:
            summary = raw.strip().replace("\n", " ")[:20]
        else:
            summary = "某条消息"
        content = f"[用户对你的消息「{summary}…」点了 {label}]"
        if mr.user:
            uid = str(mr.user.id)
        elif mr.actor_chat:
            uid = str(mr.actor_chat.id)
        else:
            uid = "unknown"
        try:
            save_message(
                session_id=session_id,
                role="user",
                content=content,
                user_id=uid,
                channel_id=str(chat_id),
                message_id=f"reaction_{update.update_id}",
                character_id=_character_id_for_reaction_save(),
                platform=Platform.TELEGRAM,
                media_type="reaction",
                vision_processed=1,
            )
            asyncio.create_task(trigger_micro_batch_check(session_id))
        except Exception as e:
            logger.error(f"保存反应消息失败: {e}")
    
    async def run_async(self):
        """
        异步运行 Telegram 机器人。
        
        使用 python-telegram-bot v20+ 的异步启动方式：
        app.initialize() + app.start() + app.updater.start_polling()
        确保不阻塞主事件循环。
        
        Raises:
            ValueError: 如果 Telegram 令牌未设置
        """
        try:
            token = config.TELEGRAM_BOT_TOKEN
            if not token:
                logger.warning("TELEGRAM_BOT_TOKEN 未设置，Telegram 机器人将不会启动")
                return
            
            logger.info("启动 Telegram 机器人...")

            # 不显式传 proxy 时 trust_env=False → 直连 api.telegram.org（不受 Discord 写入的
            # HTTP_PROXY 影响）。国内直连常被墙 → initialize 易 Timed out；请在 .env 设
            # TELEGRAM_PROXY（如 http://127.0.0.1:7897）。显式 proxy + trust_env=False 可避免
            # 误用环境变量，又能在需要时走代理。
            def _tg_http_request() -> HTTPXRequest:
                return HTTPXRequest(
                    connect_timeout=25.0,
                    read_timeout=120.0,
                    write_timeout=120.0,
                    proxy=config.TELEGRAM_PROXY,
                    httpx_kwargs={"trust_env": False},
                )

            self.application = (
                Application.builder()
                .token(token)
                .request(_tg_http_request())
                .get_updates_request(_tg_http_request())
                .build()
            )
            
            # 添加命令处理器
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("model", self.model_command))
            self.application.add_handler(CommandHandler("clear", self.clear_command))
            self.application.add_handler(
                CommandHandler("rescanpic", self.rescanpic_command)
            )

            # 添加消息处理器
            self.application.add_handler(
                MessageHandler(
                    (
                        filters.PHOTO
                        | filters.VOICE
                        | filters.TEXT
                        | filters.Sticker.ALL
                    )
                    & ~filters.COMMAND,
                    self.handle_message,
                )
            )
            self.application.add_handler(
                MessageReactionHandler(self.handle_message_reaction)
            )
            
            # 启动机器人（使用 polling 模式）
            logger.info("Telegram 机器人开始 polling...")
            await self.application.initialize()
            _bot_cmds = [
                BotCommand("start", "显示欢迎信息"),
                BotCommand("help", "显示帮助"),
                BotCommand("model", "当前模型信息"),
                BotCommand("clear", "清除对话历史"),
                BotCommand("rescanpic", "重新识别贴纸图片"),
            ]
            # 多 scope 注册：仅 Default 时部分私聊/群聊里 `/` 可能不弹出补全
            for _scope in (
                BotCommandScopeDefault(),
                BotCommandScopeAllPrivateChats(),
                BotCommandScopeAllGroupChats(),
            ):
                await self.application.bot.set_my_commands(_bot_cmds, scope=_scope)
            logger.info(
                "Telegram set_my_commands 已成功执行（5 条命令 × Default/私聊/群聊 三 scope）"
            )
            await self.application.start()
            await self.application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
            )
            
            logger.info("Telegram 机器人已启动（异步 polling 模式）")
            
            # 通知 dashboard 模块：Telegram 已上线
            try:
                from api.dashboard import set_bot_online
                set_bot_online("telegram", True)
                logger.info("已更新 Telegram 在线状态 → True")
            except Exception as e:
                logger.warning(f"更新 Telegram 在线状态失败: {e}")
            
            # 保持运行直到收到停止信号
            stop_event = asyncio.Event()
            await stop_event.wait()
            
        except Exception as e:
            logger.error(f"启动 Telegram 机器人时出错: {e}")
            raise
    
    async def run(self):
        """
        运行 Telegram 机器人（兼容旧接口）。
        
        注意：这个方法会阻塞，建议使用 run_async()。
        """
        try:
            await self.run_async()
            # 保持运行
            await self.application.updater.idle()
        except Exception as e:
            logger.error(f"Telegram 机器人运行失败: {e}")
            raise
    
    async def stop(self):
        """
        停止 Telegram 机器人。
        """
        if hasattr(self, 'application'):
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram 机器人已停止")


async def run_telegram_bot():
    """
    运行 Telegram 机器人的便捷函数。
    
    Returns:
        asyncio.Task: Telegram 机器人任务
    """
    try:
        bot = TelegramBot()
        await bot.run()
    except Exception as e:
        logger.error(f"Telegram 机器人运行失败: {e}")
        raise


def main():
    """
    Telegram 机器人主函数。
    """
    try:
        # 验证配置
        validate_config()
        
        # 创建并运行机器人
        bot = TelegramBot()
        
        # 运行异步主循环
        asyncio.run(bot.run())
        
    except ValueError as e:
        logger.error(f"配置验证失败: {e}")
        print(f"错误: {e}")
        print("请检查 .env 文件中的配置项")
    except Exception as e:
        logger.error(f"机器人运行失败: {e}")
        print(f"错误: {e}")


if __name__ == "__main__":
    """Telegram 机器人模块测试入口。"""
    main()