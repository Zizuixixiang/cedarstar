"""
日终跑批独立入口，由 cron 定时调用。
用法：python run_daily_batch.py [YYYY-MM-DD]

失败时由 ``schedule_daily_batch_retry_if_needed`` 判定：``retry_count`` < 3 时
在约 2 小时后由独立子进程重试同 ``batch_date``（断点续跑）；达上限则发 Telegram 告警。
"""
import sys
import asyncio
import logging
from dotenv import load_dotenv

load_dotenv()

from memory.database import initialize_database
from memory.daily_batch import (
    DailyBatchProcessor,
    resolve_daily_batch_date,
    schedule_daily_batch_retry_if_needed,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    batch_arg = sys.argv[1] if len(sys.argv) > 1 else None

    # 初始化数据库连接池
    await initialize_database()
    resolved_batch_date = await resolve_daily_batch_date(batch_arg)

    processor = DailyBatchProcessor()
    success = await processor.run_daily_batch(resolved_batch_date)

    if success:
        logger.info("日终跑批完成")
        sys.exit(0)
    else:
        logger.error("日终跑批失败")
        await schedule_daily_batch_retry_if_needed(resolved_batch_date)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
