v3.1 · 2026-05-07 更新 · TTS · 零花钱 · 业务 SSE · 实现以代码为准

# CedarClio 记忆系统架构完整版 v3

## 一、项目概述

CedarStar 是一个具备长期记忆能力的 AI 聊天系统，支持 Telegram、Discord 与 Mini App 管理后台。系统以“短期消息 → 微批摘要 → 日终小传 → 长期记忆向量”四层记忆链为核心，并通过 PostgreSQL、ChromaDB、BM25 与 LLM 工具调用共同完成上下文组装、对话生成与离线归档。

系统关键目标：

- 对话上下文稳定可控
- 长期记忆可追溯、可检索、可清理
- 日终跑批可断点续跑
- 配置热更新，无需重启即可生效

启动硬门槛：`main.py` 在初始化数据库前读取 `config.DEFAULT_CHARACTER_ID`（环境变量 `DEFAULT_CHARACTER_ID`），未配置则进程直接退出（fail-loud）。

## 二、数据与配置

### 2.1 运行参数表 `config`

| 键名 | 说明 |
|---|---|
| `buffer_delay` | 消息缓冲延迟 |
| `chunk_threshold` | 微批摘要阈值 |
| `short_term_limit` | 最近原文消息条数 |
| `context_max_daily_summaries` | Context 中纳入最近 N 天的 daily 摘要 |
| `context_max_longterm` | Context 中长期记忆注入条数 |
| `event_split_max` | Step 4 单日事件拆分上限 |
| `mmr_lambda` | 长期记忆 MMR 相关性权重 |
| `context_archived_daily_limit` | 长期召回命中较早日期时补充的 daily 概况上限 |
| `archived_daily_min_hits` | 同一较早日期召回事件数达到该阈值时优先补充 daily |
| `starred_boost_factor` | 被收藏长期事件的召回融合分乘数 |
| `daily_batch_hour` | 日终跑批时刻，支持 0.0–23.5 |
| `relationship_timeline_limit` | 关系时间线注入条数 |
| `gc_stale_days` | Chroma GC 闲置天数阈值 |
| `gc_exempt_hits_threshold` | Chroma GC hits 豁免阈值 |
| `retrieval_top_k` | 向量 / BM25 各路召回数 |
| `telegram_max_chars` | Telegram 分段最大字数 |
| `telegram_max_msg` | Telegram 分段最大条数 |
| `idle_activity_enabled` | 是否启用 AI 自主活动（true/false） |
| `idle_activity_level` | 自主活动概率档位：low/mid/high（0.3/0.6/1.0） |
| `idle_activity_threshold_min` | 距离用户最后发言达到多少分钟后才有资格触发 |
| `idle_activity_cooldown_min` | 两次自主活动之间最小间隔（分钟） |
| `idle_activity_start_hour` | 自主活动允许开始小时（东八区，0-23） |
| `idle_activity_end_hour` | 自主活动允许结束小时（东八区，0-23） |
| `external_chunk_max_chars` | MCP 外部写入单条 content 最大字数，默认 2000 |
| `rerank_enabled` | 是否启用 SiliconFlow Rerank 精排，默认 true |
| `rerank_candidate_size` | rerank 候选集大小上限，默认 50 |
| `rerank_score_floor` | 非收藏事件的 rerank 分数阈值，默认 0.3 |
| `rerank_starred_floor` | 收藏事件的 rerank 分数阈值，默认 0.15 |
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

### 2.2 `api_configs`

`api_configs.config_type` 允许：`chat`、`summary`、`vision`、`stt`、`embedding`、`search_summary`、`analysis`、`rerank`、`tts`。

其中：

- `chat`：日常对话
- `summary`：微批 / 日终摘要
- `vision`：图片理解
- `stt`：语音转录
- `embedding`：向量嵌入（默认 SiliconFlow Qwen3-Embedding-8B，dim=1024；通过 `EMBEDDING_PROVIDER` 环境变量可切回智谱 embedding-3）
- `search_summary`：网页搜索结果压缩
- `analysis`：日终 Step 4 事件聚类、描述与打分；不可用时回退 `summary`
- `rerank`：长期记忆精排（SiliconFlow Qwen3-Reranker-4B）
- `tts`：语音合成（MiniMax T2A v2，国内域名 `api.minimaxi.com`）；`api_configs` 中需配置 `api_key` 与 `voice_id`

### 2.3 `persona_configs`

人设表同时承载角色、用户画像与工具开关。与 v3 当前实现对齐的重点字段包括：

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
- `enable_weather_tool`
- `enable_weibo_tool`
- `enable_search_tool`
- `enable_x_tool`

### 2.4 辅助表

