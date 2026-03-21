import sqlite3
import os

def add_new_tables():
    """为 CedarStar 数据库添加新表"""
    db_path = 'cedarstar.db'
    
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("开始添加新表...")
        
        # 创建 persona_configs 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS persona_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sirius_persona TEXT,
                user_name TEXT,
                user_body TEXT,
                user_habits TEXT,
                user_likes_dislikes TEXT,
                user_values TEXT,
                user_hobbies TEXT,
                user_taboos TEXT,
                user_nsfw TEXT,
                user_other TEXT,
                system_rules TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✓ 创建 persona_configs 表")
        
        # 创建 api_configs 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT NOT NULL,
                base_url TEXT NOT NULL,
                persona_id INTEGER,
                is_active INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (persona_id) REFERENCES persona_configs(id)
            )
        """)
        print("✓ 创建 api_configs 表")
        
        # 创建 config 表（用于存储助手配置参数）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✓ 创建 config 表")
        
        # 检查 logs 和 token_usage 表是否存在（应该存在，但检查一下）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level TEXT NOT NULL,
                platform TEXT,
                message TEXT NOT NULL,
                stack_trace TEXT
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                platform TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                model TEXT
            )
        """)
        
        # 创建索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_persona_configs_name
            ON persona_configs (name)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_configs_persona
            ON api_configs (persona_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_config_key
            ON config (key)
        """)
        
        conn.commit()
        conn.close()
        
        print("新表添加完成！")
        return True
        
    except sqlite3.Error as e:
        print(f"添加新表失败: {e}")
        return False

if __name__ == "__main__":
    success = add_new_tables()
    if success:
        print("执行成功！")
    else:
        print("执行失败！")