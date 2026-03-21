"""
插入一条带思维链的测试消息，用于测试前端展示效果
"""
import sqlite3
import os
from datetime import datetime

db_path = os.path.join(os.path.dirname(__file__), 'cedarstar.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# 用一个测试用的 session_id（模拟 Telegram 格式）
TEST_SESSION_ID = 'test_user_8624126581'

# 先插入一条用户提问
cur.execute("""
    INSERT INTO messages (role, platform, session_id, content, thinking, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
""", (
    'user',
    'telegram',
    TEST_SESSION_ID,
    '请帮我分析一下：为什么天空是蓝色的？',
    None,
    now
))

# 再插入一条带思维链的 assistant 回复
thinking_content = """好的，用户在问天空为什么是蓝色的。这是一个关于瑞利散射的经典物理问题。

让我理清楚思路：
1. 太阳光是由不同波长的可见光组成的白光
2. 当阳光进入地球大气层时，会与大气中的气体分子发生相互作用
3. 瑞利散射（Rayleigh Scattering）理论指出，散射强度与波长的4次方成反比
4. 蓝光波长（约450nm）比红光波长（约700nm）短得多
5. 因此蓝光被散射的程度远大于红光（大约是红光的5.5倍）
6. 从各个方向看天空，都能接收到被散射的蓝光，所以天空呈现蓝色

补充：为什么日落时天空是红色的？
- 日落时太阳角度低，阳光穿过大气层的路径更长
- 蓝光在长途穿越中几乎被散射殆尽
- 剩余的红光和橙光到达观察者眼睛
- 所以日落/日出时天空呈现红橙色

我觉得这个解释很完整，可以用通俗易懂的语言向用户解释。"""

answer_content = """天空呈现蓝色的原因是**瑞利散射（Rayleigh Scattering）**现象。

## 基本原理

太阳光（白光）是由红、橙、黄、绿、蓝、紫等不同波长的光组成的。当阳光进入地球大气层时，会与空气中的气体分子（主要是氮气和氧气）发生碰撞和散射。

关键规律是：**散射强度与光的波长的4次方成反比**，即波长越短，散射越强。

- 蓝光波长约 450 纳米
- 红光波长约 700 纳米
- 蓝光被散射的程度约是红光的 **5.5 倍**

## 为什么我们看到蓝色

从大气层的各个方向，被散射的蓝色光子飞向四面八方，当我们抬头看天空时，各个方向都有大量蓝光进入我们的眼睛，所以天空整体呈现蓝色。

## 小彩蛋：为什么日落是红色的？

日落时太阳位于地平线附近，阳光需要穿越更厚的大气层才能到达我们眼睛。在这段漫长的旅途中，蓝光早已被散射消耗殆尽，只剩下波长较长的红光和橙光，于是日落就变成了美丽的红橙色 🌅"""

cur.execute("""
    INSERT INTO messages (role, platform, session_id, content, thinking, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
""", (
    'assistant',
    'telegram',
    TEST_SESSION_ID,
    answer_content,
    thinking_content,
    now
))

conn.commit()
conn.close()

print(f"已插入测试数据（时间: {now}）")
print("- 一条用户提问（无思维链）")
print("- 一条助手回复（含思维链）")