| 表 | 说明 |
|---|---|
| `sticker_cache` | Telegram 贴纸元数据缓存（file_unique_id → emoji / description），用于贴纸消息的文本化摘要 |
| `group_chat_state` | 群聊状态（chat_id → round_count），跟踪群聊轮次 |
| `model_favorites` | Mini App 配置页按供应商收藏的模型列表（base_url + model 唯一） |
| `sensor_events` | 外部传感器事件 ingestion（event_type + JSONB payload） |
| `autonomous_diary` | 自主日记条目（title / content / trigger_reason / tool_log） |
| `daily_batch_log` | 日终跑批每步状态跟踪（batch_date 主键，step1–5 status，retry_count） |
| `meme_pack` | 表情包配置（name / url / is_animated） |
| `pending_approvals` | 写入操作审批队列（详见 8.5） |
| `transactions` | 零花钱流水（收入/支出、分类、余额快照、审批预留字段） |
| `pocket_money_config` | 零花钱配置（月额度、下月额度、年化利率） |
| `pocket_money_job_log` | 零花钱日任务执行日志（按日期+任务类型+character 唯一） |

## 三、上下文构建与召回

### 3.0 新增记忆字段（数据结构）

`summaries`：

- `archived_by`：可空，指向归档该 chunk 的 daily summary id。chunk 生成 daily 后不再删除，而是写入该字段。
- `is_starred`：是否收藏该 chunk / daily，默认 false。
- `source`：VARCHAR(32)，默认 `internal`。MCP 外部写入的 chunk 标记为 `claude_web`。
- `external_events_generated`：BOOLEAN，默认 FALSE。标记该 chunk 的事件已在 `add_external_chunk` 时预生成，日终跑批跳过重复聚类。

`longterm_memories`：

- `source_chunk_ids`：JSONB，记录 Step 4 事件由哪些 chunk 合并而来。
- `is_starred`：事件是否收藏，来源 chunk 任一被收藏则为 true。
- `source_date`：DATE，可空。记录事件对应的业务日期。内部日终 Step 4 事件按 `batch_date` 写入；MCP 外部写入事件按 `as_of_date`（未传则当天）写入。
- `theme`：VARCHAR(32)，事件主题标签（如 daily_life、work_career、milestone 等），由 Step 4b 生成。
- `entities`：JSONB 数组，事件涉及的命名实体（最多 5 个），由 Step 4b 生成。
- `emotion`：VARCHAR(32)，事件情绪标签（如 happy、sad、anxious 等），由 Step 4b 生成。
- `event_type`：VARCHAR(32)，事件类型标签（如 daily_warmth、decision、emotional_shift、milestone 等），由 Step 4b 生成。
- `metadata_manual_override`：BOOLEAN，默认 FALSE。为 TRUE 时 migrate 脚本跳过该行的 metadata 覆盖。

### 3.1 Context 拼装顺序

Context 组装时，系统按以下顺序注入信息（前缀缓存边界标注在侧）：

1. system prompt + 指令块（优先级、引用、思考语言、工具口播） —— 前缀缓存 ✓
2. temporal_states —— 前缀缓存 ✓
3. memory_cards —— 前缀缓存 ✓
4. relationship_timeline —— 前缀缓存 ✓
5. daily summaries —— 前缀缓存 ✓
6. 长期记忆召回
7. 远古 daily 概况补充
8. 未归档 chunk summaries
9. 动态内容（当前时间、工具记录、结束语）
10. 最近消息
11. 当前用户消息

其中长期记忆召回采用双路检索 + SiliconFlow Rerank 精排 + 阈值过滤 + event_type 分级时间衰减 + MMR 多样性筛选：

1. **构建 rerank query**：取当前 session 最近 `rerank_query_turns` 轮对话，加角色前缀（南杉: / 小克:），截断到 `rerank_query_max_chars` 字符
2. **双路检索**：向量检索与 BM25 各自召回 `retrieval_top_k`（默认 30），候选去重合并，上限 `rerank_candidate_size`（默认 50）
3. **Rerank 精排**：调用 SiliconFlow Qwen3-Reranker-4B API，每条候选得到 0-1 的 relevance_score；超时或异常时降级到旧的 `fuse_rerank_with_time_decay` 路径
4. **阈值拦截**（用 rerank 纯语义分，不混入加权）：
   - `is_starred=true`：score >= `rerank_starred_floor`（0.15）通过
   - 其他：score >= `rerank_score_floor`（0.3）通过
5. **加权排序**：`final_score = rerank_score × starred_boost × time_decay`
   - time_decay 按 `event_type` 分级半衰期：milestone → 1000 天，decision / emotional_shift → 200 天，其他 → 60 天
   - `is_starred=true` → 衰减系数固定 1.0（不衰减）
6. **MMR 多样性**：按 `mmr_lambda` 做 MMR
7. **注入**：最终注入 `context_max_longterm` 条

