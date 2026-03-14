#!/usr/bin/env python3
"""
测试 Python 系统路径
"""

import sys
import os

print("当前工作目录:", os.getcwd())
print("\nPython 系统路径:")
for i, path in enumerate(sys.path):
    print(f"{i}: {path}")

# 检查当前目录是否在 sys.path 中
current_dir = os.getcwd()
if current_dir in sys.path:
    print(f"\n✓ 当前目录 {current_dir} 在 sys.path 中")
else:
    print(f"\n✗ 当前目录 {current_dir} 不在 sys.path 中")
    print("尝试导入 config 模块...")
    try:
        import config
        print("✓ config 模块导入成功")
    except ImportError as e:
        print(f"✗ config 模块导入失败: {e}")