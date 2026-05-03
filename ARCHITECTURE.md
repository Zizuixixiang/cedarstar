# CedarStar 项目架构文档 v3

v3 · 2026-04-26 重写 · 实现以代码为准

> 本文档是 CedarStar 当前实现的主架构说明，已按代码重写，去除历史补丁式修订痕迹。

## 1. 项目概述

CedarStar 是一个具备长期记忆能力的 AI 聊天系统，支持 Telegram、Discord 与 Mini App 管理后台。系统围绕“短期消息 → 微批摘要 → 日终小传 → 长期记忆向量”构建分层记忆链，并通过 PostgreSQL、ChromaDB、BM25 与 LLM 工具调用完成上下文组装、对话生成与离线归档。

核心目标：

- 对话上下文稳定可控
- 长期记忆可追溯、可检索、可清理
- 日终跑批可断点续跑
- 配置热更新，无需重启即可生效

## 2. 数据与配置

### 2.1 运行参数表 `config`

| 键名 | 说明 |
|---|---|
| `buffer_delay` | 消息缓冲延迟 |
| `chunk_threshold` | 微批摘要阈值 |
| `short_term_limit` | 最近原文消息条数 |
| `context_max_daily_summaries` | Context 中 daily 摘要条数 |
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
| `external_chunk_max_chars` | MCP 外部写入单条 content 最大字数，默认 2000 |

### 2.2 token_usage / tool_executions

- `token_usage` 现在除 `raw_usage_json` 外，还会持久化 `base_url`，用于按模型提供方与网关来源追踪 token 使用。
- `provider_cache_hit_tokens` 与 `theoretical_cached_tokens` 口径已经拆分：
  - `provider_cache_hit_tokens` 直接按上游写入的 `cache_hit_tokens` 统计
  - `theoretical_cached_tokens` 记录本轮请求里稳定前缀部分的输入量估算值（按 context 拼装顺序中前缀缓存边界内的内容计算）；观测 API 会以供应商实际命中值作为旧数据兜底下限，并按 prompt tokens 封顶
- `tool_executions` 的 Mini App 观测接口改为分页返回，包含 `items / total / limit / offset`，并压缩 `result_raw_preview` 以降低前端渲染成本。
- Observability 页面支持“本次”视图，聚合区默认展示最新一条 token usage；“最近调用（今日）”固定展示东八区当天全部调用。

### 2.2 `api_configs`

`api_configs.config_type` 允许：`chat`、`summary`、`vision`、`stt`、`embedding`、`search_summary`、`analysis`。

- `chat`：日常对话
- `summary`：微批 / 日终摘要
- `vision`：图片理解
- `stt`：语音转录
- `embedding`：向量嵌入
- `search_summary`：网页搜索结果压缩
- `analysis`：日终 Step 4 事件聚类、描述与打分；不可用时回退 `summary`

### 2.3 `persona_configs`

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
- `enable_weather_tool`
- `enable_weibo_tool`
- `enable_search_tool`
- `enable_x_tool`

## 3. 上下文构建与召回

### 3.0 记忆归档与收藏字段

`summaries` 表新增：

- `archived_by`：可空，引用归档该 chunk 的 daily summary id。daily 生成后 chunk 不删除，只写入该字段。
- `is_starred`：收藏标记，默认 false。
- `source`：VARCHAR(32)，默认 `internal`。MCP 外部写入的 chunk 标记为 `claude_web`。
- `external_events_generated`：BOOLEAN，默认 FALSE。标记该 chunk 的事件已在 `add_external_chunk` 时预生成，日终跑批跳过重复聚类。

`longterm_memories` 表新增：

- `source_chunk_ids`：JSONB，记录长期事件由哪些 chunk 合并而来，并通过 GIN 索引支持包含查询。
- `is_starred`：长期事件收藏标记，来源 chunk 任一被收藏则为 true。
- `source_date`：DATE，可空。记录事件对应的业务日期，由 MCP 外部写入时通过 `as_of_date` 传入。

### 3.1 Context 拼装顺序

前缀缓存边界内（稳定部分，每次请求字节一致）：

1. system prompt + 指令块（优先级、引用、思考语言、工具口播）
2. temporal_states
3. memory_cards
4. relationship_timeline
5. daily summaries

前缀缓存边界外（每次请求可能变化）：

6. 长期记忆召回
7. 远古 daily 概况补充
8. 未归档 chunk summaries
9. 动态内容（当前时间、工具记录、结束语）
10. 最近消息
    - 其中会额外带入最近几条已摘要消息，作为 chunk→正常对话的衔接窗口，避免摘要边界过于突兀
11. 当前用户消息

### 3.2 长期记忆召回

长期记忆采用双路检索 + 融合排序 + MMR 多样性筛选：

