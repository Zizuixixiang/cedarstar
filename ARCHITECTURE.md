# CedarStar 项目架构文档

> 生成时间：2026-03-22（后续随代码演进修订；2026-04 起：Telegram webhook、`ENABLE_DISCORD`、日终 cron、`/webhook/telegram` 等与实现对齐）
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

CedarStar 是一个具备**长期记忆能力**的 AI 聊天机器人系统，支持 Discord 和 Telegram 双平台接入。**Discord** 可通过环境变量 **`ENABLE_DISCORD`** 关闭（关闭时不校验 `DISCORD_BOT_TOKEN`、不启动 Discord 进程）。**Telegram** 由 **FastAPI 接收 Bot API webhook**（`POST /webhook/telegram`，不经 `/api`），`main.py` 在启动 FastAPI 前调用 **`setup_telegram_webhook_app()`** 完成 `Application` 初始化与 handler 注册，**不再**在进程内 `getUpdates` 轮询。**日终跑批**不在 `main.py` 内定时调度，由 **cron（或同类）调用项目根目录 `run_daily_batch.py`** 触发；触发时刻与库表 `config.daily_batch_hour` 应对齐（由运维配置 cron）。

系统通过分层记忆架构（短期消息缓冲 → 微批摘要 → 日终小传 → 向量长期记忆）实现跨会话的持久化记忆，并提供一个 React 管理后台（Mini App）用于可视化管理。

**技术栈：**
- 后端：Python / FastAPI / PostgreSQL（asyncpg 连接池）/ ChromaDB
- 机器人：discord.py / python-telegram-bot
- LLM：OpenAI 兼容 API / Anthropic Claude（可配置）
- Embedding：长期记忆用智谱 AI embedding-3（1024 维）；表情包 Chroma 集合 `meme_pack` 用硅基流动 BAAI/bge-m3（与主记忆隔离）
- 检索：ChromaDB 向量检索 + BM25 关键词检索 + Cohere Rerank
- 前端：React + Vite（管理 Mini App）

---

## 2. 目录结构树

