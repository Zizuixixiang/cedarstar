# CedarClio 记忆系统架构完整版 v2

> 本文档整理自设计讨论全程，已合并 GPT System Design Review 的两项基础设施补丁。
> 可直接作为交给 Cline 的施工需求文档。
> 文档版本：2026-04-19；**2026-04-19** 修订：§六 **Step 3.5**（三操作 JSON、**`update_temporal_state_expire_at`**、整段解析失败 **raise** 与 **3** 次重试、全败 **Telegram**）、Step 2 原文拼接说明、Step 5 / §一.4 DDL 与 PostgreSQL 一致。**与 CedarStar 实现对齐**：主库为 **PostgreSQL（asyncpg）**；可配置跑批时刻 / 半衰期 / GC 闲置天 / Context 条数等，见 §2.1、§3.1；**CedarClio 输出 Guard** 见 **§三（补丁）**，以 `llm/llm_interface.py` 为准；**Chroma `metadata.summary_type`、Mini App 长期记忆列表** 见 **§二.1**；**`state_archive` 写入与 Context 召回白名单** 见 **§二.1**、**§三「两阶段长期记忆召回」**、**§六 Step 3**；**`daily_batch_log.retry_count`、跑批/微批 Telegram 熔断与智谱 embedding / `add_memory` 写入重试** 见 **§六**、**§五**、**§二**；**Telegram 缓冲用户原文在调用上游模型之前落库**、**`enable_weather_tool` / `get_weather`（JSON 对象字符串）** 见 **§三（补丁）同步链路** 与仓库 **`ARCHITECTURE.md`**，以代码为准）

---

## 零、系统总览

### 设计原则
- AI 本体只负责聊天和主动调用工具，其余全部由网关在后台调度
- System Prompt 由人工维护，不走自动写入流程，作为人格的稳定锚点
- 记忆分层管理：原文可核对、摘要不断线、卡片找得到、向量长期沉淀

### 组件分工

| 组件 | 职责 |
|---|---|
| Gateway（网关） | 组装上下文、拦截回复、触发后台任务、服务启动时初始化 BM25 |
| PostgreSQL（asyncpg，`DATABASE_URL`） | 聊天原文、摘要、记忆卡片、关系时间线、时效状态、跑批日志、`config` 键值等 |
| ChromaDB | 长期记忆向量存储（双轨：daily 小传 + event 事件片段） |
| BM25 内存索引 | 关键词双路召回，服务启动时强制预热 |
| daily_batch.py | 东八区整点定时跑批（`Asia/Shanghai`）；触发小时由 PostgreSQL `config.daily_batch_hour` 配置，**默认 23**，热更新 |

---

## 一、数据库表结构（PostgreSQL，asyncpg）

实现以 `memory/database.py` 的 `create_tables` / `migrate_database_schema` 为准。以下为与 CedarClio 记忆管线相关的核心字段说明（类型以 PostgreSQL 为参照）。

> **索引建设原则（必须遵守）：**
> 所有高频用于 `WHERE` 过滤和 `ORDER BY` 排序的字段必须建立索引。
> 不加索引的后果：跑批和 Context 组装时全表扫描，数据量积累后会严重拖慢甚至卡死系统。

---

### 1. messages（聊天原文）

除对话正文外，实现侧包含平台、多模态与缓冲落库等字段，例如：

| 字段 | 说明 |
|------|------|
| `id` | `SERIAL` 主键 |
| `session_id` | 会话标识（Discord / Telegram 格式见仓库约定） |
| `role` | `user` / `assistant` |
| `content` | 正文 |
| `created_at` | 时间戳 |
| `is_summarized` | 微批摘要标记 |
| `user_id` / `channel_id` / `message_id` | 平台消息关联 |
| `character_id` | 人设 / 角色 |
| `platform` | 来源平台（写入时使用 `config.Platform` 常量） |
| `thinking` | 助手思维链落库（若上游传入） |
| `media_type` / `image_caption` / `vision_processed` | 图片等多模态与视觉批处理 |

必须建立的索引包括 `(session_id, created_at)`、`is_summarized`、`(session_id, is_summarized)` 等（见迁移脚本）。

---

### 2. summaries（摘要表）

| 字段 | 说明 |
|------|------|
| `id` | `SERIAL` 主键 |
| `session_id` | 来源会话或 `daily_batch` 等 |
| `summary_text` | 摘要正文 |
| `start_message_id` / `end_message_id` | chunk 对应消息范围（日终 daily 可为占位） |
| `summary_type` | `chunk` / `daily`（**仅 `summaries` 表**；枚举约束以代码为准） |
| `source_date` | 业务日期（迁移补齐） |
| `created_at` | 创建时间 |

