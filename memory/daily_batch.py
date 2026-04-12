"""
日终跑批处理模块。

每天东八区（Asia/Shanghai）在 `config.daily_batch_hour` 整点（默认 23）自动触发，执行五步流水线：
Step 1 - 到期 temporal_states 结算并改写为客观过去时，供 Step 2 使用
Step 2 - 生成今日小传（prompt 含 Step 1 输出）
Step 3 - 记忆卡片 Upsert + 可选写入 relationship_timeline
Step 4 - 今日小传全量向量化（按分映射 halflife_days），可选拆事件片段入库 + BM25 增量
Step 5 - Chroma 长期未访问且衰减得分过低、无子节点的记忆 GC

断点续跑：每次触发前先查 daily_batch_log 表（step1–step5），已完成的步骤跳过。
"""

import asyncio
import json
import logging
import sys
import os
import re
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple
import pytz

# 添加项目根目录到 Python 路径
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import config, Platform
from llm.llm_interface import (
    LLMInterface,
    CedarClioOutputGuardExhausted,
    batch_one_shot_with_async_output_guard,
    coerce_score_and_arousal_defaults,
)
from memory.micro_batch import SummaryLLMInterface, fetch_active_persona_display_names

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
        delete_today_chunk_summaries,
        get_unsummarized_messages_by_session,
        save_summary,
        mark_messages_as_summarized_by_ids,
        get_all_active_memory_cards,
        save_memory_card,
        update_memory_card,
        get_memory_cards,
        get_latest_memory_card_for_dimension,
        get_recent_daily_summaries,
        save_daily_batch_log,
        get_daily_batch_log,
        update_daily_batch_step_status,
        get_unsummarized_count_by_session,
        list_expired_active_temporal_states,
        deactivate_temporal_states_by_ids,
        insert_relationship_timeline_event,
        list_incomplete_daily_batch_dates_in_range,
        mark_expired_skipped_daily_batch_logs_before,
        purge_logs_older_than_days,
    )
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    from memory.database import (
        get_database,
        get_today_chunk_summaries,
        delete_today_chunk_summaries,
        get_unsummarized_messages_by_session,
        save_summary,
        mark_messages_as_summarized_by_ids,
        get_all_active_memory_cards,
        save_memory_card,
        update_memory_card,
        get_memory_cards,
        get_latest_memory_card_for_dimension,
        get_recent_daily_summaries,
        save_daily_batch_log,
        get_daily_batch_log,
        update_daily_batch_step_status,
        get_unsummarized_count_by_session,
        list_expired_active_temporal_states,
        deactivate_temporal_states_by_ids,
        insert_relationship_timeline_event,
        list_incomplete_daily_batch_dates_in_range,
        mark_expired_skipped_daily_batch_logs_before,
        purge_logs_older_than_days,
    )

# 设置日志
logger = logging.getLogger(__name__)

# 时区配置
TIMEZONE = pytz.timezone("Asia/Shanghai")


async def _daily_batch_trigger_hour() -> int:
    """日终跑批触发小时（0–23）：优先 config 表 daily_batch_hour，否则默认 23。"""
    try:
        raw = await get_database().get_config("daily_batch_hour")
        if raw is not None and str(raw).strip() != "":
            h = int(str(raw).strip())
            if 0 <= h <= 23:
                return h
    except (ValueError, TypeError):
        pass
    except Exception as e:
        logger.debug("读取 daily_batch_hour 失败，使用默认 23: %s", e)
    return 23


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


