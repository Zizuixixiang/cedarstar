# CedarStar 项目架构文档 v3

v3.5 · 2026-05-25 更新 · 群聊引用作者持久化 · 实现以代码为准

> 本文档是 CedarStar 当前实现的主架构说明，已按代码重写，去除历史补丁式修订痕迹。

## 1. 项目概述

CedarStar 是一个具备长期记忆能力的 AI 聊天系统，支持 Telegram、Discord 与 Mini App 管理后台。系统围绕“短期消息 → 微批摘要 → 日终小传 → 长期记忆向量”构建分层记忆链，并通过 PostgreSQL、ChromaDB、BM25 与 LLM 工具调用完成上下文组装、对话生成与离线归档。

核心目标：

- 对话上下文稳定可控
- 长期记忆可追溯、可检索、可清理
- 日终跑批可断点续跑
- 配置热更新，无需重启即可生效

启动硬门槛：`main.py` 在初始化数据库前读取 `config.DEFAULT_CHARACTER_ID`（环境变量 `DEFAULT_CHARACTER_ID`），未配置则进程直接退出（fail-loud）。

## 2. 数据与配置

### 2.1 运行参数表 `config`

| 键名 | 说明 |
|---|---|
| `buffer_delay` | 消息缓冲延迟 |
| `chunk_threshold` | 微批摘要阈值 |
| `short_term_limit` | 最近原文消息条数 |
| `context_max_daily_summaries` | Context 中纳入最近 N 天的 daily 摘要；长期记忆召回会排除该窗口内的 `date`，避免与 daily 重复 |
| `context_max_longterm` | Context 中长期记忆注入条数 |
| `event_split_max` | Step 4 单日事件拆分上限 |
| `mmr_lambda` | 长期记忆 MMR 相关性权重 |
| `context_archived_daily_limit` | 长期召回命中较早日期时补充的 daily 概况上限 |
| `archived_daily_min_hits` | 同一较早日期召回事件数达到该阈值时优先补充 daily |
| `starred_boost_factor` | 被收藏长期事件的召回融合分乘数 |
| `rerank_blend_weight` | Rerank 成功路径融合权重，`rerank_score` 占比，默认 0.7 |
| `daily_batch_hour` | 日终跑批时刻，支持 0.0–23.5 |
| `relationship_timeline_limit` | 关系时间线注入条数 |
| `gc_stale_days` | Chroma GC 闲置天数阈值 |
| `gc_exempt_hits_threshold` | Chroma GC hits 豁免阈值 |
| `retrieval_top_k` | 向量 / BM25 各路召回数 |
| `telegram_max_chars` | Telegram 分段最大字数 |
| `telegram_max_msg` | Telegram 分段最大条数 |
| `group_chat_max_message_chars` | Telegram 群聊每段字数上限（`format_telegram_group_segment_directive` 注入 system：日常严守、专业解答/吐槽分析/安慰等可酌情单行略超；每轮最多 3 段；发送端按该值贪心装箱、末块可略超；合法范围 10–3800） |
| `send_cot_to_telegram` | 是否在 Telegram 展示思维链（`blockquote expandable`）；关则私聊与群聊均不发 |
| `send_cot_in_group_chat` | 群聊额外开关（默认关）；须 `send_cot_to_telegram` 也为真 |
| `idle_activity_enabled` | 是否启用 AI 自主活动（true/false） |
| `idle_activity_level` | 自主活动概率档位：low/mid/high（0.25/0.5/1.0） |
| `idle_activity_threshold_min` | 距离用户最后发言达到多少分钟后才有资格触发 |
| `idle_activity_cooldown_min` | 两次自主活动之间最小间隔（分钟） |
| `idle_activity_start_hour` | 自主活动允许开始小时（东八区，0-23） |
| `idle_activity_end_hour` | 自主活动允许结束小时（东八区，0-23） |
| `idle_activity_next_trigger_at` | AI 回复 `[NEXT_AT_HH:MM]` 写入的下次预约时间（ISO UTC）；有值且未到期则 tick 跳过；有值且已到期则走预约触发（仍受时段约束，触发前清空该键）；空则走 `threshold_min` + `cooldown_min` + 概率 |
| `stardew_autoplay` | 星露谷自动模式：为 true 时 idle 调度每 3 分钟触发，`check_and_trigger` 跳过普通 idle 条件并注入 `[STARDEW_AUTO]` 虚拟用户句；助手回复中含 `[STARDEW_STOP]` 时自动写回 false |
| `api_failover_fail_count_{id}` | 单条 `api_configs` 连续可转移失败次数；达阈值自动取消激活后清零 |
| `api_failover_all_failed_alert_latch_{config_type}` | 该类型 API 激活池「全失败」Telegram 是否已提醒（`1`=已发；恢复后清零，告警正文不入 `messages`） |
| `external_chunk_max_chars` | MCP 外部写入单条 content 最大字数，默认 2000 |
| `rerank_enabled` | 是否启用 SiliconFlow Rerank 精排，默认 true |
| `rerank_candidate_size` | rerank 候选集大小上限，默认 50 |
| `rerank_score_floor` | 非收藏事件的 rerank 分数阈值，默认 0.3；Mini App Config 可调范围 0.05-0.8，步长 0.05 |
| `rerank_starred_floor` | 收藏事件的 rerank 分数阈值，默认 0.15；Mini App Config 可调范围 0.05-0.5，步长 0.05 |
| `rerank_query_max_chars` | 构建 rerank query 的最大字符数，默认 300 |
| `rerank_query_turns` | 构建 rerank query 取最近几轮对话，默认 2 |
| `rerank_timeout_sec` | rerank API 超时秒数，默认 3.0 |
| `half_life_milestone` | milestone 类事件时间衰减半衰期（天），默认 1000 |
| `half_life_decision` | decision / emotional_shift 类事件半衰期（天），默认 200 |
| `half_life_default` | 其他事件类型半衰期（天），默认 60 |
| `tts_enabled` | 是否启用 TTS 语音输出，0/1 |
| `tts_speed` | TTS 语速，0.5–2.0，默认 0.95 |
| `tts_vol` | TTS 音量，0.5–2.0，默认 1.0 |
| `tts_pitch` | TTS 音调，-12–12，默认 0 |
| `tts_intensity` | TTS 情感强度，0–10，默认 0 |
| `tts_timbre` | TTS 音色相似度，0–10，默认 0 |

### 2.2 token_usage / tool_executions

- `token_usage` 现在除 `raw_usage_json` 外，还会持久化 `base_url`，用于按模型提供方与网关来源追踪 token 使用。
- `provider_cache_hit_tokens` 与 `theoretical_cached_tokens` 口径已经拆分：
  - `provider_cache_hit_tokens` 直接按上游写入的 `cache_hit_tokens` 统计
  - `theoretical_cached_tokens` 记录本轮请求里稳定前缀部分的输入量估算值（按 context 拼装顺序中前缀缓存边界内的内容计算）；观测 API 会以供应商实际命中值作为旧数据兜底下限，并按 prompt tokens 封顶
- `tool_executions` 的 Mini App 观测接口改为分页返回，包含 `items / total / limit / offset`，并压缩 `result_raw_preview` 以降低前端渲染成本。
- Observability 页面支持“本次”视图，聚合区默认展示最新一条 token usage；“最近调用（今日）”固定展示东八区当天全部调用。

### 2.3 `api_configs`

`api_configs.config_type` 允许：`chat`、`summary`、`vision`、`stt`、`embedding`、`search_summary`、`analysis`、`rerank`、`tts`。

- `chat`：日常对话
- `summary`：微批 / 日终摘要；摘要调用统一经 `await SummaryLLMInterface.create()`（`memory/micro_batch.py`）绑定 **当前激活** 的 `api_configs` 行（`api_key`、`base_url`、`model` 齐全才采用），否则回退 `.env` 的 `SUMMARY_*`。后台摘要有效超时为 `max(SUMMARY_TIMEOUT, SUMMARY_BACKGROUND_TIMEOUT)`，`SUMMARY_BACKGROUND_TIMEOUT` 默认 300 秒，用于日终跑批、微批摘要与 idle daily 预压缩等后台任务，避免被实时聊天的短超时截断。日终 `DailyBatchProcessor` 与微批共用该构造路径。
- `vision`：图片理解
- `stt`：语音转录
- `embedding`：向量嵌入（默认 SiliconFlow Qwen3-Embedding-8B，dim=1024；通过 `EMBEDDING_PROVIDER` 环境变量可切回智谱 embedding-3）
- `search_summary`：通用工具结果压缩（`tool_result_for_model` / `summarize_tool_result_for_context` 使用；`web_search` 本身只返回原始搜索拼接文本）
- `analysis`：日终 Step 4 事件聚类、描述与打分；不可用时回退 `summary`
- `rerank`：长期记忆精排（默认 SiliconFlow Qwen3-Reranker-4B；Mini App 可配置 base_url / model / api_key，默认 endpoint 为 `{base_url}/rerank`，若 base_url 已以 `/rerank` 结尾则直接使用）
- `tts`：语音合成（MiniMax T2A v2，国内域名 `api.minimaxi.com`）；`api_configs` 中需配置 `api_key` 与 `voice_id`

**激活池与 API 故障转移**（`memory/database.py`、`llm/llm_interface.py`、`api/settings.py`）：

- 同一 `config_type` 可有多行 `is_active=1`（Mini App「**加入激活池**」）；`PUT /api/settings/api-configs/{id}/activate` **不会**取消同类型其它行；`PUT .../deactivate` 取消激活。重新加入激活池时清零该行的 `api_failover_fail_count_{id}` 与对应类型的全池告警 latch。
- `get_active_api_configs(config_type)` 按 `id ASC` 返回全部激活行；`get_active_api_config()` 取其中 **id 最小** 的一条（仪表盘等「主配置」展示）。`chat` / `vision` 等经 `await LLMInterface.create(config_type)` 的路径会加载**整池**；`summary` 等仍主要用「主激活」单行语义。
- 所有经 `_post_with_api_failover` 的 LLM HTTP（`/chat/completions` 或 `/messages`、含流式）在**当前渠道 `_post_with_retry` 仍失败**且 `is_api_failover_eligible_exc` 为真时，按 id **顺序切下一激活行**再试；**任一次 HTTP 成功则不切换**。非可转移错误（如典型 400）立即失败，不切换。
- OpenAI tools 多轮请求中，`complete_with_lutopia_tool_loop` 会把上游返回的思维链归一值 `thinking` 回填为 assistant 消息的 `reasoning_content`，用于 DeepSeek thinking mode 在 tool calls 后的后续请求校验；`_openai_compatible_messages` 仅在 DeepSeek 相关请求中保留该字段，其他 OpenAI 兼容网关仍剥除以避免额外字段 400。若当前渠道在已产生 tool calls 后失败并故障转移到官方 DeepSeek，而既有 assistant tool_calls 历史来自前一渠道、没有 `reasoning_content`，则本次 DeepSeek payload 显式写入 `thinking: {"type": "disabled"}`，避免官方 DeepSeek 对无法补齐的跨渠道思维链做强制校验。
- 可转移错误：401、403、429、所有 HTTP 5xx（含 Cloudflare / CDN 常见 520-524）、读超时与连接异常等。单渠道内这些可转移错误先用**同一 key** 完成 `_post_with_retry`（最多 6 次、间隔 2s），这一组尝试全部失败后才给该 key 记 1 次连续失败并切下一条。
- 同一 `api_configs.id` 连续可转移失败达 **5 次**（`API_FAILOVER_FAIL_THRESHOLD`）→ 自动 `deactivate_api_config`；计数存 `config` 表键 `api_failover_fail_count_{id}`。该渠道任一次 LLM 成功则计数清零。
- **本轮激活池全部仍失败**：`bot/telegram_notify.send_telegram_main_user_text` 向 `.env` 的 `TELEGRAM_MAIN_USER_CHAT_ID` 发**一次性**提醒（告警正文**不入库** `messages`）； latch 存 `config.api_failover_all_failed_alert_latch_{config_type}`，任一渠道成功或重新激活后清零。**激活池为空**（仅回退 `.env`）时只打日志，不发 Telegram。`main.py` 启动时 `register_llm_failover_event_loop` 供 `asyncio.to_thread` 内 LLM 路径调度 DB/Telegram 副作用。

### 2.4 `persona_configs`

与 v3 当前实现对齐的重点字段：

- `char_name`
- `char_identity`
- `char_personality`
- `char_speech_style`
- `char_redlines`
- `char_appearance`
- `char_relationships`
- `char_nsfw`
- `char_tools_guide`
- `char_offline_mode`
- `user_name`
- `user_body`
- `user_work`
- `user_habits`
- `user_likes_dislikes`
- `user_values`
- `user_hobbies`
- `user_taboos`
- `user_nsfw`
- `user_other`
- `system_rules`
- `enable_lutopia`
- `enable_rcommunity`（rcommunity 论坛 MCP；还须环境变量 **`RCOMMUNITY_MCP_TOKEN`**）
- `enable_weather_tool`
- `enable_weibo_tool`
- `enable_search_tool`
- `enable_x_tool`
- `enable_xhs_tool`（与部署环境 `ENABLE_XHS_TOOL` 同时为真才注册）
- `enable_ai_news_tool`（`get_ai_news` / AI HOT；与部署环境 `ENABLE_AI_NEWS_TOOL` 同时为真才注册）

