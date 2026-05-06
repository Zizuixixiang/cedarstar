"""
Telegram 机器人模块。

接收 Telegram 消息，调用 LLM 接口生成回复，并发送回 Telegram。
复用现有的消息缓冲逻辑，与 Discord 实现解耦。
"""

import os
import sys
import io
import copy
import json
import time
import asyncio
import base64
import logging
import traceback
import threading
import requests
import uuid
import random
import re
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
    telegram_send_text_collapse,
)
from bot.telegram_html_sanitize import split_body_into_html_chunks
from bot.logutil import exc_detail
from bot.reply_citations import (
    parse_telegram_segments_with_memes_async,
    schedule_update_memory_hits_and_clean_reply,
)
from bot.stt_client import TRANSCRIBE_FAIL_USER_CONTENT, transcribe_voice
from bot.vision_caption import schedule_generate_image_caption
from config import config, validate_config, Platform
from llm.llm_interface import (
    LLMInterface,
    TELEGRAM_GUARD_PROMPT_APPEND,
    append_guard_hint_to_last_user_message,
    build_user_multimodal_content,
    complete_with_lutopia_tool_loop,
    output_guard_blocks_model_text,
    split_thinking_and_content,
    truncate_accumulator_at_first_refusal,
)
from memory.database import (
    VISION_FAIL_CAPTION_SHORT,
    VISION_FAIL_CAPTION_TIMEOUT,
    get_assistant_content_for_platform_message_id,
    get_recent_image_messages,
    get_database,
    save_message,
)
from memory.micro_batch import trigger_micro_batch_check
from memory.context_builder import build_context
from tools.lutopia import (
    OPENAI_LUTOPIA_TOOLS,
    append_tool_exchange_to_messages,
    build_lutopia_internal_memory_appendix,
    create_lutopia_mcp_session,
    strip_lutopia_user_facing_assistant_text,
)
from tools.prompts import (
    OPENAI_SEARCH_TOOLS,
    OPENAI_WEATHER_TOOLS,
    OPENAI_WEIBO_TOOLS,
    OPENAI_X_TOOLS,
    build_tool_system_suffix,
    inject_tool_suffix_into_messages,
)
from tools.meme import search_meme_async, send_meme


# 设置日志
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


_TELEGRAM_THINK_PLACEHOLDER = "…"
# 超长截断后缀（不用前导「…」，避免与正文省略号叠成「……」难看）
_TELEGRAM_PLAIN_TRUNC_SUFFIX = "（已截断）"
_TELEGRAM_STREAM_GENERIC_ERROR = "抱歉，生成回复时出错了，请稍后再试。"
# Guard 用尽或仍拒答时的情境兜底（避免向用户展示模型安全拒答原文）
_TELEGRAM_GUARD_ROLEPLAY_FALLBACK = "……刚才有点走神，我们继续吧。"
_PARSE_IMAGE_COUNT_RE = re.compile(r'[前上]([1-9一二两三四五六七八九])张(?:图|照片|图片)')
_PARSE_IMAGE_COUNT_MAP = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def _telegram_user_visible_model_error(
    exc: BaseException,
    *,
    stream_chunk_timeout: bool,
) -> str:
    """
    将模型 HTTP/流式异常转为简短用户可见说明（不含堆栈）。
    stream_chunk_timeout=True：读超时按「SSE 两次 chunk 间隔」解释（Telegram 流式线程）。
    """
    rs = config.LLM_STREAM_READ_TIMEOUT
    if isinstance(exc, requests.exceptions.ReadTimeout):
        if stream_chunk_timeout:
            return (
                f"抱歉，流式读超时（{rs}s 内上游无新数据）。"
                f"可调大 .env 的 LLM_STREAM_READ_TIMEOUT（默认 {rs}s）。"
            )
        return (
            "抱歉，等待模型响应读超时。"
            f"可调大 .env 中的 LLM_TIMEOUT；若在 Telegram 使用流式，还可调大 LLM_STREAM_READ_TIMEOUT（默认 {rs} 秒）。"
        )
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "抱歉，连接模型接口超时。请检查网络、API 地址与代理后重试。"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "抱歉，无法连接到模型接口。请检查网络、域名解析、防火墙与代理。"
    if isinstance(exc, requests.exceptions.HTTPError) and getattr(exc, "response", None) is not None:
        try:
            sc = int(exc.response.status_code)
        except (TypeError, ValueError):
            sc = None
        if sc is not None:
            return (
                f"抱歉，模型接口返回 HTTP {sc}。"
                "请核对 API 密钥、配额、模型名与上游服务状态。"
            )
        return "抱歉，模型接口返回了 HTTP 错误。请核对密钥与上游服务。"
    if isinstance(exc, requests.exceptions.SSLError):
        return "抱歉，访问模型接口时 SSL/TLS 失败。请检查证书、代理与系统时间。"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return "抱歉，模型流式传输中断（数据不完整）。请稍后重试。"
    return (
        "抱歉，模型侧或网络异常导致生成失败。"
        "请稍后重试；若持续出现请查看服务日志中的详细报错。"
    )


def _is_stream_read_timeout_exc(exc: BaseException) -> bool:
    """流式 chat/completions 读超时（含 requests 对 urllib3 的封装）。"""
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return True
    try:
        import urllib3.exceptions as u3exc
    except ImportError:
        return False
    return isinstance(exc, u3exc.ReadTimeoutError)


# 流式读超时后最多重试次数（不含首次请求，共 1+ 此次 次 HTTP）
STREAM_READ_TIMEOUT_MAX_RETRIES = 3


def _normalize_telegram_reply_segment_markers(text: str) -> str:
    """全角竖线 ｜｜｜ (U+FF5C) 归一为 ASCII |||，避免模型用错符号导致无法分段。"""
    if not text:
        return ""
    return text.replace("｜｜｜", "|||")


def _split_telegram_body_parts(text: str) -> List[str]:
    """按 ||| 切正文段（先归一化全角分隔符）。仅用于 Telegram 缓冲回复。"""
    norm = _normalize_telegram_reply_segment_markers(text or "")
    return [p.strip() for p in norm.split("|||") if p.strip()]


def _sanitize_tts_voice_text(text: str) -> str:
    """
    语音兜底清洗：
    - 去掉任意中英文括号及其内部内容（含动作描述）。
    - 保留 <#x#> 停顿标签，供 TTS 使用。
    """
    if not text:
        return ""
    out = str(text)
    # 处理嵌套括号：循环替换直到稳定
    for _ in range(32):
        nxt = re.sub(r"\([^()]*\)", "", out)
        nxt = re.sub(r"（[^（）]*）", "", nxt)
        if nxt == out:
            break
        out = nxt
    out = re.sub(r"\s+", " ", out).strip()
    return out


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


class _TelegramSseRound(NamedTuple):
    """单轮 chat/completions SSE 结束快照（供定稿思维链 / 工具轮 / 最终入库）。"""

    done_payload: Optional[Dict[str, Any]]
    err_pack: Optional[Tuple[Any, str, str]]
    thinking_msg_id: Optional[int]
    think_from_delta: str
    think_plain: str
    raw_content: str
    interrupted: bool


def _telegram_reaction_emoji_label(rt: Any) -> Optional[str]:
    if isinstance(rt, ReactionTypeEmoji):
        return rt.emoji
    if isinstance(rt, ReactionTypeCustomEmoji):
        return rt.custom_emoji_id
    return None


async def _character_id_for_reaction_save() -> str:
    """激活 chat 行 `persona_id`；否则 `DEFAULT_CHARACTER_ID`（不经 LLMInterface）。"""
    cfg = await get_database().get_active_api_config("chat")
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
                    logger.warning("贴纸重扫超时回复失败: %s", exc_detail(e))
        finally:
            _rescan_timeout_tasks.pop(session_id, None)

    _rescan_timeout_tasks[session_id] = asyncio.create_task(_run())


def _sync_describe_sticker_vision(b64: str, mime_type: str,
                                   _db_cfg: Optional[Dict[str, Any]] = None) -> str:
    """同步调用 vision 配置，供 asyncio.to_thread 使用。"""
    prompt = (
        "请用40字以内描述这张贴纸的含义和情绪，\n"
        "如果图片中有文字请原样引用，不要描述技术细节"
    )
    llm = LLMInterface(config_type="vision", _db_cfg=_db_cfg)
    imgs = [{"type": "image", "data": b64, "mime_type": mime_type}]
    content = build_user_multimodal_content(
        llm.api_base, llm.model_name, prompt, imgs
    )
    llm_resp = llm.generate_with_context_and_tracking(
        [{"role": "user", "content": content}], platform=Platform.TELEGRAM
    )
    t = (llm_resp.content or "").strip()
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


class _SendOnlyChat:
    def __init__(self, chat_id: int, chat_type: str = "group") -> None:
        self.id = chat_id
        self.type = chat_type


