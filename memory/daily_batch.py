"""
日终跑批处理模块。

每天东八区（Asia/Shanghai）在 `config.daily_batch_hour` 整点（默认 23）自动触发，执行五步流水线：
Step 1 - 到期 temporal_states 结算并改写为客观过去时，供 Step 2 使用
Step 2 - 生成今日小传（prompt 含 Step 1 输出）
Step 3 - 记忆卡片 Upsert + 可选写入 relationship_timeline
Step 3.5 - 从当日 daily 小传解析时效操作 JSON（新增 / 停用 / 调整到期；step3=1 且 step4=0 时执行；失败不阻断 Step 4）
Step 4 - 今日小传拆事件片段向量化（按分映射 halflife_days）+ BM25 增量
Step 5 - Chroma 长期未访问且衰减得分过低、无子节点的记忆 GC

断点续跑：每次触发前先查 daily_batch_log 表（step1–step5），已完成的步骤跳过。
"""

import asyncio
import json
import logging
import subprocess
import sys
import os
import re
from decimal import Decimal
from datetime import datetime, date, time, timedelta
from typing import List, Dict, Any, Optional, Tuple, NamedTuple
from uuid import uuid4
import pytz

from bot.logutil import exc_detail
from bot.telegram_notify import send_telegram_main_user_text

# 添加项目根目录到 Python 路径
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import config, Platform
from llm.llm_interface import (
    LLMInterface,
    APIConfigLoadError,
    CedarClioOutputGuardExhausted,
    NoActiveAPIConfigError,
    batch_one_shot_with_async_output_guard,
)
from memory.micro_batch import SummaryLLMInterface, fetch_active_persona_display_names
from tools.lutopia import strip_lutopia_internal_memory_blocks
from api.stream import EventType, publish_event

# 导入向量存储函数
try:
    from .vector_store import (
        add_memory,
        build_daily_summary_doc_id,
        build_daily_event_doc_id,
        garbage_collect_stale_memories,
        get_vector_store,
    )
except ImportError:
    from memory.vector_store import (
        add_memory,
        build_daily_summary_doc_id,
        build_daily_event_doc_id,
        garbage_collect_stale_memories,
        get_vector_store,
    )

# 导入数据库函数
try:
    from .database import (
        get_database,
        get_today_chunk_summaries,
        archive_chunk_summaries_by_daily,
        archive_external_chunks_by_daily,
        get_today_user_character_pairs,
        get_memory_cards,
        get_latest_memory_card_for_dimension,
        get_unsummarized_messages_by_session,
        save_summary,
        mark_messages_as_summarized_by_ids,
        get_all_active_memory_cards,
        save_memory_card,
        update_memory_card,
        get_daily_summary_by_date,
        get_daily_summaries_by_date,
        save_daily_batch_log,
        get_daily_batch_log,
        update_daily_batch_step_status,
        get_unsummarized_count_by_session,
        list_expired_active_temporal_states,
        deactivate_temporal_states_by_ids,
        get_all_active_temporal_states,
        save_temporal_state,
        update_temporal_state_expire_at,
        insert_relationship_timeline_event,
        list_incomplete_daily_batch_dates_in_range,
        mark_expired_skipped_daily_batch_logs_before,
        purge_logs_older_than_days,
        cleanup_tool_executions,
        increment_daily_batch_retry_count,
        reset_daily_batch_retry_count,
        expire_stale_approvals,
        run_daily_pocket_money_job,
        upsert_pocket_money_job_log,
        list_incomplete_pocket_money_job_dates_in_range,
    )
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    from memory.database import (
        get_database,
        get_today_chunk_summaries,
        archive_chunk_summaries_by_daily,
        archive_external_chunks_by_daily,
        get_today_user_character_pairs,
        get_memory_cards,
        get_latest_memory_card_for_dimension,
        get_unsummarized_messages_by_session,
        save_summary,
        mark_messages_as_summarized_by_ids,
        get_all_active_memory_cards,
        save_memory_card,
        update_memory_card,
        get_daily_summary_by_date,
        get_daily_summaries_by_date,
        save_daily_batch_log,
        get_daily_batch_log,
        update_daily_batch_step_status,
        get_unsummarized_count_by_session,
        list_expired_active_temporal_states,
        deactivate_temporal_states_by_ids,
        get_all_active_temporal_states,
        save_temporal_state,
        update_temporal_state_expire_at,
        insert_relationship_timeline_event,
        list_incomplete_daily_batch_dates_in_range,
        mark_expired_skipped_daily_batch_logs_before,
        purge_logs_older_than_days,
        cleanup_tool_executions,
        increment_daily_batch_retry_count,
        reset_daily_batch_retry_count,
        expire_stale_approvals,
        run_daily_pocket_money_job,
        upsert_pocket_money_job_log,
        list_incomplete_pocket_money_job_dates_in_range,
    )

# 设置日志
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step 4b 事件标签 enum 常量
# ---------------------------------------------------------------------------

EVENT_THEMES = [
    "daily_life", "work_career", "education", "health", "relationship",
    "emotion", "hobby", "travel", "finance", "family", "conflict",
    "milestone", "decision", "other",
]

EVENT_EMOTIONS = [
    "happy", "sad", "angry", "anxious", "excited", "calm", "grateful",
    "nostalgic", "frustrated", "hopeful", "neutral", "other",
]

EVENT_TYPES = [
    "daily_warmth", "decision", "emotional_shift", "milestone",
    "conflict", "routine", "other",
]


class _MemoryMergeResult(NamedTuple):
    """记忆卡片合并结果：discarded 仅 current_status / preferences 可能非空。"""

    merged: str
    discarded: Optional[str] = None

# 时区配置
TIMEZONE = pytz.timezone("Asia/Shanghai")

# 跑批失败后由独立进程延迟重试（秒）；与 cron 入口 ``run_daily_batch.py`` 共用
DAILY_BATCH_FAILURE_RETRY_SECONDS = 2 * 3600

# Step 4 分两段执行：4a 聚类 chunk_id，4b 逐组生成事件描述与分数。
# 设为 False 可回退到旧的单次 LLM 调用路径。
STEP4_SPLIT_MODE = True