补充：**`web_fetch`** 不是 `persona_configs` 的列；仅部署环境 **`ENABLE_WEB_FETCH_TOOL`**（默认 true）控制是否注册，见 **§4.2.2**。**通用自定义 MCP** 同样不是人设字段；部署总开关为 **`ENABLE_CUSTOM_MCP`**，具体 server / tool 开关存 `mcp_servers` 与 `mcp_tools`，见 **§4.2.7**。

### 2.5 辅助表

| 表 | 说明 |
|---|---|
| `sticker_cache` | Telegram 贴纸元数据缓存（file_unique_id → emoji / description），用于贴纸消息的文本化摘要 |
| `group_chat_state` | 群聊状态（chat_id → round_count），跟踪双 bot 接力步数；与 `config.group_chat_max_rounds` 等配合，详见 **§2.6** |
| `model_favorites` | Mini App 配置页按供应商收藏的模型列表（base_url + model 唯一） |
| `sensor_events` | 外部传感器事件 ingestion（event_type + JSONB payload） |
| `autonomous_diary` | 自主日记条目（title / content / trigger_reason / tool_log） |
| `daily_batch_log` | 日终跑批每步状态跟踪（batch_date 主键，step1–5 status，retry_count） |
| `meme_pack` | 表情包配置（name / url / is_animated） |
| `pending_approvals` | 写入操作审批队列（详见 8.5） |
| `transactions` | 零花钱流水（收入/支出、分类、余额快照、审批预留字段） |
| `pocket_money_config` | 零花钱配置（月额度、下月额度、年化利率） |
| `pocket_money_job_log` | 零花钱日任务执行日志（按日期+任务类型+character 唯一） |
| `game_sessions` | 游戏模式 session（game_type / display_name / 规则 prompt / state_json / config_json / participants / state_mode / summary / ended_at） |
| `game_turns` | 游戏模式逐轮记录（session_id / turn_idx / turn_data），按 session + turn_idx 建索引 |
| `mcp_servers` | CedarStar 通用自定义 MCP Server（name / transport / url / headers / enabled / trigger_keywords / allow_idle / idle_activity_prompt） |
| `mcp_tools` | 自定义 MCP 工具清单（server_id / name / description / input_schema / enabled / require_approval；`input_schema` 为 MCP `inputSchema` JSON；`require_approval` 本轮仅存储） |

### 2.6 Telegram 双实例、共享群表与接力计数（以 `bot/telegram_bot.py` 为准）

**双实例数据库与向量（勿写成「共用同一 PostgreSQL 库」）**：

| 实例 | 进程端口 | `DATABASE_URL`（主库） | `SHARED_GROUP_DB_URL`（群聊） | `CHROMA_COLLECTION_NAME`（示例） |
|---|---|---|---|---|
| CedarStar / Sirius | 8000 | 通常 **`cedarstar_db`** | 通常 **`cedarclio_db`** | `cedarstar_memories` |
| CedarClio / Clio | 8001 | 通常 **`cedarclio_db`** | 通常 **`cedarclio_db`** | `cedarclio_v2` |

主库各自存私聊 `messages`、记忆表、`mcp_servers` / `mcp_tools`、`persona_configs`、`config` 等；仅 **群聊消息表** 通过 `SHARED_GROUP_DB_URL` 共享（常与 Clio 主库同 database）。Chroma 多为同一 HTTP 服务、不同 collection。`memory/database.py` 的 `migrate_database_schema` 在**各实例连接的主库**上执行；涉及 DDL 时需 **cedarstar 与 cedarclio 进程各重启一次**（或分别在 `cedarstar_db` / `cedarclio_db` 上确认迁移）。

游戏模式同样存各实例主库：`messages.game_session_id` 可关联当前游戏 session；`config.active_game_session_id` 为空表示普通模式，有值表示当前启用游戏模式。`migrate_database_schema` 会幂等创建 `game_sessions` / `game_turns`、补 `messages.game_session_id` 并插入默认空指针。

- **环境变量（`.env`）**：`DATABASE_URL` 为当前实例主库；`SHARED_GROUP_DB_URL` 指向存放 `shared_group_messages` 的 PostgreSQL database（可与主库同 **服务器实例**，database 名可不同）。CedarStar ↔ CedarClio 群聊 HTTP relay 使用 `TELEGRAM_GROUP_PEER_RELAY_URLS`、`TELEGRAM_GROUP_PEER_RELAY_TOKEN`、`TELEGRAM_GROUP_PEER_RELAY_APP_ID`（见 `config.py`）。Context 侧 Telegram 私聊/群聊交叉原文依赖 `TELEGRAM_MAIN_USER_CHAT_ID` 与可选的 `TELEGRAM_CONTEXT_GROUP_CHAT_ID`（单群时可由 `memory/database.py` 的 `get_unique_shared_group_chat_id_for_context()` 自动推断），见 **§3.1** chunk 小节。
- **`group_chat_state.round_count`**：存主库表 `group_chat_state`。`group_chat_max_rounds`、`group_chat_silent_mode`、`group_chat_interject_enabled` / `group_chat_interject_probability` 等在 **`config` 表**（Mini App「助手配置」）。对端 bot 的 Telegram 入站或 HTTP relay 在接话路径上 **`increment_group_chat_round_count`**；**真人用户**在群内产生「新一句」时 **`set_group_chat_round_count(chat_id, 0)`**，覆盖：群聊纯文本入口、`voice`/`sticker`/`photo` 分支、`document`/`video`/`video_note`/`animation` 分支（`MessageHandler` 已注册这些类型），以及缓冲路径**首次**将用户写入 `shared_group_messages` 时（与纯文本入口语义一致，避免仅发语音/附件仍沿用旧计数导致 relay `signal_round_limited`）。
- **@ 到本 bot 的判定、随机插话与 peer 幂等**：使用 Telegram **`get_me().username` / `id`** 与正文匹配（`@username`、`tg://user?id=`、零宽字符清洗，`_shared_group_text_mentions_this_bot`）；**无硬编码**具体 @handle。对端 bot 明确 @ 本 bot 时必接话；未 @ 本 bot 时，只有最近用户句显式 @ 了另一名 bot 且未 @ 本 bot，才按 `group_chat_interject_probability` 随机插话。HTTP relay payload 带 `tg_message_id` 时按这条具体对端消息判定是否 @ 本 bot；旧 payload 无该字段时回退到最近一条对端消息。Telegram 入站与 HTTP relay 共用 `chat_id + sender + tg_message_id` 的短期接话幂等，并对同一 bot 连续分段消息设置短冷却，避免一条助手回复被 Telegram update 与 relay、或被多段发送重复触发 LLM。
- **回复引用上下文**：Telegram 缓冲路径在用户消息引用其它消息时通过 `_extract_reply_prefix()` 给本轮 LLM 注入一段不可见系统上下文。来源覆盖 `reply_to_message`、Telegram `quote` 与 `external_reply`；文本优先取被回复消息正文/图注，再取 quote 文本，仍无文本时按媒体类型标成 `[图片消息]`、`[贴纸消息]`、`[语音]` 等非文本描述。作者名优先取 `from_user`，并兼容频道/匿名 sender_chat 与 external reply origin。共享群表 `shared_group_messages.reply_to_author TEXT DEFAULT NULL` 只持久化被回复者作者名/标签，不保存被回复全文；后续从 DB 拼群聊 transcript 时，若该字段非空，会在正文前加一行 `[回复了 {reply_to_author}]` 供两名 bot 都能看到引用关系。
- **未实现入缓冲的类型**：当前对 `Document` / `VIDEO` / `VIDEO_NOTE` / `ANIMATION` 入 `handle_message` 后**仅执行接力清零并 `return`**，不触发 LLM 缓冲；若日后要支持对话，需另接入 `_add_to_buffer` 等链路。
- **群聊出站互斥（Clio/Sirius）**：`_telegram_deliver_ordered_segments` 在群聊入口经 `MessageDatabase.acquire_shared_group_send_lock` 在 **`shared_group_pool`**（`SHARED_GROUP_DB_URL`）上按 `chat_id` 持 `pg_try_advisory_lock`（同连接持锁至本轮 text/meme/voice 发完）；超时或未配置共享库时 WARNING 后无锁发送。正文多气泡由 `_group_chat_newline_send_segments` 按 `group_chat_max_message_chars` 贪心装箱（硬编码最多 3 条 Telegram 消息），再经 `_telegram_html_body_chunks` 做 4096 限长。

## 3. 上下文构建与召回

### 3.0 记忆归档与收藏字段

`memory_cards` 表新增：

- `manual_override`：BOOLEAN，默认 FALSE。为 TRUE 时，Step 3 的 structured output 跳过该维度该子项的覆盖，由人工手动维护。

`summaries` 表新增：

- `archived_by`：可空，引用归档该 chunk 的 daily summary id。daily 生成后 chunk 不删除，只写入该字段。
- `is_starred`：收藏标记，默认 false。
- `source`：VARCHAR(32)，默认 `internal`。MCP 外部写入的 chunk 标记为 `claude_web`。
- `external_events_generated`：BOOLEAN，默认 FALSE。标记该 chunk 的事件已在 `add_external_chunk` 时预生成，日终跑批跳过重复聚类。

`longterm_memories` 表新增：

- `source_chunk_ids`：JSONB，记录长期事件由哪些 chunk 合并而来，并通过 GIN 索引支持包含查询。
- `is_starred`：长期事件收藏标记，来源 chunk 任一被收藏则为 true。
- `source_date`：DATE，可空。记录事件对应的业务日期。内部日终 Step 4 事件会按 `batch_date` 写入；MCP 外部写入事件会按 `as_of_date`（未传则当天）写入。
- `theme`：VARCHAR(32)，事件主题标签（如 daily_life、work_career、milestone 等），由 Step 4b 生成。
- `entities`：JSONB 数组，事件涉及的命名实体（最多 5 个），由 Step 4b 生成。
- `emotion`：VARCHAR(32)，事件情绪标签（如 happy、sad、anxious 等），由 Step 4b 生成。
- `event_type`：VARCHAR(32)，事件类型标签（如 daily_warmth、decision、emotional_shift、milestone 等），由 Step 4b 生成。
- `metadata_manual_override`：BOOLEAN，默认 FALSE。为 TRUE 时 migrate 脚本跳过该行的 metadata 覆盖。

### 3.0.1 游戏模式 Context

`memory/context_builder.py` 的 `ContextBuilder.build_context` 会在普通模式拼装前读取 `config.active_game_session_id`。若该指针指向未结束的 `game_sessions` 行，则走独立的 `build_game_context`；若 session 不存在或已结束，会清空指针后回到普通 context。

游戏 context 是轻量分支：只注入核心人设字段（`char_identity` / `char_personality` / `char_speech_style` / `char_appearance`，不含 `char_nsfw`、`char_offline_mode`）、七维 `memory_cards`、最近 1 天 daily summaries、游戏规则与 `state_json`、按 `turn_idx` 正序的 `game_turns`、最近原文消息和当前用户消息。它**不包含**长期记忆召回、`temporal_states`、`relationship_timeline`、远古 daily 补充和 chunk summaries。`state_mode=per_turn` 时 system 要求每轮末尾输出完整 `[GAME_STATE]...[/GAME_STATE]`；`on_end` 仅在需要存档或结束时输出；所有游戏轮次都要求输出 `[GAME_TURN]...[/GAME_TURN]`。

`bot/game_mode.py` 的 `process_game_mode_response` 在发送给用户和落库前剥离 `[GAME_STATE]` / `[GAME_TURN]`，写入 `game_sessions.state_json` 与 `game_turns`。缺少 `[GAME_TURN]` 会打 WARNING；`per_turn` 缺少 `[GAME_STATE]` 也会打 WARNING；成功解析后会在用户可见正文末尾追加轻量状态提示。`save_message` 在活跃游戏存在时自动写入 `messages.game_session_id`，游戏消息仍照常进入微批摘要链路。

### 3.1 Context 拼装顺序

前缀缓存边界内（稳定部分，每次请求字节一致）：

1. system prompt + 指令块（优先级、引用、思考语言、工具口播；口播段与 Lutopia / rcommunity 等 OpenAI tools 对齐）
2. temporal_states
3. memory_cards
4. relationship_timeline
5. daily summaries

**daily 常规注入范围（实现口径）**：`_build_daily_summaries_section` 调用 `get_recent_daily_summaries(limit)` 时**不传 `session_id`**，即在东八区「最近 N 个日历日」窗口内查询**全局**所有 `summary_type='daily'` 行（N=`context_max_daily_summaries`），**不按当前会话过滤**；私聊、群聊、`daily_batch` 等各来源的 daily 会出现在同一块「每日摘要」中。