索引包括 `(session_id, created_at)`、`(session_id, summary_type, source_date)`、`(source_date)` 等。

---

### 3. memory_cards（7维度核心记忆卡片）

每个 **`(user_id, character_id, dimension)`** 在业务上只保留 1 张有效卡片：**无数据库级 `UNIQUE` 约束**，由日终 Step 3 等路径 **应用层 Upsert（查后 INSERT / UPDATE）** 保证不膨胀。

```sql
-- 示意（完整列与默认值以 migrate 为准）
CREATE TABLE memory_cards (
    id                SERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    character_id      TEXT NOT NULL,
    dimension         TEXT NOT NULL,
    content           TEXT NOT NULL,
    updated_at        TIMESTAMP DEFAULT NOW(),
    source_message_id TEXT,
    is_active         INTEGER DEFAULT 1
);
```

索引包括 `(user_id, character_id, dimension, updated_at)`、`(user_id, is_active)`、`(is_active)` 等。

**7个固定维度（枚举，代码层面写死，不可新增，不设"其他"兜底项）：**

| dimension | 说明 |
|---|---|
| `preferences` | 偏好与喜恶 |
| `interaction_patterns` | AI 对用户的行为观察。只记录有具体对话支撑的行为模式，禁止抽象性格定论；新旧观察存在矛盾时并存保留并注明日期 |
| `current_status` | 近况与生活动态 |
| `goals` | 目标与计划 |
| `relationships` | 重要关系 |
| `key_events` | 重要事件 |
| `rules` | 相处规则与禁区 |

> 真正无法归类的信息留在 daily summary，不强行写入卡片。

---

### 4. relationship_timeline（关系时间线）

**Append-Only，只追加不覆盖，禁止合并重写。**

```sql
CREATE TABLE relationship_timeline (
    id                  VARCHAR   PRIMARY KEY,
    created_at          DATETIME  NOT NULL,
    event_type          VARCHAR   NOT NULL,   -- milestone / emotional_shift / conflict / daily_warmth
    content             TEXT,                 -- 可空；与迁移一致（event_type 有 CHECK）
    source_summary_id   VARCHAR               -- 软引用，关联当天 summaries 表的 ID
);

-- 必须建立的索引
CREATE INDEX idx_relationship_timeline_created ON relationship_timeline (created_at);
```

> 大多数普通的一天不写入，只有真正有意义的节点才追加，宁可漏记不要滥记。

> **（以代码为准）** 日终 Step 3 调用 **`insert_relationship_timeline_event(..., created_at=datetime.combine(date.fromisoformat(batch_date), time(23, 59, 59)))`**，**`created_at`** 落在 **`batch_date` 业务日** 末刻，便于按日历排序；库表 **`insert_relationship_timeline_event`** 仍允许省略 **`created_at`**（**`DEFAULT NOW()`**）。

---

### 5. temporal_states（时效状态表）

用于处理"有明确终点"的短期情况，如生病吃药、备考阶段、临时约定等。

```sql
CREATE TABLE temporal_states (
    id              VARCHAR   PRIMARY KEY,
    state_content   TEXT      NOT NULL,   -- 状态描述，如"最近得了胃病"
    action_rule     TEXT,                 -- AI 应对策略，如"每天提醒按时吃药"
    expire_at       DATETIME  NOT NULL,
    is_active       INTEGER   NOT NULL DEFAULT 1,   -- 1=生效，0=已失效
    created_at      DATETIME  NOT NULL
);

-- 必须建立的索引
CREATE INDEX idx_temporal_states_is_active  ON temporal_states (is_active);
CREATE INDEX idx_temporal_states_expire_at  ON temporal_states (expire_at, is_active);
```

**生命周期：** active → 到达 expire_at → 跑批 Step 1 结算转化归档

---

### 6. daily_batch_log（断点续跑记录表）

```sql
CREATE TABLE daily_batch_log (
    batch_date      DATE      PRIMARY KEY,   -- 如 2026-03-16
    step1_status    INTEGER   NOT NULL DEFAULT 0,   -- 0=未完成，1=已完成
    step2_status    INTEGER   NOT NULL DEFAULT 0,
    step3_status    INTEGER   NOT NULL DEFAULT 0,
    step4_status    INTEGER   NOT NULL DEFAULT 0,
    step5_status    INTEGER   NOT NULL DEFAULT 0,
    retry_count     INTEGER   NOT NULL DEFAULT 0,   -- 已排队延迟重试次数；>=3 时不再 spawn 子进程并发 Telegram 熔断
    error_message   TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
```

---

## 二、ChromaDB 向量库结构（双轨）

### 轨道一：今日小传（daily）

