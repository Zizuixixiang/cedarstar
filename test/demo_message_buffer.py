#!/usr/bin/env python3
"""
演示消息缓冲功能。
"""

import sys
import os
import asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def demo_message_buffer():
    """演示消息缓冲功能"""
    print("=== 消息缓冲功能演示 ===")
    print()
    
    try:
        from memory.database import get_database
        db = get_database()
        
        # 显示当前配置
        buffer_delay = db.get_config("buffer_delay", "5")
        print(f"1. 当前消息缓冲延迟配置: {buffer_delay}秒")
        print(f"   - 默认值: 5秒")
        print(f"   - 可调整范围: 3-100秒")
        print()
        
        # 演示配置更新
        print("2. 演示配置更新:")
        print("   - 通过前端 Config 页面可以调整 buffer_delay 值")
        print("   - 调整后立即生效，无需重启服务")
        print("   - Discord bot 和 Telegram bot 都会使用新配置")
        print()
        
        # 演示消息缓冲逻辑
        print("3. 消息缓冲逻辑:")
        print("   - 收到消息后等待 buffer_delay 秒")
        print("   - 期间如果同一 session 有新消息进来就重置计时器")
        print("   - buffer_delay 秒内没有新消息才处理缓冲区")
        print("   - 将缓冲区里所有消息合并成一条处理")
        print()
        
        # 演示 bot 如何使用配置
        print("4. Bot 如何使用配置:")
        print("   - Discord bot: 从数据库获取 buffer_delay 配置")
        print("   - Telegram bot: 从数据库获取 buffer_delay 配置")
        print("   - 配置存储在 SQLite 数据库的 config 表中")
        print()
        
        # 显示数据库中的配置
        all_configs = db.get_all_configs()
        print("5. 数据库中的配置项:")
        for key, value in all_configs.items():
            print(f"   - {key}: {value}")
        print()
        
        # 演示配置热更新
        print("6. 配置热更新演示:")
        print("   - 修改 buffer_delay 为 10 秒")
        db.set_config("buffer_delay", "10")
        new_value = db.get_config("buffer_delay", "5")
        print(f"   - 新值: {new_value}秒")
        
        # 恢复默认值
        db.set_config("buffer_delay", "5")
        print("   - 已恢复默认值: 5秒")
        print()
        
        print("=== 演示完成 ===")
        print()
        print("总结:")
        print("- 消息缓冲延迟已从固定值改为可配置项")
        print("- 配置项名: buffer_delay")
        print("- 默认值: 5秒")
        print("- 可调整范围: 3-100秒")
        print("- 支持热更新，无需重启服务")
        print("- Discord bot 和 Telegram bot 都已集成")
        
    except Exception as e:
        print(f"演示失败: {e}")
        import traceback
        traceback.print_exc()

def main():
    """主函数"""
    asyncio.run(demo_message_buffer())

if __name__ == "__main__":
    main()