**Idle daily 预压缩（实现口径）**：`bot/idle_activity.py` 在调用 `build_context` 前读取最近 16 天 daily 窗口，保留最新一条 daily 原文不压缩；其余按日期聚合，最多取 15 天。每个日期以进程内缓存（key=`YYYY-MM-DD`）复用压缩结果，未缓存日期合并为一次摘要 LLM 调用（`SummaryLLMInterface.create()` + `batch_one_shot_with_async_output_guard`，与 chunk summary 共用 summary 模型配置），按“每天不超过 300 字、保留日期标题/关键事件/情绪节点/主题”压缩后写回内存缓存，再作为 `daily_summaries_override` 传入 `_build_daily_summaries_section`。压缩失败时仅回退为未压缩 override，不中断本轮自主活动；普通 Telegram / Discord 对话不传 override，仍走常规 daily 注入。

前缀缓存边界外（每次请求可能变化）：

6. 长期记忆召回
7. 远古 daily 概况补充
8. 未归档 chunk summaries

**chunk 常规注入范围（实现口径）**：`_build_chunk_summaries_section` 使用 `get_today_chunk_summaries()` **全局**取「内容日 ≤ 东八区今天」且默认 `archived_by IS NULL` 的 chunk，再按 `context_max_chunk_summaries` 截断；**不按当前 `session_id` 过滤**。

**Telegram 群/私聊交叉原文（实现口径）**：群聊侧需 `TELEGRAM_MAIN_USER_CHAT_ID` 以解析对端私聊 `telegram_{id}`。私聊侧对端群优先用 `.env` 的 `TELEGRAM_CONTEXT_GROUP_CHAT_ID`；未配置时若 `shared_group_messages` 仅有**一个** distinct `chat_id`，则自动使用该群（单群部署）。在「今日对话摘要」块内于两类 chunk 之间插入对端近期原文摘录（条数与短期历史一致，总长度有上限）。当前为群聊 `telegram_group_*` 时顺序为：私聊 chunk → 私聊原文 → 群聊 chunk（当前群聊原文仍在下文「最近消息」）；当前为私聊 `telegram_*`（非 group）时顺序为：群聊 chunk → 群聊原文 → 私聊 chunk（当前私聊原文仍在「最近消息」）。无法解析对端或拉取失败时不插入。若实际注入了非空对端摘录，会在「# 今日对话摘要」标题下追加 **`TELEGRAM_CROSS_CHANNEL_PEER_DIRECTIVE`**（`memory/context_builder.py` 常量），提示模型区分当前会话与摘录来源。

9. 动态内容（当前时间、工具记录、结束语）
10. 最近消息
    - 其中会额外带入最近几条已摘要消息，作为 chunk→正常对话的衔接窗口，避免摘要边界过于突兀
    - **`telegram_group_*`（共享群表）**：`_build_recent_messages_section` 从 `shared_group_messages` 取近期行；每条正文前用方括号标说话人——**用户**为激活 chat 人设的 `user_name`，两名助手固定为 **`[Clio]`**、**`[Sirius]`**（与表字段 `sender` 一致）。为避免两名助手在 API 中均被标成同一 `assistant` role 导致指代混淆，这些历史行在 LLM **messages** 里一律 **`role=user`**，单条正文为「方括号标签 + 换行 + 原内容」；若共享行 `reply_to_author` 非空，正文前还会插入 `[回复了 {reply_to_author}]`，只作为 LLM context 中的引用关系提示。用户行仍注入发送时间等既有逻辑，助手行仍 `strip_lutopia_behavior_appendix`。system 在 `telegram_segment_hint` 时另含 **`format_telegram_group_segment_directive()`**（群聊分段/字数/最多 3 段，专业或情绪向场景可酌情略超每段字数）、**`TELEGRAM_GROUP_CONTINUATION_DIRECTIVE`**（群聊续话）与 **`TELEGRAM_GROUP_IN_CHARACTER_DIRECTIVE`**（禁助手腔/能力菜单，工具照常）。Bot 群聊用户轮次在 `combined_content` 末尾追加 **`TELEGRAM_GROUP_USER_TURN_HINT`**；接话/peer 信号 user 提示亦要求用人设口吻、禁助手腔（`bot/telegram_bot.py`）。「## 群聊摘要」标题下第一行由 **`_telegram_group_chunk_viewpoint_line()`** 说明方括号含义、本实例对应 Clio 或 Sirius（**`MessageDatabase._shared_summary_actor()`**，依据 `APP_NAME` / `TELEGRAM_GROUP_PEER_RELAY_APP_ID`）及摘要正文中「我」与人设 **`char_name`** 的对齐。对端群聊摘录 **`_build_telegram_peer_recent_for_system`** 中群聊行亦用 **`[Clio]` / `[Sirius]` / 用户称呼**，并同样还原 `[回复了 ...]` 引用行，不再以「用户/助手」泛称。
11. 当前轮消息：`_build_current_user_message()` 统一在末尾 current message 正文前加 **`【本轮最新消息】`**，再注入 `【当前时间：...】`。该标签不写死“南杉”，因为群聊用户输入、图片/语音缓冲、游戏模式与 peer bot 接话信号都复用同一 current-message 构造链路；模型应将它视为本轮最新触发内容，而非历史消息。

### 3.2 长期记忆召回

**`build_context`（Telegram / Discord 主对话默认使用）**：若调用方显式传入 `skip_vector_search=True`，则跳过整块长期记忆向量注入；跳过时本轮长期召回 trace 清空，后续远古 daily 概况补充也不会注入内容。未跳过时，主路径先读取 `rerank_enabled`：为 true 时优先调用 `_build_vector_search_section_async`，走 SiliconFlow / Mini App `rerank` API 精排链路；该调用异常时记录 warning 并回退 `_build_vector_search_section` 旧链路。为 false 时直接走旧链路。当前 `bot/idle_activity.py` 的自主活动路径调用 `build_context(..., skip_vector_search=True)`，因此 idle 不跑长期向量/BM25 召回；主对话路径默认受 `rerank_enabled` 控制。

**`_build_vector_search_section_async`（主路径在 `rerank_enabled=true` 时调用；`build_context_async` 也复用）**：长期记忆采用双路检索 + Rerank 精排 + 阈值过滤 + 记录自身衰减分融合 + MMR 多样性筛选：

1. **构建 rerank query**：取当前 session 最近 `rerank_query_turns` 轮对话，加角色前缀（南杉: / 小克:），截断到 `rerank_query_max_chars` 字符；若仅空白则回退为 `user_message`
2. **双路检索**：向量检索与 BM25 复用上一步构建的多轮 `rerank_query` 作为检索 query（包含最近对话与当前消息），各自召回 `retrieval_top_k`（默认 30），候选去重合并，上限 `rerank_candidate_size`（默认 50）。召回前按 `context_max_daily_summaries` 计算 `cutoff_date = 今天 - N 天`；Chroma `where` 仅按 `summary_type` 白名单过滤，不追加日期比较。向量结果与 BM25 结果在双路合并后统一按 metadata 中的 `date < cutoff_date` 字符串比较过滤，避免长期记忆与已注入的近期 daily summaries 时间段重叠。
3. **Rerank 精排**：调用激活的 `api_configs.config_type='rerank'` 配置；未配置时回退 `SILICONFLOW_API_KEY` + 默认 SiliconFlow Qwen3-Reranker-4B。每条候选得到 0-1 的 relevance_score；超时或异常时降级到旧的 `fuse_rerank_with_time_decay` 路径
4. **阈值拦截**（用 rerank 纯语义分，不混入加权）：
   - `is_starred=true`：score >= `rerank_starred_floor`（0.15）通过
   - 其他：score >= `rerank_score_floor`（0.3）通过
5. **加权排序**：`fusion_score = (rerank_blend_weight × rerank_score + (1−rerank_blend_weight) × norm_decay_score) × starred_boost`
   - `norm_decay_score` 由 `base_score × time_decay(halflife_days, arousal) × hits_boost` 在通过阈值过滤的候选内 min-max 归一化得到
   - `halflife_days` 优先取记录自身 Chroma metadata；缺失时按 `event_type` fallback 到 config 的 `half_life_*` 参数
   - `time_decay` 以 `source_date` / `date` 为事件发生日计算；缺失时回退 `created_at` / `last_access_ts`
   - `is_starred=true` → 衰减系数固定 1.0（不衰减），最后乘 `starred_boost_factor`
   - `rerank_blend_weight` 可在 Mini App Config 页调整，默认 0.7
   - `valence` 字段当前未实现（DB schema 无此字段），暂不纳入融合公式
6. **MMR 多样性**：按 `mmr_lambda` 做 MMR
7. **注入**：最终注入 `context_max_longterm` 条

默认召回白名单为：`daily`、`daily_event`、`manual`、`app_event`；在回溯语义下可纳入 `state_archive`。

### 3.3 工具执行摘要

`tool_executions` 记录每次工具调用的短摘要与原始结果。摘要生成统一走 `tools/lutopia.py` 的字数分支：原始结果 `<300` 字原样；`300–10000` 字调用 `search_summary`（不可用回退 `summary`，再失败回退旧截断）压到 200–300 字；`>10000` 字先压到 5000 字以内供本轮使用，再二次压到 200–300 字供跨轮上下文与落库摘要使用。DB 仍兜底截断摘要 1200、原始结果 50K；Mini App 观测页会对工具执行做分页展示，并默认仅展示压缩后的原文预览。日终跑批自动清理 7 天前的记录。`api/observability.py` 的 usage 归一化会按 `base_url` 区分 DeepSeek / OpenRouter / SiliconFlow 的缓存命中口径，并同时保留 provider 实际命中值与理论命中值。

### 3.4 远古 daily 补充

长期记忆召回完成后，系统会从命中事件的 `date/source_date` 中找出近期 daily 窗口外的日期。优先选择召回事件数不少于 `archived_daily_min_hits` 的日期；不足时按该日期召回事件最高分补足，最多注入 `context_archived_daily_limit` 条 daily 概况。

该块紧跟长期记忆召回块，固定说明为：`以下是长期记忆中涉及到的较早日期的概况补充，仅作为背景，不代表近期发生`。

### 3.5 chunk 注入过滤

Context 中的 chunk summaries 默认只注入 `archived_by IS NULL` 的记录，且内容日口径是 `COALESCE(source_date::date, created_at::date) <=` 东八区今天（不是仅“当天 created_at”）。已被 daily 归档的 chunk 保留在数据库中用于追溯、收藏和 Step 4 来源映射，但不重复进入常规上下文。

### 3.6 Context trace

`memory/context_builder.py` 会在每次真实构建 Context 后，在进程内记录最近一轮实际注入的记忆清单：

- `built_at`、`session_id`、`user_message_preview`
- `daily_summary_ids`
- `chunk_summary_ids`
- `archived_daily_summary_ids`
- `longterm_doc_ids`
- `memory_card_dimensions`：本轮注入的 `memory_cards.dimension` 列表（供 Mini App 记忆卡片页「本轮」标记）
- `rerank_scores`：每条注入长期记忆的 rerank_score、fusion_score、event_type

`GET /api/memory/context-trace` 返回这份最近一次 trace。该 trace 是 Mini App 排查入口，不参与模型上下文，也不持久化；服务重启后会清空。

## 4. 对话与工具

### 4.1 对话通路

