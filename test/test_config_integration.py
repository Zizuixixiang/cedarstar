#!/usr/bin/env python3
"""
测试配置集成功能。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_database_config():
    """测试数据库配置功能"""
    print("=== 测试数据库配置功能 ===")
    
    try:
        from memory.database import get_database
        db = get_database()
        
        # 测试获取默认配置
        buffer_delay = db.get_config("buffer_delay", "5")
        print(f"从数据库获取 buffer_delay: {buffer_delay}")
        print(f"默认值应为5: {'✓' if buffer_delay == '5' else '✗'}")
        
        # 测试设置配置
        success = db.set_config("buffer_delay", "10")
        print(f"设置 buffer_delay=10: {'✓' if success else '✗'}")
        
        # 测试获取更新后的配置
        buffer_delay = db.get_config("buffer_delay", "5")
        print(f"获取更新后的 buffer_delay: {buffer_delay}")
        print(f"值应为10: {'✓' if buffer_delay == '10' else '✗'}")
        
        # 恢复默认值
        db.set_config("buffer_delay", "5")
        print("已恢复默认值 buffer_delay=5")
        
        # 测试获取所有配置
        all_configs = db.get_all_configs()
        print(f"所有配置项数量: {len(all_configs)}")
        if all_configs:
            print("配置项:")
            for key, value in all_configs.items():
                print(f"  {key}: {value}")
        
    except Exception as e:
        print(f"✗ 数据库配置测试失败: {e}")
        import traceback
        traceback.print_exc()

def test_api_config():
    """测试API配置功能"""
    print("\n=== 测试API配置功能 ===")
    
    try:
        from api.config import DEFAULT_CONFIG, _get_config, _save_config_to_db
        
        # 测试默认配置
        print(f"API默认配置: {DEFAULT_CONFIG}")
        print(f"buffer_delay默认值应为5: {'✓' if DEFAULT_CONFIG.get('buffer_delay') == 5 else '✗'}")
        
        # 测试获取配置
        config = _get_config()
        print(f"从API获取配置: {config}")
        print(f"buffer_delay值: {config.get('buffer_delay')}")
        
        # 测试保存配置
        new_config = config.copy()
        new_config["buffer_delay"] = 8
        success = _save_config_to_db(new_config)
        print(f"保存配置 buffer_delay=8: {'✓' if success else '✗'}")
        
        # 重新获取配置
        config = _get_config()
        print(f"重新获取配置 buffer_delay: {config.get('buffer_delay')}")
        print(f"值应为8: {'✓' if config.get('buffer_delay') == 8 else '✗'}")
        
        # 恢复默认值
        new_config["buffer_delay"] = 5
        _save_config_to_db(new_config)
        print("已恢复默认值 buffer_delay=5")
        
    except Exception as e:
        print(f"✗ API配置测试失败: {e}")
        import traceback
        traceback.print_exc()

def test_bot_config_usage():
    """测试bot如何使用配置"""
    print("\n=== 测试bot配置使用 ===")
    
    try:
        # 测试Discord bot配置获取
        print("测试Discord bot配置获取...")
        from bot.discord_bot import DiscordBot
        bot = DiscordBot()
        
        # 测试数据库导入
        from memory.database import get_database
        db = get_database()
        
        # 设置测试值
        db.set_config("buffer_delay", "7")
        
        # 模拟_process_buffer中的配置获取
        buffer_delay_str = db.get_config("buffer_delay", "5")
        buffer_delay = int(buffer_delay_str)
        print(f"Discord bot获取的buffer_delay: {buffer_delay}秒")
        print(f"值应为7: {'✓' if buffer_delay == 7 else '✗'}")
        
        # 测试Telegram bot配置获取
        print("\n测试Telegram bot配置获取...")
        from bot.telegram_bot import TelegramBot
        tbot = TelegramBot()
        
        # 模拟_process_buffer中的配置获取
        buffer_delay_str = db.get_config("buffer_delay", "5")
        buffer_delay = int(buffer_delay_str)
        print(f"Telegram bot获取的buffer_delay: {buffer_delay}秒")
        print(f"值应为7: {'✓' if buffer_delay == 7 else '✗'}")
        
        # 恢复默认值
        db.set_config("buffer_delay", "5")
        print("\n已恢复默认值 buffer_delay=5")
        
    except Exception as e:
        print(f"✗ bot配置测试失败: {e}")
        import traceback
        traceback.print_exc()

def test_config_range():
    """测试配置范围"""
    print("\n=== 测试配置范围 ===")
    
    try:
        # 直接读取前端配置文件内容
        config_file_path = os.path.join(os.path.dirname(__file__), "miniapp", "src", "pages", "Config.jsx")
        
        if os.path.exists(config_file_path):
            with open(config_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 查找 buffer_delay 配置
            import re
            pattern = r"\{\s*key:\s*'buffer_delay'.*?min:\s*(\d+).*?max:\s*(\d+)"
            match = re.search(pattern, content, re.DOTALL)
            
            if match:
                min_val = int(match.group(1))
                max_val = int(match.group(2))
                print(f"前端buffer_delay配置范围:")
                print(f"  最小值: {min_val}")
                print(f"  最大值: {max_val}")
                print(f"  范围应为3-100: {'✓' if min_val == 3 and max_val == 100 else '✗'}")
            else:
                print("✗ 未找到buffer_delay配置范围")
        else:
            print(f"✗ 配置文件不存在: {config_file_path}")
            
    except Exception as e:
        print(f"✗ 配置范围测试失败: {e}")
        import traceback
        traceback.print_exc()

def main():
    """主测试函数"""
    print("配置集成测试")
    print("=" * 50)
    
    test_database_config()
    test_api_config()
    test_bot_config_usage()
    test_config_range()
    
    print("\n" + "=" * 50)
    print("测试完成！")

if __name__ == "__main__":
    main()