```
doc_id:    daily_{batch_date}，如 daily_2026-03-16
text:      今日小传正文
metadata:
  date             归档日期
  session_id       来源 session
  summary_type     固定为 "daily"
  base_score       大模型原始打分（1-10）
  halflife_days    映射出的初始半衰期（1–3 分→30 天，4–7 分→200 天，8–10 分→600 天）
  arousal          情绪强度（float，0.0–1.0；平静约 0.1，情绪激烈事件约 0.8+）
  hits             初始值 0
  last_access_ts   初始值为入库时间戳（float）
```

### 轨道二：独立事件片段（event）

从今日小传里拆分出语义独立、值得单独检索的事件。若当天主题单一，模型可判断"无需拆分"。

```
doc_id:    daily_{batch_date}_event_0、_event_1...
text:      单条事件正文
metadata:
  date
  session_id       来源 session（与轨道一一致）
  summary_type     实现为 **"daily_event"**（与轨道一 **"daily"** 区分）
  parent_id        关联回当天 daily 的 doc_id（软引用，非硬外键）
  base_score       按单条事件独立打分（与当日 daily 同批打分策略）
  halflife_days
  arousal          继承自当日 daily 的 arousal 值（float，0.0–1.0）
  hits             初始值 0
  last_access_ts   初始值为入库时间戳（float）
```

> **ChromaDB 使用 doc_id 作为原生主键**，反写时直接 `collection.update(ids=[uid_list], ...)` 实现 O(1) 更新，禁止通过 metadata 字段过滤查询来定位记录。

> **（以代码为准）写入韧性：** `memory/vector_store.ZhipuEmbedding.get_embedding` 对智谱 HTTP **429/503** 最多 **3** 次尝试、间隔 **2s**；`VectorStore.add_memory` 对 **embedding + `collection.add`** 整段最多 **3** 次、间隔 **1s**，仍失败则返回 **`False`**（由 `daily_batch` 等决定是否整步失败）。

> **parent_id 是软引用：** 查询不到时静默降级返回 null，不抛异常，不做强外键约束。

### 半衰期映射规则（与 `memory/daily_batch.py` 一致）

| 打分区间 | halflife_days |
|---|---|
| 8-10 分 | 600 天 |
| 4-7 分 | 200 天 |
| 1-3 分 | 30 天 |

### 2.1 Chroma `metadata.summary_type`（与 `summaries.summary_type` 区分）

- **PostgreSQL `summaries` 表**的 **`summary_type`** 只有 **`chunk`（微批）** 与 **`daily`（今日小传文本）**，供 Context 注入与跑批衔接；**`chunk` 不落 Chroma**。
- **ChromaDB** 每条向量另有 **`metadata.summary_type`**，由写入路径决定，常见取值：
  - **`daily`**：轨道一主文档（`doc_id=daily_{batch_date}`）
  - **`daily_event`**：轨道二事件片段（实现侧字段名；文档「轨道二」表若未列此项，以代码为准）
  - **`state_archive`**：日终 Step 3 对 **`preferences` / `current_status`** 合并时，模型输出 **`merged` + `discarded`**；**仅当 `discarded` 非 null** 时，经轻量 LLM 改写（失败则降级原文 + `rewrite_failed`）后写入向量库；metadata 含 **`archived_at`**、**`original_dimension`** 等（**不再**在合并前整卡入库）
  - **`manual`**：Mini App 手动新增长期记忆（`doc_id` 前缀 `manual_`）
- **Mini App「长期记忆」类型筛选**按 Chroma 的 **`metadata.summary_type`** 过滤；**不提供 `chunk`**。

---

### 2.1 运行参数表 `config`（PostgreSQL，Mini App「助手配置」可改）

以下键优先读库，缺失或非法时回退默认值；**不必改 `.env`** 即可热更新（与 `api/config.py`、`memory/context_builder.py` 等一致）。

| 键 | 默认 | 作用 |
|---|---|---|
| `buffer_delay` | 5 | 消息缓冲合并等待秒数 |
| `short_term_limit` | 40 | Context 注入近期原文条数 |
| `chunk_threshold` | 50 | 微批触发未摘要消息条数 |
| `context_max_daily_summaries` | 5 | 注入 `daily` 小传条数 |
| `context_max_longterm` | 3 | 双路召回+精排后注入长期记忆条数 |
| `daily_batch_hour` | 23 | 东八区日终跑批整点（0–23） |
| `relationship_timeline_limit` | 3 | 关系时间线注入条数 |
| `gc_stale_days` | 180 | Step 5 GC：`last_access_ts` 闲置天数阈值 |
| `gc_exempt_hits_threshold` | 10 | Step 5 GC：hits 豁免阈值；达到此值的记忆跳过所有删除条件 |
| `retrieval_top_k` | 5 | 向量路 / BM25 路各自粗排条数 |