- Telegram 通过 webhook 接入
- Discord 通过 bot gateway 接入
- 两者都先进入消息缓冲，再统一构建 Context 与调用 LLM
- **消息缓冲与锁**：`bot/message_buffer.py` 的 `MessageBuffer` 按 `config.buffer_delay` 防抖；在临界区内只取出并清空该 `session_id` 的队列、聚合正文，**释放** `buffer_locks[session_id]` **之后**再 `await flush_callback`（Telegram / Discord 注册的缓冲收尾）。避免单轮生成或 MCP 极慢时长时间持锁，导致同会话后续 `add_to_buffer` 阻塞、用户感觉「完全没反应」。
- Telegram 侧会根据是否携带图片选择 `LLMInterface.create(config_type="vision")` 或 `LLMInterface.create(config_type="chat")`，非缓冲生成路径则固定走 `vision`
- **API 激活池故障转移**：见 **§2.3**；对话/识图等 `LLMInterface.create` 路径在 `_post_with_api_failover` 中按报错切换下一渠道，成功不换。
- **LLM 上游 API 路由**（`llm/llm_interface.py` 的 `use_anthropic_messages_api`）：默认当 `LLM_API_BASE` 含 `anthropic` 时走 **`/v1/messages`** + `_parse_anthropic_response`；否则走 OpenAI 兼容 **`/chat/completions`**。环境变量 **`LLM_USE_ANTHROPIC_API=true`**（`true`/`1`/`yes`）时**强制**走 `/messages`（用于 Cedargate 等代理，以拿到 `cache_creation_input_tokens` / `cache_read_input_tokens`）。认证：Anthropic 直连用 **`x-api-key`**；`LLM_USE_ANTHROPIC_API` 且 base **不含** `anthropic` 时用 **`Authorization: Bearer {LLM_API_KEY}`**。CedarClio 部署通常开启该变量；CedarStar 默认不设。走 **`/chat/completions`** 时，若 base 为 **OpenRouter 上的 Claude** 或 **本机 CedarGate**（`127.0.0.1:8780` / `localhost:8780`），`_prepare_openai_payload` 会在 `messages` 中**保留**各 text 块的 **`cache_control`**，供代理翻译成 Anthropic 侧块级缓存；其余 OpenAI 兼容网关仍压成纯字符串以免 400。CedarGate / CLIProxyAPI 的 OpenAI→Claude 翻译层也必须把 `system[]` 与 `messages[].content[]` 的 text block `cache_control` 原样复制到 Anthropic `/v1/messages` body；若 request-log 显示 CedarClio 入站有 blocks 但上游 body 缺少 `cache_control`，优先检查该翻译层。走 **`/v1/messages`** 时，OpenAI tools schema 会转换为 Anthropic `tools`（`name` / `description` / `input_schema`），Anthropic `tool_use` block 会归一为内部 `tool_calls`，多轮工具循环可复用同一执行分发；若 `.env` 中 **`LLM_THINKING_BUDGET`** 为正整数，请求体会附带 **`thinking`**（`budget_tokens`）并**省略** `temperature`（与 Anthropic extended thinking 要求一致）。LLM POST 在当前渠道内会先对 401/403/429、所有 HTTP 5xx、超时与连接异常最多重试 5 次，再交给激活池故障转移；Embedding 客户端同样对 429 / 5xx 做重试。
- Idle Activity 由进程内 `schedule_idle_activity_check()` 定时检查（当前 10 分钟一次）：启用且处于允许时段后，`idle_activity_next_trigger_at` 已到期则预约触发（直接 `trigger_idle_activity`）；无预约时须满足空闲阈值、冷却与概率后再注入 `[IDLE_TRIGGER]` 并调用 `complete_with_lutopia_tool_loop`；该触发提示不落库。空闲阈值使用 `memory/database.get_latest_idle_user_activity()` 取 **主库 `messages` 真人用户消息**（`role='user'` 且 `user_id!='system'`）与 **共享群表 `shared_group_messages.sender='user'`** 两边的最新时间，私聊/普通通道和群聊都超过阈值才触发；触发提示会注明最后一条活动来自群聊或私聊/普通通道。助手回复正文为固定前缀 `【自主活动】` 加模型输出；Telegram `send_message` 与 `messages` 落库使用同一段字符串。拼接前若模型输出已带头衔，则循环剥除开头重复的 `【自主活动】`（`_strip_leading_idle_assistant_mark`），再统一加一层前缀，避免叠层。若本轮存在工具调用，会在助手消息写入后按 `session_id + turn_id` 回填 `tool_executions.assistant_message_id`，确保微批摘要能内联这轮工具结果。发送目标**仅 Telegram 私聊**：优先 `.env` 的 `TELEGRAM_MAIN_USER_CHAT_ID`（与审批回执同源）；未配置时从 `messages` 推断最近一条 `platform` 为 `telegram` 或空、`session_id` 为 `telegram_<正整数>` 且非 `telegram_group_*`、`channel_id` 为正整数字符串的记录（排除群/超群的负 chat id）。实现见 `bot/idle_activity.py` 的 `_resolve_idle_activity_telegram_dm_chat_id`。
- `[IDLE_TRIGGER]` 固定文案（`_IDLE_TRIGGER_TEXT`）中会列举可做的事：含 Lutopia 论坛（`lutopia_cli`）与在人设 **`enable_rcommunity`** 已开时的 **rcommunity** 工具；并含在已启用工具时调用 `get_ai_news` 浏览 AI HOT 资讯/日报的提示（与人设 + `ENABLE_AI_NEWS_TOOL` 一致），也会直接提示可用 `schedule_next_wakeup` 设置下次醒来时间。自主活动不注册、不提示小红书工具（`idle_activity` 内将 `enable_xhs_tool = False`；主对话与 Telegram 链接触发仍可用 `read_xhs_note`）。通用自定义 MCP 仅在部署开关 **`ENABLE_CUSTOM_MCP`** 为真且对应 `mcp_servers.allow_idle=1` 时注入；各 server 可选 `idle_activity_prompt` 拼入 trigger（见 **§4.2.7**）。`schedule_next_wakeup` 是内置工具，不受人设开关控制；`tool_oral_coaching` 在任一工具可用（含该内置工具）时开启，与实际 tools payload 对齐。
- **Idle context 裁剪**：自主活动在 `build_context` 前执行 daily 预压缩（见 §3.1），并传入 `skip_vector_search=True` 跳过长期向量/BM25 召回与远古 daily 补充，降低空闲自发消息的延迟与 token 压力；temporal states、memory cards、relationship timeline、压缩后的 daily、chunk summaries、最近消息和工具记录仍照常拼装。
- **自主活动 LLM 外层重试**（`bot/idle_activity.py`）：`complete_with_lutopia_tool_loop` 抛错且 `_is_retriable_idle_llm_exc` 时，最多再试 3 次，间隔 10s / 15s / 30s（覆盖 429、5xx、401/403、超时与连接类错误，与 §2.3 可转移集合对齐）。仍失败时向私聊发 `⚠️ 「自主活动」本轮触发失败：…`（需 `TELEGRAM_MAIN_USER_CHAT_ID`）；该提示**不入库** `messages`。自主活动同样走 `LLMInterface.create` 的 API 激活池与 `_post_with_api_failover`。
- **下次触发时间（可选）**：触发句末尾会注入当前北京时间，并提示模型二选一预约：优先可调用内置 `schedule_next_wakeup` 工具（`time_hhmm` 为北京时间 `HH:MM`，今天已过则顺延次日；`delay_minutes` 为 N 分钟后；两者都传时 `time_hhmm` 优先），或在最终回复末尾写 `[NEXT_AT_HH:MM]` 作为兜底文本标记。两条路径最终都写入 `idle_activity_next_trigger_at`（UTC ISO）。工具路径写入后会临时设置 config `idle_next_trigger_set_by_tool=true`；`trigger_idle_activity` 收尾时若检测到该 flag，会清空 flag 并跳过 `_apply_idle_next_trigger_at`，避免文本解析覆盖工具已写入值。`delay_minutes` 路径直接以当前 UTC 时间加偏移，不经过北京时间解析。`check_and_trigger` 在启用与**时段**（`start_hour`~`end_hour`，两路径共用）之后分支：**预约路径**——`next_trigger_at` 有值且未到期则跳过 tick；已到期则清空该键并直接 `trigger_idle_activity`（不看 `threshold_min`、`cooldown_min`、概率）；解析失败则清空并落入概率路径。**概率路径**——`next_trigger_at` 为空时，按空闲阈值、自主活动冷却与档位概率判定后触发。发给用户的正文会剥除 `[NEXT_AT_...]` 标记。

### 4.2 工具开关

人设可单独控制：Lutopia、**rcommunity 论坛 MCP**（`persona_configs.enable_rcommunity`；五类工具 `forum` / `forum_write` / `forum_interact` / `chat` / `profile`，OpenAI 侧 `rcommunity_*`）、天气、微博热搜、网页搜索、X (Twitter)、**AI HOT 资讯**（`get_ai_news`）。记忆工具（`tools/memory_tools.py`）无条件加载，不受人设开关控制。**游戏工具**（`tools/game_tools.py`：`game_start` / `game_end` / `game_update`）拆为两组：`OPENAI_GAME_START_TOOLS` 仅含 `game_start`，始终注入，让模型可在普通对话中创建并激活游戏；`OPENAI_GAME_ACTIVE_TOOLS` 含 `game_end` / `game_update`，仅当 `config.active_game_session_id` 指向未结束 session 时注入，用于结束当前游戏或补更遗漏的 state/turn。`game_start` 在已有活跃游戏时会返回错误。对应 system 后缀同样拆为 `game_start` 与 `game_active` 两段。**网页正文抓取 `web_fetch`**（`tools/web_fetch.py`）无人设开关，仅由部署环境 **`ENABLE_WEB_FETCH_TOOL`**（默认 true）控制是否注册；**自主唤醒预约 `schedule_next_wakeup`**（`tools/wakeup_tool.py`）为内置工具，始终注入工具循环和 Telegram 流式工具路径，供模型直接写入下一次自主唤醒时间；**通用自定义 MCP** 由部署环境 **`ENABLE_CUSTOM_MCP`** 与 `mcp_servers` / `mcp_tools` 控制，不新增人设字段。工具包说明由 `tools/prompts.py` 的 `TOOL_DIRECTIVES` + `inject_tool_suffix_into_messages` 注入；**工具调用前口播**由 `memory/context_builder.py` 的 `TOOL_ORAL_COACHING_BLOCK`（`tool_oral_coaching=True` 时）注入，与启用 tools 的请求对齐。

`get_ai_news` 还须环境变量 **`ENABLE_AI_NEWS_TOOL`**（`config.py` / `.env`，默认 true）为真，与人设列 **`enable_ai_news_tool`** 同时为真才注册；实现见 `tools/aihot.py`、`tools/prompts.py`（`OPENAI_AIHOT_TOOLS` / `AIHOT_TOOL_DIRECTIVE`）、`llm/llm_interface.py` 的 `create()`。

### 4.2.1 网页搜索（`web_search`，`tools/search.py`）

`web_search` 通过 Tavily Search API 拉取最多 **10** 条结果，直接返回按 `[序号] 标题 / URL / 摘要原文` 拼接的纯文本；失败返回纯文本 `暂时无法搜索`。`tools/search.py` 不再调用小模型、不再返回 `{"summary": ...}` JSON，也不再直接读取 `search_summary` 配置。搜索结果过长时统一交给 **§4.3 工具结果压缩层**处理。

### 4.2.2 网页正文抓取（`web_fetch`，`tools/web_fetch.py`）

对用户给出的 http(s) URL：`aiohttp` GET（总超时 10 秒、响应体上限 1MB），`trafilatura.extract` 抽取正文，工具返回 JSON（成功为 `text`，失败为 `error`；正文最多 4000 字符）。**部署开关**：`ENABLE_WEB_FETCH_TOOL`（`config.py`，默认 true）。**依赖**：`requirements.txt` 含 `trafilatura`、`lxml_html_clean`（lxml 6+ 需后者，否则 `import trafilatura` 失败）、`aiohttp`。OpenAI function schema 与 system 后缀见 `tools/prompts.py`（`OPENAI_WEB_FETCH_TOOLS`、`WEB_FETCH_TOOL_DIRECTIVE`）。执行入口：`llm/llm_interface.py` 的 `complete_with_lutopia_tool_loop`、`tools/lutopia.py` 的 `append_tool_exchange_to_messages`、Telegram `bot/telegram_bot.py` 的 `_telegram_stream_thinking_and_reply_with_lutopia`。`web_fetch` 写入 **`tool_executions`**，且在 Lutopia 流式路径上与 `web_search` / `get_ai_news` 等一致**不**进入 `execution_log` 旁白附录（`tools/lutopia.py` 排除列表）。Telegram 缓冲路径、Discord、idle 的「是否走带 tools 的 OpenAI 兼容流式 / `tool_oral_coaching`」与 **`ENABLE_WEB_FETCH_TOOL`** 做逻辑或，避免仅开启该工具时仍走无 tools 分支。

### 4.2.3 AI HOT 资讯（`tools/aihot.py`）

匿名请求 `https://aihot.virxact.com/api/public` 的 `items` / `daily` / `daily/{date}` / `dailies`；`User-Agent: CedarStar/1.0`。`dailies` 请求始终带 `take`（默认 10，硬上限 15）。`daily` / `daily_by_date` 格式化正文总长度上限由模块内常量控制（当前 10000 字符量级，超长截断）。`get_ai_news` 与天气/搜索类似写入 **`tool_executions`**，且不进入 Lutopia 流式 `execution_log` 拼附录（见 `tools/lutopia.py`）。

### 4.2.4 X (Twitter) 工具集

`tools/x_tool.py` 提供 13 个 OpenAI function calling 工具，通过 tweepy（OAuth 1.0a）调用 X API：

- 写入类：`post_tweet`、`like_tweet`、`unlike_tweet`、`retweet_tweet`、`unretweet_tweet`、`reply_tweet`、`follow_user`、`unfollow_user`
- 读取类：`read_mentions`、`search_tweets`、`get_timeline`、`get_user`、`get_followers`

`retweet_tweet`：仅 `tweet_id` 时为 X API 纯转推（无法在纯转推上附加文字）；可选参数 `comment`（非空）时为引用转推（`create_tweet` + `quote_tweet_id`），用于「带话的转发」。工具说明与 system suffix 见 `tools/prompts.py`（`OPENAI_X_TOOLS`、`X_TOOL_DIRECTIVE`）。

