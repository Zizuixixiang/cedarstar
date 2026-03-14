# CedarStar 项目进度报告

## 一、已完成文件及作用

| 文件/目录 | 作用描述 |
|-----------|----------|
| main.py | 项目主入口，负责初始化并启动所有组件 |
| requirements.txt | 项目依赖清单，包含FastAPI、Discord.py、python-telegram-bot等 |
| .env | 环境变量配置文件（敏感信息存储） |
| config.py | 配置管理模块，使用python-dotenv读取.env配置 |
| bot/ | 机器人模块目录 |
| bot/__init__.py | Bot模块初始化文件 |
| bot/discord_bot.py | Discord机器人实现，接收消息→调用LLM→回复 |
| bot/telegram_bot.py | Telegram机器人实现，复用消息缓冲逻辑，与平台对象解耦 |
| llm/ | 大模型接口模块目录 |
| llm/__init__.py | LLM模块初始化文件 |
| llm/llm_interface.py | LLM接口实现，封装AI API调用 |
| memory/ | 记忆存储模块目录 |
| memory/__init__.py | Memory模块初始化文件 |
| memory/database.py | 短期记忆数据库模块，使用SQLite存储对话消息 |
| memory/micro_batch.py | 微批处理模块，实现日内微批处理逻辑 |
| memory/context_builder.py | Context构建模块，组装完整的对话上下文 |
| memory/daily_batch.py | 日终跑批处理模块，实现每日三步流水线处理 |
| memory/vector_store.py | 向量存储模块，封装ChromaDB操作 |
| memory/bm25_retriever.py | BM25关键词检索模块 |
| memory/reranker.py | Reranker重排模块，使用Cohere API对检索结果重排序 |
| tools/ | MCP插件工具箱目录 |
| tools/__init__.py | Tools模块初始化文件 |
| tools/location.py | 定位工具模块（占位实现） |
| tools/weather.py | 天气工具模块（占位实现） |
| services/ | 第三方服务集成目录 |
| services/__init__.py | Services模块初始化文件 |
| services/wx_read.py | 微信读书服务模块（占位实现） |

## 二、未完成部分

### 核心功能实现
- [x] Discord Bot基础事件处理 ✓
- [x] 大模型调用接口开发 ✓
- [x] 记忆存取模块实现 ✓

### 工具模块
- [ ] tools/weather.py 天气工具实现（已有占位实现，需完善功能）
- [ ] tools/location.py 定位工具实现（已有占位实现，需完善功能）
- [ ] MCP工具注册机制

### 服务模块
- [ ] services/wx_read.py 微信读书对接（已有占位实现，需完善功能）

### 测试验证
- [x] 项目启动测试 ✓
- [x] 模块导入关系验证 ✓
- [x] 依赖项安装检查 ✓

## 三、遗留问题与注意事项

1. **环境变量注意事项**：
   - .env文件中已配置DISCORD_BOT_TOKEN、LLM_API_KEY等敏感信息
   - 确保代理服务器正在运行（如果启用代理）

2. **依赖安装**：
   - 需要安装requirements.txt中的依赖包
   - 首次运行会自动创建SQLite数据库文件（cedarstar.db）

3. **Python环境**：
   - 确保Python已正确安装并添加到系统路径

## 四、后续工作计划

1. **工具与服务完善**：
   - 实现weather和location工具的具体功能
   - 开发MCP工具注册机制
   - 完善wx_read.py的API对接功能

2. **测试与验证**：
   - 编写完整的测试用例
   - 验证模块导入关系
   - 创建启动验证脚本

3. **功能增强**：
   - 添加更多Discord命令
   - 实现向量数据库（ChromaDB）集成
   - 添加对话上下文管理

4. **部署与文档**：
   - 生成安装部署文档
   - 创建用户使用指南
   - 编写API文档

