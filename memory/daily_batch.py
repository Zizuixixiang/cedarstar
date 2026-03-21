"""
日终跑批处理模块。

每天东八区（Asia/Shanghai）晚上23:00自动触发，执行五步流水线：
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

from config import config
from llm.llm_interface import LLMInterface
from memory.micro_batch import SummaryLLMInterface

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
        get_today_chunk_summaries,
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
    )
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    from memory.database import (
        get_today_chunk_summaries,
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
    )

# 设置日志
logger = logging.getLogger(__name__)

# 时区配置
TIMEZONE = pytz.timezone("Asia/Shanghai")


def _score_to_halflife_days(score: int) -> int:
    """日终打分映射半衰期：8–10→60 天，4–7→30 天，1–3→7 天。"""
    if score >= 8:
        return 60
    if score >= 4:
        return 30
    return 7


class DailyBatchProcessor:
    """
    日终跑批处理器类。
    
    负责执行每日的五步流水线处理。
    """
    
    def __init__(self):
        """
        初始化日终跑批处理器。
        """
        # 创建 LLM 接口
        self.llm = LLMInterface()
        self.summary_llm = SummaryLLMInterface()
        self._settled_temporal_snippets: List[str] = []
        
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
            if batch_date is None:
                batch_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            
            logger.info(f"开始日终跑批处理，日期: {batch_date}")
            
            batch_log = get_daily_batch_log(batch_date)
            
            if batch_log is None:
                save_daily_batch_log(
                    batch_date,
                    step1_status=0,
                    step2_status=0,
                    step3_status=0,
                    step4_status=0,
                    step5_status=0,
                )
                batch_log = get_daily_batch_log(batch_date)
            
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
                    update_daily_batch_step_status(batch_date, 1, 1)
                    batch_log["step1_status"] = 1
                    logger.info(f"Step 1 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 1, 0, error_message)
                    logger.error(f"Step 1 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 1 已跳过（已完成），日期: {batch_date}")
            
            # Step 2 — 今日小传
            if _s(2) == 0:
                logger.info(f"执行 Step 2 - 生成今日小传，日期: {batch_date}")
                success, error_message = await self._step2_generate_daily_summary(batch_date)
                if success:
                    update_daily_batch_step_status(batch_date, 2, 1)
                    batch_log["step2_status"] = 1
                    logger.info(f"Step 2 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 2, 0, error_message)
                    logger.error(f"Step 2 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 2 已跳过（已完成），日期: {batch_date}")
            
            # Step 3 — 记忆卡片 + relationship_timeline
            if _s(3) == 0:
                logger.info(f"执行 Step 3 - 记忆卡片与关系时间轴，日期: {batch_date}")
                success, error_message = await self._step3_memory_cards_and_timeline(batch_date)
                if success:
                    update_daily_batch_step_status(batch_date, 3, 1)
                    batch_log["step3_status"] = 1
                    logger.info(f"Step 3 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 3, 0, error_message)
                    logger.error(f"Step 3 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 3 已跳过（已完成），日期: {batch_date}")
            
            # Step 4 — 全量向量归档 + 事件拆分
            if _s(4) == 0:
                logger.info(f"执行 Step 4 - 向量归档与事件拆分，日期: {batch_date}")
                success, error_message = await self._step4_archive_daily_and_events(batch_date)
                if success:
                    update_daily_batch_step_status(batch_date, 4, 1)
                    batch_log["step4_status"] = 1
                    logger.info(f"Step 4 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 4, 0, error_message)
                    logger.error(f"Step 4 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 4 已跳过（已完成），日期: {batch_date}")
            
            # Step 5 — Chroma GC
            if _s(5) == 0:
                logger.info(f"执行 Step 5 - Chroma 记忆 GC，日期: {batch_date}")
                success, error_message = await self._step5_chroma_gc(batch_date)
                if success:
                    update_daily_batch_step_status(batch_date, 5, 1)
                    logger.info(f"Step 5 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 5, 0, error_message)
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
                    update_daily_batch_step_status(batch_date, 1, 0, str(e))
                except Exception:
                    pass
            return False
    
    async def _step1_expire_temporal_states(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """Step 1：到期 temporal_states 置 inactive，摘要模型改写为过去时事实列表。"""
        self._settled_temporal_snippets = []
        try:
            now_iso = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            rows = list_expired_active_temporal_states(now_iso)
            if not rows:
                logger.info(f"无到期 temporal_states，Step 1 空跑，日期: {batch_date}")
                return True, None
            
            ids = [str(r["id"]) for r in rows if r.get("id")]
            deactivate_temporal_states_by_ids(ids)
            logger.info(f"已停用 {len(ids)} 条 temporal_states，日期: {batch_date}")
            
            contents = [str(r.get("state_content") or "").strip() or "（空）" for r in rows]
            prompt = f"""下列每条是用户曾经的「进行中」状态描述，已于本批处理日 {batch_date} 到期结算。
