import logging
import traceback
import asyncio
from typing import Optional, List, Dict, Any
from config import config

# 全局日志缓冲（线程安全）
_log_buffer: List[Dict[str, Any]] = []

class AsyncDatabaseLogHandler(logging.Handler):
    """
    异步数据库日志处理器。
    每次 emit 时只写入内存全局数组，配合异步 task 批量入库，
    完美避开了不同事件循环共享 asyncpg.Pool 导致的 RuntimeError。
    """
    def __init__(self, platform: Optional[str] = None):
        super().__init__()
        self.platform = platform

    def emit(self, record: logging.LogRecord):
        try:
            stack_trace = None
            if record.exc_info:
                stack_trace = ''.join(traceback.format_exception(*record.exc_info))
            
            _log_buffer.append({
                "level": record.levelname,
                "message": record.getMessage(),
                "platform": getattr(record, 'platform', self.platform),
                "stack_trace": stack_trace
            })
            
            # 安全防溢出限制
            if len(_log_buffer) > 5000:
                _log_buffer.pop(0)
        except Exception as e:
            print(f"日志排队失败: {e}")

async def log_flusher_task():
    """
    后台协程：每隔 1.5 秒将排队的日志一并入库
    """
    from memory.database import get_database
    
    while True:
        await asyncio.sleep(1.5)
        if not _log_buffer:
            continue
            
        logs_to_write = _log_buffer[:]
        _log_buffer.clear()
        
        try:
            db = get_database()
            if db.pool is None:
                continue
                
            for item in logs_to_write:
                try:
                    await db.save_log(
                        level=item["level"],
                        message=item["message"][:4000],  # 截断超长报文
                        platform=item["platform"],
                        stack_trace=item["stack_trace"]
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"日志后台刷新异常: {e}")

def setup_async_logging(platform: Optional[str] = "SYSTEM"):
    """
    注册 Handler 到根 logger。需要随后在带事件循环的环境运行 log_flusher_task
    """
    root_logger = logging.getLogger()
    handler = AsyncDatabaseLogHandler(platform=platform)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    
    # 避免 httpx 等模块低级别被过度写入数据库（这里跟 config.LOG_LEVEL 对齐）
    try:
        handler.setLevel(getattr(logging, config.LOG_LEVEL))
    except AttributeError:
        handler.setLevel(logging.INFO)
        
    root_logger.addHandler(handler)
    logging.getLogger(__name__).debug("异步数据库写日志机制已挂载")