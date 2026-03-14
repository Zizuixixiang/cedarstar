#!/usr/bin/env python3
"""
修复数据库表结构，添加 platform 字段。
"""

import sqlite3
import os

def fix_database():
    """修复数据库表结构"""
    db_path = os.path.join(os.path.dirname(__file__), "cedarstar.db")
    
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 检查 messages 表是否有 platform 字段
        cursor.execute('PRAGMA table_info(messages)')
        columns = [col[1] for col in cursor.fetchall()]
        print('messages 表字段:', columns)
        
        # 如果没有 platform 字段，添加它
        if 'platform' not in columns:
            print('添加 platform 字段到 messages 表...')
            try:
                cursor.execute("ALTER TABLE messages ADD COLUMN platform TEXT DEFAULT 'discord'")
                conn.commit()
                print('成功添加 platform 字段')
            except Exception as e:
                print(f'添加字段失败: {e}')
                return False
        else:
            print('platform 字段已存在')
        
        # 验证字段已添加
        cursor.execute('PRAGMA table_info(messages)')
        columns_after = [col[1] for col in cursor.fetchall()]
        print('修复后的 messages 表字段:', columns_after)
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"修复数据库时出错: {e}")
        return False

if __name__ == "__main__":
    print("开始修复数据库表结构...")
    if fix_database():
        print("数据库修复成功！")
    else:
        print("数据库修复失败！")