---

## 三、读逻辑：Context 组装 + BM25 冷启动

### ⚠️ 补丁二：BM25 冷启动初始化（必须实现）

BM25 倒排索引常驻内存，服务重启后内存清空，关键词召回直接瘫痪。

**强制要求：** Gateway 启动流程中必须增加 `init_bm25_index()` 生命周期钩子，在任何请求被处理之前完成 BM25 预热。

```python
# Gateway 启动时强制执行，伪代码示意
async def on_startup():
    await init_bm25_index()   # 从 ChromaDB 全量加载文档重建内存索引
    # 其他初始化...

def init_bm25_index():
    """
    从 ChromaDB 中全量捞取所有文档，
    用 jieba 分词后重建 BM25 倒排索引，写入内存缓存。
    ChromaDB 为空时优雅降级为空索引，不阻断服务启动。
    """
```

**触发时机：**
- 服务冷启动时（必须，阻塞直至完成）
- 每次日终跑批 Step 4 写入新向量后（增量更新）
- Mini App 手动触发重建时（可选）

---

### Context 组装顺序（每次收到消息时）

按以下优先级从上到下拼接，构成发给 LLM 的完整 Prompt：

```
1. System Prompt（人工维护，不走自动流程）
   + temporal_states 中 is_active=1 的全部记录

2. 7张核心记忆卡片（memory_cards，is_active=1）
   + relationship_timeline 最近 N 条（N=`relationship_timeline_limit`，默认 3），注入前按时间正序

3. 长期记忆（两阶段召回，见下方详述）

4. 近期摘要：今日小传若干条（`summary_type=daily`，条数=`context_max_daily_summaries`，默认 5）
   + 碎片摘要（`summary_type=chunk`）：实现为 **`get_today_chunk_summaries()`**，条件为 **内容日** `COALESCE(source_date::date, created_at::date) <=` 东八区今日（**含尚未被日终卷入的积压**；见「五 / 六」）

5. 最近若干条原生消息（`is_summarized=0`，条数=`short_term_limit`，默认 40，按时间正序）

6. 引用指令注入（Prompt 末尾死命令，见 Citation 机制）
```

**与 `MEMORY_BLOCK_PRIORITY_DIRECTIVE` 的关系：** 上表是 **system 内各区块的拼接顺序**（越靠后的块在字面上离「当前轮」越远、越像背景）。**冲突消解优先级**由 **`memory/context_builder.py`** 中 **`MEMORY_BLOCK_PRIORITY_DIRECTIVE`** 全文定义（**近期消息 > chunk碎片摘要 > 时效状态 > 记忆卡片 = 关系时间线 > 每日小传 > 长期记忆**；同类型块内日期更近优先；**`action_rule`** 与近期消息对状态描述的覆盖关系见该常量）；在 **`_assemble_full_system_prompt` 中于 `system_prompt`（人设正文）之后、第一个记忆块（如 `temporal_states`）之前注入**，与上表拼接顺序 **并列存在**——模型在信息冲突时按该优先级消解，**不因**字面拼接顺序而自动覆盖。**引用死命令与思维链语言要求**（`MEMORY_CITATION_DIRECTIVE`、`THINKING_LANGUAGE_DIRECTIVE`）仍在 **system 块末尾**（各记忆块与桥接语之后）。

### 两阶段长期记忆召回

**阶段一：粗排（向量层 + BM25 双路并行）**

- 语义路：ChromaDB 向量相似度 Top K（K=`retrieval_top_k`，默认 5），**`collection.query` 带 `where`**：`metadata.summary_type` ∈ **`daily` / `daily_event` / `manual`**（**默认排除** **`state_archive`**）
- 关键词路：BM25 关键词匹配 Top K（同上），对 **`metadata.summary_type`** 做**相同白名单**过滤（实现：`memory/bm25_retriever.py` 在排序后跳过非白名单文档）
- **回溯扩展：** 当 **`memory/retrieval.is_retrospect_query(user_message)`** 为真（用户消息命中关键词表）时，白名单**追加** **`state_archive`**。检测与过滤在 **`memory/context_builder`** 内基于本轮 **`user_message`** 执行（**非** gateway 单独入参，以代码为准）
- 两路结果合并去重后进入父子折叠（实现为去重合并 + 折叠，非严格 RRF）

**阶段一·五：父子折叠（去重）**

合并结果按 parent_id 分组，同一天的 daily 和其 event 片段为一组，只保留组内综合得分最高的一条。防止同源内容同时进入后续排序抢占名额，citation 混淆问题同时消除。

**阶段二：精排（内存层）**

Python 后端在内存中对折叠后的候选套用综合权重公式重排：