- 向量检索与 BM25 各自召回 `retrieval_top_k`
- 候选去重后进行语义与时间衰减融合
- 若事件 `is_starred=true`，融合分完成后乘以 `starred_boost_factor`
- 按 `fuse_rerank_with_time_decay` 排序
- 再用 `mmr_lambda` 做 MMR
- 最终注入 `context_max_longterm` 条

默认召回白名单为：`daily`、`daily_event`、`manual`、`app_event`；在回溯语义下可纳入 `state_archive`。

### 3.3 工具执行摘要

`tool_executions` 记录每次工具调用的短摘要与原始结果。摘要上限 150 字（短于 150 直接存原文），DB 兜底截断 1200；原始结果截断 50K，仅供排查。Context 注入与 chunk 摘要均使用 150 字摘要。Mini App 观测页会对工具执行做分页展示，并默认仅展示压缩后的原文预览。日终跑批自动清理 7 天前的记录。`api/observability.py` 的 usage 归一化会按 `base_url` 区分 DeepSeek / OpenRouter / SiliconFlow 的缓存命中口径，并同时保留 provider 实际命中值与理论命中值。

### 3.4 远古 daily 补充

长期记忆召回完成后，系统会从命中事件的 `date/source_date` 中找出近期 daily 窗口外的日期。优先选择召回事件数不少于 `archived_daily_min_hits` 的日期；不足时按该日期召回事件最高分补足，最多注入 `context_archived_daily_limit` 条 daily 概况。

该块紧跟长期记忆召回块，固定说明为：`以下是长期记忆中涉及到的较早日期的概况补充，仅作为背景，不代表近期发生`。

### 3.5 chunk 注入过滤

Context 中的 chunk summaries 只注入 `archived_by IS NULL` 的记录。已被 daily 归档的 chunk 保留在数据库中用于追溯、收藏和 Step 4 来源映射，但不重复进入常规上下文。

### 3.6 Context trace

`memory/context_builder.py` 会在每次真实构建 Context 后，在进程内记录最近一轮实际注入的记忆清单：

- `built_at`、`session_id`、`user_message_preview`
- `daily_summary_ids`
- `chunk_summary_ids`
- `archived_daily_summary_ids`
- `longterm_doc_ids`

`GET /api/memory/context-trace` 返回这份最近一次 trace。该 trace 是 Mini App 排查入口，不参与模型上下文，也不持久化；服务重启后会清空。

## 4. 对话与工具

### 4.1 对话通路

- Telegram 通过 webhook 接入
- Discord 通过 bot gateway 接入
- 两者都先进入消息缓冲，再统一构建 Context 与调用 LLM
- Telegram 侧会根据是否携带图片选择 `LLMInterface.create(config_type="vision")` 或 `LLMInterface.create(config_type="chat")`，非缓冲生成路径则固定走 `vision`

### 4.2 工具开关

人设可单独控制：Lutopia、天气、微博热搜、网页搜索、X (Twitter)。工具口播提示由 system suffix 注入，确保模型在调用工具前先说一句自然口语。

### 4.2.1 X (Twitter) 工具集

`tools/x_tool.py` 提供 11 个 OpenAI function calling 工具，通过 tweepy（OAuth 1.0a）调用 X API：

- 写入类：`post_tweet`、`like_tweet`、`unlike_tweet`、`reply_tweet`、`follow_user`、`unfollow_user`
- 读取类：`read_mentions`、`search_tweets`、`get_timeline`、`get_user`、`get_followers`

所有操作共享每日配额（`get_user` 除外），配额 key 为 `x_usage_YYYY-MM-DD`，存储在 `config` 表。写入类每次 +1，读取类按返回条数累加。内存 + DB 双写，进程重启后从 DB 恢复。Mini App 可通过 `/api/config/x-usage` 查询当日用量，通过 `/api/config` 设置 `x_daily_read_limit` 调整上限。

### 4.3 工具循环与多轮执行

`complete_with_lutopia_tool_loop` 采用「模型产出 tool_calls → 代码执行工具 → 将 tool 结果回填到 messages → 再次调用模型」的闭环方式。对模型而言，同一条工具链中的前序工具结果会保留在后续轮次上下文中，因此后续工具可以看到前一工具的执行结果。

- 外层工具循环存在轮次上限，当前实现最多 10 轮
- 单轮内若遇到拒答/输出守卫问题，允许静默重试一次
- 工具结果会以 `role=tool` 形式回填上下文，并参与后续推理
- Telegram 侧会对工具轮次中极短的中间前缀做抑制，避免误把残片当作正常口播

### 4.4 思维链展示

LLM 响应中的思维链字段会统一归一到 `thinking`，并兼容 `reasoning_content`、`reasoning`、`thoughts`、`<thinking>...</thinking>` 等格式。Telegram 端在支持时使用可折叠 `blockquote expandable` 展示思维链；若模型或网关返回了混合包裹内容，则会先拆分思维链与正文，再分别渲染。