### 3.4 远古 daily 补充

长期记忆召回完成后，系统会检查命中的 `daily_event` 日期：

- 最近 `context_max_daily_summaries` 天的 daily 已通过常规 daily 通道注入，不重复补充
- 对近期窗口外的命中日期按召回事件数分组
- 优先选择命中数不少于 `archived_daily_min_hits` 的日期
- 不足时按该日期召回事件最高分补足
- 最多注入 `context_archived_daily_limit` 条 daily 概况

该块紧跟长期记忆召回块，固定说明为：`以下是长期记忆中涉及到的较早日期的概况补充，仅作为背景，不代表近期发生`。

### 3.5 chunk 注入过滤

Context 中的 chunk 摘要只注入 `archived_by IS NULL` 的记录。已经被 daily 归档的 chunk 保留在数据库中用于追溯、收藏和 Step 4 来源映射，但不再重复进入常规上下文。

### 3.6 Context trace

`memory/context_builder.py` 会在每次真实构建 Context 后，在进程内记录最近一轮实际注入的记忆清单：

- `built_at`、`session_id`、`user_message_preview`
- `daily_summary_ids`
- `chunk_summary_ids`
- `archived_daily_summary_ids`
- `longterm_doc_ids`
- `rerank_scores`：每条注入长期记忆的 rerank_score、fusion_score、event_type

`GET /api/memory/context-trace` 返回这份最近一次 trace。该 trace 仅用于 Mini App 排查，不参与模型上下文，也不持久化；服务重启后会清空。

### 3.2 长期记忆过滤

长期记忆默认只召回：

- `daily`
- `daily_event`
- `manual`
- `app_event`

当用户消息命中回溯语义时，才额外放开 `state_archive`。

### 3.3 工具执行摘要

`tool_executions` 用于记录每次工具调用的短摘要与原始结果。摘要上限 150 字（短于 150 直接存原文），DB 兜底截断 1200；原始结果截断 50K，仅供排查。Context 注入与 chunk 摘要均使用 150 字摘要。日终跑批自动清理 7 天前的记录。

## 四、对话与工具

### 4.1 对话通路

- Telegram 通过 webhook 接入
- Discord 通过 bot gateway 接入
- 两者都先进入消息缓冲，再统一构建 Context 与调用 LLM

### 4.2 工具开关

人设可单独控制：

- Lutopia
- 天气
- 微博热搜
- 网页搜索
- X (Twitter)（11 个工具：发推、点赞、回复、搜索、时间线、关注/取关、粉丝列表等，共享每日配额）

工具口播提示由 system suffix 注入，确保模型在调用工具前先说一句自然口语。

### 4.3 AI 自主活动（Idle Activity）

进程内 `schedule_idle_activity_check()` 定时检查（当前 10 分钟一次）。当启用且满足时段、阈值、冷却与概率条件时，系统会向上下文注入 `[IDLE_TRIGGER]` 用户提示并调用 `complete_with_lutopia_tool_loop` 生成一条自主活动消息。触发提示不写入 `messages`，助手消息会写入 `messages` 且内容前缀为 `【自主活动】`，并更新 `idle_activity_last_triggered_at`。若本轮触发了工具调用，会在助手消息落库后按 `session_id + turn_id` 回填 `tool_executions.assistant_message_id`，确保微批摘要可内联该轮工具结果。

## 五、微批与日终跑批

### 5.1 微批摘要

当未摘要消息达到阈值后，系统会生成 chunk 摘要并写入 `summaries` 表。摘要前会注入人物称呼锚点；工具执行结果按 `assistant_message_id` 内联到对应对话轮次中，与对话一起作为摘要输入，避免工具信息与对话脱节。

chunk 生命周期：生成后长期保留；日终 Step 2 生成 daily 后，chunk 不删除，只通过 `archived_by=<daily_id>` 标记为已归档。归档日期口径与读取当天 chunk 一致，使用 `COALESCE(source_date::date, created_at::date)` 匹配业务日；这是为了兼容 `source_date` 字段加入前的旧 chunk，避免旧 chunk 进入 daily 后仍因 `source_date` 为空显示为未归档。

### 5.2 日终跑批总流程

日终跑批由进程内 `schedule_daily_batch()` 按数据库 `daily_batch_hour` 配置定时触发（东八区），按业务日执行六步。`run_daily_batch.py` 保留为独立命令行入口（手动补跑 / 重试子进程调用）。

1. Step 1：到期 temporal_states 结算
2. Step 2：生成今日小传
3. Step 3：记忆卡片 Upsert + relationship_timeline
4. Step 3.5：从今日小传提取时效状态操作
5. Step 4：事件聚类 + 描述打分 + 长期事件入库
6. Step 5：Chroma GC