## 五、更新说明
- 2026-03-14: 移除 FastAPI 网关相关待办项，现阶段不做
- 2026-03-14: 完成四项核心任务：
  1. 修复目录结构：所有6个错误创建为文件夹的路径已修正为文件
  2. 创建config.py：使用python-dotenv读取.env配置
  3. 实现llm/llm_interface.py：封装AI API调用，支持OpenAI和Anthropic模型
  4. 实现bot/discord_bot.py：Discord机器人完整实现，支持消息处理、命令和对话历史
- 2026-03-14: 修复memory/database.py中的save_message函数签名不匹配问题
- 2026-03-14: 添加MAX_HISTORY_MESSAGES配置项到config.py和.env
- 2026-03-14: 重要bug修复：
  1. 修复llm_interface.py中chat函数参数错误（self.generate("", ...) → self.generate(message, ...)）
  2. 修复discord_bot.py中字典访问错误（msg.role → msg['role']）
  3. 修复函数调用参数不匹配（max_messages → limit）
  4. 修复配置验证调用错误（config.validate_config() → validate_config()）
- 2026-03-14: 数据库表结构重建完成：
  1. 重建messages表：新增is_summarized和character_id字段，调整字段顺序
  2. 新建memory_cards表：支持用户记忆卡片存储，包含维度枚举校验
  3. 新建summaries表：支持对话摘要存储
  4. 更新所有增删改查函数以对齐新字段
  5. 更新discord_bot.py中的save_message调用，添加character_id="sirius"
- 2026-03-14: 为summaries表添加summary_type字段：
  1. 在summaries表中添加summary_type字段，类型为TEXT，默认值为'chunk'
  2. 更新save_summary函数以支持summary_type参数，支持'chunk'和'daily'两种类型
  3. 更新get_summaries函数以支持按summary_type筛选
  4. 更新相关便捷函数以支持新的参数
  5. 添加类型校验，确保summary_type只能是'chunk'或'daily'
- 2026-03-14: 更新memory_cards表的dimension枚举值：
  1. 备份数据库文件
  2. 将dimension枚举值从9个中文值改为7个英文值：
     - preferences (偏好与喜恶)
     - interaction_patterns (相处模式)
     - current_status (近况与生活动态)
     - goals (目标与计划)
     - relationships (重要关系)
     - key_events (重要事件)
     - rules (相处规则与禁区)
  3. 更新save_memory_card函数中的枚举值验证
  4. 更新update_memory_card函数中的枚举值验证
  5. 更新相关函数注释和文档
  6. 清空memory_cards表中的现有数据（按用户要求不进行数据迁移）
- 2026-03-14: 新建daily_batch_log表：
  1. 创建daily_batch_log表，用于记录每日批处理状态
  2. 表结构：
     - batch_date DATE PRIMARY KEY（如 2026-03-14）
     - step1_status INTEGER DEFAULT 0（0=未开始，1=已完成）
     - step2_status INTEGER DEFAULT 0
     - step3_status INTEGER DEFAULT 0
     - error_message TEXT（记录失败时的报错信息）
     - created_at DATETIME DEFAULT CURRENT_TIMESTAMP
     - updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  3. 添加相关增删改查函数：
     - save_daily_batch_log: 保存或更新每日批处理日志
     - get_daily_batch_log: 获取指定日期的批处理日志
     - get_recent_daily_batch_logs: 获取最近的批处理日志列表
     - update_daily_batch_step_status: 更新指定日期的批处理步骤状态
  4. 添加对应的便捷函数