请将每条改写为一条简洁的汉语客观过去时事实陈述（可含时间，不要编造未给出的信息）。
严格只输出一个 JSON 数组，长度与输入一致，元素为字符串，顺序与输入相同。

输入 JSON 数组：
{json.dumps(contents, ensure_ascii=False)}"""
            
            try:
                raw = self.summary_llm.generate_summary(
                    [{"role": "user", "content": prompt}]
                )
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    self._settled_temporal_snippets = [str(x) for x in parsed]
                else:
                    raise ValueError("not a list")
            except Exception as e:
                logger.warning(f"时效状态改写 JSON 解析失败，使用原文兜底: {e}")
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
            chunk_summaries = get_today_chunk_summaries()
            
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
            
            prompt = f"""请基于以下材料，生成一份简洁的今日小传，总结今天的主要话题和重要信息：

{today_content}

今日小传（中文，简洁明了）:"""
            
            try:
                daily_summary = self.summary_llm.generate_summary(
                    [{"role": "user", "content": prompt}]
                )
            except Exception as e:
                logger.error(f"生成今日小传失败: {e}")
                daily_summary = f"今日总结：包含 {len(chunk_summaries)} 个对话片段。"
            
            save_summary(
                session_id="daily_batch",
                summary_text=daily_summary,
                start_message_id=0,
                end_message_id=0,
                summary_type="daily"
            )
            
            logger.info(f"今日小传保存成功，日期: {batch_date}")
            return True, None
            
        except Exception as e:
            logger.error(f"Step 2 执行失败: {e}")
            return False, str(e)

    def _call_summary_llm_custom(self, prompt: str) -> str:
        """使用摘要模型配置执行自定义 prompt（不经 micro_batch 的对话摘要模板包装）。"""
        from llm.llm_interface import LLMInterface

        sl = self.summary_llm
        llm = LLMInterface(model_name=sl.model_name)
        llm.api_key = sl.api_key
        llm.api_base = sl.api_base
        llm.timeout = sl.timeout
        base = int(getattr(sl, "max_tokens", 500) or 500)
        llm.max_tokens = min(2048, max(base, 900))
        return llm.generate_simple(prompt).strip()

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

    def _merge_memory_card_contents(
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
                "若对同一话题的观察明显矛盾，可在一小段内并列说明并带时间或场景提示，不要用列表堆砌。"
            )
        else:
            merge_rules = (
                "以今日新增为准修正或补充过时信息；相同事实只保留一份，整合为自然连贯的叙述，不要简单首尾拼接。"
            )

        prompt = f"""你是记忆整理助手。请将「既有记忆卡片」与「今日新增摘要」合并为一段连贯的中文（可用逗号、分号连接，不要用 Markdown 列表或编号条）。

维度代码：{dimension}
维度说明：{dimension_label}
今日日期：{batch_date}

【既有记忆卡片】
{old_trim}

【今日新增】
{new_content.strip()}