class _SendOnlyTelegramMessage:
    """Small stand-in used by HTTP peer relay when Telegram did not deliver a Message."""

    def __init__(self, bot, chat_id: int) -> None:
        self._bot = bot
        self.chat = _SendOnlyChat(chat_id)

    async def reply_text(self, text: str, parse_mode: Optional[str] = None):
        return await self._bot.send_message(
            chat_id=self.chat.id,
            text=text,
            parse_mode=parse_mode,
        )


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

    @staticmethod
    def _is_group_message(message) -> bool:
        chat = getattr(message, "chat", None)
        return getattr(chat, "type", "") in ("group", "supergroup")

    @staticmethod
    def _is_truthy_config_value(value: Any) -> bool:
        return str(value if value is not None else "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    async def _should_ignore_group_user_mention(
        self, context: ContextTypes.DEFAULT_TYPE, message
    ) -> bool:
        """
        群聊中，普通消息两个 Bot 都能看；若用户单独 @ 某个 Bot，
        未被 @ 的另一个 Bot 不入库、不回复。
        """
        if not self._is_group_message(message):
            return False
        sender = getattr(message, "from_user", None)
        if sender is not None and getattr(sender, "is_bot", False):
            return False
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
        if "@" not in text:
            return False
        mentioned = {
            m.group(1).lower()
            for m in re.finditer(r"(?<![\w.])@([A-Za-z0-9_]{5,32})\b", text)
        }
        if not mentioned:
            return False
        try:
            me = await context.bot.get_me()
            me_username = (getattr(me, "username", "") or "").lower()
        except Exception as e:
            logger.debug("群聊 @ 过滤读取 bot username 失败，继续处理: %s", exc_detail(e))
            return False
        return bool(me_username and me_username not in mentioned)

    async def _telegram_should_send_cot(self, base_message=None) -> bool:
        db = get_database()
        send_cot_cfg = await db.get_config("send_cot_to_telegram", "1")
        if not self._is_truthy_config_value(send_cot_cfg):
            return False
        if base_message is not None and self._is_group_message(base_message):
            group_cot_cfg = await db.get_config("send_cot_in_group_chat", "0")
            return self._is_truthy_config_value(group_cot_cfg)
        return True

    @staticmethod
    def _strip_tts_markers(text: str) -> str:
        """去掉 TTS 专用标记，避免显示给用户。"""
        text = re.sub(r"<#[\d.]+#>", "", text)
        text = re.sub(
            r"\((sighs|chuckle|laughs|breath|inhale|exhale|gasps|groans|"
            r"humming|emm|coughs|clear-throat|sniffs|pant|snorts|hissing|sneezes)\)",
            "",
            text,
        )
        voice_tag = r"(?:voice|语音)(?:\s*\+\s*(?:voice|语音))*"
        text = re.sub(
            rf"\[\s*{voice_tag}\s*\].*?\[\s*/\s*{voice_tag}\s*\]",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return text

    async def _send_text_near_base(
        self,
        base_message,
        bot,
        text: str,
        *,
        parse_mode: Optional[str] = None,
    ):
        """
        私聊保留 reply 语义；群聊用 send_message，避免每条助手回复都显示引用用户消息。
        """
        text = self._strip_tts_markers(text)
        if base_message is not None and self._is_group_message(base_message) and bot is not None:
            return await bot.send_message(
                chat_id=base_message.chat.id,
                text=text,
                parse_mode=parse_mode,
            )
        return await base_message.reply_text(text, parse_mode=parse_mode)

    async def _send_voice_after_text(
        self,
        full_text: str,
        base_message,
        bot,
        chat_id: int,
    ) -> bool:
        """TTS 语音发送。只在私聊触发，失败静默降级不影响文字消息。返回是否成功发送语音。"""
        if base_message is not None and self._is_group_message(base_message):
            return False

        from memory.database import get_database
        db = get_database()
        tts_cfg = await db.get_tts_config()
        if not tts_cfg["enabled"] or not tts_cfg["voice_id"] or not tts_cfg["api_key"]:
            return False

        # 只去 HTML 标签，保留 TTS 停顿标记 <#1.5#>
        clean_text = re.sub(r"<(?!(?:#[\d.]+#))[a-zA-Z/][^>]*>", "", full_text).strip()
        # 兜底：语音内容中强制移除括号与括号内文本（包括中文全角括号）
        clean_text = _sanitize_tts_voice_text(clean_text)
        if not clean_text:
            return False

        clean_text = clean_text[:10000]

        from tools.tts_minimax import minimax_tts

        audio_bytes, tts_error = await minimax_tts(
            text=clean_text,
            api_key=tts_cfg["api_key"],
            voice_id=tts_cfg["voice_id"],
            model=tts_cfg["model"],
            speed=tts_cfg["speed"],
            vol=tts_cfg["vol"],
            pitch=tts_cfg["pitch"],
            intensity=tts_cfg["intensity"],
            timbre=tts_cfg["timbre"],
        )
        if audio_bytes is None:
            if tts_error:
                try:
                    await bot.send_message(chat_id=chat_id, text=tts_error)
                except Exception:
                    pass
            return False

        # Telegram send_voice 要求 OGG/Opus 格式，MiniMax TTS 返回 MP3，需要转换
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", "32k",
                "-vbr", "on", "-application", "voip",
                "-f", "ogg", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            ogg_bytes, stderr = await proc.communicate(input=audio_bytes)
            if proc.returncode != 0 or not ogg_bytes:
                logger.error("ffmpeg MP3→OGG failed: rc=%s err=%s", proc.returncode, stderr.decode(errors="replace")[:200])
                return False
        except Exception as e:
            logger.error("ffmpeg conversion error: %s", e)
            return False

        audio_io = io.BytesIO(ogg_bytes)
        audio_io.name = "voice.ogg"

        try:
            await bot.send_voice(chat_id=chat_id, voice=audio_io)
            return True
        except Exception as e:
            logger.error("send_voice failed: %s", e)
            return False

    async def _relay_group_assistant_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        content: str,
        character_id: Optional[str] = None,
        thinking: Optional[str] = None,
    ) -> None:
        urls = config.TELEGRAM_GROUP_PEER_RELAY_URLS
        token = config.TELEGRAM_GROUP_PEER_RELAY_TOKEN
        body = (content or "").strip()
        if not urls or not token or not body:
            return
        payload = {
            "sender_app_id": config.TELEGRAM_GROUP_PEER_RELAY_APP_ID,
            "sender_name": config.APP_NAME,
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "content": body,
            "character_id": character_id,
            "thinking": thinking,
        }
        try:
            app = getattr(self, "application", None)
            bot_obj = getattr(app, "bot", None) if app is not None else None
            if bot_obj is not None:
                me = await bot_obj.get_me()
                payload["sender_bot_id"] = str(getattr(me, "id", "") or "")
                payload["sender_name"] = (
                    getattr(me, "username", None)
                    or getattr(me, "first_name", None)
                    or config.APP_NAME
                )
        except Exception as e:
            logger.debug("peer relay 读取当前 bot 信息失败，继续发送: %s", exc_detail(e))

        headers = {
            "Content-Type": "application/json",
            "X-Cedarstar-Token": token,
        }

        def _post_all() -> None:
            for url in urls:
                try:
                    resp = requests.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=config.TELEGRAM_GROUP_PEER_RELAY_TIMEOUT,
                    )
                    if resp.status_code >= 400:
                        logger.warning(
                            "群聊 peer relay 失败 status=%s url=%s body=%r",
                            resp.status_code,
                            url,
                            (resp.text or "")[:300],
                        )
                except Exception as e:
                    logger.warning("群聊 peer relay 请求失败 url=%s: %s", url, exc_detail(e))

        asyncio.create_task(asyncio.to_thread(_post_all))

    async def _recent_assistant_other_content_exists(
        self, session_id: str, content: str, *, limit: int = 12
    ) -> bool:
        target = (content or "").strip()
        if not target:
            return False
        try:
            rows = await get_database().get_recent_messages(session_id, limit=limit)
        except Exception as e:
            logger.debug("读取近期 assistant_other 去重窗口失败: %s", exc_detail(e))
            return False
        return any(
            str(row.get("role") or "") == "assistant_other"
            and str(row.get("content") or "").strip() == target
            for row in rows
        )
    
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
        current_llm = await LLMInterface.create()
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
        await db.clear_session_messages(session_id)
        
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

    async def silent_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await get_database().set_config("group_chat_silent_mode", "1")
        if update.message:
            await update.message.reply_text("已进入群聊静默模式")

    async def wake_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await get_database().set_config("group_chat_silent_mode", "0")
        if update.message:
            await update.message.reply_text("已退出群聊静默模式")

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
        if await self._handle_group_bot_message(update, context, message, session_id):
            return
        if await self._should_ignore_group_user_mention(context, message):
            logger.info(
                "群聊消息单独 @ 了其他 bot，当前 bot 忽略: chat_id=%s message_id=%s",
                getattr(message.chat, "id", None),
                getattr(message, "message_id", None),
            )
            return
        if await get_database().get_config("group_chat_silent_mode", "0") == "1":
            if getattr(message.chat, "type", "") in ("group", "supergroup"):
                return
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
        if getattr(message.chat, "type", "") in ("group", "supergroup"):
            await get_database().set_group_chat_round_count(str(chat_id), 0)
        logger.info(
            "[TG路径追踪] 入口 handle_message(纯文本) session_id=%s -> MessageBuffer.add_to_buffer；"
            "buffer_delay 到期后 MessageBuffer 回调 _flush_buffered_messages -> _generate_reply_from_buffer",
            session_id,
        )

        # 将消息添加到缓冲区
        await self._add_to_buffer(update, context, session_id, message, content, user_id, message_id)

    async def _handle_group_bot_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message,
        session_id: str,
    ) -> bool:
        """群聊中记录另一名 bot，并按 @/概率插话触发回复。"""
        chat_type = getattr(message.chat, "type", "")
        sender = getattr(message, "from_user", None)
        if chat_type not in ("group", "supergroup") or not sender or not getattr(sender, "is_bot", False):
            return False
        try:
            me = await context.bot.get_me()
            if getattr(me, "id", None) == getattr(sender, "id", None):
                return True
        except Exception:
            me = None

        content = (message.text or message.caption or "").strip()
        if not content:
            return True
        db = get_database()
        if await self._recent_assistant_other_content_exists(session_id, content):
            return True
        other_name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "other_bot"
        await save_message(
            session_id=session_id,
            role="assistant_other",
            content=content,
            user_id=str(getattr(sender, "id", "unknown")),
            channel_id=str(message.chat.id),
            message_id=str(message.message_id),
            character_id=other_name,
            platform=Platform.TELEGRAM,
            vision_processed=1,
        )
        asyncio.create_task(trigger_micro_batch_check(session_id))

        if await db.get_config("group_chat_silent_mode", "0") == "1":
            return True
        max_rounds = int(await db.get_config("group_chat_max_rounds", "3") or 3)
        round_count = await db.get_group_chat_round_count(str(message.chat.id))
        if round_count >= max_rounds:
            return True

        me_username = (getattr(me, "username", "") or "").lower() if me else ""
        mentioned = bool(me_username and f"@{me_username}" in content.lower())
        interject = False
        if not mentioned and await db.get_config("group_chat_interject_enabled", "0") == "1":
            try:
                prob = float(await db.get_config("group_chat_interject_probability", "0.2") or 0.2)
            except (TypeError, ValueError):
                prob = 0.2
            interject = random.random() < max(0.0, min(1.0, prob))
        if not mentioned and not interject:
            return True

        delta = 1 if mentioned else 2
        if round_count + delta > max_rounds:
            return True
        await db.increment_group_chat_round_count(str(message.chat.id), delta)
        prompt = (
            f"[另一名助手 {other_name} 的发言]：{content}\n\n"
            "请自然接话，避免重复对方内容。"
        )
        gen = await self._generate_reply_from_buffer(
            session_id=session_id,
            combined_raw=prompt,
            combined_content=prompt,
            user_id=str(getattr(sender, "id", "unknown")),
            chat_id=str(message.chat.id),
            message_id=str(message.message_id),
            buffer_messages=[{
                "message": message,
                "context": context,
                "user_id": str(getattr(sender, "id", "unknown")),
                "message_id": str(message.message_id),
            }],
            base_message=message,
            bot=context.bot,
            persist_user=False,
        )
        if gen.persist_assistant and gen.reply.strip():
            assistant_mid = gen.assistant_message_id or f"ai_{message.message_id}"
            await save_message(
                session_id=session_id,
                role="assistant",
                content=gen.reply,
                user_id=str(getattr(sender, "id", "unknown")),
                channel_id=str(message.chat.id),
                message_id=assistant_mid,
                character_id=gen.character_id,
                platform=Platform.TELEGRAM,
                thinking=gen.thinking,
            )
            await self._relay_group_assistant_message(
                chat_id=str(message.chat.id),
                message_id=assistant_mid,
                content=gen.reply,
                character_id=gen.character_id,
                thinking=gen.thinking,
            )
        return True

    async def handle_peer_group_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """接收另一实例通过 HTTP relay 发来的群聊助手消息。"""
        sender_app_id = str(payload.get("sender_app_id") or "").strip()
        if sender_app_id and sender_app_id == config.TELEGRAM_GROUP_PEER_RELAY_APP_ID:
            return {"status": "ignored_self"}

        content = str(payload.get("content") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        source_message_id = str(payload.get("message_id") or "").strip()
        if not content or not chat_id or not source_message_id:
            return {"status": "ignored_empty"}

        session_id = f"telegram_{chat_id}"
        peer_message_id = f"peer_{sender_app_id or 'unknown'}_{source_message_id}"
        db = get_database()
        if await db.message_exists(session_id, peer_message_id):
            return {"status": "duplicate"}
        if await self._recent_assistant_other_content_exists(session_id, content):
            return {"status": "duplicate_content"}

        other_name = (
            str(payload.get("sender_name") or "").strip()
            or str(payload.get("character_id") or "").strip()
            or sender_app_id
            or "other_bot"
        )
        await save_message(
            session_id=session_id,
            role="assistant_other",
            content=content,
            user_id=str(payload.get("sender_bot_id") or sender_app_id or "peer"),
            channel_id=chat_id,
            message_id=peer_message_id,
            character_id=other_name,
            platform=Platform.TELEGRAM,
            vision_processed=1,
        )
        asyncio.create_task(trigger_micro_batch_check(session_id))

        if await db.get_config("group_chat_silent_mode", "0") == "1":
            return {"status": "saved_silent"}
        max_rounds = int(await db.get_config("group_chat_max_rounds", "3") or 3)
        round_count = await db.get_group_chat_round_count(chat_id)
        if round_count >= max_rounds:
            return {"status": "saved_round_limited"}

        app = getattr(self, "application", None)
        bot_obj = getattr(app, "bot", None) if app is not None else None
        if bot_obj is None:
            return {"status": "saved_no_bot"}
        try:
            me = await bot_obj.get_me()
            me_username = (getattr(me, "username", "") or "").lower()
        except Exception as e:
            logger.warning("peer relay 获取当前 bot 信息失败: %s", exc_detail(e))
            me_username = ""

        mentioned = bool(me_username and f"@{me_username}" in content.lower())
        interject = False
        if not mentioned and await db.get_config("group_chat_interject_enabled", "0") == "1":
            try:
                prob = float(await db.get_config("group_chat_interject_probability", "0.2") or 0.2)
            except (TypeError, ValueError):
                prob = 0.2
            interject = random.random() < max(0.0, min(1.0, prob))
        if not mentioned and not interject:
            return {"status": "saved_no_reply"}

        delta = 1 if mentioned else 2
        if round_count + delta > max_rounds:
            return {"status": "saved_round_limited"}
        await db.increment_group_chat_round_count(chat_id, delta)

        prompt = (
            f"[另一名助手 {other_name} 的发言]：{content}\n\n"
            "请自然接话，避免重复对方内容。"
        )
        fake_message = _SendOnlyTelegramMessage(bot_obj, int(chat_id))
        gen = await self._generate_reply_from_buffer(
            session_id=session_id,
            combined_raw=prompt,
            combined_content=prompt,
            user_id=str(payload.get("sender_bot_id") or sender_app_id or "peer"),
            chat_id=chat_id,
            message_id=peer_message_id,
            buffer_messages=[{
                "message": fake_message,
                "context": None,
                "user_id": str(payload.get("sender_bot_id") or sender_app_id or "peer"),
                "message_id": peer_message_id,
            }],
            base_message=fake_message,
            bot=bot_obj,
            persist_user=False,
        )
        if gen.persist_assistant and gen.reply.strip():
            assistant_mid = gen.assistant_message_id or f"ai_{peer_message_id}"
            await save_message(
                session_id=session_id,
                role="assistant",
                content=gen.reply,
                user_id=str(payload.get("sender_bot_id") or sender_app_id or "peer"),
                channel_id=chat_id,
                message_id=assistant_mid,
                character_id=gen.character_id,
                platform=Platform.TELEGRAM,
                thinking=gen.thinking,
            )
            await self._relay_group_assistant_message(
                chat_id=chat_id,
                message_id=assistant_mid,
                content=gen.reply,
                character_id=gen.character_id,
                thinking=gen.thinking,
            )
            return {"status": "saved_replied"}
        return {"status": "saved_reply_empty"}

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
                    "platform_file_id": photo.file_id,
                }
                await self._message_buffer.add_to_buffer(
                    session_id,
                    {
                        "update": update,
                        "context": context,
                        "message": message,
                        "content": "",
                        "image_payload": image_payload,
                        "platform_file_id": photo.file_id,
                        "user_id": user_id,
                        "message_id": message_id,
                        "timestamp": asyncio.get_event_loop().time(),
                    },
                )
            except Exception as e:
                logger.exception("Telegram 图片下载失败: %s", exc_detail(e))
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
                logger.exception("Telegram 语音下载失败: %s", exc_detail(e))
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
                logger.warning("Telegram 语音转录失败: %s", exc_detail(e))
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
            row = await db.get_sticker_cache(fid)
            if row and (row.get("description") or "").strip():
                return str(row["description"]).strip()

            if fid in processing_stickers:
                t0 = time.monotonic()
                while fid in processing_stickers and time.monotonic() - t0 < 3.0:
                    await asyncio.sleep(0.1)
                    row = await db.get_sticker_cache(fid)
                    if row and (row.get("description") or "").strip():
                        return str(row["description"]).strip()
                row = await db.get_sticker_cache(fid)
                if row and (row.get("description") or "").strip():
                    return str(row["description"]).strip()
                if fid in processing_stickers:
                    return fallback
                await asyncio.sleep(0.05)
                continue

            async with _sticker_coord_lock:
                row = await db.get_sticker_cache(fid)
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
                try:
                    from memory.database import get_database as _gdb
                    _vision_db_cfg = await _gdb().get_active_api_config("vision")
                except Exception:
                    _vision_db_cfg = None
                text = await asyncio.to_thread(
                    _sync_describe_sticker_vision, b64, mime, _vision_db_cfg
                )
                if text:
                    desc = text
            except Exception as e:
                logger.warning(
                    "贴纸视觉解析失败 fid=%s: %s", fid, exc_detail(e)
                )
                desc = fallback
            finally:
                await db.save_sticker_cache(fid, emoji, set_name, desc)
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
                await db.delete_sticker_cache(fid)
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
        text = strip_lutopia_user_facing_assistant_text(text or "")
        return split_body_into_html_chunks(text, max_html_len)

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
            logger.debug(
                "Telegram edit_message_text 失败 chat_id=%s msg_id=%s: %s",
                chat_id,
                message_id,
                exc_detail(e),
            )

    def _telegram_thinking_blockquote_html(self, think_plain: str) -> str:
        """思维链定稿：优先使用 Telegram 可折叠 blockquote；内容按模型标签拆包。"""
        thinking, content = split_thinking_and_content(think_plain or "")
        if content:
            think_plain = content
        elif thinking:
            think_plain = thinking
        think_plain = telegram_send_text_collapse((think_plain or "").replace("\x00", ""))
        esc = self._escape_telegram_html(think_plain)
        head = "<blockquote expandable>🧠 思维链\n"
        tail = "</blockquote>"
        max_len = 4096
        inner_max = max_len - len(head) - len(tail)
        if inner_max < 1:
            return head + self._escape_telegram_html("…") + tail
        if len(esc) <= inner_max:
            return head + esc + tail
        trunc_m = _TELEGRAM_PLAIN_TRUNC_SUFFIX
        esc_t = self._think_display_trunc(esc, inner_max, trunc_m)
        return head + esc_t + tail

    def _telegram_foldable_blockquote_html(
        self, title_plain: str, body_plain: str
    ) -> str:
        """
        `<blockquote expandable>`，首行为标题，其余为正文（Lutopia 工具失败/完成等长内容）。
        """
        title_plain = telegram_send_text_collapse(
            (title_plain or "📎").replace("\x00", "")
        )
        body_plain = telegram_send_text_collapse((body_plain or "").replace("\x00", ""))
        e_title = self._escape_telegram_html(title_plain)
        e_body = self._escape_telegram_html(body_plain)
        head = f"<blockquote expandable>{e_title}\n"
        tail = "</blockquote>"
        max_len = 4096
        inner_max = max_len - len(head) - len(tail)
        if inner_max < 1:
            return head + self._escape_telegram_html("…") + tail
        if len(e_body) <= inner_max:
            return head + e_body + tail
        trunc_m = _TELEGRAM_PLAIN_TRUNC_SUFFIX
        e_b = self._think_display_trunc(e_body, inner_max, trunc_m)
        return head + e_b + tail

    @staticmethod
    def _telegram_lutopia_tool_display_name(tool_name: str) -> str:
        t = (tool_name or "").strip()
        if t == "get_weather":
            return "天气"
        if t == "get_weibo_hot":
            return "微博热搜"
        if t == "web_search":
            return "网页搜索"
        if t.startswith("lutopia_"):
            return t[8:] or t
        return t or "tool"

    async def _telegram_send_body_segments(
        self, base_message, cleaned_with_separators: str, bot=None
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
            stack = "".join(traceback.format_stack())
            logging.warning(f"send called from: {stack}")
            sent = await self._send_text_near_base(
                base_message, bot, chunk, parse_mode="HTML"
            )
            if first_mid is None:
                first_mid = str(sent.message_id)
            if i + 1 < len(out_chunks):
                await asyncio.sleep(0.5)
        return body_for_db, first_mid

    async def _telegram_send_body_via_chat(
        self, bot, chat_id: int, cleaned: str
    ) -> Tuple[str, Optional[str]]:
        """无 base_message 时向 chat 发送分段 HTML（与 _telegram_send_body_segments 对齐）。"""
        parts = _split_telegram_body_parts(cleaned)
        body_for_db = "\n".join(parts)
        out_chunks: List[str] = []
        for seg in parts:
            out_chunks.extend(self._telegram_html_body_chunks(seg))
        first_mid: Optional[str] = None
        for i, chunk in enumerate(out_chunks):
            chunk = self._strip_tts_markers(chunk)
            stack = "".join(traceback.format_stack())
            logging.warning(f"send called from: {stack}")
            sent = await bot.send_message(
                chat_id=chat_id, text=chunk, parse_mode="HTML"
            )
            if first_mid is None:
                first_mid = str(sent.message_id)
            if i + 1 < len(out_chunks):
                await asyncio.sleep(0.5)
        return body_for_db, first_mid

    async def _telegram_send_one_meme(
        self, bot, chat_id: int, query: str
    ) -> Tuple[bool, Optional[str]]:
        """单条描述 search_meme(top_k=1) 并发送；空查询或检索无结果则跳过。返回 (是否发出, message_id)。"""
        q = (query or "").strip()
        if not q:
            return False, None
        results = await search_meme_async(q, 1)
        if not results:
            return False, None
        row = results[0]
        url = (row.get("url") or "").strip()
        if not url:
            return False, None
        try:
            isa = int(row.get("is_animated", 0))
        except (TypeError, ValueError):
            isa = 0
        try:
            sent = await send_meme(url, isa, bot, chat_id)
            mid: Optional[str] = None
            if sent is not None:
                raw_mid = getattr(sent, "message_id", None)
                if raw_mid is not None:
                    mid = str(raw_mid)
            return True, mid
        except Exception as e:
            logger.warning("send_meme 失败: %s", exc_detail(e))
            return False, None

    async def _telegram_send_meme_queries(
        self, bot, chat_id: int, queries: List[str]
    ) -> Tuple[bool, Optional[str]]:
        """按顺序对每条描述发送表情包（内部复用 _telegram_send_one_meme）。"""
        any_sent = False
        first_mid: Optional[str] = None
        for q in queries:
            sent, mid = await self._telegram_send_one_meme(bot, chat_id, q)
            if sent:
                any_sent = True
                if first_mid is None and mid:
                    first_mid = mid
                await asyncio.sleep(0.3)
        return any_sent, first_mid

    async def _telegram_deliver_ordered_segments(
        self,
        bot,
        chat_id: int,
        segments: List[Tuple[str, str]],
        *,
        base_message=None,
    ) -> Tuple[Optional[str], bool, bool]:
        """
        按 segments 顺序交替发文字段（可走 HTML 分段）、表情包与语音。
        base_message 非空时文字用 reply；否则用 send_message（与 _telegram_send_body_via_chat 一致）。
        返回 (首条助手消息 message_id, 是否至少发出过一张表情, 是否成功发送语音)。
        """
        first_mid: Optional[str] = None
        meme_any = False
        voice_any = False
        for i, (kind, payload) in enumerate(segments):
            logger.info(
                f"[segment_debug] 发送第{i + 1}段 type={kind} "
                f"len={len(payload or '')} preview={repr((payload or '')[:50])}"
            )
            if kind == "text":
                t = (payload or "").strip()
                if not t:
                    continue
                if base_message is not None:
                    _, mid = await self._telegram_send_body_segments(
                        base_message, t, bot=bot
                    )
                else:
                    _, mid = await self._telegram_send_body_via_chat(
                        bot, chat_id, t
                    )
                if first_mid is None and mid:
                    first_mid = mid
                await asyncio.sleep(0.25)
            elif kind == "meme":
                sent, mid = await self._telegram_send_one_meme(
                    bot, chat_id, payload
                )
                if sent:
                    meme_any = True
                    if first_mid is None and mid:
                        first_mid = mid
                    await asyncio.sleep(0.3)
            elif kind == "voice":
                v = (payload or "").strip()
                if not v:
                    continue
                sent = await self._send_voice_after_text(
                    v, base_message, bot, chat_id
                )
                if sent:
                    voice_any = True
                await asyncio.sleep(0.25)

        return first_mid, meme_any, voice_any

    async def _telegram_deliver_prefetched_llm_response(
        self, llm_resp: Any, base_message, bot
    ) -> _TelegramStreamOutcome:
        """非流式 LLM 结果：思维链 blockquote + 正文分段（与流式结束态一致）。"""
        send_cot = await self._telegram_should_send_cot(base_message)

        think_plain_raw = (llm_resp.thinking or "").strip() or None
        if not send_cot:
            think_plain = None
        else:
            th_part, body_part = split_thinking_and_content(think_plain_raw or "")
            think_plain = th_part or think_plain_raw
            if body_part and not (llm_resp.content or "").strip():
                llm_resp = copy.copy(llm_resp)
                llm_resp.content = body_part

        if think_plain:
            html_th = self._telegram_thinking_blockquote_html(think_plain)
            stack = "".join(traceback.format_stack())
            logging.warning(f"send called from: {stack}")
            await self._send_text_near_base(
                base_message, bot, html_th, parse_mode="HTML"
            )
        cleaned = schedule_update_memory_hits_and_clean_reply(llm_resp.content or "")
        segments, body_for_db = await parse_telegram_segments_with_memes_async(cleaned)
        has_text_seg = any(
            k == "text" and (s or "").strip() for k, s in segments
        )
        assistant_message_id: Optional[str] = None
        meme_sent = False
        voice_sent = False
        if segments:
            assistant_message_id, meme_sent, voice_sent = (
                await self._telegram_deliver_ordered_segments(
                    bot,
                    base_message.chat.id,
                    segments,
                    base_message=base_message,
                )
            )
        if not body_for_db.strip() and meme_sent:
            body_for_db = "[表情包]"
        sent_something = bool(has_text_seg or think_plain or meme_sent)
        if not sent_something:
            await self._send_text_near_base(
                base_message,
                bot,
                "抱歉，本轮未得到可发送的内容。请重试。",
                parse_mode=None,
            )
        return _TelegramStreamOutcome(
            body_for_db=body_for_db,
            assistant_message_id=assistant_message_id,
            thinking=think_plain,
            save_user=True,
        )

    async def _telegram_stream_llm_one_sse_round(
        self,
        llm: LLMInterface,
        messages: List[Dict[str, Any]],
        base_message,
        bot,
        tools: Optional[List[Dict[str, Any]]] = None,
        cacheable_ratio: float = 0.0,
    ) -> _TelegramSseRound:
        """
        一轮 SSE（可含多次 HTTP）：仅对 **流式读超时** 自动重试，最多重试 STREAM_READ_TIMEOUT_MAX_RETRIES 次。

        「超时重试中（n/m）」为 Telegram 纯提示，不入库；最终错误提示同样不入库（body_for_db 无模型正文）。
        """
        chat_id = base_message.chat.id
        for attempt in range(STREAM_READ_TIMEOUT_MAX_RETRIES + 1):
            sse = await self._telegram_stream_llm_one_sse_attempt(
                llm, messages, base_message, bot, tools=tools,
                cacheable_ratio=cacheable_ratio,
            )
            if sse.err_pack is None:
                return sse
            raw_ex = sse.err_pack[0]
            ex_obj = (
                raw_ex
                if isinstance(raw_ex, BaseException)
                else RuntimeError(str(raw_ex))
            )
            if not _is_stream_read_timeout_exc(ex_obj):
                return sse
            if attempt >= STREAM_READ_TIMEOUT_MAX_RETRIES:
                return sse
            if sse.thinking_msg_id is not None:
                try:
                    await bot.delete_message(
                        chat_id=chat_id, message_id=sse.thinking_msg_id
                    )
                except Exception as del_e:
                    logger.warning(
                        "读超时重试前删除思维链占位失败 chat_id=%s: %s",
                        chat_id,
                        exc_detail(del_e),
                    )
            # 仅 Telegram 提示，不参与 save_message / outcome.body_for_db
            await self._send_text_near_base(
                base_message,
                bot,
                f"超时重试中（{attempt + 1}/{STREAM_READ_TIMEOUT_MAX_RETRIES}）",
                parse_mode=None,
            )
            logger.warning(
                "流式 ReadTimeout，第 %s/%s 次重试即将开始",
                attempt + 1,
                STREAM_READ_TIMEOUT_MAX_RETRIES,
            )

    async def _telegram_stream_llm_one_sse_attempt(
        self,
        llm: LLMInterface,
        messages: List[Dict[str, Any]],
        base_message,
        bot,
        tools: Optional[List[Dict[str, Any]]] = None,
        cacheable_ratio: float = 0.0,
    ) -> _TelegramSseRound:
        """单次 HTTP 流式：实时编辑思维链占位；结束态供 _telegram_finalize_sse_round_outcome 定稿。"""
        chat_id = base_message.chat.id
        send_cot = await self._telegram_should_send_cot(base_message)
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def run_stream() -> None:
            co_list: List[str] = []
            th_list: List[str] = []
            try:
                gen = llm.generate_stream(
                    messages, platform=Platform.TELEGRAM, tools=tools,
                    cacheable_ratio=cacheable_ratio,
                )
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
                                        "tool_calls": fin.get("tool_calls"),
                                        "guard_refusal_abort": bool(
                                            fin.get("guard_refusal_abort")
                                        ),
                                        "cacheable_ratio": fin.get("cacheable_ratio", 0.0),
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
                logger.exception(
                    "Telegram LLM 流式线程异常（上游 SSE / chat/completions 或网络）: %s",
                    exc_detail(ex),
                )
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
                if not send_cot:
                    continue
                thinking_parts.append(item[1])
                cur = "".join(thinking_parts)
                if thinking_msg_id is None:
                    stack = "".join(traceback.format_stack())
                    logging.warning(f"send called from: {stack}")
                    sent = await self._send_text_near_base(
                        base_message, bot, _TELEGRAM_THINK_PLACEHOLDER
                    )
                    thinking_msg_id = sent.message_id
                now = time.monotonic()
                if now - last_think_edit >= config.TELEGRAM_THINK_STREAM_EDIT_INTERVAL_SEC:
                    plain = cur or _TELEGRAM_THINK_PLACEHOLDER
                    plain = telegram_send_text_collapse(plain)
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
            if send_cot:
                if isinstance(t_api, str) and t_api.strip():
                    think_plain = t_api.strip()
                else:
                    think_plain = think_from_delta
            else:
                think_plain = ""
            interrupted = False
            
            # 保存 Token (流式在子线程丢弃了记录，这里在主 loop 补记)
            u_data = done_payload.get("usage")
            if u_data:
                llm._save_token_usage_async(
                    u_data, Platform.TELEGRAM,
                    done_payload.get("cacheable_ratio", 0.0),
                )

        elif err_pack is not None:
            _ex, c_partial, t_partial = err_pack
            raw_content = c_partial or ""
            if send_cot:
                think_plain = think_from_delta or (t_partial or "").strip()
            else:
                think_plain = ""
            interrupted = True
        else:
            raw_content = ""
            think_plain = think_from_delta if send_cot else ""
            interrupted = False

        return _TelegramSseRound(
            done_payload=done_payload,
            err_pack=err_pack,
            thinking_msg_id=thinking_msg_id,
            think_from_delta=think_from_delta,
            think_plain=think_plain,
            raw_content=raw_content,
            interrupted=interrupted,
        )

    async def _telegram_finalize_thinking_blockquote(
        self,
        base_message,
        bot,
        chat_id: int,
        thinking_msg_id: Optional[int],
        think_plain: str,
        interrupted: bool,
    ) -> Optional[int]:
        """将占位思维链消息定稿为 blockquote，或删除空占位。返回最终 message_id（若新建）。"""
        if interrupted and think_plain and "（已中断）" not in think_plain:
            think_plain_show = think_plain + "（已中断）"
        else:
            think_plain_show = think_plain

        out_mid = thinking_msg_id
        think_plain_show = (think_plain_show or "").replace("\x00", "")
        if thinking_msg_id is not None:
            if think_plain_show.strip():
                html_th = self._telegram_thinking_blockquote_html(think_plain_show)
                edited_ok = False
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=thinking_msg_id,
                        text=html_th,
                        parse_mode="HTML",
                    )
                    edited_ok = True
                except Exception as e:
                    logger.warning(
                        "思维链定稿 edit_message(HTML 可折叠) 失败，将尝试删旧消息并重发: %s",
                        exc_detail(e),
                    )
                if not edited_ok:
                    deleted_ok = False
                    try:
                        await bot.delete_message(
                            chat_id=chat_id, message_id=thinking_msg_id
                        )
                        deleted_ok = True
                    except Exception:
                        pass
                    try:
                        stack = "".join(traceback.format_stack())
                        logging.warning(f"send called from: {stack}")
                        sent_th = await self._send_text_near_base(
                            base_message, bot, html_th, parse_mode="HTML"
                        )
                        out_mid = sent_th.message_id
                    except Exception as e2:
                        logger.warning(
                            "思维链删旧后重发仍失败: %s",
                            exc_detail(e2),
                        )
                        if deleted_ok:
                            out_mid = None
            else:
                try:
                    await bot.delete_message(
                        chat_id=chat_id, message_id=thinking_msg_id
                    )
                except Exception:
                    pass
                out_mid = None
        elif think_plain_show.strip():
            html_th = self._telegram_thinking_blockquote_html(think_plain_show)
            try:
                stack = "".join(traceback.format_stack())
                logging.warning(f"send called from: {stack}")
                sent_th = await self._send_text_near_base(
                    base_message, bot, html_th, parse_mode="HTML"
                )
                out_mid = sent_th.message_id
            except Exception as e:
                logger.warning("思维链首条发送(HTML 可折叠) 失败: %s", exc_detail(e))
                out_mid = None
        return out_mid

    async def _telegram_finalize_sse_round_outcome(
        self,
        sse: _TelegramSseRound,
        base_message,
        bot,
    ) -> _TelegramStreamOutcome:
        """定稿思维链 + 发送正文，组装缓冲 outcome。"""
        chat_id = base_message.chat.id
        done_payload = sse.done_payload
        err_pack = sse.err_pack
        think_plain = sse.think_plain
        raw_content = sse.raw_content
        th_part, body_part = split_thinking_and_content(think_plain or "")
        if th_part:
            think_plain = th_part
        if body_part and not (raw_content or "").strip():
            raw_content = body_part
        if done_payload is not None:
            if done_payload.get("guard_refusal_abort") and not (raw_content or "").strip():
                raw_content = _TELEGRAM_GUARD_ROLEPLAY_FALLBACK
            elif output_guard_blocks_model_text(raw_content or ""):
                raw_content = _TELEGRAM_GUARD_ROLEPLAY_FALLBACK
        interrupted = sse.interrupted

        if err_pack is not None:
            _ex, c_partial, t_partial = err_pack
            logger.error(
                "Telegram 流式生成异常（见下方堆栈/摘要）: %s | "
                "已缓冲 partial 正文=%d 字符 partial 思维链=%d 字符",
                exc_detail(_ex) if isinstance(_ex, BaseException) else repr(_ex),
                len(c_partial or ""),
                len(t_partial or ""),
            )

        if sse.done_payload is not None:
            t_api = sse.done_payload.get("thinking")
            if isinstance(t_api, str) and t_api.strip():
                thinking_stored = t_api.strip()
            elif sse.think_from_delta:
                thinking_stored = sse.think_from_delta
            else:
                thinking_stored = None
        elif err_pack is not None:
            _ex, c_partial, t_partial = err_pack
            thinking_stored = sse.think_from_delta or (
                (t_partial or "").strip() or None
            )
        else:
            thinking_stored = sse.think_from_delta or None
        if thinking_stored:
            th_part2, body_part2 = split_thinking_and_content(thinking_stored)
            thinking_stored = th_part2 or thinking_stored
            if body_part2 and not raw_content.strip():
                raw_content = body_part2

        await self._telegram_finalize_thinking_blockquote(
            base_message,
            bot,
            chat_id,
            sse.thinking_msg_id,
            think_plain,
            interrupted,
        )

        cleaned = schedule_update_memory_hits_and_clean_reply(raw_content)
        segments, body_for_db = await parse_telegram_segments_with_memes_async(cleaned)
        has_text_seg = any(
            k == "text" and (s or "").strip() for k, s in segments
        )
        logger.debug(
            "Telegram 流式结束: 有序段数=%s (||| / [meme:…] / 正文换行二级分段)",
            len(segments),
        )
        assistant_message_id: Optional[str] = None
        meme_sent = False
        voice_sent = False
        if segments:
            assistant_message_id, meme_sent, voice_sent = (
                await self._telegram_deliver_ordered_segments(
                    bot, chat_id, segments, base_message=base_message
                )
            )
        if not body_for_db.strip() and meme_sent:
            body_for_db = "[表情包]"

        sent_something = bool(
            has_text_seg or (think_plain or "").strip() or meme_sent
        )
        # 以下仅 Telegram 提示：不入库。落库正文只来自 outcome.body_for_db（模型/分段结果），见 _flush_buffered_messages。
        if done_payload is not None and not sent_something:
            logger.warning(
                "Telegram SSE 收尾：无正文且无思维链，将发通用错误提示。"
                " raw_len=%s cleaned_len=%s raw_preview=%r",
                len(raw_content or ""),
                len(cleaned or ""),
                (raw_content or "")[:400],
            )
            await self._send_text_near_base(
                base_message,
                bot,
                "抱歉，本轮未得到有效回复（模型返回为空或被过滤）。请重试。",
                parse_mode=None,
            )
        if err_pack is not None and not sent_something:
            _ex_err, _, _ = err_pack
            await self._send_text_near_base(
                base_message,
                bot,
                _telegram_user_visible_model_error(
                    _ex_err if isinstance(_ex_err, BaseException) else RuntimeError(str(_ex_err)),
                    stream_chunk_timeout=True,
                ),
                parse_mode=None,
            )

        if done_payload is not None:
            save_user = True
        elif err_pack is not None:
            save_user = bool(sent_something)
        else:
            save_user = False

        return _TelegramStreamOutcome(
            body_for_db=body_for_db,
            assistant_message_id=assistant_message_id,
            thinking=thinking_stored,
            save_user=save_user,
        )

    async def _telegram_stream_thinking_and_reply(
        self,
        llm: LLMInterface,
        messages: List[Dict[str, Any]],
        base_message,
        bot,
        cacheable_ratio: float = 0.0,
    ) -> _TelegramStreamOutcome:
        cur_messages: List[Dict[str, Any]] = messages
        sse: Optional[_TelegramSseRound] = None
        for attempt in range(2):
            sse = await self._telegram_stream_llm_one_sse_round(
                llm, cur_messages, base_message, bot,
                cacheable_ratio=cacheable_ratio,
            )
            fin = sse.done_payload or {}
            if fin.get("guard_refusal_abort") and attempt == 0:
                cur_messages = append_guard_hint_to_last_user_message(
                    messages, TELEGRAM_GUARD_PROMPT_APPEND
                )
                logger.warning(
                    "CedarClio Guard：同步链路流式掐断或拒答，正在静默重试一次"
                )
                continue
            break
        assert sse is not None
        return await self._telegram_finalize_sse_round_outcome(
            sse, base_message, bot
        )

    async def _telegram_stream_thinking_and_reply_with_lutopia(
        self,
        llm: LLMInterface,
        messages: List[Dict[str, Any]],
        base_message,
        bot,
        session_id: Optional[str] = None,
        user_message_id: Optional[int] = None,
        cacheable_ratio: float = 0.0,
    ) -> _TelegramStreamOutcome:
        """
        Sirius + OpenAI 兼容路径：首轮起携带 Lutopia tools；若模型发起 function call，
        执行后把 tool 结果追加进对话再继续 SSE，直至得到面向用户的正文。
        """
        cur_messages = copy.deepcopy(messages)
        tools_list: List[Dict[str, Any]] = []
        suffix_keys: List[str] = []
        from tools.memory_tools import OPENAI_MEMORY_TOOLS
        tools_list.extend(OPENAI_MEMORY_TOOLS)
        suffix_keys.append("memory")
        if llm.enable_lutopia:
            tools_list.extend(OPENAI_LUTOPIA_TOOLS)
            suffix_keys.append("lutopia")
        if getattr(llm, "enable_weather_tool", False):
            tools_list.extend(OPENAI_WEATHER_TOOLS)
            suffix_keys.append("weather")
        if getattr(llm, "enable_weibo_tool", False):
            tools_list.extend(OPENAI_WEIBO_TOOLS)
            suffix_keys.append("weibo")
        if getattr(llm, "enable_search_tool", False):
            tools_list.extend(OPENAI_SEARCH_TOOLS)
            suffix_keys.append("search")
        if getattr(llm, "enable_x_tool", False):
            tools_list.extend(OPENAI_X_TOOLS)
            suffix_keys.append("x")
        if suffix_keys:
            inject_tool_suffix_into_messages(
                cur_messages, build_tool_system_suffix(suffix_keys)
            )
        tools_param: Optional[List[Dict[str, Any]]] = (
            tools_list if tools_list else None
        )
        chat_id = base_message.chat.id
        sse: Optional[_TelegramSseRound] = None
        pre_tool_segments: List[str] = []
        # 跨每一轮 SSE（含多轮工具调用）共用同一列表：每轮 append_tool_exchange 按 tool_calls 顺序追加，
        # 全局顺序 = 第 1 轮工具… → 第 2 轮工具…，收尾时一次性 build_lutopia_internal_memory_appendix，不会丢中间轮。
        lutopia_stream_exec_log: List[Tuple[str, str, str]] = []
        lutopia_stream_turn_id = uuid.uuid4().hex

        def _merge_stream_outcome(outcome: _TelegramStreamOutcome) -> _TelegramStreamOutcome:
            merged_parts = [p for p in pre_tool_segments if p.strip()]
            if outcome.body_for_db.strip():
                merged_parts.append(outcome.body_for_db)
            merged_speech = "\n".join(merged_parts)
            cleaned_merged = schedule_update_memory_hits_and_clean_reply(merged_speech)
            ap = build_lutopia_internal_memory_appendix(lutopia_stream_exec_log)
            if ap and cleaned_merged.strip():
                body_for_db = cleaned_merged + "\n" + ap
            elif ap:
                body_for_db = ap
            else:
                body_for_db = cleaned_merged
            return _TelegramStreamOutcome(
                body_for_db=body_for_db,
                assistant_message_id=outcome.assistant_message_id,
                thinking=outcome.thinking,
                save_user=outcome.save_user,
            )

        async with create_lutopia_mcp_session() as lutopia_mcp_session:
            for _ in range(8):
                for attempt in range(2):
                    sse = await self._telegram_stream_llm_one_sse_round(
                        llm,
                        cur_messages,
                        base_message,
                        bot,
                        tools=tools_param,
                        cacheable_ratio=cacheable_ratio,
                    )
                    fin = sse.done_payload or {}
                    if fin.get("guard_refusal_abort") and attempt == 0:
                        cur_messages = append_guard_hint_to_last_user_message(
                            cur_messages, TELEGRAM_GUARD_PROMPT_APPEND
                        )
                        logger.warning(
                            "CedarClio Guard：同步链路流式掐断或拒答，正在静默重试一次（含 Lutopia 工具）"
                        )
                        continue
                    break
                assert sse is not None
                fin = sse.done_payload or {}
                tc = fin.get("tool_calls")
                if isinstance(tc, list) and len(tc) > 0:
                    rc = (sse.raw_content or "").strip()
                    if rc:
                        pre_tool_segments.append(rc)
                        await self._telegram_lutopia_send_partial_user_text(
                            bot,
                            chat_id,
                            rc,
                        )

                    async def _lutopia_on_start(tool_name: str) -> None:
                        await self._telegram_lutopia_notify_tool_before(
                            bot, chat_id, tool_name
                        )

                    async def _lutopia_on_done(tool_name: str, out: str) -> None:
                        await self._telegram_lutopia_notify_tool_after(
                            bot, chat_id, tool_name, out
                        )

                    await append_tool_exchange_to_messages(
                        cur_messages,
                        sse.raw_content or "",
                        tc,
                        on_tool_start=_lutopia_on_start,
                        on_tool_done=_lutopia_on_done,
                        execution_log=lutopia_stream_exec_log,
                        mcp_session=lutopia_mcp_session,
                        session_id=session_id,
                        turn_id=lutopia_stream_turn_id,
                        platform=Platform.TELEGRAM,
                        user_message_id=user_message_id,
                    )
                    continue
                outcome = await self._telegram_finalize_sse_round_outcome(
                    sse, base_message, bot
                )
                return _merge_stream_outcome(outcome)
            assert sse is not None
            logger.warning("Lutopia 工具轮次已达上限（8），按末轮 SSE 结果收尾")
            outcome = await self._telegram_finalize_sse_round_outcome(
                sse, base_message, bot
            )
            return _merge_stream_outcome(outcome)

    async def _telegram_lutopia_notify_tool_before(
        self, bot: Any, chat_id: int, tool_name: str
    ) -> None:
        """工具执行前仅发送 typing，不向用户推送工具状态行。"""
        del tool_name
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as e:
            logger.warning(
                "Lutopia send_chat_action(typing) 失败 chat_id=%s: %s",
                chat_id,
                exc_detail(e),
            )

    async def _telegram_lutopia_notify_tool_after(
        self, bot: Any, chat_id: int, tool_name: str, result_json: str
    ) -> None:
        """工具结束后发一行纯文本状态（无 HTML / blockquote）。"""
        disp = self._telegram_lutopia_tool_display_name(tool_name)
        ok = True
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                err = parsed.get("error")
                ok = err is None or str(err).strip() == ""
        except json.JSONDecodeError:
            # get_weather 等返回自然语言而非 JSON 时视为成功（有内容即可）
            ok = bool((result_json or "").strip())
        if not ok:
            logger.warning(
                "[notify_tool_after] tool failure tool_name=%s result_json=%s",
                tool_name,
                (result_json or "")[:800],
            )
        text = f"✅ 已调用{disp}" if ok else f"❌ {disp}调用失败"
        if len(text) > 4096:
            suf = _TELEGRAM_PLAIN_TRUNC_SUFFIX
            text = text[: 4096 - len(suf)] + suf
        try:
            stack = "".join(traceback.format_stack())
            logging.warning(f"send called from: {stack}")
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=None
            )
        except Exception as e:
            logger.warning(
                "Lutopia 工具结束提示发送失败 chat_id=%s: %s",
                chat_id,
                exc_detail(e),
            )

    async def _telegram_lutopia_send_partial_user_text(
        self, bot: Any, chat_id: int, text: str
    ) -> None:
        """
        工具轮次之间的口播：与最终正文一致——先按 ``|||`` / ``[meme:…]`` / 换行二级分段，
        再对每段走 Markdown→HTML；不得在分段前对整段做空白折叠（否则会吃掉用于拆段的换行）。
        """
        raw = strip_lutopia_user_facing_assistant_text((text or "").strip())
        if not raw:
            return
        try:
            segments, _ = await parse_telegram_segments_with_memes_async(raw)
        except Exception as e:
            logger.warning(
                "Lutopia 口播分段失败 chat_id=%s: %s",
                chat_id,
                exc_detail(e),
            )
            return
        if not segments:
            return
        try:
            await self._telegram_deliver_ordered_segments(
                bot, chat_id, segments, base_message=None
            )
        except Exception as e:
            logger.warning(
                "Lutopia 口播消息发送失败 chat_id=%s: %s",
                chat_id,
                exc_detail(e),
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
        trunc_m = _TELEGRAM_PLAIN_TRUNC_SUFFIX
        reply = strip_lutopia_user_facing_assistant_text(reply or "")
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

    def _parse_image_count_from_text(self, text: str) -> int:
        """解析前X张图/上X张图，返回X（1-9），无匹配返回0。"""
        m = _PARSE_IMAGE_COUNT_RE.search(text or "")
        if not m:
            return 0
        v = m.group(1)
        if v in _PARSE_IMAGE_COUNT_MAP:
            return _PARSE_IMAGE_COUNT_MAP[v]
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    async def _recent_image_caption_hint(
        self,
        session_id: str,
        current_text: str,
        *,
        limit: int = 3,
    ) -> str:
        rows = list(reversed(await get_recent_image_messages(session_id, limit=limit)))
        lines: List[str] = []
        for idx, row in enumerate(rows, start=1):
            cap = (row.get("image_caption") or row.get("content") or "").strip()
            if not cap or cap in (VISION_FAIL_CAPTION_SHORT, VISION_FAIL_CAPTION_TIMEOUT):
                continue
            created = str(row.get("created_at") or "").strip()
            lines.append(f"{idx}. {created} {cap[:260]}")
        if not lines:
            return ""
        return (
            "\n\n[系统提示：用户这轮可能在追问近期图片。以下为近期图片摘要，"
            "顺序从远到近，最后一条是最近一张：\n"
            + "\n".join(lines)
            + "]"
        )

    async def _load_recent_telegram_image_payloads(
        self,
        bot,
        session_id: str,
        *,
        limit: int = 3,
        exclude_file_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按 platform_file_id 临时下载近期图片并转为 LLM image payload；失败静默跳过。"""
        if bot is None:
            return []
        excluded = {str(x) for x in (exclude_file_ids or set()) if x}
        rows = await get_recent_image_messages(session_id, limit=limit)
        out: List[Dict[str, Any]] = []
        for row in rows:
            fid = row.get("platform_file_id")
            if not fid or str(fid) in excluded:
                continue
            try:
                tg_file = await bot.get_file(fid)
                try:
                    data = await tg_file.download_as_bytearray()
                except AttributeError:
                    bio = io.BytesIO()
                    await tg_file.download_to_memory(bio)
                    data = bio.getvalue()
                path = (tg_file.file_path or "").lower()
                mime = "image/jpeg"
                if path.endswith(".png"):
                    mime = "image/png"
                elif path.endswith(".webp"):
                    mime = "image/webp"
                elif path.endswith(".gif"):
                    mime = "image/gif"
                caption = (row.get("image_caption") or "").strip()
                if caption in (VISION_FAIL_CAPTION_SHORT, VISION_FAIL_CAPTION_TIMEOUT):
                    caption = (row.get("content") or "").strip()
                idx = len(out) + 1
                out.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(bytes(data)).decode("ascii"),
                        "mime_type": mime,
                        "caption": caption,
                        "label": f"历史图片{idx}（从远到近，最后一张是上一张可用图片）",
                        "platform_file_id": fid,
                    }
                )
            except Exception as e:
                logger.debug("近期图片下载失败，跳过 file_id=%s: %s", fid, exc_detail(e))
        return out

    async def _generate_reply_from_buffer(
        self,
        session_id: str,
        combined_raw: str,
        combined_content: str,
        user_id: str,
        chat_id: str,
        message_id: str,
        buffer_messages: List[Dict[str, Any]],
        images: Optional[List[Dict[str, Any]]] = None,
        text_for_llm: Optional[str] = None,
        base_message=None,
        bot=None,
        persist_user: bool = True,
    ) -> _BufferGenResult:
        """从缓冲区合并的消息流式生成回复（思维链 + 正文 ||| 分条）。用户消息在调用上游模型之前落库，避免模型报错时丢失。"""
        try:
            llm = await LLMInterface.create(
                config_type="vision" if images else "chat"
            )
            if llm.character_id is None:
                logger.error(
                    "persona_id 缺失，无法处理消息 session_id=%s config_type=%s",
                    session_id, "vision" if images else "chat",
                )
                if bot:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="配置缺失,请检查 mini app api_configs",
                    )
                return _BufferGenResult(
                    reply="",
                    character_id=None,
                    persist_assistant=False,
                )
            # 在调用上游模型之前落库用户合并消息，避免 HTTP 4xx/5xx、超时等导致「用户话被吞」
            try:
                has_img = bool(images)
                media_t = ordered_media_type_from_buffer(buffer_messages)
                platform_file_id = None
                for bm in buffer_messages:
                    if bm.get("platform_file_id"):
                        platform_file_id = bm.get("platform_file_id")
                        break
                    ip = bm.get("image_payload") or {}
                    if isinstance(ip, dict) and ip.get("platform_file_id"):
                        platform_file_id = ip.get("platform_file_id")
                        break
                user_row_id = None
                if persist_user:
                    user_row_id = await save_message(
                        session_id=session_id,
                        role="user",
                        content=combined_raw,
                        user_id=user_id,
                        channel_id=chat_id,
                        message_id=message_id,
                        character_id=llm.character_id,
                        platform=Platform.TELEGRAM,
                        media_type=media_t,
                        image_caption=None,
                        vision_processed=0 if has_img else 1,
                        platform_file_id=platform_file_id,
                        is_summarized=_telegram_user_content_error_fallback_is_summarized(
                            combined_raw
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
            except Exception as persist_u:
                logger.exception(
                    "缓冲路径：用户消息落库失败 session_id=%s: %s",
                    session_id,
                    exc_detail(persist_u),
                )

            is_anthropic = llm._use_anthropic_messages_api()
            lutopia_on = bool(getattr(llm, "enable_lutopia", False))
            weather_on = bool(getattr(llm, "enable_weather_tool", False))
            weibo_on = bool(getattr(llm, "enable_weibo_tool", False))
            search_on = bool(getattr(llm, "enable_search_tool", False))
            x_on = bool(getattr(llm, "enable_x_tool", False))
            oral = (
                lutopia_on or weather_on or weibo_on or search_on or x_on
            ) and not is_anthropic
            llm_images = images or None
            if bot is not None:
                current_text = text_for_llm or combined_content
                recent_image_count = self._parse_image_count_from_text(current_text)
                if recent_image_count > 0:
                    image_hint = await self._recent_image_caption_hint(
                        session_id,
                        current_text,
                        limit=recent_image_count,
                    )
                    if image_hint:
                        text_for_llm = (
                            (current_text or "").strip() + image_hint
                        ).strip()
                    current_file_ids: Set[str] = set()
                    for ip in images or []:
                        if isinstance(ip, dict) and ip.get("platform_file_id"):
                            current_file_ids.add(str(ip.get("platform_file_id")))
                    recent_images = await self._load_recent_telegram_image_payloads(
                        bot,
                        session_id,
                        limit=recent_image_count,
                        exclude_file_ids=current_file_ids,
                    )
                    if recent_images:
                        recent_images = list(reversed(recent_images))
                        llm_images = [*recent_images, *(images or [])]
                        current_count = len(images or [])
                        history_hint = (
                            "\n\n[系统提示：已临时附上近期图片历史。图片顺序："
                            f"前 {len(recent_images)} 张为历史图片（从远到近，最后一张是「上一张/刚才那张」），"
                            f"后 {current_count} 张为用户本轮刚发的图片。]"
                        )
                        text_for_llm = ((text_for_llm or combined_content or "").strip() + history_hint).strip()
            context = await build_context(
                session_id,
                combined_content,
                images=llm_images,
                llm_user_text=text_for_llm or None,
                telegram_segment_hint=True,
                tool_oral_coaching=oral,
                exclude_message_id=user_row_id if 'user_row_id' in locals() else None,
            )
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            cacheable_ratio = context.get("cacheable_ratio", 0.0)
            if not messages:
                messages = [{"role": "user", "content": combined_content}]

            if is_anthropic:
                llm_path = "anthropic_prefetch → generate_with_context_and_tracking（无 tools）"
            elif lutopia_on or weather_on or weibo_on or search_on or x_on:
                llm_path = (
                    "openai_compatible → _telegram_stream_thinking_and_reply_with_lutopia "
                    "→ generate_stream(tools=Lutopia±天气±微博±搜索±X)（persona 工具开关）"
                )
            else:
                llm_path = (
                    "openai_compatible → _telegram_stream_thinking_and_reply "
                    "→ generate_stream(tools=None)（未启用任何工具）"
                )
            logger.info(
                "[TG路径追踪] _generate_reply_from_buffer 已建 LLM：session_id=%s model=%s "
                "api_base=%s character_id=%r enable_lutopia=%s is_anthropic=%s → %s",
                session_id,
                llm.model_name,
                (llm.api_base or "")[:80],
                llm.character_id,
                lutopia_on,
                is_anthropic,
                llm_path,
            )

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

            if llm._use_anthropic_messages_api():
                cur_m: List[Dict[str, Any]] = messages
                llm_resp: Any = None
                last_hit = False
                for attempt in range(2):
                    snap = cur_m
                    llm_resp = await asyncio.to_thread(
                        lambda m=snap: llm.generate_with_context_and_tracking(
                            m, platform=Platform.TELEGRAM,
                            cacheable_ratio=cacheable_ratio,
                        )
                    )
                    raw_txt = llm_resp.content or ""
                    safe, hit = truncate_accumulator_at_first_refusal(raw_txt)
                    last_hit = hit
                    llm_resp.content = safe
                    if hit and attempt == 0:
                        cur_m = append_guard_hint_to_last_user_message(
                            messages, TELEGRAM_GUARD_PROMPT_APPEND
                        )
                        logger.warning(
                            "CedarClio Guard（Anthropic）：拒答片段，正在静默重试一次"
                        )
                        continue
                    break
                assert llm_resp is not None
                if last_hit and not (llm_resp.content or "").strip():
                    llm_resp.content = _TELEGRAM_GUARD_ROLEPLAY_FALLBACK

                outcome = await self._telegram_deliver_prefetched_llm_response(
                    llm_resp, base_message, bot
                )
            else:
                if getattr(llm, "enable_lutopia", False) or getattr(
                    llm, "enable_weather_tool", False
                ) or getattr(llm, "enable_weibo_tool", False) or getattr(
                    llm, "enable_search_tool", False
                ) or getattr(llm, "enable_x_tool", False):
                    outcome = await self._telegram_stream_thinking_and_reply_with_lutopia(
                        llm,
                        messages,
                        base_message,
                        bot,
                        session_id=session_id,
                        user_message_id=user_row_id if 'user_row_id' in locals() else None,
                        cacheable_ratio=cacheable_ratio,
                    )
                else:
                    outcome = await self._telegram_stream_thinking_and_reply(
                        llm, messages, base_message, bot,
                        cacheable_ratio=cacheable_ratio,
                    )

            # 用户消息已在上方先行落库；此处仅处理助手侧 persist 标记
            persist = bool(outcome.body_for_db.strip())
            return _BufferGenResult(
                outcome.body_for_db,
                llm.character_id,
                persist,
                thinking=outcome.thinking,
                assistant_message_id=outcome.assistant_message_id,
            )

        except ValueError as e:
            logger.error("LLM 配置错误: %s", exc_detail(e))
            return _BufferGenResult(
                "抱歉，LLM 配置有问题，请检查 API 密钥设置。", None, False
            )
        except requests.exceptions.ReadTimeout as e:
            logger.error(
                "LLM 读超时 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            if images:
                return _BufferGenResult(
                    "抱歉，模型响应读超时。带图请求往往更慢；"
                    "若仍失败请在 .env 提高 LLM_VISION_TIMEOUT（默认 180 秒）或 LLM_TIMEOUT。",
                    None,
                    False,
                )
            return _BufferGenResult(
                _telegram_user_visible_model_error(e, stream_chunk_timeout=False),
                None,
                False,
            )
        except requests.exceptions.ConnectTimeout as e:
            logger.error(
                "LLM 连接超时 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _BufferGenResult(
                _telegram_user_visible_model_error(e, stream_chunk_timeout=False),
                None,
                False,
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
                "上下文长或上游慢时可调 LLM_TIMEOUT）"
            )
            return _BufferGenResult(
                "抱歉，模型响应超时。可调大 .env 中的 LLM_TIMEOUT；"
                f"Telegram 流式还可调 LLM_STREAM_READ_TIMEOUT（默认 {config.LLM_STREAM_READ_TIMEOUT} 秒）。",
                None,
                False,
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "缓冲区路径模型 HTTP 异常 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _BufferGenResult(
                _telegram_user_visible_model_error(e, stream_chunk_timeout=False),
                None,
                False,
            )
        except TelegramNetworkError as e:
            logger.warning(
                "缓冲区路径：发往 Telegram 失败（多为网络或 TELEGRAM_PROXY）"
                " session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _BufferGenResult(
                "抱歉，当前连不上 Telegram 服务器（网络或代理异常）。"
                "请确认本机网络与 .env 中 TELEGRAM_PROXY 可用后重试。",
                None,
                False,
            )
        except Exception as e:
            logger.exception(
                "缓冲区生成回复异常 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _BufferGenResult(
                "抱歉，生成回复时发生未预期错误。请稍后再试；详情见服务日志。",
                None,
                False,
            )

    @staticmethod
    def _extract_reply_prefix(message) -> str:
        """若用户引用了某条消息，返回前缀提示字符串（发给 LLM，用户不可见）；否则返回空字符串。"""
        replied = getattr(message, "reply_to_message", None)
        if not replied:
            return ""
        text = (
            getattr(replied, "text", None) or getattr(replied, "caption", None) or ""
        ).strip()
        if not text:
            return ""
        _MAX_QUOTE = 200
        if len(text) > _MAX_QUOTE:
            text = text[:_MAX_QUOTE] + "…"
        from_user = getattr(replied, "from_user", None)
        is_bot = getattr(from_user, "is_bot", False) if from_user else False
        if is_bot:
            return (
                f"[系统上下文：用户正在回复 AI 的消息「{text}」。"
                "此信息只用于理解上下文，禁止在回答中复述这段括号内容。]\n\n"
            )
        return (
            f"[系统上下文：用户正在回复自己之前的消息「{text}」。"
            "此信息只用于理解上下文，禁止在回答中复述这段括号内容。]\n\n"
        )

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
        
        若用户使用了 Telegram 引用回复，则在 content 前拼接引用上下文提示
        （仅发给 LLM，用户不可见，历史记录存原始 content）。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
            session_id: 会话ID
            message: 消息对象
            content: 消息内容
            user_id: 用户ID
            message_id: 消息ID
        """
        reply_prefix = self._extract_reply_prefix(message)
        content_for_llm = reply_prefix + content if reply_prefix else content
        await self._message_buffer.add_to_buffer(
            session_id,
            {
                "update": update,
                "context": context,
                "message": message,
                "content": content_for_llm,   # 包含引用前缀，供 LLM 使用
                "raw_content": content,        # 原始消息，不含前缀，供落库使用
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
        combined_raw: str,
        combined_content: str,
        images: List[Dict[str, Any]],
        buffer_messages: List[Dict[str, Any]],
        text_for_llm: str,
    ) -> None:
        """缓冲到期后由 MessageBuffer 调用：typing、流式生成、思维链消息与正文 ||| 分条回复。"""
        logger.info(
            "[TG路径追踪] _flush_buffered_messages 开始 session_id=%s 合并条数=%s combined_len=%s",
            session_id,
            len(buffer_messages),
            len(combined_content or ""),
        )
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
                "send_chat_action 失败 chat_id=%s（略过「正在输入」，仍继续生成）。"
                " 多为连不上 api.telegram.org，请检查 TELEGRAM_PROXY。详情: %s",
                base_message.chat.id,
                exc_detail(e),
            )

        gen = await self._generate_reply_from_buffer(
            session_id=session_id,
            combined_raw=combined_raw,
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
        # 须与 _generate_reply 非缓冲路径一致：无 Telegram 首条正文 message_id 时仍落库（用合成 id），
        # 否则 HTML 净化后为空导致未走 reply_text(HTML)、first_mid 为空，仅下方纯文本兜底发出时不会入库。
        if gen.persist_assistant and gen.reply.strip():
            assistant_mid = gen.assistant_message_id or f"ai_{base_message_id}"
            await save_message(
                session_id=session_id,
                role="assistant",
                content=gen.reply,
                user_id=base_user_id,
                channel_id=str(base_message.chat.id),
                message_id=assistant_mid,
                character_id=gen.character_id,
                platform=Platform.TELEGRAM,
                thinking=gen.thinking,
            )
            if self._is_group_message(base_message):
                await self._relay_group_assistant_message(
                    chat_id=str(base_message.chat.id),
                    message_id=assistant_mid,
                    content=gen.reply,
                    character_id=gen.character_id,
                    thinking=gen.thinking,
                )
        if gen.reply and not gen.assistant_message_id:
            try:
                stack = "".join(traceback.format_stack())
                logging.warning(f"send called from: {stack}")
                await self._send_text_near_base(
                    base_message,
                    base_context.bot,
                    telegram_send_text_collapse(
                        strip_lutopia_user_facing_assistant_text(gen.reply)
                    ),
                    parse_mode=None,
                )
            except TelegramNetworkError as e:
                logger.warning(
                    "缓冲收尾：向用户发送说明失败（Telegram 仍不可达）: %s",
                    exc_detail(e),
                )
    
    async def _generate_reply(
        self,
        session_id: str,
        content: str,
        user_id: str,
        chat_id: str,
        message_id: str,
        telegram_bot: Optional[Any] = None,
    ) -> Optional[str]:
        """
        生成回复消息（非缓冲路径；主会话走缓冲）。
        传入 telegram_bot 时：发送思维链、去掉 [meme:…] 后的正文与检索到的表情包。
        """
        try:
            llm = await LLMInterface.create(config_type="vision")
            if llm.character_id is None:
                logger.error("persona_id 缺失，无法处理消息 session_id=%s", session_id)
                if telegram_bot:
                    await telegram_bot.send_message(
                        chat_id=chat_id,
                        text="配置缺失,请检查 mini app api_configs",
                    )
                return None
            cid = int(chat_id)
            oral = (
                bool(getattr(llm, "enable_lutopia", False))
                or bool(getattr(llm, "enable_weather_tool", False))
                or bool(getattr(llm, "enable_weibo_tool", False))
                or bool(getattr(llm, "enable_search_tool", False))
                or bool(getattr(llm, "enable_x_tool", False))
            ) and not llm._use_anthropic_messages_api()
            logger.info(
                "oral=%s lutopia=%s weather=%s weibo=%s search=%s x=%s anthropic=%s",
                oral,
                getattr(llm, "enable_lutopia", False),
                getattr(llm, "enable_weather_tool", False),
                getattr(llm, "enable_weibo_tool", False),
                getattr(llm, "enable_search_tool", False),
                getattr(llm, "enable_x_tool", False),
                llm._use_anthropic_messages_api(),
            )
            context = await build_context(
                session_id,
                content,
                telegram_segment_hint=telegram_bot is not None,
                tool_oral_coaching=oral,
                exclude_message_id=user_row_id if 'user_row_id' in locals() else None,
            )
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            cacheable_ratio = context.get("cacheable_ratio", 0.0)
            if not messages:
                messages = [{"role": "user", "content": content}]

            lutopia_appendix = ""
            if oral:
                if telegram_bot is not None:

                    async def _lutopia_on_start(n: str) -> None:
                        await self._telegram_lutopia_notify_tool_before(
                            telegram_bot, cid, n
                        )

                    async def _lutopia_on_done(n: str, out: str) -> None:
                        await self._telegram_lutopia_notify_tool_after(
                            telegram_bot, cid, n, out
                        )

                    async def _partial(txt: str) -> None:
                        await self._telegram_lutopia_send_partial_user_text(
                            telegram_bot, cid, txt
                        )

                    outcome = await complete_with_lutopia_tool_loop(
                        llm,
                        messages,
                        platform=Platform.TELEGRAM,
                        on_tool_start=_lutopia_on_start,
                        on_tool_done=_lutopia_on_done,
                        on_assistant_partial_text=_partial,
                        session_id=session_id,
                        user_message_id=user_row_id if 'user_row_id' in locals() else None,
                    )
                else:
                    outcome = await complete_with_lutopia_tool_loop(
                        llm,
                        messages,
                        platform=Platform.TELEGRAM,
                        session_id=session_id,
                        user_message_id=user_row_id if 'user_row_id' in locals() else None,
                    )
                llm_resp = outcome.response
                lutopia_appendix = outcome.behavior_appendix or ""
                cleaned = schedule_update_memory_hits_and_clean_reply(
                    outcome.aggregated_assistant_text
                )
            else:

                def _call() -> Any:
                    return llm.generate_with_context_and_tracking(
                        messages, platform=Platform.TELEGRAM,
                        cacheable_ratio=cacheable_ratio,
                    )

                llm_resp = await asyncio.to_thread(_call)
                cleaned = schedule_update_memory_hits_and_clean_reply(llm_resp.content or "")
            think_plain = (llm_resp.thinking or "").strip()
            if telegram_bot and think_plain:
                html_th = self._telegram_thinking_blockquote_html(think_plain)
                try:
                    stack = "".join(traceback.format_stack())
                    logging.warning(f"send called from: {stack}")
                    await telegram_bot.send_message(
                        chat_id=cid, text=html_th, parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(
                        "发送思维链失败 chat_id=%s: %s",
                        cid,
                        exc_detail(e),
                    )

            segments, body_for_db = await parse_telegram_segments_with_memes_async(
                cleaned
            )
            meme_sent = False
            if telegram_bot and segments:
                _, meme_sent, _ = await self._telegram_deliver_ordered_segments(
                    telegram_bot, cid, segments, base_message=None
                )
            if body_for_db.strip():
                reply = body_for_db
            elif meme_sent:
                reply = "[表情包]"
            else:
                reply = ""
            await save_message(
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
            assistant_content = reply
            if lutopia_appendix:
                assistant_content = (
                    (reply.rstrip() + "\n" + lutopia_appendix)
                    if (reply or "").strip()
                    else lutopia_appendix
                )
            await save_message(
                session_id=session_id,
                role="assistant",
                content=assistant_content,
                user_id=user_id,
                channel_id=chat_id,
                message_id=f"ai_{message_id}",
                character_id=llm.character_id,
                platform=Platform.TELEGRAM,
                thinking=think_plain,
            )
            logger.info(
                "为 Telegram 用户 %s 生成回复，context 消息数量: %s",
                user_id,
                len(messages),
            )
            logger.debug("System prompt 长度: %s", len(system_prompt))
            asyncio.create_task(trigger_micro_batch_check(session_id))
            return reply

        except ValueError as e:
            logger.error("LLM 配置错误: %s", exc_detail(e))
            return "抱歉，LLM 配置有问题，请检查 API 密钥设置。"
        except requests.exceptions.ReadTimeout as e:
            logger.error(
                "_generate_reply 读超时 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _telegram_user_visible_model_error(e, stream_chunk_timeout=False)
        except requests.exceptions.ConnectTimeout as e:
            logger.error(
                "_generate_reply 连接超时 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _telegram_user_visible_model_error(e, stream_chunk_timeout=False)
        except requests.exceptions.Timeout:
            logger.error(
                "_generate_reply 请求超时 session_id=%s（其余 Timeout）",
                session_id,
            )
            return (
                "抱歉，模型响应超时。可调大 .env 中的 LLM_TIMEOUT；"
                f"Telegram 流式还可调 LLM_STREAM_READ_TIMEOUT（默认 {config.LLM_STREAM_READ_TIMEOUT} 秒）。"
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "_generate_reply 模型 HTTP 异常 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return _telegram_user_visible_model_error(e, stream_chunk_timeout=False)
        except TelegramNetworkError as e:
            logger.warning(
                "_generate_reply：发往 Telegram 失败 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return (
                "抱歉，当前连不上 Telegram（网络或 TELEGRAM_PROXY）。"
                "请检查代理与网络后重试。"
            )
        except Exception as e:
            logger.exception(
                "_generate_reply 异常 session_id=%s: %s",
                session_id,
                exc_detail(e),
            )
            return "抱歉，生成回复时发生未预期错误。请稍后再试；详情见服务日志。"

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
        raw = await get_assistant_content_for_platform_message_id(
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
            await save_message(
                session_id=session_id,
                role="user",
                content=content,
                user_id=uid,
                channel_id=str(chat_id),
                message_id=f"reaction_{update.update_id}",
                character_id=await _character_id_for_reaction_save(),
                platform=Platform.TELEGRAM,
                media_type="reaction",
                vision_processed=1,
            )
            asyncio.create_task(trigger_micro_batch_check(session_id))
        except Exception as e:
            logger.error("保存反应消息失败: %s", exc_detail(e))
    
    async def setup_webhook(self) -> None:
        """
        初始化 Application 并 start，供 FastAPI webhook 接收更新；不启动 polling。
        """
        try:
            token = config.TELEGRAM_BOT_TOKEN
            if not token:
                logger.warning("TELEGRAM_BOT_TOKEN 未设置，Telegram 机器人将不会启动")
                return

            logger.info("启动 Telegram 机器人（webhook 模式）...")

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
            self.application.add_handler(CommandHandler("silent", self.silent_command))
            self.application.add_handler(CommandHandler("wake", self.wake_command))

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

            await self.application.initialize()
            _bot_cmds = [
                BotCommand("start", "显示欢迎信息"),
                BotCommand("help", "显示帮助"),
                BotCommand("model", "当前模型信息"),
                BotCommand("clear", "清除对话历史"),
                BotCommand("rescanpic", "重新识别贴纸图片"),
                BotCommand("silent", "群聊静默"),
                BotCommand("wake", "唤醒群聊回复"),
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

            logger.info("Telegram 机器人已就绪（webhook，无 polling）")

            # 通知 dashboard 模块：Telegram 已上线
            try:
                from api.dashboard import set_bot_online
                set_bot_online("telegram", True)
                logger.info("已更新 Telegram 在线状态 → True")
            except Exception as e:
                logger.warning(
                    "更新 Telegram 在线状态失败: %s", exc_detail(e)
                )

        except Exception as e:
            logger.exception(
                "启动 Telegram 机器人时出错: %s", exc_detail(e)
            )
            raise

    async def run_async(self) -> None:
        """
        兼容旧名：等价于 setup_webhook()，不再启动 polling。
        """
        await self.setup_webhook()

    async def run(self):
        """
        单独运行本模块时：完成 webhook 侧初始化后阻塞，避免进程立即退出。
        正常部署请使用 main.py 启动 FastAPI 接收 webhook。
        """
        try:
            await self.setup_webhook()
            if not getattr(self, "application", None):
                return
            stop_event = asyncio.Event()
            await stop_event.wait()
        except Exception as e:
            logger.exception(
                "Telegram 机器人 run() 失败: %s", exc_detail(e)
            )
            raise
    
    async def stop(self):
        """
        停止 Telegram 机器人。
        """
        if hasattr(self, 'application'):
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram 机器人已停止")


_webhook_telegram_bot: Optional["TelegramBot"] = None
_webhook_setup_done: bool = False


async def setup_telegram_webhook_app() -> None:
    """由 main 在 FastAPI 收消息前调用：构建 Application、注册 handler、initialize/start。"""
    global _webhook_telegram_bot, _webhook_setup_done
    if _webhook_setup_done:
        return
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN 未设置，跳过 Telegram webhook 初始化")
        _webhook_setup_done = True
        return
    bot = TelegramBot()
    await bot.setup_webhook()
    _webhook_telegram_bot = bot
    _webhook_setup_done = True


async def process_update(update_data: dict) -> None:
    """供 FastAPI webhook 后台任务调用：将 JSON update 交给 Application 处理。"""
    bot = _webhook_telegram_bot
    app = getattr(bot, "application", None) if bot else None
    if app is None:
        logger.warning("Telegram Application 未初始化，忽略 update")
        return
    try:
        update = Update.de_json(update_data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.exception("process_update 失败: %s", exc_detail(e))


async def handle_peer_group_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """供 /api/peer/group-message 调用：把 peer relay 消息交给当前 TelegramBot。"""
    bot = _webhook_telegram_bot
    if bot is None:
        logger.warning("TelegramBot 未初始化，忽略 peer relay 消息")
        return {"status": "ignored_no_telegram_bot"}
    return await bot.handle_peer_group_message(payload)


async def shutdown_telegram_webhook_app() -> None:
    global _webhook_telegram_bot, _webhook_setup_done
    if _webhook_telegram_bot is not None:
        try:
            await _webhook_telegram_bot.stop()
        except Exception as e:
            logger.warning("shutdown_telegram_webhook_app: %s", exc_detail(e))
        _webhook_telegram_bot = None
    _webhook_setup_done = False


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
        logger.exception(
            "run_telegram_bot 失败: %s", exc_detail(e)
        )
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
        logger.error("配置验证失败: %s", exc_detail(e))
        print(f"错误: {e}")
        print("请检查 .env 文件中的配置项")
    except Exception as e:
        logger.exception("机器人 main 运行失败: %s", exc_detail(e))
        print(f"错误: {e}")


if __name__ == "__main__":
    """Telegram 机器人模块测试入口。"""
    main()
