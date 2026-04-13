"""
CedarStar 项目主入口。

负责初始化并启动所有组件，包括：
1. 配置校验后、Bot 收消息前阻塞重建 BM25 索引（对齐 Chroma）
2. Discord 机器人
3. Telegram：初始化 Application，由 FastAPI `/webhook/telegram` 接收推送（无 polling）
4. FastAPI REST API 服务

日终跑批由 cron 调用项目根目录 `run_daily_batch.py`，不再在进程内定时调度。
"""

import asyncio
import logging
import sys
import os
from datetime import datetime
from pathlib import Path
import pytz
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
import uvicorn

# 添加当前目录到 Python 路径
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import config, validate_config
from api.router import api_router
from api.webhook import router as telegram_webhook_router

# 创建 FastAPI 应用
app = FastAPI(
    title="CedarStar API",
    description="CedarStar Mini App 管理接口",
    version="0.1.0"
)

# 配置 CORS：本地 Vite + Tunnel / Cloudflare Pages 前端
_CORS_ALLOW_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "https://exercises-broadway-expenditures-bacteria.trycloudflare.com",
    "https://cedarstar.pages.dev",
]
# https://*.cedarstar.pages.dev（含多级子域）；apex 亦在上表与正则中均可匹配
_CORS_PAGES_DEV_REGEX = r"^https://([\w-]+\.)*cedarstar\.pages\.dev$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOW_ORIGINS,
    allow_origin_regex=_CORS_PAGES_DEV_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Telegram webhook：不带 /api，与控制台 /api/* 的 Access 规则分离
app.include_router(telegram_webhook_router)

API_KEY_HEADER = APIKeyHeader(name="X-Cedarstar-Token", auto_error=False)


async def verify_token(token: str | None = Depends(API_KEY_HEADER)):
    if token != config.MINIAPP_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


# 包含 API 路由（Mini App 控制台：须带 X-Cedarstar-Token）
app.include_router(api_router, prefix="/api", dependencies=[Depends(verify_token)])

# 根路径
@app.get("/")
async def root():
    return {"message": "CedarStar API is running"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


MINIAPP_DIST = Path("miniapp/dist")


@app.get("/app/{full_path:path}")
async def serve_miniapp(full_path: str):
    candidate = MINIAPP_DIST / full_path
    if candidate.is_file():
        return FileResponse(str(candidate))
    index = MINIAPP_DIST / "index.html"
    if index.is_file():
        return FileResponse(str(index))
    return Response(status_code=404)


class _SuppressTelegramBotApiUrlInfoFilter(logging.Filter):
    """
    httpx/httpcore 在 INFO 会打印完整 URL；Telegram Bot API 路径含 token，写入日志有泄露风险。
    对指向 api.telegram.org 的记录仅保留 WARNING 及以上（等效于对该主机把 INFO 降噪）。
    """

    _needle = "://api.telegram.org"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self._needle in msg:
            return False
        return True


def setup_logging():
    """
    设置日志配置。
    """
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f"{config.APP_NAME}.log", encoding='utf-8')
        ]
    )
    
    # 设置第三方库的日志级别
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    _tg_url_filter = _SuppressTelegramBotApiUrlInfoFilter()
    logging.getLogger("httpx").addFilter(_tg_url_filter)
    logging.getLogger("httpcore").addFilter(_tg_url_filter)


async def run_discord_bot():
    """
    运行 Discord 机器人。
    
    使用线程运行 Discord 机器人，避免阻塞主事件循环。
    
    Returns:
        asyncio.Task: Discord 机器人任务
    """
    from bot.discord_bot import DiscordBot
    
    logger = logging.getLogger(__name__)
    logger.info("启动 Discord 机器人...")
    
    try:
        bot = DiscordBot()
        
        # 由于 discord.py 的 run() 是阻塞的，我们在单独的线程中运行它
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
        return asyncio.Future()  # 永远不会完成的 Future
        
    except Exception as e:
        logger.error(f"启动 Discord 机器人失败: {e}")
        raise


async def run_fastapi_server():
    """
    运行 FastAPI 服务器。
    
    Returns:
        asyncio.Task: FastAPI 服务器任务
    """
    logger = logging.getLogger(__name__)
    logger.info("启动 FastAPI 服务器...")
    
    try:
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config)
        
        logger.info("FastAPI 服务器已启动，端口 8000")
        logger.info("API 文档地址: http://localhost:8000/docs")
        
        # 运行服务器
        await server.serve()
        
    except Exception as e:
        logger.error(f"启动 FastAPI 服务器失败: {e}")
        raise


async def main_async():
    """
    异步主函数。
    
    并行启动任务：
    1. Discord 机器人（可选，见 ENABLE_DISCORD）
    2. FastAPI REST API 服务器（含 Telegram webhook 路由）

    Telegram Application 在收请求前由 setup_telegram_webhook_app() 初始化，无独立长驻任务。
    日终跑批见根目录 run_daily_batch.py（cron）。
    """
    logger = logging.getLogger(__name__)
    
    try:
        # 验证配置
        logger.info("验证配置...")
        validate_config()

        # 初始化 PostgreSQL 连接池（必须在启动任何 Bot 或 LLM 组件之前完成）
        from memory.database import initialize_database
        await initialize_database()

        from tools.lutopia import ensure_lutopia_dm_send_enabled_on_startup

        await ensure_lutopia_dm_send_enabled_on_startup()

        # 挂载异步系统日志以供 MiniApp 面板查询
        from memory.async_log_handler import setup_async_logging, log_flusher_task
        setup_async_logging("SYSTEM")
        asyncio.create_task(log_flusher_task())

        # 任一 Bot 开始收消息前，阻塞重建 BM25 索引（与 Chroma 全量对齐；无文档时为空索引，不抛错）
        logger.info("重建 BM25 内存索引（memory.bm25_retriever.refresh_index）...")
        from memory.bm25_retriever import get_bm25_retriever

        if not get_bm25_retriever().refresh_index():
            logger.warning(
                "BM25 索引刷新未成功，关键词检索可能为空；服务仍继续启动"
            )

        from bot.telegram_bot import (
            setup_telegram_webhook_app,
            shutdown_telegram_webhook_app,
        )

        await setup_telegram_webhook_app()

        try:
            # 并行启动任务
            logger.info("并行启动任务（Discord / FastAPI）...")

            tasks = []
            if config.ENABLE_DISCORD:
                tasks.append(asyncio.create_task(run_discord_bot()))

            # 启动 FastAPI 服务器
            tasks.append(asyncio.create_task(run_fastapi_server()))

            logger.info("所有组件启动完成")
            logger.info(f"当前时区: {pytz.timezone('Asia/Shanghai')}")
            logger.info(f"API 文档地址: http://localhost:8000/docs")

            # 等待所有任务完成（实际上会一直运行）
            await asyncio.gather(*tasks)
        finally:
            await shutdown_telegram_webhook_app()

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