合并要求：
1. 去除重复、同义反复的内容，禁止把同样意思写两遍。
2. {merge_rules}
3. 总长度控制在 400 字以内。
4. 严格只返回 JSON，格式为：{{"content":"合并后的正文"}}，不要 markdown 代码块或其他说明文字。"""

        fallback = f"{old_content.strip()}\n[{batch_date}更新] {new_content.strip()}"
        try:
            raw = self._call_summary_llm_custom(prompt)
        except Exception as e:
            logger.warning(f"记忆卡片合并 LLM 调用失败，使用拼接回退: {e}")
            return fallback

        merged = self._parse_merged_content_json(raw)
        if merged:
            return merged

        logger.warning(
            "记忆卡片合并 JSON 解析失败，使用拼接回退；原始片段: %s",
            raw[:200] if raw else "(空)",
        )
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
            daily_summaries = get_recent_daily_summaries(limit=1)
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
                with __import__('sqlite3').connect(db.db_path) as conn:
                    conn.row_factory = __import__('sqlite3').Row
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT DISTINCT user_id, character_id
                        FROM messages
                        WHERE DATE(created_at) = ?
                          AND user_id IS NOT NULL
                          AND user_id != ''
                          AND role = 'user'
                    """, (batch_date,))
                    user_rows = cursor.fetchall()
                    user_character_pairs = [(row['user_id'], row['character_id']) for row in user_rows
                                            if row['user_id'] and row['character_id']]
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
                "interaction_patterns": "相处模式（只记录有具体对话支撑的行为观察，不做性格定论；新旧矛盾时并存保留并注明日期）",
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
1. 只提取今日小传中明确出现的新信息，不要推断或捏造
2. 有新信息的维度，用简洁的中文描述（100字以内）
3. 没有新信息的维度，返回 null
4. 必须严格返回 JSON 格式，不要有任何其他文字

返回格式（严格 JSON）：
{{
  "preferences": "内容或null",
  "interaction_patterns": "内容或null",
  "current_status": "内容或null",
  "goals": "内容或null",
  "relationships": "内容或null",
  "key_events": "内容或null",
  "rules": "内容或null"
}}"""

            # 4. 调用 SUMMARY LLM 分析维度
            logger.info(f"调用 LLM 分析今日小传维度，日期: {batch_date}")
            try:
                llm_response = self.summary_llm.generate_summary([
                    {"role": "user", "content": prompt}
                ])
            except Exception as e:
                logger.error(f"LLM 调用失败，Step 3 中止: {e}")
                return False, f"LLM 调用失败: {e}"

            # 5. 解析 LLM 返回的 JSON（容忍前置说明、markdown 代码块）
            dimension_data: Dict[str, Any] = {}
            raw_resp = (llm_response or "").strip()
            parse_err: Optional[Exception] = None
            try:
                dimension_data = json.loads(raw_resp)
            except json.JSONDecodeError as e:
                parse_err = e
                slice_json = self._extract_first_json_object(raw_resp)
                if slice_json:
                    try:
                        dimension_data = json.loads(slice_json)
                        parse_err = None
                    except json.JSONDecodeError as e2:
                        parse_err = e2
                if parse_err is not None:
                    json_match = re.search(r"\{[\s\S]*\}", raw_resp)
                    if json_match:
                        try:
                            dimension_data = json.loads(json_match.group())
                            parse_err = None
                        except json.JSONDecodeError as e3:
                            parse_err = e3
                if parse_err is not None:
                    logger.error(
                        "无法解析 LLM 维度分析 JSON: %s，原始响应前 500 字: %s",
                        parse_err,
                        raw_resp[:500],
                    )
                    return False, f"JSON 解析失败: {parse_err}"

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
                        existing_card = get_latest_memory_card_for_dimension(
                            user_id, character_id, dimension
                        )

                        if existing_card:
                            # 已有记录 → 模型合并去重后 UPDATE，并重新激活
                            card_id = existing_card["id"]
                            old_content = existing_card["content"]
                            dim_label = dimensions_desc.get(dimension, dimension)
                            merged_content = self._merge_memory_card_contents(
                                dimension,
                                dim_label,
                                old_content,
                                str(new_content),
                                batch_date,
                            )

                            update_memory_card(
                                card_id,
                                merged_content,
                                dimension=None,
                                reactivate=True,
                            )
                            logger.info(f"更新记忆卡片: dimension={dimension}, card_id={card_id}")

                        else:
                            # 无记录 → INSERT
                            card_id = save_memory_card(
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
            tl_prompt = f"""今日小传（{batch_date}）：
{summary_text}