### 5.3 Step 4 重写

Step 4 使用当天按时间顺序排列的 chunk 列表作为输入。聚类前先过滤掉 `external_events_generated=TRUE` 的外部 chunk（其事件已在 `add_external_chunk` 时预生成），仅对内部 chunk 执行聚类。聚类完成后，调用 `archive_external_chunks_by_daily()` 回填外部 chunk 的 `archived_by`。若当天仅有外部 chunk，则跳过聚类，直接回填。

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

Step 4 结果只写事件片段，不再写 daily 小传向量。事件写入 `longterm_memories.source_chunk_ids`，并根据来源 chunk 的 `is_starred` 汇总出事件的 `is_starred`。同时，Step 4 写入 Chroma metadata 时会同步带上 `date` 与 `source_date`（均为当日 `batch_date`），供长期记忆列表日期显示与远古 daily 补充逻辑统一使用。

## 六、记忆召回策略

### 6.1 长期记忆融合

长期记忆召回采用以下策略：

- **SiliconFlow Rerank 语义精排**优先（Qwen3-Reranker-4B，0-1 relevance_score）
- **阈值过滤**剔除低分候选（starred >= 0.15，其他 >= 0.3）
- **event_type 分级时间衰减**辅助修正（milestone 1000d / decision 200d / default 60d，starred 不衰减）
- **MMR 保证多样性**

最终效果是：既尽量选中”最相关”的记忆，也避免同质内容扎堆。Rerank 超时或异常时降级到旧的 `fuse_rerank_with_time_decay` 路径。

### 6.1.1 收藏加权

用户可在 Mini App 收藏 chunk。收藏状态会同步影响由该 chunk 派生的长期事件：

- 任一来源 chunk 被收藏，则事件 `is_starred=true`
- Chroma metadata 同步写入 `is_starred`
- 阈值过滤阶段：收藏事件阈值更低（0.15 vs 0.3）
- 加权阶段：收藏事件不参与时间衰减（衰减系数固定 1.0），乘以 `starred_boost_factor`
- 加权后再进入 MMR，避免收藏事件完全绕过多样性约束

### 6.2 记忆卡片

`memory_cards` 用于稳定保存角色/用户的重要事实。Step 3 会按维度合并更新，而不是无限累加重复内容。`manual_override` 列（BOOLEAN，默认 FALSE）为 TRUE 时，Step 3 的 structured output 跳过该维度该子项的覆盖，由人工手动维护。

### 6.3 时效状态

`temporal_states` 记录短期、会过期的状态与行动规则。日终会做过期结算，并在需要时改写为客观过去时事实。

## 七、Mini App 与配置管理

### 7.1 Settings

Settings 页管理 API 配置，支持：

- 新增 / 编辑 / 删除
- 激活某配置
- 拉取模型列表
- 查看 token 使用统计

### 7.2 Config

Config 页管理运行参数，包括：

- 缓冲延迟
- 摘要阈值
- 最近原文条数
- Telegram 分段参数

### 7.3 Memory

Memory 页管理记忆卡片、长期记忆和 summaries。summaries 列表支持对 chunk 点星收藏，收藏状态会通过 `PATCH /api/memory/summaries/{id}/star` 同步到引用该 chunk 的长期事件与 Chroma metadata。

长期记忆列表日期显示口径来自 Chroma metadata：优先 `date`，缺失时回退 `last_access_ts`。因此日期修复应优先保证 Chroma metadata 完整，再考虑 PostgreSQL 镜像字段。

Memory 页的 summaries 与长期记忆列表支持“只看本轮”排查：

- summaries 调用 `GET /api/memory/summaries?context_only=true`，按最近一次 Context trace 中的 summary id 返回实际注入条目；可继续按 `summary_type` 限定 chunk / daily。
- 长期记忆调用 `GET /api/memory/longterm?context_only=true`，按最近一次 Context trace 中的 Chroma doc id 返回实际注入条目；可继续按 `summary_type` 限定类型。
- 前端用蓝色”本轮”标签标记最近一次 context 实际注入的摘要和长期记忆。

### 7.4 待审批

待审批页（`/approvals`）展示来自内部记忆工具写入操作与 MCP `api_admin` 管理写入工具的 pending approval 请求。用户可在此批准或拒绝请求，批准后由 `_apply_approved_update` 执行实际写入。

## 八、MCP Memory Server

### 8.1 端点与鉴权

MCP 服务器以 ASGI 中间件形式挂载在 `/mcp/memory` 路径下，通过 SSE（Server-Sent Events）传输层对外暴露，供 Claude.ai Custom Connector 等外部客户端连接。

**端点路径：**

- SSE 连接：`GET /mcp/memory/{token}/sse`
- POST 消息：`POST /mcp/memory/{token}/messages/?session_id=xxx`