def _score_to_halflife_days(score: int) -> int:
    """日终打分映射半衰期：8–10→600 天，4–7→200 天，1–3→30 天。"""
    if score >= 8:
        return 600
    if score >= 4:
        return 200
    return 30


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
        self.summary_llm = SummaryLLMInterface()
        self._settled_temporal_snippets: List[str] = []
        self._batch_char_name: str = "AI"
        self._batch_user_name: str = "用户"
        
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
            self._batch_char_name, self._batch_user_name = (
                await fetch_active_persona_display_names()
            )

            if batch_date is None:
                batch_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            
            logger.info(f"开始日终跑批处理，日期: {batch_date}")

            try:
                n_del = await purge_logs_older_than_days(7)
                if n_del > 0:
                    logger.info("已清理早于 7 天的系统日志（logs）%s 条", n_del)
            except Exception as e:
                logger.warning("清理过期系统日志失败（不影响跑批）: %s", e)

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
            prompt = f"""下列每条是用户曾经的「进行中」状态描述，已于本批处理日 {batch_date} 到期结算。
请将每条改写为一条简洁的汉语客观过去时事实陈述（可含时间，不要编造未给出的信息）。
严格只输出一个 JSON 数组，长度与输入一致，元素为字符串，顺序与输入相同。

输入 JSON 数组：
{json.dumps(contents, ensure_ascii=False)}"""
            
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
            chunk_summaries = await get_today_chunk_summaries()
            
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
                    summary_text = summary['summary_text']
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
            
            prompt = self._persona_dialogue_prefix() + f"""请基于以下材料生成今日小传，按时间顺序概括今天的主要话题、重要事件和情感状态。
要求：
- 篇幅 150–400 字，内容丰富时可到上限，平淡日常 150 字足够。
- 必须保留重要的互动细节、具体结论（数字、决定、名字、时间节点等），不得泛泛而谈。
- 语气自然流畅，不要分点列举，不要加标题。
- 如有时效状态结算内容，自然融入正文，不要单独列块。
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
            
            await save_summary(
                session_id="daily_batch",
                summary_text=daily_summary,
                start_message_id=0,
                end_message_id=0,
                summary_type="daily"
            )

            n_chunk_del = await delete_today_chunk_summaries()
            if n_chunk_del:
                logger.info(
                    "已删除今日 chunk 摘要 %s 条（daily 写入成功后），日期: %s",
                    n_chunk_del,
                    batch_date,
                )
            
            logger.info(f"今日小传保存成功，日期: {batch_date}")
            return True, None
            
        except Exception as e:
            logger.error(f"Step 2 执行失败: {e}")
            return False, str(e)

    def _call_summary_llm_custom(self, prompt: str) -> str:
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
        )

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

    async def _merge_memory_card_contents(
        self,
        dimension: str,
        dimension_label: str,
        old_content: str,
        new_content: str,
        batch_date: str,
    ) -> str:
        """
        将既有卡片与今日提取的新文案交给模型，重写为一段连贯、去重后的中文。
        调用失败或解析失败时回退为简单拼接（并打日志）。
        """
        old_trim = old_content.strip()
        if len(old_trim) > 6000:
            old_trim = "…（前文已截断）\n" + old_trim[-6000:]

        if dimension == "interaction_patterns":
            merge_rules = (
                "该维度记录有对话支撑的相处观察。若新旧描述指向同一行为且意思重复，只保留一份表述；"
                "合并时主动删除过程性描述，只保留结论性表述。"
                "若对同一话题的观察明显矛盾，可在一小段内并列说明并带时间或场景提示，不要用列表堆砌。"
            )
        elif dimension in ("current_status", "preferences"):
            merge_rules = (
                "该维度记录用户的当前状态与偏好。若新旧信息存在矛盾，意味着状态已发生改变，请直接以今日新增信息为准进行覆写，"
                "无需保留冲突的旧信息（旧状态已另行归档）。相同事实只保留一份，整合为自然连贯的叙述。"
            )
        else:
            merge_rules = (
                "以今日新增为准修正或补充过时信息；相同事实只保留一份，整合为自然连贯的叙述，不要简单首尾拼接。"
            )

        if dimension in ("current_status", "preferences"):
            contradiction_bullet = ""
        else:
            contradiction_bullet = (
                "- 如新旧信息存在矛盾，保留两者并严格使用 [YYYY-MM-DD] 格式在新增内容前标注日期，不要静默覆盖；\n"
            )

        prompt = self._persona_dialogue_prefix() + f"""你是记忆整理助手。请将「既有记忆卡片」与「今日新增摘要」进行逻辑合并。