**API 与网页**：CedarClio 实例 OAuth 为 `@Clio_Cedar`。`reply_tweet`、带非空 `comment` 的引用转推仅当原帖 @Clio_Cedar 或 `tweet_id` 来自 `read_mentions`（互关/网页可回≠ API 可回）；`tweet_id`/`user_id` 须数字 ID。关联账号：`Shan_Cedar`、`Sirius_Cedar`、`Clio_Cedar`。见 `X_TOOL_DIRECTIVE`。

所有操作共享每日配额（`get_user` 除外），配额 key 为 `x_usage_YYYY-MM-DD`，存储在 `config` 表。写入类每次 +1，读取类按返回条数累加。内存 + DB 双写，进程重启后从 DB 恢复。Mini App 可通过 `/api/config/x-usage` 查询当日用量，通过 `/api/config` 设置 `x_daily_read_limit` 调整上限。

`post_tweet`、`reply_tweet`、`retweet_tweet`、点赞/取消点赞、转推/取消转推、关注/取关等 X 写操作会生成 `[系统内部记忆：...]` 内部旁白块，保留 tweet/user ID、URL、失败原因等关键字段；发给用户前会剥除该块，落库正文保留，供后续上下文记住“已经做过什么”。

### 4.2.5 小红书（链接预处理 + 工具）

- **依赖**：`xiaohongshu-cli`（`xhs` 命令）；Cookie 路径由环境变量 **`XHS_COOKIE_PATH`** 指向与 CLI 兼容的 `cookies.json`（进程内为每个 `APP_NAME` 在临时目录下构造 `HOME/.xiaohongshu-cli` 并 symlink 到该文件，以满足 CLI 固定路径约定）。
- **模块**：`tools/xhs_tool.py` — 当前对外仅注册 **`read_xhs_note`**（读单篇笔记正文与配图）；`tools/prompts.py` 的 `OPENAI_XHS_TOOLS` / `XHS_TOOL_DIRECTIVE` 已注释其余 5 个 function（`search_xhs` / `get_xhs_feed` / `get_xhs_user` / `like_xhs_note` / `favorite_xhs_note`），`execute_xhs_function_call` 仍保留实现以备恢复。读配额为 `config` 表 `xhs_read_usage_YYYY-MM-DD`，上限键 `xhs_daily_read_limit`（`/api/config`、`/api/config/xhs-usage`）。
- **人设开关**：`persona_configs.enable_xhs_tool`；还须环境变量 **`ENABLE_XHS_TOOL`**（`config.py` / `.env`，默认 true）为真，与人设列同时为真才注册工具。
- **Telegram**：在 `bot/telegram_bot.py` 缓冲生成路径进入 `build_context` 之前（`telegram_append_xhs_note_to_message`），对正文中的 `xhslink.com` / `xiaohongshu.com` 链接自动拉取首条笔记标题与正文并追加 `[小红书笔记]…`，配图最多 6 张注入多模态（不占日读配额）；笔记字段解析支持 HTML 形态的 `imageList` / `urlDefault` 与 feed 形态的 `items[0].note_card`；解析后打 `image_urls_count` 日志，CDN 下载失败为 WARNING；**仅当 `ENABLE_XHS_TOOL` 为真时执行**；失败仅打日志不阻断。
- **工具调用配图**：`read_xhs_note` 会下载最多 6 张配图，并用 `vision` 配置生成 `image_summary` 文本；OpenAI tool 调用结果默认不回传 base64 图像数据，只回传标题、正文、图片数量与视觉摘要，避免模型把工具 JSON 里的 base64 当普通文本而无法看图。Telegram 链接预处理仍保留原多模态图片注入。

### 4.2.6 rcommunity 论坛 MCP（`tools/rcommunity.py`）

- **人设开关**：`persona_configs.enable_rcommunity`；为真且环境变量 **`RCOMMUNITY_MCP_TOKEN`** 非空时，模型才有可能经工具链访问 rcommunity；**不会在首轮 `chat/completions` 之前**为 rcommunity 建立 MCP Streamable HTTP 会话。
- **连接**：默认基 URL `https://rcommunity-v2.rhysen.love/mcp`，完整 MCP URL 由 **`rcommunity_mcp_url()`** 拼为 `{base}?token={token}`；**不在** HTTP Header 或 MCP 参数中重复注入 token。可选 **`RCOMMUNITY_MCP_BASE_URL`** 覆盖基路径。传输实现为 **`mcp.client.streamable_http.streamablehttp_client`**（非 `sse_client`）；超时常量 **`RCOMMUNITY_MCP_HTTP_TIMEOUT_SEC`** / **`RCOMMUNITY_MCP_STREAM_READ_TIMEOUT_SEC`** / **`RCOMMUNITY_MCP_INIT_TIMEOUT_SEC`**（`initialize` 另包 `asyncio.wait_for`）。建连失败时 **`create_rcommunity_mcp_session` 会 `yield None`**，调用侧降级为单次建连或仅返回错误 JSON。
- **OpenAI 工具**：`OPENAI_RCOMMUNITY_TOOLS`（`rcommunity_forum` / `rcommunity_forum_write` / `rcommunity_forum_interact` / `rcommunity_chat` / `rcommunity_profile`）与 system 后缀 **`RCOMMUNITY_TOOL_DIRECTIVE`**（`tools/prompts.py`；内写各 MCP 工具允许的 **`action` 枚举**，与站方 `list_tools` 的 inputSchema 一致，减少模型自造 action 导致 `McpError`）。函数 `parameters` 在 JSON Schema 根级声明可选 ``request``（内层 ``additionalProperties: true``）且 **根级 ``additionalProperties: true``**，模型既可输出站方风格的**平铺**键（与 MCP ``call_tool`` 入参一致），也可使用 ``request`` 嵌套；执行前由 `_normalize_rcommunity_openai_args` 合并为单层 dict 再调用 MCP。全空参数在调用 MCP 前会被拒绝并返回可读错误。MCP 返回序列化见 `tools/mcp_utils.py` 的 `mcp_call_tool_result_to_json_str`（`tools/rcommunity.py` 顶部 import）。
- **调度（懒加载 MCP 传输）**：`llm/llm_interface.py` 的 `complete_with_lutopia_tool_loop` 与 Telegram `bot/telegram_bot.py` 的 `_telegram_stream_thinking_and_reply_with_lutopia` 使用 `contextlib.AsyncExitStack`：**首轮先调模型**；仅当本轮 `tool_calls` 中实际出现 Lutopia / rcommunity 工具名时，再 `enter_async_context(create_lutopia_mcp_session())` 与/或 `enter_async_context(maybe_rcommunity_mcp_session(True))`，避免 MCP 握手阻塞导致永远走不到上游 LLM。Lutopia 仍为 SSE；rcommunity 为 **Streamable HTTP**（`tools/rcommunity.py` 的 `streamablehttp_client`）。仍需 **`enable_rcommunity` 为人设真**才会为 rcommunity 建连（`maybe_rcommunity_mcp_session`）；仅配置 token 未开开关则不会建连。工具执行与回填经 **`append_tool_exchange_to_messages`**（返回每轮各工具原始 JSON 字符串列表；单工具异常捕获后仍追加 `role=tool`）；Discord / idle 走 `complete_with_lutopia_tool_loop` 的同一懒加载策略。对站方 ``call_tool`` 使用 ``asyncio.wait_for``（当前 **75s**）封顶，避免站方长时间不返回时拖死事件循环。**防卡死**：若连续 **3** 轮工具结果均为顶层 JSON 含 ``error``，则暂时 **`tools=None`** 并插入系统提示强制收束（`llm/llm_interface.py` 的 `tool_loop_json_payload_indicates_error_round`）。
- **观测与附录**：`tool_executions` 照常记录；`rcommunity_forum_write` 的发帖/回复/编辑/删除与 `rcommunity_forum_interact` 的置顶/收藏/点赞会生成 `[系统内部记忆：...]` 内部旁白块，保留 thread/reply/comment/post ID、URL、失败原因等关键字段；发给用户前剥除，落库正文保留。`web_search` / `web_fetch` / `get_ai_news` 等只读旁路工具不进入 Lutopia 行为附录。
- **探测脚本**：`scripts/list_rcommunity_tools.py`、`scripts/test_rcommunity_connection.py`（需已配置 token）。
- **DB 迁移**：`memory/database.py` 的 `migrate_database_schema` 含 `ALTER TABLE persona_configs ADD COLUMN IF NOT EXISTS enable_rcommunity ...`；手工可执行 `migrations/20260514_add_enable_rcommunity_persona.sql`。应用启动连库时会跑迁移，**一般无需单独执行 SQL**。

### 4.2.7 通用自定义 MCP（`tools/custom_mcp.py`）

部署总开关为 **`ENABLE_CUSTOM_MCP`**（`config.py` / `.env.example`，默认 true）。为 false 时 `build_openai_tools()` 直接返回空列表，也不会为自定义 MCP 建连接。

**数据模型**：

- `mcp_servers`：`id`（UUID 文本主键）、`name`、`transport`（`sse` 或 `streamable_http`）、`url`、`headers`（JSON 字符串，API 列表不回显明文）、`enabled`、`trigger_keywords`（JSON 数组字符串；NULL 或空表示普通对话每轮注入）、`allow_idle`（1 表示自主活动可注入）、`idle_activity_prompt`（TEXT，可选；自主活动 trigger 末尾拼接的用户手写说明，最长 500 字，API 归一化截断）。
- `mcp_tools`：`id`、`server_id`、`name`、`description`、`input_schema`（TEXT，MCP `list_tools` 的 `inputSchema` JSON；无则 OpenAI 侧回退通用 `request` 包装）、`enabled`、`require_approval`。`require_approval` 当前仅保留存储字段，不参与执行审批。列由 `memory/database.py` 的 `_ensure_custom_mcp_tables` 在启动迁移时 `ADD COLUMN IF NOT EXISTS`。

**同步与调用**：

- Mini App「工具中心 → MCP 管理」与 REST `api/custom_mcp.py` 维护 server、headers、开关和工具列表；路由挂载在 `/api/mcp`。列表页标题行右侧 **✦ 图标** 打开「自主活动」弹窗：`GET /api/mcp/servers/idle-activity-prompts` 返回 `enabled=1` 且 `allow_idle=1` 的 server，按 `名称：` + 文本框逐条编辑 `idle_activity_prompt`；编辑页在开启「允许自主活动」时也可写同字段。列表页名称与启用徽章同一行，同步/保存失败提示显示在对应卡片旁；工具列表页展示已同步参数的摘要（未同步 schema 时提示重新同步）。
- `sync_tools_from_server(server_id)` 按 server 的 `transport` 连接 MCP，执行 `list_tools()` 并 upsert 到 `mcp_tools`（含 `input_schema`）；新工具默认 `enabled=1`，已有工具保留开关状态。
- `build_openai_tools()` 优先用 DB 中的 `input_schema` 生成 OpenAI `parameters`（保留可选顶层 `request` 嵌套键供合并）；缺失或非 object schema 时回退通用 `request` 包装。
- OpenAI function 名格式为 `mcp_{server_id}_{tool_name}`。`dispatch_tool_call()` 解析前缀后按 `sse_client` 或 `streamablehttp_client` 建连并 `call_tool()`，headers 从 DB 的 JSON 直接注入请求头。
- `list_tools()` / `call_tool()` 超时统一 **75s**，返回经 `mcp_call_tool_result_to_json_str` 序列化为字符串。Telegram 工具状态会尽量显示为 `已调用{server_name}MCP（简短概况）`。

**懒注入规则**：

- `build_openai_tools(servers, user_message=None, is_idle=False)` 先过滤 `enabled=0` 的 server 与 tool。
- 普通对话：`trigger_keywords` 为空则每轮注入；非空时仅当最新用户消息命中任一关键词（大小写不敏感）才注入。
- 自主活动：只注入 `allow_idle=1` 的 server，不看关键词。
- **自主活动 trigger 文案**（`bot/idle_activity.py`）：在 `[IDLE_TRIGGER]` / 最近活动时间 / `[NEXT_AT_…]` 说明之后，若存在 `enabled=1` 且 `allow_idle=1` 且 `idle_activity_prompt` 非空的 server，追加固定段首 `【自主活动可用的自定义 MCP】` 与各行 prompt 正文（按库内顺序、换行拼接；不自动加 server 名称前缀）。

### 4.3 工具循环与多轮执行

`complete_with_lutopia_tool_loop` 采用「模型产出 tool_calls / Anthropic tool_use → 代码执行工具 → 将 tool 结果回填到 messages → 再次调用模型」的闭环方式。对模型而言，同一条工具链中的前序工具结果会保留在后续轮次上下文中，因此后续工具可以看到前一工具的执行结果。需要 Lutopia / rcommunity MCP 时，在**已得到含相应 tool_calls 的模型输出之后**再按需建立并复用各 MCP 会话（Lutopia：SSE；rcommunity：Streamable HTTP；见 **§4.2.6**）；通用自定义 MCP 则在 schema 注入阶段按关键词 / idle 规则过滤，实际执行时按工具名 `mcp_` 前缀临时连接对应 server（见 **§4.2.7**）。工具分发在循环内按函数名分支；`game_` 前缀走本地 `tools/game_tools.py`，`schedule_next_wakeup` 走本地 `tools/wakeup_tool.py`，都不建立 MCP 连接；单工具执行包 **try/except**，异常以 JSON ``error`` 回填。Lutopia、rcommunity 与自定义 MCP 对站方 ``call_tool`` 均使用 **75s** 超时封顶，避免 MCP 读流挂起拖死事件循环。连续多轮仅错误时禁用 tools 的逻辑见 **§4.2.6**。