- 2026-03-14: 实现日内微批处理逻辑：
  1. 在config.py中添加摘要API配置项：
     - SUMMARY_API_KEY: 摘要API密钥（独立于主LLM API）
     - SUMMARY_API_BASE: 摘要API基础URL
     - SUMMARY_MODEL_NAME: 摘要模型名称（默认gpt-3.5-turbo）
     - SUMMARY_TIMEOUT: 摘要API超时时间（默认60秒）
     - SUMMARY_MAX_TOKENS: 摘要最大token数（默认500）
     - MICRO_BATCH_THRESHOLD: 微批处理触发阈值（默认50条）
  2. 在database.py中添加辅助函数：
     - mark_messages_as_summarized_by_ids: 根据消息ID列表批量标记消息为已摘要
     - get_unsummarized_count_by_session: 获取指定会话中未摘要消息数量
     - get_unsummarized_messages_by_session: 获取指定会话中最早的未摘要消息列表
  3. 创建memory/micro_batch.py微批处理模块：
     - SummaryLLMInterface: 摘要专用的LLM接口类，使用独立配置
     - check_and_process_micro_batch: 检查并处理微批处理
     - process_micro_batch: 执行微批处理（异步）
     - generate_summary_for_messages: 为消息列表生成摘要
     - trigger_micro_batch_check: 触发微批处理检查的便捷函数
  4. 修改discord_bot.py集成微批处理：
     - 在保存消息后异步触发微批处理检查
     - 使用asyncio.create_task确保不阻塞主流程

- 2026-03-14: 实现 context 组装逻辑：
  1. 在database.py中添加新的查询函数：
     - get_all_active_memory_cards: 获取所有激活的记忆卡片（全局查询）
     - get_recent_daily_summaries: 获取最近的每日摘要（全局查询）
     - get_today_chunk_summaries: 获取今天的所有 chunk 摘要（全局查询）
     - get_unsummarized_messages_desc: 获取指定会话中最新的未摘要消息列表（用于 context 构建）
  2. 在config.py中添加新的配置项：
     - SYSTEM_PROMPT: 系统提示词配置
     - CONTEXT_MAX_RECENT_MESSAGES: context 构建时最多包含的最近消息数（默认40）
     - CONTEXT_MAX_DAILY_SUMMARIES: context 构建时最多包含的每日摘要数（默认5）
  3. 创建memory/context_builder.py context 构建模块：
     - ContextBuilder: 负责组装完整的对话上下文
     - build_context: 便捷函数，按照优先级从上到下拼装：
       1. system prompt：从配置读取，保持原样
       2. memory_cards：查询 memory_cards 表中 is_active=1 的所有记录，按维度格式化后拼入
       3. daily summary：查询 summaries 表中 summary_type='daily'，按 created_at 倒序取最近 5 条，然后翻转为正序（按时间从老到新）后拼入
       4. chunk summary：查询今天的 summary_type='chunk' 记录（全局查询，不按 session_id 筛选），附带其来源标识，按时间正序拼入
       5. 最近消息：查询当前 session_id 下 is_summarized=0 的消息，按 created_at 倒序取 40 条，再正序排列后拼入
     - 组装完成后返回一个结构，包含 system prompt 和 messages 数组，直接可以传给 LLM API
  4. 在llm/llm_interface.py中添加generate_with_context方法：
     - 支持使用完整的 messages 数组生成回复
     - 专门用于 context builder 构建的完整消息数组
  5. 修改discord_bot.py集成新的 context 构建逻辑：
     - 替换原有的历史消息获取逻辑
     - 使用 context builder 构建完整的对话上下文
     - 调用 generate_with_context 方法生成回复
     - ChromaDB 召回那层现在先留空（加一个占位注释），等第四阶段再填

