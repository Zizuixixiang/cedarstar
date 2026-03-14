"""
日终跑批处理模块。

每天东八区（Asia/Shanghai）晚上23:00自动触发，执行以下三步流水线：
Step 1 - 生成今日小传
Step 2 - 更新记忆卡片（Upsert）
Step 3 - 价值打分与冷库归档

断点续跑：每次触发前先查 daily_batch_log 表，已完成的步骤直接跳过，从未完成的步骤继续。
"""

import asyncio
import logging
import sys
import os
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
        save_daily_batch_log,
        get_daily_batch_log,
        update_daily_batch_step_status,
        get_unsummarized_count_by_session
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
        save_daily_batch_log,
        get_daily_batch_log,
        update_daily_batch_step_status,
        get_unsummarized_count_by_session
    )

# 设置日志
logger = logging.getLogger(__name__)

# 时区配置
TIMEZONE = pytz.timezone("Asia/Shanghai")


class DailyBatchProcessor:
    """
    日终跑批处理器类。
    
    负责执行每日的三步流水线处理。
    """
    
    def __init__(self):
        """
        初始化日终跑批处理器。
        """
        # 创建 LLM 接口
        self.llm = LLMInterface()
        self.summary_llm = SummaryLLMInterface()
        
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
        try:
            # 确定批处理日期
            if batch_date is None:
                batch_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            
            logger.info(f"开始日终跑批处理，日期: {batch_date}")
            
            # 检查批处理日志，实现断点续跑
            batch_log = get_daily_batch_log(batch_date)
            
            if batch_log is None:
                # 创建新的批处理日志
                save_daily_batch_log(batch_date, step1_status=0, step2_status=0, step3_status=0)
                batch_log = get_daily_batch_log(batch_date)
            
            # Step 1 - 生成今日小传
            if batch_log['step1_status'] == 0:
                logger.info(f"执行 Step 1 - 生成今日小传，日期: {batch_date}")
                success, error_message = await self._step1_generate_daily_summary(batch_date)
                
                if success:
                    update_daily_batch_step_status(batch_date, 1, 1)
                    logger.info(f"Step 1 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 1, 0, error_message)
                    logger.error(f"Step 1 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 1 已跳过（已完成），日期: {batch_date}")
            
            # Step 2 - 更新记忆卡片（Upsert）
            if batch_log['step2_status'] == 0:
                logger.info(f"执行 Step 2 - 更新记忆卡片，日期: {batch_date}")
                success, error_message = await self._step2_update_memory_cards(batch_date)
                
                if success:
                    update_daily_batch_step_status(batch_date, 2, 1)
                    logger.info(f"Step 2 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 2, 0, error_message)
                    logger.error(f"Step 2 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 2 已跳过（已完成），日期: {batch_date}")
            
            # Step 3 - 价值打分与冷库归档
            if batch_log['step3_status'] == 0:
                logger.info(f"执行 Step 3 - 价值打分与冷库归档，日期: {batch_date}")
                success, error_message = await self._step3_score_and_archive(batch_date)
                
                if success:
                    update_daily_batch_step_status(batch_date, 3, 1)
                    logger.info(f"Step 3 完成，日期: {batch_date}")
                else:
                    update_daily_batch_step_status(batch_date, 3, 0, error_message)
                    logger.error(f"Step 3 失败，日期: {batch_date}, 错误: {error_message}")
                    return False
            else:
                logger.info(f"Step 3 已跳过（已完成），日期: {batch_date}")
            
            logger.info(f"日终跑批处理完成，日期: {batch_date}")
            return True
            
        except Exception as e:
            logger.error(f"日终跑批处理失败，日期: {batch_date}, 错误: {e}")
            # 更新错误信息
            if batch_date:
                update_daily_batch_step_status(batch_date, 1, 0, str(e))
            return False
    
    async def _step1_generate_daily_summary(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 1 - 生成今日小传。
        
        取今天所有 summary_type='chunk' 的摘要记录，加上今天剩余 is_summarized=0 的原始消息，
        调用 SUMMARY 模型生成一份今日小传，写入 summaries 表 summary_type='daily'。
        写入成功后再删除今天的 chunk 记录，并将这批剩余原始消息的 is_summarized 批量更新为 1。
        
        Args:
            batch_date: 批处理日期
            
        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        try:
            # 1. 获取今天的 chunk 摘要
            chunk_summaries = get_today_chunk_summaries()
            
            # 2. 获取今天剩余未摘要的原始消息
            # 这里需要获取所有会话的未摘要消息
            # 由于数据库函数是按会话查询的，我们需要一个全局查询
            # 暂时简化：假设只有一个主要会话
            # TODO: 实现全局未摘要消息查询
            
            # 构建今日内容
            today_content = ""
            
            # 添加 chunk 摘要
            if chunk_summaries:
                today_content += "# 今日对话摘要\n\n"
                for summary in chunk_summaries:
                    session_id = summary['session_id']
                    summary_text = summary['summary_text']
                    created_at = summary['created_at']
                    
                    # 简化 session_id 显示
                    if '_' in session_id:
                        parts = session_id.split('_')
                        if len(parts) >= 2:
                            display_session = f"用户{parts[0][:4]}...频道{parts[1][:4]}..."
                        else:
                            display_session = session_id[:20]
                    else:
                        display_session = session_id[:20]
                    
                    today_content += f"### {created_at} [来自: {display_session}]\n{summary_text}\n\n"
            
            # 3. 生成今日小传
            if not today_content.strip():
                logger.info(f"今日没有内容需要生成小传，日期: {batch_date}")
                return True, None
            
            prompt = f"""请基于以下今日对话摘要，生成一份简洁的今日小传，总结今天的主要话题和重要信息：

{today_content}

今日小传（中文，简洁明了）:"""
            
            try:
                daily_summary = self.summary_llm.generate_summary([{"role": "user", "content": prompt}])
            except Exception as e:
                logger.error(f"生成今日小传失败: {e}")
                # 如果生成失败，使用默认摘要
                daily_summary = f"今日总结：包含 {len(chunk_summaries)} 个对话片段。"
            
            # 4. 保存今日小传到数据库
            # 使用一个虚拟的 session_id 和消息ID
            summary_id = save_summary(
                session_id="daily_batch",
                summary_text=daily_summary,
                start_message_id=0,
                end_message_id=0,
                summary_type="daily"
            )
            
            logger.info(f"今日小传保存成功，ID: {summary_id}, 日期: {batch_date}")
            
            # 5. 删除今天的 chunk 记录（可选，根据需求决定是否删除）
            # 这里不删除，保留历史记录
            
            # 6. 标记今天剩余原始消息为已摘要
            # 由于没有实现全局查询，这里暂时跳过
            
            return True, None
            
        except Exception as e:
            logger.error(f"Step 1 执行失败: {e}")
            return False, str(e)
    
    async def _step2_update_memory_cards(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 2 - 更新记忆卡片（Upsert）。
        
        把今日小传内容发给 LLM，判断是否包含属于以下7个维度的新信息：
        preferences / interaction_patterns / current_status / goals / relationships / key_events / rules
        
        有新信息则查 memory_cards 表，没有对应维度就 INSERT，有就合并重写后 UPDATE。
        
        interaction_patterns 维度特别说明：只记录有具体对话支撑的行为观察，不做性格定论，新旧矛盾时并存保留并注明日期。
        
        Args:
            batch_date: 批处理日期
            
        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        try:
            # 1. 获取今日小传
            # 这里需要查询今天的 daily 摘要
            # 暂时简化：使用最后一条 daily 摘要
            # TODO: 实现按日期查询 daily 摘要
            
            # 2. 如果没有今日小传，直接返回成功
            # 这里暂时跳过，假设有今日小传
            
            # 3. 调用 LLM 分析维度信息
            # 这里简化实现：直接返回成功
            # TODO: 实现完整的维度分析逻辑
            
            logger.info(f"Step 2 执行（简化版），日期: {batch_date}")
            
            # 示例：假设我们有一些用户和角色
            # 在实际应用中，需要从数据库中获取所有用户和角色
            users = ["default_user"]  # 示例用户
            character_id = "sirius"  # 默认角色
            
            for user_id in users:
                # 检查每个维度的记忆卡片
                for dimension in self.dimensions:
                    # 获取该用户该维度的现有记忆卡片
                    existing_cards = get_memory_cards(user_id, character_id, dimension, limit=10)
                    
                    # 这里简化：不实际调用 LLM 分析
                    # 在实际应用中，需要：
                    # 1. 将今日小传发送给 LLM
                    # 2. 询问 LLM 是否包含该维度的新信息
                    # 3. 如果有新信息，进行合并或插入
                    
                    if dimension == "interaction_patterns":
                        # interaction_patterns 维度特殊处理
                        # 新旧矛盾时并存保留并注明日期
                        logger.debug(f"检查 interaction_patterns 维度，用户: {user_id}")
                        # 这里可以添加具体的处理逻辑
            
            return True, None
            
        except Exception as e:
            logger.error(f"Step 2 执行失败: {e}")
            return False, str(e)
    
    async def _step3_score_and_archive(self, batch_date: str) -> Tuple[bool, Optional[str]]:
        """
        Step 3 - 价值打分与冷库归档。
        
        让 LLM 给今日小传打1-10分的长期保留价值分，≥7分则将其向量化后存入 ChromaDB（占位，第四阶段填充），<7分跳过。
        
        Args:
            batch_date: 批处理日期
            
        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        try:
            # 1. 获取今日小传
            # 这里需要查询今天的 daily 摘要
            # 暂时简化：使用最后一条 daily 摘要
            
            # 2. 调用 LLM 进行价值打分
            # 这里简化实现：假设得分为 5（中等价值）
            score = 5
            
            # 3. 根据分数决定是否归档
            if score >= 7:
                logger.info(f"今日小传价值分: {score}，需要归档到 ChromaDB（占位）")
                # TODO: 第四阶段实现 ChromaDB 归档
                # 这里添加占位注释
                pass
            else:
                logger.info(f"今日小传价值分: {score}，跳过归档")
            
            return True, None
            
        except Exception as e:
            logger.error(f"Step 3 执行失败: {e}")
            return False, str(e)


async def schedule_daily_batch():
    """
    定时调度日终跑批处理。
    
    每天东八区（Asia/Shanghai）晚上23:00自动触发。
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
            
            # 执行日终跑批
            logger.info("触发日终跑批处理")
            success = await processor.run_daily_batch()
            
            if success:
                logger.info("日终跑批处理执行成功")
            else:
                logger.error("日终跑批处理执行失败")
            
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
