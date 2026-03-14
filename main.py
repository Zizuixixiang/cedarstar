"""
CedarStar 项目主入口。

负责初始化并启动所有组件，包括：
1. Discord 机器人
2. LLM 接口
3. 记忆存储
4. 日终跑批定时任务
"""

import asyncio
import logging
import sys
import os
from datetime import datetime
import pytz

# 添加当前目录到 Python 路径
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import config, validate_config


def setup_logging():
    """
    设置日志配置。
    """
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('cedarstar.log', encoding='utf-8')
        ]
    )
    
    # 设置第三方库的日志级别
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)


async def run_discord_bot():
    """
    运行 Discord 机器人。
    """
    from bot.discord_bot import DiscordBot
    
    logger = logging.getLogger(__name__)
    logger.info("启动 Discord 机器人...")
    
    try:
        bot = DiscordBot()
        
        # 直接运行机器人（这是一个阻塞调用）
        # 注意：discord.py 的 run() 方法是阻塞的，我们需要在单独的线程中运行它
        # 或者使用 discord.py 的异步启动方式
        
        # 方法1：在线程中运行（简单但可能有问题）
        # bot_task = asyncio.create_task(asyncio.to_thread(bot.run))
        
        # 方法2：直接运行（阻塞主线程）
        # 由于 discord.py 的 run() 是阻塞的，我们在这里直接运行它
        # 这会导致这个函数不会返回，所以我们需要调整架构
        
        # 简化：直接运行机器人，不返回任务
        # 在实际部署中，可能需要更复杂的异步处理
        logger.info("Discord 机器人将在主线程中运行...")
        
        # 由于 discord.py 的 run() 是阻塞的，我们无法在这里返回任务
        # 所以我们将机器人运行放在单独的线程中
        import threading
        
        def run_bot():
            try:
                bot.run()
            except Exception as e:
                logger.error(f"Discord 机器人运行失败: {e}")
        
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        
        logger.info("Discord 机器人已启动（在后台线程中）")
        
        # 返回一个永远不会完成的任务，以保持异步循环运行
        # 实际上，我们不需要返回任务，因为机器人已经在后台运行
        return asyncio.Future()  # 永远不会完成的 Future
        
    except Exception as e:
        logger.error(f"启动 Discord 机器人失败: {e}")
        raise


async def run_daily_batch_scheduler():
    """
    运行日终跑批定时调度器。
    """
    from memory.daily_batch import schedule_daily_batch
    
    logger = logging.getLogger(__name__)
    logger.info("启动日终跑批定时调度器...")
    
    try:
        # 启动定时调度器
        scheduler_task = asyncio.create_task(schedule_daily_batch())
        
        return scheduler_task
        
    except Exception as e:
        logger.error(f"启动日终跑批定时调度器失败: {e}")
        raise


async def main_async():
    """
    异步主函数。
    """
    logger = logging.getLogger(__name__)
    
    try:
        # 验证配置
        logger.info("验证配置...")
        validate_config()
        
        # 启动 Discord 机器人
        bot_task = await run_discord_bot()
        
        # 启动日终跑批定时调度器
        scheduler_task = await run_daily_batch_scheduler()
        
        logger.info("所有组件启动完成")
        logger.info(f"当前时区: {pytz.timezone('Asia/Shanghai')}")
        logger.info(f"日终跑批触发时间: 每天 23:00 (Asia/Shanghai)")
        
        # 等待所有任务完成（实际上会一直运行）
        await asyncio.gather(bot_task, scheduler_task)
        
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    except Exception as e:
        logger.error(f"主程序运行失败: {e}")
        raise


def main():
    """
    主函数。
    """
    # 设置日志
    setup_logging()
    
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("CedarStar 项目启动")
    logger.info(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Python 版本: {sys.version}")
    logger.info(f"工作目录: {os.getcwd()}")
    logger.info("=" * 60)
    
    try:
        # 运行异步主函数
        asyncio.run(main_async())
        
    except KeyboardInterrupt:
        logger.info("程序已终止")
    except Exception as e:
        logger.error(f"程序运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