- 外层工具循环存在轮次上限，`complete_with_lutopia_tool_loop` 默认最多 **8** 轮（Telegram 带工具流式路径同为 8）
- 单轮 SSE（Telegram 同步流式 / Anthropic 非流式预取）：`_telegram_stream_sse_with_sync_retries` 在 **`TELEGRAM_SSE_SYNC_MAX_ATTEMPTS`（默认 3）** 内可串联 **Guard 拒答**（`guard_refusal_abort` → `TELEGRAM_GUARD_PROMPT_APPEND`）与 **空正文**（`_telegram_effectively_empty_reply` → `TELEGRAM_EMPTY_REPLY_PROMPT_APPEND`）静默重试；有 `tool_calls` 或会走 Guard 兜底文案时不按空回复重试。流式 **读超时** 另由 `STREAM_READ_TIMEOUT_MAX_RETRIES` 重连，与此无关
- 工具结果会以 `role=tool` 形式回填上下文，并参与后续推理；结果压缩统一在 `tools/lutopia.py` 入口完成，不按工具名分支：原始结果 `<300` 字原样返回并原样作为跨轮摘要；`300–10000` 字本轮原样、落库/跨轮摘要异步调用 `search_summary`（不可用回退 `summary`，再失败回退旧截断）压到 200–300 字；`>10000` 字先调用同配置压到 5000 字以内供本轮使用，再以该结果二次压到 200–300 字供跨轮使用。落库保存通过后台任务执行，避免中等长度工具结果为了摘要阻塞主回复；超长结果只压一次并复用，避免重复压缩。
- Telegram 侧会对工具轮次中极短的中间前缀做抑制，避免误把残片当作正常口播

### 4.4 思维链展示

LLM 响应中的思维链字段会统一归一到 `thinking`，并兼容 `reasoning_content`、`reasoning`、`thoughts`、`<thinking>...</thinking>` 等格式。`split_thinking_and_content` 在整段以标准思维链标签开头且含闭合标签时，除提取思维链外会**保留闭标签后的正文**（避免同一段 `delta.content` 内正文被丢弃）。Telegram 是否展示思维链由 **`_telegram_should_send_cot`** 统一判定（`send_cot_to_telegram` + 群聊时 `send_cot_in_group_chat`）；流式 `delta_th` 与 `_telegram_finalize_sse_round_outcome` 收尾均遵守该开关——关闭时仍可从正文拆出推理合并回 `raw_content`，但**不**调用 `_telegram_finalize_thinking_blockquote`。在支持时使用可折叠 `blockquote expandable` 展示思维链。带 OpenAI tools 的流式路径（`_telegram_stream_thinking_and_reply_with_lutopia`）在多轮 `tool_calls` 之间：仅 `send_cot` 为真时每轮 SSE 进入工具前调用 `_telegram_finalize_thinking_blockquote` 定稿占位，避免流式纯文本 `edit_message_text` 永远无法套上可折叠 blockquote。分段 prompt（`format_telegram_reply_segment_hint` / `format_telegram_group_segment_directive`）含分点叙述、话题转折等强制分段场景说明。

同时，LLM usage 归一化（`_normalize_usage_for_storage`）会把 DeepSeek / 部分网关的 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`cached_tokens`、`cache_read_input_tokens` 合并成更稳定的缓存命中统计，并且不再因为多模态图片消息而强制关闭 OpenRouter cache control。**本机 CedarGate**（`127.0.0.1:8780` / `localhost:8780`）走 `/chat/completions` 时，另从 **`prompt_tokens_details.cached_tokens`** 读取缓存命中。走 `/messages` 时上游直接返回 Anthropic `usage` 字段；仅走 `/chat/completions` 时可能只有 `prompt_tokens_details.cached_tokens`（无 `cache_read_input_tokens` 列），Observability「读/写缓存」列会显示为 0。

### 4.5 TTS 语音输出

Telegram 私聊启用 TTS 后，助手回复会追发一条语音消息（MiniMax T2A v2）。实现要点：

- **Prompt 注入**：`tts_enabled=1` 时，`context_builder` 在 system prompt 末尾追加 `TTS_PROMPT_BLOCK`，指导模型使用 `(sighs)` / `(chuckle)` / `<#1.5#>` 等标签控制语气和停顿
- **文本过滤**：发送给用户的文字侧通过 `_strip_tts_markers()` 去掉所有 TTS 标签；发送给 TTS 引擎的原文保留标签
- **调用链路**：`_telegram_deliver_ordered_segments` → 收集全部 text 段 → `_send_voice_after_text` → `minimax_tts()` → `bot.send_voice()`
- **静默降级**：TTS 失败不影响文字消息，API key / voice_id 缺失时自动跳过
- **配置**：`api_configs` 表 `config_type='tts'` 存 `api_key` + `voice_id`；`config` 表存调参（速度、音量、音调等），Config 页滑块实时调节

## 5. 微批与日终跑批

### 5.1 微批摘要

未摘要消息达到阈值后，系统会生成 chunk 摘要并写入 `summaries` 表。摘要前注入关系锚点与激活记忆卡（`memory/micro_batch.py` 的 `_resolve_micro_batch_memory_prefix`）：`telegram_group_*` 会话注入南杉-Sirius/Clio 三人关系锚点，私聊会话注入当前 `user_name`/`char_name` 的一对一恋人关系锚点。**chunk 摘要用户 prompt 按群聊/私聊拆分**：`session_id` 以 `telegram_group_` 开头时使用群聊专用任务说明（多角色、话题主线、区分助手人设），否则使用私聊专用说明；两类 prompt 都要求按主题归纳同一话题的多轮交互，不逐条复述，仅在话题切换或有明确时间跨度时标注时间点，同时保留具体数字、ID、域名、IP、文件名、报错、决策和承诺等关键事实原文；日常互动须保留因果与情感语境（含正反示例，避免仅用形容词概括情绪）；`[系统通知]` 行处理与字数要求两套共用（`_build_chunk_summary_user_prompt`）。同会话上一条未归档 chunk 摘要会以“仅供衔接、不重复归纳”的块注入当前 prompt，帮助摘要承接上下文。工具执行结果按 `assistant_message_id` 内联到对应对话轮次中，与对话一起作为摘要输入，避免工具信息与对话脱节。摘要链路共享背景块由 `memory/prompt_background.py` 统一维护，避免多处文案漂移。上下文侧会额外保留少量已摘要消息作为过渡窗口。摘要 LLM 使用 **`await SummaryLLMInterface.create()`**（见 **§2.3 `summary`**），并使用后台摘要超时下限 `SUMMARY_BACKGROUND_TIMEOUT`。

chunk 生命周期：生成后长期保留；日终 Step 2 生成 daily 后不删除 chunk，而是写入 `archived_by=<daily_id>` 标记归档。归档日期口径与读取当天 chunk 一致，使用 `COALESCE(source_date::date, created_at::date)` 匹配业务日；这是为了兼容 `source_date` 字段加入前的旧 chunk，避免旧 chunk 进入 daily 后仍因 `source_date` 为空显示为未归档。

### 5.2 日终跑批流程

日终跑批由进程内 `schedule_daily_batch()` 按数据库 `daily_batch_hour` 配置定时触发（东八区），按业务日执行五步。无参触发时，当前时间早于配置触发时刻则处理前一天，达到或晚于触发时刻才处理当天；显式传入 `YYYY-MM-DD` 的手动补跑不受影响。`run_daily_batch.py` 仍保留为独立命令行入口（手动补跑 / 重试子进程调用）。

1. Step 1：到期 temporal_states 结算
2. Step 2：生成今日小传（按 `session_id` 分组；每条 daily 写入前在 prompt 中注入 `_daily_step2_session_framing(session_id)`，与 `micro_batch._build_chunk_summary_user_prompt` 对齐的群聊/私聊材料说明；`telegram_group_*` 视为群聊；日摘要沿用“按主题归纳并在主题内保持时间脉络”的口径，并要求材料要点不得漏写、须按各事件实际发生的时间先后顺序记载；日常互动要求保留能体现情感的因果逻辑与关键互动细节，避免把 chunk 重新扩写成逐句流水账；摘要 LLM 经 `_call_summary_llm_custom`，输出 `max_tokens` 下限 3000（`min(8192, max(base, 3000))`），HTTP 超时取 `SummaryLLMInterface.timeout`，即 `max(SUMMARY_TIMEOUT, SUMMARY_BACKGROUND_TIMEOUT)`；摘要链路统一注入 CedarStar/CedarClio 角色背景块）
3. Step 3：记忆卡片 Upsert + relationship_timeline
4. Step 3.5：从今日小传提取时效状态操作
5. Step 4：事件聚类 + 描述打分 + 长期事件入库
6. Step 5：Chroma GC

### 5.3 Step 4

Step 4 使用当天按时间顺序排列的 chunk 列表作为输入。聚类前先过滤掉 `external_events_generated=TRUE` 的外部 chunk（其事件已在 `add_external_chunk` 时预生成），仅对内部 chunk 执行聚类。聚类完成后，调用 `archive_external_chunks_by_daily()` 回填外部 chunk 的 `archived_by`。若当天仅有外部 chunk，则跳过聚类，直接回填。

Step 4a 聚类 prompt 在输入区会强制分段为 `【私聊】` 与 `【群聊】` 两块（若该类存在），再在块内按时间顺序列出 chunk，降低跨场景串话题聚类概率。

默认开启 `STEP4_SPLIT_MODE=True`，将旧的单次 LLM 调用拆成两段：

1. Step 4a：只做 chunk 聚类，输出 `[[chunk_id, ...], ...]`。
2. Step 4b：逐组生成事件描述、`score`、`arousal` 及 4 个 metadata 标签（`theme`、`entities`、`emotion`、`event_type`）。

4a 与 4b 都优先使用 `analysis` 配置；若 `analysis` 不可用，则回退 `summary` 配置，避免使用昂贵的 `chat` 模型。若 `summary` 也不可用，则不再隐式回退 `chat`，直接使用默认事件值继续。`STEP4_SPLIT_MODE=False` 时保留旧的单次调用路径，便于回滚。

Step 4 的事件拆分遵循：

- 至少 1 条事件
- 最多 `event_split_max` 条事件
- 4a 返回的 `chunk_ids` 会与当天输入 chunk 集合校验，非法 ID 会被过滤
- 某分组过滤后为空则丢弃；4a 连续 3 次失败后回退为每个 chunk 单独成组
- 4b 不返回 `chunk_ids`；事件的 `chunk_ids` 直接复制 4a 分组结果，降低 ID 幻觉风险
- 4b 对每个分组串行调用，连续 3 次失败则返回 `None`，该组丢弃并记 ERROR 日志
- 4a/4b 单次 LLM 调用均使用 600 秒超时，重试次数为 3 次
- 若全部 4b 分组失败，则使用默认事件继续，默认值为 `score=5`、`arousal=0.1`

Step 4 结果只写事件片段，不再写 daily 小传向量。普通 chunk 事件写入 `longterm_memories.source_chunk_ids`，并根据来源 chunk 的 `is_starred` 汇总出事件的 `is_starred`。同时，Step 4 写入 Chroma metadata 时会同步带上 `date` 与 `source_date`（均为当日 `batch_date`），供长期记忆列表日期展示与远古 daily 补充逻辑一致使用。

Step 1 结算出的 `_settled_temporal_snippets` 会在普通 Step 4 事件写入完成后追加处理：每条 snippet 不参与 Step 4a 聚类，而是包装成 `source=settled_temporal_state | source_date=<batch_date>` 的单条输入后直接送入 Step 4b，生成单独长期事件，再按 `daily_event` 写入 ChromaDB、BM25 与 PG `longterm_memories`，`source_date=batch_date`。这些事件不来自 chunk，因此不写 Chroma `source_chunk_ids`，PG 镜像中的 `source_chunk_ids` 置为 `NULL`。单条 4b 失败只跳过该 snippet 并记 WARNING，不影响其他 snippets 或当天跑批。

## 6. 记忆召回策略

