-- persona_configs：rcommunity 论坛 MCP 人设开关（与 migrate_database_schema 中 ALTER 一致，可单独手工执行）
ALTER TABLE persona_configs
    ADD COLUMN IF NOT EXISTS enable_rcommunity INTEGER NOT NULL DEFAULT 0;