def spawn_run_daily_batch_retry_after_hours(
    batch_date: str,
    *,
    delay_seconds: int = DAILY_BATCH_FAILURE_RETRY_SECONDS,
) -> bool:
    """
    跑批失败后：在独立后台进程中等待 ``delay_seconds``，再执行
    ``python run_daily_batch.py <batch_date>``（与 cron 同源；断点续跑仍有效）。

    返回是否已成功启动子进程（Popen 成功）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "run_daily_batch.py")
    argv = [sys.executable, script, batch_date]
    code = (
        "import time, subprocess; "
        f"time.sleep({int(delay_seconds)}); "
        f"subprocess.run({argv!r})"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(
            "已安排 %ss 后由子进程重试日终跑批（batch_date=%s）",
            int(delay_seconds),
            batch_date,
        )
        return True
    except Exception as e:
        logger.error("无法启动跑批延迟重试子进程: %s", e)
        return False


def _infer_stuck_step_from_batch_log(log: Optional[Dict[str, Any]]) -> int:
    """从 daily_batch_log 推断当前未完成或失败步骤（首个 stepN_status==0）。"""
    if not log:
        return 1
    for i in range(1, 6):
        if int(log.get(f"step{i}_status") or 0) == 0:
            return i
    return 5


async def schedule_daily_batch_retry_if_needed(batch_date: str) -> None:
    """
    跑批失败后：retry_count < 3 时递增 retry_count、排队子进程重跑并发 Telegram；
    retry_count >= 3 时仅发熔断告警与日志，不再 spawn。
    """
    try:
        log = await get_daily_batch_log(batch_date)
    except Exception as e:
        logger.warning("读取 daily_batch_log 失败，仍尝试 spawn 重跑: %s", e)
        log = None

    rc = int(log.get("retry_count") or 0) if log else 0
    step = _infer_stuck_step_from_batch_log(log)
    err_s = str((log or {}).get("error_message") or "（无）")[:800]

    if rc >= 3:
        msg = (
            f"🚨 日终跑批多次重试仍失败，需要手动介入\n"
            f"批次日期：{batch_date}\n"
            f"已重试次数：{rc}\n"
            f"失败步骤：Step {step}\n"
            f"错误信息：{err_s}"
        )
        try:
            await send_telegram_main_user_text(msg)
        except Exception:
            logger.warning("Telegram 跑批熔断告警发送异常", exc_info=True)
        logger.error(
            "日终跑批已达 retry 上限，不再安排延迟重试 batch_date=%s retry_count=%s",
            batch_date,
            rc,
        )
        return

    spawned = spawn_run_daily_batch_retry_after_hours(batch_date)
    if not spawned:
        logger.error("日终跑批延迟重试子进程未启动，retry_count 未递增 batch_date=%s", batch_date)
        return

    try:
        new_rc = await increment_daily_batch_retry_count(batch_date)
    except Exception as e:
        logger.error("increment_daily_batch_retry_count 失败: %s", e)
        new_rc = rc + 1

    msg2 = (
        f"⚠️ 日终跑批卡住，已安排 2 小时后重试\n"
        f"批次日期：{batch_date}\n"
        f"卡住步骤：Step {step}\n"
        f"当前重试次数：{new_rc}/3\n"
        f"错误信息：{err_s}"
    )
    try:
        await send_telegram_main_user_text(msg2)
    except Exception:
        logger.warning("Telegram 跑批排队通知发送异常", exc_info=True)


async def _daily_batch_trigger_hour() -> float:
    """日终跑批触发时刻（0–23.5，支持半小时）：优先 config 表 daily_batch_hour，否则默认 23。"""
    try:
        raw = await get_database().get_config("daily_batch_hour")
        if raw is not None and str(raw).strip() != "":
            h = round(float(str(raw).strip()) * 2) / 2
            if 0 <= h <= 23.5:
                return h
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 daily_batch_hour 失败，使用默认 23: %s", e)
    return 23


async def resolve_daily_batch_date(batch_date: Optional[str] = None) -> str:
    """解析无参跑批日期：早于配置触发时刻时处理前一天。"""
    if batch_date and str(batch_date).strip():
        return str(batch_date).strip()

    now = datetime.now(TIMEZONE)
    trigger_hour = await _daily_batch_trigger_hour()
    current_hour = now.hour + now.minute / 60
    if current_hour < trigger_hour:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


async def _gc_stale_days_threshold() -> float:
    """Step 5 GC 闲置天数阈值：优先 config 表 gc_stale_days，否则默认 180。"""
    try:
        raw = await get_database().get_config("gc_stale_days")
        if raw is not None and str(raw).strip() != "":
            return max(1.0, float(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 gc_stale_days 失败，使用默认 180: %s", e)
    return 180.0


async def _gc_exempt_hits_threshold() -> int:
    """Step 5 GC hits 豁免阈值：优先 config 表 gc_exempt_hits_threshold，否则默认 10。"""
    try:
        raw = await get_database().get_config("gc_exempt_hits_threshold")
        if raw is not None and str(raw).strip() != "":
            return max(0, int(str(raw).strip()))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 gc_exempt_hits_threshold 失败，使用默认 10: %s", e)
    return 10


async def _event_split_max() -> int:
    """Step 4 事件拆分软上限：优先 config 表 event_split_max，否则默认 8。"""
    try:
        raw = await get_database().get_config("event_split_max")
        if raw is not None and str(raw).strip() != "":
            return max(1, min(15, int(str(raw).strip())))
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 event_split_max 失败，使用默认 8: %s", e)
    return 8


def _score_to_halflife_days(score: int) -> int:
    """日终打分映射半衰期：8–10→600 天，4–7→200 天，1–3→30 天。"""
    if score >= 8:
        return 600
    if score >= 4:
        return 200
    return 30


# ---------------------------------------------------------------------------
# Step 3 结构化 JSON Schema（strict mode）
# 注意：manual_override 字段只由 PG 表和后端代码控制，绝不进 LLM schema
# ---------------------------------------------------------------------------

STEP3_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "memory_cards",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "current_status": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "updated_at": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["updated_at", "content"],
                            "additionalProperties": False,
                        },
                        {"type": "null"},
                    ]
                },
                "goals": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string"},
                                    "content": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["active", "completed", "abandoned", "deferred"],
                                    },
                                },
                                "required": ["date", "content", "status"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
                "key_events": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string"},
                                    "content": {"type": "string"},
                                    "importance": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 5,
                                    },
                                },
                                "required": ["date", "content", "importance"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
                "interaction_patterns": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string"},
                                    "pattern": {"type": "string"},
                                    "frequency": {"type": "string"},
                                },
                                "required": ["date", "pattern", "frequency"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
                "rules": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string"},
                                    "content": {"type": "string"},
                                    "established_by": {"type": "string"},
                                },
                                "required": ["date", "content", "established_by"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
                "preferences": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "evidence_date": {"type": "string"},
                                },
                                "required": ["content", "evidence_date"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
                "relationships": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string"},
                                    "event": {"type": "string"},
                                    "polarity": {
                                        "type": "string",
                                        "enum": ["+", "-", "neutral"],
                                    },
                                },
                                "required": ["date", "event", "polarity"],
                                "additionalProperties": False,
                            },
                        },
                        {"type": "null"},
                    ]
                },
            },
            "required": [
                "current_status",
                "goals",
                "key_events",
                "interaction_patterns",
                "rules",
                "preferences",
                "relationships",
            ],
            "additionalProperties": False,
        },
    },
}


class DailyBatchProcessor:
    """
    日终跑批处理器类。
    
    负责执行每日的五步流水线处理。
    """
    
    def __init__(self):
        """
        初始化日终跑批处理器。
        """
        # LLM 接口在 run_daily_batch 中异步初始化（await LLMInterface.create()）
        self.llm: Optional[LLMInterface] = None
        self.scoring_llm: Optional[LLMInterface] = None
        self.summary_llm = SummaryLLMInterface()
        self._settled_temporal_snippets: List[str] = []
        self._batch_char_name: str = "AI"
        self._batch_user_name: str = "用户"
        self._batch_user_id: str = "default_user"
        self._batch_char_id: str = config.DEFAULT_CHARACTER_ID

        # 维度列表
        self.dimensions = [
            "preferences",  # 偏好与喜恶
            "interaction_patterns",  # 相处模式
            "current_status",  # 近况与生活动态
            "goals",  # 目标与计划
            "relationships",  # 重要关系
            "key_events",  # 重要事件
            "rules"  # 相处规则与禁区
        ]
        
        logger.info("日终跑批处理器初始化完成")

    def _persona_dialogue_prefix(self) -> str:
        """跑批 Prompt 统一前缀：标明角色称呼，减轻上下文断裂与名字丢失。"""
        return f"这是 {self._batch_char_name} 与 {self._batch_user_name} 的对话记录。\n"

    async def _resolve_batch_memory_identity(self, batch_date: str) -> None:
        """按当日首对 user/character 解析记忆卡查询主键；无则回退到 DEFAULT_CHARACTER_ID。"""
        default_cid = config.DEFAULT_CHARACTER_ID
        try:
            pairs = await get_today_user_character_pairs(batch_date)
            if pairs:
                r0 = pairs[0]
                self._batch_user_id = str(r0.get("user_id") or "default_user")
                c = r0.get("character_id")
                self._batch_char_id = (
                    str(c).strip()
                    if c is not None and str(c).strip()
                    else default_cid
                )
                if self._batch_char_id == default_cid:
                    logger.warning(
                        "跑批 character_id 为空，回退到 DEFAULT_CHARACTER_ID=%s (batch_date=%s)",
                        default_cid, batch_date,
                    )
            else:
                self._batch_user_id = "default_user"
                self._batch_char_id = default_cid
                logger.warning(
                    "今日无用户对话记录，回退到 DEFAULT_CHARACTER_ID=%s (batch_date=%s)",
                    default_cid, batch_date,
                )
        except Exception as e:
            logger.warning("跑批记忆身份解析失败，回退到 DEFAULT_CHARACTER_ID=%s: %s", default_cid, e)
            self._batch_user_id = "default_user"
            self._batch_char_id = default_cid

    async def _memory_context_prefix(self) -> str:
        """注入 current_status / relationships 激活卡与关系锚点，供小模型对齐语义。"""
        dims = {
            "current_status": "用户近况",
            "relationships": "重要关系",
        }
        lines = ["【基础设定】小克是南杉的二号男友。"]
        try:
            for dim, label in dims.items():
                cards = await get_memory_cards(
                    self._batch_user_id, self._batch_char_id, dim, limit=1
                )
                card = cards[0] if cards else None
                if card and card.get("content"):
                    lines.append(f"【{label}】{card['content']}")
        except Exception as e:
            logger.warning("构建跑批记忆上下文前缀失败（已跳过卡片行）: %s", e)
        return "\n".join(lines) + "\n\n"

    async def _retry_call_and_parse(self, task_name: str, generate_func, parse_func, max_retries: int = 5):
        import asyncio
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                raw_resp = generate_func()
                parsed = parse_func(raw_resp)
                return parsed
            except CedarClioOutputGuardExhausted:
                raise
            except Exception as e:
                last_err = e
                logger.warning(f"[{task_name}] 第 {attempt}/{max_retries} 次失败: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
        logger.error(f"[{task_name}] 重试 {max_retries} 次后仍失败")
        raise last_err

    async def _combined_daily_summary_by_date(
        self, batch_date: str
    ) -> Optional[Dict[str, Any]]:
        """Return a combined view of all per-session daily summaries for legacy downstream steps."""
        rows = await get_daily_summaries_by_date(batch_date)
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]
        text_parts: List[str] = []
        for row in rows:
            sid = row.get("session_id") or "unknown"
            txt = str(row.get("summary_text") or "").strip()
            if txt:
                text_parts.append(f"[session:{sid}]\n{txt}")
        first = rows[0]
        return {
            **first,
            "session_id": "daily_batch",
            "summary_text": "\n\n".join(text_parts),
        }

    async def run_daily_batch(self, batch_date: Optional[str] = None) -> bool:
        """
        执行日终跑批处理。
        
        Args:
            batch_date: 批处理日期，格式为 'YYYY-MM-DD'，如果为 None 则使用今天
            
        Returns:
            bool: 批处理是否成功完成
        """
        self._settled_temporal_snippets = []
        try:
            # 异步初始化 LLM 接口（读取最新激活配置）
            self.llm = await LLMInterface.create()
            try:
                self.scoring_llm = await LLMInterface.create(config_type="analysis")
            except (NoActiveAPIConfigError, APIConfigLoadError) as e:
                logger.warning(
                    "analysis 配置未激活或加载失败，Step 4 回退到 summary 模型: %s",
                    exc_detail(e),
                )
                try:
                    self.scoring_llm = await LLMInterface.create(config_type="summary")
                except Exception as se:
                    logger.warning(
                        "summary 配置初始化失败，Step 4 将使用默认值继续: %s",
                        exc_detail(se),
                    )
                    self.scoring_llm = None
            except Exception as e:
                logger.warning(
                    "analysis 配置初始化异常，Step 4 回退到 summary 模型: %s",
                    exc_detail(e),
                )
                try:
                    self.scoring_llm = await LLMInterface.create(config_type="summary")
                except Exception as se:
                    logger.warning(
                        "summary 配置初始化失败，Step 4 将使用默认值继续: %s",
                        exc_detail(se),
                    )
                    self.scoring_llm = None
            self._batch_char_name, self._batch_user_name = (
                await fetch_active_persona_display_names()
            )

            batch_date = await resolve_daily_batch_date(batch_date)

            await self._resolve_batch_memory_identity(batch_date)

            logger.info(f"开始日终跑批处理，日期: {batch_date}")

            try:
                n_del = await purge_logs_older_than_days(7)
                if n_del > 0:
                    logger.info("已清理早于 7 天的系统日志（logs）%s 条", n_del)
            except Exception as e:
                logger.warning("清理过期系统日志失败（不影响跑批）: %s", e)

            try:
                n_tool = await cleanup_tool_executions(7)
                if n_tool > 0:
                    logger.info("已清理早于 7 天的工具执行记录 %s 条", n_tool)
            except Exception as e:
                logger.warning("清理工具执行记录失败（不影响跑批）: %s", e)

            batch_log = await get_daily_batch_log(batch_date)
            
            if batch_log is None:
                await save_daily_batch_log(
                    batch_date,
                    step1_status=0,
                    step2_status=0,
                    step3_status=0,
                    step4_status=0,
                    step5_status=0,
                )
                batch_log = await get_daily_batch_log(batch_date)
            
            assert batch_log is not None
            
            def _s(n: int) -> int:
                k = f"step{n}_status"
                v = batch_log.get(k)
                return 0 if v is None else int(v)
            
            # Step 1 — 到期 temporal_states
            if _s(1) == 0:
                logger.info(f"执行 Step 1 - 到期 temporal_states 结算，日期: {batch_date}")
                success, error_message = await self._step1_expire_temporal_states(batch_date)
                if success:
                    await update_daily_batch_step_status(batch_date, 1, 1)
                    batch_log["step1_status"] = 1
                    logger.info(f"Step 1 完成，日期: {batch_date}")
                else:
                    await update_daily_batch_step_status(batch_date, 1, 0, error_message)
                    logger.error(f"Step 1 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 1 已跳过（已完成），日期: {batch_date}")
            
            # Step 2 — 今日小传
            if _s(2) == 0:
                logger.info(f"执行 Step 2 - 生成今日小传，日期: {batch_date}")
                success, error_message = await self._step2_generate_daily_summary(batch_date)
                if success:
                    await update_daily_batch_step_status(batch_date, 2, 1)
                    batch_log["step2_status"] = 1
                    logger.info(f"Step 2 完成，日期: {batch_date}")
                else:
                    await update_daily_batch_step_status(batch_date, 2, 0, error_message)
                    logger.error(f"Step 2 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 2 已跳过（已完成），日期: {batch_date}")
            
            # Step 3 — 记忆卡片 + relationship_timeline
            if _s(3) == 0:
                logger.info(f"执行 Step 3 - 记忆卡片与关系时间轴，日期: {batch_date}")
                success, error_message = await self._step3_memory_cards_and_timeline(batch_date)
                if success:
                    await update_daily_batch_step_status(batch_date, 3, 1)
                    batch_log["step3_status"] = 1
                    logger.info(f"Step 3 完成，日期: {batch_date}")
                else:
                    await update_daily_batch_step_status(batch_date, 3, 0, error_message)
                    logger.error(f"Step 3 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 3 已跳过（已完成），日期: {batch_date}")

            # Step 3.5 — 从当日 daily 小传再提取 temporal_states（step3=1 且 step4=0 时执行；失败不阻断 Step 4）
            if _s(3) == 1 and _s(4) == 0:
                try:
                    ds = await self._combined_daily_summary_by_date(batch_date)
                    today_summary_text = (
                        str(ds.get("summary_text") or "").strip() if ds else ""
                    )
                    if today_summary_text:
                        step35_success = False
                        step35_last_exc = None
                        for _attempt in range(3):
                            try:
                                await self._step35_extract_temporal_states(
                                    today_summary_text,
                                    date.fromisoformat(batch_date),
                                )
                                step35_success = True
                                break
                            except Exception as e:
                                step35_last_exc = e
                                logger.warning(
                                    f"Step 3.5 第 {_attempt + 1}/3 次失败: {exc_detail(e)}，"
                                    + (
                                        "继续重试"
                                        if _attempt < 2
                                        else "放弃，不阻断 Step 4"
                                    )
                                )

                        if not step35_success:
                            logger.warning(
                                f"Step 3.5 三次均失败，最后异常: {exc_detail(step35_last_exc)}"
                            )
                            try:
                                await send_telegram_main_user_text(
                                    f"⚠️ Step 3.5 三次均失败（{batch_date}），时效状态未更新，请手动检查。\n{exc_detail(step35_last_exc)}"
                                )
                            except Exception as te:
                                logger.warning(
                                    "Step 3.5 三次失败后 Telegram 通知发送失败: %s",
                                    exc_detail(te),
                                    exc_info=True,
                                )
                except Exception as e:
                    logger.warning(
                        "[Step 3.5] 时效状态提取失败，跳过: %s",
                        exc_detail(e),
                    )
            
            # Step 4 — 全量向量归档 + 事件拆分
            if _s(4) == 0:
                logger.info(f"执行 Step 4 - 向量归档与事件拆分，日期: {batch_date}")
                success, error_message = await self._step4_archive_daily_and_events(batch_date)
                if success:
                    await update_daily_batch_step_status(batch_date, 4, 1)
                    batch_log["step4_status"] = 1
                    logger.info(f"Step 4 完成，日期: {batch_date}")
                else:
                    await update_daily_batch_step_status(batch_date, 4, 0, error_message)
                    logger.error(f"Step 4 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 4 已跳过（已完成），日期: {batch_date}")
            
            # Step 5 — Chroma GC
            if _s(5) == 0:
                logger.info(f"执行 Step 5 - Chroma 记忆 GC，日期: {batch_date}")
                success, error_message = await self._step5_chroma_gc(batch_date)
                if success:
                    await update_daily_batch_step_status(batch_date, 5, 1)
                    logger.info(f"Step 5 完成，日期: {batch_date}")
                else:
                    await update_daily_batch_step_status(batch_date, 5, 0, error_message)
                    logger.error(f"Step 5 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 5 已跳过（已完成），日期: {batch_date}")
            
            logger.info(f"日终跑批处理完成，日期: {batch_date}")
            try:
                await reset_daily_batch_retry_count(batch_date)
            except Exception as e:
                logger.warning("重置 daily_batch_log.retry_count 失败（可忽略）: %s", e)
            return True
            
        except Exception as e:
            logger.error(f"日终跑批处理失败，日期: {batch_date}, 错误: {e}")
            if batch_date:
                try:
                    await update_daily_batch_step_status(batch_date, 1, 0, str(e))
                except Exception:
                    pass
            return False
    
    async def _step1_expire_temporal_states(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """Step 1：到期 temporal_states 置 inactive，摘要模型改写为过去时事实列表。"""
        self._settled_temporal_snippets = []
        try:
            now_iso = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            rows = await list_expired_active_temporal_states(now_iso)
            if not rows:
                logger.info(f"无到期 temporal_states，Step 1 空跑，日期: {batch_date}")
                return True, None
            
            ids = [str(r["id"]) for r in rows if r.get("id")]
            await deactivate_temporal_states_by_ids(ids)
            logger.info(f"已停用 {len(ids)} 条 temporal_states，日期: {batch_date}")
            
            contents = [str(r.get("state_content") or "").strip() or "（空）" for r in rows]
            prompt = f"""以下为已到期的进行中状态，到期日期：{batch_date}