**`build_context`（Telegram / Discord 主对话默认）**：长期记忆在 `rerank_enabled=true` 时先用 `_build_rerank_query` 生成多轮 query，并用同一 query 执行向量/BM25 双路检索与 Rerank 语义精排，再经阈值过滤、记录自身 `halflife_days`/`arousal`/`base_score`/`hits` 融合加权与 MMR；Rerank 配置优先来自 Mini App 核心设置里的 `rerank` 激活配置，失败则降级到本地 `fuse_rerank_with_time_decay` + MMR。`rerank_enabled=false` 或主路径异常回退时，旧链路同样以 `_build_rerank_query` 多轮字符串同时做向量和 BM25 检索，再本地融合排序。`_build_rerank_query` 的私聊逻辑保持取当前 session 最近 `rerank_query_turns` 轮并加 `南杉:` / `小克:` 前缀；**群聊 `telegram_group_*` 且本轮真人 user 消息少于 60 字时**，改从 `shared_group_messages` 读取最近记录，按 `created_at DESC` 贪心装填最近群聊上下文，标签使用 `[南杉]` / `[Clio]` / `[Sirius]`，历史片段总长上限 200 字，跳过与本轮正文相同的当前用户行后再追加本轮消息，最后仍统一按 `rerank_query_max_chars` 截断。`[群聊接话信号]` 与 `[另一名助手 ... 的发言]` 等 peer/内部触发不走短 user 分支；群聊真人 user 消息达到 60 字及以上时沿用原多轮 session query。调用方传 `skip_vector_search=True` 时跳过该块；当前 idle 自主活动路径使用该开关。收藏事件在 Rerank 链路中不参与时间衰减且阈值更低。记忆卡片用于稳定保存角色/用户的重要事实（支持 manual_override 跳过自动覆盖）；时效状态用于临时状态与动作规则。

## 7. Mini App 与配置管理

Settings 页管理 API 配置，其中 Reranker 独立为 `rerank` tab，支持新增、激活池、模型收藏与连通性测试；测试会向 `{base_url}/rerank` 发送 query/documents，若 Base URL 已以 `/rerank` 结尾则直接使用。Config 页管理运行参数，包括缓冲延迟、摘要阈值、最近原文条数、Rerank 融合权重、Rerank 召回阈值（`rerank_score_floor`）与 Rerank 收藏阈值（`rerank_starred_floor`）、Telegram 分段参数。助手配置页「自主活动」区块提供「星露谷模式」闸刀按钮，读写 `config.stardew_autoplay`，对应接口 `GET/POST /api/stardew/autoplay`；其下方「游戏模式」区块读取 `GET /api/game/active`，有活跃 session 时再读 `GET /api/game/sessions/{id}` 展示 `display_name` / `game_type`，并可用 `PUT /api/game/active {"session_id": null}` 停止当前游戏，创建/激活仍在 `/game` 页面或对话 `game_start` 中完成。工具中心提供「MCP 管理」入口（路由 `/mcp`），用于维护通用自定义 MCP server、headers、触发关键词（说明文案在输入框下方）、自主活动授权、工具同步与单工具启用状态；工具列表展示同步得到的参数摘要。

Settings 页（`miniapp/src/pages/Settings.jsx`）补充能力：

- **API 激活池**：同 `config_type` 可多行「已启用」；`PUT /api/settings/api-configs/{id}/activate` 加入激活池、`PUT .../deactivate` 取消激活（见 **§2.3**）。后端自动踢出失败 key 后，Settings 页会定时静默刷新当前 tab 以同步「已启用」状态；删除前须先取消激活。
- **API 配置测试**：`POST /api/settings/api-configs/{config_id}/test` 使用被测配置的 Key / Base URL / 模型（与是否激活无关）；对话类从 `config` 表键 `api_config_test_fixed_context_v1` 读取固定约 2 万字抽样（首次从 `messages` 拼入后缓存），尾部指令要求模型仅回复「收到」；`max_tokens=32`。LLM 调用优先 `generate_with_context`（非流式）；若上游返回 HTTP 400 或错误信息含 `stream_required`，则 fallback 为 `generate_stream` 同步拼 chunk 后仍返回完整 `reply`（实现见 `api/settings.py` 的 `_api_config_test_generate_reply`）。返回 `message_count`、`context_char_count`、`used_fixed_context`、`llm_ms` 等供前端展示。
- **拉取模型列表**：`POST /api/settings/api-configs/fetch-models`；编辑时可传 `config_id`，在 API Key 留空时使用库内已存 Key。
- **收藏模型**：`GET/POST/DELETE /api/settings/model-favorites`（按 `base_url` + `model`）；卡片外层可按 Base URL 切换收藏模型；编辑弹窗内收藏芯片支持 ↑↓ / 拖动排序。
- **UI 偏好**（存 `config` 表，不改 schema）：`GET/PUT /api/settings/ui-preferences?config_type=`，字段 `group_order`（配置卡片顺序）、`favorite_model_order`（按 base_url 的模型顺序 map）。

Memory 页 summaries 分页：翻页时 `loadSummaries(page)` 显式传入目标页，避免仅改 state 不拉取；`context_only=true` 时不展示分页条。

Mini App「时光机历史」（`/history`）：**私聊** tab 调 **`GET /api/history`**，服务端经 **`memory/database.get_messages_filtered`** 查主库 **`messages`**，固定排除 **`session_id` 以 `telegram_group_` 开头** 的 Telegram 群聊行（群聊与私聊同表存储，避免私聊列表混入群内消息）；**群聊** tab 调 **`GET /api/messages?type=group`**（`get_messages_by_type`，数据源为共享群消息表链路，与上者分离）。编辑/删除操作按当前 tab 分流：私聊仍走 `/api/history/{id}`，群聊走 `PATCH /api/messages/{id}` 与 `DELETE /api/messages/{id}`，由 `update_shared_group_message_by_id` / `delete_shared_group_message_by_id` 直接作用于共享群表主键；前端删除使用自定义确认弹窗，不依赖 `window.confirm`。

Memory 页的 summaries 列表支持对 chunk 点星收藏；收藏状态会通过 `PATCH /api/memory/summaries/{id}/star` 同步到引用该 chunk 的长期事件与 Chroma metadata。

Memory 页长期记忆列表的日期显示口径来自 Chroma metadata：优先 `date`，缺失时回退 `last_access_ts`。因此长期记忆日期相关修复需优先保证 Chroma metadata 完整（而不仅是 PostgreSQL 镜像表字段）。

Memory 页的 summaries 与长期记忆列表还支持“只看本轮”排查：

- summaries 调用 `GET /api/memory/summaries?context_only=true`，按最近一次 Context trace 中的 summary id 返回实际注入条目；可继续按 `summary_type` 限定 chunk / daily；可与 `session_kind=group|private` 组合，服务端在 trace id 命中后再按会话类型过滤。
- 长期记忆调用 `GET /api/memory/longterm?context_only=true`，按最近一次 Context trace 中的 Chroma doc id 返回实际注入条目；可继续按 `summary_type` 限定类型。
- `GET /api/memory/summaries` 可选 `session_kind`（`group` / `private`）：判定与 `memory/database.get_summaries_filtered` 一致（`session_id LIKE 'telegram_group_%'` 或 `is_group=1` 为群聊；否则为私聊侧）；列表项返回 `is_group`（bool）供前端展示。
- `memory/database.get_summaries_filtered` 分页排序为 `ORDER BY COALESCE(source_date::date, created_at::date) DESC, created_at DESC`，避免仅用 `source_date DESC NULLS LAST` 时大量 `source_date` 为空的旧 chunk 被挤到末页、前几页看起来像「只有今天」。
- 摘要 Tab 提供次级样式的「全部会话 / 群聊 / 私聊」筛选；与「本轮」同排的「群聊」标签仅在该条摘要属于群聊来源且在本轮 trace 中时显示。
- 记忆卡片 Tab 进入时会刷新 `context-trace`；卡片标题旁对 trace 中 `memory_card_dimensions` 命中的维度显示「本轮」。
- 前端用蓝色「本轮」标签标记最近一次 context 实际注入的摘要和长期记忆。

待审批页（`/approvals`）展示来自内部记忆工具写入与 MCP `api_admin` 管理写入工具的 pending approval 请求，用户可在此批准或拒绝。

游戏模式页（`/game`，`miniapp/src/pages/Game.jsx`）管理 `game_sessions` 和 `game_turns`：页头采用 MCP 管理页同款紧凑返回/刷新/新增操作；列表 Tab 按进行中 / 已结束分组展示，可新建、编辑、激活/停止、结束、删除 session；当前游戏 Tab 读取 `GET /api/game/active` 并展示活跃 session 的规则、参与者、`state_json` 与 turns。JSON 编辑使用 textarea + `JSON.parse` 校验；删除/结束使用自定义确认弹窗，不使用 `window.confirm`。后端 REST 前缀为 `/api/game`，含 `GET/POST/PUT/DELETE /sessions`、`PUT /active`、`GET/POST /sessions/{id}/turns`、`PUT/DELETE /turns/{turn_id}`，实现见 `api/game.py`。

## 8. MCP Memory Server

### 8.1 端点与鉴权

MCP 服务器以 ASGI 中间件形式挂载在 `/mcp/memory` 路径下，支持两个子路径：

- `/mcp/memory/{token}/sse` — SSE 连接（GET）
- `/mcp/memory/{token}/messages/` — POST 消息（由 MCP server 通过 SSE 自动下发给客户端，客户端无需手动构造）

鉴权方案为 URL 内嵌 token，原因：Claude.ai Custom Connector 不支持 Authorization header 认证（仅支持无认证或 OAuth 2.1 + DCR），故将 token 内嵌于 URL 路径中。

- token 格式：8–256 字符（`[^/]{8,256}`），通过环境变量配置
- 匹配 `MCP_WEB_READ_TOKEN` → 绑定 `web_read` scope（7 个只读工具）
- 匹配 `MCP_WEB_WRITE_TOKEN` → 绑定 `web_write` scope（7 个只读工具 + `add_external_chunk`）
- 匹配 `MCP_API_READ_TOKEN` → 绑定 `api_read` scope（7 个只读工具）
- 匹配 `MCP_API_TOKEN` → 绑定 `api_admin` scope（7 个只读工具 + 4 个管理写入工具）
- 都不匹配 → 404（非 401，避免攻击者通过响应码判断 token 存在性）

`root_path` 设为 `/mcp/memory/{token}`，使 MCP server 在 SSE `endpoint` 事件中自动下发含 token 的 messages URL，客户端后续 POST 请求自然携带 token。

### 8.2 工具清单

**读工具（所有 scope 可用）：**

| 工具 | 说明 |
|---|---|
| `search_memories` | 向量 + BM25 双路召回搜索长期记忆，支持 type_filter、source_filter。默认 type_filter 为 `daily_event`、`manual`、`app_event` |
| `get_recent_summaries` | 分页列出 summaries，支持按日期、天数、类型、来源、归档状态和 `keyword` 正文关键词过滤 |
| `get_memory_cards` | 获取记忆卡片列表，支持 `keyword` 按卡片正文过滤 |
| `get_temporal_states` | 列出 temporal_states（含已停用），支持 `days` 和 `keyword` 按状态内容/行为规则过滤 |
| `get_relationship_timeline` | 关系时间线，支持 `days` 和 `keyword` 按正文过滤 |
| `get_persona` | 获取单个人设配置详情 |
| `get_context_trace` | 最近一次 context 构建时实际注入的记忆清单 |

**写工具（`web_write` scope）：**

| 工具 | 说明 |
|---|---|
| `add_external_chunk` | 从网页端 Claude 整理的对话摘要写入记忆库。仅在用户明确说出「整理这个窗口」「写进记忆库」等显式指令时调用。支持 `as_of_date` 参数补录历史窗口，补录后需在服务器执行 `python run_daily_batch.py YYYY-MM-DD` 重跑当日 daily。LLM 拆分事件 → summaries 写 chunk 留底（`[APP端]` 前缀） → longterm_memories 逐条写事件（`summary_type=app_event`） → ChromaDB embedding → BM25 |

**管理写入工具（`api_admin` scope，走审批）：**

| 工具 | 说明 |
|---|---|
| `update_memory_card` | 修改七维记忆卡片，参数：persona_id, dimension, content |
| `update_temporal_state` | 修改时效状态，参数：id, content |
| `update_relationship_timeline_entry` | 修改关系时间线条目，参数：id, content |
| `update_persona_field` | 修改人设字段，参数：persona_id, field_name, content |

管理写入工具创建 pending approval，需用户在 Mini App 待审批页确认后才生效。

### 8.3 审计日志

所有 `call_tool` 调用（含鉴权失败）写入 `mcp_audit_log` 表：

| 列 | 说明 |
|---|---|
| `token_scope` | web_read / web_write / api_read / api_admin / `__auth__`（鉴权失败时） |
| `tool_name` | 工具名称，鉴权失败时为 `__auth__` |
| `arguments` | JSONB，工具参数 |
| `result_status` | success / error |
| `error_message` | 错误信息 |
| `approval_id` | UUID，关联 pending_approvals 表（管理写入工具有值） |
| `called_at` | 调用时间（东八区本地墙钟，TIMESTAMP） |

### 8.4 日志脱敏

uvicorn access log 中的 token 路径由 `_RedactMcpTokenFilter` 自动替换为 `***`。中间件在鉴权通过后会将 `scope[“path”]` 重写为内部路径（`/sse` 或 `/messages/`），原始路径保存在 `scope[“mcp_original_path”]` 中。