本日已结算的时效状态（客观陈述）：
{settled_block}

请判断今天是否有值得写入「关系时间轴」的事件（可含上述时效结算中的关系变化）。
若有，返回严格 JSON，格式：{{"events":[{{"event_type":"milestone|emotional_shift|conflict|daily_warmth","content":"..."}}]}}；若无则 {{"events":[]}}。
event_type 必须四选一：milestone、emotional_shift、conflict、daily_warmth。不要其他文字。"""

            tl_raw = ""
            try:
                tl_raw = self.summary_llm.generate_summary(
                    [{"role": "user", "content": tl_prompt}]
                )
                tl_data = json.loads(tl_raw)
            except json.JSONDecodeError:
                jm = re.search(r"\{[\s\S]*\}", tl_raw)
                tl_data = json.loads(jm.group()) if jm else {"events": []}
            except Exception as e:
                logger.warning(f"关系时间轴 LLM 解析失败，跳过写入: {e}")
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
                        insert_relationship_timeline_event(
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
            daily_summaries = get_recent_daily_summaries(limit=1)
            if not daily_summaries:
                logger.info(f"今日没有小传，跳过 Step 4，日期: {batch_date}")
                return True, None
            
            daily_summary = daily_summaries[0]
            summary_text = daily_summary['summary_text']
            summary_id = daily_summary['id']
            
            prompt = f"""请评估以下今日小传的长期保留价值，给出1-10分的评分（10分最高）：
            
今日小传内容：
{summary_text}

评分标准：
1-3分：日常琐事，没有长期参考价值
4-6分：有一定参考价值，但信息较为普通
7-8分：有价值的信息，值得长期保留
9-10分：非常重要的信息，对长期记忆有显著价值

请只返回一个整数分数（1-10），不要有其他文字。"""
            
            try:
                score_response = self.llm.generate(prompt)
                score_text = score_response.content
                score_match = re.search(r'\b([1-9]|10)\b', score_text)
                if score_match:
                    score = int(score_match.group(1))
                else:
                    score = 5
                    logger.warning(
                        f"无法从LLM响应中提取分数，使用默认值: {score}, 响应: {score_text}"
                    )
                logger.info(f"今日小传价值分: {score}/10")
            except Exception as e:
                logger.error(f"LLM 价值打分失败: {e}")
                score = 5
            
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
若需要拆分，返回严格 JSON：{{"events":["事件1","事件2",...]}}；若不需要则 {{"events":[]}}。
不要编造原文没有的内容。

今日小传：
{summary_text}"""
            
            event_texts: List[str] = []
            split_raw = ""
            try:
                split_raw = self.summary_llm.generate_summary(
                    [{"role": "user", "content": split_prompt}]
                )
                split_data = json.loads(split_raw)
            except json.JSONDecodeError:
                sm = re.search(r"\{[\s\S]*\}", split_raw)
                split_data = json.loads(sm.group()) if sm else {"events": []}
            except Exception as e:
                logger.warning(f"事件拆分解析失败，跳过子文档: {e}")
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
        """Step 5 - Chroma 向量记忆 GC（衰减 + 90 天未访问 + 无子节点）。"""
        try:
            n = garbage_collect_stale_memories(
                idle_days_threshold=90.0,
                strength_threshold=0.05,
                scan_limit=10000,
            )
            logger.info(f"Step 5 GC 删除 {n} 条，日期: {batch_date}")
            return True, None
        except Exception as e:
            logger.error(f"Step 5 执行失败: {e}")
            return False, str(e)


async def schedule_daily_batch():
    """
    定时调度日终跑批处理。
    
    每天东八区（Asia/Shanghai）晚上23:00自动触发。
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
            
            # 计算到今晚23:00的时间差
            target_time = now.replace(hour=23, minute=0, second=0, microsecond=0)
            
            # 如果现在已经过了23:00，则目标时间设为明天的23:00
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
            
            mark_expired_skipped_daily_batch_logs_before(window_start_s)
            
            pending = list_incomplete_daily_batch_dates_in_range(
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
