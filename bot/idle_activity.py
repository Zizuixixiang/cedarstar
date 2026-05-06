"""
AI 自主活动（Idle Activity）模块。

按固定频率检查是否长时间无人发言，满足条件后触发一次「自由活动」回复。
"""

import logging
import random
from datetime import datetime, timezone
from typing import Any, Optional

import pytz

from config import Platform
from llm.llm_interface import LLMInterface, complete_with_lutopia_tool_loop
from memory.context_builder import build_context
from memory.database import save_message

logger = logging.getLogger(__name__)

# 自主活动档位：只绑定“每次检查命中率”
IDLE_LEVELS = {
    "low": {"trigger_prob": 0.3},
    "mid": {"trigger_prob": 0.6},
    "high": {"trigger_prob": 1.0},
}

_IDLE_TRIGGER_TEXT = (
    "[IDLE_TRIGGER] 南杉有一段时间没来，你可以趁现在自由活动一下。"
    "可以做的事：可以翻翻记忆，整理记忆；可以去lutopia论坛刷帖、回帖、发帖等等；可以去X搜索感兴趣的内容、发推、和人类互动；"
    "可以查询天气、热搜、随便搜搜感兴趣的；可以给南杉留言（不要发语音和表情）；也可以什么也不做。"
)
_SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")


def _to_aware_utc(ts: Any) -> Optional[datetime]:
    """把数据库时间统一转成 UTC aware datetime，便于做分钟差计算。"""
    if ts is None:
        return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        # 数据库返回无时区时间时，按东八区本地时间解释，再转换到 UTC。
        return _SHANGHAI_TZ.localize(ts).astimezone(timezone.utc)
    return ts.astimezone(timezone.utc)


def _is_truthy(v: Optional[str]) -> bool:
    """统一解析 config 里的布尔字符串。"""
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _format_shanghai_timestamp(ts: Any) -> Optional[str]:
    """将时间格式化为东八区可读文本：YYYY年M月D日 HH:MM。"""
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        local_dt = _SHANGHAI_TZ.localize(ts)
    else:
        local_dt = ts.astimezone(_SHANGHAI_TZ)
    return (
        f"{local_dt.year}年{local_dt.month}月{local_dt.day}日 "
        f"{local_dt.hour:02d}:{local_dt.minute:02d}"
    )


async def check_and_trigger(telegram_bot_instance, db) -> None:
    """检查是否满足 idle 条件，满足则触发自主活动。"""
    enabled = await db.get_config("idle_activity_enabled", "false")
    if not _is_truthy(enabled):
        return

    # 仅在东八区设定时段内允许触发
    now_sh = datetime.now(_SHANGHAI_TZ)
    try:
        start_hour = int(await db.get_config("idle_activity_start_hour", "8"))
        end_hour = int(await db.get_config("idle_activity_end_hour", "23"))
    except (TypeError, ValueError):
        start_hour, end_hour = 8, 23
    if now_sh.hour < start_hour or now_sh.hour > end_hour:
        return

    level_name = str(await db.get_config("idle_activity_level", "mid") or "mid").strip().lower()
    level = IDLE_LEVELS.get(level_name, IDLE_LEVELS["mid"])

    # 查最近一条用户消息时间
    async with db.pool.acquire() as conn:
        last_user_row = await conn.fetchrow(
            "SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
        )
    if not last_user_row or last_user_row["created_at"] is None:
        return

    now_utc = datetime.now(timezone.utc)
    last_user_msg_at = _to_aware_utc(last_user_row["created_at"])
    if last_user_msg_at is None:
        return
    idle_min = (now_utc - last_user_msg_at).total_seconds() / 60.0

    # 触发阈值（分钟）由 Mini App 可调
    try:
        threshold_min = float(
            max(1, min(1440, int(await db.get_config("idle_activity_threshold_min", "10"))))
        )
    except (TypeError, ValueError):
        threshold_min = 10.0

    # 两次自主活动最小间隔（分钟）由 Mini App 可调
    try:
        cooldown_min = float(
            max(1, min(1440, int(await db.get_config("idle_activity_cooldown_min", "120"))))
        )
    except (TypeError, ValueError):
        cooldown_min = 120.0

    # 冷却时间：首次触发视为超大值，直接允许进入下一步
    last_triggered_raw = await db.get_config("idle_activity_last_triggered_at", "")
    since_last = 999999.0
    if str(last_triggered_raw or "").strip():
        try:
            parsed = datetime.fromisoformat(str(last_triggered_raw))
            parsed_utc = _to_aware_utc(parsed)
            if parsed_utc is not None:
                since_last = (now_utc - parsed_utc).total_seconds() / 60.0
        except ValueError:
            since_last = 999999.0

    logger.debug(
        f"[idle] level={level_name} idle={idle_min:.1f}min "
        f"since_last={since_last:.1f}min prob={level['trigger_prob']}"
    )

    if idle_min < threshold_min:
        return
    if since_last < cooldown_min:
        return
    if random.random() > float(level["trigger_prob"]):
        return

    await trigger_idle_activity(telegram_bot_instance, db)


