#!/usr/bin/env python3
"""
检查程序中是否有存储总token量的参数。
"""

import sqlite3
import os

def check_database_tables():
    """检查数据库表结构"""
    db_path = os.path.join(os.path.dirname(__file__), "cedarstar.db")
    
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 获取所有表名
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print('数据库中的表:')
        for table in tables:
            print(f'  {table[0]}')
        
        # 检查是否有token使用统计相关的表
        token_tables = [t[0] for t in tables if 'token' in t[0].lower() or 'usage' in t[0].lower()]
        if token_tables:
            print(f'\n找到与token相关的表: {token_tables}')
        else:
            print('\n没有找到专门的token使用统计表')
        
        # 检查messages表是否有token相关字段
        cursor.execute("PRAGMA table_info(messages)")
        columns = cursor.fetchall()
        print('\nmessages表字段:')
        token_fields = []
        for col in columns:
            print(f'  {col[1]} ({col[2]})')
            if 'token' in col[1].lower():
                token_fields.append(col[1])
        
        if token_fields:
            print(f'\nmessages表中与token相关的字段: {token_fields}')
        else:
            print('\nmessages表中没有token相关字段')
        
        conn.close()
        
    except Exception as e:
        print(f"检查数据库时出错: {e}")

def check_config_for_token_stats():
    """检查配置中是否有token统计相关参数"""
    print('\n=== 检查配置中的token相关参数 ===')
    
    # 从代码中已知的token相关配置
    token_configs = [
        'LLM_MAX_TOKENS',  # LLM最大生成token数
        'SUMMARY_MAX_TOKENS',  # 摘要最大生成token数
    ]
    
    print('已知的token相关配置参数:')
    for config in token_configs:
        print(f'  {config}')
    
    print('\n注意: 这些是限制参数，不是统计参数')

def analyze_token_usage_implementation():
    """分析token使用统计的实现情况"""
    print('\n=== 分析token使用统计实现 ===')
    
    print('1. LLM接口返回的token使用信息:')
    print('   - LLMInterface类中的LLMResponse包含usage字段')
    print('   - usage字段包含input_tokens和output_tokens')
    print('   - 但这些信息没有被持久化存储')
    
    print('\n2. 当前实现的问题:')
    print('   - token使用信息只在API响应中返回')
    print('   - 没有保存到数据库')
    print('   - 没有累计统计功能')
    print('   - 无法查询历史token使用量')
    
    print('\n3. 建议的改进方案:')
    print('   - 在数据库中创建token_usage表')
    print('   - 每次LLM调用后保存token使用信息')
    print('   - 添加累计统计功能')
    print('   - 添加按用户/会话/日期的统计查询')

if __name__ == "__main__":
    print("开始检查程序中是否有存储总token量的参数...")
    check_database_tables()
    check_config_for_token_stats()
    analyze_token_usage_implementation()