```
cedarstar/                          # 项目根目录
├── main.py                         # 主入口：校验配置 → 阻塞重建 BM25 → `setup_telegram_webhook_app()`（无 TG 轮询）→ 可选 `ENABLE_DISCORD` 时启动 Discord 线程 → FastAPI（`/webhook/telegram`；`/api/*` 须请求头 `X-Cedarstar-Token` 与 `MINIAPP_TOKEN` 一致；存在 `miniapp/dist` 时挂载 `/app` 托管静态 Mini App）；日终跑批不由此进程调度。setup_logging 含 httpx/httpcore 对 api.telegram.org 的 INFO 过滤（避免 Token 入日志）
├── config.py                       # 全局配置类（从 .env 读取），含 Platform 平台常量定义
├── requirements.txt                # Python 依赖清单（含 `python-telegram-bot[socks]` / `httpx[socks]`，供 `TELEGRAM_PROXY=socks5://...`）
├── README.md                       # 项目简介、技术栈、简略目录与「规划中」模块说明
├── start_bot.py                    # 备用启动脚本（校验配置 → 阻塞重建 BM25 → 仅启动 Discord Bot）
├── .env                            # 环境变量配置文件（不入库）
├── cedarstar.db                    # SQLite 数据库文件（已迁移至 PostgreSQL，此文件仅作历史备份参考）
├── cedarstar.log                   # 运行日志文件（运行时生成）
├── PROGRESS.md                     # 开发进度记录文档
│
├── api/                            # FastAPI REST API 层
│   ├── router.py                   # API 路由汇总，统一注册所有子路由
│   ├── dashboard.py                # 控制台概览接口（Bot 状态、记忆概览、批处理日志）
│   ├── persona.py                  # 人设配置 CRUD 接口
│   ├── memory.py                   # 记忆管理接口（记忆卡片 + 长期记忆：先 Chroma 后数据库写入，列表含 is_orphan）
│   ├── history.py                  # 对话历史查询接口（支持平台/关键词/日期过滤+分页）
│   ├── logs.py                     # 系统日志查询接口（支持平台/级别/关键词过滤+分页）
│   ├── config.py                   # 助手运行参数配置接口；GET/PUT 成功时 data 含 _meta.updated_at（见 §5.7）
│   ├── settings.py                 # API 配置管理接口（api_configs CRUD + Token 消耗统计）
│   └── webhook.py                  # Telegram Bot API webhook：`POST /webhook/telegram`（由 main 直接挂到 app，无 /api 前缀；校验 Secret-Token 后后台 `process_update`）
│
├── bot/                            # 聊天机器人层
│   ├── __init__.py                 # 包初始化文件
│   ├── message_buffer.py           # 消息缓冲公共实现（按 session 列表聚合条目；超时合并 texts/图片后回调）
│   ├── logutil.py                  # `exc_detail(exc)`：异常类型 + 说明 + `__cause__` 链，供 WARNING/ERROR 日志易读
│   ├── reply_citations.py          # 解析 [[used:uid]] / 误写 [used:…]、【used:…】；异步 update_memory_hits；`parse_telegram_segments_with_memes`（`|||` 与 `[meme:…]` 顺序分段）；清洗后再存库/发送
│   ├── vision_caption.py           # 异步视觉描述任务（vision API 写回 image_caption / vision_processed）
│   ├── stt_client.py               # 语音转录（httpx 异步调用 OpenAI 兼容 /audio/transcriptions，读 stt 配置或 OPENAI_* 回退）
│   ├── markdown_telegram_html.py   # Markdown→HTML（markdown）+ bleach 白名单；bleach 后展开正文 `<blockquote>`，避免模型滥用 `>` 导致 TG 满屏引用竖线
│   ├── telegram_html_sanitize.py   # 封装整段净化与 split_safe_html_telegram_chunks 切 4096
│   ├── discord_bot.py              # Discord 机器人（组合 MessageBuffer、LLM、消息存储）
│   └── telegram_bot.py            # Telegram 机器人（组合 MessageBuffer、LLM、消息存储）
│
├── llm/                            # LLM 接口层
│   ├── __init__.py                 # 包初始化文件
│   └── llm_interface.py            # 统一 LLM 接口（支持 OpenAI 兼容 API 和 Anthropic Claude）
│
├── memory/                         # 记忆系统层（核心模块）
│   ├── __init__.py                 # 包初始化文件
│   ├── database.py                 # PostgreSQL 数据库封装（asyncpg 连接池 + MessageDatabase 类 + 全局单例 + 便捷函数）
│   ├── context_builder.py          # Context 组装器（system + 时效状态 + 记忆卡片 + 关系时间线 + 摘要 + 折叠/精排长期记忆 + 近期消息）
│   ├── micro_batch.py              # 微批处理（消息达阈值时异步生成 chunk 摘要）
│   ├── daily_batch.py              # 日终五步流水线实现（`DailyBatchProcessor.run_daily_batch`）；生产由 cron 执行 `run_daily_batch.py`。库内仍保留 `schedule_daily_batch` 循环供自建调度，`main.py` 不启动
│   ├── vector_store.py             # ChromaDB 向量存储封装（智谱 Embedding + 增删查）
│   ├── meme_store.py               # 表情包专用 Chroma 集合 `meme_pack`（与主记忆隔离；写入/查询用显式向量，硅基流动 BAAI/bge-m3）
│   ├── bm25_retriever.py           # BM25 关键词检索（jieba 分词 + rank_bm25，内存缓存索引）
│   ├── reranker.py                 # Cohere Rerank 重排器（异步，对双路检索结果重排序）
│   └── async_log_handler.py        # 异步日志处理器（将日志写入数据库 logs 表）
│
├── services/                       # 外部服务集成层（待开发）
│   ├── __init__.py                 # 包初始化文件
│   └── wx_read.py                  # 微信读书服务（仅占位，尚未实现）
│
├── tools/                          # 工具函数层（部分实现）
│   ├── __init__.py                 # 包初始化文件
│   ├── meme.py                     # 表情包：`search_meme` / **`search_meme_async`**（向量检索，可选 `top_k`；TG 缓冲路径用 async）、`send_meme`（TG Bot 发静图/动图）；不由 LLM function calling 注册
│   ├── weather.py                  # 天气查询工具（仅占位，尚未实现）
│   └── location.py                 # 位置工具（仅占位，尚未实现）
│
├── miniapp/                        # 前端管理 Mini App（React + Vite）
│   ├── index.html                  # HTML 入口文件
│   ├── package.json                # Node.js 依赖配置
│   ├── vite.config.js              # Vite 构建配置（代理 /api 到 localhost:8000）
│   ├── .env.production             # 生产构建环境变量（如 VITE_API_BASE_URL）
│   └── src/
│       ├── apiBase.js              # `apiUrl()` / `API_BASE_URL`（`VITE_API_BASE_URL`）；`apiFetch()` 自动带 `Content-Type: application/json` 与 `X-Cedarstar-Token`（`VITE_MINIAPP_TOKEN`，须与后端 `MINIAPP_TOKEN` 一致）
│       ├── main.jsx                # React 应用入口，挂载根组件
│       ├── App.jsx                 # 根组件（响应式侧边栏导航 + 路由出口，支持移动端抽屉式菜单）
│       ├── router.jsx              # 路由配置（7 个页面；显式 import React）
│       ├── pages/                  # 页面组件
│       │   ├── Dashboard.jsx       # 控制台概览页（status / memory-overview / batch-log，顶栏与日历、记忆 KPI）
│       │   ├── Persona.jsx         # 人设配置页（角色/用户信息 CRUD）
│       │   ├── Memory.jsx          # 记忆管理页（四 Tab；固定视口内高度 + 内容区独立滚动，见 §3.6）
│       │   ├── History.jsx         # 对话历史页（聊天气泡布局 + 筛选，见 §3.6）
│       │   ├── Logs.jsx            # 系统日志页（固定高度布局，支持过滤）
│       │   ├── Config.jsx          # 助手配置页（运行参数滑块 + Telegram 分段 telegram_max_chars / telegram_max_msg，见 §3.6）
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
├── run_daily_batch.py              # 日终跑批独立入口：`await initialize_database()` 后 `DailyBatchProcessor().run_daily_batch()`；供 cron 调用，用法见 §4.2
├── migrate_to_postgres.py          # SQLite → PostgreSQL：仅迁移 persona_configs / api_configs / config 三表
├── supervisord.conf                # Supervisor 示例：运行 `python main.py`（部署路径按需修改）
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
- 提供 `validate_config()` 函数在启动时做必要性检查：**仅当 `ENABLE_DISCORD=true` 时校验 `DISCORD_BOT_TOKEN`**，否则跳过 Discord 令牌检查

**关键配置项：**

| 配置项 | 说明 |
|--------|------|
| `ENABLE_DISCORD` | 默认 `true`。为 `false` 时 `main.py` 不启动 Discord 线程，且 `validate_config()` **不**要求 `DISCORD_BOT_TOKEN` |
| `DISCORD_BOT_TOKEN` | Discord 机器人令牌；**仅 `ENABLE_DISCORD=true` 时必填** |
| `TELEGRAM_BOT_TOKEN` | Telegram 机器人令牌（可选；未设置则跳过 webhook 侧 `Application` 初始化） |
| `TELEGRAM_WEBHOOK_SECRET` | 与 Bot API 请求头 `X-Telegram-Bot-Api-Secret-Token` 一致；`api/webhook.py` 校验，不一致返回 401 |
| `TELEGRAM_PROXY` | 可选。仅 **Telegram Bot API**（`python-telegram-bot` / httpx）访问 `api.telegram.org` 使用：在 `HTTPXRequest` 上 **显式** `proxy=`，且 `httpx_kwargs={"trust_env": False}`，**不**读取 `HTTP_PROXY`/`HTTPS_PROXY`，避免与 Discord 启动时写入的环境变量混用。留空则 **直连**（部分网络下 `initialize()` 易超时）。国内常见：Clash **SOCKS5**（如 `socks5://127.0.0.1:7897`，端口与 Clash「混合端口」或 SOCKS 配置一致）；纯 `http://` 经 CONNECT 访问 Telegram 易出现 `ConnectError`/`start_tls` 失败。需安装 `requirements.txt` 中的 `python-telegram-bot[socks]`、`httpx[socks]` |
| `LLM_API_KEY / LLM_API_BASE / LLM_MODEL_NAME` | 主 LLM 配置 |
| `LLM_TIMEOUT` / `LLM_VISION_TIMEOUT` | `requests` 读超时（秒）：默认 **60** / **180**；`LLMInterface` 在 **messages 含多模态图片** 时取二者较大值作为单次 `chat/completions`（或等价）超时，否则仅用 `LLM_TIMEOUT`；**`config_type=vision`** 时实例 `timeout` 保底为 `max(LLM_TIMEOUT, LLM_VISION_TIMEOUT)` |
| `LLM_STREAM_READ_TIMEOUT` | 可选。仅 **`generate_stream`（SSE）** 的 **读** 超时（两次 chunk 之间最长等待）；默认 **`max(LLM_TIMEOUT, 300)`**，避免推理模型（如 R1）长时间无 token 时误触发 `ReadTimeout`。未设置时不必写 `.env` |
| `OPENAI_API_KEY / OPENAI_API_BASE` | 语音转录（STT）在库内无激活 `stt` 配置时回退；**不复用** `LLM_API_*`；`OPENAI_API_BASE` 默认 `https://api.openai.com/v1` |
| `SUMMARY_API_KEY / SUMMARY_API_BASE / SUMMARY_MODEL_NAME` | 摘要专用 LLM 配置 |
| `ZHIPU_API_KEY` | 智谱 Embedding API 密钥 |
| `SILICONFLOW_API_KEY` | 硅基流动 API 密钥兜底：表情包向量在 **`api_configs` 已激活 `embedding` 行且 `api_key` 非空时仅用库内 key**；否则读此项（`config.py` → `.env`） |
| `COHERE_API_KEY` | Cohere Rerank API 密钥 |
| `DATABASE_URL` | PostgreSQL 连接 DSN（asyncpg 格式，如 `postgresql://user:pass@host/db`）；未设置时返回空字符串 |
| `MINIAPP_TOKEN` | Mini App 访问 **`/api/*`** 时，请求头 **`X-Cedarstar-Token`** 须与本值**完全一致**，否则 `main.py` 返回 401；**不影响** **`POST /webhook/telegram`**（见 §3.5、§4.3）。前端构建环境变量 **`VITE_MINIAPP_TOKEN`** 须与之对齐（见 §3.6 `apiFetch`） |
| `CHROMADB_PERSIST_DIR` | ChromaDB 本地存储目录 |
| `MICRO_BATCH_THRESHOLD` | 微批触发阈值**兜底**：当数据库 `config.chunk_threshold` 未配置或无效时使用（默认 50 条） |
| `MESSAGE_BUFFER_DELAY` | 消息缓冲等待时间（默认 5 秒）；**主路径**为数据库 `config.buffer_delay`（见 `bot/message_buffer.py`） |
| `CONTEXT_MAX_RECENT_MESSAGES` | Context 最近原文条数**兜底**：当数据库 `config.short_term_limit` 未配置或无效时使用（默认 40 条） |
| `CONTEXT_MAX_DAILY_SUMMARIES` | Context 中每日摘要数（默认 5 条） |
| `DEFAULT_CHARACTER_ID` | 无有效激活 `chat` 行 `persona_id` 时的 `messages.character_id` 兜底（默认 `sirius`）；**Telegram 反应**落库走此路径，不经 `LLMInterface` |

---

### 3.2 `bot/` — 聊天机器人层

**职责：** 接收来自 Discord / Telegram 的用户消息，经过消息缓冲后调用 LLM 生成回复，并将消息和回复持久化到数据库。**Discord** 是否随 `main.py` 启动由 **`config.ENABLE_DISCORD`** 控制（见 §3.1）。

**边界：**
- 不直接操作 LLM 参数，每次请求动态创建 `LLMInterface` 实例（支持热更新）
- 写入 `messages.character_id`：**主对话（缓冲 flush）路径**使用**同一次请求**创建的 `LLMInterface.character_id`（来自当前激活 `chat` 配置中的 `persona_id`，解析逻辑与 `get_active_api_config('chat')` 一致；无有效 `persona_id` 时实例内兜底为 `"sirius"`）。**例外：** **Telegram 消息反应**落库不经 `LLMInterface`，直接查 `api_configs`（`config_type='chat'`、`is_active=1`）取 `persona_id`，无效则用环境变量 **`DEFAULT_CHARACTER_ID`**（见配置表），见本条下「Telegram 消息反应」
- 不直接操作数据库，通过 `memory.database` 的便捷函数存储消息
- **诊断日志：** Bot 与 **`llm/llm_interface.py`** 在关键 WARNING/ERROR 中使用 **`bot.logutil.exc_detail(exc)`** 输出异常类型、说明（空消息时用 `repr` 截断）及 **`__cause__`** 链。LLM 另记 **`endpoint` / `model`**；`requests` 有响应体时附 **HTTP status 与 body 前缀**。Telegram 流式路径：工作线程内 **`logger.exception`** 与 **`_telegram_finalize_sse_round_outcome`** 在 **`err_pack`** 时的 ERROR（含已缓冲 partial 长度）互补
- 不构建 prompt，通过 `memory.context_builder.build_context()` 获取完整上下文（`_assemble_full_system_prompt` 在引用死命令后追加 **`THINKING_LANGUAGE_DIRECTIVE`**，要求 thinking / reasoning 使用中文；Telegram 缓冲 flush 另传 `telegram_segment_hint=True`，在 system 末尾再追加 **`format_telegram_reply_segment_hint()`**（Markdown→发送侧见 `markdown_telegram_html`）：提示词内 **MAX_CHARS / MAX_MSG** 来自 `config` 表 `config.telegram_max_chars` / `config.telegram_max_msg`，默认 50 / 8，可由 Mini App 助手配置页调整；含表情包自然融入与 `[meme:…]` / `|||` 顺序说明；`|||` 仅用于最终正文、不在思维链中使用）；Discord 与其余路径不传 `telegram_segment_hint`，仍含中文思维链指令
- 消息缓冲（`message_buffers` / `buffer_locks` / `buffer_timers`、`add_to_buffer`、读 `buffer_delay` 后合并）由 **`bot/message_buffer.py`** 的 `MessageBuffer` 统一实现；超时后 `aggregate_buffer_entries()` 得到落库用 `combined_content`、当前轮 `images`（`image_payload` 列表）及 `text_for_llm`（多模态用纯文本）。flush 回调签名为 `(session_id, combined_content, images, buffer_messages, text_for_llm)`。两 bot 负责入缓冲条目（`content` 与/或 `image_payload` / `image_payloads`）、平台 typing 与分片发送；**`_flush_buffered_messages` → `_generate_reply_from_buffer` 须传入同一批 `buffer_messages`**（Discord / Telegram 一致）。**Telegram 缓冲 flush：** `_generate_reply_from_buffer` 按线路分支：**Anthropic `/messages`** → **`asyncio.to_thread` + `generate_with_context_and_tracking`（不传 `tools`）** → **`_telegram_deliver_prefetched_llm_response`**；**OpenAI 兼容 SSE** → **`_telegram_stream_thinking_and_reply`**（单轮 **`generate_stream`，`tools=None`**）→ **`_telegram_finalize_sse_round_outcome`**：流式编辑思维链占位，结束定稿一条 `<blockquote expandable>🧠 思维链`…（`parse_mode=HTML`），再按有序段交付助手回复（文字与表情包交替）。无正文且无思维链且无成功表情包时发通用错误提示前打 **WARNING**（含 `raw_preview`）。**表情包：** 在 `[[used:…]]` 清洗之后，**`parse_telegram_segments_with_memes`** 将 **`|||`** 与 **`[meme:描述]`** 拆成有序段，**`_telegram_deliver_ordered_segments`** 按序**交替**发送各段 HTML 与各 **`await search_meme_async(query, 1)`** → **`send_meme`**（无命中静默跳过）；仅表情无字且至少发出一张时落库可为 `[表情包]`，并可用首条媒体 `message_id` 落库。助手**对外正文**在 Citation 与 meme 标记清洗后由 **`bot/markdown_telegram_html.py`**（`markdown` + `bleach`）将模型 Markdown 转为 HTML 并白名单清洗（允许 `b` / `i` / `u` / `s` / `code` / `pre` / `a`（`href` 限 `http`/`https`/`tg`/`mailto`）/ `blockquote`（可选 `expandable`）；API 不支持 `<br>`，`nl2br` 产出在清洗前转为换行符），不在白名单的标签剥离并保留内文；**`bot/telegram_html_sanitize.py`** 对每段 `|||` 正文整段只做一次上述转换，再用 `split_safe_html_telegram_chunks` 按净化后长度适配 4096；思维链与正文同条时正文前缀用 `prefix_safe_html_by_max_len` 在**已净化 HTML** 上切分。再按 `|||` 拆成多条 `reply_text`（`parse_mode=HTML`），段间 `asyncio.sleep(0.5)`。Mini App **Persona** 页「系统规则」下提示模型在 Telegram 场景使用上述 HTML 标签、勿用 Markdown。`messages` 中 assistant **仅落库清洗后的整段正文**（分段用换行拼接，**不含** `|||`）。配置类错误等 flush 前失败仍走 `reply` 字符串由 `_flush_buffered_messages` 单条发出。`media_type` 由 **`ordered_media_type_from_buffer`** 按条目**时间顺序**遍历：每条目若有图/贴纸/语音则依次尝试追加 `image` / `sticker` / `voice`（已存在则跳过），得到**去重且保序**的逗号拼接串；不可只依赖 `combined_content` 字符串推断
- **慢处理与 flush：** 贴纸识图、语音 STT、图片下载等在 **`add_to_buffer` 之前**可能远长于 `buffer_delay`。Bot 在慢段前后调用 `MessageBuffer.begin_heavy` / `end_heavy`；定时器 `sleep(buffer_delay)` 结束后若该 session 仍有未配平的 heavy，**再轮询等待至多约 180 秒**（`message_buffer._BUFFER_HEAVY_WAIT_CAP_S`）后才取出缓冲区合并，避免「只有先发图片入队、贴纸/语音还在识图/转录就被 flush」的拆分。超时仍可能拆分时会打 WARNING 日志
- 图片：单张大于 10MB 不入视觉管线，缓冲内以文案 `[发送了1张图片（文件过大，已跳过视觉解析）]` 记录。用户消息落库时若有图：`media_type='image'`、`vision_processed=0`，并由 **`bot/vision_caption.py`** 调度异步任务（`config_type='vision'` 的激活配置）写回 `image_caption` 与 `vision_processed=1`；失败写 **`memory.database.VISION_FAIL_CAPTION_SHORT`**（`[视觉解析失败]`）。**`update_message_vision_result`** 在 `image_caption` 为 `[视觉解析失败]` 或 **`[系统提示：视觉解析超时失败]`**（与 **`expire_stale_vision_pending`** 超时 UPDATE）时同时将 **`is_summarized=1`**，避免占位行占用微批「未摘要」计数
- 语音：**入 Buffer 前**由 **`bot/stt_client.py`** 同步转录（`httpx.AsyncClient` → `api_base.rstrip('/') + '/audio/transcriptions'`，模型默认 `whisper-1`，可选 `language=zh`）。配置优先读 `api_configs` 中激活的 `config_type='stt'`；未配置则回退 **`.env` 的 `OPENAI_API_KEY` + `OPENAI_API_BASE`**（**不复用** chat LLM 环境变量）。Telegram：`message.voice`（`.ogg`/Opus）；`file_size` 大于 **50MB** 不下载，下载后 `len(bytes)` 大于 **25MB** 则落库 `[语音] 文件过大，跳过转录`。Discord：附件 `content_type` 为 `audio/ogg` 或 `audio/mpeg` 时同逻辑。成功落库 `content='[语音] …'`、`media_type` 中含 `voice`、`vision_processed=1`；失败为 **`bot/stt_client.TRANSCRIBE_FAIL_USER_CONTENT`**（`[语音] 转录失败`）。**Telegram** 用户行 `save_message(..., is_summarized=1)` 当 `combined_content` 含该兜底、或含 **`[贴纸]`** 且含占位 **`（贴纸）`**，或正文含上述视觉失败/超时文案。同轮图+语音等组合时 `media_type` 为按缓冲顺序去重后的逗号串（如 `image,voice`）。长语音转录若超过 `buffer_delay`，可能与紧邻文字拆成两轮对话，属耗时限制而非 bug
- **Telegram 贴纸（`message.sticker`）：** 以 `file_unique_id` 查 **`sticker_cache`**（§5.13）；**`MessageDatabase.get_sticker_cache` / `save_sticker_cache` / `delete_sticker_cache` 均为 `async`，机器人内须 `await`**（含 `/rescanpic` 删缓存）。未命中则模块级 `processing_stickers` + `asyncio.Lock` 去重，已在处理中的 id 轮询等待最多约 3 秒再读库。下载贴纸（跳过 `.tgs` / `.webm` 等、大于 10MB 跳过）转 Base64，**`asyncio.to_thread`** 内调用 **`LLMInterface(config_type='vision')`**，提示词要求 40 字以内描述含义与情绪，图内文字原样引用、不描述技术细节；结果写入缓存，失败亦写入 **`（贴纸）`**。正文 `content='[贴纸] {emoji} {description}'`；`media_type` 在缓冲顺序中与其它类型一并去重拼接（可与 `image`/`voice` 同轮，如 `image,sticker,voice`），`vision_processed=1`。**`/rescanpic`：** 用户先发命令后进入待重扫（模块集 `pending_rescan` + 60s 超时任务）；下一条贴纸会先 **`await delete_sticker_cache`** 并 `processing_stickers.discard` 再照常走识图；超时未发贴纸回复「已取消」，下一条非贴纸消息回复「未检测到贴纸，已取消」并照常处理该消息
- **Telegram 消息反应：** `MessageReactionHandler` 处理 `MessageReactionUpdated`。更新由 **webhook** 推送的 JSON 经 **`process_update`** → `Application.process_update` 进入同一套 handler，**无需** `start_polling`。取 `new_reaction` 中第一个可展示项（标准 emoji 或自定义表情 id）；**`new_reaction` 为空**（用户撤回反应）**不写库**。用 `message_id` 查同会话 `messages` 中 **`role='assistant'`** 且 **`message_id` 等于该条 Bot 发出消息的平台 ID** 的正文，取前 20 字作摘要；**查不到**时用摘要「某条消息」。合成 `content='[用户对你的消息「摘要…」点了 …]'`，`media_type='reaction'`，`role='user'`，**不入 MessageBuffer**，直接 `save_message` 并可触发微批检查。**`character_id`：** 查 `api_configs` 中 `config_type='chat'` 且 `is_active=1` 的 `persona_id`（转字符串）；无有效 `persona_id` 时用环境变量 **`DEFAULT_CHARACTER_ID`**（未设则 `sirius`），**不实例化 `LLMInterface`**。**说明：** 助手行自本逻辑起以 Telegram **真实发出**的 `message_id` 落库，旧数据中 `ai_{用户消息id}` 无法与反应事件对齐，反应摘要会走兜底文案
- 助手原始回复中的记忆引用由 **`bot/reply_citations.py`** 的 **`schedule_update_memory_hits_and_clean_reply`** 处理：规范格式为 `[[used:uid]]`；另兼容模型误写的 **单括号 `[used:…]`** 与 **全角 `【used:…】`**（均参与 `update_memory_hits` 并从正文剥离）。**Telegram** 在同一清洗之后由 **`reply_citations.parse_telegram_segments_with_memes`** 将 **`|||`** 与 **`[meme:描述]`** 一并作为顺序分隔符拆段，**`telegram_bot`** 按该顺序**交替**发送 HTML 文字与表情包；**`messages` 落库正文**为各文字段按序换行拼接（无 meme 标记）（见上条缓冲说明）
- 主对话 LLM：**Discord** 缓冲 flush 使用 `generate_with_context_and_tracking`。**Telegram 缓冲 flush**：OpenAI 兼容为 **SSE `generate_stream`（单轮、无 tools）**；Anthropic 为 **`generate_with_context_and_tracking`（无 tools）**（见上条）。非缓冲 **`_generate_reply`** 为单次 **`generate_with_context_and_tracking`**；若传入 **`telegram_bot`** 则先发思维链，再与缓冲路径相同按 **`parse_telegram_segments_with_memes`** 有序交替发文字与表情包。上述带 `usage` 的路径异步写入 `token_usage`（见 §3.3、§5.11）。`_assistant_outgoing_chunks` 仍保留（思维链转义 + 正文白名单净化）
- **`requests.exceptions.Timeout`（缓冲生成路径）：** 日志与用户可见提示按 **`images`（相册/拍照类多模态 payload）是否非空** 分支——无 payload 时仅提示上下文/上游慢、建议调大 `LLM_TIMEOUT`（**不**写「未带图片」，避免与「贴纸已转文本进主对话」混淆）；有 payload 时才提示多模态更慢及 `LLM_VISION_TIMEOUT`

**消息缓冲机制：**
```
用户发消息 → MessageBuffer.add_to_buffer() → 启动/重置 N 秒定时器
                                    ↓（超时）
                          合并 buffer 条目（文本 + 图片 payload）→ 调用各 bot 的 flush 回调
                                    ↓
                          build_context(..., images=..., llm_user_text=...；Telegram 缓冲另传 `telegram_segment_hint=True`) → LLM（Discord：`generate_with_context_and_tracking`；Telegram：OpenAI 兼容 **`generate_stream`（SSE，无 tools）** 或 Anthropic **`generate_with_context_and_tracking`（无 tools）**）→ 引用 hits 与 meme 清洗 → TG 按 `|||` / `[meme:…]` 有序交替发送 → 保存 → 可选异步视觉描述 → 触发微批检查
```

**Discord Bot 特有：**
- **`main.py` 仅在 `ENABLE_DISCORD=true` 时**在后台线程启动 `DiscordBot`；为 `false` 时整进程不连接 Discord Gateway
- 仅响应 `@mention` 或私聊消息
- 支持 `!ping` / `!clear` / `!model` / `!help` 命令
- 消息长度限制 2000 字符（自动分割）
- 支持 `attachments` 中 `image/*`：`await attachment.read()` 转 Base64 入缓冲（与文本同条合并）；支持 `audio/ogg`、`audio/mpeg`：读入后 **`transcribe_voice`** 再入缓冲（与文本同条合并）

**Telegram Bot 特有（webhook 模式）：**
- **入口：** Telegram 服务器 **`POST`** 公网 HTTPS **`/webhook/telegram`**（由 `main.py` 将 `api/webhook.py` 的 router **直接** `include_router` 到 `app`，**不带** `/api` 前缀）。请求头 `X-Telegram-Bot-Api-Secret-Token` 须与 **`TELEGRAM_WEBHOOK_SECRET`** 一致。Handler 内 **`BackgroundTasks`** 调用 **`bot.telegram_bot.process_update(update_json)`**：`Update.de_json(..., bot)` 后 **`await application.process_update(update)`**，与 polling 时代相同的 `CommandHandler` / `MessageHandler` / `MessageReactionHandler` 逻辑。
- **`main.py` 启动顺序：** `await initialize_database()` → BM25 `refresh_index()` → **`await setup_telegram_webhook_app()`**（内部 `TelegramBot.setup_webhook()`：`Application.builder()`… **`initialize()`** → **`set_my_commands`**（三 scope）→ **`start()`**，**不调用** `updater.start_polling`）。进程退出路径上 **`shutdown_telegram_webhook_app()`** 执行 `stop`/`shutdown`。
- 响应文本、语音、贴纸与图片消息（`VOICE` / `PHOTO` / `TEXT` / `Sticker`）
- 支持 `/start` / `/help` / `/model` / `/clear` / `/rescanpic` 命令；`initialize()` 后对 **`BotCommandScopeDefault`**、**`BotCommandScopeAllPrivateChats`**、**`BotCommandScopeAllGroupChats`** 各调用一次 **`bot.set_my_commands`**（同一组 5 条，含 `rescanpic`「重新识别贴纸图片」），避免仅写默认 scope 时部分会话里输入 `/` 不出现命令补全。客户端会缓存命令表，更新后若仍无补全可重开与该 Bot 的对话或重启 Telegram
- 消息长度限制 4096 字符（自动分割）。**缓冲回复（OpenAI 兼容主路径）：** SSE 流式编辑思维链占位消息，节流间隔为 **`config.TELEGRAM_THINK_STREAM_EDIT_INTERVAL_SEC`**（默认 **0.9s**，环境变量可覆盖，下限 0.15），结束时**定稿为单独一条**消息（`<blockquote expandable>🧠 思维链`…，`parse_mode=HTML`）；若 **`edit_message_text(HTML)`** 失败则 **WARNING** 并尝试**删占位**后以 **`reply_text`** 重发同内容（内文去 `\x00` 以降低实体解析失败概率）。随后按 **`parse_telegram_segments_with_memes`** 将 **`|||`** 与 **`[meme:…]`** 拆成有序段，**交替**发送 HTML 正文与表情包（非「全文后发完再逐条发图」）。**非缓冲路径**（`_generate_reply`）：可选传入 **`telegram_bot`** 时同样先发思维链，再按上述有序段交付
- session_id 格式：`telegram_{chat_id}`（Discord 为 `{user_id}_{channel_id}`）
- **Bot API 网络（出站）：** `Application.builder().token(...).request(HTTPXRequest(...)).get_updates_request(HTTPXRequest(...)).build()`。两处 `HTTPXRequest` 使用 `config.TELEGRAM_PROXY` 作为 `proxy`、并 `httpx_kwargs={"trust_env": False}`，避免 httpx 默认继承环境变量代理（Discord 会设置 `HTTP_PROXY` 等）。未配置 `TELEGRAM_PROXY` 时为直连；`connect_timeout`/`read_timeout`/`write_timeout` 相对默认放宽（约 25s / 120s / 120s）。**入站更新**不再使用 `getUpdates` 轮询；`send_message` / `edit_message` / `get_file` 等仍经上述 httpx 客户端访问 `api.telegram.org`。**说明：** 缓冲 flush 时 **`send_chat_action`（正在输入）** 若报 `httpx.ConnectError` / `NetworkError`，仅 **WARNING**、不中断生成，多见于当前进程**无法稳定连上** `api.telegram.org`（需检查 `TELEGRAM_PROXY`、防火墙或国际链路）。**发往用户的 `reply_text` / `send_message` 若在同一轮仍报 `telegram.error.NetworkError`**，`_generate_reply_from_buffer` **单独捕获**，用户可见提示侧重「连不上 Telegram / 检查代理」，与统称「生成回复出错」区分；`_flush_buffered_messages` 对无 `assistant_message_id` 时的补发 **`reply_text` 单次 try**，避免代理不可达时未处理异常刷屏

---

### 3.3 `llm/` — LLM 接口层

**职责：** 封装对 AI API 的 HTTP 调用，提供统一接口，屏蔽 OpenAI 和 Anthropic 的 API 差异。

**边界：**
- 优先从数据库 `api_configs` 表读取激活配置，回退到 `.env` 环境变量；激活行中的 `persona_id` 在构造时解析为实例属性 `character_id`（字符串，与 Bot 存消息共用，无则 `"sirius"`）
- 支持 `config_type` 为 `chat` / `summary` / `vision`（**语音转录 `stt` 不走本类**，由 **`bot/stt_client.py`** 单独读库调用 `/audio/transcriptions`）；**对话 API 路径**根据 `api_base` 是否含 `anthropic`（或模型名含 `claude`）选择 Anthropic Messages API 与 OpenAI 兼容 `chat/completions`；用户多模态 content 按提供商组装（Claude：`image`+base64 source；OpenAI 兼容：`image_url`+data URL）
- **读超时：** `generate_with_context` / `generate_with_context_and_tracking` 使用 `_request_timeout_seconds(messages)`：`messages_contain_multimodal_images(messages)` 为真时取 `max(LLM_TIMEOUT, LLM_VISION_TIMEOUT)`，否则为 `LLM_TIMEOUT`。**`config_type=vision`** 构造时已将 `self.timeout` 设为 `max(LLM_TIMEOUT, LLM_VISION_TIMEOUT)`，与贴纸识图等路径一致。`requests.post(..., timeout=…)` 触发超时时，ERROR 日志附带「请求中含多模态图片」或「无多模态图片，多为上下文过大或上游慢」，便于与 Bot 侧「本轮是否带图」对照。**`generate_stream`（Telegram 缓冲等）** 使用 `timeout=(min(30, LLM_STREAM_READ_TIMEOUT), LLM_STREAM_READ_TIMEOUT)`，与单次非流式请求的单一 `LLM_TIMEOUT` 语义不同（流式读超时约束「两次 SSE 数据之间」）
- 不维护对话历史状态（无状态）
- 支持思维链内容提取（DeepSeek R1 的 `reasoning_content`、Gemini 的 `thinking`），由 `generate_with_context_and_tracking` / `generate_with_thinking` 等在完整响应中解析；**流式**由 `generate_stream` 在 SSE `delta` 中读取 **`reasoning_content` / `reasoning` / `thinking`** 逐段 yield `("thinking", chunk)`，正文 yield `("content", chunk)`；若此前 delta 无推理片段，则在 **`choices[0].message`** 中同名字段补一次整段（适配仅末包给推理的网关）。生成器返回 `{"content","thinking","usage"}`
- **Tool calling（OpenAI 兼容）：** `generate` / `generate_with_token_tracking` / `generate_with_context_and_tracking` / `generate_with_thinking` / `generate_stream` 可选传入 `tools`；`_prepare_openai_payload` 在有 `tools` 时附带 `tool_choice: "auto"`；`_parse_openai_response` 将 `choices[0].message.tool_calls` 规范为 `LLMResponse.tool_calls`（每项 `id` / `name` / `arguments` 字符串）。Anthropic Messages API 路径暂不注入 tools，解析侧 `tool_calls` 恒为 `None`
- **Token 统计：**仅当调用带 tracking 的方法且响应中含 `usage` 时才会写入 `token_usage`（见 §5.11）：若当前线程存在**正在运行的** asyncio 事件循环，则 `create_task` 走 `_async_save_token_usage`；否则（例如 `bot/vision_caption.py` 在 `run_in_executor` 线程内调 vision LLM）**同步**调用 `get_database().save_token_usage(...)`，避免 `no running event loop` 与未 await 的协程告警。`generate` / `generate_simple` / `generate_with_context` / `chat` **不会**落库用量。

**主要方法：**

| 方法 | 说明 |
|------|------|
| `generate(prompt, system_prompt, history, tools=...)` | 基础生成，返回 `LLMResponse`（可含 `tool_calls`；不记 token） |
| `generate_simple(prompt)` | 简化版，只返回文本（不记 token） |
| `generate_with_context(messages)` | 接收完整 messages 数组（不记 token、**不传 tools**；Bot 主路径用 tracking 版） |
| `generate_with_token_tracking(..., tools=...)` | 单轮 prompt 生成并异步写 `token_usage` |
| `generate_with_context_and_tracking(messages, platform=..., tools=...)` | 完整 messages 非流式生成，返回 `LLMResponse`（`content`、`thinking`、`tool_calls` 等），并异步写 `token_usage` |
| `generate_stream(messages, platform=..., tools=...)` | OpenAI 兼容：`stream=True` + SSE，yield 思维链/正文 chunk，返回同上字典；Anthropic 路径整段生成后单次 yield `("content", text)`（不传 tools） |
| `generate_with_thinking(..., tools=...)` | 生成并提取思维链内容，并异步写 `token_usage` |
| `chat(message, history)` | 维护历史的聊天接口（不记 token） |

**实例属性：**

| 属性 | 说明 |
|------|------|
| `character_id` | 由本次构造时读到的激活 `api_configs.persona_id` 转成字符串；无激活配置或 `persona_id` 为空时恒为 `"sirius"` |

**`LLMResponse`：** 除 `content` / `model` / `usage` / `finish_reason` / `raw_response` / `thinking` 外，可选 **`tool_calls`**（OpenAI 风格列表，元素含 `id`、`name`、`arguments`（JSON 字符串））。

---

### 3.4 `memory/` — 记忆系统层（核心）

这是整个项目最复杂的模块，实现了分层记忆架构。

#### 3.4.1 `database.py` — 数据持久化

**职责：** 封装所有 PostgreSQL 操作，提供单例 `MessageDatabase` 实例和模块级便捷函数。使用 `asyncpg` 连接池（`min_size=2, max_size=10`），所有数据库操作均为 `async def`。

**边界：**
- 所有数据库操作都通过此模块，其他模块不直接操作数据库
- 提供 `get_database()` 单例工厂函数（同步）；应用启动时需调用 `await initialize_database()` 完成连接池初始化（读取 `config.DATABASE_URL`、调用 `init_pool` 并建表）
- **`save_message(..., is_summarized=0|1)`** 写入 `messages.is_summarized`；**模块便捷函数与 `MessageDatabase.save_message` 在绑 PostgreSQL TEXT 列前**，将 **`user_id` / `channel_id` / `message_id` / `character_id` / `platform` / `media_type`** 统一转为 **`str`**（Telegram 等平台 ID 常为 `int`，避免 asyncpg 报「expected str, got int」）。**`update_message_vision_result`** 在 `image_caption` 为 `[视觉解析失败]` / `[系统提示：视觉解析超时失败]` 时同步置 **`is_summarized=1`**
- 管理核心数据表（含 `meme_pack` 等，及日志/统计表）的 CRUD 操作；启动时由 `migrate_database_schema()` 幂等补齐列与索引（每次初始化成功执行后，`memory.database` 打 **INFO** 日志：`数据库 schema 迁移（索引/列）已执行`）
- Context 只读：`get_all_active_temporal_states()`（`temporal_states.is_active=1` 全量）、`get_recent_relationship_timeline(limit)`（数据库按 `created_at` 倒序取前 `limit` 条；`context_builder` 注入前对关系时间线再按 `created_at` 正序排列）
- 记忆卡片：`get_memory_cards()` 仅返回 `is_active=1`（供 API / Context）；日终 Step 3 Upsert 使用 `get_latest_memory_card_for_dimension()`，按 `user_id` + `character_id` + `dimension` 取**最近一条且不过滤 `is_active`**，避免批量软删后无法命中旧行；`update_memory_card(..., reactivate=True)` 在更新正文同时将 `is_active` 置 1（跑批合并写回后重新展示）

**✅ 已修复（2026-04-05）：** `get_messages_filtered` 的 **`date_from` / `date_to`** 若以字符串传入（如查询参数），在 SQL 绑定 `created_at::date` 条件前用 **`date.fromisoformat`** 转为 **`datetime.date`**，避免 asyncpg 类型不匹配导致 **`GET /api/history` 500**（History 页日期筛选）。

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
8. 最近消息（当前 session 中 `is_summarized=0` 的最新若干条，正序；条数优先 `config` 表 `short_term_limit`，否则环境变量 `CONTEXT_MAX_RECENT_MESSAGES`；**`format_user_message_for_context`**：先输出去掉图片/贴纸/语音结构行后的**纯文字**；再按 **`media_type.split(",")` 的顺序**依次调用 `_format_image_part` / `_format_sticker_part` / `_format_voice_part`（主函数仅路由）；**`media_type='reaction'`** 时由 **`_format_reaction_part`** 原样返回 `content`（Bot 已拼好完整语义）。`image_caption` 在 `_format_image_part` 中按**单字符串**处理（未来多图可升级为 JSON 数组）。旧行若无 `media_type`，则按正文出现顺序推断 `image`/`sticker`/`voice`
9. 当前用户消息（可选多模态：`build_context(session_id, user_message, images=..., llm_user_text=...)`，`images` 非空时由 `build_user_multimodal_content` 组装最后一轮 user content）

**精排（仅异步路径）：** 并行双路检索并折叠后，对剩余候选调用 Cohere 得到语义相关分；对每条再算时间衰减复活分（`age_days` 优先由 metadata `created_at` 推算，否则由 `last_access_ts`）：

```
arousal          = clamp(metadata.arousal ?? 0.1, 0.0, 1.0)   # 历史数据无此字段时兜底 0.1
effective_hl     = halflife_days × (1 + arousal)               # arousal 越高半衰期越长
decay_score      = base_score × exp(-ln(2) / effective_hl × age_days) × (1 + 0.35 × ln(1 + hits))
```

两路分数各自在当批候选内 min-max 归一化后按 **0.8×语义 + 0.2×衰减** 综合得分排序，取 top 2 写入 context。

**边界：**
- 同步版 `build_context()`：双路检索 + 父子折叠，无 Cohere；长期记忆块标题为「双路检索结果」
- 异步版 `build_context_async()`：并行检索 + 折叠 + Cohere 全候选打分 + 上述融合公式取 top2；`COHERE_API_KEY` 不可用时回退为同步双路逻辑
- System 块末尾固定追加引用死命令：若参考了上述历史记忆，须在回复文末标注 `[[used:uid]]`（可多个）；**勿**用单括号 `[used:…]` 或 `【used:…】`（`MEMORY_CITATION_DIRECTIVE`）；并追加思维链须中文（`THINKING_LANGUAGE_DIRECTIVE`：thinking / reasoning 使用中文）
- 可选 `telegram_segment_hint=True`（`build_context` / `build_context_async`）：在 system 末尾再追加 Telegram **HTML 白名单、`|||` 分段、勿滥用 Markdown `>` / `<blockquote>`、`[meme:描述]` 与 `|||` 同级顺序分隔、表情包自然融入对话的指引**（见 `context_builder.format_telegram_reply_segment_hint()`，其中 MAX_CHARS / MAX_MSG 读自 `config` 表的 `telegram_max_chars` / `telegram_max_msg`；`|||` 不得出现在思维链；仅 Telegram 缓冲路径启用）

**✅ 已改动（2026-04-05）：** `_build_system_prompt` 为 **`async def`**：`await get_active_api_config('chat')` 取 **`persona_id`**，再 **`SELECT * FROM persona_configs WHERE id = …`** 组装 system 正文（【Char 人设】/【我的人设】/【系统规则】等，与 Mini App `Persona.jsx` 预览格式一致）；无有效 `persona_id`、行不存在、拼装结果为空或异常时回退 **`config.SYSTEM_PROMPT`**（`.env` 的 `SYSTEM_PROMPT`）。`build_context` / `build_context_async` 均 **`await self._build_system_prompt()`**。

#### 3.4.3 `micro_batch.py` — 微批处理

**职责：** 每次消息写入后异步检查，当 session 中 `is_summarized=0` 且 **`vision_processed=1`** 的消息达到阈值时触发摘要生成。阈值优先 `config` 表 `config.chunk_threshold`，否则环境变量 `MICRO_BATCH_THRESHOLD`（默认 50）。

**视觉兜底：** `check_and_process_micro_batch` 与 `process_micro_batch` 开头调用 `expire_stale_vision_pending(5)`：将 `vision_processed=0` 且 `created_at` 早于当前 5 分钟以上的行置为 `vision_processed=1`，`image_caption='[系统提示：视觉解析超时失败]'`，避免异步任务丢失导致微批永远不满足。

**流程：**
```
消息写入 → trigger_micro_batch_check(session_id)
              ↓（expire_stale_vision_pending）
              ↓（达到阈值，且仅统计 vision_processed=1）
         取出最早的「阈值」条未摘要消息（同样要求 vision_processed=1）
              ↓
         `SummaryLLMInterface` → `LLMInterface.generate_with_context_and_tracking`（`platform=Platform.BATCH`）生成 chunk 摘要
              ↓
         写入 summaries 表（summary_type='chunk'）
              ↓
         批量标记消息 is_summarized=1
```

**✅ 已改动（2026-04）：** `process_micro_batch` 开头 **`await fetch_active_persona_display_names()`**（`get_active_api_config('chat')` → `persona_id` → **`persona_configs`** 取 **`char_name` / `user_name`**，失败或空则 **`AI` / `用户`**），经 **`generate_summary_for_messages(..., char_name=..., user_name=...)`** 传入 **`SummaryLLMInterface.generate_summary`**。`generate_summary` 在 prompt 首行注入 **`这是 {char_name} 与 {user_name} 的对话记录。`**，拼装对话正文时用 **`{user_name}:`** / **`{char_name}:`** 替代原先的「用户:」「助手:」；chunk 摘要指令为 **约 150–200 字**，强调主要话题、**双方情绪起伏**、关键信息并弱化无意义语气词，结尾 **`摘要（中文）:`**。模块导出 **`fetch_active_persona_display_names`** 供日终跑批复用。

#### 3.4.4 `daily_batch.py` — 日终跑批

**职责：** 在东八区某业务日执行五步流水线（支持断点续跑）。**标准部署**下由 **cron（或同类）按 `config.daily_batch_hour` 所设整点**（默认 23:00）调用项目根目录 **`run_daily_batch.py`** 触发；`daily_batch_hour` 为业务约定，**cron 表达式须与之一致**（代码不会替运维「自动对齐」系统时钟）。

**五步流水线：**

| 步骤 | 说明 |
|------|------|
| Step 1 | 巡视 `temporal_states`：`expire_at` 已到期且 `is_active=1` 的记录先 `UPDATE is_active=0`，再用 SUMMARY LLM 将 `state_content` 从「进行时」改写为过去时客观事实，结果列表供 Step 2 使用 |
| Step 2 | 将 Step 1 输出附在 prompt 开头，合并今日 chunk 摘要，调用 SUMMARY LLM 生成今日小传（`summary_type='daily'`） |
| Step 3 | 记忆卡片 Upsert：无对应维度则 `INSERT`；**有则调用模型合并去重后 `UPDATE`，合并失败时 fallback 为追加写入**；结束时再调 SUMMARY LLM 判断是否写入 `relationship_timeline`（含 Step 1 结算的时效事件），有则 `INSERT` |
| Step 4 | 主 LLM 打分，prompt 同步输出 `score`（整数 1–10）与 `arousal`（浮点 0.0–1.0，情绪强度；平静约 0.1，激烈事件 0.8+）；`halflife_days`：8–10→600，4–7→200，1–3→30。**全量**向量化入库（`generate_with_context_and_tracking`，`platform=Platform.BATCH`）；metadata 新增 `arousal: float`；先存 `daily_{batch_date}`，再按需拆分事件片段 `daily_{batch_date}_event_N`（同含 `arousal`），metadata 含 `parent_id` 指向当日主文档；增量更新 BM25 |
| Step 5 | Chroma GC：`vector_store.garbage_collect_stale_memories()` — **前置豁免**：`hits >= gc_exempt_hits_threshold`（优先 `config` 表 `gc_exempt_hits_threshold`，默认 10）则跳过不删；再依次判断：闲置天数超过 `gc_stale_days`（默认 180）、半衰期衰减得分 \<0.05、无子文档以该 `doc_id` 为 `parent_id`，三条全满足才物理删除 |

**Step 3 实现要点（与代码一致）：**
- **维度 JSON：** 对 SUMMARY LLM 返回依次尝试整段 `json.loads`；失败则截取**首个平衡的 JSON 对象**（跳过前置说明、处理字符串内转义；支持 \`\`\`json 代码块）；再回退原贪婪 `\{...\}` 正则。
- **Upsert 行定位：** `get_latest_memory_card_for_dimension()`（不过滤 `is_active`），保证「全表 `is_active=0` 后重跑」仍更新同一逻辑行，而非误当作无记录而堆叠 `INSERT`。
- **合并写回：** `_merge_memory_card_contents` → `_call_summary_llm_custom` 使用摘要模型配置的 `LLMInterface.generate_with_context_and_tracking([{"role":"user","content":prompt}], platform=Platform.BATCH)`（**不经** `SummaryLLMInterface.generate_summary` 的 chunk 多轮摘要外壳）；prompt 含 **`_persona_dialogue_prefix()`**、新版「逻辑合并」说明（去重、无缝整合段落、勿 Markdown 列表、勿遗漏旧设定等）、**维度补充**（`interaction_patterns` 与其它维度不同细则）、**输出要求**为严格 JSON `{"content":"…"}`（**不再**在 prompt 中写死 400 字上限）。合并失败则 fallback 为「旧正文 + `[batch_date]更新` + 新摘要」式追加。`update_memory_card(..., dimension=None, reactivate=True)` 写库并**重新激活**该卡。

**跑批 Prompt 与人物称呼（2026-04，与代码一致）：**
- **`run_daily_batch`** 在 **`await LLMInterface.create()`** 之后 **`await fetch_active_persona_display_names()`**（同 §3.4.3，来自 `memory.micro_batch`），写入 **`_batch_char_name` / `_batch_user_name`**；**`_persona_dialogue_prefix()`** 返回 `这是 {char} 与 {user} 的对话记录。\n`。
- **Step 1**（时效状态 JSON 数组改写）：**前缀 + 原任务正文**，仅 **`_call_summary_llm_custom`**，避免套 chunk「为对话生成摘要」模板。
- **Step 2**（今日小传）：**前缀 +** 按时间顺序、话题/事件/情感、保留互动细节与羁绊、勿分点列举等指令 + **`today_content`** + **`今日小传（中文）:`**，**`_call_summary_llm_custom`**。
- **Step 3**（七维 JSON、**关系时间轴** JSON）：仍 **`summary_llm.generate_summary([{"role":"user","content":prompt}], char_name=..., user_name=...)`**（内部带对话记录前缀，单条 user 承载完整任务文）。
- **Step 4**（小传 **score/arousal**）：主 LLM 的 user **prompt 前加 `_persona_dialogue_prefix()`**；**事件拆分**仍 **`generate_summary`** 并传 `char_name` / `user_name`。

**断点续跑：** `daily_batch_log` 记录 `step1_status`～`step5_status`，重启后跳过已完成步骤。

**库内自建调度（`schedule_daily_batch`，可选）：** 每次到点先将 `batch_date` 早于「含今日共 7 天」窗口且仍有未完成步骤的行标记为 `error_message='expired, skipped'`、五步均置 1；再对窗口内未完成日期按 `batch_date` 升序串行调用 `run_daily_batch(该日)`；若当日未出现在补跑列表中，最后再 `run_daily_batch()` 执行今天。**当前 `main.py` 主进程不启动此循环**；若需进程内定时器，须自行在独立进程或脚本中调用，生产推荐 **cron + `run_daily_batch.py`**。

#### 3.4.5 `vector_store.py` — 向量存储

**职责：** 封装 ChromaDB 操作，使用智谱 AI `embedding-3` 模型生成向量；**工程约定为 1024 维**（与占位零向量、检索逻辑一致）。

**边界：**
- 日终由 `daily_batch` 全量写入当日小传（及可选事件片段）；手工长期记忆仍通过 Mini App 写入
- 提供 `add_memory()` / `search_memory()` / `delete_memory()` / `update_memory_hits()` 便捷函数
- 集合名称固定为 `cedarstar_memories`
- **智谱 API 与维度：** `embedding-3` 在 HTTP 请求体中**若不传 `dimensions`，默认返回 2048 维**。`vector_store.ZhipuEmbedding` **必须**在调用 `/embeddings` 时显式传入 **`dimensions: 1024`**，否则首次 `collection.add` 会把 Chroma 集合固定为 2048，而查询与其它路径仍按 1024 维构造向量，会出现 `Collection expecting embedding with dimension of 2048, got 1024`（或同类维度不匹配），`get_all_memories`、BM25 `refresh_index` 也会异常。
- **旧库 / 误建成 2048 的集合：** 若本地 `chroma_db` 已按错误维度写入，**处理（推荐）：先停止占用 Chroma 的进程**，备份后**删除** `chroma_db` 目录，确保代码已带 `dimensions: 1024` 后再启动并重新跑批写入。旧架构向量与当前 metadata / 双轨约定不一致时，重建通常比就地迁移更干净；`longterm_memories` 表中历史行可能变为 Chroma 侧「孤儿」，由 Mini App `is_orphan` 提示，可按需清理或重新录入。
- **写入 metadata（Chroma）：** 在 `date` / `session_id` / `summary_type` 等调用方字段之外，`add_memory()` 会统一写入 `base_score`（float，可由调用方传入或从旧字段 `score` 推导，默认 5.0）、`halflife_days`（int，默认 30）、`hits`（int，新文档恒为 0）、`last_access_ts`（float，当前 Unix 时间戳），并保留 `created_at`（ISO 字符串）
- **doc_id 约定：** 日终主文档为 `daily_{batch_date}`（`build_daily_summary_doc_id`）；同一日多条事件片段为 `daily_{batch_date}_event_0`、`daily_{batch_date}_event_1`…（`build_daily_event_doc_id`）；Mini App 手工长期记忆仍为 `manual_{uuid}`
- **`update_memory_hits(uid_list)`：** 仅按 `doc_id` 列表 `get` 再 `update`，逐条 `hits+1` 并刷新 `last_access_ts`，不用 metadata `where` 查询
- **`garbage_collect_stale_memories()`：** 日终 Step 5 调用；衰减公式 `(base_score/10) * 0.5^(idle_days/halflife_days)`，与 `gc_stale_days` 天未访问、无 `parent_id` 子文档等条件组合后再 `delete`

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

**职责：** 将 Python logging 的日志异步写入数据库 `logs` 表，供 Mini App 查询。

#### 3.4.9 `meme_store.py` — 表情包向量集合

**职责：** 维护 ChromaDB 集合名 **`meme_pack`**，与 `vector_store` 的长期记忆集合完全隔离；**不使用** Chroma 内置 embedding，文档存原始检索文本，`collection.add(..., embeddings=[...])` 与查询时 `query_embeddings` 均使用 OpenAI 兼容 **`/v1/embeddings`**。`base_url`、`model`、`api_key` 来自 **`get_active_api_config('embedding')`**；库内 `api_key` 为空时回退 **`.env` 的 `SILICONFLOW_API_KEY`**（经 `config.py`）。

**边界：**
- `get_meme_store()` 单例；持久化目录同 `CHROMADB_PERSIST_DIR`（集合名不同，数据文件与主记忆分集合存放）
- `add_meme(id, name, url, is_animated, document_text=...)`：对文档文本调用 `siliconflow_embed_text` 后写入；metadata 含 `sqlite_id`（历史兼容字段名，实际存储数据库 `meme_pack.id`）等与 `meme_pack` 表对齐
- `search_by_vector(vector, top_k)`：返回 metadata 列表（含解析后的 `id`）
- 批量导入脚本（如大规模视觉描述）可另建脚本调用 `add_meme`；仓库内 `scripts/import_memes.py` 等为独立流程，不属核心启动路径

---

### 3.5 `api/` — REST API 层

**职责：** 提供 FastAPI 接口：Mini App 使用的 REST 路径均在 **`/api/*`** 下；**Telegram Bot API webhook** 单独挂在 **`POST /webhook/telegram`**（**无** `/api` 前缀，见 `main.py` 与 `webhook.py`）。Mini App 的 `/api/*` 接口统一返回 `{success, data, message}` 格式。

**鉴权：** 凡挂载在 **`prefix="/api"`** 下的路由（经 `api/router.py` 汇总）均在 `main.py` 层统一依赖校验：请求头 **`X-Cedarstar-Token`** 必须等于 **`config.MINIAPP_TOKEN`**（`.env` **`MINIAPP_TOKEN`**），否则 **401**。**`POST /webhook/telegram`** 由 `main.py` **单独** `include_router`，**不**走上述依赖，**不**要求 `X-Cedarstar-Token`（入站鉴权仍以 **`TELEGRAM_WEBHOOK_SECRET`** 等与 `api/webhook.py` 一致为准）。

**主要模块：**
- `webhook.py`：Telegram 入站；校验 `X-Telegram-Bot-Api-Secret-Token` 后后台 `process_update`（见 §3.2）。
- `config.py`：助手运行参数配置接口。`GET /api/config/config` 和 `PUT /api/config/config` 成功时，`data` 字段会包含 `_meta.updated_at`，用于前端展示配置的真实落库时间（UTC 时间，前端负责转为本地时区）。

**路由前缀映射：**

| 前缀 / 路径 | 模块 | 主要功能 |
|------|------|----------|
| **`POST` `/webhook/telegram`** | `webhook.py` | Telegram 服务器推送更新；**不经** `/api`；Secret-Token 须匹配 `TELEGRAM_WEBHOOK_SECRET` |
| `/api/dashboard` | `dashboard.py` | Bot 在线状态、记忆概览、批处理日志 |
| `/api/persona` | `persona.py` | 人设配置 CRUD + system prompt 预览 |
| `/api/memory` | `memory.py` | 记忆卡片 CRUD + 长期记忆 + `temporal-states` / `relationship-timeline`（长期记忆列表合并 Chroma 元数据，见下） |
| `/api/history` | `history.py` | 对话历史查询（过滤+分页） |
| `/api/logs` | `logs.py` | 系统日志查询（过滤+分页） |
| `/api/config` | `config.py` | 运行参数读写（含 `buffer_delay`、`chunk_threshold`、`telegram_max_chars`、`telegram_max_msg` 等，见 §5.7） |
| `/api/settings` | `settings.py` | API 配置 CRUD + 激活切换 + Token 统计 |

**Mini App 设置：存储与保存方式（速查）**

- **存储位置：** 均在同一 PostgreSQL 库（由 `memory/database.py` 的 `initialize_database()` 初始化，DSN 来自 `.env` 的 `DATABASE_URL`），**无**独立 `settings` 配置文件目录。与 Mini App 强相关表：**`config`**（`key` / `value` / `updated_at` 运行参数）、**`api_configs`**（多组 API：`name`、`api_key`、`base_url`、`model`、`persona_id`、`config_type`、`is_active` 等）、**`token_usage`**（`GET /api/settings/token-usage` 读取统计）。人设等另有 **`persona_configs`** 等表（见 §5.8）。
- **核心设置页**（`miniapp/src/pages/Settings.jsx`，路由 `/settings`）：**不是**「整页一个接口写死全部配置」。按配置行使用 **`GET` / `POST` / `PUT` / `DELETE /api/settings/api-configs`**；**`PUT /api/settings/api-configs/{config_id}`** 的请求体为 [`ApiConfigUpdate`](api/settings.py) 可选字段，**仅非 `null` 字段更新**（HTTP 为 PUT，语义接近 PATCH）；**`PUT /api/settings/api-configs/{id}/activate`** 切换激活；**`POST /api/settings/api-configs/fetch-models`** 拉模型列表；**`GET /api/settings/token-usage`** 周期统计。
- **助手配置页**（`miniapp/src/pages/Config.jsx`，路由 `/config`）：主按钮 **`PUT /api/config/config`** 提交与 [`api/config.py`](api/config.py) **`DEFAULT_CONFIG`** 键集合对齐的**整对象**，后端写回 **`config` 表**（`set_config` 逐键 `INSERT INTO ... ON CONFLICT DO UPDATE SET`）。**Telegram 回复分段**（`telegram_max_chars` / `telegram_max_msg`）另支持**仅含单键**的 **`PUT /api/config/config`**，与整页保存共用同一接口。

**边界：**
- API 层不包含业务逻辑，直接调用 `memory.database` 的方法
- `dashboard.py` 维护一个进程内共享的 `_bot_status` 字典，由 bot 的 `on_ready`/`on_disconnect` 事件写入
- **`GET /api/dashboard/status` 的模型信息：** `active_api_config` / `model_name` 来自 `get_active_api_config('chat')`，与 Settings「对话 API」Tab 的激活项及 Bot 对话路径一致（不包含摘要 API）
- `settings.py` 的 API Key 在返回时脱敏（只显示末4位）
- `memory.py` 手工长期记忆：`POST /longterm` 先写 ChromaDB（`doc_id` 形如 `manual_{uuid}`），成功后再写数据库；`DELETE /longterm/{id}` 先删数据库再删 ChromaDB，Chroma 步骤失败仅记日志、接口仍返回成功；`GET /longterm` 在每条记录上附加 `is_orphan`（`chroma_doc_id` 缺失时为 `true`，非数据库列），并按 `chroma_doc_id` 批量读取 Chroma 元数据附加 `hits`、`halflife_days`、`last_access_ts`（孤儿行三项为 `null`）
- `memory.py` 时效状态：`GET/POST /temporal-states`、`DELETE /temporal-states/{id}`（将 `is_active` 置 0）；`GET /relationship-timeline` 返回全表按 `created_at` 倒序（只读）

**✅ 已改动（2026-04-05）：** **`/api/persona`** 人设 CRUD 与预览已读写 **`persona_configs.user_work`**，与库迁移、Mini App Persona 页、`context_builder._build_system_prompt` 一致。

---

### 3.6 `miniapp/` — 前端管理界面

**职责：** 提供可视化管理界面，通过 REST API 与后端交互。

**技术：** React 18 + React Router + Vite，无 UI 组件库（纯 CSS）

**视觉（2026-04）：** Mini App 使用统一 **新拟态（Soft UI）** 规范：页面与卡片表面色 `#E8ECF0`，主/次文字 `#4A5568` / `#8A94A6`，强调色紫色 `#7C6BC4`、状态绿 `#48C78E`；**凸起**与**内凹**（输入框）阴影、圆角与间距等以 CSS 变量集中在 **`miniapp/src/styles/global.css`**（如 `--shadow-raised` / `--shadow-inset`、`--surface`），按钮类控件默认凸起阴影、**`:active`** 时切换为内凹；七页各自 `*.css` 与之对齐。

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

**Dashboard 页（`Dashboard.jsx` / `dashboard.css`）：** 挂载时并发请求 §3.5 三个控制台接口。顶栏为 Discord/Telegram 在线、**对话**侧激活配置名与模型（`/status`，与 `get_active_api_config('chat')` 一致）、批处理结论（由同页已拉取的 `/batch-log` 最近一条的 `step1_status`～`step5_status` 推导）。下方为跑批日历与记忆库概览；概览数据来自 `/memory-overview`，含 `chromadb_count`、`short_term_limit`、`chunk_summary_count`（今日微批摘要条数）、`dimension_status`（七维度圆点）、`latest_daily_summary_time` 等，具体字段以 `api/dashboard.py` 为准。样式层含核心 KPI 大字、今日日历高亮、维度 Tooltip 等（纯前端，不改变接口）。

**Settings 页（`Settings.jsx` / `settings.css`）：** 「对话 API」「摘要 API」「视觉 API」「语音转录 API」「**Embedding**」五个 Tab，列表分别请求 `GET /api/settings/api-configs?config_type=…`（`chat` / `summary` / `vision` / `stt` / `embedding`），切换 Tab 时重新拉取。首次迁移会在 `api_configs` 插入默认 **`config_type=embedding`** 行（名称「硅基流动 bge-m3」、`base_url`/`model` 预填、`api_key` 空、**已激活**），用户在 Mini App 中补 Key 即可。新增/编辑弹窗内可改 `config_type`；**保存成功后以表单中的类型为准**——若与当前 Tab 不一致则自动切换到对应 Tab 并加载列表。`POST`/`PUT` 允许的 `config_type` 含 `embedding`（表情包向量用，与 `stt` 同理独立激活）。Tab 样式见 `settings.css` 中 `.config-tabs` / `.config-tab` / `.embedding-type`。移动端（<768px）为竖向堆叠布局与 2x2 Token 网格。

**Config 页（`Config.jsx` / `config.css`）：** 与 `api/config.py` 的 `DEFAULT_CONFIG` 对齐：上方为通用运行参数（**滑块 + 数字步进**），底部 **「保存并立即生效」** 一次 `PUT /api/config/config` 写回当前页全部键。其下 **「Telegram 回复分段」** 为 `telegram_max_chars`（10–1000、步长 10）与 `telegram_max_msg`（1–20），控件布局与同页其它行一致；每项可点 **「保存此项」** 单独 `PUT`，请求体仅含该键（仍走同一接口）。`memory/database.py` 的 `migrate_database_schema` 通过 `_config_insert_defaults_if_missing` 为缺失行插入两键默认值 **50 / 8**（`INSERT OR IGNORE`，不覆盖已有值）。

**Persona 页（`Persona.jsx` / `persona.css`）：** 右侧 System Prompt 预览区使用 `position: sticky`（配合 `align-self: flex-start`、`max-height` 与预览正文区域内部滚动），主内容区纵向滚动时预览与「复制全文」仍留在视口内，便于对照长表单编辑。「系统规则」区块下含 **Telegram HTML 格式化** 提示（与 `bot/telegram_bot.py` 正文 `parse_mode=HTML` 一致）。

**✅ 已改动（2026-04-05）：** 表单与预览增加 **用户工作**（`user_work`），与 **`persona_configs.user_work`**、后端预览及 **`context_builder._build_system_prompt`** 对齐。

**Memory 页（`Memory.jsx` / `memory.css`）：** 四 Tab（记忆卡片、长期记忆、时效状态、关系时间线）。**外壳**：`.memory-container` 为 `height: calc(100vh - 80px)`（与主内容区上下各约 `20px` 的 padding 对齐）、`overflow: hidden`；Tab 栏下方 **`.memory-content-scroll-area`** 为 `flex: 1; min-height: 0; overflow-y: auto; scrollbar-gutter: stable`，**仅该区域纵向滚动**，避免整页高度随 Tab 切换跳变。各 Tab 根为 Fragment，**首子节点**统一 **`.memory-tab-header`**（`margin-top: 24px` 与 Tab 栏留白一致），标题为 **`h2.memory-tab-header__title`**，emoji 与正文分置于 **`span.memory-tab-header__emoji` / `span.memory-tab-header__title-text`**。长期记忆条目中 Chroma 元数据用 **`.memory-meta-chip`** 胶囊展示：`hits`、`halflife_days`、`arousal`（保留两位小数，历史数据无此字段时不显示）；`hits` 达到 `gc_exempt_hits_threshold` 阈值的记忆在正文右侧显示 **`.gc-exempt-badge`**「🔒 免删」徽章（阈值从 `GET /api/config/config` 读取）。顶部 Tab（**`.memory-tabs button.memory-tab`**）采用与全站一致的新拟态凸起/选中态，强调色与 §3.6「视觉」一致。

**History 页（`History.jsx` / `history.css`）：** 筛选区 **`.filter-controls-row`** 全宽；平台 **`.platform-tabs`** 在移动端使用 2x2 网格布局以适应长文字，**`.tab-button`** 不换行。列表卡片 **`.message-list-container`** 水平 **`padding: 24px 10px`** 使对话区贴近卡片左右约 10px；内层 **`.history-chat-column`**（`max-width: 480px`，移动端 100%）**`padding-left/right: 0`**，**`.message-list`** 同样无额外左右 padding。消息气泡 **`width: fit-content`**、**`max-width: 70%`**（移动端 85%），随内容长短伸缩；**`.message-row.user-row`** **`justify-content: flex-end`** 用户气泡贴右，**`.message-row.assistant-row`** **`flex-start`** 助手贴左；内层避免 **`width: 100%`** 撑满行宽导致「中间一条」。气泡内正文统一左对齐，头部分角色对齐（移动端用户气泡头部为 row-reverse 对称）。**不改变** `/api/history` 参数与响应消费方式。

**API 根地址与请求封装：** 各页通过 `src/apiBase.js` 的 **`apiFetch(path, options)`** 调用后端（内部用 **`apiUrl()`** 拼 URL）。**`apiFetch`** 会为每次请求自动设置 **`Content-Type: application/json`** 与 **`X-Cedarstar-Token`**，令牌来自构建时环境变量 **`VITE_MINIAPP_TOKEN`**（未设置则为空字符串），须与服务器 `.env` 中的 **`MINIAPP_TOKEN`** 一致，否则 `/api/*` 返回 401。环境变量 **`VITE_API_BASE_URL`** 未设置或为空时 **`API_BASE_URL`** 为空字符串，URL 为相对路径 `/api/...`；**开发环境**下由 Vite 将 `/api` 代理到 `http://localhost:8000`。**生产构建**（`vite build`）会读取 `miniapp/.env.production` 等文件中的 `VITE_API_BASE_URL`，用于指向实际后端（公网域名或隧道 URL）；隧道域名变更时只需改环境变量并重新构建，勿在页面中硬编码 `localhost:8000`。

**路由入口：** `src/router.jsx` 导出 `navItems` 与 `routes`，文件顶部 `import React from 'react'`（见 §6.11）。

**✅ 已修复（2026-04-05）：** **`miniapp/src/App.jsx`** 中 **`BrowserRouter`** 设置 **`basename={routerBasename()}`**（由 **`import.meta.env.BASE_URL`** 推导，与 **`vite.config.js` 的 `base`** 一致）。生产静态资源挂在 **`/app`** 时，无 basename 会导致路径 **`/app/`** 无法匹配路由 **`/`**，Telegram Mini App 打开白屏；设置后与 **`StaticFiles(..., html=True)`** 挂载前缀一致。

---

### 3.7 `services/` 和 `tools/` — 扩展层

- `services/wx_read.py`：微信读书集成（仅有版本号占位，无实现）
- `tools/meme.py`：**`search_meme`** / **`search_meme_async`** 调 `meme_store` 向量检索（**Telegram 有序段发表情走 `search_meme_async`**，以便 `await` 读库内 embedding 配置）；**`send_meme`** 为异步，需传入 Telegram `bot` 与 `chat_id`。不在 LLM 请求中注册为 tools；Telegram 在解析助手正文中的 **`[meme:…]`** 后调用（见 `bot/telegram_bot.py`、`bot/reply_citations.py`）
- `tools/weather.py`：天气查询工具（仅有版本号占位，无实现）
- `tools/location.py`：位置工具（仅有版本号占位，无实现）

占位项在根目录 **`README.md`** 中多已标注为「规划中」；`meme.py` 为已实现模块。

---

## 4. 模块调用关系（数据流向）

### 4.1 消息处理主流程

**入站：** Telegram 为 **`POST /webhook/telegram`** → `process_update` → 与下述相同的缓冲与生成链路；Discord 仅当 **`ENABLE_DISCORD=true`** 时经 Gateway 进入 `discord_bot`。

```
用户消息（Discord：可选 / Telegram：webhook）
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
  memory/database.py（保存用户消息 + AI 回复到 messages 表；`character_id` 一般为同次 `LLMInterface.character_id`；Telegram 反应见 §3.2）
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
cron（或同类）在运维约定时刻触发 —— 应与 `config.daily_batch_hour`（东八区整点，默认 23）一致
        │
        ▼
  python run_daily_batch.py（项目根目录；内部 `initialize_database` 后 `DailyBatchProcessor().run_daily_batch()`）
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
  │  Step 4：主 LLM 打分（`Platform.BATCH`）+ 全量 Chroma   │
  │    prompt 输出 score + arousal；metadata 含 arousal    │
  │    vector_store.add_memory / bm25 增量                │
  ├─────────────────────────────────────────────────────┤
  │  Step 5：Chroma GC（hits 豁免 + 衰减 + 未访问 + 无子节点）  │
  └─────────────────────────────────────────────────────┘
```

**手动触发与验收（以 `memory/daily_batch.py` / `run_daily_batch.py` 为准）**

- `DailyBatchProcessor` 仅 `__init__(self)`，**不接受**数据库参数；跑批内通过 `memory.database` 的模块级访问读写库。
- `await DailyBatchProcessor().run_daily_batch(batch_date)`：`batch_date` 为 `None` 时用东八区当天（与 cron 触发语义一致）。
- `trigger_daily_batch_manual(batch_date=None)`：同步封装，内部同样是 `DailyBatchProcessor()` + 事件循环里跑 `run_daily_batch`。

在项目根目录执行示例（`python` 指向已安装依赖的解释器即可）：

```bash
# 推荐：独立入口（与 cron 相同路径）
python run_daily_batch.py
python run_daily_batch.py 2026-03-21

# 跑「今天」
python -c "import sys, asyncio; sys.path.insert(0, '.'); from memory.daily_batch import DailyBatchProcessor; asyncio.run(DailyBatchProcessor().run_daily_batch())"

# 跑指定日（重跑 / 断点续跑验证）
python -c "import sys, asyncio; sys.path.insert(0, '.'); from memory.daily_batch import DailyBatchProcessor; asyncio.run(DailyBatchProcessor().run_daily_batch('2026-03-21'))"

# 与上等价的同步入口
python -c "import sys; sys.path.insert(0, '.'); from memory.daily_batch import trigger_daily_batch_manual; trigger_daily_batch_manual()"
```

```bash
# 查最近跑批状态（显式列名，对应五步 + 错误信息）
python -c "
import sys, asyncio
sys.path.insert(0, '.')
from memory.database import initialize_database, get_database
async def main():
    await initialize_database()
    db = get_database()
    rows = await db.execute_query('SELECT batch_date, step1_status, step2_status, step3_status, step4_status, step5_status, error_message, updated_at FROM daily_batch_log ORDER BY batch_date DESC LIMIT 3')
    [print(r) for r in rows]
asyncio.run(main())
"
```

```bash
# 抽样向量库 metadata（预期含 hits、halflife_days、last_access_ts 等，见 §3.4.5）
python -c "import sys; sys.path.insert(0, '.'); from memory.vector_store import get_vector_store; vs=get_vector_store(); r=vs.collection.get(limit=5, include=['metadatas']); [print(r['ids'][i], r['metadatas'][i]) for i in range(len(r['ids']))]"
```

### 4.3 Mini App 数据流

**CORS（`main.py`）：** 允许的来源与正则以源码中的 **`_CORS_ALLOW_ORIGINS`**、**`_CORS_PAGES_DEV_REGEX`** 为准；部署新前端域名或 Tunnel 时请在 `main.py` 中按需修改上述常量。

**静态 Mini App（`main.py`）：** 若存在目录 **`miniapp/dist`**，启动时将构建产物以 **`StaticFiles(..., html=True)`** 挂载到 **`/app`**，浏览器可直接访问同源的 **`/app`** 使用控制台；与 **`/api/*`**、**`/webhook/telegram`** 并列，互不替代。

**`/api/*` 鉴权：** 浏览器或前端发往 **`/api/...`** 的请求须带请求头 **`X-Cedarstar-Token: <MINIAPP_TOKEN>`**（与 §3.1、§3.6 一致）。**`POST /webhook/telegram`** **不**要求该头。

**Telegram 服务器 → 后端：** **`POST /webhook/telegram`**（**非** `/api`）直达 `api/webhook.py`，与下述 Mini App 的 `/api/*` 分流并列，**不**经过 `api/router.py` 的同一前缀树（实现上以 `main.py` 分别 `include_router`）。

```
浏览器（React Mini App）
        │  HTTP GET/POST/PUT/DELETE /api/...  +  X-Cedarstar-Token（须与 MINIAPP_TOKEN 一致）
        ▼
  main.py（FastAPI + CORS）
        │
        ├──  （可选）GET /app/... ──► miniapp/dist 静态资源（存在 dist 时）
        │
        ├──  Telegram Bot API ──► POST /webhook/telegram ──► api/webhook.py ──► bot.telegram_bot.process_update
        │
        ▼
  api/router.py（/api 路由分发）
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
  bot/discord_bot.py（仅 ENABLE_DISCORD）或 bot/telegram_bot.py
        │  每次请求动态 new LLMInterface()
        ▼
  llm/llm_interface.py._load_active_config()
        │  从 api_configs 表读取 is_active=1 的配置（含 persona_id）；`config_type` 为 `chat` / `summary` / `vision` / `stt` / `embedding` 时各自独立激活
        ▼
  使用新配置调用 LLM API；构造时同时确定 character_id（热更新生效，无需重启）
```

---

## 5. 数据库表结构概览

数据库：PostgreSQL（通过 `asyncpg` 连接，DSN 由环境变量 `DATABASE_URL` 指定）

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
| `message_id` | TEXT | 平台原始消息ID（Telegram 助手回复为本 bot 发出消息的 ID，供反应事件等对齐；历史数据可能为 `ai_*`） |
| `is_summarized` | INTEGER | 是否已摘要（0=未摘要，1=已摘要）；微批摘要成功后会批量置 1；**占位/兜底**（语音转录失败、贴纸 `（贴纸）`、视觉失败/超时 `image_caption` 等）在写入时亦置 1，以免计入「未摘要」条数 |
| `character_id` | TEXT | 角色/人设标识：主路径为与同次 `LLMInterface` 对应的激活 `chat` 行 `persona_id`（字符串）；**Telegram `media_type=reaction`** 为直接读库 `get_active_api_config('chat')` 的 `persona_id`，无效则用 **`DEFAULT_CHARACTER_ID`**（默认 `sirius`），不经 LLM 实例 |
| `platform` | TEXT | 平台标识（`discord` / `telegram`） |
| `thinking` | TEXT | 思维链内容（DeepSeek R1 等模型的推理过程） |
| `media_type` | TEXT | **按消息实际接收顺序**（缓冲条目顺序）**逗号拼接且去重**的媒体标记：`image`、`voice`、`sticker` 等可组合（如 `sticker,voice`）；**`reaction`** 为用户对助手消息点表情等事件，**不与**其它类型复合；纯文本可为 `NULL`。扩展方式：在 Bot 缓冲遍历中 append 新类型，在 `context_builder` 增加对应 `_format_*_part` |
| `image_caption` | TEXT | 图片说明 / 视觉模型生成的描述（可选） |
| `vision_processed` | INTEGER | 是否已完成视觉处理（0=待处理，1=已处理；默认 1） |

**索引：** `(session_id, created_at)`、`(is_summarized)`、`(session_id, is_summarized)`、`(is_summarized, vision_processed)`（`idx_messages_vision_batch`，供未摘要且待视觉处理等批量查询）

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

> 此表是 ChromaDB 的数据库镜像，用于 Mini App 展示，两者通过 `chroma_doc_id` 关联。

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
- `chunk_threshold`：微批处理阈值（条数）；`memory/micro_batch.py` 优先读库，缺省则用环境变量 `MICRO_BATCH_THRESHOLD`
- `short_term_limit`：Context 中最近原文消息条数；`memory/context_builder.py` 优先读库，缺省则用环境变量 `CONTEXT_MAX_RECENT_MESSAGES`
- `context_max_daily_summaries`：注入 `daily` 摘要条数（默认 5）；`context_builder` 优先读库，缺省则用环境变量 `CONTEXT_MAX_DAILY_SUMMARIES`
- `context_max_longterm`：长期记忆最终注入条数（默认 3）；`context_builder` 精排/同步路径截断
- `daily_batch_hour`：东八区日终跑批**目标整点**小时 0–23（默认 23）；供运维配置 **cron** 与业务文档对齐。**`schedule_daily_batch`** 若在进程内运行会在每次睡眠前读库刷新该值；**`run_daily_batch.py` / cron 路径不依赖进程内定时循环**，跑批本身仍读 `config` 表其它键（如 GC 阈值）
- `relationship_timeline_limit`：关系时间线注入条数（默认 3）
- `gc_stale_days`：Step 5 Chroma GC 闲置天数阈值（默认 180）
- `gc_exempt_hits_threshold`：Step 5 GC hits 豁免阈值（默认 10）；`hits` 达到此值的记忆无论衰减分多低都不会被物理删除
- `retrieval_top_k`：向量与 BM25 各路召回候选数（默认 5）
- `telegram_max_chars`：Telegram 正文分段提示词中的 **MAX_CHARS**（默认 50；`api/config.py` 校验 10–1000 且对齐步长 10）；`context_builder.format_telegram_reply_segment_hint()` 读库注入 system
- `telegram_max_msg`：同上 **MAX_MSG**（默认 8；校验 1–20）

**API 响应元数据：** `GET` / `PUT` `/api/config/config` 成功时，返回体中的 `data` 除上述键外另含 `_meta: { updated_at: string | null }`，值为 **`DEFAULT_CONFIG` 所含全部键**（含 `telegram_*`）在 `config` 表中的 `MAX(updated_at)`（ISO 8601 字符串，前端解析时需注意这是 UTC 时间，需转为本地时区），用于 Mini App「上次保存时间」；`_meta` 不是配置项，不参与 `PUT` 写回。实现：`memory/database.py` 的 `get_config_max_updated_at_for_keys`、`api/config.py` 的 `_payload_with_meta`。

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
| `user_work` | TEXT | 用户工作 / 职业等信息（迁移默认 `''`） |
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

**✅ 已改动（2026-04-05）：** 表新增列 **`user_work`**（`migrate_database_schema` 中 `ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS user_work …`）。**`api/persona.py`**（Pydantic 模型、预览拼装 **「【用户工作】」**）、**`miniapp/src/pages/Persona.jsx`**（表单字段与预览 **「工作：…」**）、**`memory/context_builder.py`** 的 **`_build_system_prompt`** 用户块（**`工作：…`**）已同步。

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
| `config_type` | TEXT | 配置类型（`chat` / `summary` / `vision` / `stt` / `embedding`） |
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
| `platform` | TEXT | 调用来源：`config.Platform` 常量，常见值 `discord` / `telegram` / `batch`（日终与微批摘要等）；可为空 |
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

**逻辑字段顺序**（与 `save_daily_batch_log` / 代码中的读写一致）即上表从上到下的顺序。**若库由较早版本的 SQLite 创建、后经 `ALTER TABLE` 追加 `step4_status` / `step5_status`，** 历史 SQLite 中 `SELECT *` 的**物理列顺序**可能为：`batch_date`，`step1_status`～`step3_status`，`error_message`，`created_at`，`updated_at`，`step4_status`，`step5_status`。迁移至 PostgreSQL 后应确认列顺序；验收与手工 `UPDATE` 时请写**显式列名**，勿依赖 `SELECT *` 的下标含义。

**历史数据（三步时代已全完成、升级后 step4/step5 仍为 0）：** 服务启动时 `migrate_database_schema` 会**一次性**执行等价于下面的 `UPDATE`，并在 `config` 表写入键 `backfill_daily_batch_step45_legacy_v1`，之后不再执行。可手工执行以下 SQL 修补：

```sql
UPDATE daily_batch_log
SET step4_status = 1, step5_status = 1
WHERE step1_status = 1 AND step2_status = 1 AND step3_status = 1;
```

---

### 5.13 `sticker_cache` — Telegram 贴纸描述缓存表

| 字段 | 类型 | 说明 |
|------|------|------|
| `file_unique_id` | TEXT PK | Telegram `Sticker.file_unique_id`（全局稳定指纹） |
| `emoji` | TEXT | 贴纸关联 emoji（可为空） |
| `sticker_set_name` | TEXT | 所属套装名 `set_name`（可为空） |
| `description` | TEXT | 视觉模型生成的短描述；失败时为 `（贴纸）` |
| `created_at` | DATETIME | 写入时间，默认 `CURRENT_TIMESTAMP` |

**建表：** `migrate_database_schema` 内 `_ensure_sticker_cache_table` 执行 `CREATE TABLE IF NOT EXISTS`，已存在则跳过。**访问：** `MessageDatabase.get_sticker_cache` / `save_sticker_cache` / `delete_sticker_cache`（均为 **`async`**，调用方 **`await`**）及模块便捷函数 `get_sticker_cache_row` / `save_sticker_cache_row` / `delete_sticker_cache_row`。

---

### 5.14 `meme_pack` — 表情包元数据表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | SERIAL PRIMARY KEY | 自增主键；Chroma metadata 中以 `sqlite_id` 字段引用（历史兼容名） |
| `name` | TEXT NOT NULL | 展示名 / 检索用文本来源之一 |
| `url` | TEXT NOT NULL | 图片或动图 URL |
| `is_animated` | INTEGER NOT NULL DEFAULT 0 | `1` 表示动图（发送侧用 `send_animation`），`0` 为静图（`send_photo`） |

**建表：** 与多张核心表一同在 `migrate_database_schema` 初始化阶段 `CREATE TABLE IF NOT EXISTS`。插入辅助：`MessageDatabase.insert_meme_pack`。

---

## 6. 结构问题与改进建议

### 6.1 ✅ 已修复：消息缓冲逻辑代码重复

**问题：** `bot/discord_bot.py` 和 `bot/telegram_bot.py` 中的消息缓冲逻辑（`_add_to_buffer`、`_process_buffer`、`buffer_locks`、`buffer_timers`、`message_buffers`）几乎完全相同，约 150 行代码重复。

**修复（2026-03-21）：** 新建 `bot/message_buffer.py`，`MessageBuffer` 类持有三字典与 `add_to_buffer()` / `_process_buffer()`；超时合并后调用 `flush_callback(session_id, combined_content, images, buffer_messages, text_for_llm)`。Discord / Telegram 各自保留 `_add_to_buffer` 薄封装及 `_flush_buffered_messages`。后续演进（2026-03-22）：支持图片入缓冲、多模态 Context、异步视觉描述与微批 `vision_processed` 门槛；**`begin_heavy` / `end_heavy` + flush 前等待** 缓解贴纸识图/语音 STT 慢于 `buffer_delay` 时的误拆分。详见 §3.2、§4.1。

---

### 6.2 ✅ 已修复：`character_id` 硬编码

**问题：** 在 `bot/discord_bot.py` 和 `bot/telegram_bot.py` 中，`character_id` 被硬编码为字符串 `"sirius"`，没有从 `api_configs` 关联的 `persona_id` 动态读取。

**修复（2026-03-21）：** `llm/llm_interface.py` 在 `__init__` 中根据 `_load_active_config()` 返回的激活行解析 `persona_id`，暴露实例属性 `character_id`（无则 `"sirius"`）。`get_active_api_config` 本身为 `SELECT *`，不增加查询次数。两个 Bot 在主对话 `save_message()` 时使用与同一次 `LLMInterface()` 调用对应的 `llm.character_id`（与「每次请求 new LLM」的热更新策略一致）。**补充（反应）：** Telegram `MessageReactionUpdated` 落库单独用 `get_active_api_config('chat')` + `DEFAULT_CHARACTER_ID`，不实例化 `LLMInterface`（见 §3.2、§5.1 `character_id`）。

---

### 6.3 ✅ 已修复 / 已演进：`daily_batch.py` Step 3 记忆卡片

**问题（历史）：** 日终记忆卡片更新曾缺失或仅为简单拼接，同维度内容重复堆叠；仅按 `get_memory_cards`（`is_active=1`）判断「是否有旧卡」时，批量软删后无法命中旧行；SUMMARY 模型若返回前置说明或非严格 JSON，维度解析易失败。

**当前行为（与 §3.4.4 Step 3 一致）：**

1. 从 `summaries` 表取最新一条 `summary_type='daily'` 的今日小传（Step 2 产出）
2. 从 `messages` 表查询当批日期的 `(user_id, character_id)` 列表（无记录时兜底 `default_user/sirius`）
3. 构建 Prompt，要求 SUMMARY LLM 按 7 个维度返回严格 JSON（`content` 或 `null`）
4. **解析 JSON：** 整段 `json.loads` → 失败则截取首个**平衡** `{...}`（含 \`\`\`json 块）→ 再回退贪婪正则；仍失败则 Step 3 报错退出
5. **Upsert：** `get_latest_memory_card_for_dimension` 取该用户/角色/维度最近一条（**含 `is_active=0`**）；有则 `_merge_memory_card_contents`（`_call_summary_llm_custom` → `generate_with_context_and_tracking`，`platform=BATCH`）合并去重，`update_memory_card(..., reactivate=True)`；无则 `INSERT`；合并 LLM 失败时 fallback 为追加式拼接
6. 单维度 `try/except + continue`，互不拖累
7. 维度分析仍走 `summary_llm.generate_summary`（内部为 `generate_with_context_and_tracking`，`platform=BATCH`，经 **chunk 式**前缀 + 任务正文包装，并传入 `char_name`/`user_name`）；**合并**走 `_call_summary_llm_custom`（**不经** chunk 外壳；含人物前缀与逻辑合并文案，**无** prompt 内 400 字上限，见 §3.4.4）

---

### 6.4 ✅ 已修复 / 已演进：`daily_batch.py` Step 4 小传打分

**问题（历史）：** 小传归档前价值打分路径曾把 `self.llm.generate(prompt)` 的返回值（`LLMResponse`）误当作字符串做正则，应先取 `.content`。

**修复与后续：** 已改为先使用 `score_text = score_response.content` 再匹配；当前实现为 **`_step4_archive_daily_and_events`** 中 `score_text, _thinking = self.llm.generate_with_context_and_tracking([{"role":"user","content":prompt}], platform=Platform.BATCH)`（返回 `(str, Optional[str])`，打分仅用正文），并异步写入 `token_usage`。**演进（2026-04）：** 打分用 **user `prompt`** 在任务正文前加 **`_persona_dialogue_prefix()`**（激活人设 `char_name`/`user_name`，见 §3.4.4）。

---

### 6.5 ✅ 已修复：`api/history.py` / `api/logs.py` 全量加载后内存过滤

**问题：** `get_history()` 接口调用 `db.get_all_messages()` 获取所有消息后在 Python 内存中过滤和分页，当消息量大时性能极差。`api/logs.py` 存在同样问题。

**修复（2026-03-21）：** 在 `memory/database.py` 中新增两个方法，将过滤与分页逻辑完全下推到 SQL 层：

- `get_messages_filtered(platform, keyword, date_from, date_to, page, page_size)`：对 `messages` 表使用 `WHERE` 条件过滤（platform 精确匹配、keyword 对 content/thinking 做 `ILIKE`、date_from/date_to 用 `created_at::date` 比较），`COUNT(*)` 获取总条数，`LIMIT/OFFSET` 分页，同时返回 `{total, messages}`。
- `get_logs_filtered(platform, level, keyword, page, page_size)`：对 `logs` 表同理，level 自动转大写后精确匹配，keyword 对 message/stack_trace 做 `LIKE`。

`api/history.py` 和 `api/logs.py` 改为直接调用上述新方法，删除了原有的全量加载、Python 内存过滤、手动排序和切片逻辑。过滤条件为空时不拼接对应 `WHERE` 子句。前端接口格式（`total / page / page_size / messages|logs`）保持不变。

**✅ 已修复（2026-04-05）：** `get_messages_filtered` 内将 **`date_from` / `date_to`** 从字符串 **`date.fromisoformat`** 转为 **`datetime.date`** 再绑定 SQL（见 §3.4.1），修复带日期筛选时 History 接口 **500**。

---

### 6.6 ✅ 已修复：`BM25Retriever` 初始化时索引为空

**问题：** `BM25Retriever._build_index()` 在初始化时将索引设为空列表，需要手动调用 `refresh_index()` 才能从 ChromaDB 加载数据。但 `refresh_index()` 只在日终归档时被调用，导致服务重启后 BM25 索引始终为空，直到下次日终跑批。

**修复（2026-03-21）：** 重写 `_build_index()`，在服务启动时直接从 ChromaDB 拉取全量文档并建立索引。ChromaDB 为空或连接失败时优雅降级为空索引，不抛异常、不阻断服务启动。

---

### 6.7 ✅ 已修复：`longterm_memories` 表与 ChromaDB 双写不一致风险

**问题：** `api/memory.py` 的 `create_longterm_memory()` 先写数据库再写 ChromaDB，如果 ChromaDB 写入失败，数据库中已有记录但 `chroma_doc_id` 为空，导致数据不一致。删除时也可能出现 ChromaDB 删除成功但数据库删除失败的情况。

**修复（2026-03-21）：**

1. **创建：** 先 `vector_store.add_memory()`（`doc_id` 使用 `manual_{uuid}`），成功后再 `create_longterm_memory(..., chroma_doc_id=...)`；Chroma 失败则直接返回业务失败且不写数据库。若数据库写入失败则尝试 `delete_memory` 回滚 Chroma 中的同 `doc_id`。
2. **删除：** 先 `delete_longterm_memory`，成功后再删 Chroma；数据库失败仍返回删除失败；Chroma 删除失败仅 `warning` 日志，接口仍返回成功（避免向量残留影响接口语义，由运维/后续清理处理）。
3. **查询：** `GET /longterm` 对每条记录附加 `is_orphan: true/false`（`chroma_doc_id` 缺失即为孤儿行），供前端提示历史遗留数据。

---

### 6.8 ✅ 已跟进：`services/`、`tools/` 占位模块说明

**问题：** `services/wx_read.py`、`tools/weather.py`、`tools/location.py` 仅有版本号字符串，无任何实现。

**修复（2026-03-21）：** 根目录新增 `README.md`，在「规划中，暂未实现」一节明确列出上述三个文件，避免误读为已交付功能。根目录 **`test/`** 目录已移除（曾计划单独测试体系，当前仓库不再包含该路径）。

---

### 6.9 ✅ 已修复：`config.py` 中 `Platform` 常量未被完整使用

**问题：** `config.py` 定义了 `Platform.RIKKAHUB = "rikkahub"` 常量，但在代码中没有任何地方使用该平台。同时，两个 bot 中仍有直接写字符串 `"discord"` / `"telegram"` 的地方，没有统一引用 `Platform` 常量。

**修复（2026-03-21）：** 在 `bot/discord_bot.py` 和 `bot/telegram_bot.py` 的 import 行补充了 `Platform`，并将所有 `save_message()` 调用中的 `platform="discord"` / `platform="telegram"` 字符串字面量全部替换为 `Platform.DISCORD` / `Platform.TELEGRAM` 常量（两个文件各 4 处，共 8 处）。

---

### 6.10 ✅ 已迁移：数据库从 SQLite 迁移至 PostgreSQL（asyncpg）

**背景：** 项目初期使用 Python 内置 `sqlite3`，`requirements.txt` 曾短暂引入 `psycopg2-binary` 后被删除。

**迁移（2026-04）：**
- `memory/database.py` 全面重写：`sqlite3` → `asyncpg`，所有操作改为 `async def`，使用连接池（`min_size=2, max_size=10`）。
- SQL 方言转换：`?` → `$N` 位置参数；`INSERT OR REPLACE` → `ON CONFLICT DO UPDATE`；`INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`；`datetime('now')` → `NOW()`；`IN (?)` → `= ANY($1::type[])`。
- 新增 `async def initialize_database()` 作为启动入口，从 `config.DATABASE_URL` 读取 DSN 后调用 `init_pool` 并建表。
- `asyncpg` 返回的 `datetime`/`date` 对象统一通过 `_norm` 转换为 ISO 字符串以保持与上层代码兼容。
- `config.py` 中 `DATABASE_URL` 改为 `str` 类型，未设置时返回空字符串。

---

### 6.11 ✅ 已修复：`miniapp/src/router.jsx` 使用 JSX 但未导入 React

**问题：** `router.jsx` 中使用了 JSX 语法（`<Dashboard />`），但文件顶部没有 `import React from 'react'`。在 React 17+ 的新 JSX Transform 下可以工作，但依赖构建工具配置，可能在某些环境下报错。

**修复（2026-03-21）：** 在文件顶部补充 `import React from 'react'`（位于页面组件 import 之前），与显式 JSX 用法一致，降低对自动 JSX Runtime 配置的隐式依赖。

---

### 6.12 ✅ 已修复：`Config.jsx` 加载失败静默兜底与重置说明

**问题：** `GET /api/config/config` 失败或返回非成功时，页面将 `DEFAULT_CONFIG` 当作已加载数据展示，用户误以为即数据库真实值；「重置默认值」与后端 `config.py` 环境默认值可能不一致，缺少说明。

**修复（2026-03-21）：** 失败时不在界面用本地默认值冒充服务端数据：顶部红色 `role="alert"` 错误区 +「重新加载」重试；成功拉取时剥离 `data._meta` 后合并参数键与 `DEFAULT_CONFIG`（见 §5.7、§7.2）。「重置默认值」的说明以悬停 Tooltip（`config.css` 中 `.config-reset-tooltip`）及按钮 `title` 呈现。详见 §7.2。移动端（<768px）配置项采用上下堆叠布局，释放文字与滑块宽度。

---

### 6.13 ✅ 已修复：日志中 httpx 打印 Telegram Bot API 完整 URL（Token 泄露）

**问题：** `python-telegram-bot` 经 httpx 发请求时，httpx（及底层 httpcore）在 **INFO** 会记录整行 `HTTP Request: POST https://api.telegram.org/bot<token>/...`，`cedarstar.log` 与控制台长期留存 Bot Token，属安全隐患且非单纯噪音。

**修复（2026-03-22）：** `main.py` 的 `setup_logging()` 在 `discord` / `telegram` / `urllib3` / `requests` 之外，为 **`httpx`** 与 **`httpcore`** 注册 `logging.Filter`：若消息含 `://api.telegram.org` 且级别低于 **WARNING**，则丢弃该条；WARNING 及以上仍输出，便于排查连接或 API 错误。历史日志若已含 token，需轮转或删除文件并视情况在 BotFather 轮换 token。

---

### 6.14 ✅ 演进：Telegram 独立代理 `TELEGRAM_PROXY` 与 PTB `[socks]`

**背景：** Discord 启动会向进程环境写入 `HTTP_PROXY`/`HTTPS_PROXY`。`python-telegram-bot` 使用的 httpx 默认 `trust_env=True`，会把 Bot API 请求也走同一 HTTP 代理，对 `api.telegram.org` 常出现 `ConnectError`（经代理 `start_tls` 失败）。关闭 `trust_env` 后直连，在国内等环境又易 `Timed out`。

**演进（2026-03-28）：** `config.TELEGRAM_PROXY`（`.env`）仅用于 Telegram：`HTTPXRequest(proxy=..., httpx_kwargs={"trust_env": False})`，与 LLM 的 `requests`、Discord 代理解耦。推荐 **SOCKS5** URL（与 Clash 混合端口或 SOCKS 端口一致）；`requirements.txt` 使用 `python-telegram-bot[socks]`、`httpx[socks]`。详见 §3.1 配置表、§3.2「Telegram Bot 特有」。

---

### 6.15 ✅ 已改动：运行日志 logrotate（宿主机）

**说明（2026-04-05）：** 在部署机新增 **`/etc/logrotate.d/cedarstar`**，对项目根目录 **`cedarstar.log`** 做 **`daily`** 轮转、**`rotate 7`**（保留 7 份）、**`compress`**、**`missingok`**、**`notifempty`**、**`copytruncate`**（与常见 Python 单文件日志进程配合，避免移动文件后进程仍写旧 inode）。具体路径与策略以机上该文件为准。

---

### 6.16 ✅ 已改动（2026-04）：微批 / 日终跑批 Prompt 注入 persona 称呼与模板分流

**背景：** 跑批链路中摘要与小传类任务缺少主对话里的 Char/用户显示名，易造成上下文断裂；部分任务套用「为对话生成摘要」的 chunk 模板与真实任务不符。

**实现概要：**
- **`memory/micro_batch.py`：** `fetch_active_persona_display_names`；`process_micro_batch` 入口读名；chunk 摘要 prompt 与对话行标签见 **§3.4.3**。
- **`memory/daily_batch.py`：** `run_daily_batch` 入口读名；Step1 / Step2 / 记忆卡片合并 / Step4 打分等路径见 **§3.4.4**；记忆卡片合并文案与取消 prompt 内字数上限见该节 **合并写回** 条。

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
3. 移动端（<768px）配置项采用上下堆叠布局，释放文字与滑块宽度。

**演进：** 助手运行参数与 `api/config.py` 的 `DEFAULT_CONFIG` 对齐，当前仅 **`short_term_limit`、`buffer_delay`、`chunk_threshold`** 三项（已移除仅落库、未接运行时的旧键）；其中 `short_term_limit` / `chunk_threshold` 分别由 `context_builder` / `micro_batch` 优先读库，缺省再回退环境变量（见 §5.7、§3.4.2、§3.4.3）。

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
| **Settings.jsx** | `GET /api/settings/api-configs?config_type=chat|summary|vision|stt|embedding`（按 Tab 过滤）、`POST` / `PUT` / `DELETE` / `PUT .../activate`、`POST .../fetch-models`、`GET /api/settings/token-usage`、`GET /api/persona`；保存配置后按返回表单中的 `config_type` 切换 Tab 或刷新当前列表（见 §3.6 Settings 页说明） |
| **Config.jsx** | `GET /api/config/config`、`PUT /api/config/config`（`data` 含 `_meta.updated_at`；失败时顶部错误提示 + 重试，见 §5.7、§7.2） |

---

### 7.5 ✅ 已修复：`router.jsx` 显式导入 React

**文件：** `miniapp/src/router.jsx`（路由与 `navItems` / `routes` 配置，非页面组件）

**说明：** 该文件内使用 JSX（如 `<Dashboard />`），此前未导入 React。已在顶部补充 `import React from 'react'`，与 §6.11 一致；构建侧仍可配合 Vite 的 React 插件使用。

---

*文档由代码自动分析生成，如有遗漏请以实际代码为准。*
