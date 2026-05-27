# CedarStar

多平台 AI 伴侣系统，FastAPI 后端 + Telegram/Discord Bot + React Mini App。
仓库：https://github.com/Zizuixixiang/cedarstar

---

## 技术栈

- 后端：Python / FastAPI / PostgreSQL（asyncpg 连接池）/ ChromaDB
- Bot：python-telegram-bot（webhook 模式）/ discord.py（可选）
- LLM：OpenAI 兼容 API + Anthropic Claude（可配置）
- Embedding：SiliconFlow Qwen3-Embedding-8B（默认，1024 维，主记忆）/ 智谱 embedding-3（可通过 `EMBEDDING_PROVIDER=zhipu` 切换）/ 硅基流动 BAAI/bge-m3（表情包）
- 检索：ChromaDB + BM25（jieba）+ SiliconFlow / OpenAI 兼容 Rerank
- 前端：React 18 + Vite（无 UI 组件库）

---

## 目录结构

```
cedarstar/
├── main.py              # 主入口
├── config.py            # 全局配置（从 .env 读取）
├── api/                 # FastAPI REST API（/api/* 需 X-Cedarstar-Token）
├── bot/                 # Telegram / Discord Bot
├── llm/                 # LLM 接口封装
├── memory/              # 记忆系统核心（database / context_builder / micro_batch / daily_batch / vector_store）
├── tools/               # 工具函数（lutopia / weather / weibo / search / meme / prompts）
├── miniapp/             # React Mini App（挂载在 /app/*）
├── portal/              # React Portal（挂载在 /daily/*）
└── run_daily_batch.py   # 日终跑批独立入口（cron 调用）
```

---

## 架构约束（必须遵守）

**数据库访问**
- 所有 DB 操作必须通过 `memory/database.py`，禁止在其他模块直接访问 PostgreSQL
- 所有 DB 函数均为 `async def`，调用必须 `await`
- `save_message` 必须传 `thinking` 参数（与类方法签名保持一致）

**LLM 接口**
- 统一通过 `llm/llm_interface.py` 的 `LLMInterface` 调用
- 每次请求动态 `await LLMInterface.create()` 以支持热更新
- Token 落库用 `generate_with_context_and_tracking`，不用 `generate_with_context`

**记忆系统**
- 向量写入用 1024 维（SiliconFlow Qwen3-Embedding-8B / 智谱 embedding-3 均保持 1024 维）
- `meme_pack` Chroma 集合与主记忆集合完全隔离，不得混用
- Chroma doc_id 命名约定：`daily_{batch_date}`、`daily_{batch_date}_event_N`、`manual_{uuid}`
- 摘要/跑批 prompt 默认文本集中在 `memory/prompt_registry.py`；Mini App「人设 → 全局 Prompt」只写 `prompt_overrides` 覆盖值。运行时读取覆盖失败必须回退默认值，不能阻断微批摘要、日终小传或 Step 4。

**Telegram Bot**
- 入站走 webhook（`POST /webhook/telegram`），不使用 `start_polling`
- `/webhook/telegram` 不挂在 `/api` 下，不需要 `X-Cedarstar-Token`
- 助手落库用 `combined_raw`（不含引用前缀），LLM 上下文用 `combined_content`

**工具调用（OpenAI 兼容 tools）**
- `execution_log` 不记录 `get_weather`、`get_weibo_hot`、`web_search`、`web_fetch`、`get_ai_news`、`schedule_next_wakeup`
- 工具结果压缩统一走 `tools/lutopia.py` 的字数分支；`web_search` 返回 Tavily 原始拼接文本，由通用工具压缩层处理
- `schedule_next_wakeup` 是内置工具，无人设开关；需同时接入 `complete_with_lutopia_tool_loop`、Telegram 流式工具 schema、`append_tool_exchange_to_messages` 和 Anthropic `tool_use` 解析
- Lutopia 走 MCP（`mcp` Python 包 + SSE），不直接 HTTP 调论坛接口

