#!/usr/bin/env python3
"""
测试 context builder 功能。
"""

import sys
import os

# 添加项目根目录到 Python 路径
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from memory.context_builder import build_context
from memory.database import get_database

def test_context_builder():
    """测试 context builder 功能。"""
    print("开始测试 context builder...")
    
    try:
        # 获取数据库实例
        db = get_database()
        
        # 创建测试会话ID
        test_session = "test_session_123"
        
        # 清理测试数据
        print(f"清理测试数据: {test_session}")
        db.clear_session_messages(test_session)
        
        # 保存测试消息
        print("保存测试消息...")
        for i in range(3):
            db.save_message("user", f"测试用户消息 {i+1}", test_session)
            db.save_message("assistant", f"测试助手回复 {i+1}", test_session)
        
        # 测试构建 context
        print("构建 context...")
        context = build_context(test_session, "你好，这是一个测试消息")
        
        # 检查结果
        print(f"\nContext 构建成功！")
        print(f"System prompt 长度: {len(context['system_prompt'])}")
        print(f"Messages 数量: {len(context['messages'])}")
        
        # 显示消息结构
        print("\nMessages 结构:")
        for i, msg in enumerate(context['messages']):
            role = msg['role']
            content_preview = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
            print(f"  [{i}] {role}: {content_preview}")
        
        # 清理测试数据
        print(f"\n清理测试数据...")
        db.clear_session_messages(test_session)
        
        print("测试完成！")
        return True
        
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_context_builder()
    sys.exit(0 if success else 1)