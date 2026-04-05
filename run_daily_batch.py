"""
日终跑批独立入口，由 cron 定时调用。
用法：python run_daily_batch.py [YYYY-MM-DD]
"""
import sys
import asyncio
import logging
from dotenv import load_dotenv

load_dotenv()

from config import config
from memory.database import initialize_database
from memory.daily_batch import DailyBatchProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    batch_date = sys.argv[1] if len(sys.argv) > 1 else None

    # 初始化数据库连接池
    await initialize_database()

    processor = DailyBatchProcessor()
    success = await processor.run_daily_batch(batch_date)

    if success:
        logger.info("日终跑批完成")
        sys.exit(0)
    else:
        logger.error("日终跑批失败")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