**前端**
- Mini App 禁用 `window.confirm`（Telegram WebView 不支持）
- 禁用 `localStorage`（使用 React state）
- API 请求必须带 `X-Cedarstar-Token`，通过 `apiFetch()` 封装调用
- 人设页包含「角色人设 / 全局 Prompt」Tab；全局 Prompt 使用 `/api/prompts` 管理 `summary_background`、chunk/daily/event extraction 等运行时 prompt 覆盖。

**CedarClio 第二套实例**（`clio.cedarstar.org`，supervisord 端口 8001）
- 与 CedarStar **同一 PostgreSQL 服务进程**（常见 `localhost:5432`），但 **主库 database 不同**：Sirius 的 `DATABASE_URL` → `cedarstar_db`，Clio 的 `DATABASE_URL` → `cedarclio_db`（`messages`、`mcp_tools`、`persona_configs` 等各自一份，**不是同一个库**）。
- **群聊共享库**：两边 `SHARED_GROUP_DB_URL` 通常都指向 **`cedarclio_db`**（`shared_group_messages` 等）；`group_chat_state` 等仍在各自 **主库**。
- **ChromaDB**：常见同一 HTTP 端点（`CHROMADB_URL`），用 **`CHROMA_COLLECTION_NAME`** 区分集合（如 `cedarstar_memories` vs `cedarclio_v2`）。
- 其它行为用 **`APP_NAME`**、Bot Token、人设等环境变量解耦。Schema 迁移在各自主库执行（进程启动时 `memory/database.py` 的 `migrate_database_schema`）；改表后需 **cedarstar 与 cedarclio 各重启一次**（或分别在对应库跑迁移）。

---

## 开发规范

**Python**
- 数据库时间参数绑定前统一转为 Python 类型（`date.fromisoformat` 等），不传字符串给 asyncpg
- asyncpg 位置参数用 `$N`，不用 `?`
- 日志可恢复路径用 `WARNING`，硬故障用 `ERROR`
- 新增工具（`tools/` 下）：在 `tools/prompts.py` 注册 directive 和 OpenAI tools schema，在 `LLMInterface.complete_with_lutopia_tool_loop` 合并；若 Telegram 流式路径也要可见，还要同步 `bot/telegram_bot.py` 的 `_telegram_stream_thinking_and_reply_with_lutopia` schema 列表
- 游戏模式使用 `config.active_game_session_id` 切换轻量 context；相关 DDL 在 `migrate_database_schema` 中，改表后 CedarStar / CedarClio 两边都要重启或分别跑迁移

**前端**
- 纯 CSS，无 UI 组件库；图标用 `lucide-react`
- CSS 变量遵循 `global.css` 定义的 neo-brutalist 主题（`--page-bg`、`--surface`、`--shadow-solid` 等）
- 卡片角标用 `::before` + `translateY(-50%)` + `width: max-content`
- Mini App 游戏管理页为 `/game`，session/turn JSON 编辑只用 textarea + JSON.parse 校验，不引第三方 JSON 编辑器

---

## 常用入口

```bash
# 启动主服务
python main.py

# 手动触发日终跑批
python run_daily_batch.py
python run_daily_batch.py 2026-04-22   # 指定日期

# 查跑批状态
python -c "
import sys, asyncio; sys.path.insert(0, '.')
from memory.database import initialize_database, get_database
async def main():
    await initialize_database()
    rows = await get_database().execute_query(
        'SELECT batch_date, step1_status, step2_status, step3_status, step4_status, step5_status FROM daily_batch_log ORDER BY batch_date DESC LIMIT 3')
    [print(r) for r in rows]
asyncio.run(main())
"

# 构建 Mini App
cd miniapp && npm run build

# 构建 Portal
cd portal && npm run build
```

---

## 禁止事项

- 禁止绕过 `memory/database.py` 直接操作 PostgreSQL
- 禁止在 Chroma 主记忆集合写入非 1024 维向量
- 禁止在 Bot 消息存储路径省略 `thinking` 参数
- 禁止在前端使用 `window.confirm`、`localStorage`、`sessionStorage`
- 禁止修改 `/webhook/telegram` 路由前缀或为其添加 Token 鉴权
- 禁止在 Step 4 f-string prompt 中使用未转义的单独 `{}`（须写 `{{` / `}}`）