async def trigger_idle_activity(telegram_bot_instance, db) -> None:
    """执行一次自主活动：构建临时 trigger 上下文，生成并发送 assistant 文本。"""
    # 兼容请求里的 chat_id 语义：本项目 messages 表实际列名为 channel_id（即 Telegram chat_id）
    async with db.pool.acquire() as conn:
        chat_row = await conn.fetchrow(
            "SELECT channel_id FROM messages ORDER BY created_at DESC LIMIT 1"
        )
    if not chat_row or not chat_row["channel_id"]:
        return

    chat_id_str = str(chat_row["channel_id"])
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        logger.warning("idle activity 跳过：最新 channel_id 不是 Telegram chat_id (%s)", chat_id_str)
        return
    logger.info(f"[idle] activity triggered → chat_id={chat_id}")

    session_id = f"telegram_{chat_id_str}"
    llm = await LLMInterface.create(config_type="chat")
    if llm.character_id is None:
        logger.warning("idle activity 跳过：缺少 persona_id")
        return

    tool_oral = (
        bool(getattr(llm, "enable_lutopia", False))
        or bool(getattr(llm, "enable_weather_tool", False))
        or bool(getattr(llm, "enable_weibo_tool", False))
        or bool(getattr(llm, "enable_search_tool", False))
        or bool(getattr(llm, "enable_x_tool", False))
    ) and not llm._use_anthropic_messages_api()

    # 把“用户最后发言时间”注入本次 idle trigger，增强模型对时间感知。
    idle_trigger_text = _IDLE_TRIGGER_TEXT
    async with db.pool.acquire() as conn:
        last_user_row = await conn.fetchrow(
            "SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
        )
    if last_user_row and last_user_row.get("created_at") is not None:
        last_user_text = _format_shanghai_timestamp(last_user_row["created_at"])
        if last_user_text:
            idle_trigger_text = (
                f"{_IDLE_TRIGGER_TEXT}\n南杉最后一条消息在{last_user_text}。"
            )

    # 只把 idle trigger 注入本次推理，不写入 messages 表
    context = await build_context(
        session_id,
        idle_trigger_text,
        telegram_segment_hint=True,
        tool_oral_coaching=tool_oral,
    )
    messages = context.get("messages", []) or [{"role": "user", "content": idle_trigger_text}]

    outcome = await complete_with_lutopia_tool_loop(
        llm,
        messages,
        platform=Platform.TELEGRAM,
        session_id=session_id,
    )
    reply_text = (outcome.aggregated_assistant_text or outcome.response.content or "").strip()
    if not reply_text:
        return

    app = getattr(telegram_bot_instance, "application", None)
    tg_bot = getattr(app, "bot", None) if app is not None else None
    if tg_bot is None:
        logger.warning("idle activity 跳过：Telegram bot 实例未就绪")
        return

    sent = await tg_bot.send_message(chat_id=chat_id, text=reply_text)
    msg_id = getattr(sent, "message_id", None)
    db_content = f"【自主活动】{reply_text}"
    await save_message(
        role="assistant",
        content=db_content,
        session_id=session_id,
        user_id="system",
        channel_id=chat_id_str,
        message_id=str(msg_id) if msg_id is not None else f"idle_{int(datetime.now().timestamp())}",
        character_id=llm.character_id,
        platform=Platform.TELEGRAM,
        thinking=(outcome.response.thinking or "").strip() or None,
    )
    await db.set_config("idle_activity_last_triggered_at", datetime.now(timezone.utc).isoformat())