合并规则：
- 剔除重复内容，将新信息无缝整合进原有段落。
{contradiction_bullet}- 禁止在正文中使用"今天"、"最近"等相对时间词。
- 形成连贯的中文，可用逗号、分号连接，不要用 Markdown 列表或编号条。
- 不要遗漏任何旧的有效设定，只做提炼，不做强行删减。
- 若旧卡片中有明显过时且已被今日信息明确取代的内容（如旧工作/旧居住地），可标注「（已更新）」后保留简短记录。
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

        fallback = f"{old_content.strip()}\n[{batch_date}更新] {new_content.strip()}"
        
        def _gen():
            return self._call_summary_llm_custom(prompt)
            
        def _parse(raw):
            m = self._parse_merged_content_json(raw)
            if m:
                return m
            raise ValueError(f"记忆卡片合并 JSON 解析失败; 原始片段: {raw[:200] if raw else '(空)'}")

        try:
            return await self._retry_call_and_parse("合并记忆卡片", _gen, _parse)
        except CedarClioOutputGuardExhausted as e:
            logger.warning(f"合并记忆卡片 Guard 用尽，使用拼接回退: {e}")
            return fallback
        except Exception as e:
            logger.warning(f"使用拼接回退: {e}")
            return fallback
    
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
            # 1. 获取今日 daily 摘要（取最新一条）
            daily_summaries = await get_recent_daily_summaries(limit=1)
            if not daily_summaries:
                logger.info(f"今日没有 daily 摘要，跳过 Step 3，日期: {batch_date}")
                return True, None

            daily_summary = daily_summaries[0]
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

            # 如果没有查到用户，使用默认值（兜底）
            if not user_character_pairs:
                logger.info("今日无用户对话记录，使用默认 user_id/character_id 进行记忆卡片更新")
                user_character_pairs = [("default_user", "sirius")]

            # 3. 构建 LLM Prompt，要求按 7 个维度分析今日小传
            dimensions_desc = {
                "preferences": "偏好与喜恶（食物、音乐、活动、风格等具体偏好）",
                "interaction_patterns": "相处模式（只记录有具体对话支撑的行为观察，注明日期，不做性格定论；与已有认知矛盾时并存保留）；只提取可归纳的行为模式或结论性规律，不记录单次对话的过程细节。",
                "current_status": "近况与生活动态（当前工作、学习、健康、居住等状态）",
                "goals": "目标与计划（短期或长期的目标、计划、心愿）",
                "relationships": "重要关系（家人、朋友、同事等重要人物及关系）",
                "key_events": "重要事件（值得长期记录的重大事件、里程碑）",
                "rules": "相处规则与禁区（用户明确表达的偏好规则、禁忌话题）"
            }
            dimensions_list = "\n".join([f"- {k}：{v}" for k, v in dimensions_desc.items()])

            prompt = f"""请仔细阅读以下今日小传，分析其中是否包含关于用户的新信息，并按照7个维度进行分类提取。
今日小传（{batch_date}）：
{summary_text}
请按以下7个维度分析，提取今日小传中出现的新信息：
{dimensions_list}
要求：
1. 只提取今日小传中明确出现的新信息，不要推断或捏造。
2. interaction_patterns 可写至 150 字；其他维度 80 字以内。
3. 没有新信息的维度，返回 null。
4. 直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁使用 markdown 代码块包裹，严禁输出任何前言后语。
格式示例：
{{"preferences":null,"interaction_patterns":"...","current_status":null,"goals":null,"relationships":null,"key_events":null,"rules":null}}"""

            # 4. 调用 SUMMARY LLM 分析维度
            logger.info(f"调用 LLM 分析今日小传维度，日期: {batch_date}")

            def _gen_dim():
                return self.summary_llm.generate_summary(
                    [{"role": "user", "content": prompt}],
                    char_name=self._batch_char_name,
                    user_name=self._batch_user_name,
                )

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

            logger.info(f"LLM 维度分析完成，有内容的维度: {[k for k, v in dimension_data.items() if v and v != 'null']}")

            # 6. 对每个用户执行 Upsert
            for user_id, character_id in user_character_pairs:
                logger.info(f"更新记忆卡片: user_id={user_id}, character_id={character_id}")

                for dimension in self.dimensions:
                    # 单个维度失败不影响其他维度
                    try:
                        new_content = dimension_data.get(dimension)

                        # 跳过 null 或空值
                        if not new_content or new_content == "null":
                            logger.debug(f"维度 {dimension} 无新信息，跳过")
                            continue

                        # 该维度最近一条（含 is_active=0），便于「全表软删后重跑」仍更新同一行
                        existing_card = await get_latest_memory_card_for_dimension(
                            user_id, character_id, dimension
                        )

                        if existing_card:
                            # 已有记录 → 模型合并去重后 UPDATE，并重新激活
                            card_id = existing_card["id"]
                            old_content = existing_card["content"]
                            dim_label = dimensions_desc.get(dimension, dimension)
                            if dimension in ("current_status", "preferences") and (old_content or "").strip():
                                doc_id = f"state_{user_id}_{character_id}_{dimension}_{batch_date}"
                                try:
                                    archive_meta = {
                                        "date": str(batch_date),
                                        "session_id": f"{user_id}_{character_id}",
                                        "summary_type": "state_archive",
                                        "source": "state_archive",
                                        "dimension": dimension,
                                        "base_score": 5,
                                        "halflife_days": 90,
                                    }
                                    if not add_memory(doc_id, old_content.strip(), archive_meta):
                                        logger.warning(
                                            "状态卡片旧内容归档向量库失败 doc_id=%s user=%s dim=%s",
                                            doc_id,
                                            user_id,
                                            dimension,
                                        )
                                except Exception as ex:
                                    logger.warning(
                                        "状态卡片旧内容归档向量库异常 doc_id=%s: %s",
                                        doc_id,
                                        ex,
                                    )
                            merged_content = await self._merge_memory_card_contents(
                                dimension,
                                dim_label,
                                old_content,
                                str(new_content),
                                batch_date,
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
请判断今天是否有值得写入「关系时间轴」的事件（可含上述时效结算中的关系变化）。
event_type 说明：
- milestone：关系性质的转折或里程碑
- emotional_shift：情绪基调的明显变化（争吵、和好、感情升温等）
- conflict：明确的冲突或摩擦
- daily_warmth：仅限今天有特别记录价值的温馨互动，普通平静日常不要强行凑数写此类
content 要求：
1. 视角限定：必须使用第三人称客观视角记录，严禁使用"我"或"你"，必须使用真实姓名 {self._batch_char_name} 和 {self._batch_user_name}。禁止使用"今天""昨天"等相对时间词，用具体事件背景替代。
2. 描述风格：20–60字，用第三人称客观陈述发生的事实。例如："{self._batch_user_name} 因 Java 底层原理学习感到烦躁时，{self._batch_char_name} 一直陪伴并用通俗例子讲解"。
3. 勿带过度夸张的虚假主观抒情，保持记忆作为时间轴记录的清晰度和真实感。
若有，返回严格 JSON：{{"events":[{{"event_type":"...","content":"..."}}]}}；若无则 {{"events":[]}}。
直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁使用 markdown 的 json 代码块包裹，严禁输出任何前言后语。"""

            def _gen_tl():
                return self.summary_llm.generate_summary(
                    [{"role": "user", "content": tl_prompt}],
                    char_name=self._batch_char_name,
                    user_name=self._batch_user_name,
                )

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
    
    async def _step4_archive_daily_and_events(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 4 - 今日小传全量入库 Chroma，按分映射 halflife_days，可选事件拆分 + BM25。
        """
        try:
            daily_summaries = await get_recent_daily_summaries(limit=1)
            if not daily_summaries:
                logger.info(f"今日没有小传，跳过 Step 4，日期: {batch_date}")
                return True, None
            
            daily_summary = daily_summaries[0]
            summary_text = daily_summary['summary_text']
            summary_id = daily_summary['id']
            
            prompt = self._persona_dialogue_prefix() + f"""请评估以下今日小传的长期保留价值，给出评分和情绪强度。
今日小传内容：
{summary_text}
评分标准（score，整数 1-10）：
1-3：日常琐事，无长期参考价值
4-6：有一定参考价值，但信息较普通
7-8：有价值的信息，值得长期保留
9-10：重要里程碑或关键信息，对长期记忆有显著价值
情绪强度（arousal，浮点数 0.0–1.0）：
0.0–0.2：平静普通，几乎无情绪波动
0.3–0.6：有情绪起伏，但整体平稳
0.7–1.0：情绪激烈（争吵、重大喜讯、悲伤、重要决定等）
输出要求：直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁使用 markdown 代码块包裹，严禁输出任何其他文字，格式：{{"score":<整数>,"arousal":<浮点数>}}。"""
            
            score = 5
            arousal = 0.1
            try:
                llm_resp = self.llm.generate_with_context_and_tracking(
                    [{"role": "user", "content": prompt}],
                    platform=Platform.BATCH,
                )
                raw_score = (llm_resp.content or "").strip()
                score, arousal = coerce_score_and_arousal_defaults(raw_score)
            except Exception as e:
                logger.warning(f"LLM 价值打分调用失败，使用默认 score/arousal: {e}")
                score = 5
                arousal = 0.1
            logger.info(f"今日小传价值分: {score}/10, arousal: {arousal:.2f}")
            
            halflife = _score_to_halflife_days(score)
            parent_doc_id = build_daily_summary_doc_id(batch_date)
            store = get_vector_store()
            
            for i in range(50):
                store.delete_memory(build_daily_event_doc_id(batch_date, i))
            store.delete_memory(parent_doc_id)
            
            base_meta = {
                "date": batch_date,
                "session_id": daily_summary.get('session_id', 'daily_batch'),
                "summary_type": "daily",
                "score": str(score),
                "summary_id": str(summary_id),
                "base_score": float(score),
                "halflife_days": halflife,
                "arousal": float(arousal),
            }
            
            if not add_memory(parent_doc_id, summary_text, base_meta):
                return False, "ChromaDB 主文档归档失败"
            
            try:
                try:
                    from .bm25_retriever import add_document_to_bm25, refresh_bm25_index
                except ImportError:
                    from memory.bm25_retriever import add_document_to_bm25, refresh_bm25_index
                
                final_meta = dict(base_meta)
                if not add_document_to_bm25(parent_doc_id, summary_text, final_meta):
                    refresh_bm25_index()
            except Exception as e:
                logger.error(f"BM25 主文档增量失败: {e}")
            
            split_prompt = f"""阅读以下「今日小传」，判断是否应拆成多条独立、可分别检索的具体事件。
拆分原则：
- 只拆时间上明显分离或主题截然不同的独立事件。
- 每条事件应自成一体、可独立理解，无需依赖其他条目。必须补全代词主语，将"他/她/我"全部替换为具体的真实姓名（如 {self._batch_char_name} 或 {self._batch_user_name}）；禁止使用"今天/昨天"等相对时间词，如有需要请描述为具体事件背景，确保独立检索时不产生歧义。
- 最多拆 4 条，不要过度细碎；若整篇围绕同一主题，无需拆分。
- 不要编造原文没有的内容，每条 50–150 字。
若需要拆分，返回严格 JSON：{{"events":["事件1","事件2",...]}}；若不需要则 {{"events":[]}}。
输出要求：直接输出以大括号 {{}} 开头的纯 JSON 字符串，严禁使用 markdown 代码块包裹，严禁输出任何其他文字。
今日小传：
{summary_text}"""
            
            event_texts: List[str] = []
            
            def _gen_split():
                return self.summary_llm.generate_summary(
                    [{"role": "user", "content": split_prompt}],
                    char_name=self._batch_char_name,
                    user_name=self._batch_user_name,
                )

            def _parse_split(raw):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    sm = re.search(r"\{[\s\S]*\}", raw)
                    if sm:
                        return json.loads(sm.group())
                    raise ValueError("JSON parse error")

            try:
                split_data = await self._retry_call_and_parse("事件拆分解析", _gen_split, _parse_split)
            except Exception as e:
                logger.warning(f"事件拆分最终解析失败，跳过子文档: {e}")
                split_data = {"events": []}
            
            if isinstance(split_data, dict):
                evs = split_data.get("events")
                if isinstance(evs, list):
                    event_texts = [str(x).strip() for x in evs if str(x).strip()]
            
            for idx, frag in enumerate(event_texts):
                eid = build_daily_event_doc_id(batch_date, idx)
                em = {
                    "date": batch_date,
                    "session_id": daily_summary.get('session_id', 'daily_batch'),
                    "summary_type": "daily_event",
                    "score": str(score),
                    "summary_id": str(summary_id),
                    "base_score": float(score),
                    "halflife_days": halflife,
                    "arousal": float(arousal),
                    "parent_id": parent_doc_id,
                }
                if not add_memory(eid, frag, em):
                    logger.error(f"事件片段入库失败 id={eid}")
                    return False, f"ChromaDB 事件片段失败: {eid}"
                try:
                    from .bm25_retriever import add_document_to_bm25, refresh_bm25_index
                except ImportError:
                    from memory.bm25_retriever import add_document_to_bm25, refresh_bm25_index
                try:
                    if not add_document_to_bm25(eid, frag, dict(em)):
                        refresh_bm25_index()
                except Exception as e:
                    logger.error(f"BM25 事件片段增量失败: {e}")
            
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
            
            hour = await _daily_batch_trigger_hour()
            # 计算到下一次触发整点的时间差
            target_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)

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
            if not ran_today:
                logger.info("触发日终跑批处理（今日）")
                success = await processor.run_daily_batch()
                if success:
                    logger.info("日终跑批处理（今日）执行成功")
                else:
                    logger.error("日终跑批处理（今日）执行失败")
            else:
                logger.info("今日已在补跑队列中执行，跳过重复 run_daily_batch()")
            
            # 等待1分钟，避免重复执行
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"日终跑批调度器错误: {e}")
            # 发生错误时等待5分钟再重试
            await asyncio.sleep(300)


def trigger_daily_batch_manual(batch_date: Optional[str] = None) -> bool:
    """
    手动触发日终跑批处理。
    
    Args:
        batch_date: 批处理日期，格式为 'YYYY-MM-DD'，如果为 None 则使用今天
        
    Returns:
        bool: 批处理是否成功完成
    """
    try:
        # 创建事件循环并运行
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        processor = DailyBatchProcessor()
        success = loop.run_until_complete(processor.run_daily_batch(batch_date))
        
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
