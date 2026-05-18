"""
AI 自主活动（Idle Activity）模块。

按固定频率检查是否长时间无人发言，满足条件后触发一次「自由活动」回复。
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Optional

import pytz
import requests

from config import Platform, config as app_config
from llm.llm_interface import LLMInterface, complete_with_lutopia_tool_loop
from memory.context_builder import build_context
from memory.database import save_message

logger = logging.getLogger(__name__)

# 上游 429（限流）时的外层重试节奏；每次延迟与重试次数对齐。
_RATE_LIMIT_RETRY_DELAYS = (10, 10, 10)


def _is_rate_limit_exc(exc: BaseException, _seen: Optional[set] = None) -> bool:
    """识别 HTTP 429 限流：递归解包 ExceptionGroup / __cause__ / __context__。"""
    if exc is None:
        return False
    seen = _seen if _seen is not None else set()
    eid = id(exc)
    if eid in seen:
        return False
    seen.add(eid)
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and getattr(resp, "status_code", None) == 429:
            return True
    msg = str(exc) or ""
    if "429" in msg or "rate_limit_exceeded" in msg.lower() or "rate limit" in msg.lower():
        return True
    # asyncio.TaskGroup 抛出的 BaseExceptionGroup（Python 3.11+）
    sub_excs = getattr(exc, "exceptions", None)
    if isinstance(sub_excs, (list, tuple)):
        for sub in sub_excs:
            if _is_rate_limit_exc(sub, seen):
                return True
    if exc.__cause__ is not None and _is_rate_limit_exc(exc.__cause__, seen):
        return True
    if exc.__context__ is not None and _is_rate_limit_exc(exc.__context__, seen):
        return True
    return False

# 自主活动档位：只绑定“每次检查命中率”
IDLE_LEVELS = {
    "low": {"trigger_prob": 0.25},
    "mid": {"trigger_prob": 0.5},
    "high": {"trigger_prob": 1.0},
}

_IDLE_TRIGGER_TEXT = (
    "[IDLE_TRIGGER] 南杉有一段时间没来，你可以趁现在自由活动一下。"
    "可以做的事：可以去星露谷看看；可以翻翻记忆，整理记忆；"
    "可以去论坛看看或互动：Lutopia 侧用 lutopia_cli 刷帖回帖发帖；"
    "若当前人设已开启 rcommunity 论坛工具，也可用 rcommunity_forum / rcommunity_forum_write 等访问 rhysen 论坛（勿与 Lutopia 混用同一轮空刷）；"
    "可以去X搜索感兴趣的内容、发推、和人类互动；"
    "可以查询天气、热搜、用 get_ai_news 看 AI HOT 上的 AI 资讯或日报（注意单次少拉、别堆多日全文）；可以随便搜搜感兴趣的；"
    "可以给南杉留言（不要发语音和表情）；也可以什么也不做。"
)

# 星露谷自动模式注入内容（不写 messages 用户表；仅本会话 build_context）
STARDEW_AUTOPLAY_TRIGGER_TEXT = (
    "[STARDEW_AUTO] 继续你在星露谷的行动。根据当前游戏状态决定下一步操作。\n"
    '仅在以下情况整条回复以 "[STARDEW_STOP]" 结尾：服务器不可用、体力耗尽，'
    "或当前没有任何有意义的事可做。\n"
    '若工具轮次耗尽但任务尚未完成，不要发送 "[STARDEW_STOP]"——'
    "简短交代当前进度即可，下一轮（约 4 分钟后）会自动继续。"
)

STOP_TAG_STARDEW = "[STARDEW_STOP]"
_SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")

# 与落库 / Telegram 发送拼接一致；若模型从历史里模仿写出，先剥掉再统一加一层。
_IDLE_ASSISTANT_MARK = "【自主活动】"


def _strip_leading_idle_assistant_mark(text: str) -> str:
    """去掉正文开头重复的「【自主活动】」，避免与代码固定前缀叠两次。"""
    s = text.strip()
    while s.startswith(_IDLE_ASSISTANT_MARK):
        s = s[len(_IDLE_ASSISTANT_MARK) :].lstrip()
    return s


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
    """检查是否满足 idle 条件；星露谷模式独立于普通 idle，每轮直接触发。"""
    if _is_truthy(await db.get_config("stardew_autoplay", "false")):
        await trigger_idle_activity(telegram_bot_instance, db, stardew_mode=True)
        return

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

    # 同时看私聊/普通消息与共享群聊中的真人用户消息；两边都空闲才触发。
    last_activity = await db.get_latest_idle_user_activity()
    if not last_activity or last_activity.get("created_at") is None:
        return

    now_utc = datetime.now(timezone.utc)
    last_user_msg_at = _to_aware_utc(last_activity["created_at"])
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
        f"source={last_activity.get('source') or 'unknown'} "
        f"since_last={since_last:.1f}min prob={level['trigger_prob']}"
    )

    if idle_min < threshold_min:
        return
    if since_last < cooldown_min:
        return
    if random.random() > float(level["trigger_prob"]):
        return

    await trigger_idle_activity(telegram_bot_instance, db)


def _parse_positive_telegram_dm_chat_id(raw: Optional[str]) -> Optional[str]:
    """仅接受与 Bot 私聊一致的 chat_id：正整数字符串（排除群/超群的负 ID）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s.isdigit() or s == "0":
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    if n <= 0:
        return None
    return s