```
最终得分 = (语义相似度归一化 × 0.8) + (时间衰减复活得分 × 0.2)

衰减复活得分计算：
  age_days        = (当前时间 - last_access_ts) / 86400   # 优先 last_access_ts；缺失时兜底 created_at（见 `fuse_rerank_with_time_decay` / `_memory_age_days`）
  base            = clamp(base_score, 0, 10)
  boost           = 1 + 0.35 × ln(1 + hits)
  arousal         = metadata.arousal ?? 0.1          # 历史数据无此字段时兜底 0.1
  effective_hl    = halflife_days × (1 + arousal)    # arousal 越高半衰期越长，记忆消退越慢
  decay_score     = base × exp(-ln(2) / effective_hl × age_days) × boost
  → 归一化后参与融合
```

**截断输出：** 取重排后前 Top N 条（N=`context_max_longterm`，默认 3，Mini App「助手配置」+ PostgreSQL `config`）；**同步路径**（无 Cohere）在折叠后直接截断为 N 条。注入 Prompt 时每条必须带 uid 前缀：

```
[uid:daily_2026-03-16] 今天下班后去看了电影……
[uid:daily_2026-03-16_event_0] 最近胃不舒服……
```

### Citation 引用指令（Prompt 末尾死命令）

```
如果你在生成回复时参考了上述历史记忆，必须在回复文本末尾标注引用，
格式为 [[used:uid]]，可以有多个。
```

---

## 三（补丁）、CedarClio 输出 Guard（v2，以代码为准）

> 实现位置：`llm/llm_interface.py`；Telegram 接入：`bot/telegram_bot.py`；异步摘要/跑批：`memory/micro_batch.py`、`memory/daily_batch.py`。与「读逻辑」并列，描述**模型输出侧**在入库/展示前的安检与重试策略。

### 通用规则

1. **正文相对思维链：** `body_for_output_guard(accumulated)` 自左向右剥离**完整**的思维链块；支持多种开闭标签（`COT_TAG_PAIRS`，含 `<redacted_thinking>`、`<thinking>`、`<reasoning>`、反引号 `think` 块等，按标签 open 长度优先匹配）。  
2. **未闭合保底：** 若仅有开标签、在**开标签之后内层累计超过** `_GUARD_COT_UNCLOSED_INNER_MAX`（默认 **12000** 字符）仍无对应闭标签，则从该偏移起**强制视为正文**并启用拒答检测（避免截断或漏写 `</...>` 导致永远不检测）。  
3. **流式：** `generate_stream` 对正文 delta 前置掐断；SSE 返回字典含 `guard_refusal_abort`。

### 同步链路（实时对话，Telegram）

- **用户原文落库时机（CedarStar）：** `bot/telegram_bot._generate_reply_from_buffer` 在 **`await LLMInterface.create()`** 之后**立即**写入合并后的用户 **`messages`** 行（**`combined_raw`**），再 **`build_context`** 与调用上游模型，避免 HTTP 4xx/5xx、超时或工具环失败时用户侧「话被吞」。  
- **天气工具（可选）：** 人设 **`persona_configs.enable_weather_tool`** 开启时注册 **`get_weather`**；**`tools/weather.execute_weather_function_call`** 返回可解析为 **JSON object** 的字符串（如 **`{"summary":…}`**），以满足部分网关对 **`function_response` / Struct** 的要求；**`append_tool_exchange_to_messages`** 的 **`execution_log`** 不记录 **`get_weather`**（与 Lutopia 内部记忆附录分工一致）。  
- 首轮若流式/Anthropic 路径判定需拦截，**最多静默重试 1 次**（在最后一条 user 文本末附加 `TELEGRAM_GUARD_PROMPT_APPEND`）。  
- 仍失败或正文为空时，使用**情境兜底文案**（`_TELEGRAM_GUARD_ROLEPLAY_FALLBACK`），不向用户展示模型安全拒答原文。

### 异步链路（chunk 摘要、daily Step2/3、合并卡片等）

- `batch_one_shot_with_async_output_guard`：**最多 5 次**、温度递减，第 2 次起附加 `ASYNC_BATCH_GUARD_PROMPT_APPEND`。  
- 仍失败则抛 `CedarClioOutputGuardExhausted`：**chunk 摘要不写入、不标记已摘要**；日终各步按代码**跳过写入或降级**（如 Step1 用原文兜底、Step2 失败则中止等）。

### Step 4 结构化数值（score / arousal）

- **不走** Guard 文本重试；`coerce_score_and_arousal_defaults` 尽力解析 JSON/正则，失败则 **score=5、arousal=0.1**，继续向量归档。

---

## 四、写逻辑：回复返回后的网关拦截

LLM 生成回复后，网关在存库和下发前执行以下拦截流程：