### 8.5 审批系统

MCP `api_admin` scope 的写入工具与内部记忆工具的写入操作均通过 `pending_approvals` 表排队，需用户在 Mini App “待审批”页确认后才生效。

`pending_approvals` 表：

| 列 | 说明 |
|---|---|
| `id` | UUID 主键 |
| `tool_name` | 操作名称 |
| `arguments` | JSONB，操作参数 |
| `arguments_hash` | 参数哈希，用于去重 |
| `before_snapshot` | JSONB，修改前快照 |
| `after_preview` | JSONB，修改后预览 |
| `requested_by_token_hash` | 请求来源 token 哈希 |
| `status` | pending / approved / rejected / expired |
| `created_at` | 创建时间（东八区本地墙钟，TIMESTAMP） |
| `expires_at` | 过期时间（东八区本地墙钟，TIMESTAMP） |
| `resolved_at` | 处理时间（东八区本地墙钟，TIMESTAMP） |
| `resolution_note` | 处理备注 |

审批 API 端点：

| 端点 | 说明 |
|---|---|
| `GET /api/approvals` | 列出审批记录，可按 `status` 过滤；可选 `limit`（1–100，省略 = 不限，Mini App 不传） |
| `GET /api/approvals/{id}` | 单条审批详情（同时挂在 `/api/memory/approvals/{id}` 别名下，供 `memory_get_approval_status` 工具调用） |
| `POST /api/approvals/request` | 创建审批请求（内部工具写入入口） |
| `POST /api/approvals/{id}/approve` | 批准 |
| `POST /api/approvals/{id}/reject` | 拒绝，可附带 note |

过期清理：`schedule_expire_stale_approvals()` 每小时自动将超时的 pending 记录标记为 expired。

**审批结果回执：** `approve_approval` / `reject_approval` 完成事务后调用 `_resolve_approval_target()`（优先 `.env` 的 `TELEGRAM_MAIN_USER_CHAT_ID`，未配置时回退到 `messages` 表最近一条 telegram 用户消息推断 session）解析推送目标，然后做两件事：① `bot.telegram_notify.send_telegram_text_to_chat()` 推送一条自然语言通知到 Telegram 聊天框（如「南杉同意了你「更新记忆卡片(preferences)」的申请，已生效。」），② 以 `role='user'` / `user_id='system'` / `[系统通知]` 前缀写入 `messages` 表，让 AI 在下一轮 context 里看到。`memory/micro_batch.py`（chunk 摘要群/私分支共用 `[系统通知]` 约束）与 `memory/daily_batch.py` 的摘要 prompt 都加了硬约束识别 `[系统通知]` 前缀，避免污染 chunk / daily 小传 / 长期记忆。

**内部工具新增 `memory_get_approval_status`：** OpenAI Function Calling 内部工具集（`tools/memory_tools.py`）新增只读工具 `memory_get_approval_status(approval_id?, status?, limit?)`，配合系统通知回执让 AI 主动复查申请状态，避免追问"我那条申请怎么样了"。详见 `CedarClio_记忆架构完整版_v3.md` 第 8.8 节。

### 8.6 业务 SSE 通道与事件总线

业务侧新增独立 SSE 端点 `GET /api/stream`（走 `X-Cedarstar-Token` 鉴权，挂在 `/api/*` 路由体系内）。该通道与 MCP SSE（`/mcp/memory/{token}/sse`）完全隔离：

- MCP SSE：面向外部 MCP 客户端与工具协议
- 业务 SSE：面向 CedarStar 业务状态推送（前端/业务控制台）

进程内使用异步队列事件总线，支持多客户端并发订阅。事件类型统一使用 `EventType` 枚举：

- `STATUS_UPDATE`
- `CONNECTION_UPDATE`
- `CHAT_MSG`
- `TOOL_PENDING_APPROVAL`

当前仅打通 `STATUS_UPDATE` 发布链路。`publish_event(EventType, partial_payload)` 会在内部补全完整状态 payload：

- 必含三字段：`pocketMoney`、`emotion`、`currentMode`
- 调用方仅需传变更字段（如只传 `pocketMoney`）
- `emotion/currentMode` 现阶段为占位值（`neutral` / `default`），并在代码中保留 TODO，待全局状态源接入后替换

### 8.7 零花钱模块 Schema（PostgreSQL）

`transactions`：

- `id BIGSERIAL PRIMARY KEY`
- `character_id TEXT NOT NULL`
- `amount NUMERIC(16,2) NOT NULL`
- `type VARCHAR(16) NOT NULL`
- `income_category VARCHAR(64)`
- `expense_category VARCHAR(64)`
- `love_sub_category VARCHAR(64)`
- `note TEXT`
- `timestamp TIMESTAMP NOT NULL DEFAULT NOW()`
- `balance_after NUMERIC(16,2) NOT NULL`
- `requested_by_ai BOOLEAN NOT NULL DEFAULT FALSE`
- `pending_approval_id BIGINT`（预留字段，本期不建外键）

`pocket_money_config`：

- `character_id TEXT PRIMARY KEY`
- `monthly_allowance NUMERIC(16,2) NOT NULL DEFAULT 0`
- `next_month_allowance NUMERIC(16,2)`
- `annual_interest_rate NUMERIC(8,6) NOT NULL DEFAULT 0`
- `updated_at TIMESTAMP NOT NULL DEFAULT NOW()`

`pocket_money_job_log`：

- `id BIGSERIAL PRIMARY KEY`
- `job_date DATE NOT NULL`
- `job_type VARCHAR(32) NOT NULL`（当前使用 `daily_pocket_money`）
- `character_id TEXT NOT NULL`
- `status VARCHAR(16) NOT NULL`（`pending/success/failed`）
- `executed_at TIMESTAMP NOT NULL DEFAULT NOW()`
- `error_message TEXT`
- `UNIQUE(job_date, job_type, character_id)`

**零花钱 REST（`/api/pocket-money`，`X-Cedarstar-Token` 鉴权）：**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/state` | 当前余额与 `pocket_money_config` |
| GET | `/transactions` | 分页流水，`limit`/`offset` |
| POST | `/transactions` | 创建收入或支出流水；成功后发布 `STATUS_UPDATE`（`pocketMoney`） |
| DELETE | `/transactions/{tx_id}` | 删除一条流水并重算后续余额；成功后同上推送 |
| PUT | `/config` | 更新 `monthly_allowance` / `annual_interest_rate`（至少传一项） |

实现文件：`api/pocket_money.py`。

### 8.7.1 游戏模式模块（Schema + Context + REST + 内部工具）

**PostgreSQL Schema（迁移由 `memory/database.py` 的 `migrate_database_schema` 确保）：**

- `game_sessions`：`id TEXT DEFAULT gen_random_uuid()::TEXT`、`game_type`、`display_name`、`system_prompt`、`state_json JSONB`、`config_json JSONB`、`participants JSONB`、`state_mode`（`per_turn` / `on_end`）、`summary`、`created_at`、`updated_at`、`ended_at`。
- `game_turns`：`id BIGSERIAL`、`session_id REFERENCES game_sessions(id)`、`turn_idx`、`turn_data JSONB`、`created_at`；索引 `idx_game_turns_session(session_id, turn_idx)`。
- `messages.game_session_id`：可空外键，活跃游戏期间 `save_message` 自动写入当前游戏 id。
- `config.active_game_session_id`：当前活跃游戏指针，空字符串表示普通模式。

**REST（前缀 `/api/game`，`X-Cedarstar-Token` 鉴权）：** `GET/POST /sessions`；`GET/PUT/DELETE /sessions/{id}`；`PUT /sessions/{id}/state`；`POST /sessions/{id}/end`；`GET/POST /sessions/{id}/turns`；`GET/PUT /active`；`PUT/DELETE /turns/{turn_id}`。

**Context 与回复处理：** 活跃 session 存在时走轻量 `build_game_context`（见 §3.0.1）。模型回复末尾的 `[GAME_STATE]...[/GAME_STATE]` 与 `[GAME_TURN]...[/GAME_TURN]` 由 `bot/game_mode.py` 解析并剥除；Telegram 缓冲/流式、非缓冲与 Discord 路径均在发送前处理。

**内部工具：** `tools/game_tools.py` 暴露 `game_start`、`game_end`、`game_update`。schema 分为 `OPENAI_GAME_START_TOOLS`（仅 `game_start`，始终注入）与 `OPENAI_GAME_ACTIVE_TOOLS`（`game_end` / `game_update`，仅 `active_game_session_id` 有值时注入），两组分别对应 `tools/prompts.py` 的 `game_start` / `game_active` system 后缀；执行由 `tools/lutopia.py` 按 `game_` 前缀分发到本地 DB 操作，不需要 MCP。`game_update` 在 `state_mode=on_end` 时拒绝进行中覆盖 `state_json`，只能追加 `turn_data`；最终状态应在 `game_end` 传入。

## 9. 外部写入（External Chunk）

### 9.1 数据结构

`summaries` 表新增两列：

- `source`：VARCHAR(32)，默认 `internal`。MCP 外部写入的 chunk 标记为 `claude_web`。
- `external_events_generated`：BOOLEAN，默认 FALSE。标记该 chunk 的事件已在 `add_external_chunk` 时预生成，日终跑批不应重复处理。

外部 chunk 的事件 `summary_type` 为 `app_event`（非 `daily_event`），以区分日终跑批产出的事件。`longterm_memories` 与 Chroma metadata 都会同步记录 `source_date` 以支持历史补录与召回日期口径统一。

### 9.2 日终跑批 Step 4 处理

Step 4 聚类前先将当天 chunk 按 `external_events_generated` 分为两组：

- **内部 chunk**（`external_events_generated=FALSE`）：正常进入 4a 聚类 → 4b 描述打分流程。
- **外部 chunk**（`external_events_generated=TRUE`）：跳过聚类，因为事件已在 `add_external_chunk` 时由 LLM 拆分并写入。聚类完成后，调用 `archive_external_chunks_by_daily()` 回填这些 chunk 的 `archived_by=<daily_id>`。

若当天仅有外部 chunk，则直接回填 `archived_by`，跳过整个聚类流程。

## 10. 机制速查

| 机制 | 说明 |
|---|---|
| 消息缓冲 | 将同一会话短时间内的多条消息合并处理 |
| 微批摘要 | 达到阈值后生成 chunk 摘要 |
| 日终跑批 | 每日离线归档、更新记忆与时间线 |
| 双路召回 | 向量检索 + BM25 关键词检索，各路 top-30 |
| Rerank 精排 | SiliconFlow Qwen3-Reranker-4B 对候选做语义精排，超时降级到旧融合路径 |
| 阈值过滤 | rerank 分数低于阈值的候选直接丢弃（starred 0.15 / 其他 0.3） |
| event_type 分级衰减 | 按事件类型设定不同半衰期（milestone 1000d / decision 200d / default 60d） |
| MMR 多样性 | 在召回结果中避免同质内容扎堆 |
| 事件拆分 | Step 4 将当天 chunk 列表合并为可独立记忆的事件片段，输出 theme/entities/emotion/event_type 标签 |
| 远古 daily 补充 | 长期召回命中较早日期时补充对应 daily 概况 |
| 记忆关键词检索 | MCP 与内部记忆工具支持 `keyword` 过滤 summaries、memory_cards、temporal_states、relationship_timeline；PostgreSQL 启用 `pg_trgm`，为 `summary_text`、卡片正文、时效正文/规则、关系时间线正文和 `longterm_memories.content` 建 GIN trigram 索引 |
| 收藏加权 | 收藏 chunk 会提升其派生长期事件的召回权重，且不参与时间衰减 |
| Context trace | 记录最近一次实际注入的摘要与长期记忆（含 rerank_scores），供 Mini App “只看本轮”排查 |
| 时效状态 | 临时状态会自动结算并可改写为历史事实 |
| Tool 执行记录 | 按原始结果长度保存原文或 LLM 压缩摘要，供后续上下文与微批使用 |
| MCP Memory Server | URL 内嵌 token 鉴权的 MCP SSE 端点，供 Claude.ai 等外部客户端读写记忆 |
| 业务 SSE 通道 | `/api/stream` 业务事件推送通道，使用 `EventType` 枚举与进程内事件总线 |
| 零花钱日任务 | 东八区 00:00 执行 `daily_pocket_money`，并补跑最近 7 天缺失/失败/pending 日期 |
| 游戏模式 | `active_game_session_id` 切换轻量游戏 context；`game_sessions` 存状态，`game_turns` 存逐轮记录；Mini App `/game` 管理 session/turn |
| 外部写入 | MCP add_external_chunk 写入的 chunk 标记 source=claude_web，事件 summary_type=app_event，日终跳过重复聚类。支持 as_of_date 历史补录 |

## 11. 结语

本 v3 文档按当前实现重写，作为 CedarStar 记忆系统的主说明文档。后续若代码演进，应直接更新 v3 正文，不再通过补丁式追加历史修订说明。