POST 消息路径由 MCP server 在 SSE 连接建立时自动下发（`event: endpoint`），客户端无需手动构造。

**鉴权方案 — URL 内嵌 token：**

Claude.ai Custom Connector 不支持 `Authorization: Bearer` header 认证（GitHub issue #112），仅支持无认证或 OAuth 2.1 + DCR。因此采用 URL 内嵌 token 方案：

- token 格式：8–256 字符（`[^/]{8,256}`），通过环境变量配置
- 匹配 `MCP_WEB_READ_TOKEN` → 绑定 `web_read` scope（7 个只读工具）
- 匹配 `MCP_WEB_WRITE_TOKEN` → 绑定 `web_write` scope（7 个只读工具 + `add_external_chunk`）
- 匹配 `MCP_API_READ_TOKEN` → 绑定 `api_read` scope（7 个只读工具）
- 匹配 `MCP_API_TOKEN` → 绑定 `api_admin` scope（7 个只读工具 + 4 个管理写入工具）
- 都不匹配 → 返回 404（非 401，避免攻击者通过响应码判断 token 存在性）

中间件鉴权通过后，将 `scope[“root_path”]` 设为 `/mcp/memory/{token}`，使 MCP server 在 SSE `endpoint` 事件中自动构造含 token 的 messages URL：`/mcp/memory/{token}/messages/?session_id=xxx`。客户端后续 POST 请求自然携带 token，无需额外处理。

### 8.2 工具清单

**读工具（所有 scope 可用）：**

| 工具 | 参数 | 说明 |
|---|---|---|
| `search_memories` | query, top_k=10, type_filter, source_filter | 向量 + BM25 双路召回搜索长期记忆。type_filter 可选 daily_event / manual / app_event，默认 ['daily_event', 'manual', 'app_event']。source_filter 按 Chroma metadata.source 过滤 |
| `get_recent_summaries` | date, days, summary_type, only_unarchived, source_filter, page=1, page_size=20 | 分页列出 summaries。date 为具体日期 YYYY-MM-DD，days 为最近 N 天，summary_type 为 chunk/daily/省略=全部 |
| `get_memory_cards` | user_id, character_id, dimension, limit=50 | 获取记忆卡片列表，不传 user_id/character_id 时返回全部激活卡片 |
| `get_temporal_states` | 无 | 列出全部 temporal_states（含已停用），按 created_at 倒序 |
| `get_relationship_timeline` | 无 | 全部关系时间线，按 created_at 倒序 |
| `get_persona` | persona_id | 获取单个人设配置详情 |
| `get_context_trace` | 无 | 最近一次 context 构建时实际注入的摘要和长期记忆清单 |

**写工具（`web_write` scope）：**

| 工具 | 参数 | 说明 |
|---|---|---|
| `add_external_chunk` | content, as_of_date | 从网页端 Claude 整理的对话摘要写入记忆库。仅在用户明确说出「整理这个窗口」「写进记忆库」「存进去」等显式指令时调用，不要主动调用。as_of_date 支持历史补录，补录后需在服务器执行 `python run_daily_batch.py YYYY-MM-DD` 重跑当日 daily |

`add_external_chunk` 内部流程：
1. 日期校验（`as_of_date` 不允许未来日期）
2. 字数校验（`external_chunk_max_chars`，默认 2000）
3. LLM 拆分事件（使用 `analysis` 配置，不可用时回退 `summary`），输出 `[{summary, score, arousal}, ...]`，跳过 < 50 字事件
4. PG `summaries` 表写入 chunk 留底（`source=claude_web`, `external_events_generated=TRUE`，文本加 `[APP端]` 前缀）
5. 逐条事件写入（`summary_type=app_event`）：ChromaDB embedding → PG `longterm_memories` 镜像（含 `source_date`） → BM25 增量索引

### 8.3 审计日志

所有 MCP `call_tool` 调用（含鉴权失败）写入 `mcp_audit_log` 表，用于安全审计和使用追踪：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | SERIAL | 主键 |
| `token_scope` | VARCHAR(32) | web_read / web_write / api_read / api_admin / `__auth__`（鉴权失败时） |
| `tool_name` | VARCHAR(64) | 工具名称，鉴权失败时为 `__auth__` |
| `arguments` | JSONB | 工具调用参数 |
| `result_status` | VARCHAR(32) | success / error |
| `error_message` | TEXT | 错误信息（成功时为 NULL） |
| `approval_id` | UUID | 关联 pending_approvals 表（管理写入工具有值） |
| `called_at` | TIMESTAMPTZ | 调用时间，默认 NOW() |

### 8.4 日志脱敏

