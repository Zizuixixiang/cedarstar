"""触发数据库迁移并列出 sqlite_master 中的表名与索引名。"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from memory.database import get_database


def main() -> None:
    db = get_database()
    with sqlite3.connect(db.db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
        indexes = [r[0] for r in cur.fetchall()]
    print("表:", tables)
    print("索引:", indexes)


if __name__ == "__main__":
    main()
