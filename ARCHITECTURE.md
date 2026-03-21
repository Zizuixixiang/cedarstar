# CedarStar 项目架构文档

> 生成时间：2026-03-22
> 项目仓库：https://github.com/Zizuixixiang/cedarstar

---

## 目录

1. [项目概述](#1-项目概述)
2. [目录结构树](#2-目录结构树)
3. [各模块职责与边界](#3-各模块职责与边界)
4. [模块调用关系（数据流向）](#4-模块调用关系数据流向)
5. [数据库表结构概览](#5-数据库表结构概览)
6. [结构问题与改进建议](#6-结构问题与改进建议)

---

## 1. 项目概述

CedarStar 是一个具备**长期记忆能力**的 AI 聊天机器人系统，支持 Discord 和 Telegram 双平台接入。系统通过分层记忆架构（短期消息缓冲 → 微批摘要 → 日终小传 → 向量长期记忆）实现跨会话的持久化记忆，并提供一个 React 管理后台（Mini App）用于可视化管理。

**技术栈：**
- 后端：Python / FastAPI / SQLite / ChromaDB
- 机器人：discord.py / python-telegram-bot
- LLM：OpenAI 兼容 API / Anthropic Claude（可配置）
- Embedding：智谱 AI embedding-3（1024维）
- 检索：ChromaDB 向量检索 + BM25 关键词检索 + Cohere Rerank
- 前端：React + Vite（管理 Mini App）

---

## 2. 目录结构树

```
cedarstar/                          # 项目根目录
├── main.py                         # 主入口：校验配置 → 阻塞重建 BM25 索引 → 并行启动 Discord/TG Bot、日终跑批、FastAPI
├── config.py                       # 全局配置类（从 .env 读取），含 Platform 平台常量定义
├── requirements.txt                # Python 依赖清单
├── README.md                       # 项目简介、技术栈、简略目录与「规划中」模块说明
├── start_bot.py                    # 备用启动脚本（校验配置 → 阻塞重建 BM25 → 仅启动 Discord Bot）
├── .env                            # 环境变量配置文件（不入库）
├── cedarstar.db                    # SQLite 数据库文件（运行时生成）
├── cedarstar.log                   # 运行日志文件（运行时生成）
├── PROGRESS.md                     # 开发进度记录文档
│
├── api/                            # FastAPI REST API 层
│   ├── router.py                   # API 路由汇总，统一注册所有子路由
│   ├── dashboard.py                # 控制台概览接口（Bot 状态、记忆概览、批处理日志）
│   ├── persona.py                  # 人设配置 CRUD 接口
│   ├── memory.py                   # 记忆管理接口（记忆卡片 + 长期记忆：先 Chroma 后 SQLite 写入，列表含 is_orphan）
│   ├── history.py                  # 对话历史查询接口（支持平台/关键词/日期过滤+分页）
│   ├── logs.py                     # 系统日志查询接口（支持平台/级别/关键词过滤+分页）
│   ├── config.py                   # 助手运行参数配置接口；GET/PUT 成功时 data 含 _meta.updated_at（见 §5.7）
│   └── settings.py                 # API 配置管理接口（api_configs CRUD + Token 消耗统计）
│
├── bot/                            # 聊天机器人层
│   ├── __init__.py                 # 包初始化文件
│   ├── message_buffer.py           # 消息缓冲公共实现（buffer 字典 + 锁 + 定时器 + 合并后回调）
│   ├── reply_citations.py          # 解析 [[used:uid]]、异步 update_memory_hits、清洗回复后再存库/发送
│   ├── discord_bot.py              # Discord 机器人（组合 MessageBuffer、LLM、消息存储）
│   └── telegram_bot.py            # Telegram 机器人（组合 MessageBuffer、LLM、消息存储）
│
├── llm/                            # LLM 接口层
│   ├── __init__.py                 # 包初始化文件
│   └── llm_interface.py            # 统一 LLM 接口（支持 OpenAI 兼容 API 和 Anthropic Claude）
│
├── memory/                         # 记忆系统层（核心模块）
│   ├── __init__.py                 # 包初始化文件
│   ├── database.py                 # SQLite 数据库封装（MessageDatabase 类 + 全局单例 + 便捷函数）
│   ├── context_builder.py          # Context 组装器（system + 时效状态 + 记忆卡片 + 关系时间线 + 摘要 + 折叠/精排长期记忆 + 近期消息）
│   ├── micro_batch.py              # 微批处理（消息达阈值时异步生成 chunk 摘要）
│   ├── daily_batch.py              # 日终跑批（每天 23:00 五步：时效结算→小传→卡片/时间轴→向量+事件→Chroma GC）
│   ├── vector_store.py             # ChromaDB 向量存储封装（智谱 Embedding + 增删查）
│   ├── bm25_retriever.py           # BM25 关键词检索（jieba 分词 + rank_bm25，内存缓存索引）
│   ├── reranker.py                 # Cohere Rerank 重排器（异步，对双路检索结果重排序）
│   └── async_log_handler.py        # 异步日志处理器（将日志写入 SQLite logs 表）
│
├── services/                       # 外部服务集成层（待开发）
│   ├── __init__.py                 # 包初始化文件
│   └── wx_read.py                  # 微信读书服务（仅占位，尚未实现）
│
├── tools/                          # 工具函数层（待开发）
│   ├── __init__.py                 # 包初始化文件
│   ├── weather.py                  # 天气查询工具（仅占位，尚未实现）
│   └── location.py                 # 位置工具（仅占位，尚未实现）
│
├── miniapp/                        # 前端管理 Mini App（React + Vite）
│   ├── index.html                  # HTML 入口文件
│   ├── package.json                # Node.js 依赖配置
│   ├── vite.config.js              # Vite 构建配置（代理 /api 到 localhost:8000）
│   └── src/
│       ├── main.jsx                # React 应用入口，挂载根组件
│       ├── App.jsx                 # 根组件（侧边栏导航 + 路由出口）
│       ├── router.jsx              # 路由配置（7 个页面；显式 import React）
│       ├── pages/                  # 页面组件
│       │   ├── Dashboard.jsx       # 控制台概览页（status / memory-overview / batch-log，顶栏与日历、记忆 KPI）
│       │   ├── Persona.jsx         # 人设配置页（角色/用户信息 CRUD）
│       │   ├── Memory.jsx          # 记忆管理页（四 Tab；固定视口内高度 + 内容区独立滚动，见 §3.6）
│       │   ├── History.jsx         # 对话历史页（聊天气泡布局 + 筛选，见 §3.6）
│       │   ├── Logs.jsx            # 系统日志页（固定高度布局，支持过滤）
│       │   ├── Config.jsx          # 助手配置页（运行参数调整，优化了移动端触控体验）
│       │   └── Settings.jsx        # 核心设置页（API 配置管理 + Token 统计）
│       └── styles/                 # CSS 样式文件（每个页面对应一个 CSS 文件）
│           ├── global.css          # 全局样式
│           ├── sidebar.css         # 侧边栏样式
│           ├── dashboard.css       # 控制台样式
│           ├── persona.css         # 人设页样式
│           ├── memory.css          # 记忆页样式
│           ├── history.css         # 历史页样式
│           ├── logs.css            # 日志页样式
│           ├── config.css          # 配置页样式
│           └── settings.css        # 设置页样式
│
├── chroma_db/                      # ChromaDB 本地持久化数据目录（运行时生成）
│   └── c0d21c8d-.../               # ChromaDB 内部集合数据文件
│
├── backups/                        # 数据库备份文件目录
│   ├── cedarstar.db.backup         # 数据库备份
│   ├── cedarstar.db.backup2        # 数据库备份2
│   └── cedarstar.db.backup_dimension # 维度字段变更前的备份
│
├── test/                           # 测试目录（当前为空）
│
├── add_new_tables.py               # 数据库迁移脚本：新增表结构
├── fix_database.py                 # 数据库修复脚本：修复字段/结构问题
├── clean_duplicate_memories.py     # 清理重复记忆数据的工具脚本
├── setup_api_configs.py            # 初始化 API 配置的工具脚本
├── insert_test_logs.py             # 插入测试日志数据的脚本
├── insert_test_thinking.py         # 插入测试思维链数据的脚本
├── demo_message_buffer.py          # 消息缓冲逻辑演示脚本
└── test_config_integration.py      # 配置集成测试脚本
```

---

## 3. 各模块职责与边界

### 3.1 `config.py` — 全局配置中心

**职责：** 统一管理所有环境变量的读取，提供类型安全的配置属性。

**边界：**
- 只读取 `.env` 文件，不写入任何状态
- 提供 `Platform` 常量类，规范 platform 字段的字符串值（`discord` / `telegram` / `batch` / `system` / `rikkahub`）
- 提供 `validate_config()` 函数在启动时做必要性检查

**关键配置项：**

| 配置项 | 说明 |
|--------|------|
| `DISCORD_BOT_TOKEN` | Discord 机器人令牌（必填） |
| `TELEGRAM_BOT_TOKEN` | Telegram 机器人令牌（可选） |
| `LLM_API_KEY / LLM_API_BASE / LLM_MODEL_NAME` | 主 LLM 配置 |
| `SUMMARY_API_KEY / SUMMARY_API_BASE / SUMMARY_MODEL_NAME` | 摘要专用 LLM 配置 |
| `ZHIPU_API_KEY` | 智谱 Embedding API 密钥 |
| `COHERE_API_KEY` | Cohere Rerank API 密钥 |
| `DATABASE_URL` | SQLite 数据库路径 |
| `CHROMADB_PERSIST_DIR` | ChromaDB 本地存储目录 |
| `MICRO_BATCH_THRESHOLD` | 微批处理触发阈值（默认 50 条） |
| `MESSAGE_BUFFER_DELAY` | 消息缓冲等待时间（默认 5 秒） |
| `CONTEXT_MAX_RECENT_MESSAGES` | Context 中最近消息数（默认 40 条） |
| `CONTEXT_MAX_DAILY_SUMMARIES` | Context 中每日摘要数（默认 5 条） |

---

### 3.2 `bot/` — 聊天机器人层

**职责：** 接收来自 Discord / Telegram 的用户消息，经过消息缓冲后调用 LLM 生成回复，并将消息和回复持久化到数据库。

**边界：**
- 不直接操作 LLM 参数，每次请求动态创建 `LLMInterface` 实例（支持热更新）
- 写入 `messages.character_id` 时使用**同一次请求**创建的 `LLMInterface.character_id`（来自当前激活的 `chat` 类型 `api_configs.persona_id`，无则 `"sirius"`），不额外查库
- 不直接操作数据库，通过 `memory.database` 的便捷函数存储消息
- 不构建 prompt，通过 `memory.context_builder.build_context()` 获取完整上下文
- 消息缓冲（`message_buffers` / `buffer_locks` / `buffer_timers`、`add_to_buffer`、读 `buffer_delay` 后合并）由 **`bot/message_buffer.py`** 的 `MessageBuffer` 统一实现；两 bot 仅实现 **flush 回调**（平台相关的 typing、生成、分片发送与入缓冲时的条目结构）
- 助手原始回复若含 `[[used:uid]]`（可多个），由 **`bot/reply_citations.py`** 在存库与发送前：用正则 `\[\[used:(.*?)\]\]` 收集 uid 去重；若非空则 `asyncio.create_task` + `asyncio.to_thread` 异步调用 `memory.vector_store.update_memory_hits`（不阻塞）；再用 `re.sub(r'\[\[used:.*?\]\]', '', reply)` 清洗正文，**仅清洗后的文本**写入 `messages` 与发往平台

**消息缓冲机制：**
```
用户发消息 → MessageBuffer.add_to_buffer() → 启动/重置 N 秒定时器
                                    ↓（超时）
                          合并 buffer 中所有消息 → 调用各 bot 的 flush 回调
                                    ↓
                          build_context() → LLM → 引用解析/hits/清洗 → 保存 → 触发微批检查
```

**Discord Bot 特有：**
- 仅响应 `@mention` 或私聊消息
- 支持 `!ping` / `!clear` / `!model` / `!help` 命令
- 消息长度限制 2000 字符（自动分割）

**Telegram Bot 特有：**
- 响应所有文本消息
- 支持 `/start` / `/help` / `/model` / `/clear` 命令
- 消息长度限制 4096 字符（自动分割）
- session_id 格式：`telegram_{chat_id}`（Discord 为 `{user_id}_{channel_id}`）

---

### 3.3 `llm/` — LLM 接口层

**职责：** 封装对 AI API 的 HTTP 调用，提供统一接口，屏蔽 OpenAI 和 Anthropic 的 API 差异。

**边界：**
- 优先从数据库 `api_configs` 表读取激活配置，回退到 `.env` 环境变量；激活行中的 `persona_id` 在构造时解析为实例属性 `character_id`（字符串，与 Bot 存消息共用，无则 `"sirius"`）
- 支持 `config_type='chat'` 和 `config_type='summary'` 两种配置类型
- 不维护对话历史状态（无状态）
- 支持思维链内容提取（DeepSeek R1 的 `reasoning_content`、Gemini 的 `thinking`）
- 异步记录 Token 使用量到 `token_usage` 表

**主要方法：**

| 方法 | 说明 |
|------|------|
| `generate(prompt, system_prompt, history)` | 基础生成，返回 `LLMResponse` |
| `generate_simple(prompt)` | 简化版，只返回文本 |
| `generate_with_context(messages)` | 接收完整 messages 数组（context builder 输出格式） |
| `generate_with_thinking(...)` | 生成并提取思维链内容 |
| `chat(message, history)` | 维护历史的聊天接口 |

**实例属性：**

| 属性 | 说明 |
|------|------|
| `character_id` | 由本次构造时读到的激活 `api_configs.persona_id` 转成字符串；无激活配置或 `persona_id` 为空时恒为 `"sirius"` |

---

### 3.4 `memory/` — 记忆系统层（核心）

这是整个项目最复杂的模块，实现了分层记忆架构。

#### 3.4.1 `database.py` — 数据持久化

**职责：** 封装所有 SQLite 操作，提供单例 `MessageDatabase` 实例和模块级便捷函数。

**边界：**
- 所有数据库操作都通过此模块，其他模块不直接操作 SQLite
- 提供 `get_database()` 单例工厂函数
- 管理 12 张核心数据表（及日志/统计等表）的 CRUD 操作；启动时由 `migrate_database_schema()` 幂等补齐列与索引（每次初始化成功执行后，`memory.database` 打 **INFO** 日志：`数据库 schema 迁移（索引/列）已执行`）
- Context 只读：`get_all_active_temporal_states()`（`temporal_states.is_active=1` 全量）、`get_recent_relationship_timeline(limit)`（数据库按 `created_at` 倒序取前 `limit` 条；`context_builder` 注入前对关系时间线再按 `created_at` 正序排列）
- 记忆卡片：`get_memory_cards()` 仅返回 `is_active=1`（供 API / Context）；日终 Step 3 Upsert 使用 `get_latest_memory_card_for_dimension()`，按 `user_id` + `character_id` + `dimension` 取**最近一条且不过滤 `is_active`**，避免批量软删后无法命中旧行；`update_memory_card(..., reactivate=True)` 在更新正文同时将 `is_active` 置 1（跑批合并写回后重新展示）

#### 3.4.2 `context_builder.py` — Context 组装

**职责：** 在每次 LLM 调用前，将多个记忆来源组装成完整的 `messages` 数组。

**组装顺序（优先级从高到低）：**
1. `system_prompt`（来自 `.env` 的 `SYSTEM_PROMPT`）
2. `temporal_states`（`temporal_states` 表中 `is_active=1` 的全部记录，置于记忆卡片之前）
3. `memory_cards`（`memory_cards` 表中 `is_active=1` 的记录，按维度分组）
4. `relationship_timeline`（数据库倒序取最近 3 条，注入 Context 前按 `created_at` 正序排列；紧接记忆卡片之后）
5. `daily_summaries`（最近 5 条 `summary_type='daily'` 的摘要，正序）
6. `chunk_summaries`（今日所有 `summary_type='chunk'` 的摘要，正序）
7. 长期记忆检索（ChromaDB top5 + BM25 top5，按 `doc_id` 去重后最多 10 条；**进入精排前**按 Chroma `metadata.parent_id` 做父子折叠——同一父文档（当日 `daily_*`）与下属 `*_event_*` 片段为一组，组内仅保留语义相似度最高的一条；注入 prompt 时每条正文前带 `[uid:<chroma_doc_id>]` 前缀，与回复末尾引用 `[[used:uid]]` 中的 `uid` 一致）
8. 最近消息（当前 session 中 `is_summarized=0` 的最新 40 条，正序）
9. 当前用户消息

**精排（仅异步路径）：** 并行双路检索并折叠后，对剩余候选调用 Cohere 得到语义相关分；对每条再算时间衰减复活分 `base_score × exp(-ln(2)/halflife_days × age_days) × (1 + 0.35×ln(1+hits))`（`age_days` 优先由 metadata `created_at` 推算，否则由 `last_access_ts`）；两路分数各自在当批候选内 min-max 归一化后按 **0.8×语义 + 0.2×衰减** 综合得分排序，取 top 2 写入 context。

**边界：**
- 同步版 `build_context()`：双路检索 + 父子折叠，无 Cohere；长期记忆块标题为「双路检索结果」
- 异步版 `build_context_async()`：并行检索 + 折叠 + Cohere 全候选打分 + 上述融合公式取 top2；`COHERE_API_KEY` 不可用时回退为同步双路逻辑
- System 块末尾固定追加引用死命令：若参考了上述历史记忆，须在回复文末标注 `[[used:uid]]`（可多个）

#### 3.4.3 `micro_batch.py` — 微批处理

**职责：** 每次消息写入后异步检查，当 session 中 `is_summarized=0` 的消息达到阈值（默认 50 条）时，触发摘要生成。

**流程：**
```
消息写入 → trigger_micro_batch_check(session_id)
              ↓（达到阈值）
         取出最早的 50 条未摘要消息
              ↓
         调用 SUMMARY LLM 生成 chunk 摘要
              ↓
         写入 summaries 表（summary_type='chunk'）
              ↓
         批量标记消息 is_summarized=1
```

#### 3.4.4 `daily_batch.py` — 日终跑批

**职责：** 每天 23:00（Asia/Shanghai）执行五步流水线，支持断点续跑。

**五步流水线：**

| 步骤 | 说明 |
|------|------|
| Step 1 | 巡视 `temporal_states`：`expire_at` 已到期且 `is_active=1` 的记录先 `UPDATE is_active=0`，再用 SUMMARY LLM 将 `state_content` 从「进行时」改写为过去时客观事实，结果列表供 Step 2 使用 |
| Step 2 | 将 Step 1 输出附在 prompt 开头，合并今日 chunk 摘要，调用 SUMMARY LLM 生成今日小传（`summary_type='daily'`） |
| Step 3 | 记忆卡片 Upsert：无对应维度则 `INSERT`；**有则调用模型合并去重后 `UPDATE`，合并失败时 fallback 为追加写入**；结束时再调 SUMMARY LLM 判断是否写入 `relationship_timeline`（含 Step 1 结算的时效事件），有则 `INSERT` |
| Step 4 | 主 LLM 打分（1-10）；**全量**向量化入库（不再按分数跳过）。`halflife_days`：8–10→60，4–7→30，1–3→7。先存 `daily_{batch_date}`，再按需拆分事件片段 `daily_{batch_date}_event_N`，metadata 含 `parent_id` 指向当日主文档；增量更新 BM25 |
| Step 5 | Chroma GC：`vector_store.garbage_collect_stale_memories()` — 仅当 `last_access_ts` 距今 ≥90 天、半衰期衰减得分 \<0.05、且无子文档以该 `doc_id` 为 `parent_id` 时物理删除 |

**Step 3 实现要点（与代码一致）：**
- **维度 JSON：** 对 SUMMARY LLM 返回依次尝试整段 `json.loads`；失败则截取**首个平衡的 JSON 对象**（跳过前置说明、处理字符串内转义；支持 \`\`\`json 代码块）；再回退原贪婪 `\{...\}` 正则。
- **Upsert 行定位：** `get_latest_memory_card_for_dimension()`（不过滤 `is_active`），保证「全表 `is_active=0` 后重跑」仍更新同一逻辑行，而非误当作无记录而堆叠 `INSERT`。
- **合并写回：** `_merge_memory_card_contents` 使用摘要模型配置的 `LLMInterface.generate_simple`（不经 `micro_batch` 的对话摘要模板）；合并失败则 fallback 为「旧正文 + `[batch_date]更新` + 新摘要」式追加。`update_memory_card(..., dimension=None, reactivate=True)` 写库并**重新激活**该卡。

**断点续跑：** `daily_batch_log` 记录 `step1_status`～`step5_status`，重启后跳过已完成步骤。

**定时触发（`schedule_daily_batch`）：** 每次到点先将 `batch_date` 早于「含今日共 7 天」窗口且仍有未完成步骤的行标记为 `error_message='expired, skipped'`、五步均置 1；再对窗口内未完成日期按 `batch_date` 升序串行调用 `run_daily_batch(该日)`；若当日未出现在补跑列表中，最后再 `run_daily_batch()` 执行今天。

#### 3.4.5 `vector_store.py` — 向量存储

**职责：** 封装 ChromaDB 操作，使用智谱 AI `embedding-3` 模型生成向量；**工程约定为 1024 维**（与占位零向量、检索逻辑一致）。

**边界：**
- 日终由 `daily_batch` 全量写入当日小传（及可选事件片段）；手工长期记忆仍通过 Mini App 写入
- 提供 `add_memory()` / `search_memory()` / `delete_memory()` / `update_memory_hits()` 便捷函数
- 集合名称固定为 `cedarstar_memories`
- **智谱 API 与维度：** `embedding-3` 在 HTTP 请求体中**若不传 `dimensions`，默认返回 2048 维**。`vector_store.ZhipuEmbedding` **必须**在调用 `/embeddings` 时显式传入 **`dimensions: 1024`**，否则首次 `collection.add` 会把 Chroma 集合固定为 2048，而查询与其它路径仍按 1024 维构造向量，会出现 `Collection expecting embedding with dimension of 2048, got 1024`（或同类维度不匹配），`get_all_memories`、BM25 `refresh_index` 也会异常。
- **旧库 / 误建成 2048 的集合：** 若本地 `chroma_db` 已按错误维度写入，**处理（推荐）：先停止占用 Chroma 的进程**，备份后**删除** `chroma_db` 目录，确保代码已带 `dimensions: 1024` 后再启动并重新跑批写入。旧架构向量与当前 metadata / 双轨约定不一致时，重建通常比就地迁移更干净；SQLite `longterm_memories` 中历史行可能变为 Chroma 侧「孤儿」，由 Mini App `is_orphan` 提示，可按需清理或重新录入。
- **写入 metadata（Chroma）：** 在 `date` / `session_id` / `summary_type` 等调用方字段之外，`add_memory()` 会统一写入 `base_score`（float，可由调用方传入或从旧字段 `score` 推导，默认 5.0）、`halflife_days`（int，默认 30）、`hits`（int，新文档恒为 0）、`last_access_ts`（float，当前 Unix 时间戳），并保留 `created_at`（ISO 字符串）
- **doc_id 约定：** 日终主文档为 `daily_{batch_date}`（`build_daily_summary_doc_id`）；同一日多条事件片段为 `daily_{batch_date}_event_0`、`daily_{batch_date}_event_1`…（`build_daily_event_doc_id`）；Mini App 手工长期记忆仍为 `manual_{uuid}`
- **`update_memory_hits(uid_list)`：** 仅按 `doc_id` 列表 `get` 再 `update`，逐条 `hits+1` 并刷新 `last_access_ts`，不用 metadata `where` 查询
- **`garbage_collect_stale_memories()`：** 日终 Step 5 调用；衰减公式 `(base_score/10) * 0.5^(idle_days/halflife_days)`，与 90 天未访问、无 `parent_id` 子文档等条件组合后再 `delete`

#### 3.4.6 `bm25_retriever.py` — BM25 检索

**职责：** 基于 jieba 分词 + rank_bm25 实现关键词检索，数据来源是 ChromaDB 中的全量文档。

**边界：**
- 内存缓存索引：首次 `get_bm25_retriever()` 时 `BM25Retriever.__init__` 会从 ChromaDB 建索引；ChromaDB 为空或连接失败时优雅降级为空索引，不阻断导入
- **`main.py` / `start_bot.py`：** `validate_config()` 之后、启动 Bot **之前**，同步阻塞调用 `get_bm25_retriever().refresh_index()`，与 Chroma 全量再对齐一次；无文档时为空索引不报错；`main.py` 若返回 `False` 仅记录告警；`start_bot.py` 打印提示后仍启动
- 提供 `refresh_index()` 供上述启动步骤及手动全量重建
- 提供 `add_document_to_bm25()` 用于增量更新（日终归档时调用）

#### 3.4.7 `reranker.py` — Reranker 重排

**职责：** 使用 Cohere `rerank-multilingual-v3.0` 模型对（已父子折叠后的）候选文档打分，供 `context_builder` 与半衰期衰减分融合后排序。

**边界：**
- 纯异步实现（`cohere.AsyncClient`）
- 在 `build_context_async()` 中对折叠后候选调用，`top_n` 可为全量候选数以取齐语义分
- Cohere API 不可用时由 `context_builder` 回退到同步双路路径；失败时 `rerank()` 也可能返回无前缀分数的原始前 N 条，融合逻辑仍以检索 `score` 充当语义项

#### 3.4.8 `async_log_handler.py` — 异步日志处理

**职责：** 将 Python logging 的日志异步写入 SQLite `logs` 表，供 Mini App 查询。

---

### 3.5 `api/` — REST API 层

**职责：** 提供 FastAPI REST 接口，供前端 Mini App 调用。所有接口统一返回 `{success, data, message}` 格式。

**主要模块：**
- `config.py`：助手运行参数配置接口。`GET /api/config/config` 和 `PUT /api/config/config` 成功时，`data` 字段会包含 `_meta.updated_at`，用于前端展示配置的真实落库时间（UTC 时间，前端负责转为本地时区）。

**路由前缀映射：**

| 前缀 | 模块 | 主要功能 |
|------|------|----------|
| `/api/dashboard` | `dashboard.py` | Bot 在线状态、记忆概览、批处理日志 |
| `/api/persona` | `persona.py` | 人设配置 CRUD + system prompt 预览 |
| `/api/memory` | `memory.py` | 记忆卡片 CRUD + 长期记忆 + `temporal-states` / `relationship-timeline`（长期记忆列表合并 Chroma 元数据，见下） |
| `/api/history` | `history.py` | 对话历史查询（过滤+分页） |
| `/api/logs` | `logs.py` | 系统日志查询（过滤+分页） |
| `/api/config` | `config.py` | 运行参数读写（buffer_delay、chunk_threshold 等） |
| `/api/settings` | `settings.py` | API 配置 CRUD + 激活切换 + Token 统计 |

**边界：**
- API 层不包含业务逻辑，直接调用 `memory.database` 的方法
- `dashboard.py` 维护一个进程内共享的 `_bot_status` 字典，由 bot 的 `on_ready`/`on_disconnect` 事件写入
- **`GET /api/dashboard/status` 的模型信息：** `active_api_config` / `model_name` 来自 `get_active_api_config('chat')`，与 Settings「对话 API」Tab 的激活项及 Bot 对话路径一致（不包含摘要 API）
- `settings.py` 的 API Key 在返回时脱敏（只显示末4位）
- `memory.py` 手工长期记忆：`POST /longterm` 先写 ChromaDB（`doc_id` 形如 `manual_{uuid}`），成功后再写 SQLite；`DELETE /longterm/{id}` 先删 SQLite 再删 ChromaDB，Chroma 步骤失败仅记日志、接口仍返回成功；`GET /longterm` 在每条记录上附加 `is_orphan`（`chroma_doc_id` 缺失时为 `true`，非数据库列），并按 `chroma_doc_id` 批量读取 Chroma 元数据附加 `hits`、`halflife_days`、`last_access_ts`（孤儿行三项为 `null`）
- `memory.py` 时效状态：`GET/POST /temporal-states`、`DELETE /temporal-states/{id}`（将 `is_active` 置 0）；`GET /relationship-timeline` 返回全表按 `created_at` 倒序（只读）

---

### 3.6 `miniapp/` — 前端管理界面

**职责：** 提供可视化管理界面，通过 REST API 与后端交互。

**技术：** React 18 + React Router + Vite，无 UI 组件库（纯 CSS）

**页面与对应 API：**

| 页面 | 路径 | 对应后端 API |
|------|------|-------------|
| Dashboard（控制台概览） | `/` | `/api/dashboard/status` `/api/dashboard/memory-overview` `/api/dashboard/batch-log` |
| Persona（人设配置） | `/persona` | `/api/persona` |
| Memory（记忆管理） | `/memory` | `/api/memory/cards` `/api/memory/longterm` `/api/memory/temporal-states` `/api/memory/relationship-timeline` |
| History（对话历史） | `/history` | `/api/history` |
| Logs（系统日志） | `/logs` | `/api/logs` |
| Config（助手配置） | `/config` | `/api/config/config` |
| Settings（核心设置） | `/settings` | `/api/settings/api-configs` `/api/settings/token-usage` |

**Dashboard 页（`Dashboard.jsx` / `dashboard.css`）：** 挂载时并发请求 §3.5 三个控制台接口。顶栏为 Discord/Telegram 在线、**对话**侧激活配置名与模型（`/status`，与 `get_active_api_config('chat')` 一致）、批处理结论（由同页已拉取的 `/batch-log` 最近一条的 `step1_status`～`step5_status` 推导）。下方为跑批日历与记忆库概览；概览数据来自 `/memory-overview`，含 `chromadb_count`、`longterm_score_threshold`、`short_term_limit`、`chunk_summary_count`（今日微批摘要条数）、`dimension_status`（七维度圆点）、`latest_daily_summary_time` 等，具体字段以 `api/dashboard.py` 为准。样式层含核心 KPI 大字、今日日历高亮、维度 Tooltip 等（纯前端，不改变接口）。

**Settings 页（`Settings.jsx` / `settings.css`）：** 「对话 API」与「摘要 API」为两个 Tab，列表分别请求 `GET /api/settings/api-configs?config_type=chat` 与 `?config_type=summary`，切换 Tab 时重新拉取。新增/编辑弹窗内可改 `config_type`；**保存成功后以表单中的类型为准**——若与当前 Tab 不一致则自动切换到对应 Tab 并加载列表，若一致则仅刷新当前 Tab，避免在对话 Tab 下创建摘要配置后摘要列表仍为空。Tab 切换条样式与 Token 统计周期 Tab 同系的间距/分组（见 `settings.css` 中 `.config-tabs` / `.config-tab`）。

**Persona 页（`Persona.jsx` / `persona.css`）：** 右侧 System Prompt 预览区使用 `position: sticky`（配合 `align-self: flex-start`、`max-height` 与预览正文区域内部滚动），主内容区纵向滚动时预览与「复制全文」仍留在视口内，便于对照长表单编辑。

**Memory 页（`Memory.jsx` / `memory.css`）：** 四 Tab（记忆卡片、长期记忆、时效状态、关系时间线）。**外壳**：`.memory-container` 为 `height: calc(100vh - 120px)`（可按主内容区 padding 微调）、`overflow: hidden`；Tab 栏下方 **`.memory-content-scroll-area`** 为 `flex: 1; min-height: 0; overflow-y: auto; scrollbar-gutter: stable`，**仅该区域纵向滚动**，避免整页高度随 Tab 切换跳变。各 Tab 根为 Fragment，**首子节点**统一 **`.memory-tab-header`**（`margin-top: 24px` 与 Tab 栏留白一致），标题为 **`h2.memory-tab-header__title`**，emoji 与正文分置于 **`span.memory-tab-header__emoji` / `span.memory-tab-header__title-text`**。长期记忆条目中 Chroma 元数据（hits、halflife 等）用 **`.memory-meta-chip`** 展示；顶部 Tab 使用 **`.memory-tabs button.memory-tab`** 暖橙选中样式。均为前端布局/样式，**接口与数据字段不变**。

**History 页（`History.jsx` / `history.css`）：** 筛选区 **`.filter-controls-row`** 全宽；平台 **`.platform-tabs`** 可横向滚动，**`.tab-button`** 不换行。列表卡片 **`.message-list-container`** 水平 **`padding: 24px 10px`** 使对话区贴近卡片左右约 10px；内层 **`.history-chat-column`**（`max-width: 480px`）**`padding-left/right: 0`**，**`.message-list`** 同样无额外左右 padding。消息气泡 **`width: fit-content`**、**`max-width: 70%`**（与窄屏一致），随内容长短伸缩；**`.message-row.user-row`** **`justify-content: flex-end`** 用户气泡贴右，**`.message-row.assistant-row`** **`flex-start`** 助手贴左；内层避免 **`width: 100%`** 撑满行宽导致「中间一条」。气泡内正文统一左对齐，头部分角色对齐。**不改变** `/api/history` 参数与响应消费方式。

**开发代理：** Vite 将 `/api` 请求代理到 `http://localhost:8000`

**路由入口：** `src/router.jsx` 导出 `navItems` 与 `routes`，文件顶部 `import React from 'react'`（见 §6.11）。

---

### 3.7 `services/` 和 `tools/` — 扩展层（待开发）

- `services/wx_read.py`：微信读书集成（仅有版本号占位，无实现）
- `tools/weather.py`：天气查询工具（仅有版本号占位，无实现）
- `tools/location.py`：位置工具（仅有版本号占位，无实现）

以上三项在根目录 **`README.md`** 中已标注为「规划中，暂未实现」。

---

## 4. 模块调用关系（数据流向）

### 4.1 消息处理主流程

```
用户消息（Discord/Telegram）
        │
        ▼
  bot/discord_bot.py / bot/telegram_bot.py
        │
        ▼
  bot/message_buffer.py（MessageBuffer：Lock + timer + 合并）
  ┌─────────────────────────────────────────────────────┐
  │  等待 buffer_delay 秒，合并同 session 的多条消息      │
  │  再回调各 bot 的 flush（typing / 发送等平台逻辑）      │
  └─────────────────────────────────────────────────────┘
        │
        ▼
  memory/context_builder.py ◄── memory/database.py（读取记忆卡片、摘要、近期消息）
        │                   ◄── memory/vector_store.py（向量检索 top5）
        │                   ◄── memory/bm25_retriever.py（BM25 检索 top5）
        │                   ◄── memory/reranker.py（可选，Reranker 重排 top2）
        │
        ▼（返回完整 messages 数组）
  llm/llm_interface.py ──► 外部 LLM API（OpenAI/Claude/其他）
        │
        ▼（返回回复文本）
  memory/database.py（保存用户消息 + AI 回复到 messages 表，character_id = 同次 LLMInterface.character_id）
        │
        ▼（异步触发）
  memory/micro_batch.py（检查是否达到摘要阈值）
        │（达到阈值时）
        ▼
  llm/llm_interface.py（SUMMARY 配置）──► 外部 LLM API
        │
        ▼
  memory/database.py（写入 summaries 表，标记消息 is_summarized=1）
```

### 4.2 日终跑批流程

```
asyncio 定时器（每天 23:00 Asia/Shanghai）
        │
        ▼
  memory/daily_batch.py
  ┌─────────────────────────────────────────────────────┐
  │  Step 1：temporal_states 到期结算 + 过去时改写       │
  │    memory/database.py（查询/停用 temporal_states）    │
  │    SummaryLLMInterface                                │
  ├─────────────────────────────────────────────────────┤
  │  Step 2：生成今日小传（含 Step1 文本）                │
  │    memory/database.py（chunk 摘要 + 写 daily）        │
  ├─────────────────────────────────────────────────────┤
  │  Step 3：记忆卡片 Upsert + relationship_timeline      │
  │    SummaryLLMInterface（维度 JSON + 时间轴 JSON）     │
  ├─────────────────────────────────────────────────────┤
  │  Step 4：打分 + 全量 Chroma（主文档 + 可选事件片段）  │
  │    vector_store.add_memory / bm25 增量                │
  ├─────────────────────────────────────────────────────┤
  │  Step 5：Chroma GC（衰减 + 90 天未访问 + 无子节点）   │
  └─────────────────────────────────────────────────────┘
```

**手动触发与验收（以 `memory/daily_batch.py` 为准）**

- `DailyBatchProcessor` 仅 `__init__(self)`，**不接受**数据库参数；跑批内通过 `memory.database` 的模块级访问读写库。
- `await DailyBatchProcessor().run_daily_batch(batch_date)`：`batch_date` 为 `None` 时用东八区当天（与定时任务一致）。
- `trigger_daily_batch_manual(batch_date=None)`：同步封装，内部同样是 `DailyBatchProcessor()` + 事件循环里跑 `run_daily_batch`。

在项目根目录执行示例（`python` 指向已安装依赖的解释器即可）：

```bash
# 跑「今天」
python -c "import sys, asyncio; sys.path.insert(0, '.'); from memory.daily_batch import DailyBatchProcessor; asyncio.run(DailyBatchProcessor().run_daily_batch())"

# 跑指定日（重跑 / 断点续跑验证）
python -c "import sys, asyncio; sys.path.insert(0, '.'); from memory.daily_batch import DailyBatchProcessor; asyncio.run(DailyBatchProcessor().run_daily_batch('2026-03-21'))"

# 与上等价的同步入口
python -c "import sys; sys.path.insert(0, '.'); from memory.daily_batch import trigger_daily_batch_manual; trigger_daily_batch_manual()"
```

```bash
# 查最近跑批状态（显式列名，对应五步 + 错误信息）
python -c "import sqlite3; c=sqlite3.connect('cedarstar.db').cursor(); c.execute('SELECT batch_date, step1_status, step2_status, step3_status, step4_status, step5_status, error_message, updated_at FROM daily_batch_log ORDER BY batch_date DESC LIMIT 3'); [print(r) for r in c.fetchall()]"
```

```bash
# 抽样向量库 metadata（预期含 hits、halflife_days、last_access_ts 等，见 §3.4.5）
python -c "import sys; sys.path.insert(0, '.'); from memory.vector_store import get_vector_store; vs=get_vector_store(); r=vs.collection.get(limit=5, include=['metadatas']); [print(r['ids'][i], r['metadatas'][i]) for i in range(len(r['ids']))]"
```

### 4.3 Mini App 数据流

```
浏览器（React Mini App）
        │  HTTP GET/POST/PUT/DELETE /api/...
        ▼
  main.py（FastAPI + CORS）
        │
        ▼
  api/router.py（路由分发）
        │
        ├──► api/dashboard.py ──► memory/database.py（读取状态数据）
        ├──► api/persona.py   ──► memory/database.py（persona_configs CRUD）
        ├──► api/memory.py    ──► memory/database.py（memory_cards CRUD）
        │                    ──► memory/vector_store.py（长期记忆：先向量库后镜像表创建；删除先镜像表后向量库）
        ├──► api/history.py   ──► memory/database.py（messages 表查询）
        ├──► api/logs.py      ──► memory/database.py（logs 表查询）
        ├──► api/config.py    ──► memory/database.py（config 表读写）
        └──► api/settings.py  ──► memory/database.py（api_configs CRUD + token_usage 统计）
```

### 4.4 LLM 配置热更新流程

```
Mini App 用户在 Settings 页面切换激活 API 配置
        │  PUT /api/settings/api-configs/{id}/activate
        ▼
  memory/database.py（更新 api_configs.is_active 字段）
        │
        ▼（下次消息到来时）
  bot/discord_bot.py 或 bot/telegram_bot.py
        │  每次请求动态 new LLMInterface()
        ▼
  llm/llm_interface.py._load_active_config()
        │  从 api_configs 表读取 is_active=1 的配置（含 persona_id）
        ▼
  使用新配置调用 LLM API；构造时同时确定 character_id（热更新生效，无需重启）
```

---

## 5. 数据库表结构概览

数据库文件：`cedarstar.db`（SQLite）

### 5.1 `messages` — 对话消息表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `session_id` | TEXT | 会话ID（Discord: `{user_id}_{channel_id}`，Telegram: `telegram_{chat_id}`） |
| `role` | TEXT | 消息角色（`user` / `assistant` / `system`） |
| `content` | TEXT | 消息内容 |
| `created_at` | TIMESTAMP | 创建时间 |
| `user_id` | TEXT | 用户ID |
| `channel_id` | TEXT | 频道/聊天ID |
| `message_id` | TEXT | 平台原始消息ID |
| `is_summarized` | INTEGER | 是否已摘要（0=未摘要，1=已摘要） |
| `character_id` | TEXT | 角色/人设标识：Bot 侧为当前激活 `chat` 类型 `api_configs.persona_id` 的字符串形式，未绑定或走环境变量回退时为 `"sirius"` |
| `platform` | TEXT | 平台标识（`discord` / `telegram`） |
| `thinking` | TEXT | 思维链内容（DeepSeek R1 等模型的推理过程） |

**索引：** `(session_id, created_at)`、`(is_summarized)`、`(session_id, is_summarized)`

---

### 5.2 `summaries` — 对话摘要表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `session_id` | TEXT | 会话ID |
| `summary_text` | TEXT | 摘要内容 |
| `start_message_id` | INTEGER | 摘要覆盖的起始消息ID |
| `end_message_id` | INTEGER | 摘要覆盖的结束消息ID |
| `created_at` | TIMESTAMP | 创建时间 |
| `summary_type` | TEXT | 摘要类型（`chunk`=微批摘要，`daily`=今日小传） |
| `source_date` | DATETIME | 摘要所覆盖/归属的日期（新写入由应用填入本地日期；旧库由启动迁移按 `date(created_at)` 回填） |

**索引：** `(session_id, created_at)`、`(session_id, summary_type, source_date)`、`(source_date)`

---

### 5.3 `memory_cards` — 记忆卡片表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `user_id` | TEXT | 用户ID |
| `character_id` | TEXT | 角色ID |
| `dimension` | TEXT | 记忆维度（枚举，见下方） |
| `content` | TEXT | 记忆内容 |
| `updated_at` | TIMESTAMP | 最后更新时间 |
| `source_message_id` | TEXT | 来源消息ID |
| `is_active` | INTEGER | 是否激活（0=停用，1=激活，软删除） |

**维度枚举（7个）：**
- `preferences`：偏好与喜恶
- `interaction_patterns`：相处模式
- `current_status`：近况与生活动态
- `goals`：目标与计划
- `relationships`：重要关系
- `key_events`：重要事件
- `rules`：相处规则与禁区

**索引：** `(user_id, character_id, dimension, updated_at)`、`(user_id, is_active)`、`(is_active)`

**访问约定：** 列表与 Context 仅展示 `is_active=1`（`get_memory_cards`）。日终跑批 Step 3 用 `get_latest_memory_card_for_dimension` 读写**含停用行**的最近一条，合并后通过 `update_memory_card(..., reactivate=True)` 恢复展示。

---

### 5.4 `temporal_states` — 临时/时态状态表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PK | 主键（字符串 ID） |
| `state_content` | TEXT | 状态内容 |
| `action_rule` | TEXT | 行为/动作规则描述 |
| `expire_at` | DATETIME | 过期时间 |
| `is_active` | INTEGER | 是否生效（默认 1） |
| `created_at` | DATETIME | 创建时间 |

**索引：** `(expire_at, is_active)`、`(is_active)`

---

### 5.5 `relationship_timeline` — 关系时间线表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PK | 主键（字符串 ID） |
| `created_at` | DATETIME | 事件时间 |
| `event_type` | TEXT | 事件类型枚举：`milestone` / `emotional_shift` / `conflict` / `daily_warmth` |
| `content` | TEXT | 事件内容 |
| `source_summary_id` | TEXT | 关联摘要 ID（字符串，可与 `summaries.id` 对应） |

**索引：** `(created_at)`

---

### 5.6 `longterm_memories` — 长期记忆镜像表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `content` | TEXT | 记忆内容 |
| `chroma_doc_id` | TEXT | 对应 ChromaDB 中的文档ID |
| `score` | INTEGER | 价值分（1-10，日终打分结果） |
| `created_at` | TIMESTAMP | 创建时间 |

> 此表是 ChromaDB 的 SQLite 镜像，用于 Mini App 展示，两者通过 `chroma_doc_id` 关联。

**API 说明：** `GET /api/memory/longterm` 返回的每条 `items[]` 在表字段之外包含：
- `is_orphan`（布尔）：当 `chroma_doc_id` 为空或仅空白时为 `true`（历史双写失败遗留）；新通过 Mini App 创建的长期记忆在正常路径下恒为 `false`。
- `hits`、`halflife_days`、`last_access_ts`：来自 Chroma 文档元数据（与 `memory/vector_store.py` 写入规则一致）；当 `is_orphan` 为 `true` 时三项均为 `null`。`last_access_ts` 为 Unix 时间戳（秒，浮点）。

---

### 5.7 `config` — 运行参数配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | TEXT PK | 配置键名 |
| `value` | TEXT | 配置值（字符串存储） |
| `updated_at` | TIMESTAMP | 最后更新时间 |

**当前配置项：**
- `buffer_delay`：消息缓冲延迟（秒）
- `chunk_threshold`：微批处理阈值（条数）
- `short_term_limit`：Context 中最近消息数
- `longterm_score_threshold`：长期记忆归档分数阈值
- `reranker_top_n`：Reranker 返回结果数

**API 响应元数据：** `GET` / `PUT` `/api/config/config` 成功时，返回体中的 `data` 除上述键外另含 `_meta: { updated_at: string | null }`，值为这些键在 `config` 表中的 `MAX(updated_at)`（SQLite 时间字符串，前端解析时需注意这是 UTC 时间，需转为本地时区），用于 Mini App「上次保存时间」；`_meta` 不是配置项，不参与 `PUT` 写回。实现：`memory/database.py` 的 `get_config_max_updated_at_for_keys`、`api/config.py` 的 `_payload_with_meta`。

---

### 5.8 `persona_configs` — 人设配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `name` | TEXT | 人设名称 |
| `char_name` | TEXT | 角色姓名 |
| `char_personality` | TEXT | 角色性格描述 |
| `char_speech_style` | TEXT | 角色说话方式 |
| `user_name` | TEXT | 用户名称 |
| `user_body` | TEXT | 用户身体特征 |
| `user_habits` | TEXT | 用户习惯 |
| `user_likes_dislikes` | TEXT | 用户喜恶 |
| `user_values` | TEXT | 用户价值观 |
| `user_hobbies` | TEXT | 用户爱好 |
| `user_taboos` | TEXT | 用户禁忌 |
| `user_nsfw` | TEXT | NSFW 设置 |
| `user_other` | TEXT | 其他信息 |
| `system_rules` | TEXT | 系统规则 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

---

### 5.9 `api_configs` — API 配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `name` | TEXT | 配置名称 |
| `api_key` | TEXT | API 密钥 |
| `base_url` | TEXT | API 基础 URL |
| `model` | TEXT | 模型名称 |
| `persona_id` | INTEGER | 关联的人设ID（外键，可为空） |
| `is_active` | INTEGER | 是否激活（同类型内唯一激活） |
| `config_type` | TEXT | 配置类型（`chat` / `summary`） |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

---

### 5.10 `logs` — 系统日志表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `created_at` | TIMESTAMP | 日志时间 |
| `level` | TEXT | 日志级别（INFO/WARNING/ERROR） |
| `platform` | TEXT | 平台标识 |
| `message` | TEXT | 日志消息 |
| `stack_trace` | TEXT | 堆栈跟踪（可为空） |

**索引：** `(created_at)`

---

### 5.11 `token_usage` — Token 使用量表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `created_at` | TIMESTAMP | 记录时间 |
| `platform` | TEXT | 平台标识 |
| `prompt_tokens` | INTEGER | 输入 Token 数 |
| `completion_tokens` | INTEGER | 输出 Token 数 |
| `total_tokens` | INTEGER | 总 Token 数 |
| `model` | TEXT | 使用的模型名称 |

**索引：** `(created_at)`

---

### 5.12 `daily_batch_log` — 日终跑批日志表

| 字段 | 类型 | 说明 |
|------|------|------|
| `batch_date` | DATE PK | 批处理日期（YYYY-MM-DD） |
| `step1_status` | INTEGER | Step1：时效状态结算（0=未完成，1=已完成） |
| `step2_status` | INTEGER | Step2：今日小传 |
| `step3_status` | INTEGER | Step3：记忆卡片 + 关系时间轴 |
| `step4_status` | INTEGER | Step4：向量归档与事件拆分 |
| `step5_status` | INTEGER | Step5：Chroma GC |
| `error_message` | TEXT | 错误信息 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

**逻辑字段顺序**（与 `save_daily_batch_log` / 代码中的读写一致）即上表从上到下的顺序。**若库由较早版本创建、后经 `ALTER TABLE` 追加 `step4_status` / `step5_status`，** SQLite 中 `SELECT *` 的**物理列顺序**可能为：`batch_date`，`step1_status`～`step3_status`，`error_message`，`created_at`，`updated_at`，`step4_status`，`step5_status`。验收与手工 `UPDATE` 时请写**显式列名**，勿依赖 `SELECT *` 的下标含义。

**历史数据（三步时代已全完成、升级后 step4/step5 仍为 0）：** 服务启动时 `migrate_database_schema` 会**一次性**执行等价于下面的 `UPDATE`，并在 `config` 表写入键 `backfill_daily_batch_step45_legacy_v1`，之后不再执行。若需手工在 sqlite3 中修补，可执行：

```sql
UPDATE daily_batch_log
SET step4_status = 1, step5_status = 1
WHERE step1_status = 1 AND step2_status = 1 AND step3_status = 1;
```

---

## 6. 结构问题与改进建议

### 6.1 ✅ 已修复：消息缓冲逻辑代码重复

**问题：** `bot/discord_bot.py` 和 `bot/telegram_bot.py` 中的消息缓冲逻辑（`_add_to_buffer`、`_process_buffer`、`buffer_locks`、`buffer_timers`、`message_buffers`）几乎完全相同，约 150 行代码重复。

**修复（2026-03-21）：** 新建 `bot/message_buffer.py`，`MessageBuffer` 类持有三字典与 `add_to_buffer()` / `_process_buffer()`；超时合并后调用构造时传入的异步 `flush_callback(session_id, combined_content, buffer_messages)`。Discord / Telegram 各自保留 `_add_to_buffer` 薄封装（组装会话 ID 与缓冲条目）及 `_flush_buffered_messages`（原超时后的平台逻辑）。行为与时序（含 `buffer_delay` 读取、`CancelledError` 处理）与重构前一致。详见 §3.2、§4.1。

---

### 6.2 ✅ 已修复：`character_id` 硬编码

**问题：** 在 `bot/discord_bot.py` 和 `bot/telegram_bot.py` 中，`character_id` 被硬编码为字符串 `"sirius"`，没有从 `api_configs` 关联的 `persona_id` 动态读取。

**修复（2026-03-21）：** `llm/llm_interface.py` 在 `__init__` 中根据 `_load_active_config()` 返回的激活行解析 `persona_id`，暴露实例属性 `character_id`（无则 `"sirius"`）。`get_active_api_config` 本身为 `SELECT *`，不增加查询次数。两个 Bot 在 `save_message()` 时使用与同一次 `LLMInterface()` 调用对应的 `llm.character_id`（与「每次请求 new LLM」的热更新策略一致）。

---

### 6.3 ✅ 已修复 / 已演进：`daily_batch.py` Step 3 记忆卡片

**问题（历史）：** 日终记忆卡片更新曾缺失或仅为简单拼接，同维度内容重复堆叠；仅按 `get_memory_cards`（`is_active=1`）判断「是否有旧卡」时，批量软删后无法命中旧行；SUMMARY 模型若返回前置说明或非严格 JSON，维度解析易失败。

**当前行为（与 §3.4.4 Step 3 一致）：**

1. 从 `summaries` 表取最新一条 `summary_type='daily'` 的今日小传（Step 2 产出）
2. 从 `messages` 表查询当批日期的 `(user_id, character_id)` 列表（无记录时兜底 `default_user/sirius`）
3. 构建 Prompt，要求 SUMMARY LLM 按 7 个维度返回严格 JSON（`content` 或 `null`）
4. **解析 JSON：** 整段 `json.loads` → 失败则截取首个**平衡** `{...}`（含 \`\`\`json 块）→ 再回退贪婪正则；仍失败则 Step 3 报错退出
5. **Upsert：** `get_latest_memory_card_for_dimension` 取该用户/角色/维度最近一条（**含 `is_active=0`**）；有则 `_merge_memory_card_contents`（摘要模型 `generate_simple`）合并去重，`update_memory_card(..., reactivate=True)`；无则 `INSERT`；合并 LLM 失败时 fallback 为追加式拼接
6. 单维度 `try/except + continue`，互不拖累
7. 维度分析仍走 `summary_llm.generate_summary`（经 micro_batch 摘要模板包装）；**合并**走 `_call_summary_llm_custom`（直连 `generate_simple`，避免模板干扰）

---

### 6.4 ✅ 已修复：`daily_batch.py` Step3 打分逻辑 Bug

**问题：** `_step3_score_and_archive()` 中调用 `self.llm.generate(prompt)` 返回的是 `LLMResponse` 对象，但代码直接对其做正则匹配，应该取 `.content` 属性再匹配。

**修复（2026-03-21）：** 在 `generate()` 调用后增加 `score_text = score_response.content`，后续正则匹配和日志输出均改为使用 `score_text`。

```python
score_response = self.llm.generate(prompt)   # 返回 LLMResponse 对象
score_text = score_response.content           # ✅ 取 .content 属性
score_match = re.search(r'\b([1-9]|10)\b', score_text)
```

---

### 6.5 ✅ 已修复：`api/history.py` / `api/logs.py` 全量加载后内存过滤

**问题：** `get_history()` 接口调用 `db.get_all_messages()` 获取所有消息后在 Python 内存中过滤和分页，当消息量大时性能极差。`api/logs.py` 存在同样问题。

**修复（2026-03-21）：** 在 `memory/database.py` 中新增两个方法，将过滤与分页逻辑完全下推到 SQL 层：

- `get_messages_filtered(platform, keyword, date_from, date_to, page, page_size)`：对 `messages` 表使用 `WHERE` 条件过滤（platform 精确匹配、keyword 对 content/thinking 做 `LIKE`、date_from/date_to 用 SQLite `date()` 函数比较），`COUNT(*)` 获取总条数，`LIMIT/OFFSET` 分页，同时返回 `{total, messages}`。
- `get_logs_filtered(platform, level, keyword, page, page_size)`：对 `logs` 表同理，level 自动转大写后精确匹配，keyword 对 message/stack_trace 做 `LIKE`。

`api/history.py` 和 `api/logs.py` 改为直接调用上述新方法，删除了原有的全量加载、Python 内存过滤、手动排序和切片逻辑。过滤条件为空时不拼接对应 `WHERE` 子句。前端接口格式（`total / page / page_size / messages|logs`）保持不变。

---

### 6.6 ✅ 已修复：`BM25Retriever` 初始化时索引为空

**问题：** `BM25Retriever._build_index()` 在初始化时将索引设为空列表，需要手动调用 `refresh_index()` 才能从 ChromaDB 加载数据。但 `refresh_index()` 只在日终归档时被调用，导致服务重启后 BM25 索引始终为空，直到下次日终跑批。

**修复（2026-03-21）：** 重写 `_build_index()`，在服务启动时直接从 ChromaDB 拉取全量文档并建立索引。ChromaDB 为空或连接失败时优雅降级为空索引，不抛异常、不阻断服务启动。

---

### 6.7 ✅ 已修复：`longterm_memories` 表与 ChromaDB 双写不一致风险

**问题：** `api/memory.py` 的 `create_longterm_memory()` 先写 SQLite 再写 ChromaDB，如果 ChromaDB 写入失败，SQLite 中已有记录但 `chroma_doc_id` 为空，导致数据不一致。删除时也可能出现 ChromaDB 删除成功但 SQLite 删除失败的情况。

**修复（2026-03-21）：**

1. **创建：** 先 `vector_store.add_memory()`（`doc_id` 使用 `manual_{uuid}`），成功后再 `create_longterm_memory(..., chroma_doc_id=...)`；Chroma 失败则直接返回业务失败且不写 SQLite。若 SQLite 写入失败则尝试 `delete_memory` 回滚 Chroma 中的同 `doc_id`。
2. **删除：** 先 `delete_longterm_memory`，成功后再删 Chroma；SQLite 失败仍返回删除失败；Chroma 删除失败仅 `warning` 日志，接口仍返回成功（避免向量残留影响接口语义，由运维/后续清理处理）。
3. **查询：** `GET /longterm` 对每条记录附加 `is_orphan: true/false`（`chroma_doc_id` 缺失即为孤儿行），供前端提示历史遗留数据。

---

### 6.8 ✅ 已跟进：`services/`、`tools/` 占位模块说明

**问题：** `services/wx_read.py`、`tools/weather.py`、`tools/location.py` 仅有版本号字符串，无任何实现，`test/` 目录也完全为空。

**修复（2026-03-21）：** 根目录新增 `README.md`，在「规划中，暂未实现」一节明确列出上述三个文件，避免误读为已交付功能。`test/` 目录仍为空，可后续单独补齐测试体系。

---

### 6.9 ✅ 已修复：`config.py` 中 `Platform` 常量未被完整使用

**问题：** `config.py` 定义了 `Platform.RIKKAHUB = "rikkahub"` 常量，但在代码中没有任何地方使用该平台。同时，两个 bot 中仍有直接写字符串 `"discord"` / `"telegram"` 的地方，没有统一引用 `Platform` 常量。

**修复（2026-03-21）：** 在 `bot/discord_bot.py` 和 `bot/telegram_bot.py` 的 import 行补充了 `Platform`，并将所有 `save_message()` 调用中的 `platform="discord"` / `platform="telegram"` 字符串字面量全部替换为 `Platform.DISCORD` / `Platform.TELEGRAM` 常量（两个文件各 4 处，共 8 处）。

---

### 6.10 ✅ 已修复：`requirements.txt` 包含无效依赖

**问题：** `requirements.txt` 中包含 `psycopg2-binary`（PostgreSQL 驱动），但项目实际使用的是 SQLite（Python 内置），不需要此依赖。注释说明也有误（写的是 "SQLite support"）。

**修复（2026-03-21）：** 已删除 `psycopg2-binary` 及其注释行。SQLite 为 Python 内置模块，无需额外安装。

---

### 6.11 ✅ 已修复：`miniapp/src/router.jsx` 使用 JSX 但未导入 React

**问题：** `router.jsx` 中使用了 JSX 语法（`<Dashboard />`），但文件顶部没有 `import React from 'react'`。在 React 17+ 的新 JSX Transform 下可以工作，但依赖构建工具配置，可能在某些环境下报错。

**修复（2026-03-21）：** 在文件顶部补充 `import React from 'react'`（位于页面组件 import 之前），与显式 JSX 用法一致，降低对自动 JSX Runtime 配置的隐式依赖。

---

### 6.12 ✅ 已修复：`Config.jsx` 加载失败静默兜底与重置说明

**问题：** `GET /api/config/config` 失败或返回非成功时，页面将 `DEFAULT_CONFIG` 当作已加载数据展示，用户误以为即数据库真实值；「重置默认值」与后端 `config.py` 环境默认值可能不一致，缺少说明。

**修复（2026-03-21）：** 失败时不在界面用本地默认值冒充服务端数据：顶部红色 `role="alert"` 错误区 +「重新加载」重试；成功拉取时剥离 `data._meta` 后合并参数键与 `DEFAULT_CONFIG`（见 §5.7、§7.2）。「重置默认值」的说明以悬停 Tooltip（`config.css` 中 `.config-reset-tooltip`）及按钮 `title` 呈现。详见 §7.2。

---

## 7. 前端页面 Mock 数据排查报告

> 排查时间：2026-03-21  
> 排查范围：`miniapp/src/pages/` 下全部 7 个页面组件

**说明（2026-03-21）：** Bot 侧 `messages.character_id` 已改为随激活 API 配置的 `persona_id` 写入（见 §6.2），本节前端 Mock 排查结论未变；History 等页若按 `character_id` 过滤，将反映数据库中的实际值。

**说明（2026-03-21）：** `GET /api/memory/longterm` 的 `items[]` 已包含 `is_orphan` 字段（见 §6.7）。Memory 页可对 `is_orphan === true` 的长期记忆展示「未关联向量库」等提示，不属于 Mock 数据问题，为可选 UI 增强。

**说明（2026-03-21）：** `miniapp/src/router.jsx` 已显式 `import React from 'react'`（见 §6.11、§7.5），属路由工程规范修复，与页面 Mock 数据无关。

**说明（2026-03-21）：** `Config.jsx` 加载失败行为与重置说明已调整（见 §6.12、§7.2）。

**说明（2026-03-21）：** Bot 消息缓冲已抽取至 `bot/message_buffer.py`（见 §6.1），与 Mini App 页面 Mock 数据无关。

### 排查结论总览

| 页面 | 文件 | 是否有 Mock 数据 | 严重程度 |
|------|------|-----------------|---------|
| Dashboard（控制台概览） | `Dashboard.jsx` | ✅ 已修复（2026-03-21） | — |
| Memory（记忆管理） | `Memory.jsx` | ✅ 无 Mock；长期记忆可展示 `is_orphan`（§7.3） | 🟢 可选 UI |
| History（对话历史） | `History.jsx` | ✅ 无 | — |
| Logs（系统日志） | `Logs.jsx` | ✅ 无 | — |
| Persona（人设配置） | `Persona.jsx` | ✅ 无 | — |
| Config（助手配置） | `Config.jsx` | ✅ 已修复（2026-03-21，§7.2） | — |
| Settings（核心设置） | `Settings.jsx` | ✅ 无 | — |

---

### 7.1 ✅ 已修复：`Dashboard.jsx` —「批处理」状态曾硬编码为「全部成功」

**页面名称：** Dashboard（控制台概览）

**原问题：** `HealthCard` 中「最近批处理 / 批处理」曾写死为绿色「全部成功」，未反映真实跑批结果。

**修复：** `HealthCard` 接收父组件传入的 `batchLogs`（与页面级 `GET /api/dashboard/batch-log` 同源）。取**最近一条**日志（数组已按日期倒序），根据 `step1_status`～`step5_status` 是否均为 `1` 渲染「全部成功」或「存在失败」，并区分颜色；无记录时显示「暂无记录」。

**相关接口：** `/api/dashboard/batch-log`（批处理状态）；`/api/dashboard/status` 仍仅负责 Bot 在线与**对话**激活配置名/模型名（见 §3.5 边界说明）。

---

### 7.2 ✅ 已修复：`Config.jsx` — API 失败静默兜底与重置说明

**页面名称：** Config（助手配置）

**原问题：**  
1. `GET /api/config/config` 失败或非成功响应时，曾用 `DEFAULT_CONFIG` 填充表单，易与数据库真实值混淆。  
2. 「重置默认值」使用前端常量，与后端 `config.py` / 数据库可能不一致，缺少提示。

**修复（2026-03-21）：**  
1. 失败时不将本地默认值当作已加载数据：`config` 保持 `null`，页面顶部红色错误区（`role="alert"`）展示原因，并提供「重新加载」。成功时从 `data.data` 中解构出 `_meta`，其余键与 `DEFAULT_CONFIG` 合并为表单状态（勿把 `_meta` 写入 `config` 状态）。「上次保存时间」使用 `_meta.updated_at`（库内助手相关 key 的最近落库时间，见 §5.7），**不得**在每次 `GET` 成功时用 `new Date()` 冒充。`PUT` 成功后同样优先用响应中的 `_meta.updated_at` 更新展示，无则客户端兜底 `new Date()`。  
2. 「重置默认值」说明文案：悬停 Tooltip + 按钮 `title`（与后端/数据库默认值可能不一致）；确认弹窗内保留二次说明。

**对应接口：** `GET /api/config/config`、`PUT /api/config/config`（响应 `data` 形状见 §5.7）

---

### 7.3 ✅ 已增强：`Memory.jsx` — Tab 与长期记忆展示

**页面名称：** Memory（记忆管理）

**说明（2026-03-21 任务 7）：** 页面分为四个 Tab：记忆卡片、长期记忆、时效状态（`temporal_states` 列表/新增/软删除）、关系时间线（`relationship_timeline` 只读倒序）。长期记忆每条展示 Chroma 侧 `hits`、`halflife_days`、`last_access_ts`，并对 `is_orphan` 显示提示文案。

**时效状态 Tab UI：** 列表状态由 `getTemporalDisplayStatus` 根据 `is_active` 与 `expire_at` 推导（`生效中` / `已过期`）。**「软删除」（停用）按钮仅对「生效中」展示**；`expire_at` 已到期但日终跑批尚未把该行 `is_active` 置 0 时，界面显示「已过期」且**不**出现软删除，与 §6.2 Step 1 到期结算语义一致，避免对已到期记录重复操作。

**布局与 Tab 切页：** 固定 `.memory-container` 高度 + `.memory-content-scroll-area` 内滚动、统一 `.memory-tab-header` / `h2` 页头与顶距，已去除各 Tab 外层区块不一致的 `margin-top`（原 `longterm-section` / `temporal-section` / `timeline-section`），避免切 Tab 时标题上下跳动；详见 §3.6 Memory 页说明。

**对应接口：** `GET /api/memory/cards`、`GET/POST/DELETE /api/memory/*`（见 §7.4 表）

---

### 7.4 ✅ 其余页面：无 Mock 数据

以下页面均完整调用了对应的后端 API，无硬编码 mock 数据（Memory 页见 §7.3 为可选增强而非 Mock）：

| 页面 | 调用的 API 接口 |
|------|---------------|
| **Memory.jsx** | `GET /api/memory/cards`、`GET /api/memory/longterm`、`POST /api/memory/cards`、`PUT /api/memory/cards/{id}`、`DELETE /api/memory/cards/{id}`、`POST /api/memory/longterm`、`DELETE /api/memory/longterm/{id}`、`GET/POST /api/memory/temporal-states`、`DELETE /api/memory/temporal-states/{id}`、`GET /api/memory/relationship-timeline` |
| **History.jsx** | `GET /api/history`（支持 platform / keyword / date_from / date_to / page / page_size 参数） |
| **Logs.jsx** | `GET /api/logs`（支持 platform / level / keyword / page / page_size 参数） |
| **Persona.jsx** | `GET /api/persona`、`GET /api/persona/{id}`、`POST /api/persona`、`PUT /api/persona/{id}`、`DELETE /api/persona/{id}` |
| **Settings.jsx** | `GET /api/settings/api-configs?config_type=chat|summary`（按 Tab 过滤）、`POST` / `PUT` / `DELETE` / `PUT .../activate`、`POST .../fetch-models`、`GET /api/settings/token-usage`、`GET /api/persona`；保存配置后按返回表单中的 `config_type` 切换 Tab 或刷新当前列表（见 §3.6 Settings 页说明） |
| **Config.jsx** | `GET /api/config/config`、`PUT /api/config/config`（`data` 含 `_meta.updated_at`；失败时顶部错误提示 + 重试，见 §5.7、§7.2） |

---

### 7.5 ✅ 已修复：`router.jsx` 显式导入 React

**文件：** `miniapp/src/router.jsx`（路由与 `navItems` / `routes` 配置，非页面组件）

**说明：** 该文件内使用 JSX（如 `<Dashboard />`），此前未导入 React。已在顶部补充 `import React from 'react'`，与 §6.11 一致；构建侧仍可配合 Vite 的 React 插件使用。

---

*文档由代码自动分析生成，如有遗漏请以实际代码为准。*