uvicorn access log 中的完整 URL 路径（含 token）由 `_RedactMcpTokenFilter` 自动替换为 `***`，防止 token 泄露到日志文件。中间件鉴权通过后会将 `scope[“path”]` 重写为内部路径（`/sse` 或 `/messages/`），原始路径保存在 `scope[“mcp_original_path”]` 中。

### 8.5 审批系统

MCP `api_admin` scope 的写入工具与内部记忆工具的写入操作均通过 `pending_approvals` 表排队，需用户在 Mini App “待审批”页确认后才生效。

`pending_approvals` 表：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键，默认 gen_random_uuid() |
| `tool_name` | VARCHAR(64) | 操作名称 |
| `arguments` | JSONB | 操作参数 |
| `arguments_hash` | VARCHAR(128) | 参数哈希，用于去重 |
| `before_snapshot` | JSONB | 修改前快照 |
| `after_preview` | JSONB | 修改后预览 |
| `requested_by_token_hash` | VARCHAR(128) | 请求来源 token 哈希 |
| `status` | VARCHAR(32) | pending / approved / rejected / expired |
| `created_at` | TIMESTAMPTZ | 创建时间 |
| `expires_at` | TIMESTAMPTZ | 过期时间 |
| `resolved_at` | TIMESTAMPTZ | 处理时间 |
| `resolution_note` | TEXT | 处理备注 |

审批 API 端点：

| 端点 | 说明 |
|---|---|
| `GET /api/approvals` | 列出审批记录，可按 `status` 过滤；可选 `limit`（1–100，省略则返回全部，Mini App 不传 limit 一次拉满） |
| `GET /api/approvals/{id}` | 单条审批详情（同时挂在 `/api/memory/approvals/{id}` 别名下），用于 `memory_get_approval_status` 工具回查 |
| `POST /api/approvals/request` | 创建审批请求（内部工具写入入口） |
| `POST /api/approvals/{id}/approve` | 批准 |
| `POST /api/approvals/{id}/reject` | 拒绝，可附带 note |

MCP `api_admin` scope 额外提供 4 个管理写入工具（均走审批）：

| 工具 | 说明 |
|---|---|
| `update_memory_card` | 修改七维记忆卡片，参数：persona_id, dimension, content |
| `update_temporal_state` | 修改时效状态，参数：id, content |
| `update_relationship_timeline_entry` | 修改关系时间线条目，参数：id, content |
| `update_persona_field` | 修改人设字段，参数：persona_id, field_name, content |

过期清理：`schedule_expire_stale_approvals()` 每小时自动将超时的 pending 记录标记为 expired。

**审批结果回执（approve / reject 后的实时通知）：**

`approve_approval` / `reject_approval` 处理完事务后会调用 `_resolve_approval_target()` 解析推送目标：优先读取 `.env` 中的 `TELEGRAM_MAIN_USER_CHAT_ID`，未配置时回退到 `messages` 表中最近一条 telegram 用户消息的 `session_id`（CedarClio 单用户场景下足够稳定）。解析到 target 后做两件事：

1. **Telegram 聊天框推送**：通过 `bot.telegram_notify.send_telegram_text_to_chat(chat_id, text)` 发送一条自然语言系统通知，文本由 `_compose_approval_resolution_phrase` 组装，例如「南杉同意了你「更新记忆卡片(preferences)」的申请，已生效。\n内容：xxx」或「南杉拒绝了你「新增关系时间线条目(milestone)」的申请。\n理由：xxx」。
2. **写入 messages 表**：以 `role='user'`、`user_id='system'`、`platform='telegram'` 持久化一条 `[系统通知] {phrase}` 的消息，让 AI 在下一轮 `context_builder` 取最近未摘要消息时直接看到审批结果。

工具名 → 中文动作标签由 `_TOOL_ACTION_LABELS` 维护（与 Mini App 待审批页 `TOOL_LABELS` 对齐），目标维度/字段在短语中以 `(dimension)` / `(field_name)` / `(event_type)` 形式呈现。

为避免 `[系统通知]` 行污染 chunk / daily 摘要，`memory/micro_batch.py` 的 chunk 摘要 prompt 与 `memory/daily_batch.py` 的两处日终小传 prompt 都加了硬约束，要求摘要 LLM 把 `[系统通知]` 开头的行视为元事件回执：必要时用客观第三方表述（如「南杉确认/驳回了某条记忆更新申请」），不得作为对话引语，且与正文话题无关时整体省略。

### 8.6 业务 SSE 通道与事件总线

业务侧独立 SSE 端点 `GET /api/stream`（走 `X-Cedarstar-Token` 鉴权，挂在 `/api/*` 路由体系内）。该通道与 MCP SSE（`/mcp/memory/{token}/sse`）完全隔离：