将每条改写为简洁客观的过去时事实陈述，不新增信息，不编造内容。
严格输出一个JSON数组，长度、顺序与输入完全一致，元素为纯字符串，无额外文本。

输入 JSON 数组：
{json.dumps(contents, ensure_ascii=False)}
直接输出纯JSON字符串，严禁使用markdown代码块、严禁任何前言后语与解释文本。"""
            
            def _gen():
                return self._call_summary_llm_custom(self._persona_dialogue_prefix() + prompt)
                
            def _parse(raw):
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("not a list")
                return [str(x) for x in parsed]

            try:
                self._settled_temporal_snippets = await self._retry_call_and_parse("时效状态改写", _gen, _parse)
            except CedarClioOutputGuardExhausted:
                logger.warning("时效状态改写 Guard 用尽，使用原文兜底")
                self._settled_temporal_snippets = list(contents)
            except Exception as e:
                logger.warning(f"时效状态改写 JSON 解析最终失败，使用原文兜底: {e}")
                self._settled_temporal_snippets = list(contents)
            
            return True, None
        except Exception as e:
            logger.error(f"Step 1 执行失败: {e}")
            return False, str(e)
    
    async def _step2_generate_daily_summary(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 2 - 生成今日小传（prompt 开头附带 Step 1 结算的时效事件）。
        """
        try:
            chunk_summaries = await get_today_chunk_summaries(batch_date)

            if chunk_summaries:
                grouped: Dict[str, List[Dict[str, Any]]] = {}
                for summary in chunk_summaries:
                    sid = str(summary.get("session_id") or "daily_batch")
                    grouped.setdefault(sid, []).append(summary)

                memory_prefix = await self._memory_context_prefix()

                def _display_session(session_id: str) -> str:
                    if "_" in session_id:
                        parts = session_id.split("_")
                        if len(parts) >= 2:
                            return f"user{parts[0][:4]}...channel{parts[1][:4]}..."
                    return session_id[:40]

                saved_count = 0
                for session_id, session_chunks in grouped.items():
                    today_content = ""
                    if self._settled_temporal_snippets:
                        today_content += "# 本日已结算的时效状态（客观回顾）\n\n"
                        for line in self._settled_temporal_snippets:
                            today_content += f"- {line}\n"
                        today_content += "\n"

                    today_content += "# 今日对话摘要\n\n"
                    for summary in session_chunks:
                        summary_text = strip_lutopia_internal_memory_blocks(
                            str(summary.get("summary_text") or "")
                        )
                        created_at = summary["created_at"]
                        today_content += (
                            f"### {created_at} [来自: {_display_session(session_id)}]\n"
                            f"{summary_text}\n\n"
                        )

                    prompt = self._persona_dialogue_prefix() + memory_prefix + f"""请基于以下材料生成今日小传，按时间顺序完整概括当日核心话题、重要事件与情感状态。
要求：
- 篇幅控制在 150-600 字，内容丰富可写至上限，平淡日常满足 150 字即可。
- 完整保留关键互动细节、具体事实信息（数字、决策、名称、时间节点等），禁止空泛概括。
- 行文自然连贯，纯段落文本，无分点、无标题、无额外格式。
- 若包含时效状态结算内容，自然融合至正文，不单独拆分标注。
- 若材料中残留以「[系统通知]」开头的字样（审批结果回执之类的元事件），不要当对话引语处理；与正文话题相关时用客观第三方表述（如"南杉确认/驳回了某条记忆更新申请"），无关时整体省略。
{today_content}
今日小传（中文）："""

                    def _gen():
                        return self._call_summary_llm_custom(prompt)

                    def _parse(raw):
                        if not str(raw).strip():
                            raise ValueError("Empty summary")
                        return str(raw).strip()

                    try:
                        daily_summary = await self._retry_call_and_parse(
                            f"生成今日小传[{session_id}]", _gen, _parse
                        )
                    except CedarClioOutputGuardExhausted as e:
                        logger.error("生成今日小传 Guard 用尽，session=%s: %s", session_id, e)
                        return False, str(e)
                    except Exception as e:
                        logger.error("生成今日小传最终失败，session=%s: %s", session_id, e)
                        return False, str(e)

                    daily_id = await save_summary(
                        session_id=session_id,
                        summary_text=daily_summary,
                        start_message_id=0,
                        end_message_id=0,
                        summary_type="daily",
                        source_date=date.fromisoformat(batch_date),
                        is_group=1 if str(session_id).startswith("telegram_-") else 0,
                    )
                    await archive_chunk_summaries_by_daily(
                        batch_date,
                        daily_id,
                        session_id=session_id,
                    )
                    saved_count += 1

                logger.info("今日小传保存成功: %s 个 session，日期: %s", saved_count, batch_date)
                return True, None
            
            today_content = ""
            if self._settled_temporal_snippets:
                today_content += "# 本日已结算的时效状态（客观回顾）\n\n"
                for line in self._settled_temporal_snippets:
                    today_content += f"- {line}\n"
                today_content += "\n"
            
            if chunk_summaries:
                today_content += "# 今日对话摘要\n\n"
                for summary in chunk_summaries:
                    session_id = summary['session_id']
                    summary_text = strip_lutopia_internal_memory_blocks(
                        str(summary.get("summary_text") or "")
                    )
                    created_at = summary['created_at']
                    
                    if '_' in session_id:
                        parts = session_id.split('_')
                        if len(parts) >= 2:
                            display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                        else:
                            display_session = session_id[:20]
                    else:
                        display_session = session_id[:20]
                    
                    today_content += f"### {created_at} [来自: {display_session}]\n{summary_text}\n\n"
            
            if not today_content.strip():
                logger.info(f"今日没有内容需要生成小传，日期: {batch_date}")
                return True, None

            memory_prefix = await self._memory_context_prefix()
            prompt = self._persona_dialogue_prefix() + memory_prefix + f"""请基于以下材料生成今日小传，按时间顺序完整概括当日核心话题、重要事件与情感状态。
要求：
- 篇幅控制在150–400字，内容丰富可写至上限，平淡日常满足150字即可。
- 完整保留关键互动细节、具体事实信息（数字、决策、名称、时间节点等），禁止空泛概括。
- 行文自然连贯，纯段落文本，无分点、无标题、无额外格式。
- 若包含时效状态结算内容，自然融合至正文，不单独拆分标注。
- 若材料中残留以「[系统通知]」开头的字样（审批结果回执之类的元事件），不要当对话引语处理；与正文话题相关时用客观第三方表述（如"南杉确认/驳回了某条记忆更新申请"），无关时整体省略。
{today_content}
今日小传（中文）:"""
            
            def _gen():
                return self._call_summary_llm_custom(prompt)
                
            def _parse(raw):
                if not str(raw).strip():
                    raise ValueError("Empty summary")
                return str(raw).strip()
                
            try:
                daily_summary = await self._retry_call_and_parse("生成今日小传", _gen, _parse)
            except CedarClioOutputGuardExhausted as e:
                logger.error(f"生成今日小传 Guard 用尽，跳过写入: {e}")
                return False, str(e)
            except Exception as e:
                logger.error(f"生成今日小传最终失败: {e}")
                return False, str(e)
            
            daily_id = await save_summary(
                session_id="daily_batch",
                summary_text=daily_summary,
                start_message_id=0,
                end_message_id=0,
                summary_type="daily",
                source_date=date.fromisoformat(batch_date),
                is_group=0,
            )
            await archive_chunk_summaries_by_daily(batch_date, daily_id)
            
            logger.info(f"今日小传保存成功，日期: {batch_date}")
            return True, None
            
        except Exception as e:
            logger.error(f"Step 2 执行失败: {e}")
            return False, str(e)

    def _call_summary_llm_custom(self, prompt: str, response_format: Optional[Dict[str, Any]] = None) -> str:
        """使用摘要模型配置执行自定义 prompt（不经 micro_batch 的对话摘要模板包装）。"""
        sl = self.summary_llm
        base = int(getattr(sl, "max_tokens", 500) or 500)
        mt = min(2048, max(base, 900))
        return batch_one_shot_with_async_output_guard(
            messages=[{"role": "user", "content": prompt}],
            model_name=sl.model_name,
            api_key=sl.api_key or "",
            api_base=sl.api_base or "",
            timeout=sl.timeout,
            max_tokens=mt,
            platform=Platform.BATCH,
            max_retries=5,
            response_format=response_format,
        )

    async def _step4_retry_with_defaults(
        self,
        task_name: str,
        messages: List[Dict[str, str]],
        parse_func,
        default_value,
        batch_date: str,
        max_retries: int = 3,
    ):
        """Step 4 analysis 调用：失败 3 次后告警并返回默认值，跑批继续。"""
        llm = self.scoring_llm
        if llm is None:
            logger.warning("Step 4 %s 无可用 LLM，直接使用默认值", task_name)
            return default_value

        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                llm_resp = llm.generate_with_context_and_tracking(
                    messages,
                    platform=Platform.BATCH,
                    timeout_override_seconds=600,
                )
                raw = (llm_resp.content or "").strip()
                return parse_func(raw)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "Step 4 %s 第 %s/%s 次失败: %s",
                    task_name,
                    attempt,
                    max_retries,
                    exc_detail(e),
                )
                if attempt < max_retries:
                    await asyncio.sleep(2)

        logger.warning(
            "Step 4 %s 连续 %s 次失败，使用默认值继续: %s",
            task_name,
            max_retries,
            exc_detail(last_exc),
        )
        try:
            await send_telegram_main_user_text(
                f"⚠️ Step 4 {task_name} 连续 {max_retries} 次失败（{batch_date}），已使用默认 score=5、arousal=0.1 继续入库。\n{exc_detail(last_exc)}"
            )
        except Exception as te:
            logger.warning(
                "Step 4 %s 失败告警 Telegram 通知发送失败: %s",
                task_name,
                exc_detail(te),
                exc_info=True,
            )
        return default_value

    @staticmethod
    def _fallback_event_summary(summary_text: str) -> str:
        text = re.sub(r"\s+", " ", str(summary_text or "")).strip()
        if not text:
            return "全天平淡，主要在日常互动中度过。"
        return text[:300]

    @staticmethod
    def _clamp_score(value: Any, default: int = 5) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp_arousal(value: Any, default: float = 0.1) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp_enum(value: Any, allowed: List[str], default: str) -> str:
        """将 LLM 输出的 enum 值钳制到允许列表内。"""
        s = str(value or "").strip().lower()
        return s if s in allowed else default

    @staticmethod
    def _clamp_entities(value: Any, max_items: int = 5) -> List[str]:
        """将 LLM 输出的 entities 数组钳制为去重、去空、最多 max_items 项的字符串列表。"""
        if not isinstance(value, list):
            return []
        seen = set()
        result = []
        for item in value:
            s = str(item or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            result.append(s)
            if len(result) >= max_items:
                break
        return result

    def _normalize_step4_events(
        self,
        raw_events: Any,
        daily_summary_text: str,
        fallback_score: int,
        fallback_arousal: float,
        event_split_max: int,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if isinstance(raw_events, dict):
            raw_events = raw_events.get("events")
        if isinstance(raw_events, list):
            for item in raw_events:
                if isinstance(item, dict):
                    summary = str(item.get("summary") or item.get("text") or "").strip()
                    if not summary:
                        continue
                    score = self._clamp_score(item.get("score"), fallback_score)
                    arousal = self._clamp_arousal(item.get("arousal"), fallback_arousal)
                else:
                    summary = str(item).strip()
                    if not summary:
                        continue
                    score = fallback_score
                    arousal = fallback_arousal
                events.append(
                    {"summary": summary[:300], "score": score, "arousal": arousal}
                )
                if len(events) >= event_split_max:
                    break
        if not events:
            events.append(
                {
                    "summary": self._fallback_event_summary(daily_summary_text),
                    "score": fallback_score,
                    "arousal": fallback_arousal,
                }
            )
        return events

    @staticmethod
    def _parse_merged_content_json(raw: str) -> Optional[str]:
        """从模型返回中解析 JSON 对象的 content 字段。"""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                c = data.get("content")
                if c is not None and str(c).strip():
                    return str(c).strip()
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                data = json.loads(m.group())
                if isinstance(data, dict):
                    c = data.get("content")
                    if c is not None and str(c).strip():
                        return str(c).strip()
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _parse_state_merge_json(raw: str) -> Optional[Tuple[str, Optional[str]]]:
        """解析 current_status / preferences 合并结果：merged + discarded（可为 null）。"""
        raw_s = (raw or "").strip()
        if not raw_s:
            return None
        data: Any = None
        try:
            data = json.loads(raw_s)
        except json.JSONDecodeError:
            slice_json = DailyBatchProcessor._extract_first_json_object(raw_s)
            if slice_json:
                try:
                    data = json.loads(slice_json)
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return None
        merged = data.get("merged")
        if merged is None or not str(merged).strip():
            c = data.get("content")
            if c is not None and str(c).strip():
                merged = c
        if merged is None or not str(merged).strip():
            return None
        disc_raw = data.get("discarded")
        discarded: Optional[str]
        if disc_raw is None:
            discarded = None
        elif isinstance(disc_raw, str) and disc_raw.strip().lower() in (
            "null",
            "none",
            "",
        ):
            discarded = None
        else:
            discarded = str(disc_raw).strip() or None
        return str(merged).strip(), discarded

    @staticmethod
    def _extract_first_json_object(text: str) -> Optional[str]:
        """
        从可能含前置说明、markdown 代码块的文本中截取第一个平衡的 JSON 对象字符串。
        避免贪婪正则把多个 `}` 或字符串内的括号算错。
        """
        if not text:
            return None
        t = text.strip()
        if "```" in t:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
            if m:
                inner = m.group(1).strip()
                if inner.startswith("{"):
                    t = inner
        i = t.find("{")
        if i < 0:
            t = t.replace("｛", "{").replace("｝", "}")
            i = t.find("{")
        if i < 0:
            return None
        depth = 0
        in_str = False
        escape = False
        for j in range(i, len(t)):
            ch = t[j]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"' and not in_str:
                in_str = True
                continue
            if ch == '"' and in_str:
                in_str = False
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return t[i : j + 1]
        return None

    @staticmethod
    def _parse_step35_expire_at_string(s: str) -> Optional[datetime]:
        """解析 Step 3.5 模型给出的到期时间字符串（无时区 naive 墙钟时间）。"""
        raw = (s or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        try:
            d = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            return datetime.combine(d, time(23, 59, 59))
        except ValueError:
            return None

    def _normalize_step35_operations(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ns = data.get("new_states")
        da = data.get("deactivate_ids")
        adj = data.get("adjust_expire")
        return {
            "new_states": ns if isinstance(ns, list) else [],
            "deactivate_ids": da if isinstance(da, list) else [],
            "adjust_expire": adj if isinstance(adj, list) else [],
        }

    def _parse_step35_temporal_operations_json(self, raw: str) -> Optional[Dict[str, Any]]:
        """解析 Step 3.5 LLM 返回的 JSON 对象（整段 loads → 平衡大括号）。

        成功时返回含 new_states / deactivate_ids / adjust_expire 的归一化 dict；
        空响应、无法解码或非 JSON 对象时返回 None（整次白跑，由调用方决定是否重试）。
        """
        raw_s = (raw or "").strip()
        if not raw_s:
            return None
        data: Any = None
        try:
            data = json.loads(raw_s)
        except json.JSONDecodeError:
            slice_json = DailyBatchProcessor._extract_first_json_object(raw_s)
            if slice_json:
                try:
                    data = json.loads(slice_json)
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return None
        return self._normalize_step35_operations(data)

    async def _step35_extract_temporal_states(
        self, today_summary_text: str, batch_date: date
    ) -> None:
        """从今日小传解析时效操作 JSON，执行新增 / 停用 / 调整到期。"""
        text = (today_summary_text or "").strip()
        if not text:
            return

        active_rows = await get_all_active_temporal_states()
        existing_lines: List[str] = []
        for r in active_rows:
            rid = str(r.get("id") or "").strip()
            if not rid:
                continue
            sc = str(r.get("state_content") or "").strip()
            existing_lines.append(f"- id: {rid} | state_content: {sc or '（空）'}")
        existing_states_text = "\n".join(existing_lines) if existing_lines else "（无）"

        prompt = self._persona_dialogue_prefix() + f"""以下是今日小传：
{text}

当前激活中的时效状态（id 与正文，供 deactivate_ids / adjust_expire 引用；new_states 请勿与下列语义重复）：
{existing_states_text}

请根据今日小传输出**一个 JSON 对象**（不要 markdown 代码块、不要任何前言后语），结构固定为三键，三个数组均可为 []：

{{
  "new_states": [
    {{"state_content": "...", "action_rule": "...", "expire_at": "YYYY-MM-DD HH:MM:SS"}}
  ],
  "deactivate_ids": ["id1", "id2"],
  "adjust_expire": [
    {{"id": "xxx", "new_expire_at": "YYYY-MM-DD HH:MM:SS"}}
  ]
}}

字段说明与约束：
- new_states：小传中**新出现**的、具有明确时效性的状态（如生病备考、临时约定、特殊情绪阶段等）。state_content 一句话陈述句（建议不超过 50 字）；action_rule 为 AI 应对策略，无则 null 或空字符串；expire_at 为该状态预计结束的墙钟时间，格式严格 YYYY-MM-DD HH:MM:SS。
- deactivate_ids：**仅当**小传**明确**表明某条已有状态已结束、被否定或不再适用时填入对应 id；**禁止猜测**，不确定则 []。
- adjust_expire：**仅当**小传出现**明确**的新时间信息、需要改写某条已有状态的到期时刻时填入；**禁止猜测**，不确定则 []。

若无需任何操作，输出 {{"new_states":[],"deactivate_ids":[],"adjust_expire":[]}}。"""

        raw_resp = self._call_summary_llm_custom(prompt)
        ops = self._parse_step35_temporal_operations_json(raw_resp)
        if ops is None:
            raise ValueError("Step 3.5 JSON 解析完全失败，无法提取任何操作")

        active_ids = {str(r["id"]) for r in active_rows if r.get("id")}
        remaining_active = set(active_ids)

        # 1) new_states
        try:
            n_written = 0
            for it in ops.get("new_states") or []:
                if not isinstance(it, dict):
                    continue
                state_content = str(it.get("state_content") or "").strip()
                if not state_content:
                    continue
                exp_s = str(it.get("expire_at") or "").strip()
                expire_at = self._parse_step35_expire_at_string(exp_s)
                if expire_at is None:
                    logger.warning(
                        "[Step 3.5] new_states 跳过（expire_at 无法解析）: %s", exp_s
                    )
                    continue
                ar = it.get("action_rule")
                if ar is None or (
                    isinstance(ar, str) and ar.strip().lower() in ("null", "none", "")
                ):
                    action_rule: Optional[str] = None
                else:
                    action_rule = str(ar).strip() or None
                sid = f"auto_{batch_date.isoformat()}_{uuid4().hex[:8]}"
                try:
                    await save_temporal_state(
                        id=sid,
                        state_content=state_content,
                        action_rule=action_rule,
                        expire_at=expire_at,
                        is_active=1,
                    )
                    n_written += 1
                except Exception as e:
                    logger.warning(
                        "[Step 3.5] new_states 单条写入失败: %s", exc_detail(e)
                    )
            if n_written > 0:
                logger.info("[Step 3.5] 写入 %s 条新时效状态", n_written)
        except Exception as e:
            logger.warning("[Step 3.5] new_states 分支失败: %s", exc_detail(e))

        # 2) deactivate_ids（仅当前 is_active=1 的 id）
        try:
            raw_deact = ops.get("deactivate_ids") or []
            to_deact = [
                str(x).strip()
                for x in raw_deact
                if str(x).strip() and str(x).strip() in active_ids
            ]
            if to_deact:
                n = await deactivate_temporal_states_by_ids(to_deact)
                remaining_active.difference_update(to_deact)
                if n > 0:
                    logger.info("[Step 3.5] 已停用 %s 条时效状态", n)
        except Exception as e:
            logger.warning("[Step 3.5] deactivate_ids 分支失败: %s", exc_detail(e))

        # 3) adjust_expire
        try:
            raw_adj = ops.get("adjust_expire") or []
            n_adj = 0
            for it in raw_adj:
                if not isinstance(it, dict):
                    continue
                sid = str(it.get("id") or "").strip()
                if not sid or sid not in remaining_active:
                    continue
                exp_s = str(it.get("new_expire_at") or "").strip()
                new_exp = self._parse_step35_expire_at_string(exp_s)
                if new_exp is None:
                    logger.warning(
                        "[Step 3.5] adjust_expire 跳过（时间无法解析）: id=%s raw=%s",
                        sid,
                        exp_s,
                    )
                    continue
                try:
                    rc = await update_temporal_state_expire_at(sid, new_exp)
                    if rc:
                        n_adj += 1
                except Exception as e:
                    logger.warning(
                        "[Step 3.5] adjust_expire 单条失败 id=%s: %s",
                        sid,
                        exc_detail(e),
                    )
            if n_adj > 0:
                logger.info("[Step 3.5] 已调整 %s 条时效状态到期时间", n_adj)
        except Exception as e:
            logger.warning("[Step 3.5] adjust_expire 分支失败: %s", exc_detail(e))

    @staticmethod
    def _serialize_dimension_content(dimension: str, raw_value: Any) -> str:
        """将 LLM 输出的结构化数据序列化为可读文本，存入 memory_cards.content。"""
        if dimension == "current_status":
            # 单 object: {updated_at, content}
            if isinstance(raw_value, dict):
                return raw_value.get("content", "")
            return str(raw_value) if raw_value else ""

        # 其余维度均为 array
        if not isinstance(raw_value, list) or len(raw_value) == 0:
            return ""

        lines = []
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            date_str = item.get("date", "")
            content = item.get("content", "")

            if dimension == "goals":
                status = item.get("status", "")
                lines.append(f"[{date_str}] {content} ({status})")
            elif dimension == "key_events":
                importance = item.get("importance", "")
                lines.append(f"[{date_str}] {content} (重要度:{importance})")
            elif dimension == "interaction_patterns":
                pattern = item.get("pattern", "")
                freq = item.get("frequency", "")
                lines.append(f"[{date_str}] {pattern} (频率:{freq})")
            elif dimension == "rules":
                established_by = item.get("established_by", "")
                lines.append(f"[{date_str}] {content} (来源:{established_by})")
            elif dimension == "preferences":
                evidence_date = item.get("evidence_date", "")
                lines.append(f"[{evidence_date}] {content}")
            elif dimension == "relationships":
                event = item.get("event", "")
                polarity = item.get("polarity", "")
                lines.append(f"[{date_str}] {event} ({polarity})")
            else:
                lines.append(f"[{date_str}] {content}" if date_str else content)

        return "\n".join(lines)

    async def _merge_memory_card_contents(
        self,
        dimension: str,
        dimension_label: str,
        old_content: str,
        new_content: str,
        batch_date: str,
    ) -> _MemoryMergeResult:
        """
        将既有卡片与今日提取的新文案交给模型合并。
        current_status / preferences：JSON 含 merged、discarded（覆盖片段，无则 null）。
        其余维度：JSON 含 content；discarded 恒为 None。
        失败时回退为简单拼接，discarded 为 None（不触发归档）。
        """
        old_trim = old_content.strip()
        if len(old_trim) > 6000:
            old_trim = "…（前文已截断）\n" + old_trim[-6000:]

        fallback = f"{old_content.strip()}\n[{batch_date}更新] {new_content.strip()}"

        if dimension in ("current_status", "preferences"):
            prompt = self._persona_dialogue_prefix() + f"""你是专业的记忆整理助手，将「既有记忆卡片」与「今日新增摘要」合并为可长期存储的记忆内容。
该维度（{dimension_label}）记录用户当前生活状态或个人偏好。
输出 JSON 两个字段：
- merged：整合后的新卡片正文；单张不超过 1000 字；去重、语义连贯；全文禁止使用「今天」「最近」等模糊相对时间词；纯段落文本，无列表、无编号。
- discarded：仅当新信息**覆盖、否定或取代**旧描述中的具体片段时，写出被新内容覆盖掉的旧描述片段（简要即可）。若为纯追加、无冲突、或仅去重润色，discarded 必须为 null。不要将可有可无的删减放入 discarded。
维度代码：{dimension}
今日日期：{batch_date}
【既有记忆卡片】
{old_trim}
【今日新增】
{new_content.strip()}
输出要求：直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁 markdown 代码块、严禁前言后语。格式：{{"merged":"…","discarded":null}}；discarded 为字符串或 null。"""

            def _gen():
                return self._call_summary_llm_custom(prompt)

            def _parse(raw):
                p = self._parse_state_merge_json(raw)
                if p:
                    return _MemoryMergeResult(p[0], p[1])
                raise ValueError(
                    f"状态维度合并 JSON 解析失败; 原始片段: {raw[:200] if raw else '(空)'}"
                )

            try:
                return await self._retry_call_and_parse("合并记忆卡片", _gen, _parse)
            except CedarClioOutputGuardExhausted as e:
                logger.warning(f"合并记忆卡片 Guard 用尽，使用拼接回退: {e}")
                return _MemoryMergeResult(fallback, None)
            except Exception as e:
                logger.warning(f"使用拼接回退: {e}")
                return _MemoryMergeResult(fallback, None)

        if dimension == "interaction_patterns":
            merge_rules = (
                "该维度用于记录有真实对话支撑的相处行为观察。"
                "合并要求：只保留结论性规律，严禁记录单次事件的详细过程、对话细节、情绪流水账；"
                "重复内容合并为一句；若新旧观察矛盾，可并列保留并标注日期。语言简洁凝练，不做冗余描述。"
            )
        else:
            merge_rules = (
                "以今日新增信息为准，补充或修正过时内容；相同事实只保留一次，自然融合成连贯文本，禁止简单拼接、禁止重复啰嗦、禁止冗余展开。"
            )

        contradiction_bullet = (
            "- 如新旧信息存在矛盾，保留两者并严格使用 [YYYY-MM-DD] 格式在新增内容前标注日期，不要静默覆盖；\n"
        )

        prompt = self._persona_dialogue_prefix() + f"""你是专业的记忆整理助手，负责将「既有记忆卡片」与「今日新增摘要」进行高质量合并，输出稳定、精炼、可长期存储的记忆内容。
合并规则：
- 去除重复信息，将新内容自然整合到原有记忆中，保持语义连贯。
- 单张记忆卡片总字数**严格不超过 1000 字**，内容过长时自动提炼核心、精简合并，不得超字数。
{contradiction_bullet}- 全文禁止使用「今天」「最近」等模糊相对时间词汇。
- 输出为一段连贯的纯文本，不要列表、不要编号、不要任何格式符号。
- 必须完整保留所有有效旧记忆，仅做精简提炼，不得随意删除有效信息。
- 对于已被新信息明确取代的过时内容，标注「（已更新）」后简短保留即可，无需展开。。
维度代码：{dimension}
维度说明：{dimension_label}
今日日期：{batch_date}
【既有记忆卡片】
{old_trim}
【今日新增】
{new_content.strip()}
维度补充：
{merge_rules}
输出要求：直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁使用 markdown 代码块包裹，严禁输出任何分析过程或前言后语。格式为 {{"content":"合并后的正文"}}。"""

        def _gen():
            return self._call_summary_llm_custom(prompt)

        def _parse(raw):
            m = self._parse_merged_content_json(raw)
            if m:
                return _MemoryMergeResult(m, None)
            raise ValueError(
                f"记忆卡片合并 JSON 解析失败; 原始片段: {raw[:200] if raw else '(空)'}"
            )

        try:
            return await self._retry_call_and_parse("合并记忆卡片", _gen, _parse)
        except CedarClioOutputGuardExhausted as e:
            logger.warning(f"合并记忆卡片 Guard 用尽，使用拼接回退: {e}")
            return _MemoryMergeResult(fallback, None)
        except Exception as e:
            logger.warning(f"使用拼接回退: {e}")
            return _MemoryMergeResult(fallback, None)

    def _rewrite_discarded_state_for_archive(
        self, discarded_content: str, batch_date: str
    ) -> Tuple[str, bool]:
        """
        将 discarded 片段改写为历史陈述后入库；失败则降级为原文 + rewrite_failed。
        走异步 batch guard，最多 3 次重试。
        """
        sl = self.summary_llm
        base = int(getattr(sl, "max_tokens", 500) or 500)
        mt = min(512, max(base, 256))
        prompt = f"""将下面这段已过期的状态描述改写为历史陈述，格式固定为：

[已被覆盖的旧状态 · 记录于 {batch_date}] + 过去时陈述正文

要求：
- 使用过去时
- 保留关键事实，删除行为指令性内容（如"每天提醒"之类）
- 控制在 80 字以内

原文：
{discarded_content}"""
        try:
            raw = batch_one_shot_with_async_output_guard(
                messages=[
                    {"role": "user", "content": self._persona_dialogue_prefix() + prompt}
                ],
                model_name=sl.model_name,
                api_key=sl.api_key or "",
                api_base=sl.api_base or "",
                timeout=sl.timeout,
                max_tokens=mt,
                platform=Platform.BATCH,
                max_retries=3,
            )
            text = (raw or "").strip()
            if not text:
                raise ValueError("empty rewrite")
            prefix = f"[已被覆盖的旧状态 · 记录于 {batch_date}]"
            if prefix not in text:
                text = f"{prefix} {text}"
            return text, False
        except CedarClioOutputGuardExhausted:
            logger.warning("归档改写 Guard 用尽，降级存原文")
        except Exception as e:
            logger.warning("归档改写失败，降级存原文: %s", e)
        prefix = f"[已被覆盖的旧状态 · 记录于 {batch_date}]"
        fb = f"{prefix} {discarded_content.strip()}"
        return fb, True

    async def _persist_state_archive_chunk(
        self,
        user_id: str,
        character_id: str,
        dimension: str,
        batch_date: str,
        discarded_content: str,
    ) -> None:
        """仅归档被覆盖的旧片段（经改写）；含 BM25 增量。"""
        doc_id = f"state_{user_id}_{character_id}_{dimension}_{batch_date}"
        body, rewrite_failed = self._rewrite_discarded_state_for_archive(
            discarded_content, batch_date
        )
        archive_meta: Dict[str, Any] = {
            "date": str(batch_date),
            "session_id": f"{user_id}_{character_id}",
            "summary_type": "state_archive",
            "source": "state_archive",
            "dimension": dimension,
            "archived_at": str(batch_date),
            "original_dimension": dimension,
            "base_score": 5,
            "halflife_days": 90,
        }
        if rewrite_failed:
            archive_meta["rewrite_failed"] = True
        try:
            if not add_memory(doc_id, body, archive_meta):
                logger.warning(
                    "状态 discarded 归档向量库失败 doc_id=%s user=%s dim=%s",
                    doc_id,
                    user_id,
                    dimension,
                )
            else:
                try:
                    try:
                        from .bm25_retriever import (
                            add_document_to_bm25,
                            refresh_bm25_index,
                        )
                    except ImportError:
                        from memory.bm25_retriever import (
                            add_document_to_bm25,
                            refresh_bm25_index,
                        )

                    if not add_document_to_bm25(doc_id, body, dict(archive_meta)):
                        refresh_bm25_index()
                except Exception as ex:
                    logger.warning("状态归档 BM25 增量失败 doc_id=%s: %s", doc_id, ex)
        except Exception as ex:
            logger.warning(
                "状态 discarded 归档向量库异常 doc_id=%s: %s",
                doc_id,
                ex,
            )
    
    async def _step3_memory_cards_and_timeline(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 3 - 更新记忆卡片（Upsert），并在末尾尝试写入 relationship_timeline。
        
        把今日小传内容发给 LLM，判断是否包含属于以下7个维度的新信息：
        preferences / interaction_patterns / current_status / goals / relationships / key_events / rules
        
        有新信息则查 memory_cards 表，没有对应维度就 INSERT，有则再经模型将新旧内容合并重写为一段去重后的正文后 UPDATE。
        
        interaction_patterns 维度特别说明：只记录有具体对话支撑的行为观察，不做性格定论；明显矛盾时可并列保留并带时间/场景提示，但仍需去除重复表述。
        
        Args:
            batch_date: 批处理日期
            
        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        try:
            # 1. 获取本 batch_date 的 daily 摘要（按 source_date 对齐，支持补跑历史日）
            daily_summary = await self._combined_daily_summary_by_date(batch_date)
            if not daily_summary:
                logger.info(f"今日没有 daily 摘要，跳过 Step 3，日期: {batch_date}")
                return True, None

            summary_text = daily_summary['summary_text']
            summary_row_id = daily_summary.get("id")
            logger.info(f"获取到今日小传，长度: {len(summary_text)}，日期: {batch_date}")

            # 2. 从今日小传中提取涉及的 user_id 和 character_id
            #    从 messages 表中查询今日有过对话的用户列表
            try:
                from memory.database import get_database
                db = get_database()
                user_rows = await db.get_today_user_character_pairs(batch_date)
                user_character_pairs = [
                    (row['user_id'], row['character_id']) for row in user_rows
                    if row['user_id'] and row['character_id']
                ]
            except Exception as e:
                logger.warning(f"查询今日用户列表失败，使用默认值: {e}")
                user_character_pairs = []

            # 如果没有查到用户，跳过当天 Step 3
            if not user_character_pairs:
                logger.info("今日无用户对话记录，跳过 Step 3 记忆卡片更新，日期: %s", batch_date)
                return True, None

            # 3. 构建 LLM Prompt，要求按 7 个维度分析今日小传
            dimensions_desc = {
                "preferences": "偏好与喜恶（食物、音乐、活动、风格等具体偏好）",
                "interaction_patterns": "相处模式（仅记录有对话支撑的结论性行为规律，标注日期，不做性格定论，不写单次事件细节；矛盾信息并存保留）",
                "current_status": "近况与生活动态（当前工作、学习、健康、居住等状态）",
                "goals": "目标与计划（短期或长期的目标、计划、心愿）",
                "relationships": "重要关系（家人、朋友、同事等重要人物及关系）",
                "key_events": "重要事件（值得长期记录的重大事件、里程碑）",
                "rules": "相处规则与禁区（用户明确表达的偏好规则、禁忌话题）"
            }
            dimensions_list = "\n".join([f"- {k}：{v}" for k, v in dimensions_desc.items()])

            _dim_order = [
                "preferences",
                "interaction_patterns",
                "current_status",
                "goals",
                "relationships",
                "key_events",
                "rules",
            ]
            _dim_labels = {
                "preferences": "偏好与喜恶",
                "interaction_patterns": "相处模式",
                "current_status": "近况与生活动态",
                "goals": "目标与计划",
                "relationships": "重要关系",
                "key_events": "重要事件",
                "rules": "相处规则与禁区",
            }
            old_cards_lines = []
            for dim in _dim_order:
                card = await get_latest_memory_card_for_dimension(
                    self._batch_user_id, self._batch_char_id, dim
                )
                if card and card.get("content"):
                    old_cards_lines.append(
                        f"{dim}（{_dim_labels[dim]}）：{card['content']}"
                    )
            old_cards_block = ""
            if old_cards_lines:
                old_cards_block = (
                    "（既有记忆卡片，仅供对比，禁止直接复制）：\n"
                    + "\n\n".join(old_cards_lines)
                    + "\n\n"
                )

            prompt = f"""请仔细阅读今日小传，从中仅提取客观、明确的新事实信息，严格按照指定 JSON Schema 输出，禁止推理、禁止编造、禁止扩写。
{old_cards_block}今日小传（{batch_date}）：
{summary_text}

请按以下 7 个维度提取今日小传中出现的新信息：
{dimensions_list}

输出规则：
1. 只写结论性事实，禁止写事件过程、对话细节、流水账描述。
2. 同一条信息只归入语义最相关的维度，禁止跨维度重复记录。
3. 对比现有记忆，仅提取今日新增的、发生状态变化的、或与旧认知有冲突的增量信息。
4. 无对应素材的维度：数组类型返回空数组 []，current_status 返回 null。
5. 所有日期格式：YYYY-MM-DD。
6. 字数限制：interaction_patterns.pattern 不超过 150 字；其余文本字段不超过 80 字。

few-shot 示例：
输入：「南杉今天说以后不吃辣了，还提到了下周要去华为面试。」
输出：
{{
  "current_status": null,
  "goals": [{{"date": "{batch_date}", "content": "下周华为面试", "status": "active"}}],
  "key_events": [],
  "interaction_patterns": [],
  "rules": [{{"date": "{batch_date}", "content": "不吃辣", "established_by": "南杉"}}],
  "preferences": [{{"content": "不吃辣", "evidence_date": "{batch_date}"}}],
  "relationships": []
}}

输入：「今天啥也没聊，就是闲聊打了个招呼。」
输出：
{{
  "current_status": null,
  "goals": [],
  "key_events": [],
  "interaction_patterns": [],
  "rules": [],
  "preferences": [],
  "relationships": []
}}"""

            # 4. 调用 SUMMARY LLM 分析维度
            logger.info(f"调用 LLM 分析今日小传维度，日期: {batch_date}")

            def _gen_dim():
                role_prefix = f"对话角色：{self._batch_char_name} 与 {self._batch_user_name}。\n"
                return self._call_summary_llm_custom(role_prefix + prompt, response_format=STEP3_JSON_SCHEMA)

            def _parse_dim(raw_resp):
                raw_resp = (raw_resp or "").strip()
                try:
                    return json.loads(raw_resp)
                except json.JSONDecodeError as e:
                    slice_json = self._extract_first_json_object(raw_resp)
                    if slice_json:
                        try:
                            return json.loads(slice_json)
                        except json.JSONDecodeError: pass
                    json_match = re.search(r"\{[\s\S]*\}", raw_resp)
                    if json_match:
                        try:
                            return json.loads(json_match.group())
                        except json.JSONDecodeError: pass
                    raise ValueError(f"JSON 解析失败: {e}, 原始响应前500字: {raw_resp[:500]}")

            try:
                dimension_data = await self._retry_call_and_parse("提取今日小传维度", _gen_dim, _parse_dim)
            except CedarClioOutputGuardExhausted as e:
                logger.error(f"提取今日小传维度 Guard 用尽，Step 3 中止: {e}")
                return False, f"LLM Guard 失败: {e}"
            except Exception as e:
                logger.error(f"提取今日小传维度失败，Step 3 中止: {e}")
                return False, f"LLM 调用或解析失败: {e}"

            logger.info(f"LLM 维度分析完成，有内容的维度: {[k for k, v in dimension_data.items() if v is not None and v != 'null' and not (isinstance(v, list) and len(v) == 0)]}")

            # 6. 对每个用户执行 Upsert
            for user_id, character_id in user_character_pairs:
                logger.info(f"更新记忆卡片: user_id={user_id}, character_id={character_id}")

                for dimension in self.dimensions:
                    # 单个维度失败不影响其他维度
                    try:
                        raw_value = dimension_data.get(dimension)

                        # 跳过 null / 空数组 / 空值
                        if raw_value is None or raw_value == "null":
                            logger.debug(f"维度 {dimension} 无新信息，跳过")
                            continue
                        if isinstance(raw_value, list) and len(raw_value) == 0:
                            logger.debug(f"维度 {dimension} 无新信息（空数组），跳过")
                            continue

                        # 将结构化数据序列化为可读文本
                        new_content = self._serialize_dimension_content(dimension, raw_value)
                        if not new_content:
                            logger.debug(f"维度 {dimension} 序列化后为空，跳过")
                            continue

                        # 该维度最近一条（含 is_active=0），便于「全表软删后重跑」仍更新同一行
                        existing_card = await get_latest_memory_card_for_dimension(
                            user_id, character_id, dimension
                        )

                        # manual_override 检查：如果该行被手动覆盖，跳过
                        if existing_card and existing_card.get("manual_override"):
                            logger.info(
                                "维度 %s 的记忆卡片 id=%s 已被手动覆盖，跳过自动更新",
                                dimension, existing_card["id"],
                            )
                            continue

                        if existing_card:
                            # 已有记录 → 模型合并去重后 UPDATE，并重新激活
                            card_id = existing_card["id"]
                            old_content = existing_card["content"]
                            dim_label = dimensions_desc.get(dimension, dimension)
                            merge_out = await self._merge_memory_card_contents(
                                dimension,
                                dim_label,
                                old_content,
                                new_content,
                                batch_date,
                            )
                            merged_content = merge_out.merged
                            if (
                                dimension in ("current_status", "preferences")
                                and merge_out.discarded
                            ):
                                await self._persist_state_archive_chunk(
                                    user_id,
                                    character_id,
                                    dimension,
                                    batch_date,
                                    merge_out.discarded,
                                )

                            await update_memory_card(
                                card_id,
                                merged_content,
                                dimension=None,
                                reactivate=True,
                            )
                            logger.info(f"更新记忆卡片: dimension={dimension}, card_id={card_id}")

                        else:
                            # 无记录 → INSERT
                            card_id = await save_memory_card(
                                user_id=user_id,
                                character_id=character_id,
                                dimension=dimension,
                                content=f"[{batch_date}] {new_content}",
                                source_message_id=f"daily_batch_{batch_date}"
                            )
                            logger.info(f"新增记忆卡片: dimension={dimension}, card_id={card_id}")

                    except Exception as e:
                        # 单个维度失败，记录日志后继续处理其他维度
                        logger.error(f"处理维度 {dimension} 失败（user={user_id}），跳过: {e}")
                        continue

            settled_block = (
                "\n".join(f"- {s}" for s in self._settled_temporal_snippets)
                if self._settled_temporal_snippets
                else "（无）"
            )
            tl_prompt = f"""这是 {self._batch_char_name} 与 {self._batch_user_name} 的对话记录整理。
今日小传（{batch_date}）：
{summary_text}
本日已结算的时效状态（客观陈述）：
{settled_block}
请严格判断今日是否存在具备长期记录在「关系时间轴」价值的关系事件（普通平静日常直接判定为无，禁止强行凑数），可包含上述时效结算中的关系变化。
event_type 说明：
- milestone：关系性质转折或核心里程碑
- emotional_shift：情绪基调明显变化（争吵、和好、感情升温等）
- conflict：明确的冲突或摩擦
- daily_warmth：仅收录当天极具特殊性的温馨互动，普通日常严禁写入
content 强制要求：
1. 视角限定：全程第三人称客观记录，禁用我/你，统一使用 {self._batch_char_name}、{self._batch_user_name}；禁用今天/昨天等相对时间词。
2. 字数严格控制：20-60字，简洁陈述事实，无多余修饰。
3. 风格要求：无主观抒情、无夸张描述，以记忆为时间轴保证记录真实客观、简洁清晰。
符合条件则输出标准JSON：{{"events":[{{"event_type":"...","content":"..."}}]}}；无符合事件则输出：{{"events":[]}}。
直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁使用 markdown 的 json 代码块包裹，严禁输出任何前言后语、额外解释文本。"""

            def _gen_tl():
                return self._call_summary_llm_custom(tl_prompt)

            def _parse_tl(raw):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    jm = re.search(r"\{[\s\S]*\}", raw)
                    if jm:
                        return json.loads(jm.group())
                    raise ValueError("JSON parse error")

            try:
                tl_data = await self._retry_call_and_parse("提取关系时间轴", _gen_tl, _parse_tl)
            except CedarClioOutputGuardExhausted as e:
                logger.warning(f"关系时间轴 Guard 用尽，跳过写入: {e}")
                tl_data = {"events": []}
            except Exception as e:
                logger.warning(f"关系时间轴 LLM 解析最终失败，跳过写入: {e}")
                tl_data = {"events": []}

            events_tl = tl_data.get("events") if isinstance(tl_data, dict) else []
            if isinstance(events_tl, list):
                sid = str(summary_row_id) if summary_row_id is not None else None
                rtl_created_at = datetime.combine(
                    date.fromisoformat(batch_date), time(23, 59, 59)
                )
                for ev in events_tl:
                    if not isinstance(ev, dict):
                        continue
                    et = str(ev.get("event_type") or "").strip()
                    content = str(ev.get("content") or "").strip()
                    if not content:
                        continue
                    try:
                        await insert_relationship_timeline_event(
                            event_type=et,
                            content=content,
                            source_summary_id=sid,
                            created_at=rtl_created_at,
                        )
                        logger.info("relationship_timeline 已插入 type=%s", et)
                    except ValueError:
                        logger.warning("跳过无效 event_type: %s", et)
                    except Exception as e:
                        logger.error(f"写入 relationship_timeline 失败: {e}")

            logger.info(f"Step 3 完成，日期: {batch_date}")
            return True, None

        except Exception as e:
            logger.error(f"Step 3 执行失败: {e}")
            return False, str(e)

    async def _step4a_cluster(
        self,
        chunks: List[Dict[str, Any]],
        llm_config: Optional[LLMInterface],
    ) -> List[List[int]]:
        """Step 4a - 只聚类 chunk id；失败时每个 chunk 单独成组。"""
        valid_ids = [int(c["id"]) for c in chunks]
        valid_id_set = set(valid_ids)
        chunk_lines = []
        for c in chunks:
            chunk_lines.append(
                f"- chunk_id={c['id']}\n"
                f"{strip_lutopia_internal_memory_blocks(str(c.get('summary_text') or ''))}"
            )
        prompt = self._persona_dialogue_prefix() + f"""【任务】
以下是今天按时间顺序的对话片段摘要。请只根据语义主题把 chunk_id 聚类：同一事件/话题放在同一组，不同事件/话题分开。

【输入】
{chr(10).join(chunk_lines)}

【要求】
- 只返回 JSON 数组，不要解释、不要 Markdown、不要其他文字
- 输出格式必须是：[[1,2,3],[4,5],[6]]
- 只能使用输入中出现过的 chunk_id
- 尽量覆盖所有输入 chunk_id
- 平淡的天可以聚成 1-2 组，话题明显分散时可以更多组"""

        if llm_config is None:
            logger.warning("Step 4a 无可用 analysis LLM，回退为每个 chunk 单独成组")
            return [[cid] for cid in valid_ids]

        def _parse_groups(raw: str) -> List[List[int]]:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r"\[[\s\S]*\]", raw or "")
                if not m:
                    raise ValueError("Step 4a JSON parse error")
                parsed = json.loads(m.group())
            if not isinstance(parsed, list):
                raise ValueError("Step 4a JSON must be an array")

            groups: List[List[int]] = []
            seen_ids = set()
            for raw_group in parsed:
                if not isinstance(raw_group, list):
                    raise ValueError("Step 4a group must be an array")
                group: List[int] = []
                illegal_ids: List[Any] = []
                for raw_id in raw_group:
                    try:
                        cid = int(raw_id)
                    except (TypeError, ValueError):
                        illegal_ids.append(raw_id)
                        continue
                    if cid not in valid_id_set:
                        illegal_ids.append(raw_id)
                        continue
                    if cid in seen_ids or cid in group:
                        continue
                    group.append(cid)
                if illegal_ids:
                    logger.error("Step 4a 聚类包含非法 chunk_id，已过滤: %s", illegal_ids)
                if group:
                    groups.append(group)
                    seen_ids.update(group)

            missing_ids = [cid for cid in valid_ids if cid not in seen_ids]
            if missing_ids:
                logger.warning("Step 4a 聚类遗漏 chunk_id，追加为单独分组: %s", missing_ids)
                groups.extend([[cid] for cid in missing_ids])
            if not groups:
                raise ValueError("Step 4a 校验后没有合法分组")
            return groups

        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                llm_resp = llm_config.generate_with_context_and_tracking(
                    [{"role": "user", "content": prompt}],
                    platform=Platform.BATCH,
                    timeout_override_seconds=600,
                )
                return _parse_groups((llm_resp.content or "").strip())
            except Exception as e:
                last_exc = e
                logger.warning("Step 4a 聚类第 %s/3 次失败: %s", attempt, exc_detail(e))
                if attempt < 3:
                    await asyncio.sleep(2)

        logger.warning(
            "Step 4a 聚类连续 3 次失败，回退为每个 chunk 单独成组: %s",
            exc_detail(last_exc),
        )
        return [[cid] for cid in valid_ids]

    async def _step4b_describe_and_score(
        self,
        chunk_group_content: str,
        llm_config: Optional[LLMInterface],
    ) -> Optional[Dict[str, Any]]:
        """Step 4b - 对单个 chunk 分组生成事件描述、score、arousal + 4 标签。"""
        _themes_str = " / ".join(EVENT_THEMES)
        _emotions_str = " / ".join(EVENT_EMOTIONS)
        _types_str = " / ".join(EVENT_TYPES)

        prompt = self._persona_dialogue_prefix() + f"""【任务】
请把下面同一事件/话题下的对话片段摘要合并成一条长期记忆事件，并评估长期保留价值、情绪强度，以及主题标签。

【输入】
{chunk_group_content}

【输出 schema】
{{"content": "事件描述，100-300 字", "score": 7, "arousal": 0.5, "theme": "daily_life", "entities": ["人名/组织名"], "emotion": "neutral", "event_type": "daily_warmth"}}

【要求】
- 只返回单条 JSON 对象，不要解释、不要 Markdown、不要其他文字
- content 必须是完整、可独立理解的事件描述
- score 是 1-10 的整数，表示长期保留价值
- arousal 是 0.0-1.0 的浮点数，表示情绪强度（不分正负）
- theme: 事件主题，从以下取值选择：{_themes_str}
- entities: 事件涉及的实体（人名、组织名、产品名等），最多 5 个。必须是有意义的专有名词，禁止填入「今天」「南杉」「东西」等泛指词。无明确实体时返回空数组 []
- emotion: 主导情绪，从以下取值选择：{_emotions_str}
- event_type: 事件类型，从以下取值选择：{_types_str}

【评分参考】
score:
- 8-10: 重大事件、强烈情感、关键决定
- 4-7: 有意义的互动、值得回忆的日常
- 1-3: 平淡的日常对话、重复性内容（让时间衰减处理）

arousal:
- 0.7+: 强情绪事件（吵架、惊喜、感动、暴怒）
- 0.3-0.6: 有情绪起伏的对话
- 0.0-0.2: 平静日常"""

        if llm_config is None:
            logger.warning("Step 4b 无可用 analysis LLM，返回 None")
            return None

        def _parse_desc(raw: str) -> Dict[str, Any]:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r"\{[\s\S]*\}", raw or "")
                if not m:
                    raise ValueError("Step 4b JSON parse error")
                parsed = json.loads(m.group())
            if not isinstance(parsed, dict):
                raise ValueError("Step 4b JSON must be an object")
            content = str(parsed.get("content") or parsed.get("summary") or "").strip()
            if not content:
                raise ValueError("Step 4b content is empty")
            return {
                "content": content[:300],
                "score": self._clamp_score(parsed.get("score"), 5),
                "arousal": self._clamp_arousal(parsed.get("arousal"), 0.1),
                "theme": self._clamp_enum(parsed.get("theme"), EVENT_THEMES, "other"),
                "entities": self._clamp_entities(parsed.get("entities"), max_items=5),
                "emotion": self._clamp_enum(parsed.get("emotion"), EVENT_EMOTIONS, "neutral"),
                "event_type": self._clamp_enum(parsed.get("event_type"), EVENT_TYPES, "other"),
            }

        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                llm_resp = llm_config.generate_with_context_and_tracking(
                    [{"role": "user", "content": prompt}],
                    platform=Platform.BATCH,
                    timeout_override_seconds=600,
                )
                return _parse_desc((llm_resp.content or "").strip())
            except Exception as e:
                last_exc = e
                logger.warning("Step 4b 描述打分第 %s/3 次失败: %s", attempt, exc_detail(e))
                if attempt < 3:
                    await asyncio.sleep(2)

        logger.error("Step 4b 描述打分连续 3 次失败，丢弃该组: %s", exc_detail(last_exc))
        return None
    
    async def _step4_archive_daily_and_events(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 4 - 以当天 chunk 列表为输入，合并为长期事件片段写入 ChromaDB + BM25 + PG 镜像。
        """
        try:
            daily_summary = await self._combined_daily_summary_by_date(batch_date)
            if not daily_summary:
                logger.info(f"今日没有小传，跳过 Step 4，日期: {batch_date}")
                return True, None

            summary_id = daily_summary['id']
            event_split_max = await _event_split_max()
            all_chunks = await get_today_chunk_summaries(batch_date, include_archived=True)
            # get_today_chunk_summaries 已用 <= batch_date，无需再过滤
            if not all_chunks:
                logger.info(f"今日没有 chunk 可供 Step 4 拆分，日期: {batch_date}")
                return True, None

            # 过滤掉 external_events_generated=true 的 chunk（其事件已在 add_external_chunk 时写入）
            chunks = [c for c in all_chunks if not c.get("external_events_generated")]
            external_chunks = [c for c in all_chunks if c.get("external_events_generated")]
            if external_chunks:
                logger.info(
                    "Step 4 跳过 %d 条 external chunk（事件已预生成），日期: %s",
                    len(external_chunks), batch_date,
                )
            if not chunks:
                logger.info(f"今日仅有 external chunk，跳过聚类，日期: {batch_date}")
                # 仍需回填 external chunk 的 archived_by
                try:
                    await archive_external_chunks_by_daily(batch_date, summary_id)
                except Exception as e:
                    logger.warning("回填 external chunk archived_by 失败: %s", e)
                return True, None

            valid_chunk_ids = {int(c["id"]) for c in chunks}
            chunk_by_id = {int(c["id"]): c for c in chunks}
            chunk_lines = []
            for c in chunks:
                chunk_lines.append(
                    f"- chunk_id={c['id']} | created_at={c.get('created_at')} | "
                    f"session={c.get('session_id')}\n{strip_lutopia_internal_memory_blocks(str(c.get('summary_text') or ''))}"
                )
            chunk_input = "\n\n".join(chunk_lines)
            
            parent_doc_id = build_daily_summary_doc_id(batch_date)
            store = get_vector_store()
            
            for i in range(50):
                store.delete_memory(build_daily_event_doc_id(batch_date, i))

            def _fallback_chunk_event() -> Dict[str, Any]:
                text = "；".join(
                    strip_lutopia_internal_memory_blocks(str(c.get("summary_text") or "")).strip()
                    for c in chunks
                )
                return {
                    "summary": self._fallback_event_summary(text),
                    "chunk_ids": sorted(valid_chunk_ids),
                    "score": 5,
                    "arousal": 0.1,
                }

            default_events = [_fallback_chunk_event()]

            if STEP4_SPLIT_MODE:
                groups = await self._step4a_cluster(chunks, self.scoring_llm)
                events: List[Dict[str, Any]] = []
                for group_idx, group in enumerate(groups):
                    group_lines = []
                    for cid in group:
                        c = chunk_by_id.get(cid)
                        if not c:
                            continue
                        group_lines.append(
                            f"- chunk_id={cid} | created_at={c.get('created_at')} | "
                            f"session={c.get('session_id')}\n"
                            f"{strip_lutopia_internal_memory_blocks(str(c.get('summary_text') or ''))}"
                        )
                    group_content = "\n\n".join(group_lines).strip()
                    if not group_content:
                        logger.error("Step 4b 丢弃空分组: idx=%s chunk_ids=%s", group_idx, group)
                        continue

                    desc = await self._step4b_describe_and_score(group_content, self.scoring_llm)
                    if desc is None:
                        logger.error(
                            "Step 4b 分组描述打分失败，丢弃该组: idx=%s chunk_ids=%s",
                            group_idx,
                            group,
                        )
                        continue
                    events.append(
                        {
                            "summary": str(desc.get("content") or "").strip()[:300],
                            "chunk_ids": [int(cid) for cid in group],
                            "score": self._clamp_score(desc.get("score"), 5),
                            "arousal": self._clamp_arousal(desc.get("arousal"), 0.1),
                            "theme": desc.get("theme", "other"),
                            "entities": desc.get("entities", []),
                            "emotion": desc.get("emotion", "neutral"),
                            "event_type": desc.get("event_type", "other"),
                        }
                    )

                if not events:
                    logger.warning("Step 4b 全部分组失败，使用现有默认事件兜底继续")
                    events = default_events
            else:
                _themes_str = " / ".join(EVENT_THEMES)
                _emotions_str = " / ".join(EVENT_EMOTIONS)
                _types_str = " / ".join(EVENT_TYPES)
                split_prompt = self._persona_dialogue_prefix() + f"""【任务】
以下是今天按时间顺序的对话片段摘要。请找出属于同一事件/话题的片段，将它们合并成独立完整的事件描述。每个事件必须标注由哪几条 chunk 合并而来（返回 chunk_ids 列表）。

【输入】
当天 chunk 列表：
{chunk_input}

【关键引导】
- 这是 AI 陪伴项目，日常闲聊、互动片段、心情碎片和"重大事件"同等有价值
- 一个事件 = 一个语义独立的话题段落，不是按时间切片
- 通常 3-{event_split_max} 个事件，平淡的天 1-2 个即可
- 不得超过 {event_split_max} 个
- 至少产出 1 个事件（哪怕是"全天平淡，主要在 X 度过"这样的概括）
- chunk_ids 只能使用输入中出现过的 chunk_id；不要编造 ID

【输出 schema】
[
  {{
    "summary": "事件描述，100-300 字",
    "chunk_ids": [整数chunk_id, ...],
    "score": 整数 1-10，长期保留价值,
    "arousal": 浮点 0.0-1.0，情绪强度（不分正负）,
    "theme": "事件主题枚举值",
    "entities": ["实体1", "实体2"],
    "emotion": "主导情绪枚举值",
    "event_type": "事件类型枚举值"
  }}
]

theme 取值：{_themes_str}
emotion 取值：{_emotions_str}
event_type 取值：{_types_str}
entities: 事件涉及的实体（人名、组织名、产品名等），最多 5 个，必须是有意义的专有名词。无明确实体时返回空数组 []

【评分参考】
score:
- 8-10: 重大事件、强烈情感、关键决定
- 4-7: 有意义的互动、值得回忆的日常
- 1-3: 平淡的日常对话、重复性内容（让时间衰减处理）

arousal:
- 0.7+: 强情绪事件（吵架、惊喜、感动、暴怒）
- 0.3-0.6: 有情绪起伏的对话
- 0.0-0.2: 平静日常"""

                def _normalize_chunk_events(raw_events: Any) -> List[Dict[str, Any]]:
                    events: List[Dict[str, Any]] = []
                    if isinstance(raw_events, dict):
                        raw_events = raw_events.get("events")
                    if not isinstance(raw_events, list):
                        raise ValueError("Step 4 events JSON must be a list")

                    for item in raw_events:
                        if not isinstance(item, dict):
                            logger.error("Step 4 丢弃非法事件：非对象 item=%r", item)
                            continue
                        summary = str(item.get("summary") or item.get("text") or "").strip()
                        if not summary:
                            logger.error("Step 4 丢弃非法事件：summary 为空 item=%r", item)
                            continue
                        raw_ids = item.get("chunk_ids") or item.get("source_chunk_ids") or []
                        if not isinstance(raw_ids, list):
                            raw_ids = [raw_ids]
                        parsed_ids = set()
                        for raw_id in raw_ids:
                            try:
                                parsed_ids.add(int(raw_id))
                            except (TypeError, ValueError):
                                continue
                        legal_ids = sorted(parsed_ids & valid_chunk_ids)
                        illegal_ids = sorted(parsed_ids - valid_chunk_ids)
                        if illegal_ids:
                            logger.error(
                                "Step 4 事件包含非法 chunk_ids，已过滤: illegal=%s legal=%s summary=%s",
                                illegal_ids,
                                legal_ids,
                                summary[:80],
                            )
                        if not legal_ids:
                            logger.error(
                                "Step 4 丢弃事件：chunk_ids 全部非法或为空 summary=%s raw_ids=%r",
                                summary[:120],
                                raw_ids,
                            )
                            continue
                        events.append(
                            {
                                "summary": summary[:300],
                                "chunk_ids": legal_ids,
                                "score": self._clamp_score(item.get("score"), 5),
                                "arousal": self._clamp_arousal(item.get("arousal"), 0.1),
                                "theme": self._clamp_enum(item.get("theme"), EVENT_THEMES, "other"),
                                "entities": self._clamp_entities(item.get("entities"), max_items=5),
                                "emotion": self._clamp_enum(item.get("emotion"), EVENT_EMOTIONS, "neutral"),
                                "event_type": self._clamp_enum(item.get("event_type"), EVENT_TYPES, "other"),
                            }
                        )
                        if len(events) >= event_split_max:
                            break
                    if not events:
                        raise ValueError("Step 4 校验后没有合法事件")
                    return events

                def _parse_split(raw: str) -> List[Dict[str, Any]]:
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        sm = re.search(r"\[[\s\S]*\]", raw)
                        if sm:
                            parsed = json.loads(sm.group())
                        else:
                            om = re.search(r"\{[\s\S]*\}", raw)
                            if om:
                                parsed = json.loads(om.group())
                            else:
                                raise ValueError("JSON parse error")
                    return _normalize_chunk_events(parsed)

                events = await self._step4_retry_with_defaults(
                    "事件拆分与打分",
                    [{"role": "user", "content": split_prompt}],
                    _parse_split,
                    default_events,
                    batch_date,
                    max_retries=3,
                )

            try:
                from .bm25_retriever import add_document_to_bm25, refresh_bm25_index
            except ImportError:
                from memory.bm25_retriever import add_document_to_bm25, refresh_bm25_index

            for idx, ev in enumerate(events):
                frag = str(ev.get("summary") or "").strip()
                if not frag:
                    continue
                event_score = self._clamp_score(ev.get("score"), 5)
                event_arousal = self._clamp_arousal(ev.get("arousal"), 0.1)
                event_halflife = _score_to_halflife_days(event_score)
                source_chunk_ids = [int(x) for x in ev.get("chunk_ids", [])]
                is_starred = any(
                    bool(chunk_by_id[cid].get("is_starred"))
                    for cid in source_chunk_ids
                    if cid in chunk_by_id
                )
                event_theme = ev.get("theme", "other")
                event_entities = ev.get("entities", [])
                event_emotion = ev.get("emotion", "neutral")
                event_event_type = ev.get("event_type", "other")
                eid = build_daily_event_doc_id(batch_date, idx)
                em = {
                    "date": batch_date,
                    "source_date": str(batch_date),
                    "session_id": daily_summary.get('session_id', 'daily_batch'),
                    "summary_type": "daily_event",
                    "score": int(event_score),
                    "summary_id": str(summary_id),
                    "source_chunk_ids": json.dumps(source_chunk_ids, ensure_ascii=False),
                    "is_starred": bool(is_starred),
                    "base_score": float(event_score),
                    "halflife_days": event_halflife,
                    "arousal": float(event_arousal),
                    "parent_id": parent_doc_id,
                    "theme": str(event_theme),
                    "entities": "|".join(str(e) for e in event_entities) if event_entities else "",
                    "emotion": str(event_emotion),
                    "event_type": str(event_event_type),
                }
                if not add_memory(eid, frag, em):
                    logger.error(f"事件片段入库失败 id={eid}")
                    return False, f"ChromaDB 事件片段失败: {eid}"
                try:
                    await get_database().upsert_longterm_memory_by_chroma_id(
                        content=frag,
                        chroma_doc_id=eid,
                        score=event_score,
                        source_chunk_ids=source_chunk_ids,
                        is_starred=is_starred,
                        theme=event_theme,
                        entities=event_entities,
                        emotion=event_emotion,
                        event_type=event_event_type,
                    )
                except Exception as e:
                    logger.error("longterm_memories 镜像写入失败 id=%s: %s", eid, e)
                try:
                    if not add_document_to_bm25(eid, frag, dict(em)):
                        refresh_bm25_index()
                except Exception as e:
                    logger.error(f"BM25 事件片段增量失败: {e}")

            # 回填 external chunk 的 archived_by（其事件已在 add_external_chunk 时写入，无需重复生成）
            if external_chunks:
                try:
                    await archive_external_chunks_by_daily(batch_date, summary_id)
                except Exception as e:
                    logger.warning("回填 external chunk archived_by 失败: %s", e)

            return True, None

        except Exception as e:
            logger.error(f"Step 4 执行失败: {e}")
            return False, str(e)

    async def _step5_chroma_gc(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """Step 5 - Chroma 向量记忆 GC（衰减 + 闲置天数阈值 + 无子节点 + hits 豁免）。"""
        try:
            idle_days = await _gc_stale_days_threshold()
            exempt_hits = await _gc_exempt_hits_threshold()
            n = garbage_collect_stale_memories(
                idle_days_threshold=idle_days,
                strength_threshold=0.05,
                scan_limit=10000,
                exempt_hits_threshold=exempt_hits,
            )
            logger.info(f"Step 5 GC 删除 {n} 条，日期: {batch_date}")
            return True, None
        except Exception as e:
            logger.error(f"Step 5 执行失败: {e}")
            return False, str(e)


async def schedule_daily_batch():
    """
    定时调度日终跑批处理。
    
    每天东八区（Asia/Shanghai）在 `daily_batch_hour` 整点自动触发（默认 23:00，读库热更新）。
    触发时：先将 batch_date 早于「最近7天」窗口且未完成的日志标记为 expired；
    再对窗口内未完成日期按 batch_date 升序串行执行 run_daily_batch(该日)；
    若当日未在上述补跑中执行，最后再 run_daily_batch() 跑今天。
    """
    logger.info("日终跑批定时调度器启动")
    
    processor = DailyBatchProcessor()
    
    while True:
        try:
            # 获取当前时间（东八区）
            now = datetime.now(TIMEZONE)
            
            hour_value = await _daily_batch_trigger_hour()
            hour = int(hour_value)
            minute = 30 if abs(hour_value - hour - 0.5) < 0.01 else 0
            # 计算到下一次触发时间的时间差
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if now >= target_time:
                target_time += timedelta(days=1)
            
            # 计算等待时间（秒）
            wait_seconds = (target_time - now).total_seconds()
            
            logger.info(f"下一次日终跑批将在 {target_time.strftime('%Y-%m-%d %H:%M:%S')} 执行，等待 {wait_seconds:.0f} 秒")
            
            # 等待到目标时间
            await asyncio.sleep(wait_seconds)
            
            wake = datetime.now(TIMEZONE)
            today_d = wake.date()
            today_s = today_d.isoformat()
            window_start_d = today_d - timedelta(days=6)
            window_start_s = window_start_d.isoformat()
            
            await mark_expired_skipped_daily_batch_logs_before(window_start_s)
            
            pending = await list_incomplete_daily_batch_dates_in_range(
                window_start_s, today_s
            )
            ran_today = False
            if pending:
                logger.info(
                    "日终补跑：最近7天内未完成 %s 天，顺序 %s",
                    len(pending),
                    pending,
                )
            for d in pending:
                logger.info("日终跑批补跑 batch_date=%s", d)
                ok = await processor.run_daily_batch(d)
                if d == today_s:
                    ran_today = True
                if not ok:
                    logger.error("日终跑批补跑失败 batch_date=%s", d)
                    await schedule_daily_batch_retry_if_needed(d)
            if not ran_today:
                logger.info("触发日终跑批处理（今日）")
                success = await processor.run_daily_batch()
                if success:
                    logger.info("日终跑批处理（今日）执行成功")
                else:
                    logger.error("日终跑批处理（今日）执行失败")
                    await schedule_daily_batch_retry_if_needed(today_s)
            else:
                logger.info("今日已在补跑队列中执行，跳过重复 run_daily_batch()")
            
            # 等待1分钟，避免重复执行
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"日终跑批调度器错误: {e}")
            # 发生错误时等待5分钟再重试
            await asyncio.sleep(300)


POCKET_MONEY_JOB_TYPE = "daily_pocket_money"


async def _run_single_pocket_money_job(job_date: str) -> bool:
    try:
        await upsert_pocket_money_job_log(
            job_date=job_date,
            job_type=POCKET_MONEY_JOB_TYPE,
            status="pending",
            error_message=None,
        )
        balance = await run_daily_pocket_money_job(
            job_date=job_date,
            job_type=POCKET_MONEY_JOB_TYPE,
        )
        await publish_event(
            EventType.STATUS_UPDATE,
            {"pocketMoney": float(balance)},
        )
        logger.info("零花钱日任务执行成功 job_date=%s balance=%s", job_date, balance)
        return True
    except Exception as e:
        logger.error("零花钱日任务执行失败 job_date=%s: %s", job_date, e)
        await upsert_pocket_money_job_log(
            job_date=job_date,
            job_type=POCKET_MONEY_JOB_TYPE,
            status="failed",
            error_message=str(e)[:1000],
        )
        return False


async def schedule_pocket_money_jobs():
    """
    每天东八区 00:00 执行零花钱日任务，并补跑最近 7 天未完成日期。

    未完成定义：
    - 当天无任何 job_log 记录
    - 存在 job_log 但 status 不是 success（failed / pending）
    """
    logger.info("零花钱定时调度器启动")
    while True:
        try:
            now = datetime.now(TIMEZONE)
            target_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now >= target_time:
                target_time += timedelta(days=1)
            wait_seconds = (target_time - now).total_seconds()
            logger.info(
                "下一次零花钱任务将在 %s 执行，等待 %.0f 秒",
                target_time.strftime("%Y-%m-%d %H:%M:%S"),
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

            wake = datetime.now(TIMEZONE)
            today_d = wake.date()
            today_s = today_d.isoformat()
            window_start_s = (today_d - timedelta(days=6)).isoformat()
            pending_dates = await list_incomplete_pocket_money_job_dates_in_range(
                window_start_s,
                today_s,
                POCKET_MONEY_JOB_TYPE,
            )
            if pending_dates:
                logger.info(
                    "零花钱补跑：最近7天内未完成 %s 天，顺序 %s",
                    len(pending_dates),
                    pending_dates,
                )
            for d in pending_dates:
                await _run_single_pocket_money_job(d)
            await asyncio.sleep(60)
        except Exception as e:
            logger.error("零花钱调度器错误: %s", e)
            await asyncio.sleep(300)


async def schedule_expire_stale_approvals():
    """Expire stale pending approvals once per hour."""
    logger.info("pending approval expiration scheduler started")
    while True:
        try:
            expired = await expire_stale_approvals()
            if expired:
                logger.info("expired %s stale pending approvals", expired)
        except Exception as e:
            logger.error("pending approval expiration scheduler error: %s", e)
        await asyncio.sleep(3600)


def trigger_daily_batch_manual(batch_date: Optional[str] = None) -> bool:
    """
    手动触发日终跑批处理。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'，如果为 None 则使用今天
        
    Returns:
        bool: 批处理是否成功完成
    """
    try:
        resolved = batch_date or datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        # 创建事件循环并运行
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        processor = DailyBatchProcessor()
        success = loop.run_until_complete(processor.run_daily_batch(batch_date))

        if not success:
            loop.run_until_complete(schedule_daily_batch_retry_if_needed(resolved))

        loop.close()

        return success

    except Exception as e:
        logger.error(f"手动触发日终跑批失败: {e}")
        return False


def test_daily_batch() -> None:
    """
    测试日终跑批功能。
    """
    print("测试日终跑批功能...")
    
    try:
        # 测试配置
        print(f"时区: {TIMEZONE}")
        print(f"LLM 模型: {config.LLM_MODEL_NAME}")
        print(f"摘要模型: {config.SUMMARY_MODEL_NAME}")
        
        # 测试处理器初始化
        processor = DailyBatchProcessor()
        print("日终跑批处理器初始化成功")
        
        # 测试手动触发（简化版）
        print("测试手动触发日终跑批...")
        success = trigger_daily_batch_manual()
        
        if success:
            print("日终跑批测试通过")
        else:
            print("日终跑批测试失败（可能是配置问题或没有数据）")
        
    except Exception as e:
        print(f"日终跑批测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    """日终跑批模块测试入口。"""
    test_daily_batch()
