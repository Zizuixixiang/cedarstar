v3 · 2026-04-26 重写 · 实现以代码为准

# CedarClio 记忆系统架构完整版 v3

## 一、项目概述

CedarStar 是一个具备长期记忆能力的 AI 聊天系统，支持 Telegram、Discord 与 Mini App 管理后台。系统以“短期消息 → 微批摘要 → 日终小传 → 长期记忆向量”四层记忆链为核心，并通过 PostgreSQL、ChromaDB、BM25 与 LLM 工具调用共同完成上下文组装、对话生成与离线归档。

系统关键目标：

- 对话上下文稳定可控
- 长期记忆可追溯、可检索、可清理
- 日终跑批可断点续跑
- 配置热更新，无需重启即可生效

## 二、数据与配置

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

### 2.2 `api_configs`

`api_configs.config_type` 允许：`chat`、`summary`、`vision`、`stt`、`embedding`、`search_summary`、`analysis`。

其中：

- `chat`：日常对话
- `summary`：微批 / 日终摘要
- `vision`：图片理解
- `stt`：语音转录
- `embedding`：向量嵌入
- `search_summary`：网页搜索结果压缩
- `analysis`：日终 Step 4 结构化分析与打分

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

## 三、上下文构建与召回

### 3.0 新增记忆字段（数据结构）

`summaries`：

- `archived_by`：可空，指向归档该 chunk 的 daily summary id。chunk 生成 daily 后不再删除，而是写入该字段。
- `is_starred`：是否收藏该 chunk / daily，默认 false。

`longterm_memories`：

- `source_chunk_ids`：JSONB，记录 Step 4 事件由哪些 chunk 合并而来。
- `is_starred`：事件是否收藏，来源 chunk 任一被收藏则为 true。

### 3.1 Context 拼装顺序

Context 组装时，系统按以下顺序注入信息：

1. system prompt
2. temporal_states
3. memory_cards
4. relationship_timeline
5. 长期记忆召回
6. 远古 daily 概况补充
7. daily summaries
8. 未归档 chunk summaries
9. 最近消息
10. 当前用户消息

其中长期记忆召回采用双路检索 + 融合排序 + MMR 多样性筛选：

- 向量检索与 BM25 各自召回 `retrieval_top_k`
- 候选去重后进行语义与时间衰减融合
- `is_starred=true` 的事件在融合分计算完成后乘以 `starred_boost_factor`
- 按 `fuse_rerank_with_time_decay` 排序
- 再用 `mmr_lambda` 做 MMR
- 最终注入 `context_max_longterm` 条

### 3.4 远古 daily 补充

长期记忆召回完成后，系统会检查命中的 `daily_event` 日期：

- 最近 `context_max_daily_summaries` 条 daily 已通过常规 daily 通道注入，不重复补充
- 对近期窗口外的命中日期按召回事件数分组
- 优先选择命中数不少于 `archived_daily_min_hits` 的日期
- 不足时按该日期召回事件最高分补足
- 最多注入 `context_archived_daily_limit` 条 daily 概况

该块紧跟长期记忆召回块，固定说明为：`以下是长期记忆中涉及到的较早日期的概况补充，仅作为背景，不代表近期发生`。

### 3.5 chunk 注入过滤

Context 中的 chunk 摘要只注入 `archived_by IS NULL` 的记录。已经被 daily 归档的 chunk 保留在数据库中用于追溯、收藏和 Step 4 来源映射，但不再重复进入常规上下文。

### 3.2 长期记忆过滤

长期记忆默认只召回：

- `daily`
- `daily_event`
- `manual`

当用户消息命中回溯语义时，才额外放开 `state_archive`。

### 3.3 工具执行摘要

`tool_executions` 用于记录每次工具调用的短摘要与原始结果。Context 只注入短摘要，不直接塞入长原文。

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

工具口播提示由 system suffix 注入，确保模型在调用工具前先说一句自然口语。

## 五、微批与日终跑批

### 5.1 微批摘要

当未摘要消息达到阈值后，系统会生成 chunk 摘要并写入 `summaries` 表。摘要前会注入人物称呼锚点与期间工具摘要，避免上下文断裂。

chunk 生命周期：生成后长期保留；日终 Step 2 生成 daily 后，chunk 不删除，只通过 `archived_by=<daily_id>` 标记为已归档。

### 5.2 日终跑批总流程

日终跑批由 `run_daily_batch.py` 触发，按东八区业务日执行五步：

1. Step 1：到期 temporal_states 结算
2. Step 2：生成今日小传
3. Step 3：记忆卡片 Upsert + relationship_timeline
4. Step 3.5：从今日小传提取时效状态操作
5. Step 4：analysis 结构化提取 + 事件拆分
6. Step 5：Chroma GC

### 5.3 Step 4 重写

Step 4 使用当天按时间顺序排列的 chunk 列表作为输入，由 `analysis` 配置完成结构化事件合并、`chunk_ids` 标注与 `score/arousal` 提取；若 analysis 不可用，则回退 `chat`。

Step 4 的事件拆分遵循：

- 至少 1 条事件
- 最多 `event_split_max` 条事件
- 每条事件必须返回合法 `chunk_ids`
- 模型返回的 `chunk_ids` 会与当天输入 chunk 集合求交集，非法 ID 会被过滤
- 某事件过滤后没有合法 chunk，则丢弃并记 ERROR 日志
- 连续 3 次失败后使用默认值 `score=5`、`arousal=0.1`
- 默认值兜底后继续入库，并发 Telegram 告警

Step 4 结果只写事件片段，不再写 daily 小传向量。事件写入 `longterm_memories.source_chunk_ids`，并根据来源 chunk 的 `is_starred` 汇总出事件的 `is_starred`。

## 六、记忆召回策略

### 6.1 长期记忆融合

长期记忆召回采用以下公式思路：

- 语义相关性优先
- 时间衰减辅助修正
- MMR 保证多样性

最终效果是：既尽量选中“最相关”的记忆，也避免同质内容扎堆。

### 6.1.1 收藏加权

用户可在 Mini App 收藏 chunk。收藏状态会同步影响由该 chunk 派生的长期事件：

- 任一来源 chunk 被收藏，则事件 `is_starred=true`
- Chroma metadata 同步写入 `is_starred`
- 召回融合分完成后，对收藏事件乘以 `starred_boost_factor`
- 加权后再进入 MMR，避免收藏事件完全绕过多样性约束

### 6.2 记忆卡片

`memory_cards` 用于稳定保存角色/用户的重要事实。Step 3 会按维度合并更新，而不是无限累加重复内容。

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

## 八、机制速查

| 机制 | 说明 |
|---|---|
| 消息缓冲 | 将同一会话短时间内的多条消息合并处理 |
| 微批摘要 | 达到阈值后生成 chunk 摘要 |
| 日终跑批 | 每日离线归档、更新记忆与时间线 |
| 双路召回 | 向量检索 + BM25 关键词检索 |
| MMR 多样性 | 在召回结果中避免同质内容扎堆 |
| 事件拆分 | Step 4 将 daily 小传拆为可独立记忆的事件片段 |
| 远古 daily 补充 | 长期召回命中较早日期时补充对应 daily 概况 |
| 收藏加权 | 收藏 chunk 会提升其派生长期事件的召回权重 |
| 时效状态 | 临时状态会自动结算并可改写为历史事实 |
| Tool 执行记录 | 保存工具调用摘要，供后续上下文与微批使用 |

## 九、结语

本 v3 文档按当前实现重写，作为 CedarStar 记忆系统的主说明文档。后续若代码演进，应直接更新 v3 正文，不再通过补丁式追加历史修订说明。