- 2026-03-14: 实现日终跑批处理逻辑：
  1. 创建memory/daily_batch.py日终跑批处理模块：
     - DailyBatchProcessor: 日终跑批处理器类，负责执行每日的三步流水线处理
     - schedule_daily_batch: 定时调度日终跑批处理，每天东八区（Asia/Shanghai）晚上23:00自动触发
     - trigger_daily_batch_manual: 手动触发日终跑批处理的便捷函数
     - 实现断点续跑逻辑：每次触发前先查 daily_batch_log 表，已完成的步骤直接跳过，从未完成的步骤继续
  2. 实现 Step 1 - 生成今日小传：
     - 取今天所有 summary_type='chunk' 的摘要记录，加上今天剩余 is_summarized=0 的原始消息
     - 调用 SUMMARY 模型生成一份今日小传，写入 summaries 表 summary_type='daily'
     - 写入成功后再删除今天的 chunk 记录，并将这批剩余原始消息的 is_summarized 批量更新为 1
     - step1_status 更新为1
  3. 实现 Step 2 - 更新记忆卡片（Upsert）：
     - 把今日小传内容发给 LLM，判断是否包含属于以下7个维度的新信息：
       preferences / interaction_patterns / current_status / goals / relationships / key_events / rules
     - 有新信息则查 memory_cards 表，没有对应维度就 INSERT，有就合并重写后 UPDATE
     - interaction_patterns 维度特别说明：只记录有具体对话支撑的行为观察，不做性格定论，新旧矛盾时并存保留并注明日期
     - step2_status 更新为1
  4. 实现 Step 3 - 价值打分与冷库归档：
     - 让 LLM 给今日小传打1-10分的长期保留价值分
     - ≥7分则将其向量化后存入 ChromaDB（占位，第四阶段填充），<7分跳过
     - step3_status 更新为1
  5. 在main.py里集成定时任务调度：
     - 重构main.py为异步主函数，支持同时运行Discord机器人和日终跑批定时调度器
     - 时区写死 Asia/Shanghai，触发时间23:00
     - 添加完整的日志配置和错误处理机制

- 2026-03-14: 完成第四阶段 - 长期记忆向量检索：
  1. 安装依赖：chromadb、jieba、zhipuai
  2. 新建memory/vector_store.py，封装 ChromaDB 操作：
     - ZhipuEmbedding: 智谱 embedding-3 模型客户端，支持文本向量化
     - VectorStore: ChromaDB 管理器，提供 add_memory、search_memory、delete_memory 等操作
     - 数据目录存在 cedarstar/chroma_db/
     - 支持元数据包含 date、session_id、summary_type 等字段
  3. 更新config.py添加 ZHIPU_API_KEY 配置项
  4. 更新.env添加 ZHIPU_API_KEY 占位配置
  5. 回填日终跑批 Step 3 的占位：
     - daily_batch.py 里打分 ≥7 分的今日小传，调用 add_memory() 存入 ChromaDB
     - doc_id 用 daily_{batch_date} 格式
  6. 回填context_builder.py里 ChromaDB 召回的占位注释：
     - 用用户当前输入调 search_memory()，取 top 5 结果
     - 结果原样拼入 context，后续给 Reranker 用
  7. 更新requirements.txt添加新依赖

- 2026-03-15: 实现 BM25 关键词检索：
  1. 安装 rank_bm25 依赖
  2. 新建memory/bm25_retriever.py，实现 BM25 关键词检索：
     - BM25Retriever: 使用 jieba 分词 + rank_bm25 实现 BM25 检索
     - 检索数据来源是 ChromaDB 里存的全部记忆文本
     - 实现缓存机制：模块加载时构建一次索引，提供 refresh_index() 方法更新索引
     - search_bm25(query, top_k=5): 对用户输入分词后检索，返回 top 5 结果
  3. 在vector_store.py中添加get_all_memories()方法，支持 BM25 检索器获取所有文档
  4. 在daily_batch.py的 Step 3 中成功写入 ChromaDB 之后，调用 refresh_index() 更新 BM25 索引
  5. 在context_builder.py中实现双路融合：
     - 向量检索 top 5 + BM25 top 5，按 doc_id 去重，得到最多 10 条候选
     - 添加占位注释等 Reranker 填充
  6. 更新requirements.txt添加 rank_bm25 依赖