```
1. 正则提取与去重
   用 \[\[used:(.*?)\]\] 提取所有 UID，放入 Set 去重
   （实现侧可兼容模型误写的单括号 [used:…]、全角【used:…】，均参与 hits 更新与剥离，见 bot/reply_citations.py）

2. 静默加分（复活机制，后台异步，不阻塞主线程）
   collection.update(
       ids=uid_list,
       metadatas=[{"hits": hits+1, "last_access_ts": 当前时间戳}, ...]
   )
   直接用 doc_id 作为主键更新，O(1)，禁止 metadata 过滤查询

3. 文本清洗（阅后即焚）
   移除规范格式 [[used:…]]，并兼容剥离 [used:…]、【used:…】等误写后再下发

4. 落库与下发
   将清洗后的纯净文本推送 Telegram/Discord
   INSERT 进 messages 表
```

---

## 五、日内微批处理

**触发条件：** 当前 session 中 `is_summarized=0` 且 `vision_processed=1` 的消息达到阈值时异步触发（阈值=`chunk_threshold`，默认 50，优先 PostgreSQL **`config.chunk_threshold`**）。

**执行流程：**

```
1. 查询最早的「阈值」条未摘要消息
2. 调用摘要模型生成《碎片摘要》
3. 写入 summaries 表，summary_type=chunk；source_date = chunk_source_date_from_messages（本批最后一条消息的东八区日历日；与按日筛选/日终卷入一致）
4. 批量 UPDATE 本批消息的 messages.is_summarized=1
```

**（以代码为准）连续失败告警：** 若 chunk 摘要连续 **3** 次无法产出可落库正文（空摘要 / CedarClio Guard 跳过等），`memory/micro_batch` 经 **`bot/telegram_notify.send_telegram_main_user_text`** 向 **`TELEGRAM_MAIN_USER_CHAT_ID`** 推送告警（未配置则仅日志）；告警后模块内计数归零；任意一次**成功写入 chunk 并标记已摘要**后计数亦归零。

---

## 六、日终跑批流水线（东八区整点，默认 23:00）

时区固定 `Asia/Shanghai`；**触发小时**由 PostgreSQL **`config.daily_batch_hour`** 控制（默认 **23**）；生产多为 **cron** 调用 **`run_daily_batch.py`**，与整点对齐。支持断点续跑，每步完成后更新 `daily_batch_log`（含 **`retry_count`**）。

**（以代码为准）失败后延迟重试与熔断：** `run_daily_batch.py`、`schedule_daily_batch`（若启用）、`trigger_daily_batch_manual` 在 **`run_daily_batch` 返回失败** 时调用 **`schedule_daily_batch_retry_if_needed`**：若 **`retry_count >= 3`**，**不再**启动约 2 小时后的子进程重跑，并向 **`TELEGRAM_MAIN_USER_CHAT_ID`** 发送「需手动介入」类告警（未配置则仅日志）；若 **`< 3`**，则在 **`spawn_run_daily_batch_retry_after_hours` 成功 `Popen`** 后再 **`retry_count + 1`**，并发「已安排 2 小时后重试」通知。五步**全部成功**后 **`retry_count` 置 0**。

---

### Step 1：时效状态结算（TTL）

巡视 `temporal_states` 表，精准定位 `expire_at` 已到期且 `is_active=1` 的记录。

对每条到期记录执行：

1. **软删除：** `UPDATE is_active=0`，网关此后不再将其注入 Prompt
2. **大模型时态转化：** "进行时/祈使句" → "过去时客观事实"
   - 转化前："最近得了胃病，每天要提醒按时吃药。"
   - 转化后："2026年3月，得了一次胃病，坚持吃两周药后顺利痊愈了。"
3. 将转化后的文本暂存为字符串，供 Step 2 合并使用

更新 `step1_status=1`。

---

### Step 2：汇总今日小传（Summary Merge）

将以下内容统一提交给摘要模型，生成唯一的《今日小传》（**`batch_date`** = 当日业务日 **YYYY-MM-DD**）：
- Step 1 输出的到期事件字符串（若有）
- **chunk 碎片摘要**：**`get_today_chunk_summaries(batch_date)`**，条件为 **内容日** `COALESCE(source_date::date, created_at::date) <= batch_date`（**此前积压、尚未卷入**的 chunk 一并并入，避免孤儿碎片）

**（当前实现）**不拼接未达 `chunk_threshold` 的**原始会话消息**；若当日**既无 chunk、Step 1 又无产出**，则**不写** `summary_type=daily`、**不调用** `delete_today_chunk_summaries`（空跑 Step 2 并标记完成）。有材料生成小传时，写入 `summaries` 表，`summary_type=daily`，**`source_date=batch_date`**（与业务日一致）。

