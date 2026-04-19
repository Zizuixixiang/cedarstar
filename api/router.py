"""
API 路由汇总模块。

汇总所有 API 路由，在 main.py 里 include。
"""
from fastapi import APIRouter

from api import autonomous, sensor, weather
from api.dashboard import router as dashboard_router
from api.persona import router as persona_router
from api.memory import router as memory_router
from api.history import router as history_router
from api.logs import router as logs_router
from api.config import router as config_router
from api.settings import router as settings_router

# 创建主路由
api_router = APIRouter()

# 包含所有子路由
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["控制台"])
api_router.include_router(persona_router, prefix="/persona", tags=["人设配置"])
api_router.include_router(memory_router, prefix="/memory", tags=["记忆管理"])
api_router.include_router(history_router, prefix="/history", tags=["对话历史"])
api_router.include_router(logs_router, prefix="/logs", tags=["日志"])
api_router.include_router(config_router, prefix="/config", tags=["助手配置"])
api_router.include_router(settings_router, prefix="/settings", tags=["设置"])
api_router.include_router(autonomous.router, prefix="/autonomous", tags=["autonomous"])
api_router.include_router(sensor.router, prefix="/sensor", tags=["sensor"])
api_router.include_router(weather.router, prefix="/weather", tags=["weather"])