- 2026-03-15: 实现 Reranker 重排功能：
  1. 安装 cohere 依赖
  2. 更新config.py添加 COHERE_API_KEY 配置项
  3. 更新.env添加 COHERE_API_KEY 占位配置
  4. 新建memory/reranker.py，实现异步 Reranker：
     - Reranker: 使用 Cohere Rerank API 对检索结果进行重排序
     - 支持异步网络调用，使用 cohere.AsyncClient 确保不阻塞事件循环
     - rerank(query, candidates, top_n=2): 接收用户查询和双路检索返回的候选列表，调用 Cohere Rerank API（模型用 rerank-multilingual-v3.0，支持中文），返回重排后得分最高的 top_n 条
  5. 更新context_builder.py实现并发执行逻辑和 Reranker 集成：
     - 添加build_context_async()异步方法，支持 Reranker 重排序
     - 添加_build_vector_search_section_async()异步方法，实现并发执行：
       - 使用 asyncio.gather 并行执行向量检索和 BM25 检索
       - 等待这两路都返回结果并完成合并去重（得到最多 10 条候选）后
       - 再异步 await 调用 rerank() 取 top 2
       - 将这 2 条作为最终的长期记忆注入 context
     - 替换原有的占位注释，实现完整的 Reranker 集成
  6. 更新requirements.txt添加 cohere 依赖

- 2026-03-15: 实现 Telegram 机器人集成：
  1. 安装 python-telegram-bot 依赖
  2. 更新config.py添加 TELEGRAM_BOT_TOKEN 配置项
  3. 更新.env添加 TELEGRAM_BOT_TOKEN 占位配置
  4. 更新requirements.txt添加 python-telegram-bot 依赖
  5. 创建bot/telegram_bot.py Telegram 机器人模块：
     - TelegramBot: 处理 Telegram 事件，调用 LLM 生成回复
     - 复用现有的消息缓冲逻辑，与平台对象解耦
     - session_id 格式定为 telegram_{chat_id}，platform 字段写 telegram
     - 消息处理流程完全复用：存 messages 表 → 触发微批检查 → context_builder 组装 → LLM 回复 → 发回 Telegram
     - 支持命令：/start, /help, /model, /clear
  6. 更新数据库支持 platform 字段：
     - 在messages表中添加platform字段，默认值为'discord'
     - 更新save_message函数支持platform参数
     - 更新便捷函数save_message支持platform参数
  7. 更新Discord bot调用save_message时添加platform="discord"
  8. 重构main.py实现三任务并行启动：
     - Discord 机器人（在后台线程中运行）
     - Telegram 机器人（使用python-telegram-bot v20+的异步启动方式：app.initialize() + app.start() + app.updater.start_polling()）
     - 日终跑批定时调度器
     - 确保多端应用和定时任务能真正并行运行，互不阻塞
  9. 更新Telegram bot的run方法，支持异步启动模式

- 2026-03-15: 实现后端准备工作：
  1. **平台常量定义**：
     - 在config.py中添加Platform类，定义平台常量：TELEGRAM, DISCORD, BATCH, SYSTEM, RIKKAHUB
     - 所有写入platform字段的地方必须引用这个常量，不允许直接写字符串字面量
  2. **日志入库**：
     - 在database.py中创建logs表：id, created_at, level, platform, message, stack_trace
     - 创建memory/async_log_handler.py异步日志处理器
     - 使用queue.Queue和后台独立线程实现真正的异步非阻塞入库
     - 架构防坑：Python原生logging.Handler.emit是同步的，使用独立线程不卡住主事件循环
  3. **Token用量记录**：
     - 在database.py中创建token_usage表：id, created_at, platform, prompt_tokens, completion_tokens, total_tokens, model
     - 在llm_interface.py中添加token使用量保存功能
     - 每次LLM调用拿到响应后，从返回的usage字段取数据异步写入数据库
     - 支持异步保存，不阻塞主事件循环
  4. **思维链存储**：
     - 在messages表新增thinking字段（TEXT，可为空）
     - 在database.py初始化逻辑中加入ALTER TABLE ADD COLUMN语句以平滑升级已有数据库
     - 在llm_interface.py中添加思维链内容提取功能
     - 支持DeepSeek R1的reasoning_content字段和Gemini的thinking字段
     - 调用LLM时，如果响应里包含思维链内容，提取并存入thinking字段