**今日小传写入成功后，再执行 `delete_today_chunk_summaries(batch_date)`**：删除 **同上界** `COALESCE(source_date::date, created_at::date) <= batch_date` 的全部 chunk。**删除必须在 daily 写入成功之后执行，不可提前。**

更新 `step2_status=1`。

---

### Step 3：核心卡片 Upsert + 追加关系时间线

基于 Step 2 的今日小传：

**卡片 Upsert（以 `memory/daily_batch.py` 为准）：**
- 七维提取 prompt 在「今日小传」前附 **7 个维度**既有记忆卡各一条（`get_latest_memory_card_for_dimension`，**不过滤 `is_active`**），供对比与**增量/冲突**判断；输出为严格 JSON。
- 各维无新信息则为 `null`；有则对该维 Upsert：**无行 → INSERT**；**有行 →** 先 **`_merge_memory_card_contents`**：**`current_status` / `preferences`** 为 JSON **`merged` / `discarded`**；**仅当 `discarded` 非 null** 时 **`_rewrite_discarded_state_for_archive`**（`batch_one_shot_with_async_output_guard`，最多 3 次）后 **`add_memory`**（`summary_type=state_archive`，可增量 BM25），再 **`update_memory_card`**；其余维仍为 **`{"content"}`** 合并后 **UPDATE**（应用层保证不膨胀，**非**依赖 `UNIQUE(user_id, dimension)` 单表约束——实现为 **`user_id`+`character_id`+`dimension`**）。

**关系时间线追加：**
- 由模型判断今日是否发生了关系里程碑事件（含 Step 1 刚完结的重要时效事件）
- 有则精简描述后 **`insert_relationship_timeline_event`**，`created_at` = **`batch_date` 日 23:59:59**（与业务日对齐）
- 宁可漏记不要滥记，普通的一天不写入

更新 `step3_status=1`。

---

### Step 3.5：从当日小传再提取时效状态（可选增强）

在 **`step3_status=1` 且 `step4_status=0`**（Step 4 尚未完成）时执行：读取刚写入的 **`summary_type=daily`** 当日正文，由模型返回**一个 JSON 对象**，描述三类操作（与 Step 1 到期结算互补——小传比 chunk 更完整，便于补漏）：

```json
{
  "new_states": [
    {"state_content": "...", "action_rule": "...", "expire_at": "YYYY-MM-DD HH:MM:SS"}
  ],
  "deactivate_ids": ["id1", "id2"],
  "adjust_expire": [
    {"id": "xxx", "new_expire_at": "YYYY-MM-DD HH:MM:SS"}
  ]
}
```

- **`new_states`**：仍具明确时效、需新入库的短期情况；逐条 **`save_temporal_state`**（与既有实现一致）。
- **`deactivate_ids`**：**仅当**小传**明确**表明某条已有状态已结束或被否定时填入对应 id，**禁止猜测**；与当前 **`is_active=1`** 的 id 交叉校验后调用 **`deactivate_temporal_states_by_ids`**，不在库或非激活 id 静默跳过。
- **`adjust_expire`**：**仅当**小传中有**明确**新时间信息时填入，**禁止猜测**；调用 **`update_temporal_state_expire_at`**，对无效 id 静默跳过。

三个数组均可为 `[]`。后处理三支串行：**新增 → 停用 → 调整到期**；任一支内失败仅 **WARNING**，不阻断其他支、不阻断 Step 4。断点续跑仍以 **`daily_batch_log` 五步**为准，本步**不单独占** `stepN_status` 列。

**重试与告警（以 `memory/daily_batch.py` 为准）：** **`_parse_step35_temporal_operations_json`** 在空响应、无法 `json.loads`、根节点非 `dict` 时返回 **`None`**；**`_step35_extract_temporal_states`** 遇 **`None`** 则 **`raise ValueError("Step 3.5 JSON 解析完全失败…")`**。**`batch_one_shot_with_async_output_guard` / `get_all_active_temporal_states`** 等未捕获异常同样外抛。**`run_daily_batch`** 在有小传正文时对 **`await _step35_extract_temporal_states(...)` 最多连续尝试 3 次**；仍失败则 **WARNING** 并 **`send_telegram_main_user_text`**（**`TELEGRAM_MAIN_USER_CHAT_ID`** 未配置则跳过；发送失败单独 **WARNING**），**不 `return False`**，**Step 4** 照常执行。**`get_daily_summary_by_date`** 失败走外层 **`except`**，**不参与**上述 3 次重试。

---

### Step 4：长期记忆全量入库（ChromaDB Insert）

对今日小传进行价值打分，prompt 要求模型同时输出两个字段：
- `score`（整数 1–10）：长期保留价值
- `arousal`（浮点 0.0–1.0）：情绪强度，不分正负；平静普通的一天约 0.1，情绪激烈的事件约 0.8+；**必须为 float，不可输出字符串**