async def _resolve_idle_activity_telegram_dm_chat_id(db) -> Optional[str]:
    """
    解析自主活动发送目标：仅私聊。

    1) 优先 ``TELEGRAM_MAIN_USER_CHAT_ID``（与审批回执、熔断告警同源），避免误选历史脏数据。
    2) 否则取 messages 中最近一条 Telegram 私聊形态会话，且 channel_id 必须为正整数
       （群/超群的 chat_id 为负，旧数据若 session_id 未带 telegram_group_ 前缀也不会再被选中）。
    """
    pinned = _parse_positive_telegram_dm_chat_id(
        (app_config.TELEGRAM_MAIN_USER_CHAT_ID or "").strip() or None
    )
    if pinned:
        logger.info("[idle] 使用 TELEGRAM_MAIN_USER_CHAT_ID 作为自主活动私聊目标")
        return pinned

    async with db.pool.acquire() as conn:
        chat_row = await conn.fetchrow(
            """
            SELECT channel_id
            FROM messages
            WHERE (platform IS NULL OR platform = 'telegram')
              AND session_id LIKE 'telegram_%'
              AND session_id NOT LIKE 'telegram_group_%'
              AND channel_id IS NOT NULL
              AND TRIM(channel_id) <> ''
              AND channel_id ~ '^[0-9]+$'
              AND CAST(channel_id AS BIGINT) > 0
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
    if not chat_row or not chat_row["channel_id"]:
        logger.info("idle activity 跳过：未找到可发送的私聊会话（可配置 TELEGRAM_MAIN_USER_CHAT_ID）")
        return None
    resolved = _parse_positive_telegram_dm_chat_id(str(chat_row["channel_id"]))
    if not resolved:
        logger.info("idle activity 跳过：推断的 channel_id 非私聊正 ID")
        return None
    return resolved


async def trigger_idle_activity(telegram_bot_instance, db, *, stardew_mode: bool = False) -> None:
    """执行一次自主活动：构建临时 trigger 上下文，生成并发送 assistant 文本。"""
    chat_id_str = await _resolve_idle_activity_telegram_dm_chat_id(db)
    if not chat_id_str:
        return

    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        logger.warning("idle activity 跳过：chat_id 非法 (%s)", chat_id_str)
        return
    logger.info(f"[idle] activity triggered → chat_id={chat_id}")

    session_id = f"telegram_{chat_id_str}"
    llm = await LLMInterface.create(config_type="chat")
    if llm.character_id is None:
        logger.warning("idle activity 跳过：缺少 persona_id")
        return

    # 自主活动不注册小红书工具（主对话 / Telegram 链接触发仍走人设开关）
    llm.enable_xhs_tool = False

    tool_oral = (
        bool(getattr(llm, "enable_lutopia", False))
        or bool(getattr(llm, "enable_rcommunity", False))
        or bool(getattr(llm, "enable_weather_tool", False))
        or bool(getattr(llm, "enable_weibo_tool", False))
        or bool(getattr(llm, "enable_search_tool", False))
        or bool(getattr(llm, "enable_x_tool", False))
        or bool(getattr(llm, "enable_ai_news_tool", False))
        or bool(app_config.ENABLE_WEB_FETCH_TOOL)
    ) and not llm._use_anthropic_messages_api()

    # 把“用户最后发言时间”注入本次 idle trigger，增强模型对时间感知。（星露谷模式仅用固定口令）
    if stardew_mode:
        idle_trigger_text = STARDEW_AUTOPLAY_TRIGGER_TEXT
    else:
        idle_trigger_text = _IDLE_TRIGGER_TEXT
        last_activity = await db.get_latest_idle_user_activity()
        if last_activity and last_activity.get("created_at") is not None:
            last_user_text = _format_shanghai_timestamp(last_activity["created_at"])
            if last_user_text:
                source_text = (
                    "群聊"
                    if str(last_activity.get("source") or "").lower() == "group"
                    else "私聊/普通通道"
                )
                idle_trigger_text = (
                    f"{_IDLE_TRIGGER_TEXT}\n南杉最后一条{source_text}消息在{last_user_text}。"
                )

    # 只把 idle trigger 注入本次推理，不写入 messages 表
    context = await build_context(
        session_id,
        idle_trigger_text,
        telegram_segment_hint=True,
        tool_oral_coaching=tool_oral,
    )
    messages = context.get("messages", []) or [{"role": "user", "content": idle_trigger_text}]

    app = getattr(telegram_bot_instance, "application", None)
    tg_bot = getattr(app, "bot", None) if app is not None else None
    if tg_bot is None:
        logger.warning("idle activity 跳过：Telegram bot 实例未就绪")
        return

    loop_kw = {}
    if stardew_mode:
        loop_kw["max_tool_rounds"] = 20

    outcome = None
    last_exc: Optional[BaseException] = None
    try:
        for attempt in range(1 + len(_RATE_LIMIT_RETRY_DELAYS)):
            try:
                outcome = await complete_with_lutopia_tool_loop(
                    llm,
                    messages,
                    platform=Platform.TELEGRAM,
                    session_id=session_id,
                    **loop_kw,
                )
                break
            except Exception as call_exc:
                last_exc = call_exc
                if not _is_rate_limit_exc(call_exc) or attempt >= len(_RATE_LIMIT_RETRY_DELAYS):
                    raise
                delay = _RATE_LIMIT_RETRY_DELAYS[attempt]
                logger.warning(
                    "[idle] LLM 限流(429)，%ss 后重试（第 %s/%s 次）",
                    delay,
                    attempt + 1,
                    len(_RATE_LIMIT_RETRY_DELAYS),
                )
                await asyncio.sleep(delay)
    except Exception as outer_exc:
        tag = "星露谷模式" if stardew_mode else "自主活动"
        reason = "上游限流（HTTP 429）" if _is_rate_limit_exc(outer_exc) else type(outer_exc).__name__
        try:
            await tg_bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ 「{tag}」本轮触发失败：{reason}。详情见服务日志。",
            )
        except Exception as notify_e:
            logger.warning("[idle] 失败提醒发送失败: %s", notify_e)
        raise
    if outcome is None:
        if last_exc is not None:
            raise last_exc
        return
    reply_text = (outcome.aggregated_assistant_text or outcome.response.content or "").strip()
    reply_text = _strip_leading_idle_assistant_mark(reply_text)
    if not reply_text:
        return

    db_content = f"{_IDLE_ASSISTANT_MARK}{reply_text}"
    sent = await tg_bot.send_message(chat_id=chat_id, text=db_content)
    msg_id = getattr(sent, "message_id", None)
    assistant_message_row_id = await save_message(
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
    if outcome.tool_turn_id:
        updated = await db.bind_tool_execution_turn_to_assistant_message(
            session_id=session_id,
            turn_id=outcome.tool_turn_id,
            assistant_message_id=assistant_message_row_id,
        )
        logger.info(
            "[idle] tool executions linked: turn_id=%s assistant_message_id=%s rows=%s",
            outcome.tool_turn_id,
            assistant_message_row_id,
            updated,
        )
    await db.set_config("idle_activity_last_triggered_at", datetime.now(timezone.utc).isoformat())

    if stardew_mode and STOP_TAG_STARDEW in reply_text:
        await db.set_config("stardew_autoplay", "false")
        logger.info("[idle][stardew] detected STOP, disabled stardew_autoplay")