- MCP SSE：面向外部 MCP 客户端与工具协议
- 业务 SSE：面向 CedarStar 业务状态推送（前端/业务控制台）

进程内使用异步队列事件总线，支持多客户端并发订阅。事件类型统一使用 `api.stream.EventType` 枚举：`STATUS_UPDATE`、`CONNECTION_UPDATE`、`CHAT_MSG`、`TOOL_PENDING_APPROVAL`。当前仅打通 `STATUS_UPDATE` 发布链路。`publish_event(EventType, partial_payload)` 会在内部补全完整状态 payload：必含 `pocketMoney`、`emotion`、`currentMode`；调用方仅需传变更字段（如只传 `pocketMoney`）。`emotion` / `currentMode` 现阶段为占位值（`neutral` / `default`），代码中保留 TODO，待全局状态源接入后替换。

### 8.7 零花钱模块（Schema + REST + 日任务）

**PostgreSQL Schema（迁移由 `memory/database.py` 的 `migrate_database_schema` 确保）：**

`transactions`：`id`、`character_id`、`amount`、`type`（income/expense）、`income_category`、`expense_category`、`love_sub_category`、`note`、`timestamp`、`balance_after`、`requested_by_ai`、`pending_approval_id`（预留，本期不建外键）。

`pocket_money_config`：`character_id`（主键）、`monthly_allowance`、`next_month_allowance`、`annual_interest_rate`、`updated_at`。

`pocket_money_job_log`：`job_date`、`job_type`（当前 `daily_pocket_money`）、`character_id`、`status`（pending/success/failed）、`executed_at`、`error_message`，`UNIQUE(job_date, job_type, character_id)`。

**REST（前缀 `/api/pocket-money`，`X-Cedarstar-Token` 鉴权）：** `GET /state`；`GET /transactions`；`POST /transactions`；`DELETE /transactions/{tx_id}`；`PUT /config`。写入/删除流水后会 `publish_event(STATUS_UPDATE, {pocketMoney})`。实现：`api/pocket_money.py`。

**日任务：** `memory/daily_batch.schedule_pocket_money_jobs()` 由 `main.py` 与 `schedule_daily_batch` 等并列启动；东八区 00:00 执行 `daily_pocket_money`，并补跑最近 7 天内缺失/失败/pending 的日期（见「十、机制速查」表）。

### 8.8 内部记忆工具（OpenAI Function Calling）

除了 MCP SSE 端点外，记忆系统还通过 OpenAI Function Calling 格式暴露给 Telegram/Discord 的工具循环（`_telegram_stream_thinking_and_reply_with_lutopia` / `complete_with_lutopia_tool_loop`）。内部工具通过 `httpx` 调用本地 REST API（`http://127.0.0.1:8001/api`），不经过 MCP SSE。

定义文件：`tools/memory_tools.py`

**读取工具（6 个，无条件加载）：**

| 工具 | 参数 | 说明 | 对应 API |
|---|---|---|---|
| `memory_search` | query, top_k=5 | 向量+BM25 双路召回搜索长期记忆 | `GET /api/memory/longterm` |
| `memory_get_summaries` | date, days, summary_type, starred_only, page, page_size | 分页查询 chunk 和日摘要，支持收藏过滤 | `GET /api/memory/summaries` |
| `memory_get_cards` | character_id, dimension（带 7 个枚举约束）, limit=50 | 查询七维记忆卡片 | `GET /api/memory/cards` |
| `memory_get_temporal_states` | days | 查询时效状态，可按天数过滤 | `GET /api/memory/temporal-states` |
| `memory_get_relationship_timeline` | days | 查询关系时间线，可按天数过滤 | `GET /api/memory/relationship-timeline` |
| `memory_get_approval_status` | approval_id（单条精查）/ status / limit（默认 10，最大 100） | 查询自己提交的审批的当前状态，配合「[系统通知] 南杉同意/拒绝了你「xxx」的申请」回执回查 | `GET /api/memory/approvals[/{id}]` |

**写入工具（1 个，走审批）：**

`memory_update_request` 嵌套结构 `{tool_name, arguments}`，工具描述里列出全部 7 个候选 `tool_name` 及其 enum 约束（`dimension` / `field_name` / `event_type` 必须使用规定的英文枚举值，不能用中文，不能自创）：

| tool_name | 参数 | 说明 |
|---|---|---|
| `update_memory_card` | persona_id, dimension, content | 修改七维记忆卡片 |
| `update_temporal_state` | id, content | 修改时效状态 |
| `update_relationship_timeline_entry` | id, content | 修改关系时间线条目 |
| `update_persona_field` | persona_id, field_name, content | 修改人设字段（field_name ∈ char_identity / char_personality / char_speech_style / char_redlines / char_appearance / char_relationships / char_nsfw） |
| `update_summary` | id, content | 修改摘要正文 |
| `create_relationship_timeline_entry` | event_type, content, source_summary_id? | 新增关系时间线条目（event_type ∈ milestone / emotional_shift / conflict / daily_warmth） |
| `create_temporal_state` | content, action_rule?, expire_at? | 新增时效状态（expire_at: ISO 8601） |

