# CedarClio 记忆系统架构完整版 v2

> 本文档整理自设计讨论全程，已合并 GPT System Design Review 的两项基础设施补丁。
> 可直接作为交给 Cline 的施工需求文档。
> 文档版本：2026-03-22（与 CedarStar 实现对齐：可配置跑批时刻 / 半衰期 / GC 闲置天 / Context 条数等，见 §2.1、§3.1）

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
| SQLite（cedarstar.db） | 聊天原文、摘要、记忆卡片、关系时间线、时效状态、跑批日志 |
| ChromaDB | 长期记忆向量存储（双轨：daily 小传 + event 事件片段） |
| BM25 内存索引 | 关键词双路召回，服务启动时强制预热 |
| daily_batch.py | 东八区整点定时跑批（`Asia/Shanghai`）；触发小时由 SQLite `config.daily_batch_hour` 配置，**默认 23**，热更新 |

---

## 一、数据库表结构（SQLite）

> **索引建设原则（必须遵守）：**
> 所有高频用于 `WHERE` 过滤和 `ORDER BY` 排序的字段必须建立索引。
> 不加索引的后果：跑批和 Context 组装时全表扫描，数据量积累后会严重拖慢甚至卡死系统。

---

### 1. messages（聊天原文）

```sql
CREATE TABLE messages (
    id              VARCHAR   PRIMARY KEY,
    session_id      VARCHAR   NOT NULL,
    role            VARCHAR   NOT NULL,   -- user / assistant
    content         TEXT      NOT NULL,
    created_at      DATETIME  NOT NULL,
    is_summarized   INTEGER   NOT NULL DEFAULT 0   -- 0=未总结，1=已总结
);

-- 必须建立的索引
CREATE INDEX idx_messages_session_created   ON messages (session_id, created_at);
CREATE INDEX idx_messages_is_summarized     ON messages (is_summarized);
CREATE INDEX idx_messages_session_summarized ON messages (session_id, is_summarized);
```

> `(session_id, is_summarized)` 复合索引是微批处理查询"未总结消息数量"时的核心性能保障。

---

### 2. summaries（摘要表）

```sql
CREATE TABLE summaries (
    id              VARCHAR   PRIMARY KEY,
    session_id      VARCHAR   NOT NULL,
    summary_type    VARCHAR   NOT NULL,   -- chunk / daily
    content         TEXT      NOT NULL,
    source_date     DATE      NOT NULL,
    created_at      DATETIME  NOT NULL
);

-- 必须建立的索引
CREATE INDEX idx_summaries_session_type_date ON summaries (session_id, summary_type, source_date);
CREATE INDEX idx_summaries_source_date        ON summaries (source_date);
```

---

### 3. memory_cards（7维度核心记忆卡片）

每个 `(user_id, dimension)` 严格只保留 1 张 `is_active=1` 的卡片，数据库层面加唯一约束。

```sql
CREATE TABLE memory_cards (
    id          VARCHAR   PRIMARY KEY,
    user_id     VARCHAR   NOT NULL,
    dimension   VARCHAR   NOT NULL,   -- 枚举，见下方
    content     TEXT      NOT NULL,
    is_active   INTEGER   NOT NULL DEFAULT 1,   -- 1=激活，0=归档
    created_at  DATETIME  NOT NULL,
    updated_at  DATETIME  NOT NULL,
    UNIQUE (user_id, dimension)   -- 强制每维度唯一激活
);

-- 必须建立的索引
CREATE INDEX idx_memory_cards_user_active     ON memory_cards (user_id, is_active);
CREATE INDEX idx_memory_cards_is_active       ON memory_cards (is_active);
```

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
    content             TEXT      NOT NULL,   -- AI 第一人称写的一句话
    source_summary_id   VARCHAR               -- 软引用，关联当天 summaries 表的 ID
);

-- 必须建立的索引
CREATE INDEX idx_relationship_timeline_created ON relationship_timeline (created_at);
```

> 大多数普通的一天不写入，只有真正有意义的节点才追加，宁可漏记不要滥记。

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
    error_message   TEXT,
    created_at      DATETIME  NOT NULL,
    updated_at      DATETIME  NOT NULL
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
  parent_id        关联回当天 daily 的 doc_id（软引用，非硬外键）
  event_type       highlight / rant / milestone / health 等
  base_score       按单条事件独立打分
  halflife_days
  hits             初始值 0
  last_access_ts   初始值为入库时间戳（float）
```

> **ChromaDB 使用 doc_id 作为原生主键**，反写时直接 `collection.update(ids=[uid_list], ...)` 实现 O(1) 更新，禁止通过 metadata 字段过滤查询来定位记录。

> **parent_id 是软引用：** 查询不到时静默降级返回 null，不抛异常，不做强外键约束。

### 半衰期映射规则（与 `memory/daily_batch.py` 一致）

| 打分区间 | halflife_days |
|---|---|
| 8-10 分 | 600 天 |
| 4-7 分 | 200 天 |
| 1-3 分 | 30 天 |

---

### 2.1 运行参数表 `config`（SQLite，Mini App「助手配置」可改）

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
   + 今天日内的碎片摘要（summary_type=chunk）

5. 最近若干条原生消息（`is_summarized=0`，条数=`short_term_limit`，默认 40，按时间正序）

