"""
AI 自主活动（Idle Activity）模块。

按固定频率检查是否长时间无人发言，满足条件后触发一次「自由活动」回复。
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytz
import requests

from config import Platform, config as app_config
from llm.llm_interface import LLMInterface, complete_with_lutopia_tool_loop
from memory.context_builder import build_context
from memory.daily_summary_compress import build_idle_daily_summaries_override
from memory.database import save_message
from memory.prompt_registry import get_effective_prompt_text

logger = logging.getLogger(__name__)

# 自主活动 LLM 外层重试：每次延迟秒数（共 1 + len 次请求）
_IDLE_LLM_RETRY_DELAYS = (10, 15, 30)
_RETRIABLE_HTTP_STATUS = frozenset({401, 403, 429, 500, 502, 503, 504})


def _walk_exc_chain(exc: BaseException, _seen: Optional[set] = None):
    """递归遍历异常链（含 ExceptionGroup）。"""
    if exc is None:
        return
    seen = _seen if _seen is not None else set()
    eid = id(exc)
    if eid in seen:
        return
    seen.add(eid)
    yield exc
    sub_excs = getattr(exc, "exceptions", None)
    if isinstance(sub_excs, (list, tuple)):
        for sub in sub_excs:
            yield from _walk_exc_chain(sub, seen)
    if exc.__cause__ is not None:
        yield from _walk_exc_chain(exc.__cause__, seen)
    if exc.__context__ is not None:
        yield from _walk_exc_chain(exc.__context__, seen)


def _http_status_from_exc(exc: BaseException) -> Optional[int]:
    for node in _walk_exc_chain(exc):
        if isinstance(node, requests.exceptions.HTTPError):
            resp = getattr(node, "response", None)
            if resp is not None:
                code = getattr(resp, "status_code", None)
                if isinstance(code, int):
                    return code
    return None


def _is_retriable_idle_llm_exc(exc: BaseException) -> bool:
    """自主活动 LLM 可重试：429/5xx/401/403、超时与连接类瞬时故障。"""
    status = _http_status_from_exc(exc)
    if status is not None and status in _RETRIABLE_HTTP_STATUS:
        return True
    for node in _walk_exc_chain(exc):
        if isinstance(
            node,
            (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ),
        ):
            return True
    msg = (str(exc) or "").lower()
    if not msg:
        return False
    if "read timed out" in msg or "timed out" in msg:
        return True
    if "connection" in msg and ("reset" in msg or "aborted" in msg or "refused" in msg):
        return True
    for token in (
        "rate_limit",
        "rate limit",
        "429",
        "internal server error",
        "bad gateway",
        "gateway timeout",
        "502",
        "503",
        "504",
        "500 server error",
        "401 client error",
        "403 client error",
        "unauthorized",
        "forbidden",
    ):
        if token in msg:
            return True
    return False


def _idle_llm_failure_reason(exc: BaseException) -> str:
    """失败提醒用简短原因（取异常链上最有代表性的 HTTP 状态）。"""
    status = _http_status_from_exc(exc)
    if status == 429:
        return "上游限流（HTTP 429）"
    if status == 401:
        return "上游鉴权失败（HTTP 401）"
    if status == 403:
        return "上游拒绝（HTTP 403，常见为额度不足）"
    if status in (500, 502, 503, 504):
        return f"上游错误（HTTP {status}）"
    for node in _walk_exc_chain(exc):
        if isinstance(node, requests.exceptions.Timeout):
            return "LLM 请求超时"
        if isinstance(node, requests.exceptions.ConnectionError):
            return "LLM 连接失败"
    msg = str(exc) or ""
    if "timed out" in msg.lower():
        return "LLM 请求超时"
    return type(exc).__name__

# 自主活动档位：只绑定“每次检查命中率”
IDLE_LEVELS = {
    "low": {"trigger_prob": 0.25},
    "mid": {"trigger_prob": 0.5},
    "high": {"trigger_prob": 1.0},
}

_IDLE_CUSTOM_MCP_PROMPT_HEADER = "【自主活动可用的自定义 MCP】"


async def _append_idle_custom_mcp_prompt_suffix(db, text: str) -> str:
    """拼接用户为 allow_idle MCP 手写的说明；仅加固定段首，正文原样、按库内顺序。"""
    try:
        servers = await db.list_mcp_servers(enabled_only=False)
    except Exception as e:
        logger.warning("[idle] 读取 MCP 自主活动说明失败: %s", e)
        return text
    lines: list[str] = []
    for row in servers or []:
        if int(row.get("enabled") or 0) != 1:
            continue
        if int(row.get("allow_idle") or 0) != 1:
            continue
        body = str(row.get("idle_activity_prompt") or "").strip()
        if body:
            lines.append(body)
    if not lines:
        return text
    block = _IDLE_CUSTOM_MCP_PROMPT_HEADER + "\n" + "\n".join(lines)
    return f"{text.rstrip()}\n\n{block}"


_IDLE_TRIGGER_TEXT = (
    "[IDLE_TRIGGER] 南杉有一段时间没来，你可以趁现在自由活动一下。"
    "可以做的事：可以去星露谷看看；可以翻翻记忆，整理记忆；"
    "可以去论坛看看或互动：Lutopia 侧用 lutopia_cli 刷帖回帖发帖；"
    "若当前人设已开启 Rhysen 论坛工具，也可用 rhysen_forum / rhysen_forum_write 等访问 Rhysen 论坛（勿与 Lutopia 混用同一轮空刷）；"
    "可以去X搜索感兴趣的内容、发推、和人类互动；"
    "可以查询天气、热搜、用 get_ai_news 看 AI HOT 上的 AI 资讯或日报（注意单次少拉、别堆多日全文）；可以随便搜搜感兴趣的；"
    "如果希望稍后再醒来继续自主活动，可以调用 schedule_next_wakeup 设置下次醒来时间；"
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
NEXT_AT_TAG_PREFIX = "[NEXT_AT_"
_CONFIG_KEY_NEXT_TRIGGER_AT = "idle_activity_next_trigger_at"
_NEXT_AT_TAG_RE = re.compile(
    re.escape(NEXT_AT_TAG_PREFIX) + r"(\d{1,2}):(\d{2})\]",
    re.IGNORECASE,
)
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


def _append_idle_next_at_hint(text: str) -> str:
    """在触发文案末尾注入当前北京时间与 [NEXT_AT_HH:MM] 说明。"""
    now_sh = datetime.now(_SHANGHAI_TZ)
    hhmm = f"{now_sh.hour:02d}:{now_sh.minute:02d}"
    return (
        f"{text}\n\n当前北京时间 {hhmm}。\n"
        "预约下次自主活动（可选）：若你希望在后续某个固定时刻再自主活动、出去转转"
        "（不必等南杉先来私聊，到点即触发），\n"
        "请在本条回复的**最末尾**单独加一行标记 [NEXT_AT_HH:MM]："
        "HH:MM 为 24 小时制北京时间（例：[NEXT_AT_20:00]）；\n"
        "若该时刻今天已过，则顺延到次日同一时刻。"
        "该标记会从发给南杉的正文里剥除，她看不到。\n"
        "也可以直接调用 schedule_next_wakeup 工具来设置，效果相同且工具方式更可靠。"
        "工具参数为 time_hhmm（北京时间 HH:MM）或 delay_minutes（多少分钟后）。"
        "两种方式选一个用即可，不要同时用。\n"
        "若本轮不需要预约下次时间，不要写该标记，系统将继续按 Mini App 的空闲阈值、"
        "自主活动冷却与概率档位决定是否再触发。\n"
        "注意：预约时间建议与当前时间间隔不超过 3 小时，过长会导致这段时间内正常触发也被屏蔽。"
    )


def _parse_next_at_tag_to_utc(reply_text: str) -> Optional[datetime]:
    """从回复中解析 [NEXT_AT_HH:MM]（北京时间），返回 UTC aware datetime。"""
    m = _NEXT_AT_TAG_RE.search(reply_text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    now_sh = datetime.now(_SHANGHAI_TZ)
    target_sh = now_sh.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_sh <= now_sh:
        target_sh += timedelta(days=1)
    return target_sh.astimezone(timezone.utc)


def _strip_next_at_tag(text: str) -> str:
    """去掉回复中的 [NEXT_AT_...] 标记，避免发给用户。"""
    return _NEXT_AT_TAG_RE.sub("", text).strip()


async def _apply_idle_next_trigger_at(db, reply_text: str) -> None:
    """根据回复中的 NEXT_AT 标记写入或清空下次触发时间。"""
    next_utc = _parse_next_at_tag_to_utc(reply_text)
    if next_utc is not None:
        await db.set_config(_CONFIG_KEY_NEXT_TRIGGER_AT, next_utc.isoformat())
        logger.info("[idle] next trigger scheduled at %s (UTC)", next_utc.isoformat())
    else:
        await db.set_config(_CONFIG_KEY_NEXT_TRIGGER_AT, "")


async def _apply_idle_next_trigger_at_unless_tool_set(db, reply_text: str) -> None:
    """工具已设置预约时，跳过文本标记解析，避免覆盖工具写入值。"""
    tool_already_set = await db.get_config("idle_next_trigger_set_by_tool", "false")
    if tool_already_set == "true":
        await db.set_config("idle_next_trigger_set_by_tool", "")
        logger.info("[idle] next trigger already scheduled by tool; skip text marker parsing")
        return
    await _apply_idle_next_trigger_at(db, reply_text)


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

    # 仅在东八区设定时段内允许触发（预约触发与概率触发共用）
    now_sh = datetime.now(_SHANGHAI_TZ)
    try:
        start_hour = int(await db.get_config("idle_activity_start_hour", "8"))
        end_hour = int(await db.get_config("idle_activity_end_hour", "23"))
    except (TypeError, ValueError):
        start_hour, end_hour = 8, 23
    if now_sh.hour < start_hour or now_sh.hour > end_hour:
        return

    next_at_raw = str(await db.get_config(_CONFIG_KEY_NEXT_TRIGGER_AT, "") or "").strip()
    if next_at_raw:
        next_at_utc: Optional[datetime] = None
        try:
            next_at_utc = _to_aware_utc(datetime.fromisoformat(next_at_raw))
        except ValueError:
            logger.warning("[idle] invalid next_trigger_at=%s, clearing", next_at_raw)
            await db.set_config(_CONFIG_KEY_NEXT_TRIGGER_AT, "")
        if next_at_utc is not None:
            if datetime.now(timezone.utc) < next_at_utc:
                logger.debug("[idle] skip tick: waiting for next_trigger_at=%s", next_at_raw)
                return
            logger.info(
                "[idle] next_trigger_at due (%s), scheduled trigger",
                next_at_raw,
            )
            await db.set_config(_CONFIG_KEY_NEXT_TRIGGER_AT, "")
            await trigger_idle_activity(telegram_bot_instance, db)
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

    has_idle_builtin_tools = True  # schedule_next_wakeup 是 idle 内置工具，始终可用。
    tool_oral = (
        bool(getattr(llm, "enable_lutopia", False))
        or bool(getattr(llm, "enable_rcommunity", False))
        or bool(getattr(llm, "enable_weather_tool", False))
        or bool(getattr(llm, "enable_weibo_tool", False))
        or bool(getattr(llm, "enable_search_tool", False))
        or bool(getattr(llm, "enable_x_tool", False))
        or bool(getattr(llm, "enable_ai_news_tool", False))
        or bool(app_config.ENABLE_WEB_FETCH_TOOL)
        or bool(app_config.ENABLE_CUSTOM_MCP)
        or has_idle_builtin_tools
    )

    # 把南杉最近回复时间注入本次 idle trigger，增强模型对时间感知。（星露谷模式仅用固定口令）
    if stardew_mode:
        idle_trigger_text = STARDEW_AUTOPLAY_TRIGGER_TEXT
    else:
        try:
            idle_trigger_base = await get_effective_prompt_text("idle_activity_trigger")
        except Exception as e:
            logger.warning("[idle] 读取自主活动 prompt override 失败，使用默认值: %s", e)
            idle_trigger_base = _IDLE_TRIGGER_TEXT
        idle_trigger_base = str(idle_trigger_base or "").strip() or _IDLE_TRIGGER_TEXT
        idle_trigger_text = _append_idle_next_at_hint(idle_trigger_base)
        last_activity = await db.get_latest_idle_user_activity()
        if last_activity and last_activity.get("created_at") is not None:
            last_user_text = _format_shanghai_timestamp(last_activity["created_at"])
            if last_user_text:
                idle_trigger_text = _append_idle_next_at_hint(
                    f"{idle_trigger_base}\n南杉最近一次回复你在{last_user_text}。"
                )
    idle_trigger_text = await _append_idle_custom_mcp_prompt_suffix(db, idle_trigger_text)

    # 只把 idle trigger 注入本次推理，不写入 messages 表
    daily_summaries_override = await build_idle_daily_summaries_override()
    context = await build_context(
        session_id,
        idle_trigger_text,
        telegram_segment_hint=True,
        tool_oral_coaching=tool_oral,
        daily_summaries_override=daily_summaries_override,
        skip_vector_search=True,
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
        for attempt in range(1 + len(_IDLE_LLM_RETRY_DELAYS)):
            try:
                outcome = await complete_with_lutopia_tool_loop(
                    llm,
                    messages,
                    platform=Platform.TELEGRAM,
                    session_id=session_id,
                    is_idle=True,
                    **loop_kw,
                )
                break
            except Exception as call_exc:
                last_exc = call_exc
                if not _is_retriable_idle_llm_exc(call_exc) or attempt >= len(
                    _IDLE_LLM_RETRY_DELAYS
                ):
                    raise
                delay = _IDLE_LLM_RETRY_DELAYS[attempt]
                logger.warning(
                    "[idle] LLM 可重试错误（%s），%ss 后重试（第 %s/%s 次）",
                    _idle_llm_failure_reason(call_exc),
                    delay,
                    attempt + 1,
                    len(_IDLE_LLM_RETRY_DELAYS),
                )
                await asyncio.sleep(delay)
    except Exception as outer_exc:
        tag = "星露谷模式" if stardew_mode else "自主活动"
        reason = _idle_llm_failure_reason(outer_exc)
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

    reply_text_for_next_at = reply_text
    reply_text = _strip_next_at_tag(reply_text)
    if not reply_text:
        if not stardew_mode:
            await _apply_idle_next_trigger_at_unless_tool_set(db, reply_text_for_next_at)
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

    if not stardew_mode:
        await _apply_idle_next_trigger_at_unless_tool_set(db, reply_text_for_next_at)