写入操作均通过 `POST /api/approvals/request` 创建 pending approval，需用户在 Mini App 待审批页确认后才生效。审批通过后由 `_apply_approved_update` 执行实际写入；audit/Telegram 通知 + `[系统通知]` 回执见 8.5。

模型流式返回的 `arguments` JSON 偶尔会缺末尾右括号，`tools/lutopia.py` 的 `_safe_load_tool_args` 会自动补齐 `}` / `]`、解析失败时记 WARNING（防止旧实现里 `except json.JSONDecodeError: args = {}` 静默吞掉参数）；`execute_memory_update_request` 还会检测「字段被拍平到顶层」的错误形态，返回带格式示例的错误文本引导模型下一轮自修。

dispatch 代码位于：
- `llm/llm_interface.py` → `complete_with_lutopia_tool_loop`
- `bot/telegram_bot.py` → `_telegram_stream_thinking_and_reply_with_lutopia`
- `tools/lutopia.py` → `append_tool_exchange_to_messages`

## 九、外部写入（External Chunk）

### 9.1 概述

外部写入是指通过 MCP Memory Server 的 `add_external_chunk` 工具，从网页端 Claude 将对话摘要写入记忆库。与内部 chunk（由 Telegram/Discord 对话自动生成）不同，外部 chunk 的事件在写入时就由 LLM 拆分完成，不需要在日终跑批 Step 4 中重复处理。

### 9.2 数据结构变更

`summaries` 表新增两列：

- `source`：VARCHAR(32)，默认 `internal`。MCP 外部写入的 chunk 标记为 `claude_web`。
- `external_events_generated`：BOOLEAN，默认 FALSE。标记该 chunk 的事件已在 `add_external_chunk` 时由 LLM 预生成并写入 ChromaDB + longterm_memories，日终跑批不应重复聚类。

外部 chunk 的事件 `summary_type` 为 `app_event`（非 `daily_event`），以区分日终跑批产出的事件。`longterm_memories` 与 Chroma metadata 都会同步记录 `source_date`，以支持历史补录与召回日期口径统一。

### 9.3 日终跑批 Step 4 处理

Step 4 聚类前，先将当天 chunk 按 `external_events_generated` 分为两组：

- **内部 chunk**（`external_events_generated=FALSE`）：正常进入 4a 聚类 → 4b 描述打分流程。
- **外部 chunk**（`external_events_generated=TRUE`）：跳过聚类，因为事件已在 `add_external_chunk` 时由 LLM 拆分并写入长期记忆。聚类完成后，调用 `archive_external_chunks_by_daily()` 将这些 chunk 的 `archived_by` 回填为当前 daily summary 的 ID。

若当天仅有外部 chunk（无内部 chunk），则直接回填 `archived_by`，跳过整个 4a/4b 聚类流程。

## 十、机制速查

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
| 事件拆分 | Step 4 将 daily 小传拆为可独立记忆的事件片段，输出 theme/entities/emotion/event_type 标签 |
| 远古 daily 补充 | 长期召回命中较早日期时补充对应 daily 概况 |
| 收藏加权 | 收藏 chunk 会提升其派生长期事件的召回权重，且不参与时间衰减 |
| Context trace | 记录最近一次实际注入的摘要与长期记忆（含 rerank_scores），供 Mini App “只看本轮”排查 |
| 时效状态 | 临时状态会自动结算并可改写为历史事实 |
| Tool 执行记录 | 保存工具调用摘要，供后续上下文与微批使用 |
| MCP Memory Server | URL 内嵌 token 鉴权的 MCP SSE 端点，供 Claude.ai 等外部客户端读写记忆，含审计日志与日志脱敏 |
| 业务 SSE 通道 | `GET /api/stream` 业务事件推送，与 MCP SSE 隔离；`EventType` + 进程内订阅队列 |
| 零花钱日任务 | 东八区 00:00 执行 `daily_pocket_money`，补跑最近 7 天缺失/失败/pending；`main.py` 启动 `schedule_pocket_money_jobs()` |
| 外部写入 | MCP add_external_chunk 写入的 chunk 标记 source=claude_web，事件 summary_type=app_event，日终跳过重复聚类，仅回填 archived_by。支持 as_of_date 历史补录 |

## 十一、结语

本 v3 文档按当前实现重写，作为 CedarStar 记忆系统的主说明文档。后续若代码演进，应直接更新 v3 正文，不再通过补丁式追加历史修订说明。