6. 引用指令注入（Prompt 末尾死命令，见 Citation 机制）
```

### 两阶段长期记忆召回

**阶段一：粗排（向量层 + BM25 双路并行）**

- 语义路：ChromaDB 向量相似度 Top K（K=`retrieval_top_k`，默认 5）
- 关键词路：BM25 关键词匹配 Top K（同上）
- 两路结果合并去重后进入父子折叠（实现为去重合并 + 折叠，非严格 RRF）

**阶段一·五：父子折叠（去重）**

合并结果按 parent_id 分组，同一天的 daily 和其 event 片段为一组，只保留组内综合得分最高的一条。防止同源内容同时进入后续排序抢占名额，citation 混淆问题同时消除。

**阶段二：精排（内存层）**

Python 后端在内存中对折叠后的候选套用综合权重公式重排：

```
最终得分 = (语义相似度归一化 × 0.8) + (时间衰减复活得分 × 0.2)

衰减复活得分计算：
  age_days     = (当前时间 - last_access_ts) / 86400
  base         = clamp(base_score, 0, 10)
  boost        = 1 + 0.35 × ln(1 + hits)
  decay_score  = base × exp(-ln(2) / halflife_days × age_days) × boost
  → 归一化后参与融合
```

**截断输出：** 取重排后前 Top N 条（N=`context_max_longterm`，默认 3，Mini App「助手配置」+ SQLite `config`）；**同步路径**（无 Cohere）在折叠后直接截断为 N 条。注入 Prompt 时每条必须带 uid 前缀：

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

## 四、写逻辑：回复返回后的网关拦截

LLM 生成回复后，网关在存库和下发前执行以下拦截流程：

```
1. 正则提取与去重
   用 \[\[used:(.*?)\]\] 提取所有 UID，放入 Set 去重

2. 静默加分（复活机制，后台异步，不阻塞主线程）
   collection.update(
       ids=uid_list,
       metadatas=[{"hits": hits+1, "last_access_ts": 当前时间戳}, ...]
   )
   直接用 doc_id 作为主键更新，O(1)，禁止 metadata 过滤查询

3. 文本清洗（阅后即焚）
   re.sub(r'\[\[used:.*?\]\]', '', reply_text)

4. 落库与下发
   将清洗后的纯净文本推送 Telegram/Discord
   INSERT 进 messages 表
```

---

## 五、日内微批处理

**触发条件：** 当前 session 中 `is_summarized=0` 的消息达到阈值时异步触发（阈值=`chunk_threshold`，默认 50，优先 SQLite `config`）。

**执行流程：**

```
1. 查询最早的「阈值」条 is_summarized=0 的消息
2. 调用摘要模型生成《碎片摘要》
3. 写入 summaries 表，summary_type=chunk（直接落库，防止重启丢失）
4. 批量 UPDATE 本批消息的 messages.is_summarized=1
```

---

## 六、日终跑批流水线（东八区整点，默认 23:00）

时区固定 `Asia/Shanghai`；**触发小时**由 SQLite `config.daily_batch_hour` 控制（默认 **23**），调度循环每次睡眠前读库，支持热更新。支持断点续跑，每步完成后更新 `daily_batch_log`。

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

将以下内容统一提交给摘要模型，生成唯一的《今日小传》：
- Step 1 输出的到期事件字符串（若有）
- 今日所有的 chunk 碎片摘要
- 今日剩余未满微批阈值（默认 50，可配置）的原始消息

写入 `summaries` 表，`summary_type=daily`，`source_date=today`。

**今日小传写入成功后，再删除今天的 chunk 摘要记录。删除操作必须在写入成功之后执行，不可提前。**

更新 `step2_status=1`。

---

### Step 3：核心卡片 Upsert + 追加关系时间线

基于 Step 2 的今日小传：

**卡片 Upsert：**
- 让模型判断今日内容是否包含 7 个维度的新信息
- 有则查询该 dimension 是否已有 `is_active=1` 的卡片
  - 没有 → INSERT
  - 有 → 融合重写后 UPDATE
- 数据库唯一约束 `UNIQUE(user_id, dimension)` 从结构上防止膨胀

**关系时间线追加：**
- 由模型判断今日是否发生了关系里程碑事件（含 Step 1 刚完结的重要时效事件）
- 有则以第一人称精简描述后 INSERT 入 `relationship_timeline`
- 宁可漏记不要滥记，普通的一天不写入

更新 `step3_status=1`。

---

### Step 4：长期记忆全量入库（ChromaDB Insert）

对今日小传进行价值打分（1-10 分），映射 `halflife_days`。

**轨道一：daily 入库**
```
doc_id: daily_{batch_date}
写入今日小传全文 + 完整 metadata（含 hits=0，last_access_ts=当前时间戳）
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

查询 ChromaDB，提取 `last_access_ts` 距今超过 **T** 天的记录（**T=`gc_stale_days`**，默认 **180**，优先 SQLite `config`）。

在内存中试算当前衰减得分，满足以下**全部条件**才执行物理删除：

```
条件一：衰减得分 < 0.05
条件二：last_access_ts 距今超过 T 天（T 可配置，默认 180）
条件三：没有以此 doc_id 为 parent_id 的存活子节点
```

条件三防止孤儿记录：只要有任何一个 event 片段还活着，当天的 daily 就不会被 GC 删除。

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
| temporal_states 独立表 | 时效信息需要到期自动归档，不能和长期记忆混存 |
| relationship_timeline 追加不覆盖 | 关系进展有时序，合并会压平时间线 |
| System Prompt 人工维护 | 全自动更新导致人格悄无声息漂移 |
| daily_batch_log 断点续跑 | 跑批崩溃不从头重跑，从断点继续 |
| Step 1+2 合并入库防重复向量 | temporal_state 归档事件若单独入库会和 daily 语义重叠竞争名额 |

---

*文档版本：v2 · 2026-03-21 · 已合并 GPT System Design Review 补丁*
