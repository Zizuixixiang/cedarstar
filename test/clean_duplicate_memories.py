"""
清理 longterm_memories 表中的重复数据。
保留每组相同 content 中 id 最小的那条，删除其余重复条目。
"""
import sys
sys.path.insert(0, '.')
from memory.database import get_database

db = get_database()

# 查询所有数据
r = db.get_longterm_memories(page_size=1000)
items = r['items']
print(f'清理前共 {len(items)} 条记录')

# 按 content 分组，找出重复
from collections import defaultdict
groups = defaultdict(list)
for item in items:
    groups[item['content']].append(item['id'])

deleted = 0
for content, ids in groups.items():
    if len(ids) > 1:
        # 保留 id 最小的，删除其余
        ids_sorted = sorted(ids)
        keep_id = ids_sorted[0]
        for del_id in ids_sorted[1:]:
            db.delete_longterm_memory(del_id)
            print(f'  删除重复记录 id={del_id}, 保留 id={keep_id}, 内容: {content[:40]}')
            deleted += 1

print(f'\n共删除 {deleted} 条重复记录')
r2 = db.get_longterm_memories(page_size=1000)
print(f'清理后共 {r2["total_items"]} 条记录:')
for m in r2['items']:
    print(f'  id={m["id"]} | {m["content"][:60]}')