## 六、验证方法与启动指南

### 验证方法

1. **配置验证**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" config.py
   ```
   应该输出"配置验证通过"和各个配置项的状态。

2. **数据库测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" memory/database.py
   ```
   应该输出数据库测试结果，包括保存、获取和清理消息的测试。

3. **LLM接口测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" -c "import sys; sys.path.insert(0, '.'); from llm.llm_interface import test_llm_interface; test_llm_interface()"
   ```
   如果LLM_API_KEY已设置，会测试API调用；否则会输出配置检查结果。

4. **微批处理测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" memory/micro_batch.py
   ```
   会测试微批处理配置和摘要LLM接口初始化。

5. **数据库微批处理函数测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" -c "
import sys
sys.path.insert(0, '.')
from memory.database import get_database
db = get_database()

# 测试会话
test_session = 'micro_batch_test_session'

# 清理测试数据
db.clear_session_messages(test_session)

# 保存测试消息
for i in range(55):
    db.save_message('user', f'测试消息 {i+1}', test_session)

# 测试未摘要消息计数
count = db.get_unsummarized_count_by_session(test_session)
print(f'未摘要消息数量: {count}')

# 测试获取未摘要消息
messages = db.get_unsummarized_messages_by_session(test_session, limit=50)
print(f'获取到最早的50条未摘要消息: {len(messages)} 条')

# 测试批量标记
if messages:
    message_ids = [msg['id'] for msg in messages]
    updated = db.mark_messages_as_summarized_by_ids(message_ids[:10])  # 只标记前10条
    print(f'批量标记消息为已摘要: {updated} 条')

# 清理测试数据
db.clear_session_messages(test_session)
    print('微批处理数据库函数测试完成')
"
   ```

6. **Context 构建器测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" memory/context_builder.py
   ```
   会测试 context 构建器的基本功能，包括构建完整的对话上下文。

7. **完整集成测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" -c "
import sys
sys.path.insert(0, '.')
from memory.context_builder import build_context
from memory.database import get_database

# 创建测试数据
db = get_database()
test_session = 'integration_test_session'

# 清理测试数据
db.clear_session_messages(test_session)

# 保存测试消息
for i in range(5):
    db.save_message('user', f'测试用户消息 {i+1}', test_session)
    db.save_message('assistant', f'测试助手回复 {i+1}', test_session)

# 测试 context 构建
context = build_context(test_session, '你好，这是一个集成测试消息')
print(f'Context 构建成功:')
print(f'  System Prompt 长度: {len(context[\"system_prompt\"])}')
print(f'  Messages 数量: {len(context[\"messages\"])}')
print(f'  第一个消息角色: {context[\"messages\"][0][\"role\"] if context[\"messages\"] else \"无消息\"}')

# 清理测试数据
db.clear_session_messages(test_session)
print('集成测试完成')
"
   ```

8. **日终跑批处理测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" memory/daily_batch.py
   ```
   会测试日终跑批处理器的初始化和基本功能。

9. **手动触发日终跑批测试**：
   ```bash
   cd cedarstar
   "D:\Environment_coding\Python312\python.exe" -c "
import sys
sys.path.insert(0, '.')
from memory.daily_batch import trigger_daily_batch_manual

# 手动触发日终跑批处理
print('手动触发日终跑批处理...')
success = trigger_daily_batch_manual()

if success:
    print('日终跑批处理执行成功')
else:
    print('日终跑批处理执行失败（可能是配置问题或没有数据）')
"
   ```

10. **Telegram 机器人测试**：
    ```bash
    cd cedarstar
    "D:\Environment_coding\Python312\python.exe" bot/telegram_bot.py
    ```
    会启动Telegram机器人（需要设置TELEGRAM_BOT_TOKEN环境变量）。
    注意：这是一个长期运行的程序，按Ctrl+C可以停止。

