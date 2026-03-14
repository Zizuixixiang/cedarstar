#!/usr/bin/env python3
"""
测试 Python 环境是否正常工作
"""

print("Python 环境测试开始...")

# 测试基本导入
try:
    import os
    import sys
    print("✓ 基本导入成功")
except ImportError as e:
    print(f"✗ 基本导入失败: {e}")

# 测试配置导入
try:
    import config
    print("✓ config 模块导入成功")
    
    # 测试配置验证
    config.validate_config()
    print("✓ 配置验证成功")
except ImportError as e:
    print(f"✗ config 模块导入失败: {e}")
except Exception as e:
    print(f"✗ 配置验证失败: {e}")

print("Python 环境测试完成")