同时，LLM usage 归一化会把 DeepSeek / 部分网关的 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`cached_tokens`、`cache_read_input_tokens` 合并成更稳定的缓存命中统计，并且不再因为多模态图片消息而强制关闭 OpenRouter cache control。

## 5. 微批与日终跑批

### 5.1 微批摘要

未摘要消息达到阈值后，系统会生成 chunk 摘要并写入 `summaries` 表。摘要前会注入人物称呼锚点；工具执行结果按 `assistant_message_id` 内联到对应对话轮次中，与对话一起作为摘要输入，避免工具信息与对话脱节。上下文侧会额外保留少量已摘要消息作为过渡窗口。

chunk 生命周期：生成后长期保留；日终 Step 2 生成 daily 后不删除 chunk，而是写入 `archived_by=<daily_id>` 标记归档。归档日期口径与读取当天 chunk 一致，使用 `COALESCE(source_date::date, created_at::date)` 匹配业务日；这是为了兼容 `source_date` 字段加入前的旧 chunk，避免旧 chunk 进入 daily 后仍因 `source_date` 为空显示为未归档。

### 5.2 日终跑批流程

日终跑批由进程内 `schedule_daily_batch()` 按数据库 `daily_batch_hour` 配置定时触发（东八区），按业务日执行五步。无参触发时，当前时间早于配置触发时刻则处理前一天，达到或晚于触发时刻才处理当天；显式传入 `YYYY-MM-DD` 的手动补跑不受影响。`run_daily_batch.py` 仍保留为独立命令行入口（手动补跑 / 重试子进程调用）。

1. Step 1：到期 temporal_states 结算
2. Step 2：生成今日小传
3. Step 3：记忆卡片 Upsert + relationship_timeline
4. Step 3.5：从今日小传提取时效状态操作
5. Step 4：事件聚类 + 描述打分 + 长期事件入库
6. Step 5：Chroma GC

### 5.3 Step 4

Step 4 使用当天按时间顺序排列的 chunk 列表作为输入。聚类前先过滤掉 `external_events_generated=TRUE` 的外部 chunk（其事件已在 `add_external_chunk` 时预生成），仅对内部 chunk 执行聚类。聚类完成后，调用 `archive_external_chunks_by_daily()` 回填外部 chunk 的 `archived_by`。若当天仅有外部 chunk，则跳过聚类，直接回填。

默认开启 `STEP4_SPLIT_MODE=True`，将旧的单次 LLM 调用拆成两段：

1. Step 4a：只做 chunk 聚类，输出 `[[chunk_id, ...], ...]`。
2. Step 4b：逐组生成事件描述、`score` 与 `arousal`。

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

Step 4 结果只写事件片段，不再写 daily 小传向量。事件写入 `longterm_memories.source_chunk_ids`，并根据来源 chunk 的 `is_starred` 汇总出事件的 `is_starred`。

## 6. 记忆召回策略

长期记忆召回以语义相关性为主、时间衰减为辅，并通过 MMR 保证多样性，避免同质内容扎堆。收藏事件会在融合分后乘以 `starred_boost_factor`，再进入 MMR，避免收藏完全绕过多样性约束。记忆卡片用于稳定保存角色/用户的重要事实；时效状态用于临时状态与动作规则。

## 7. Mini App 与配置管理

Settings 页管理 API 配置，Config 页管理运行参数，包括缓冲延迟、摘要阈值、最近原文条数与 Telegram 分段参数。

Memory 页的 summaries 列表支持对 chunk 点星收藏；收藏状态会通过 `PATCH /api/memory/summaries/{id}/star` 同步到引用该 chunk 的长期事件与 Chroma metadata。

Memory 页的 summaries 与长期记忆列表还支持“只看本轮”排查：

- summaries 调用 `GET /api/memory/summaries?context_only=true`，按最近一次 Context trace 中的 summary id 返回实际注入条目；可继续按 `summary_type` 限定 chunk / daily。
- 长期记忆调用 `GET /api/memory/longterm?context_only=true`，按最近一次 Context trace 中的 Chroma doc id 返回实际注入条目；可继续按 `summary_type` 限定类型。
- 前端用蓝色”本轮”标签标记最近一次 context 实际注入的摘要和长期记忆。

## 8. MCP Memory Server

### 8.1 端点与鉴权

MCP 服务器以 ASGI 中间件形式挂载在 `/mcp/memory` 路径下，支持两个子路径：

- `/mcp/memory/{token}/sse` — SSE 连接（GET）
- `/mcp/memory/{token}/messages/` — POST 消息（由 MCP server 通过 SSE 自动下发给客户端，客户端无需手动构造）