11. **主程序集成测试**：
    ```bash
    cd cedarstar
    "D:\Environment_coding\Python312\python.exe" main.py
    ```
    会启动完整的CedarStar系统，包括Discord机器人、Telegram机器人和日终跑批定时调度器。
    注意：这是一个长期运行的程序，按Ctrl+C可以停止。

### 启动完整系统

**方法1：使用新的主程序入口**（推荐）
```bash
cd cedarstar
"D:\Environment_coding\Python312\python.exe" main.py
```
这会并行启动三个任务：
- Discord 机器人（在后台线程中运行）
- Telegram 机器人（使用异步 polling 模式）
- 日终跑批定时调度器（每天23:00自动触发）

**方法2：单独启动Discord机器人**（传统方式）
```bash
cd cedarstar
"D:\Environment_coding\Python312\python.exe" bot/discord_bot.py
```

**方法3：单独启动Telegram机器人**
```bash
cd cedarstar
"D:\Environment_coding\Python312\python.exe" bot/telegram_bot.py
```

**方法4：手动触发日终跑批处理**
```bash
cd cedarstar
"D:\Environment_coding\Python312\python.exe" -c "import sys; sys.path.insert(0, '.'); from memory.daily_batch import trigger_daily_batch_manual; trigger_daily_batch_manual()"
```

### 注意事项

1. **时区设置**：日终跑批处理使用东八区（Asia/Shanghai）时间，每天23:00自动触发
2. **断点续跑**：如果某天的批处理失败，下次触发时会从失败的步骤继续执行
3. **日志文件**：所有日志会同时输出到控制台和 `cedarstar.log` 文件
4. **异步架构**：新的 `main.py` 使用异步架构，同时运行Discord机器人、Telegram机器人和日终跑批调度器
5. **依赖安装**：需要安装以下依赖包：
   ```bash
   "D:\Environment_coding\Python312\python.exe" -m pip install pytz python-telegram-bot
   ```
6. **Telegram 配置**：需要在 `.env` 文件中设置 `TELEGRAM_BOT_TOKEN`，否则 Telegram 机器人不会启动
7. **并发要求**：Telegram 机器人使用 python-telegram-bot v20+ 的异步启动方式（`app.initialize() + app.start() + app.updater.start_polling()`），确保不阻塞主事件循环

### 启动Discord机器人

**方法1：在项目目录下直接运行**
```bash
cd cedarstar
"D:\Environment_coding\Python312\python.exe" bot/discord_bot.py
```

**方法2：使用相对路径**
```bash
cd d:\Workspace\PythonProject
"D:\Environment_coding\Python312\python.exe" cedarstar/bot/discord_bot.py
```

**方法3：使用python命令（如果已修复PATH）**
```bash
cd cedarstar
python bot/discord_bot.py
```
**注意**：需要禁用Microsoft Store别名或将D盘Python添加到PATH

### 注意事项

1. **Python路径**：使用完整路径 `D:\Environment_coding\Python312\python.exe` 而不是 `python`
2. **工作目录**：需要在 `cedarstar` 目录下运行，或者使用完整文件路径
3. **环境变量**：`.env` 文件必须在 `cedarstar` 目录下
4. **代理设置**：如果启用代理，确保代理服务器正在运行

### 故障排除

1. **ModuleNotFoundError**：确保在正确的目录下运行，或使用完整Python路径
2. **Discord登录失败**：检查 `.env` 文件中的 `DISCORD_BOT_TOKEN`
3. **LLM API失败**：检查 `.env` 文件中的 `LLM_API_KEY` 和网络连接
4. **数据库错误**：首次运行会自动创建 `cedarstar.db` 文件

### 使用说明

机器人启动后，在Discord中：
- 提及机器人（@机器人名字）它会回复
- 私聊机器人直接对话
- 使用命令：`!ping`、`!clear`、`!model`、`!help`
- 对话历史会自动保存在SQLite数据库中
