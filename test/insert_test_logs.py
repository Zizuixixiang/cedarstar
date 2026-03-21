"""
插入测试日志数据到 logs 表
"""
import sqlite3
import datetime

db_path = 'd:/Workspace/PythonProject/cedarstar/cedarstar.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 查看 logs 表结构
cursor.execute("PRAGMA table_info(logs)")
cols = cursor.fetchall()
print('logs 表结构:')
for col in cols:
    print(' ', col)

# 插入测试数据
now = datetime.datetime.now()

test_logs = [
    {
        "level": "INFO",
        "platform": "telegram",
        "message": "用户 @alice 发送消息: 你好",
        "stack_trace": None,
        "created_at": (now - datetime.timedelta(minutes=5)).isoformat()
    },
    {
        "level": "INFO",
        "platform": "discord",
        "message": "用户 bob#1234 发送消息: hello",
        "stack_trace": None,
        "created_at": (now - datetime.timedelta(minutes=10)).isoformat()
    },
    {
        "level": "ERROR",
        "platform": "telegram",
        "message": "处理消息时发生错误: Connection timeout",
        "stack_trace": "Traceback (most recent call last):\n  File 'bot.py', line 42, in handler\n    response = await llm.chat(msg)\nTimeoutError: Connection timed out",
        "created_at": (now - datetime.timedelta(minutes=15)).isoformat()
    },
    {
        "level": "WARNING",
        "platform": "batch",
        "message": "每日跑批任务执行缓慢，耗时超过 30 秒",
        "stack_trace": None,
        "created_at": (now - datetime.timedelta(hours=1)).isoformat()
    },
    {
        "level": "ERROR",
        "platform": "discord",
        "message": "Discord API 调用失败: Rate limit exceeded",
        "stack_trace": "Traceback (most recent call last):\n  File 'discord_bot.py', line 88, in send\n    await channel.send(msg)\nHTTPException: 429 Too Many Requests",
        "created_at": (now - datetime.timedelta(hours=2)).isoformat()
    },
    {
        "level": "INFO",
        "platform": "batch",
        "message": "每日记忆跑批任务完成，处理了 15 条记录",
        "stack_trace": None,
        "created_at": (now - datetime.timedelta(hours=3)).isoformat()
    },
    {
        "level": "WARNING",
        "platform": "telegram",
        "message": "Token 使用量接近限制: 85%",
        "stack_trace": None,
        "created_at": (now - datetime.timedelta(hours=5)).isoformat()
    },
    {
        "level": "INFO",
        "platform": "telegram",
        "message": "用户 @carol 查询天气: 上海今天天气怎么样",
        "stack_trace": None,
        "created_at": (now - datetime.timedelta(hours=6)).isoformat()
    },
]

# 检测表的列名，动态插入
col_names = [col[1] for col in cols]
print('\n列名:', col_names)

inserted = 0
for log in test_logs:
    try:
        if 'stack_trace' in col_names:
            cursor.execute(
                "INSERT INTO logs (level, platform, message, stack_trace, created_at) VALUES (?, ?, ?, ?, ?)",
                (log['level'], log['platform'], log['message'], log['stack_trace'], log['created_at'])
            )
        else:
            cursor.execute(
                "INSERT INTO logs (level, platform, message, created_at) VALUES (?, ?, ?, ?)",
                (log['level'], log['platform'], log['message'], log['created_at'])
            )
        inserted += 1
    except Exception as e:
        print(f'插入失败: {e}')

conn.commit()
conn.close()
print(f'\n成功插入 {inserted} 条测试日志')
