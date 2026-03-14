#!/usr/bin/env python3
"""
启动 Discord 机器人的脚本。
"""

import os
import sys

# 添加当前目录到 Python 路径
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

print("正在启动 Discord 机器人...")
print(f"当前目录: {current_dir}")
print(f"Python 路径: {sys.path}")

try:
    # 导入配置模块
    import config
    print("✓ config 模块导入成功")
    
    # 验证配置
    config.validate_config()
    print("✓ 配置验证成功")
    
    # 导入 Discord 机器人
    from bot.discord_bot import DiscordBot
    print("✓ DiscordBot 类导入成功")
    
    # 创建并运行机器人
    print("正在创建 Discord 机器人实例...")
    bot = DiscordBot()
    
    print("正在启动 Discord 机器人...")
    print("注意：机器人启动后会在后台运行，按 Ctrl+C 停止")
    bot.run()
    
except ImportError as e:
    print(f"✗ 导入失败: {e}")
    print("请检查模块路径和依赖安装")
except ValueError as e:
    print(f"✗ 配置错误: {e}")
    print("请检查 .env 文件中的配置项")
except Exception as e:
    print(f"✗ 启动失败: {e}")
    import traceback
    traceback.print_exc()