`halflife_days` 由 `score` 映射；`arousal` 直接写入 metadata（`float(arousal)` 确保类型正确）。

**轨道一：daily 入库**
```
doc_id: daily_{batch_date}
写入今日小传全文 + 完整 metadata（含 hits=0，last_access_ts=当前时间戳，arousal=float）
```

**轨道二：event 片段拆分入库**
- 让模型判断今日小传中是否有语义独立、值得单独检索的事件
- 若当天主题单一可判断"无需拆分"
- 每个独立事件单独打分、单独 embed，doc_id 按顺序命名
- metadata 中带 `parent_id` 指向当天 daily（软引用）

**注意：** Step 1 中到期归档的时效事件已合并进今日小传统一处理，不单独再入库，避免重复向量。

**入库完成后：** 调用 `bm25_retriever.add_document_to_bm25()` 增量更新 BM25 内存索引。

更新 `step4_status=1`。

---

### Step 5：冷库垃圾回收（GC）

查询 ChromaDB，提取 `last_access_ts` 距今超过 **T** 天的记录（**T=`gc_stale_days`**，默认 **180**，优先 PostgreSQL **`config`** 表）。

在内存中逐条判断，**前置豁免优先**，随后依次检查三条删除条件：

```
前置豁免：hits >= gc_exempt_hits_threshold（T_hits，优先 PostgreSQL config 表，默认 10）→ 跳过，不删

条件一：last_access_ts 距今超过 T 天（T 可配置，默认 180）
条件二：衰减得分 < 0.05
条件三：没有以此 doc_id 为 parent_id 的存活子节点

以上四项全部满足（豁免不触发 + 三条全中）才执行物理删除。
```

条件三防止孤儿记录：只要有任何一个 event 片段还活着，当天的 daily 就不会被 GC 删除。hits 豁免确保被高频引用的记忆永久留存。

更新 `step5_status=1`。

---

## 七、System Prompt 维护原则

- System Prompt 由人工手动维护，不走自动写入流程
- 人格核心（性格、说话风格、价值观、相处边界）在此处直接编辑，不受任何记忆机制和衰减公式影响
- AI 可定期生成"人设更新建议草稿"供参考，但是否合并由人工决定
- 7 张核心记忆卡片是对 user 的动态认知，`relationship_timeline` 是关系史，两者均不等同于 System Prompt

---

## 八、各机制的设计理由速查

| 机制 | 解决的问题 |
|---|---|
| 高频字段强制建索引 | 数据积累后全表扫描拖慢跑批和 Context 组装 |
| BM25 冷启动预热钩子 | 服务重启后内存清空，关键词召回直接瘫痪 |
| 双轨 ChromaDB | 一天多件事语义稀释，粒度太粗导致检索不准 |
| 父子折叠去重 | daily 和 event 片段语义重叠，同时进 Top N 浪费 token |
| Citation 精准反写 | "进了 Top N"≠"真的被用上"，hits 失真导致正反馈偏差 |
| doc_id 作为 ChromaDB 主键 | 反写时 O(1) 直接更新，禁止 metadata 过滤查询 |
| parent_id 软引用 | 父节点被 GC 删除后静默降级，不产生孤儿报错 |
| GC 跳过有存活子节点的父 | 防止有价值的 event 片段失去溯源能力 |
| arousal 字段 | 记录情绪强度（独立于正负效价）；高 arousal 事件半衰期自动延长，不易被时间淹没 |
| hits 豁免 GC | 被反复引用说明有持续价值，不应因时间衰减被物理删除；避免「热门记忆」被误 GC |
| temporal_states 独立表 | 时效信息需要到期自动归档，不能和长期记忆混存 |
| relationship_timeline 追加不覆盖 | 关系进展有时序，合并会压平时间线 |
| System Prompt 人工维护 | 全自动更新导致人格悄无声息漂移 |
| daily_batch_log 断点续跑 | 跑批崩溃不从头重跑，从断点继续 |
| Step 1+2 合并入库防重复向量 | temporal_state 归档事件若单独入库会和 daily 语义重叠竞争名额 |

---

*文档版本：v2 · 2026-04-19（Step 3.5 三操作·解析失败 raise·外层 3 重试·全败 Telegram；`MEMORY_BLOCK_PRIORITY` 与 `context_builder` 一致）· 主库 PostgreSQL；已对齐 CedarClio 输出 Guard（多标签 CoT、未闭合保底、同步/异步重试、Step4 数值兜底）、日终 `retry_count` / Telegram 熔断、向量写入重试与微批连续失败告警；实现以仓库代码为准*
