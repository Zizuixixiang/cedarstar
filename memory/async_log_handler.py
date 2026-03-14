#!/usr/bin/env python3
"""
异步日志处理器模块。

实现一个自定义的 logging.Handler，将所有日志在输出终端的同时异步写入数据库。
使用 asyncio.Queue 实现真正的异步非阻塞入库，不卡住主事件循环。
"""

import asyncio
import logging
import threading
import queue
import traceback
from typing import Optional
from config import Platform
from memory.database import get_database


class AsyncDatabaseLogHandler(logging.Handler):
    """
    异步数据库日志处理器。
    
    将所有日志异步写入数据库，不阻塞主事件循环。
    """
    
    def __init__(self, level=logging.NOTSET, platform: Optional[str] = None):
        """
        初始化异步日志处理器。
        
        Args:
            level: 日志级别
            platform: 平台标识（可选）
        """
        super().__init__(level)
        
        self.platform = platform
        self.log_queue = queue.Queue(maxsize=1000)  # 线程安全的队列
        self.worker_thread = None
        self.stop_event = threading.Event()
        
        # 启动工作线程
        self._start_worker()
    
    def _start_worker(self):
        """启动工作线程处理日志队列。"""
        self.worker_thread = threading.Thread(
            target=self._worker_loop,
            name="AsyncLogHandlerWorker",
            daemon=True  # 设置为守护线程，主程序退出时自动退出
        )
        self.worker_thread.start()
        logging.getLogger(__name__).debug("异步日志处理器工作线程已启动")
    
    def _worker_loop(self):
        """工作线程循环，从队列中取出日志并写入数据库。"""
        while not self.stop_event.is_set():
            try:
                # 从队列中获取日志记录，最多等待1秒
                log_record = self.log_queue.get(timeout=1.0)
                
                try:
                    # 写入数据库
                    self._write_log_to_database(log_record)
                except Exception as e:
                    # 如果数据库写入失败，打印错误但不阻塞
                    print(f"异步日志写入数据库失败: {e}")
                
                # 标记任务完成
                self.log_queue.task_done()
                
            except queue.Empty:
                # 队列为空，继续循环
                continue
            except Exception as e:
                # 其他异常，打印错误但不退出循环
                print(f"异步日志处理器工作线程异常: {e}")
                continue
    
    def _write_log_to_database(self, log_record: logging.LogRecord):
        """
        将日志记录写入数据库。
        
        Args:
            log_record: 日志记录对象
        """
        try:
            # 获取数据库实例
            db = get_database()
            
            # 提取堆栈跟踪信息
            stack_trace = None
            if log_record.exc_info:
                # 如果有异常信息，提取堆栈跟踪
                stack_trace = ''.join(traceback.format_exception(*log_record.exc_info))
            
            # 保存日志到数据库
            db.save_log(
                level=log_record.levelname,
                message=log_record.getMessage(),
                platform=self.platform,
                stack_trace=stack_trace
            )
            
        except Exception as e:
            # 如果数据库操作失败，打印错误但不抛出
            print(f"写入日志到数据库失败: {e}")
    
    def emit(self, record: logging.LogRecord):
        """
        处理日志记录（覆盖父类方法）。
        
        将日志记录放入队列，由工作线程异步处理。
        
        Args:
            record: 日志记录对象
        """
        try:
            # 将日志记录放入队列（非阻塞，如果队列满则丢弃）
            self.log_queue.put_nowait(record)
        except queue.Full:
            # 队列已满，丢弃日志记录
            print(f"日志队列已满，丢弃日志记录: {record.getMessage()}")
        except Exception as e:
            # 其他异常，打印错误但不抛出
            print(f"异步日志处理器emit异常: {e}")
    
    def close(self):
        """
        关闭日志处理器（覆盖父类方法）。
        
        停止工作线程并等待队列处理完成。
        """
        # 设置停止事件
        self.stop_event.set()
        
        # 等待工作线程结束
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)
        
        # 调用父类的close方法
        super().close()
        
        logging.getLogger(__name__).debug("异步日志处理器已关闭")


def setup_async_logging(platform: Optional[str] = None, level: str = "INFO"):
    """
    设置异步日志记录。
    
    Args:
        platform: 平台标识（可选）
        level: 日志级别，默认为 "INFO"
    """
    # 获取根日志记录器
    root_logger = logging.getLogger()
    
    # 创建异步数据库日志处理器
    async_handler = AsyncDatabaseLogHandler(
        level=getattr(logging, level.upper()),
        platform=platform
    )
    
    # 设置日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    async_handler.setFormatter(formatter)
    
    # 添加处理器到根日志记录器
    root_logger.addHandler(async_handler)
    
    # 设置日志级别
    root_logger.setLevel(getattr(logging, level.upper()))
    
    logging.getLogger(__name__).info(f"异步日志记录已设置，平台: {platform}, 级别: {level}")


def setup_platform_logging(platform: str, level: str = "INFO"):
    """
    为特定平台设置异步日志记录。
    
    Args:
        platform: 平台标识，使用 Platform 常量
        level: 日志级别，默认为 "INFO"
    """
    setup_async_logging(platform=platform, level=level)


# 测试函数
def test_async_log_handler():
    """测试异步日志处理器。"""
    import time
    
    print("测试异步日志处理器...")
    
    try:
        # 设置异步日志记录
        setup_async_logging(platform=Platform.SYSTEM, level="DEBUG")
        
        # 获取测试日志记录器
        test_logger = logging.getLogger("test_async_log_handler")
        
        # 记录不同级别的日志
        test_logger.debug("这是一条调试日志")
        test_logger.info("这是一条信息日志")
        test_logger.warning("这是一条警告日志")
        test_logger.error("这是一条错误日志")
        
        # 记录带异常的日志
        try:
            raise ValueError("测试异常")
        except ValueError as e:
            test_logger.exception("捕获到异常: %s", e)
        
        # 等待日志处理完成
        print("等待日志处理完成...")
        time.sleep(2)
        
        print("异步日志处理器测试完成")
        
    except Exception as e:
        print(f"异步日志处理器测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    """异步日志处理器模块测试入口。"""
    test_async_log_handler()