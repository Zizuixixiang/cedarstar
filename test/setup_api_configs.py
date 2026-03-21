"""
添加 api_configs 表并插入 token 测试数据
"""
import sqlite3
from datetime import datetime, timedelta
import random

DB_PATH = "d:/Workspace/PythonProject/cedarstar/cedarstar.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# 1. 查看现有表
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cursor.fetchall()]
print("现有表:", tables)

# 2. 创建 api_configs 表
cursor.execute("""
    CREATE TABLE IF NOT EXISTS api_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        api_key TEXT NOT NULL,
        base_url TEXT NOT NULL,
        model TEXT,
        persona_id INTEGER,
        is_active INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
print("api_configs 表创建/确认完成")

# 3. 插入 token 测试数据（今日、本周都有）
now = datetime.now()

test_data = []
platforms = ['telegram', 'discord']
models = ['deepseek-chat', 'deepseek-r1']

# 今日数据
for i in range(15):
    hours_ago = random.randint(0, 8)
    minutes_ago = random.randint(0, 59)
    ts = now - timedelta(hours=hours_ago, minutes=minutes_ago)
    platform = random.choice(platforms)
    prompt = random.randint(500, 3000)
    completion = random.randint(200, 1500)
    test_data.append((ts.strftime('%Y-%m-%d %H:%M:%S'), platform, prompt, completion, prompt+completion, random.choice(models)))

# 近7天历史数据
for day in range(1, 7):
    for i in range(random.randint(5, 12)):
        ts = now - timedelta(days=day, hours=random.randint(0,23), minutes=random.randint(0,59))
        platform = random.choice(platforms)
        prompt = random.randint(500, 3000)
        completion = random.randint(200, 1500)
        test_data.append((ts.strftime('%Y-%m-%d %H:%M:%S'), platform, prompt, completion, prompt+completion, random.choice(models)))

cursor.executemany("""
    INSERT INTO token_usage (created_at, platform, prompt_tokens, completion_tokens, total_tokens, model)
    VALUES (?, ?, ?, ?, ?, ?)
""", test_data)

print(f"插入 {len(test_data)} 条 token 测试数据完成")

conn.commit()

# 4. 验证
cursor.execute("SELECT platform, SUM(total_tokens), COUNT(*) FROM token_usage WHERE date(created_at) = date('now', 'localtime') GROUP BY platform")
rows = cursor.fetchall()
print("今日 token 统计:")
for r in rows:
    print(f"  {r[0]}: {r[1]} tokens, {r[2]} 次")

cursor.execute("SELECT COUNT(*) FROM api_configs")
print(f"api_configs 条数: {cursor.fetchone()[0]}")

conn.close()
print("完成!")