鉴权方案为 URL 内嵌 token，原因：Claude.ai Custom Connector 不支持 Authorization header 认证（仅支持无认证或 OAuth 2.1 + DCR），故将 token 内嵌于 URL 路径中。

- token 格式：64 字符十六进制字符串（`[a-f0-9]{64}`）
- 匹配 `MCP_WEB_READ_TOKEN` → 绑定 read scope（7 个只读工具）
- 匹配 `MCP_WEB_WRITE_TOKEN` → 绑定 write scope（7 个只读工具 + 1 个写入工具）
- 都不匹配 → 404（非 401，避免攻击者通过响应码判断 token 存在性）

`root_path` 设为 `/mcp/memory/{token}`，使 MCP server 在 SSE `endpoint` 事件中自动下发含 token 的 messages URL，客户端后续 POST 请求自然携带 token。

### 8.2 工具清单

**读工具（7 个）：**

| 工具 | 说明 |
|---|---|
| `search_memories` | 向量 + BM25 双路召回搜索长期记忆，支持 type_filter、source_filter。默认 type_filter 为 `daily_event`、`manual`、`app_event` |
| `get_recent_summaries` | 分页列出 summaries，支持按日期、天数、类型、来源、归档状态过滤 |
| `get_memory_cards` | 获取记忆卡片列表 |
| `get_temporal_states` | 列出全部 temporal_states（含已停用） |
| `get_relationship_timeline` | 全部关系时间线 |
| `get_persona` | 获取单个人设配置详情 |
| `get_context_trace` | 最近一次 context 构建时实际注入的记忆清单 |

**写工具（1 个）：**

| 工具 | 说明 |
|---|---|
| `add_external_chunk` | 从网页端 Claude 整理的对话摘要写入记忆库。仅在用户明确说出「整理这个窗口」「写进记忆库」等显式指令时调用。支持 `as_of_date` 参数补录历史窗口，补录后需在服务器执行 `python run_daily_batch.py YYYY-MM-DD` 重跑当日 daily。LLM 拆分事件 → summaries 写 chunk 留底（`[APP端]` 前缀） → longterm_memories 逐条写事件（`summary_type=app_event`） → ChromaDB embedding → BM25 |

### 8.3 审计日志

所有 `call_tool` 调用（含鉴权失败）写入 `mcp_audit_log` 表：

| 列 | 说明 |
|---|---|
| `token_scope` | read / write / `__auth__`（鉴权失败时） |
| `tool_name` | 工具名称，鉴权失败时为 `__auth__` |
| `arguments` | JSONB，工具参数 |
| `result_status` | success / error |
| `error_message` | 错误信息 |
| `called_at` | 调用时间（东八区） |

### 8.4 日志脱敏

uvicorn access log 中的 token 路径由 `_RedactMcpTokenFilter` 自动替换为 `***`。中间件在鉴权通过后会将 `scope[“path”]` 重写为内部路径（`/sse` 或 `/messages/`），原始路径保存在 `scope[“mcp_original_path”]` 中。

## 9. 外部写入（External Chunk）

### 9.1 数据结构

`summaries` 表新增两列：

- `source`：VARCHAR(32)，默认 `internal`。MCP 外部写入的 chunk 标记为 `claude_web`。
- `external_events_generated`：BOOLEAN，默认 FALSE。标记该 chunk 的事件已在 `add_external_chunk` 时预生成，日终跑批不应重复处理。

外部 chunk 的事件 `summary_type` 为 `app_event`（非 `daily_event`），以区分日终跑批产出的事件。`longterm_memories` 同步记录 `source_date` 以支持历史补录。

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
| 双路召回 | 向量检索 + BM25 关键词检索 |
| MMR 多样性 | 在召回结果中避免同质内容扎堆 |
| 事件拆分 | Step 4 将当天 chunk 列表合并为可独立记忆的事件片段 |
| 远古 daily 补充 | 长期召回命中较早日期时补充对应 daily 概况 |
| 收藏加权 | 收藏 chunk 会提升其派生长期事件的召回权重 |
| Context trace | 记录最近一次实际注入的摘要与长期记忆，供 Mini App “只看本轮”排查 |
| 时效状态 | 临时状态会自动结算并可改写为历史事实 |
| Tool 执行记录 | 保存工具调用摘要，供后续上下文与微批使用 |
| MCP Memory Server | URL 内嵌 token 鉴权的 MCP SSE 端点，供 Claude.ai 等外部客户端读写记忆 |
| 外部写入 | MCP add_external_chunk 写入的 chunk 标记 source=claude_web，事件 summary_type=app_event，日终跳过重复聚类。支持 as_of_date 历史补录 |

## 11. 结语

本 v3 文档按当前实现重写，作为 CedarStar 记忆系统的主说明文档。后续若代码演进，应直接更新 v3 正文，不再通过补丁式追加历史修订说明。
