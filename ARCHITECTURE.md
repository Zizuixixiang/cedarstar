# CedarStar 项目架构文档

> **2026-04-19（Portal / 天气与传感器 API / 天气工具 / Telegram 用户先行落库，以代码为准）：** **`main.py`** 注册 **`GET /daily/{full_path:path}`** → 静态 **`portal/dist`**（Vite **`base: '/daily/'`**），与 **`GET /app/{full_path:path}`**（**`miniapp/dist`**）并列。**`portal/`** 生产构建 **`VITE_PORTAL_TOKEN`**（与后端 **`MINIAPP_TOKEN`** 一致）用于 **`GET /api/*`** 的 **`X-Cedarstar-Token`**。**`api/`** 增 **`weather.py`**（**`fetch_weather_cached`**，和风 **`HEFENG_*`**）、**`sensor.py`**、**`autonomous.py`**。**`memory/database`**：**`sensor_events`**、**`autonomous_diary`**。**`persona_configs.enable_weather_tool`** → **`LLMInterface.enable_weather_tool`**；**`tools/prompts.py`**：**`OPENAI_WEATHER_TOOLS`**、**`WEATHER_TOOL_DIRECTIVE`**；**`tools/weather.py`**：**`fetch_weather`** / **`execute_weather_function_call`** 返回 **JSON 对象字符串**（如 **`{"summary":…}`**），满足 Gemini 等网关对 **`function_response` Struct** 的要求。**`tools/lutopia.append_tool_exchange_to_messages`**：**`get_weather`** 走 **`execute_weather_function_call`**；**`execution_log`** 不记录 **`get_weather`**。**Telegram** **`_generate_reply_from_buffer`**：**`await LLMInterface.create()`** 后**立即** **`save_message` 用户行**，再 **`build_context`**（传入 **`exclude_message_id=user_row_id`** 避免近期消息重复） / 调用模型；**`enable_lutopia` 或 `enable_weather_tool`**（且非 Anthropic）→ **`_telegram_stream_thinking_and_reply_with_lutopia`**（**`tools`** 按开关合并 Lutopia 与天气；**微博热搜**见 **2026-04-20** 条）。详见 §2 目录树、§3.2、§3.7。
>
> **2026-04-20（微博热搜工具 + `WEIBO_COOKIE` + 上游 520 日志 hint，以代码为准）：** **`persona_configs.enable_weibo_tool`** → **`LLMInterface.enable_weibo_tool`**；**`tools/weibo.py`**：`httpx` **GET** **`https://weibo.com/ajax/side/hotSearch`**，请求头 **`Referer: https://weibo.com/`**、**`Cookie`** 读 **`.env` 的 `WEIBO_COOKIE`**（**`config.WEIBO_COOKIE`**，易过期）；**`tools/prompts.py`**：**`OPENAI_WEIBO_TOOLS`**、**`WEIBO_HOT_TOOL_DIRECTIVE`**、**`TOOL_DIRECTIVES["weibo"]`**；**`get_weibo_hot`** → **`execute_weibo_function_call`**，返回 **`{"summary":…}`**；**`append_tool_exchange_to_messages`** 的 **`execution_log`** **不记录** **`get_weibo_hot`**，**`complete_with_lutopia_tool_loop`** 的 **`tool_pairs`** 亦不记（无 Lutopia **内部记忆**旁白）。Telegram / Discord **工具口播与多轮流式**：**`enable_lutopia` 或 `enable_weather_tool` 或 `enable_weibo_tool`**；**`complete_with_lutopia_tool_loop`** / **`_telegram_stream_thinking_and_reply_with_lutopia`** 合并 **`OPENAI_LUTOPIA_TOOLS`**、**`OPENAI_WEATHER_TOOLS`**、**`OPENAI_WEIBO_TOOLS`**。**`LLMInterface._post_with_retry`**：非 2xx 的 ERROR 日志对 **520** / **502** / **504** 附加网关/CDN **hint**。**`api/persona`**、Mini App **Persona** 增微博开关。详见 §2、§3.2、§3.3、§3.7、§5.8、配置表。
>
> **2026-04-20（Tavily `web_search` + `search_summary`，以代码为准）：** **`persona_configs.enable_search_tool`** → **`LLMInterface.enable_search_tool`**；**`.env` `TAVILY_API_KEY`**（**`config.TAVILY_API_KEY`**）→ **`tools/search.py`** **`httpx` POST `https://api.tavily.com/search`**（`max_results=5`，`include_raw_content=false`）；多条 **title / url / content** 拼装后经 **`LLMInterface.generate_with_context`** 使用激活 **`api_configs`**：**`config_type=search_summary`**（`api_key` 与 `base_url` 均非空）优先，否则 **`summary`**，输出 **≤ 约 800 tokens** 高密度摘要；**`execute_search_function_call`** 返回 **`{"summary":…}`** JSON 字符串，失败为 **`{"summary":"暂时无法搜索"}`**。**`memory/database.migrate_database_schema`**：列 **`enable_search_tool`**；**`_ensure_default_search_summary_api_config_row`** 插入占位 **`search_summary`** 行（**`is_active=0`**，名称「搜索摘要模型」）。**`api/settings`**：**`search_summary`** ∈ **`ALLOWED_API_CONFIG_TYPES`**。Mini App **Settings**「搜索摘要」Tab、**Persona**「启用搜索工具」。**`tools/prompts`**：**`OPENAI_SEARCH_TOOLS`**、**`SEARCH_TOOL_DIRECTIVE`**、**`TOOL_DIRECTIVES["search"]`**。**`append_tool_exchange_to_messages`** 的 **`execution_log`** **不记录** **`web_search`**（与天气/微博一致）。**`complete_with_lutopia_tool_loop`** / **Telegram** **`_telegram_stream_thinking_and_reply_with_lutopia`** 在口播条件为真时合并 **Lutopia / 天气 / 微博 / 搜索** tools（**`enable_lutopia` 或 `enable_weather_tool` 或 `enable_weibo_tool` 或 `enable_search_tool`**）。详见 §2、§3.2、§3.3、§3.7、§5.8–§5.9、配置表。
>
> **2026-04-26（Anthropic 1h Prompt Cache + 工具执行记录，以代码为准）：** **`memory/context_builder._assemble_full_system_prompt`** 现在返回 Anthropic `text` blocks：固定人设/规则/引用/思维链/工具口播说明、慢变记忆（`temporal_states` / `memory_cards` / `relationship_timeline` / `daily`）与 `chunk` 分别加 **`cache_control={"type":"ephemeral","ttl":"1h"}`**；当前系统时间、最近工具记录与本轮长期记忆召回置于**非缓存尾部**。长期记忆块前加提示：**“以下记忆可能来自过去日期，不代表今天发生；请以条目日期为准。”**；近期原文超过 2 条时会在倒数第 3 条加 1h cache breakpoint，形成冻结上下文前缀。**`llm/llm_interface.py`**：Anthropic 请求保留 system block array，header 使用 **`anthropic-beta: extended-cache-ttl-2025-04-11`**；OpenAI 兼容路径通过 **`_openai_compatible_messages`** 将 text blocks 压回字符串并移除 `cache_control`；Anthropic usage 透传 **`cache_creation_input_tokens`** / **`cache_read_input_tokens`**。**`memory/database`** 新增 **`tool_executions`**（每次工具调用一行，`session_id` / `turn_id` / `seq` / `tool_name` / `arguments_json` / `result_summary` / `result_raw` / `user_message_id` / `assistant_message_id` / `platform` / `created_at`），迁移与索引幂等创建。**`tools/lutopia.py`**：工具结果生成 Context 摘要并落库；超长 raw 回传模型前经 **`tool_result_for_model`** 压成短 JSON，避免长帖/网页吞掉 token。**`complete_with_lutopia_tool_loop`** 与 Telegram 流式 **`append_tool_exchange_to_messages`** 传入 `session_id` / `turn_id` / `user_message_id` 记录工具链路。**`context_builder._build_recent_tool_executions_section`** 将最近 3 个工具回合的短摘要注入非缓存尾部；**`memory/micro_batch`** 在生成 chunk 摘要时读取同一消息范围内的 `tool_executions.result_summary`，避免工具信息在 chunk/daily 记忆链中断档。详见 §3.3、§3.4.2、§3.4.3、§3.7。
>
> **2026-04-26（多模型缓存观测 + Mini App 调用观测，以代码为准）：** **`llm/llm_interface._normalize_usage_for_storage`** 将 OpenRouter / OpenAI / Anthropic / DeepSeek / Z.AI GLM 的 usage 统一落到 **`token_usage`**：通用 **`prompt_tokens` / `completion_tokens` / `total_tokens`**，OpenRouter / OpenAI / GLM 的 **`prompt_tokens_details.cached_tokens` / `cache_write_tokens`**，DeepSeek 官方 **`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`**，Anthropic Messages **`cache_creation_input_tokens` / `cache_read_input_tokens`** 与 `cache_creation` 明细，并保存 **`raw_usage_json`** 供排查。**Claude / Anthropic-compatible** 继续使用显式 `cache_control` blocks；**DeepSeek / GLM** 不注入 Anthropic 专用 cache blocks，仅依赖供应商自动缓存并保持稳定前缀、动态尾部。新增 **`api/observability.py`**：**`GET /api/observability/usage`**（按 period/platform 聚合 token/cache、按平台/模型/日期分组、最近调用）与 **`GET /api/observability/tool-executions`**（最近工具调用，raw 只给截断预览）。Mini App 新增 **`Observability.jsx`** / **`observability.css`** 与侧栏「调用观测」页面，展示通用 token、缓存读写/命中估算、最近工具执行与摘要/raw 预览；页面不内置价格表，避免价格过期。详见 §2、§5.11、§7.4。
>
> **2026-04-26（多模型图片、群聊与配置交互修正，以代码为准）：** OpenRouter 上的 Claude 不再因模型名含 `claude` 强制走 Anthropic `/messages`，而走 OpenAI-compatible `/chat/completions`；仅 **OpenRouter+Claude** 在该路径保留 text block `cache_control`，其它 OpenAI 兼容模型仍 flatten 并移除 Anthropic 专用字段。思维链提取补齐 `message.reasoning_content` / `reasoning` / `thinking` / content thinking blocks，覆盖 GLM / DeepSeek / Claude / Gemini 常见网关形态。**`api/settings`** 的 `week` 改为东八区自然周；**`api/weather`** 缓存不再把展示城市写入共享缓存体。Telegram 分段在 Markdown ``` 代码围栏内不按换行或句末标点切分。**`messages.platform_file_id`** 支持 Telegram 图片历史重建；当前消息疑似引用近期图片时，临时下载最近图片并附加到本轮 LLM 请求，不落盘。新增 **`group_chat_state`**、群聊静默/互聊上限/插话概率配置与 `/silent` `/wake`；`assistant_other` 进入 Context 时包装为另一名助手发言。Mini App Config 支持半小时日终时刻（`0` 至 `23.5`），Settings 按 Base URL 分组 API 配置并支持模型收藏。详见 §3.2、§3.4.2、§5.1、§5.11、§7.4。
> **2026-04-26（收口修订，以代码为准）：** 多模态入参在 `llm/llm_interface.build_user_multimodal_content` 统一剥离重复 `data:*;base64,` 前缀并规范 `image/jpg → image/jpeg`；Anthropic 直连仍用 `image.source`，OpenRouter Claude / GLM 等 OpenAI-compatible 网关用规范 `image_url.url`，且仅 text block 可保留 OpenRouter Claude 的 `cache_control`。`daily_batch` Step 2 现按 `summaries.session_id` 分组生成 daily 小传，群聊 session 标记 `is_group=1`；Step 3/3.5/4 通过合并同日所有 per-session daily 继续兼容现有长期记忆流水线。`config.daily_batch_hour` 是 `0.0–23.5` 的半小时粒度浮点值，Mini App 的 00:00 与 23:30 均可选择。
>
> 生成时间：2026-03-22（后续随代码演进修订；2026-04 起：Telegram webhook、`ENABLE_DISCORD`、日终 cron、`/webhook/telegram` 等与实现对齐；2026-04-07：Token 流式补记、daily_batch await 修复、asyncpg datetime 类型修复、Settings 平台进度条动态化、History 气泡配色、resync_meme_chroma 异步化、Telegram 思维链发送开关、每日跑批提取得分与 JSON 重试机制增强、Context 系统时间注入；Telegram 引用回复感知（`_extract_reply_prefix`）、MessageBuffer `combined_raw`/`combined_content` 分离；2026-04-08：`memory.database` 模块便捷函数 `save_message` 补齐 **`thinking`** 转发（与 `MessageDatabase.save_message` 一致，避免 Bot 传入 `thinking=` 时 `TypeError` 致助手行未入库）；Telegram `_flush_buffered_messages` 在 `persist_assistant` 时无首条正文 Telegram `message_id` 则 **`message_id` 用 `ai_{本轮用户消息 id}`**，并与「无 id 时的纯文本兜底 `reply_text`」分支配合；**表情包表与导入**：`migrate_database_schema` 对 `meme_pack` 删除历史 `idx_meme_pack_name_unique`（若存在）、按 **url** 去重（保留最小 `id`）后建 **`idx_meme_pack_url_unique`**；`insert_meme_pack` 为 **ON CONFLICT (url) DO UPDATE**；**`fetch_meme_pack_by_url`**；**`meme_store.has_meme_id`**；**`scripts/import_memes.py`**：默认并发 **5**、视觉 **429** 指数退避重试、url 已在 PG 则不调 vision（Chroma 已有同 id 则整行跳过；Chroma 缺文档则用 PG 的 `description`/`name` 调用 **`upsert_meme_async`** 补向量）；**2026-04-09：CedarClio 输出 Guard**（多标签思维链剥离、未闭合长度保底、Telegram 同步重试与情境兜底、异步摘要 `batch_one_shot_with_async_output_guard`、Step4 `coerce_score_and_arousal_defaults`；**详见 §3.3**）；**2026-04-09（History / Mini App）**：`GET /api/history` 关键词对 **`COALESCE(content,'')` / `COALESCE(thinking,'')` 子串 `ILIKE`**（`api/history` 与 DB 层均 `strip`）；**`PATCH` / `DELETE /api/history/{message_id}`** 单条编辑删除（`MessageDatabase.update_message_by_id` / `delete_message_by_id`）；前端 History 关键词高亮用 **`split` 捕获组奇数位**（避免 `RegExp.test` + `g` 错乱）、**请求序号**丢弃过期响应；Memory 页加载卡片按 **dimension 保留 `updated_at` 最新一条**（多 `user_id` 时避免旧行覆盖）；**2026-04-10：`context_builder`** 在 `MEMORY_CITATION_DIRECTIVE` / `THINKING_LANGUAGE_DIRECTIVE` 之前注入 **`MEMORY_BLOCK_PRIORITY_DIRECTIVE`**（多区块冲突时的优先级链），并在引用指令中说明 **`[uid:xxx]` 与 `[[used:xxx]]` 一一对应**；**`format_telegram_reply_segment_hint`** 区分正文 `<blockquote>` / 行首 `>` 与思维链系统占位；**`daily_batch` Step 3**：`_merge_memory_card_contents` 按维度三分支（`interaction_patterns` / `current_status`+`preferences` 覆写 / 其余），`current_status`·`preferences` 合并前将旧卡正文 **`add_memory`** 归档为 **`summary_type=state_archive`**（`doc_id` 形如 `state_{user_id}_{character_id}_{dimension}_{batch_date}`，metadata 含 `source`、`dimension`、`date`）；**关系时间轴**提取 prompt 改为**第三人称**与真实姓名、禁相对日期词
>
> **2026-04-11：** **`llm_interface._post_with_retry`**：上游 HTTP **429/503** 最多 **5** 次**立即**重试（共 **6** 次请求，无 sleep）；**Settings** 首次 Token 统计请求 **`period=latest`**；**Memory** 记忆卡片 **查看全文**（`createPortal` 全屏只读层）；**`context_builder` / `micro_batch`** 可恢复路径降为 **WARNING**。详见 §3.3、§3.6、§3.4.2–§3.4.3。**Lutopia（以代码为准）：** `tools/lutopia.py` 论坛/摘要/私信 HTTP 客户端（Bearer 读 `config.lutopia_uid`）；发帖/评论/私信遇 `requires_confirmation` 时自动 `POST .../posts/confirm`；`main.py` 在 `initialize_database()` 后调用 **`ensure_lutopia_dm_send_enabled_on_startup()`**（`GET .../agents/me` 的 `dm_send_enabled` 非 true 则 `POST .../agents/me/dm-settings`）；`tools/prompts.py` 的 **`LUTOPIA_TOOL_DIRECTIVE`** 与 **`OPENAI_LUTOPIA_TOOLS`** 工具名对齐（含 **`lutopia_delete_post`** / **`lutopia_delete_comment`**）；**`execute_lutopia_function_call`** 每次 **`logger.info("[tool]…")`**（args/result 截断）；**`complete_with_lutopia_tool_loop`** 返回 **`LutopiaToolLoopOutcome`**（**`behavior_appendix`** 恒为 `""`，**不**再落库 **`[行为记录]`**；工具执行摘要仅 **`[tool]`** 日志）；助手 **`messages` 落库**为各轮正文换行拼接；**`build_context(..., tool_oral_coaching=True)`** 注入 **`TOOL_ORAL_COACHING_BLOCK`**。Telegram：**`_telegram_lutopia_notify_tool_before`** 仅 **`send_chat_action(typing)`**；**`_telegram_lutopia_send_partial_user_text`** 口播为 **`parse_telegram_segments_with_memes_async` → `_telegram_deliver_ordered_segments`**（与最终正文同：先分段再每段 Markdown→HTML，**无** `reply_to`）；**`_telegram_lutopia_notify_tool_after`** 发 **`✅ 已调用{显示名}`** / **`❌ {显示名}调用失败`**（`parse_mode=None`）。人设 **`persona.enable_lutopia=1`** 时缓冲走 **`_telegram_stream_thinking_and_reply_with_lutopia`**（`generate_stream` + tools）。详见 §3.7。
>
> **2026-04-12：** Telegram 助手正文分段：`reply_citations.parse_telegram_segments_with_memes` 在一级 **`|||`** / **`[meme:…]`** 之后对 text 做二级：在 `<pre>` / `<code>` / `<blockquote>` 闭合块外按 **`\n`** 拆行，再对超过 **`max_chars`**（与 **`config.telegram_max_chars`** 一致）的切片按句末标点 **`。！？…～!?`** 拆分（标点留在前段末尾；**无句末标点或拆后仍超长则整段保留、不按长度硬切**），仅标点/符号的孤立切片并入前一片，过短段合并（与 **`_is_complete_sentence`** 规则一致；相邻段用**单次换行**拼接），总段数超 **`telegram_max_msg`** 时优先合并「合并后总长最短」的相邻 text 对（无相邻 text 对则回退为从后往前合并 text；**meme** 不合并、不删）；**`markdown_telegram_html.markdown_to_telegram_safe_html`** 在 Markdown / bleach 前后对**单段**字符串做 **`_compact_vertical_whitespace`**（换行与连续水平空白压为单空格等），减轻单条气泡内版式松散；**一级/二级分段**在 **`parse_telegram_segments_with_memes`** 侧先于 Markdown 完成；**`parse_telegram_segments_with_memes_async`** 读 **`config.telegram_max_chars`** 与 **`config.telegram_max_msg`**；**`format_telegram_reply_segment_hint()`** 为 **【Telegram 排版】** 短指令。**（2026-04-19 起：正文中若含 `|||` 则不再执行上述二级与条数合并，见更新条。）** 详见 §3.2、§3.4.2、§5.7。
>
> **2026-04-12（记忆管线，2026-04-26 缓存改造后拼接顺序见 §3.4.2）：** 当时 `context_builder` 的 `_assemble_full_system_prompt` 拼接顺序为 **长期记忆（向量块）→ daily → chunk**；现已演进为 Anthropic cached text blocks（固定块 / 慢变记忆 / chunk / 动态尾部）与 BP4 近期原文前缀。精排 **`_memory_age_days`** 以 **`last_access_ts`** 为主，**`created_at`** 仅兜底；日终 Step 2 在 **`save_summary` 写入 daily 成功后** 调用 **`delete_today_chunk_summaries(batch_date)`**（**`<= batch_date` 删 chunk** 见更新条）；仓库内 **`CedarClio_记忆架构完整版_v2.md`** 已对齐。详见 §3.4.2、§3.4.4。
>
> **2026-04-12（Mini App）：** Dashboard「记忆库概览」内 **`.memory-section`** 在 **`var(--shadow-raised)`** 外再叠 **1px** 淡色闭合描边，与同底父卡区分；**`.memory-overview-grid`** **`overflow: visible`**。Memory 记忆卡片是否显示 **「查看全文」** 由 **`isMemoryCardContentTruncated`**（离屏同宽测高）判定，避免 **`-webkit-line-clamp`** 下 **`scrollHeight` 与 `clientHeight` 误判**漏出按钮。详见 §3.6。
>
> **2026-04-13：** **`GET /api/settings/token-usage`**：`period=month` 时以 **`Asia/Shanghai` 自然月**（当月 1 日 00:00 起至今）为区间起点，换算为 **UTC naive `datetime`** 与 `token_usage.created_at` 比较（`period=week` 仍为 rolling 7 日，`today` 仍为服务器本地日切）。**Telegram 出站**：`markdown_telegram_html` 的 **`_compact_vertical_whitespace`** 将换行与连续水平空白压为单空格，并合并 **`…` + 空白 + `…`** 为 **`……`**；**`telegram_send_text_collapse`** 供思维链封装前、缓冲纯文本兜底等与 Markdown 入口一致（**不含** Lutopia 工具轮口播：口播先 **`parse_telegram_segments_with_memes_async`** 再 **`_telegram_deliver_ordered_segments`**，与最终助手正文一致）；超长截断后缀 **`（已截断）`**，流式中断缀 **`（已中断）`**（不再使用 `…（已截断）` / `…（已中断）` 前缀省略号）。**多实例**：`config.APP_NAME`（默认 `cedarstar`）→ `main.py` 日志 **`{APP_NAME}.log`**、`memory/vector_store.py` 集合 **`CHROMA_COLLECTION_NAME`**（默认 **`{APP_NAME}_memories`**）；Mini App **`VITE_APP_NAME`**、`miniapp/src/appName.js` 的 **`APP_DISPLAY_NAME`** 与 `index.html` 占位 **`%VITE_APP_NAME%`**。详见 `api/settings.py`、`bot/markdown_telegram_html.py`、`bot/telegram_bot.py`、`config.py`。
>
> **2026-04-14：** Lutopia **`[行为记录]`** 不再写入助手 **`messages.content`**（**`complete_with_lutopia_tool_loop.behavior_appendix`** 恒为 **`""`**；**Telegram** 流式 Lutopia 合并不再拼接附录；**Discord**/**Telegram** 非缓冲落库与展示一致）。**`context_builder._build_recent_messages_section`** 对 **`assistant`** 条 **`strip_lutopia_behavior_appendix`**，剥离库内旧数据中的 **`\\n\\n[行为记录]…`**，避免污染 LLM。**`tools/lutopia.build_lutopia_behavior_appendix`** 仍保留、主流程不再调用。
>
> **2026-04-14（Context / 日终 / Lutopia，以代码为准）：** **`context_builder`** 对发往 LLM 的 **`role=user`** 在 **`inject_user_sent_at_into_llm_content`** 中于正文前加一行东八区 **`【当前时间：…】`**（**`format_user_context_sent_at_line`**）：**近期用户条**用库字段 **`created_at`**，**本轮用户输入**用当前时刻；**仅影响 LLM**，**`messages.content` 不落库**。多模态（**`build_user_multimodal_content`**）时写入**首个 `type: text` 段**。**`daily_batch` Step 4** 价值打分 prompt 为 f-string 时，文案里「字面花括号」须 **`{{}}`**，禁止单独 **`{}`**，否则 **`SyntaxError`** 导致 **`run_daily_batch.py`** 无法 import。**`tools/lutopia`**：**httpx 0.28+** 的 **`AsyncClient.delete()`** 无 **`json=`** 参数，删帖/删评带 **`reason`** 时用 **`AsyncClient.request("DELETE", url, headers=…, json={"reason": …})`**；模块注释含站方 **`AGENT_GUIDE.md`**（`https://daskio.de5.net/AGENT_GUIDE.md`）。详见 §3.4.2、§3.4.4、§3.7。
>
> **2026-04-14（Mini App / 系统日志，以代码为准）：** **`GET /api/logs`** 查询参数含 **`time_from` / `time_to`**（可选，ISO8601 **字符串**；**`api/logs.py`** **`_parse_log_time_param`** 解析为 **UTC naive `datetime`**）。**`memory.database.get_logs_filtered`** 在绑定 **`logs.created_at`**（**`TIMESTAMP`**）前对时间参数使用 **`_pg_timestamp_naive_utc`**，避免 asyncpg 将 **offset-aware** 与无时区列混编 **`DataError`**。前端 **Logs** 页：**datetime-local** 时间筛选；列表 **`message`** 超过 **50** 个 Unicode 码点仅预览，**「查看全文」** 展开；分页条类名 **`pagination pagination--outside`**，置于列表滚动区/白色卡片**外侧**；与 **History / Memory（长期记忆）** 分页均为 **首页 / 上页 / 下页 / 尾页**，页码文案两行居中（**`global.css`** **`.pagination-info--stacked`**）。窄屏下 Logs 时间筛选**同一行**两列；**`.search-input.datetime-input`** 略小字号以减轻 **`datetime-local`** 裁切。
>
> **2026-04-18（Mini App 视觉与 Persona，以代码为准）：** 全站 **燕麦纸 / 新粗体（neo-brutalist）** 层次：**`--page-bg`** 暖灰米、**`--surface`** 浅白、**`--control-surface`** 纯白；线框 **`--industrial-border-color`**；**实体偏移阴影** **`--shadow-solid` / `--shadow-solid-sm`**（**`--shadow-color-deep`** 灰绿）。正文 **Noto Sans SC**（`index.html`）。**Dashboard**：卡片角标 **`::before`** 骑上边框（**`top:0` + `translateY(-50%)`**）、**`width: max-content`**；移动端 **`App.jsx`** 顶栏 **Share / Copy / Plus**（**lucide-react**）。**Persona**：**`SectionHead`** = 等宽 slug（如 **`[ SYS_TOOLS ]`**）+ **Lucide** 线框图标 + **铭牌**（浅紫灰 dust 底 + 硬阴影）+ 标题下 **蓝图风** 分隔（宽间距短划 + 末端 **■**）；**`.persona-page`** 内 **低饱和** 局部变量（**`--persona-ink`** 等）。依赖 **`lucide-react`**（Persona 区块标题 + 移动顶栏）。详见 **`global.css`**、**`dashboard.css`**、**`persona.css`**、**`App.jsx`**、**`Persona.jsx`**。
>
> **2026-04-18（Mini App 侧栏 / 设置 / 助手配置 UI，以代码为准）：** **`router.jsx`** 的 **`navItems`** 每项可选 **`dividerBefore`**（在「助手配置」前插入**虚线 + ■** 分隔）。**`App.jsx`** 侧栏 **`NavLink`** 渲染 **图标 + 文案**。**`sidebar.css`**：选中项 **`.nav-item.active`** 为 **`#1A1A1A` 粗边框 + 右下硬阴影**（实体插片），且 **`overflow: visible`**，避免默认 **`.nav-item` 的 `overflow: hidden`** 裁切 **`box-shadow`**；侧栏 **右缘粗黑边 + 向右黑色硬阴影**（舱门切入感）。**`Settings.jsx` / `settings.css`**：API 分类为**横向可滚动**独立 Tab 按钮；Token 数字为**紧凑双列网格**，平台占比条**弱化为细轨道**；大卡片 **`SETTINGS` / 行内 `API CONFIG`** 等骑线角标配合 **`.settings-page` `padding-top` / 内边距** 避免 **`main-content-viewport`** 的 **`overflow-x: hidden`** 裁切；标题用 **Lucide**（**`KeyRound` / `BarChart3`**）。**`config.css`** 中 **`.config-container` `padding-top`** 同理保护 **`SYSTEM CONFIG`** 角标。Memory / History / Logs 等页延续 neo-brutalist 与 **Lucide** 替换 Emoji 的局部样式以各 **`*.css`** 为准。
>
> **2026-04-19（Mini App 侧栏，以代码为准）：** **`navItems`** 已**不含** **`code`**（历史上曾为 **`[ 01 ]`～`[ 06 ]`**、**`[ SYS ]`** 等前缀；**`App.jsx`** 不再渲染 **`nav-code`**，**`sidebar.css`** 已移除 **`.nav-code`** 及相关规则）。**`dividerBefore`** 仍保留。详见 **`router.jsx`** / **`App.jsx`** / **`sidebar.css`**。
>
> **2026-04-19（Dashboard `memory-overview`，以代码为准）：** **`GET /api/dashboard/memory-overview`** 中 **`chromadb_count`** = **`memory.vector_store.get_vector_store().collection.count()`**（主记忆 **Chroma** 集合 **`{APP_NAME}_memories`** 总条数），**非** **`longterm_memories`** 表行数；**`daily_summary_count`**（`summaries` 且 **`summary_type='daily'`**）、**`active_temporal_states_count`**（`temporal_states` 且 **`is_active=1`**）为 PostgreSQL **`COUNT(*)`**。Mini App **记忆库概览**：**ARCHIVE** 子区两列——左 **已归档小传数量**、右 **已收录片段数量**（**`.memory-archive-metrics`** / **`.memory-archive-col`**）；**REAL-TIME** 子区两列——左 **短期携带量（条）**、右 **活跃时效状态（条）**（**`.realtime-kpi-row`** / **`.realtime-kpi-col`**）。其余字段仍以 **`api/dashboard.py`** 为准。详见 **`Dashboard.jsx`**、**`dashboard.css`**。
>
> **2026-04-19（Lutopia MCP / 落库旁白 / 摘要输入，以代码为准）：** **`OPENAI_LUTOPIA_TOOLS`** 仅 **`lutopia_cli`**（`command` 字符串）与 **`lutopia_get_guide`**；论坛操作经站方 **MCP**（**`mcp` Python 包**：**`create_lutopia_mcp_session`** → SSE **`…/mcp/sse`**，**`ClientSession.initialize`** 后 **`call_tool`** **`cli`** / **`get_guide`**；**`append_tool_exchange_to_messages`** 可选 **`execution_log`** 为 **`(tool_name, arguments_json, result_text)` 三元组**）。落库旁白：**`build_lutopia_internal_memory_appendix`** / **`lutopia_internal_memory_line(name, arguments_json, result_text)`** — 仅 **`lutopia_cli`** 按 **`command` 首词**区分读/写（读：`list`、`search`、`wander`、`show`、`comment-show`、`inbox`、`whoami`、`dm-settings` 等；写：`comment`、`post`、`dm`、`delete`、`vote`、`rename`、`avatar`、`confirm`）；**`lutopia_get_guide`** 不生成旁白；写成功/失败时输出 **`[系统内部记忆：…]`** 单行，并从 **`result_text`**（JSON 或文本）提取 **`*_id`** 拼句末。**`complete_with_lutopia_tool_loop`** 的 **`behavior_appendix`** = **`build_lutopia_internal_memory_appendix(tool_pairs)`**（与旧版「恒为空」叙述不同）。**`memory/micro_batch.generate_summary_for_messages`** 在拼装对话喂 chunk 摘要 LLM 前对每条 **`content`** 调 **`strip_lutopia_internal_memory_blocks`**；**`memory/daily_batch`** Step 2 合并当日 chunk 摘要拼 **`today_content`** 时同样 **`strip_lutopia_internal_memory_blocks`**，避免 **`[系统内部记忆：…]`** 进入日终小传 prompt。详见 §3.4.3、§3.4.4、§3.7。
>
> **2026-04-13（LLM，以代码为准）：** **`LLMInterface._openai_max_tokens`**：当激活配置的 **`api_base`** 含 **`deepseek.com`** 时，OpenAI 兼容 **`chat/completions`** 请求体中的 **`max_tokens`** 钳在 **`[1, 8192]`**（与 DeepSeek 官方校验一致；环境变量 **`LLM_MAX_TOKENS`** 过大时换用 `deepseek-chat` 等非推理模型可避免首轮 **400**）。**`generate_stream`（SSE）**：若某段 **`delta.content`** 为空且此前尚未累计任何正文，则尝试从 **`choices[0].message.content`** 取整段正文；若 **`delta.tool_calls`** 为空则流结束后用 **`choices[0].message.tool_calls`** 补全（非思维链模型工具调用/Lutopia 状态行）。详见 §3.3。
>
> **2026-04-13（TG 分段）：** **`reply_citations._split_oversized_chunk`**：二级分段中超长按句末 **`。！？…～!?`** 切开时，仅在 **成对符号栈空**（`（）`「」“”《》【】`()` 与 Unicode 弯引号配对）且 **不在 ASCII `"` 成对内**时允许切段，避免把括号、引号从中间拆开。详见 `bot/reply_citations.py`。
>
> **2026-04-19（TG 分段，以代码为准）：** **`reply_citations.parse_telegram_segments_with_memes`**：正文经 **`｜｜｜`→`|||`** 归一后，若**至少含一处 `|||`**，则**仅**做一级顺序切分（**`|||`** 与 **`[meme:…]`** 同级），各 text 段整段保留（`strip`），**不**再执行二级换行拆段 / **`_split_oversized_chunk`** / 过短合并 / **`_enforce_max_msg_segments`**；若**全文无 `|||`**（**仅有 `[meme:…]` 不算**「已用 `|||` 分段」），则仍走二级强行走分割与 **`telegram_max_chars`** / **`telegram_max_msg`** 条数封顶。发送侧每条待发 HTML 仍可能因 **Telegram 4096** 在 **`telegram_html_sanitize.split_body_into_html_chunks`**（经 **`markdown_telegram_html`**）再切。详见 `bot/reply_citations.py`。
>
> **2026-04-19（Telegram 流式 / LLM 读超时 / Lutopia MCP，以代码为准）：** **`config.LLM_STREAM_READ_TIMEOUT`** 默认 **90** 秒；**`config.LLM_STREAM_READ_TIMEOUT_TOOLS_FLOOR`** 默认 **180** 秒。 **`llm_interface.generate_stream`**：当 **`tools` 非 `None`** 时，流式读秒数取 **`max(LLM_STREAM_READ_TIMEOUT, LLM_STREAM_READ_TIMEOUT_TOOLS_FLOOR)`**；否则取 **`LLM_STREAM_READ_TIMEOUT`**；HTTP **`timeout`** 元组为 **`(min(30, stream_read), stream_read)`**（读超时约束「两次 SSE 数据之间」）。 **`bot/telegram_bot`**： **`_telegram_stream_llm_one_sse_attempt`** 单次 HTTP 流式； **`_telegram_stream_llm_one_sse_round`** 对 **`requests.ReadTimeout`** / **`urllib3` `ReadTimeoutError`** 自动重试至多 **`STREAM_READ_TIMEOUT_MAX_RETRIES`（3）** 次，重试前发 **`超时重试中（n/3）`**（纯 Telegram **`reply_text`**、**不入库**）并尽量 **`delete_message`** 思维链占位； **`_telegram_user_visible_model_error`** 将流式/HTTP 异常映射为用户可见短句； **`_telegram_finalize_sse_round_outcome`** 中失败/空回复类 **`reply_text`** **不参与** **`save_message` 助手正文**（落库正文仅来自模型分段结果 **`body_for_db`**）。 **`tools/lutopia`**： **`create_lutopia_mcp_session`** 在一轮工具循环内复用 MCP SSE； **`sse_client`** 使用 **`timeout=120`** / **`sse_read_timeout=300`**。详见 §3.1、§3.2、§3.3、§3.7。
>
> **2026-04-20（summaries：chunk ↔ daily 衔接，以代码为准）：** **`memory/micro_batch`**：`save_summary(..., summary_type='chunk', source_date=chunk_day)`，**`chunk_day`** = **`chunk_source_date_from_messages`**（本批消息最后一条 **`created_at`** → **Asia/Shanghai** 日历日）。**`memory/database`**：**`get_today_chunk_summaries(batch_date)`** / **`delete_today_chunk_summaries(batch_date)`** 以 **`COALESCE(source_date::date, created_at::date) <= batch_date`** 选取或删除 chunk（日终将**积压内容日**一并卷入当日小传；写入 daily 成功后删**同一上界**的全部 chunk；**未传** `batch_date` 时 **`<=` 东八区今日**，供 Context / Dashboard，**含积压**）。**`daily_batch` Step 2** 传入 **`batch_date`**。详见 §3.4.2、§3.4.3、§3.4.4。
>
> **2026-04-21（relationship_timeline `created_at`，以代码为准）：** 日终 Step 3 **`insert_relationship_timeline_event(..., created_at=datetime.combine(date.fromisoformat(batch_date), time(23, 59, 59)))`**，**`created_at`** 对齐 **`batch_date` 业务日**（naive）；**`MessageDatabase.insert_relationship_timeline_event`** 支持可选 **`created_at`**，**省略**时列仍 **`DEFAULT NOW()`**。详见 §3.4.4、§5.5。
>
> **2026-04-21（熔断告警 / 向量写入重试 / 跑批 `retry_count`，以代码为准）：** **`memory/vector_store.ZhipuEmbedding.get_embedding`**：HTTP **429/503** 最多 **3** 次尝试、间隔 **2s**；**`VectorStore.add_memory`**：embedding + **`collection.add`** 整段最多 **3** 次、间隔 **1s**。**`migrate_database_schema`**：**`daily_batch_log.retry_count`**（`NOT NULL DEFAULT 0`）；**`increment_daily_batch_retry_count`** / **`reset_daily_batch_retry_count`**。**`memory/daily_batch.schedule_daily_batch_retry_if_needed`**：**`retry_count >= 3`** 时不再启动延迟子进程，并发 **Telegram 熔断**（**`bot/telegram_notify.send_telegram_main_user_text`**）；**`< 3`** 时 **`spawn_run_daily_batch_retry_after_hours` 成功 `Popen` 后**再 **`retry_count + 1`** 并发「已安排 2 小时后重试」；**`run_daily_batch.py` / `trigger_daily_batch_manual` / `schedule_daily_batch`** 均经该入口；**五步全成功**后 **`reset_daily_batch_retry_count`**。**`config.TELEGRAM_MAIN_USER_CHAT_ID`**（**.env**）未配置则**跳过** Telegram。**`memory/micro_batch`**：chunk 摘要连续 **3** 次无法产出可落库正文则发 Telegram 后计数归零；**成功写入 chunk 并标记消息后**归零。详见 **`bot/telegram_notify.py`**、§3.4.4、§3.4.5。
>
> **2026-04-22（日终 Step 2 空跑 / Step 3.5，以代码为准）：** **`memory/daily_batch`**：Step 2 **不**拼接未达 **`chunk_threshold`** 的**原始 `messages`**；**既无 chunk 又无 Step 1 产出**时**不写** `summary_type=daily`、**不**调用 **`delete_today_chunk_summaries`**，仍 **`update_daily_batch_step_status(..., step=2, status=1)`**。**Step 3.5**（在 Step 3 与 Step 4 之间）：当 **`step3_status=1` 且 `step4_status=0`** 时读当日 **`daily`** 正文，**`_step35_extract_temporal_states`** 解析模型 JSON 为 **`new_states` / `deactivate_ids` / `adjust_expire`**，串行 **`save_temporal_state`**、**`deactivate_temporal_states_by_ids`**、**`update_temporal_state_expire_at`**；三支内单条失败仅 **WARNING**。**LLM 调用失败**或 **JSON 整段无法解析**（**`_parse_step35_temporal_operations_json` → `None`**）时 **`raise ValueError`**，**`run_daily_batch`** 内对 **`_step35_extract_temporal_states` 最多重试 3 次**；仍失败则 **WARNING** + **`send_telegram_main_user_text`**（发送失败单独 **WARNING**，不阻断 Step 4）。**`get_daily_summary_by_date`** 失败走外层 **`except`**（不参与上述 3 重试）。**不占** `daily_batch_log` 的 **`stepN_status`**。**`MEMORY_BLOCK_PRIORITY_DIRECTIVE`**（**`memory/context_builder.py`**）冲突消解优先级以代码常量为准（**近期消息 > chunk碎片摘要 > 时效状态 > …**）。详见 **`CedarClio_记忆架构完整版_v2.md`** §六、§3.4.4。
>
> **2026-04-20（跑批 / 微批 Prompt 记忆锚点，以代码为准）：** **`DailyBatchProcessor`**：定好 **`batch_date`** 后 **`await _resolve_batch_memory_identity`**（**`get_today_user_character_pairs`** 当日首对 **`user_id`/`character_id`**，缺 **`character_id`** 时 **`sirius`**，无对则 **`default_user`/`sirius`**），写入 **`_batch_user_id` / `_batch_char_id`**；**`_memory_context_prefix()`** 注入关系锚点 **`【基础设定】…`** 与激活 **`get_memory_cards(..., current_status|relationships, limit=1)`**。**Step 2** 今日小传：**`_persona_dialogue_prefix()` + `_memory_context_prefix()` + 材料正文**。**Step 3** 七维 JSON：在「今日小传」前附 **7 个维度**既有记忆卡各一条（**`get_latest_memory_card_for_dimension`** ×7，拼 **`old_cards_block`**，供对比与增量提取）；输出要求含禁止跨维重复、**仅提取增量/状态变化/与旧认知冲突**等句。**`memory/micro_batch`**：**`get_unsummarized_messages_by_session`** 额外返回 **`character_id`**；**`generate_summary_for_messages`** **`await _resolve_micro_batch_memory_prefix(messages)`**（消息行取 **`user_id`/`character_id`**，缺 **`character_id`** 时 **`_active_character_id_fallback()`** 读激活 **`chat`** **`persona_id`**）；**`SummaryLLMInterface.generate_summary(..., memory_prefix=…)`** 插在称呼行与摘要指令之间。详见 §3.4.3、§3.4.4、§6.3。
>
> **2026-04-19（记忆管线 / Mini App 长期记忆，以代码为准）：** **`context_builder._assemble_full_system_prompt`**：在 **`system_prompt`（人设正文）之后、各记忆块（`temporal_states`、记忆卡片、`relationship_timeline`、向量检索、daily、chunk）之前**注入 **`MEMORY_BLOCK_PRIORITY_DIRECTIVE`**；**`MEMORY_CITATION_DIRECTIVE` 与 `THINKING_LANGUAGE_DIRECTIVE` 仍在 system 块末尾**（与 v2 旧述「仅在文末」不同，以代码为准）。**`GET /api/memory/longterm`**：从 **ChromaDB** 分页列出全量向量（`page` / `page_size` / 可选 **`summary_type`** 过滤 **`metadata.summary_type`**）；**`DELETE /api/memory/longterm/{chroma_doc_id}`** 仅允许 **`manual_` 前缀**（先删 Chroma，再 **`delete_longterm_memory_by_chroma_id`** 删 **`longterm_memories`**）；**`PATCH /api/memory/longterm/{chroma_doc_id}/metadata`** 仅更新 **`halflife_days` / `arousal`**。Mini App「长期记忆」Tab 类型筛选：**`daily` / `daily_event` / `manual` / `state_archive`**（**不含 `chunk`**：微批 **`summaries.summary_type=chunk`** 不写入 Chroma）。详见 §3.4.2、§3.4.4、§5.6、§6.7、§7.3。
> 项目仓库：https://github.com/Zizuixixiang/cedarstar

---

## 目录

1. [项目概述](#1-项目概述) · [1.1 服务器安全基建](#11-服务器安全基建2026-04-完工)
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

### 1.1 服务器安全基建（2026-04 完工）

| 项 | 配置 |
|---|---|
| 用户 | `root`，密钥登录，无密码 |
| SSH | `PermitRootLogin prohibit-password` + `PasswordAuthentication no` |
| SSH 端口 | `2222/tcp` |
| UFW | active，默认 deny incoming，仅放行 `2222/tcp`；80/443/8000 均关闭（流量走 Cloudflare Tunnel） |
| Fail2Ban | sshd 监控 2222，bantime 1h，maxretry 5 |
| `.env` | `chmod 600`，已在 `.gitignore` |
| 应用入站 | 全部经 Cloudflare Tunnel，VPS 无直接暴露端口 |

---

## 2. 目录结构树

```
cedarstar/                          # 项目根目录
├── main.py                         # 主入口：`initialize_database()` → `ensure_lutopia_dm_send_enabled_on_startup()` → BM25 `refresh_index()` → `setup_telegram_webhook_app()`（无 TG 轮询）→ 可选 `ENABLE_DISCORD` 时启动 Discord 线程 → FastAPI（`/webhook/telegram`；`/api/*` 须请求头 `X-Cedarstar-Token` 与 `MINIAPP_TOKEN` 一致；在全部 `include_router` 之后注册 `GET /app/{full_path:path}` → `serve_miniapp`（`miniapp/dist`）与 `GET /daily/{full_path:path}` → `serve_portal`（`portal/dist`），均实文件或回退 `index.html`）；日终跑批不由此进程调度。setup_logging 含 httpx/httpcore 对 api.telegram.org 的 INFO 过滤（避免 Token 入日志）
├── config.py                       # 全局配置类（从 .env 读取），含 Platform 平台常量定义
├── requirements.txt                # Python 依赖清单（含 `python-telegram-bot[socks]` / `httpx[socks]`，供 `TELEGRAM_PROXY=socks5://...`）
├── README.md                       # 项目简介、技术栈、简略目录与「规划中」模块说明
├── start_bot.py                    # 备用启动脚本（校验配置 → 阻塞重建 BM25 → 仅启动 Discord Bot）
├── .env                            # 环境变量配置文件（不入库）
├── cedarstar.db                    # SQLite 数据库文件（已迁移至 PostgreSQL，此文件仅作历史备份参考）
├── {APP_NAME}.log                  # 运行日志（默认 `cedarstar.log`；`config.APP_NAME`）
├── PROGRESS.md                     # 开发进度记录文档
│
├── api/                            # FastAPI REST API 层
│   ├── router.py                   # API 路由汇总，统一注册所有子路由
│   ├── dashboard.py                # 控制台概览接口（Bot 状态、记忆概览、批处理日志）
│   ├── persona.py                  # 人设配置 CRUD 接口
│   ├── memory.py                   # 记忆管理接口（记忆卡片 + 长期记忆：先 Chroma 后数据库写入，列表含 is_orphan）
│   ├── history.py                  # 对话历史：列表查询（平台/关键词/日期+分页）+ 单条 PATCH/DELETE
│   ├── logs.py                     # 系统日志查询接口（平台/级别/关键词/可选 time_from·time_to + 分页）
│   ├── config.py                   # 助手运行参数配置接口；GET/PUT 成功时 data 含 _meta.updated_at（见 §5.7）
│   ├── settings.py                 # API 配置管理接口（api_configs CRUD + Token 消耗统计）
│   ├── weather.py                  # 天气：`fetch_weather_cached`（和风 `HEFENG_*`）；供 HTTP 与 `tools/weather.get_weather` 共用
│   ├── sensor.py                   # 传感器事件写入/查询（表 `sensor_events`）
│   ├── autonomous.py               # 自主日记 CRUD（表 `autonomous_diary`）
│   ├── observability.py            # 调用观测：token/cache 聚合与工具执行列表（供 Mini App「调用观测」页）
│   └── webhook.py                  # Telegram Bot API webhook：`POST /webhook/telegram`（由 main 直接挂到 app，无 /api 前缀；校验 Secret-Token 后后台 `process_update`）
│
├── bot/                            # 聊天机器人层
│   ├── __init__.py                 # 包初始化文件
│   ├── message_buffer.py           # 消息缓冲公共实现（按 session 列表聚合条目；超时合并 texts/图片后回调）
│   ├── logutil.py                  # `exc_detail(exc)`：异常类型 + 说明 + `__cause__` 链，供 WARNING/ERROR 日志易读
│   ├── reply_citations.py          # 解析 [[used:uid]] / 误写 [used:…]、【used:…】；异步 update_memory_hits；`parse_telegram_segments_with_memes` / `parse_telegram_segments_with_memes_async`（一级 `|||` + `[meme:…]`；**全文无 `|||` 时**二级在 `<pre>` / `<code>` / `<blockquote>` 闭合块外按 `\n` 拆行，超长按句末标点拆分（**`_split_oversized_chunk`**）、仅标点片段并入前片、过短段合并，总段数受 `telegram_max_chars` / `telegram_max_msg` 约束；**含 `|||` 时**不做上述二级与条数合并）；清洗后再存库/发送
│   ├── vision_caption.py           # 异步视觉描述任务（vision API 写回 image_caption / vision_processed）
│   ├── stt_client.py               # 语音转录（httpx 异步调用 OpenAI 兼容 /audio/transcriptions，读 stt 配置或 OPENAI_* 回退）
│   ├── markdown_telegram_html.py   # Markdown→HTML（markdown）+ bleach；`_compact_vertical_whitespace` 换行压空格/合并 spaced ellipsis、`telegram_send_text_collapse`；bleach 后展开正文 `<blockquote>`
│   ├── telegram_html_sanitize.py   # 封装整段净化与 split_safe_html_telegram_chunks 切 4096
│   ├── discord_bot.py              # Discord 机器人（组合 MessageBuffer、LLM、消息存储）
│   ├── telegram_notify.py          # 后台告警：`send_telegram_main_user_text`（读 **`TELEGRAM_MAIN_USER_CHAT_ID`**；跑批 / 微批熔断，与 webhook `Application` 生命周期无关）
│   └── telegram_bot.py            # Telegram 机器人（组合 MessageBuffer、LLM、消息存储）；**`_telegram_stream_llm_one_sse_round`** / **`_telegram_user_visible_model_error`** / **`STREAM_READ_TIMEOUT_MAX_RETRIES`**
│
├── llm/                            # LLM 接口层
│   ├── __init__.py                 # 包初始化文件
│   └── llm_interface.py            # 统一 LLM 接口 + CedarClio 输出 Guard；`_post_with_retry` 对 HTTP 429/503 最多 5 次立即重试（共 6 次请求）；非 2xx ERROR 对 **520**/**502**/**504** 附加 **hint**；**`_openai_max_tokens`**（DeepSeek 官方 `api_base` 时 `max_tokens`∈[1,8192]）；**`generate_stream`** 在 **`tools`** 非空时读超时与 **`LLM_STREAM_READ_TIMEOUT_TOOLS_FLOOR`** 取 **`max`**；**`complete_with_lutopia_tool_loop`** / **`LutopiaToolLoopOutcome`**（合并 **Lutopia / 天气 / 微博 / 搜索** OpenAI tools；**`get_weibo_hot`**、**`web_search`** 不进 **`tool_pairs`**）
│
├── memory/                         # 记忆系统层（核心模块）
│   ├── __init__.py                 # 包初始化文件
│   ├── database.py                 # PostgreSQL 数据库封装（asyncpg 连接池 + MessageDatabase 类 + 全局单例 + 便捷函数）
│   ├── context_builder.py          # Context 组装器（system + … + 近期消息）；近期 **`assistant`** 经 **`strip_lutopia_behavior_appendix`**；**`user`** 条经 **`inject_user_sent_at_into_llm_content`** 注入东八区 **`【当前时间：…】`**（仅 LLM，不落库）；可选 **`telegram_segment_hint`**、**`tool_oral_coaching`**（Lutopia / 天气 / 微博 / **网页搜索**工具口播引导）
│   ├── retrieval.py                # 长期记忆 **`summary_type`** 白名单与回溯关键词（**`is_retrospect_query`**）；供 **`context_builder`** 向量/BM25 过滤
│   ├── micro_batch.py              # 微批处理（chunk 摘要；**`strip_lutopia_internal_memory_blocks`**；**`memory_prefix`** 注入锚点 + **`current_status`/`relationships`** 激活卡；**`get_unsummarized_messages_by_session`** 含 **`character_id`**）
│   ├── daily_batch.py              # 日终：`daily_batch_log` **五步** + **Step 3.5**（step3=1 且 step4=0：`new_states`/`deactivate_ids`/`adjust_expire` JSON；LLM/整段解析失败 **raise** 外层 **3** 次重试 + 全败 **Telegram**；不占 log）；**`DailyBatchProcessor`**；**`_resolve_batch_memory_identity`** + **`_memory_context_prefix`** 注入 Step 2；Step 3 七维含 **7 维既有卡 `old_cards_block`**；失败走 **`schedule_daily_batch_retry_if_needed`**；cron 执行 `run_daily_batch.py`；`main.py` 不启动 `schedule_daily_batch`
│   ├── vector_store.py             # ChromaDB 向量存储封装（智谱 Embedding **429/503 重试** + **`add_memory` 写入重试** + 增删查）
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
│   ├── prompts.py                  # LLM 工具包说明：`LUTOPIA_TOOL_DIRECTIVE`、`WEATHER_TOOL_DIRECTIVE`、`WEIBO_HOT_TOOL_DIRECTIVE`、`SEARCH_TOOL_DIRECTIVE`、`OPENAI_LUTOPIA_TOOLS`、`OPENAI_WEATHER_TOOLS`、`OPENAI_WEIBO_TOOLS`、`OPENAI_SEARCH_TOOLS`、`build_tool_system_suffix` / `inject_tool_suffix_into_messages`
│   ├── lutopia.py                  # Lutopia：**MCP**（`lutopia_cli` / `lutopia_get_guide`，SSE + **`mcp`** 客户端）；`OPENAI_LUTOPIA_TOOLS`、`create_lutopia_mcp_session`、`execute_lutopia_function_call`、`append_tool_exchange_to_messages`（**`get_weather`** / **`get_weibo_hot`** / **`web_search`** 分别走 **`execute_weather_function_call`** / **`execute_weibo_function_call`** / **`execute_search_function_call`**；**`execution_log`** 三元组且**跳过** **`get_weather`**、**`get_weibo_hot`**、**`web_search`**）；**`lutopia_internal_memory_line`** / **`build_lutopia_internal_memory_appendix`**；**`strip_lutopia_behavior_appendix`** / **`build_lutopia_behavior_appendix`**（兼容）；**`strip_lutopia_internal_memory_blocks`**；启动时 **`ensure_lutopia_dm_send_enabled_on_startup`**（`httpx` 调论坛 **`…/agents/me`** / **`dm-settings`**）；站方 **`AGENT_GUIDE.md`**
│   ├── meme.py                     # 表情包：`search_meme` / **`search_meme_async`**（向量检索，可选 `top_k`；TG 缓冲路径用 async）、`send_meme`（TG Bot 发静图/动图）；不由 LLM function calling 注册
│   ├── weather.py                  # 天气：`fetch_weather` / **`execute_weather_function_call`**（`get_weather`），返回 **JSON 对象字符串**（与 `api.weather.fetch_weather_cached` 一致）
│   ├── weibo.py                    # 微博热搜：`fetch_weibo_hot_summary_text` / **`execute_weibo_function_call`**（`get_weibo_hot`），**GET** 官方 **`hotSearch`**，**`Cookie`** = **`config.WEIBO_COOKIE`**；返回 **`{"summary":…}`**
│   ├── search.py                   # Tavily 联网：**`execute_search_function_call`**（**`web_search`**）；**`TAVILY_API_KEY`**；摘要压缩读 **`search_summary`** 激活行或回退 **`summary`**
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
│       ├── router.jsx              # 路由配置（8 个页面；`navItems` 含可选 `dividerBefore`；显式 import React）
│       ├── pages/                  # 页面组件
│       │   ├── Dashboard.jsx       # 控制台概览页（status / memory-overview / batch-log，顶栏与日历、记忆 KPI）
│       │   ├── Persona.jsx         # 人设配置页（角色/用户信息 CRUD；Lutopia / 天气 / 微博 / **网页搜索**工具开关）
│       │   ├── Memory.jsx          # 记忆管理页（四 Tab；固定视口内高度 + 内容区独立滚动，见 §3.6）
│       │   ├── History.jsx         # 对话历史页（聊天气泡布局 + 筛选，见 §3.6）
│       │   ├── Logs.jsx            # 系统日志页（筛选含时间范围；消息预览+查看全文；分页在容器外）
│       │   ├── Config.jsx          # 助手配置页（运行参数滑块 + Telegram 分段 telegram_max_chars / telegram_max_msg，见 §3.6）
│       │   ├── Settings.jsx        # 核心设置页（API 配置管理：`chat` / `summary` / `vision` / `stt` / `embedding` / **`search_summary`** + Token 统计）
│       │   └── Observability.jsx   # 调用观测页（token/cache 多模型统计 + `tool_executions` 最近记录与 raw 截断预览）
│       └── styles/                 # CSS 样式文件（每个页面对应一个 CSS 文件）
│           ├── global.css          # 全局样式
│           ├── sidebar.css         # 侧边栏样式
│           ├── dashboard.css       # 控制台样式
│           ├── persona.css         # 人设页样式
│           ├── memory.css          # 记忆页样式
│           ├── history.css         # 历史页样式
│           ├── logs.css            # 日志页样式
│           ├── config.css          # 配置页样式
│           ├── settings.css        # 设置页样式
│           └── observability.css   # 调用观测页样式
│
├── portal/                         # 独立前端（React + Vite），生产 base `/daily/`；`GET /daily/*` 由 main 提供 `portal/dist`
│   ├── vite.config.js              # `base: '/daily/'`；`package.json` 等见目录
│   └── src/                        # 页面与样式（`VITE_PORTAL_TOKEN` 与后端 `MINIAPP_TOKEN` 一致）
│
├── chroma_db/                      # ChromaDB 本地持久化数据目录（运行时生成）
│   └── c0d21c8d-.../               # ChromaDB 内部集合数据文件
│
├── backups/                        # 数据库备份文件目录
│   ├── cedarstar.db.backup         # 数据库备份
│   ├── cedarstar.db.backup2        # 数据库备份2
│   └── cedarstar.db.backup_dimension # 维度字段变更前的备份
│
├── run_daily_batch.py              # 日终跑批独立入口：`await initialize_database()` 后 `DailyBatchProcessor().run_daily_batch()`；失败时 **`await schedule_daily_batch_retry_if_needed`**；供 cron 调用，用法见 §4.2
├── backup.sh                       # 全量备份（Bash）：读 `.env` 的 `DATABASE_URL` → `pg_dump -F c` → 与 `chroma_db/`、`.env` 打 tar.gz → `rclone copy`；详见 §4.2.1
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
| `TELEGRAM_MAIN_USER_CHAT_ID` | 可选。主用户 Telegram **私聊** `chat_id`（数字字符串）。**`memory/daily_batch.schedule_daily_batch_retry_if_needed`**（跑批延迟重试 / 熔断）与 **`memory/micro_batch`**（chunk 连续失败告警）经 **`bot/telegram_notify.send_telegram_main_user_text`** 发至此；未配置则**不发** Telegram、仅日志 |
| `LLM_API_KEY / LLM_API_BASE / LLM_MODEL_NAME` | 主 LLM 配置 |
| `LLM_TIMEOUT` / `LLM_VISION_TIMEOUT` | `requests` 读超时（秒）：默认 **60** / **180**；`LLMInterface` 在 **messages 含多模态图片** 时取二者较大值作为单次 `chat/completions`（或等价）超时，否则仅用 `LLM_TIMEOUT`；**`config_type=vision`** 时实例 `timeout` 保底为 `max(LLM_TIMEOUT, LLM_VISION_TIMEOUT)` |
| `LLM_STREAM_READ_TIMEOUT` | 可选。仅 **`generate_stream`（SSE）** 的 **读** 超时（两次 chunk 之间最长等待）；默认 **90** 秒。未设置时不必写 `.env` |
| `LLM_STREAM_READ_TIMEOUT_TOOLS_FLOOR` | 可选。当 **`generate_stream`** 请求**携带 `tools`**（如 Lutopia **`OPENAI_LUTOPIA_TOOLS`** ± 天气 **`OPENAI_WEATHER_TOOLS`** ± 微博 **`OPENAI_WEIBO_TOOLS`** ± 搜索 **`OPENAI_SEARCH_TOOLS`**）时，与 **`LLM_STREAM_READ_TIMEOUT`** 取 **`max`** 作为实际读超时**下限**；默认 **180** 秒，缓解多轮 MCP 后上下文变长、模型两包间隔变长导致的误判超时 |
| `OPENAI_API_KEY / OPENAI_API_BASE` | 语音转录（STT）在库内无激活 `stt` 配置时回退；**不复用** `LLM_API_*`；`OPENAI_API_BASE` 默认 `https://api.openai.com/v1` |
| `SUMMARY_API_KEY / SUMMARY_API_BASE / SUMMARY_MODEL_NAME` | 摘要专用 LLM 配置 |
| `ZHIPU_API_KEY` | 智谱 Embedding API 密钥 |
| `SILICONFLOW_API_KEY` | 硅基流动 API 密钥兜底：表情包向量在 **`api_configs` 已激活 `embedding` 行且 `api_key` 非空时仅用库内 key**；否则读此项（`config.py` → `.env`） |
| `COHERE_API_KEY` | Cohere Rerank API 密钥 |
| `DATABASE_URL` | PostgreSQL 连接 DSN（asyncpg 格式，如 `postgresql://user:pass@host/db`）；未设置时返回空字符串 |
| `MINIAPP_TOKEN` | Mini App 访问 **`/api/*`** 时，请求头 **`X-Cedarstar-Token`** 须与本值**完全一致**，否则 `main.py` 返回 401；**不影响** **`POST /webhook/telegram`**（见 §3.5、§4.3）。前端构建环境变量 **`VITE_MINIAPP_TOKEN`** 须与之对齐（见 §3.6 `apiFetch`） |
| `WEIBO_COOKIE` | 可选。微博热搜工具 **`tools/weibo.py`** 请求 **`weibo.com/ajax/side/hotSearch`** 时使用的浏览器 **Cookie** 整串（与登录态一致，**易过期**；勿提交版本库，见 **`.env`**） |
| `TAVILY_API_KEY` | 可选。**`tools/search.py`** 调 **Tavily** **`https://api.tavily.com/search`** 时使用；未设置则 **`web_search`** 无检索结果（返回 **`暂时无法搜索`**） |
| `CHROMADB_PERSIST_DIR` | ChromaDB 本地存储目录 |
| `APP_NAME` | 应用实例标识（默认 `cedarstar`）；`main.py` 日志文件名为 **`{APP_NAME}.log`** |
| `CHROMA_COLLECTION_NAME` | 可选；Chroma 长期记忆集合名，未设置时为 **`{APP_NAME}_memories`** |
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
- 不构建 prompt，通过 `memory.context_builder.build_context()` 获取完整上下文（`_assemble_full_system_prompt` 在引用死命令后追加 **`THINKING_LANGUAGE_DIRECTIVE`**，要求 thinking / reasoning 使用中文；Telegram 缓冲 flush 另传 `telegram_segment_hint=True`，在 system 末尾再追加 **`format_telegram_reply_segment_hint()`**（Markdown→发送侧见 `markdown_telegram_html`）：**【Telegram 排版】** 短指令，含 HTML 白名单、自然换行多气泡、`|||` 为可选强制分段、**MAX_CHARS / MAX_MSG**（`config.telegram_max_chars` / `config.telegram_max_msg`，默认 50 / 8，Mini App 可调；**发送侧**二者仅约束**正文不含 `|||`** 时的二级强分与条数封顶）、`[meme:…]` 与顺序说明；思维链不使用 `|||`）；**`enable_lutopia` 或 `enable_weather_tool` 或 `enable_weibo_tool` 或 `enable_search_tool`**（OpenAI 兼容 tools）时两 Bot 另传 **`tool_oral_coaching=True`**（注入 **`TOOL_ORAL_COACHING_BLOCK`**）；Discord 与其余路径不传 `telegram_segment_hint`，无上述工具开关时不传 `tool_oral_coaching`，仍含中文思维链指令
- 消息缓冲（`message_buffers` / `buffer_locks` / `buffer_timers`、`add_to_buffer`、读 `buffer_delay` 后合并）由 **`bot/message_buffer.py`** 的 `MessageBuffer` 统一实现；超时后 `aggregate_buffer_entries()` 返回四元组：**`combined_raw`**（落库用原始内容，由各条目的 `raw_content` 字段合并，不含引用前缀）、**`combined_content`**（LLM 用内容，由 `content` 字段合并，可能含引用前缀）、当前轮 `images`（`image_payload` 列表）及 `text_for_llm`（多模态用纯文本）。flush 回调签名为 `(session_id, combined_raw, combined_content, images, buffer_messages, text_for_llm)`；**用户行落库走 `combined_raw`**，LLM 上下文走 `combined_content`。两 bot 负责入缓冲条目（`content` 与 `raw_content` 与/或 `image_payload` / `image_payloads`）、平台 typing 与分片发送；**`_flush_buffered_messages` → `_generate_reply_from_buffer` 须传入同一批 `buffer_messages`**（Discord / Telegram 一致）。**Telegram 缓冲 flush：** **`_generate_reply_from_buffer`** 在 **`await LLMInterface.create()`** 后**先行** **`save_message` 用户行**（`combined_raw`），再按线路分支：**Anthropic `/messages`** → **`asyncio.to_thread` + `generate_with_context_and_tracking`（不传 `tools`）** → **`_telegram_deliver_prefetched_llm_response`**；**OpenAI 兼容 SSE** → 若 **`LLMInterface.enable_lutopia`** 或 **`enable_weather_tool`** 或 **`enable_weibo_tool`** 为 **`_telegram_stream_thinking_and_reply_with_lutopia`**（**`async with create_lutopia_mcp_session()`** 在 Lutopia 或混编工具需要时；多轮 **`generate_stream`**，**`tools`** = **`OPENAI_LUTOPIA_TOOLS`**、**`OPENAI_WEATHER_TOOLS`**、**`OPENAI_WEIBO_TOOLS`** 按人设开关合并 + **`append_tool_exchange_to_messages`**，最多 **8** 轮工具循环），否则 **`_telegram_stream_thinking_and_reply`**（单轮 **`generate_stream`，`tools=None`**）；二者均经 **`_telegram_stream_llm_one_sse_round`**（内含 **`_telegram_stream_llm_one_sse_attempt`**；**`ReadTimeout`** 最多重试 **3** 次）→ **`_telegram_finalize_sse_round_outcome`**：流式编辑思维链占位，结束定稿一条 `<blockquote expandable>🧠 思维链`…（`parse_mode=HTML`），再按有序段交付助手回复（文字与表情包交替）。无正文且无思维链且无成功表情包时发提示前打 **WARNING**（含 `raw_preview`）；流式失败时用户可见短句由 **`_telegram_user_visible_model_error`** 映射（**不入库**）。**表情包：** 在 `[[used:…]]` 清洗之后，**`parse_telegram_segments_with_memes_async`**（读 **`config.telegram_max_chars`** / **`config.telegram_max_msg`**）调用 **`parse_telegram_segments_with_memes`**：一级 **`|||`** / **`[meme:描述]`**（顺序分隔）；**若正文含至少一处 `|||`**（全角 **`｜｜｜`** 先归一），则**仅**一级切分，各 text 段整段进入后续发送（**不**再做二级换行 / 超长句末切 / 过短合并 / **`telegram_max_msg`** 合并）；**若全文无 `|||`**（**仅** **`[meme:…]`** 不算已用 `|||` 分段），则二级在各 text 段内（`<pre>` / `<code>` / `<blockquote>` 闭合块外）按 **`\n`** 拆行，再对超长切片按句末标点拆分（无长度硬切）、合并过短段，总段数超 **`telegram_max_msg`** 时优先均匀合并相邻 text（见上条），得到有序段，**`_telegram_deliver_ordered_segments`** 按序**交替**发送各段 HTML 与各 **`await search_meme_async(query, 1)`** → **`send_meme`**（无命中静默跳过）；仅表情无字且至少发出一张时落库可为 `[表情包]`，并可用首条媒体 `message_id` 落库。助手**对外正文**在 Citation 与 meme 标记清洗后由 **`bot/markdown_telegram_html.py`**（`markdown` + `bleach`；**`markdown_to_telegram_safe_html`** 内 **`_compact_vertical_whitespace`** 压连续换行）将模型 Markdown 转为 HTML 并白名单清洗（允许 `b` / `i` / `u` / `s` / `code` / `pre` / `a`（`href` 限 `http`/`https`/`tg`/`mailto`）/ `blockquote`（可选 `expandable`）；API 不支持 `<br>`，`nl2br` 产出在清洗前转为换行符），不在白名单的标签剥离并保留内文；**`bot/telegram_html_sanitize.py`** 对**每条待发 HTML 正文段**整段只做一次上述转换，再用 `split_safe_html_telegram_chunks` 按净化后长度适配 4096；思维链与正文同条时正文前缀用 `prefix_safe_html_by_max_len` 在**已净化 HTML** 上切分。分段结果再经多条 `reply_text`（`parse_mode=HTML`）发出，段间 `asyncio.sleep(0.5)`。Mini App **Persona** 页「系统规则」下提示模型在 Telegram 场景使用上述 HTML 标签、勿用 Markdown。`messages` 中 assistant **仅落库清洗后的整段正文**（各 text 气泡按换行拼接，**不含** `|||` 与 `[meme:…]`）。配置类错误等 flush 前失败仍走 `reply` 字符串由 `_flush_buffered_messages` 单条发出。`media_type` 由 **`ordered_media_type_from_buffer`** 按条目**时间顺序**遍历：每条目若有图/贴纸/语音则依次尝试追加 `image` / `sticker` / `voice`（已存在则跳过），得到**去重且保序**的逗号拼接串；不可只依赖 `combined_content` 字符串推断
- **慢处理与 flush：** 贴纸识图、语音 STT、图片下载等在 **`add_to_buffer` 之前**可能远长于 `buffer_delay`。Bot 在慢段前后调用 `MessageBuffer.begin_heavy` / `end_heavy`；定时器 `sleep(buffer_delay)` 结束后若该 session 仍有未配平的 heavy，**再轮询等待至多约 180 秒**（`message_buffer._BUFFER_HEAVY_WAIT_CAP_S`）后才取出缓冲区合并，避免「只有先发图片入队、贴纸/语音还在识图/转录就被 flush」的拆分。超时仍可能拆分时会打 WARNING 日志
- 图片：单张大于 10MB 不入视觉管线，缓冲内以文案 `[发送了1张图片（文件过大，已跳过视觉解析）]` 记录。用户消息落库时若有图：`media_type='image'`、`vision_processed=0`，并由 **`bot/vision_caption.py`** 调度异步任务（`config_type='vision'` 的激活配置）写回 `image_caption` 与 `vision_processed=1`；失败写 **`memory.database.VISION_FAIL_CAPTION_SHORT`**（`[视觉解析失败]`）。**`update_message_vision_result`** 在 `image_caption` 为 `[视觉解析失败]` 或 **`[系统提示：视觉解析超时失败]`**（与 **`expire_stale_vision_pending`** 超时 UPDATE）时同时将 **`is_summarized=1`**，避免占位行占用微批「未摘要」计数
- 语音：**入 Buffer 前**由 **`bot/stt_client.py`** 同步转录（`httpx.AsyncClient` → `api_base.rstrip('/') + '/audio/transcriptions'`，模型默认 `whisper-1`，可选 `language=zh`）。配置优先读 `api_configs` 中激活的 `config_type='stt'`；未配置则回退 **`.env` 的 `OPENAI_API_KEY` + `OPENAI_API_BASE`**（**不复用** chat LLM 环境变量）。Telegram：`message.voice`（`.ogg`/Opus）；`file_size` 大于 **50MB** 不下载，下载后 `len(bytes)` 大于 **25MB** 则落库 `[语音] 文件过大，跳过转录`。Discord：附件 `content_type` 为 `audio/ogg` 或 `audio/mpeg` 时同逻辑。成功落库 `content='[语音] …'`、`media_type` 中含 `voice`、`vision_processed=1`；失败为 **`bot/stt_client.TRANSCRIBE_FAIL_USER_CONTENT`**（`[语音] 转录失败`）。**Telegram** 用户行 `save_message(..., is_summarized=1)` 当 `combined_raw` 含该兜底、或含 **`[贴纸]`** 且含占位 **`（贴纸）`**，或正文含上述视觉失败/超时文案。同轮图+语音等组合时 `media_type` 为按缓冲顺序去重后的逗号串（如 `image,voice`）。长语音转录若超过 `buffer_delay`，可能与紧邻文字拆成两轮对话，属耗时限制而非 bug
- **Telegram 贴纸（`message.sticker`）：** 以 `file_unique_id` 查 **`sticker_cache`**（§5.13）；**`MessageDatabase.get_sticker_cache` / `save_sticker_cache` / `delete_sticker_cache` 均为 `async`，机器人内须 `await`**（含 `/rescanpic` 删缓存）。未命中则模块级 `processing_stickers` + `asyncio.Lock` 去重，已在处理中的 id 轮询等待最多约 3 秒再读库。下载贴纸（跳过 `.tgs` / `.webm` 等、大于 10MB 跳过）转 Base64，**`asyncio.to_thread`** 内调用 **`LLMInterface(config_type='vision')`**，提示词要求 40 字以内描述含义与情绪，图内文字原样引用、不描述技术细节；结果写入缓存，失败亦写入 **`（贴纸）`**。正文 `content='[贴纸] {emoji} {description}'`；`media_type` 在缓冲顺序中与其它类型一并去重拼接（可与 `image`/`voice` 同轮，如 `image,sticker,voice`），`vision_processed=1`。**`/rescanpic`：** 用户先发命令后进入待重扫（模块集 `pending_rescan` + 60s 超时任务）；下一条贴纸会先 **`await delete_sticker_cache`** 并 `processing_stickers.discard` 再照常走识图；超时未发贴纸回复「已取消」，下一条非贴纸消息回复「未检测到贴纸，已取消」并照常处理该消息
- **Telegram 引用回复感知（2026-04-07）：** `telegram_bot._add_to_buffer` 在入队前调用 **`_extract_reply_prefix(message)`**：检查 `message.reply_to_message`；若用户使用了 Telegram 「长按→Reply」引用某条消息，则从被引用消息取 `text` 或 `caption`（`≤200` 字截断），按发送者身份生成前缀——**Bot 发的话**为 `[你正在回复 AI 的消息：「…」]`，**用户自己的话**为 `[你正在回复你之前的消息：「…」]`。前缀拼入 **`content_for_llm`**（写入 buffer entry 的 `content` 字段，供 LLM 感知）；同时写入 **`raw_content`** 字段存**不含前缀**的原始消息（供落库保持干净历史记录）。`aggregate_buffer_entries` 区分两字段、分别合并为 `combined_content`（LLM 用）与 `combined_raw`（落库用），用户不可见。**有序分段解析后发出的每一条（含仅由正文 `\n\n` 拆出的气泡）均为独立 Telegram 消息，引用某条时仅携带该条文本，精准对应**更新由 **webhook** 推送的 JSON 经 **`process_update`** → `Application.process_update` 进入同一套 handler，**无需** `start_polling`。取 `new_reaction` 中第一个可展示项（标准 emoji 或自定义表情 id）；**`new_reaction` 为空**（用户撤回反应）**不写库**。用 `message_id` 查同会话 `messages` 中 **`role='assistant'`** 且 **`message_id` 等于该条 Bot 发出消息的平台 ID** 的正文，取前 20 字作摘要；**查不到**时用摘要「某条消息」。合成 `content='[用户对你的消息「摘要…」点了 …]'`，`media_type='reaction'`，`role='user'`，**不入 MessageBuffer**，直接 `save_message` 并可触发微批检查。**`character_id`：** 查 `api_configs` 中 `config_type='chat'` 且 `is_active=1` 的 `persona_id`（转字符串）；无有效 `persona_id` 时用环境变量 **`DEFAULT_CHARACTER_ID`**（未设则 `sirius`），**不实例化 `LLMInterface`**。**说明：** 助手行自本逻辑起以 Telegram **真实发出**的 `message_id` 落库，旧数据中 `ai_{用户消息id}` 无法与反应事件对齐，反应摘要会走兜底文案
- 助手原始回复中的记忆引用由 **`bot/reply_citations.py`** 的 **`schedule_update_memory_hits_and_clean_reply`** 处理：规范格式为 `[[used:uid]]`；另兼容模型误写的 **单括号 `[used:…]`** 与 **全角 `【used:…】`**（均参与 `update_memory_hits` 并从正文剥离）。**Telegram** 在同一清洗之后由 **`parse_telegram_segments_with_memes_async`** → **`parse_telegram_segments_with_memes`** 做一级 **`|||`** / **`[meme:描述]`**；**无 `|||` 时**才有二级换行 / 超长句末切 / 过短合并 / 总段封顶（HTML 块外，见上条），**`telegram_bot`** 按该顺序**交替**发送 HTML 文字与表情包；**`messages` 落库正文**为各文字段按序换行拼接（无 meme 标记）（见上条缓冲说明）
- **Telegram 缓冲助手落库（2026-04-08，以代码为准）：** 用户行在 **`_generate_reply_from_buffer`** 内、**`build_context` / 上游模型调用之前**落库（**`LLMInterface.create()`** 后立即 **`save_message`**）；助手行在 **`_flush_buffered_messages`** 内 **`await save_message(..., role='assistant', thinking=gen.thinking)`**。若 **`persist_assistant`** 成立但未取得首条正文 Telegram **`message_id`**（例如分段 HTML 净化后未实际 `reply_text` 发出），则 **`message_id` 兜底为 `ai_{buffer_messages[0].message_id}`**（与非缓冲 **`_generate_reply`** 一致）；当仍无平台正文 id 时，另以 **`base_message.reply_text(gen.reply, parse_mode=None)`** 向用户发纯文本兜底。若模块便捷 **`save_message` 未接受 `thinking`**，会在助手插入处 **`TypeError`**，且常被 **`MessageBuffer._process_buffer` 的 `except Exception`** 记录为「处理缓冲区时出错」，表现为对话可见但 **`messages` 无 assistant、时光机仅用户消息**
- 主对话 LLM：**Discord** 缓冲 flush 使用 `generate_with_context_and_tracking`。**Telegram 缓冲 flush**：OpenAI 兼容为 **SSE `generate_stream`**——无 Lutopia/天气/微博/**搜索**工具开关时为**单轮**、**`tools=None`**；**`enable_lutopia` 或 `enable_weather_tool` 或 `enable_weibo_tool` 或 `enable_search_tool`** 时为 **`_telegram_stream_thinking_and_reply_with_lutopia`**（**多轮**、**Lutopia ± 天气 ± 微博 ± 搜索** `tools` 按开关合并，见上条）；Anthropic 为 **`generate_with_context_and_tracking`（无 tools）**（见上条）。非缓冲 **`_generate_reply`** 为单次 **`generate_with_context_and_tracking`**；若传入 **`telegram_bot`** 则先发思维链，再与缓冲路径相同 **`await parse_telegram_segments_with_memes_async`** 后有序交替发文字与表情包。上述带 `usage` 的路径异步写入 `token_usage`（见 §3.3、§5.11）。**Telegram 流式路径 Token 补记（2026-04）：** SSE 子线程无 asyncio 事件循环，原先流式 usage 被丢弃；现已在主线程的 `done_payload` 中读取 `usage` 并调用 `llm._async_save_token_usage`；Anthropic 与非缓冲路径亦在 `llm_resp.usage` 存在时调用 `llm._save_token_usage_async`，确保 Telegram 各路径均落库。`_assistant_outgoing_chunks` 仍保留（思维链转义 + 正文白名单净化）
- **`requests` 超时与 HTTP 异常（缓冲生成路径，以代码为准）：** 区分 **`ReadTimeout`** / **`ConnectTimeout`** / 其余 **`Timeout`** / **`RequestException`**；用户可见文案由 **`_telegram_user_visible_model_error(..., stream_chunk_timeout=False)`** 等生成（**流式线程内失败**在 finalize 阶段用 **`stream_chunk_timeout=True`**）。**`images` 非空**时仍对读超时强调 **`LLM_VISION_TIMEOUT`** / **`LLM_TIMEOUT`**。未捕获的 **`Exception`** 提示见日志

**消息缓冲机制：**
```
用户发消息 → MessageBuffer.add_to_buffer() → 启动/重置 N 秒定时器
                                    ↓（超时）
                          合并 buffer 条目（文本 + 图片 payload）→ 调用各 bot 的 flush 回调
                                    ↓
                          save_message(user)（TG：`create()` 后）→ build_context(..., images=..., llm_user_text=..., exclude_message_id=...；Telegram 缓冲另传 `telegram_segment_hint=True`) → LLM（Discord：`generate_with_context_and_tracking`；Telegram：OpenAI 兼容 **`generate_stream`（SSE；无工具开关则 `tools=None`；`enable_lutopia` 或 `enable_weather_tool` 或 `enable_weibo_tool` 或 `enable_search_tool` 则多轮 + 合并 `tools`**）** 或 Anthropic **`generate_with_context_and_tracking`（无 tools）**）→ 引用 hits 与 meme 清洗 → TG `parse_telegram_segments_with_memes_async`（一级 `|||` / `[meme:…]`；**无 `|||` 时**二级强分）→ 有序交替发送 → 保存助手 → 可选异步视觉描述 → 触发微批检查
```

**Discord Bot 特有：**
- **`main.py` 仅在 `ENABLE_DISCORD=true` 时**在后台线程启动 `DiscordBot`；为 `false` 时整进程不连接 Discord Gateway
- 仅响应 `@mention` 或私聊消息
- 支持 `!ping` / `!clear` / `!model` / `!help` 命令
- 消息长度限制 2000 字符（自动分割）
- 支持 `attachments` 中 `image/*`：`await attachment.read()` 转 Base64 入缓冲（与文本同条合并）；支持 `audio/ogg`、`audio/mpeg`：读入后 **`transcribe_voice`** 再入缓冲（与文本同条合并）

**Telegram Bot 特有（webhook 模式）：**
- **入口：** Telegram 服务器 **`POST`** 公网 HTTPS **`/webhook/telegram`**（由 `main.py` 将 `api/webhook.py` 的 router **直接** `include_router` 到 `app`，**不带** `/api` 前缀）。请求头 `X-Telegram-Bot-Api-Secret-Token` 须与 **`TELEGRAM_WEBHOOK_SECRET`** 一致。Handler 内 **`BackgroundTasks`** 调用 **`bot.telegram_bot.process_update(update_json)`**：`Update.de_json(..., bot)` 后 **`await application.process_update(update)`**，与 polling 时代相同的 `CommandHandler` / `MessageHandler` / `MessageReactionHandler` 逻辑。
- **`main.py` 启动顺序：** `await initialize_database()` → **`await ensure_lutopia_dm_send_enabled_on_startup()`**（`tools/lutopia.py`，未配置 `lutopia_uid` 时 no-op）→ 异步系统日志挂载 → BM25 `refresh_index()` → **`await setup_telegram_webhook_app()`**（内部 `TelegramBot.setup_webhook()`：`Application.builder()`… **`initialize()`** → **`set_my_commands`**（三 scope）→ **`start()`**，**不调用** `updater.start_polling`）。进程退出路径上 **`shutdown_telegram_webhook_app()`** 执行 `stop`/`shutdown`。
- 响应文本、语音、贴纸与图片消息（`VOICE` / `PHOTO` / `TEXT` / `Sticker`）
- 支持 `/start` / `/help` / `/model` / `/clear` / `/rescanpic` 命令；`initialize()` 后对 **`BotCommandScopeDefault`**、**`BotCommandScopeAllPrivateChats`**、**`BotCommandScopeAllGroupChats`** 各调用一次 **`bot.set_my_commands`**（同一组 5 条，含 `rescanpic`「重新识别贴纸图片」），避免仅写默认 scope 时部分会话里输入 `/` 不出现命令补全。客户端会缓存命令表，更新后若仍无补全可重开与该 Bot 的对话或重启 Telegram
- 消息长度限制 4096 字符（自动分割）。**缓冲回复（OpenAI 兼容主路径）：** SSE 流式编辑思维链占位消息，节流间隔为 **`config.TELEGRAM_THINK_STREAM_EDIT_INTERVAL_SEC`**（默认 **1.1s**，环境变量可覆盖，下限 0.15），结束时**定稿为单独一条**消息（`<blockquote expandable>🧠 思维链`…，`parse_mode=HTML`），**若 `send_cot_to_telegram` 配置开启则随之发送，否则将不再展示并隐去此占位消息**；若 **`edit_message_text(HTML)`** 失败则 **WARNING** 并尝试**删占位**后以 **`reply_text`** 重发同内容（内文去 `\x00` 以降低实体解析失败概率）。随后 **`await parse_telegram_segments_with_memes_async`**（内部 **`parse_telegram_segments_with_memes`**，读 **`config.telegram_max_chars`** / **`config.telegram_max_msg`**，二者仅作用于**正文无 `|||`** 的二级强分）：一级 **`|||`** / **`[meme:…]`**；**含 `|||`** 时不再做二级换行拆段 / 超长句末切 / 过短合并 / 总段封顶；**无 `|||`** 时仍做上述二级与封顶，**交替**发送 HTML 正文与表情包（非「全文后发完再逐条发图」）。**非缓冲路径**（`_generate_reply`）：可选传入 **`telegram_bot`** 时同样先判断 `send_cot` 后再决定是否发思维链，最后按上述有序段交付
- session_id 格式：`telegram_{chat_id}`（Discord 为 `{user_id}_{channel_id}`）
- **Bot API 网络（出站）：** `Application.builder().token(...).request(HTTPXRequest(...)).get_updates_request(HTTPXRequest(...)).build()`。两处 `HTTPXRequest` 使用 `config.TELEGRAM_PROXY` 作为 `proxy`、并 `httpx_kwargs={"trust_env": False}`，避免 httpx 默认继承环境变量代理（Discord 会设置 `HTTP_PROXY` 等）。未配置 `TELEGRAM_PROXY` 时为直连；`connect_timeout`/`read_timeout`/`write_timeout` 相对默认放宽（约 25s / 120s / 120s）。**入站更新**不再使用 `getUpdates` 轮询；`send_message` / `edit_message` / `get_file` 等仍经上述 httpx 客户端访问 `api.telegram.org`。**说明：** 缓冲 flush 时 **`send_chat_action`（正在输入）** 若报 `httpx.ConnectError` / `NetworkError`，仅 **WARNING**、不中断生成，多见于当前进程**无法稳定连上** `api.telegram.org`（需检查 `TELEGRAM_PROXY`、防火墙或国际链路）。**发往用户的 `reply_text` / `send_message` 若在同一轮仍报 `telegram.error.NetworkError`**，`_generate_reply_from_buffer` **单独捕获**，用户可见提示侧重「连不上 Telegram / 检查代理」，与统称「生成回复出错」区分；`_flush_buffered_messages` 对无 `assistant_message_id` 时的补发 **`reply_text` 单次 try**，避免代理不可达时未处理异常刷屏

---

### 3.3 `llm/` — LLM 接口层

**职责：** 封装对 AI API 的 HTTP 调用，提供统一接口，屏蔽 OpenAI 和 Anthropic 的 API 差异。

**边界：**
- 优先从数据库 `api_configs` 表读取激活配置，回退到 `.env` 环境变量；激活行中的 `persona_id` 在构造时解析为实例属性 `character_id`（字符串，与 Bot 存消息共用，无则 `"sirius"`）
- 支持 `config_type` 为 `chat` / `summary` / `vision` / **`search_summary`**（**`search_summary`** 供 **`tools/search.py`** 压缩 Tavily 原文，与主对话 **`chat`** 独立激活；**语音转录 `stt` 不走本类**，由 **`bot/stt_client.py`** 单独读库调用 `/audio/transcriptions`）；**对话 API 路径**根据 `api_base` 是否含 `anthropic`（或模型名含 `claude`）选择 Anthropic Messages API 与 OpenAI 兼容 `chat/completions`；用户多模态 content 按提供商组装（Claude：`image`+base64 source；OpenAI 兼容：`image_url`+data URL）
- **读超时：** `generate_with_context` / `generate_with_context_and_tracking` 使用 `_request_timeout_seconds(messages)`：`messages_contain_multimodal_images(messages)` 为真时取 `max(LLM_TIMEOUT, LLM_VISION_TIMEOUT)`，否则为 `LLM_TIMEOUT`。**`config_type=vision`** 构造时已将 `self.timeout` 设为 `max(LLM_TIMEOUT, LLM_VISION_TIMEOUT)`，与贴纸识图等路径一致。`requests.post(..., timeout=…)` 触发超时时，ERROR 日志附带「请求中含多模态图片」或「无多模态图片，多为上下文过大或上游慢」，便于与 Bot 侧「本轮是否带图」对照。**`generate_stream`（Telegram 缓冲等）** 先令 **`stream_read = LLM_STREAM_READ_TIMEOUT`**，若 **`tools` 非 `None`** 则 **`stream_read = max(stream_read, LLM_STREAM_READ_TIMEOUT_TOOLS_FLOOR)`**；HTTP **`timeout=(min(30, stream_read), stream_read)`**，与单次非流式请求的单一 `LLM_TIMEOUT` 语义不同（流式读超时约束「两次 SSE 数据之间」）。**`generate_with_context_and_tracking`** 在 **`tools`** 非空时还将非流式 **`req_timeout`** 与 **`LLM_STREAM_READ_TIMEOUT`** 取 **`max`**（整段等待工具调用首轮可能较久）
- **HTTP 429 / 503（以代码为准）：** **`LLMInterface._post_with_retry`** 统一封装上述入口的 `requests.post`（含 **`generate` / `generate_with_context` / `generate_with_context_and_tracking` / `generate_with_thinking` / `generate_stream`** 的流式整段请求）。仅当响应状态码为 **429** 或 **503** 时**立即**重试（**无**间隔 sleep、**不**解析 `Retry-After`），最多 **5** 次重试（共 **6** 次 HTTP 请求）；每次重试前 **WARNING**。其它非 2xx **不**重试，记 ERROR 后 **`raise_for_status()`**；对 **520** / **502** / **504** 等在日志 **hint** 中提示多为 CDN/网关或上游不可用（见 **2026-04-20** 更新条）
- 不维护对话历史状态（无状态）
- 支持思维链内容提取（DeepSeek R1 的 `reasoning_content`、Gemini 的 `thinking`），由 `generate_with_context_and_tracking` / `generate_with_thinking` 等在完整响应中解析；**流式**由 `generate_stream` 在 SSE `delta` 中读取 **`reasoning_content` / `reasoning` / `thinking`** 逐段 yield `("thinking", chunk)`，正文 yield `("content", chunk)`；若此前 delta 无推理片段，则在 **`choices[0].message`** 中同名字段补一次整段（适配仅末包给推理的网关）。若 **`delta.content`** 始终未出现且尚未累计任何正文，则回退读取 **`choices[0].message.content`** 作为正文（适配仅把全文放在 `message` 的网关）。生成器返回 `{"content","thinking","usage"}`
- **OpenAI 兼容 `max_tokens`：** `_prepare_openai_payload` 写入的 **`max_tokens`** 由 **`_openai_max_tokens()`** 计算：至少为 **1**；当 **`api_base`** 含 **`deepseek.com`**（DeepSeek 官方）时上限为 **8192**，避免 **`LLM_MAX_TOKENS`** 超过上游允许范围导致 **`deepseek-chat`** 等模型请求被拒
- **Tool calling（OpenAI 兼容）：** `generate` / `generate_with_token_tracking` / `generate_with_context_and_tracking` / `generate_with_thinking` / `generate_stream` 可选传入 `tools`；`_prepare_openai_payload` 在有 `tools` 时附带 `tool_choice: "auto"`；`_parse_openai_response` 将 `choices[0].message.tool_calls` 规范为 `LLMResponse.tool_calls`（每项 `id` / `name` / `arguments` 字符串）。**`generate_stream`（SSE）** 优先拼接 **`delta.tool_calls`**；若流式未得到任何 tool 片段，则在流结束后用末次出现的 **`choices[0].message.tool_calls`** 补全（部分网关、非思维链模型仅末包给出），以便 Telegram Lutopia 等工具环能收到 **`tool_calls`** 并触发 **`on_tool_done`** 提示。Anthropic Messages API 路径暂不注入 tools，解析侧 `tool_calls` 恒为 `None`
- **Token 统计：**仅当调用带 tracking 的方法且响应中含 `usage` 时才会写入 `token_usage`（见 §5.11）：若当前线程存在**正在运行的** asyncio 事件循环，则 `create_task` 走 `_async_save_token_usage`；否则（例如 `bot/vision_caption.py` 在 `run_in_executor` 线程内调 vision LLM）**同步**调用 `get_database().save_token_usage(...)`，避免 `no running event loop` 与未 await 的协程告警。`generate` / `generate_simple` / `generate_with_context` / `chat` **不会**落库用量。**`generate_stream`（SSE）** 在 payload 中加入 `stream_options: {"include_usage": true}`，使 OpenAI 兼容网关在末包携带 usage 数据；Telegram 缓冲 flush 的流式路径在主线程(`done_payload`) 收到 usage 后调用 `_async_save_token_usage`（原子补记，绕过子线程无事件循环限制），Anthropic 与非缓冲路径亦同步补调 `_save_token_usage_async`。

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

**CedarClio 输出 Guard（以 `llm/llm_interface.py` 为准）：** 拦截模型「出戏」道歉/安全拒答等，避免污染对话与记忆库。
- **正文判定 `body_for_output_guard`：** 自左向右剥离完整思维链块；支持多种开闭标签对（`COT_TAG_PAIRS`，含 `<redacted_thinking>`、`<thinking>`、反引号 `think` 块等，按 open 长度优先匹配）。若仅有开标签且长时间无闭标签：**开标签内累计超过 `_GUARD_COT_UNCLOSED_INNER_MAX`（默认 12000）字符**仍无闭合，则从该偏移起**强制视为正文**并启用检测（防截断/漏写闭合导致永远不检测）。
- **流式：** `StreamContentGuard` + `generate_stream` 对正文 delta 前置掐断；返回字典含 **`guard_refusal_abort`**。
- **Telegram（同步）：** `telegram_bot` 对 OpenAI 流式与 Anthropic 非流式均在首轮命中后**最多静默重试 1 次**（`append_guard_hint_to_last_user_message` + `TELEGRAM_GUARD_PROMPT_APPEND`）；仍空或拒答则用 **`_TELEGRAM_GUARD_ROLEPLAY_FALLBACK`**，不向用户展示拒答原文。
- **摘要/跑批（异步）：** `batch_one_shot_with_async_output_guard` 最多 **5** 次、温度递减，第 2 次起 user 附加 `ASYNC_BATCH_GUARD_PROMPT_APPEND`；仍失败抛 **`CedarClioOutputGuardExhausted`**（`micro_batch` 跳过 chunk 写入；`daily_batch` 按步骤跳过或降级，以代码为准）。
- **Step4 打分：** `coerce_score_and_arousal_defaults` 仅做数值兜底（**不走** Guard 文本重试）。

---

### 3.4 `memory/` — 记忆系统层（核心）

这是整个项目最复杂的模块，实现了分层记忆架构。

#### 3.4.1 `database.py` — 数据持久化

**职责：** 封装所有 PostgreSQL 操作，提供单例 `MessageDatabase` 实例和模块级便捷函数。使用 `asyncpg` 连接池（`min_size=2, max_size=10`），所有数据库操作均为 `async def`。

**边界：**
- 所有数据库操作都通过此模块，其他模块不直接操作数据库
- 提供 `get_database()` 单例工厂函数（同步）；应用启动时需调用 `await initialize_database()` 完成连接池初始化（读取 `config.DATABASE_URL`、调用 `init_pool` 并建表）
- **`save_message(..., is_summarized=0|1, thinking=None)`**：`MessageDatabase.save_message` 将 **`thinking`** 写入 `messages.thinking`；**模块便捷函数必须与类方法签名一致**（含 **`thinking`**）并原样转发，否则上层 `save_message(..., thinking=...)` 会在运行时抛出 **`TypeError`**。**`save_message`** 在绑 PostgreSQL TEXT 列前，将 **`user_id` / `channel_id` / `message_id` / `character_id` / `platform` / `media_type`** 统一转为 **`str`**（Telegram 等平台 ID 常为 `int`，避免 asyncpg 报「expected str, got int」）。**`update_message_vision_result`** 在 `image_caption` 为 `[视觉解析失败]` / `[系统提示：视觉解析超时失败]` 时同步置 **`is_summarized=1`**
- 管理核心数据表（含 `meme_pack` 等，及日志/统计表）的 CRUD 操作；启动时由 `migrate_database_schema()` 幂等补齐列与索引（每次初始化成功执行后，`memory.database` 打 **INFO** 日志：`数据库 schema 迁移（索引/列）已执行`）
- Context 只读：`get_all_active_temporal_states()`（`temporal_states.is_active=1` 全量）、`get_recent_relationship_timeline(limit)`（数据库按 `created_at` 倒序取前 `limit` 条；`context_builder` 注入前对关系时间线再按 `created_at` 正序排列）
- 记忆卡片：`get_memory_cards()` 仅返回 `is_active=1`（供 API / Context）；日终 Step 3 Upsert 使用 `get_latest_memory_card_for_dimension()`，按 `user_id` + `character_id` + `dimension` 取**最近一条且不过滤 `is_active`**，避免批量软删后无法命中旧行；`update_memory_card(..., reactivate=True)` 在更新正文同时将 `is_active` 置 1（跑批合并写回后重新展示）

**✅ 已修复（2026-04-05）：** `get_messages_filtered` 的 **`date_from` / `date_to`** 若以字符串传入（如查询参数），在 SQL 绑定 `created_at::date` 条件前用 **`date.fromisoformat`** 转为 **`datetime.date`**，避免 asyncpg 类型不匹配导致 **`GET /api/history` 500**（History 页日期筛选）。

**✅ 已演进（2026-04-09）：** 关键词条件为 **`(COALESCE(content, '') ILIKE $pattern OR COALESCE(thinking, '') ILIKE $pattern)`**（同一绑定参数重复用于两列），`pattern` 为 `%keyword%`；`keyword` 在 **`get_messages_filtered`** 内对入参 **`(keyword or "").strip()`** 后再判断是否拼接 WHERE，避免仅空白仍过滤。单条维护：**`update_message_by_id(message_id, content=..., thinking=...)`** 仅更新非 `None` 的字段；**`delete_message_by_id(message_id)`** 按主键删除一行。

**✅ 已修复（2026-04-07）：** asyncpg 不接受字符串形式的日期/时间参数。以下方法在绑定 SQL 前统一将字符串转为对应 Python 类型：`update_daily_batch_step_status`（`batch_date` → `datetime.date`）、`list_incomplete_daily_batch_dates_in_range`（`start_date`/`end_date` → `datetime.date`）、`mark_expired_skipped_daily_batch_logs_before`（`before_date` → `datetime.date`）、`list_expired_active_temporal_states`（`as_of_iso` → `datetime.datetime`）；`get_token_usage_stats` 的 `start_date` 也改为传 `datetime.datetime` 对象，避免 `DataError`。

#### 3.4.2 `context_builder.py` — Context 组装

**职责：** 在每次 LLM 调用前，将多个记忆来源组装成完整的 `messages` 数组；Anthropic 路径保留 `system` text block array 以使用 1h Prompt Cache，OpenAI 兼容路径会在 `llm_interface._openai_compatible_messages` 中压平为字符串并移除 `cache_control`。

**Anthropic system block 组装与缓存结构（从前到后）：**
1. **BP1 固定块：** 人设 / 系统规则、`MEMORY_BLOCK_PRIORITY_DIRECTIVE`、引用规则、思维链语言要求，以及可选 `TOOL_ORAL_COACHING_BLOCK`。该块最稳定，末尾带 `cache_control={"type":"ephemeral","ttl":"1h"}`。
2. **BP2 慢变记忆块：** `temporal_states`、`memory_cards`、`relationship_timeline`、`daily_summaries`。这类内容通常按天或跑批变化，单独缓存可避免本轮召回扰动固定人设块。
3. **BP3 chunk 块：** `chunk_summaries`（**`get_today_chunk_summaries()`** 无参：**`summary_type='chunk'`** 且 **内容日** `COALESCE(source_date::date, created_at::date) <=` 东八区今日，含尚未被日终卷入的积压 chunk；全局按 `created_at` 正序）。
4. **非缓存动态尾部：** 当前系统时间、最近工具执行摘要、长期记忆检索结果、历史桥接语。长期记忆块标题为 **`# 本轮召回的相关长期记忆`**，并显式提示 **`以下记忆可能来自过去日期，不代表今天发生；请以条目日期为准。`**

**长期记忆检索：** ChromaDB **`retrieval_top_k`** 条 + BM25 同 **`retrieval_top_k`** 条；两路均在 **`memory/retrieval.py`** 按 **`metadata.summary_type` 白名单过滤**：默认 **`daily` / `daily_event` / `manual`**，**不含** **`state_archive`**；当 **`is_retrospect_query(user_message)`**（用户消息命中回溯关键词表）时白名单追加 **`state_archive`**。过滤在 **`context_builder`** 内对本轮 **`user_message`** 计算（**非**独立 gateway 入参）。按 `doc_id` 去重后最多 **`2 × retrieval_top_k`** 条候选；**进入精排前**按 Chroma `metadata.parent_id` 做父子折叠——同一父文档（当日 `daily_*`）与下属 `*_event_*` 片段为一组，组内仅保留语义相似度最高的一条；注入 prompt 时每条正文前带 `[uid:<chroma_doc_id>]` 前缀，与回复末尾引用 `[[used:uid]]` 中的 `uid` 一致。

**messages 组装：**
1. 最近消息（当前 session 中 `is_summarized=0` 的最新若干条，正序；条数优先 `config` 表 `short_term_limit`，否则环境变量 `CONTEXT_MAX_RECENT_MESSAGES`；**`format_user_message_for_context`**：先输出去掉图片/贴纸/语音结构行后的**纯文字**；再按 **`media_type.split(",")` 的顺序**依次调用 `_format_image_part` / `_format_sticker_part` / `_format_voice_part`（主函数仅路由）；**`media_type='reaction'`** 时由 **`_format_reaction_part`** 原样返回 `content`（Bot 已拼好完整语义）。`image_caption` 在 `_format_image_part` 中按**单字符串**处理（未来多图可升级为 JSON 数组）。旧行若无 `media_type`，则按正文出现顺序推断 `image`/`sticker`/`voice`。**`role=assistant`** 条在上述格式化之后、注入 messages 前再经 **`strip_lutopia_behavior_appendix`**，去掉历史 **`[行为记录]`** 后缀（旧库兼容）。**`role=user`** 条在注入 messages 前再经 **`inject_user_sent_at_into_llm_content(..., msg["created_at"])`**，正文首行增加东八区 **`【当前时间：…】`**（**仅 LLM**，库内 **`content` 不变**）。
2. **BP4 近期原文前缀：** 最近消息超过 2 条时，在倒数第 3 条消息上加 1h cache breakpoint，让更早的近期原文形成可复用前缀；最近 2 条与当前消息保持非缓存高新鲜度。
3. 当前用户消息（可选多模态：`build_context(session_id, user_message, images=..., llm_user_text=..., exclude_message_id=...)`，`images` 非空时由 `build_user_multimodal_content` 组装最后一轮 user content；再由 **`inject_user_sent_at_into_llm_content(..., None)`** 用**当前时刻**注入同上时间行，多模态时写入**首个 text 段**）。

**精排（仅异步路径）：** 并行双路检索并折叠后，对剩余候选调用 Cohere 得到语义相关分；对每条再算时间衰减复活分（`_memory_age_days`：**优先**用 metadata `last_access_ts` 计龄；**仅当缺失或无法解析时**用 `created_at` 兜底）：

```
arousal          = clamp(metadata.arousal ?? 0.1, 0.0, 1.0)   # 历史数据无此字段时兜底 0.1
effective_hl     = halflife_days × (1 + arousal)               # arousal 越高半衰期越长
decay_score      = base_score × exp(-ln(2) / effective_hl × age_days) × (1 + 0.35 × ln(1 + hits))
```

两路分数各自在当批候选内 min-max 归一化后按 **0.8×语义 + 0.2×衰减** 综合得分排序，取 top **N** 写入 context（N=`config.context_max_longterm`，默认 **3**）。

**边界：**
- 向量路 **`search_memory(..., where=...)`** 与 BM25 路 **`search_bm25(..., allowed_summary_types=...)`** 共用 **`memory.retrieval.chroma_where_longterm_summary_types` / `longterm_allowed_summary_types`**（与上条 **`summary_type`** 白名单一致）
- 同步版 `build_context()`：双路检索 + 父子折叠，无 Cohere；长期记忆块标题为「双路检索结果」
- 异步版 `build_context_async()`：并行检索 + 折叠 + Cohere 全候选打分 + 上述融合公式取 top **N**（同上）；`COHERE_API_KEY` 不可用时回退为同步双路逻辑
- **`MEMORY_BLOCK_PRIORITY_DIRECTIVE`**：在固定 system 缓存块内、系统人设之后注入（多区块信息冲突时：以 **`memory/context_builder.py`** 常量为准——**近期消息 > chunk碎片摘要 > 时效状态 > 记忆卡片 = 关系时间线 > 每日小传 > 长期记忆**；**同类型块内以日期更近的条目为准**；**时效状态 `action_rule`** 与冲突消解的补充说明见代码原文）。该优先级是**冲突消解规则**，与缓存块排列不是同一概念。**`MEMORY_CITATION_DIRECTIVE`** 与 **`THINKING_LANGUAGE_DIRECTIVE`** 同属固定块：引用须文末 `[[used:uid]]`；**勿**用单括号 / 书名号形式；并说明注入块内 **`[uid:xxx]`** 与 **`[[used:xxx]]`** 一一对应；思维链须中文
- 可选 `telegram_segment_hint=True`（`build_context` / `build_context_async`）：在 system 末尾再追加 **`format_telegram_reply_segment_hint()`**——**【Telegram 排版】**：HTML 白名单、自然换行多气泡、`|||` 可选强制分段、MAX_CHARS / MAX_MSG（`config` 表；**发送侧**二者仅约束**无 `|||`** 时的二级强分）、`[meme:…]` 与顺序说明；正文勿用大段 `<blockquote>` / 行首 `>`（思维链 blockquote 由系统处理）；`|||` 不得出现在思维链；仅 Telegram 缓冲路径启用）
- 可选 **`tool_oral_coaching=True`**：在 system 末尾追加 **`TOOL_ORAL_COACHING_BLOCK`**（调用工具前口语提示）；与 `telegram_segment_hint` 可并用；**`persona.enable_lutopia`** 且主对话走 OpenAI 兼容 **tools** 时由 **Telegram / Discord** 在 **`build_context`** 调用前置位
- **工具执行摘要：** `context_builder._build_recent_tool_executions_section` 读取最近 3 个工具回合的 `tool_executions.result_summary`，按 `turn_id` 分组后注入非缓存动态尾部；不会把长帖/网页 raw 直接塞入 Context。

**✅ 已改动（2026-04，以代码为准）：** `_build_system_prompt` 为 **`async def`**：`await get_active_api_config('chat')` 取 **`persona_id`**，再 **`SELECT * FROM persona_configs WHERE id = …`**，正文由 **`build_persona_config_system_body(row)`** 拼装：**【系统规则】**（非空则置于最前）；随后 **`build_char_persona_prompt_sections`**：Char 侧为 **【存在定义】**（`char_name` 锚点句 + `char_identity` + `char_appearance`）、**【内在人格】**、**【表达契约】**、**【关系与形象】**（仅 `char_relationships`）、**【成人内容】**、**【工具与场景】** 等；最后接 **【User 的人设】** 标签行块。与 Mini App 右侧「拼接预览」及 **`GET /api/persona/{id}/preview`** 同源；**不含** Mini App「复制」剪贴板里的 Markdown `#` 大标题（该格式仅便于人工粘贴阅读）。无有效 `persona_id`、行不存在、拼装结果为空或异常时回退 **`config.SYSTEM_PROMPT`**。`build_context` / `build_context_async` 均 **`await self._build_system_prompt()`**。

**日志（以代码为准）：** 顶层 `build_context` / `build_context_async` 捕获异常后回退最小 context，以及各 `_build_*_section` 失败返回空串/空列表等**可恢复**路径，使用 **WARNING**（与硬故障区分）。

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
         读取本批消息 id 范围内的 tool_executions.result_summary，整理为【期间工具使用】
              ↓
         `SummaryLLMInterface` → `LLMInterface.generate_with_context_and_tracking`（`platform=Platform.BATCH`）生成 chunk 摘要
              ↓
         写入 summaries 表（summary_type='chunk'）
              ↓
         批量标记消息 is_summarized=1
```

**✅ 已改动（2026-04）：** `process_micro_batch` 开头 **`await fetch_active_persona_display_names()`**（`get_active_api_config('chat')` → `persona_id` → **`persona_configs`** 取 **`char_name` / `user_name`**，失败或空则 **`AI` / `用户`**），经 **`generate_summary_for_messages(..., char_name=..., user_name=...)`** 传入 **`SummaryLLMInterface.generate_summary`**。`generate_summary` 在 prompt 首行注入 **`这是 {char_name} 与 {user_name} 的对话记录。`**，拼装对话正文时用 **`{user_name}:`** / **`{char_name}:`** 替代原先的「用户:」「助手:」；chunk 摘要指令为 **约 150–200 字**，强调主要话题、**双方情绪起伏**、关键信息并弱化无意义语气词，结尾 **`摘要（中文）:`**。模块导出 **`fetch_active_persona_display_names`** 供日终跑批复用。

**✅ 已改动（2026-04-19）：** **`generate_summary_for_messages`** 在构造 **`formatted_messages`** 时对每条 **`content`** 先 **`strip_lutopia_internal_memory_blocks`**（去掉 **`[系统内部记忆：…]`**），再交给 **`SummaryLLMInterface.generate_summary`**。

**✅ 已改动（2026-04-20）：** **`process_micro_batch`** 在 **`save_summary`** 时传入 **`source_date=chunk_source_date_from_messages(messages)`**（本批最后一条消息时间的东八区日历日），供 Mini App / 日终按内容日筛选与卷入。

**✅ 已改动（2026-04-20）：** **`generate_summary_for_messages`** 先 **`await _resolve_micro_batch_memory_prefix(messages)`**（**`get_memory_cards`** 读 **`current_status` / `relationships`** 各最新激活行 + 关系锚点），传入 **`SummaryLLMInterface.generate_summary(..., memory_prefix=...)`**，插在 **`这是 {char_name} 与 {user_name} 的对话记录。`** 与「请为以下对话生成…摘要」之间；**`get_unsummarized_messages_by_session`** 的 SELECT 含 **`character_id`** 以便按会话对齐记忆卡主键（缺省时 **`_active_character_id_fallback()`**）。

**✅ 已改动（2026-04-26）：** **`generate_summary_for_messages`** 还会调用 **`_resolve_micro_batch_tool_context(messages)`**：按本批消息最小/最大 `messages.id` 读取 **`get_tool_executions_for_message_range`**，把 `tool_name`、`arguments_json` 与 `result_summary` 整理成 **【期间工具使用】** 注入摘要 prompt。这样工具查询结果会进入 chunk / daily 记忆链，而不依赖近期原文里残留的临时上下文。

**✅ CedarClio Guard（2026-04-09）：** `SummaryLLMInterface.generate_summary` 经 **`batch_one_shot_with_async_output_guard`** 生成 chunk 摘要；若 Guard 用尽则**不落库、不标记已摘要**（与 §3.3 一致）。

**日志（以代码为准）：** Guard 用尽跳过写入、摘要未生成即返回、检查/处理/触发路径吞异常不阻断主流程等，对应 **WARNING**（非 ERROR）。

**✅ 已改动（2026-04-21）：** 连续 **3** 次 chunk 摘要无法落库（空摘要 / Guard）时 **`_record_consecutive_chunk_llm_failure`** 经 **`send_telegram_main_user_text`** 告警后计数归零；**成功写入 chunk 并 `mark_messages_as_summarized_by_ids`** 后 **`_consecutive_chunk_failures = 0`**。

#### 3.4.4 `daily_batch.py` — 日终跑批

**职责：** 在东八区某业务日执行流水线（**`daily_batch_log` 记录 Step 1–5**，支持断点续跑；**Step 3.5** 插在 Step 3 与 Step 4 之间，**不写入** `stepN_status`）。**标准部署**下由 **cron（或同类）按 `config.daily_batch_hour` 所设整点**（默认 23:00）调用项目根目录 **`run_daily_batch.py`** 触发；`daily_batch_hour` 为业务约定，**cron 表达式须与之一致**（代码不会替运维「自动对齐」系统时钟）。

**✅ 已修复（2026-04-07）：** `run_daily_batch`（`DailyBatchProcessor`）内对所有 DB 便捷函数的调用均已正确加 `await`，包括 `update_daily_batch_step_status`、`list_expired_active_temporal_states`、`deactivate_temporal_states_by_ids`、`get_today_chunk_summaries`、`delete_today_chunk_summaries`、`save_summary`、`get_recent_daily_summaries`、`get_latest_memory_card_for_dimension`、`update_memory_card`、`save_memory_card`、`insert_relationship_timeline_event`、`mark_expired_skipped_daily_batch_logs_before`、`list_incomplete_daily_batch_dates_in_range`。此前缺少 `await` 会导致协程对象未执行，跑批静默跳过大量步骤且不报错。

**✅ 已修复（2026-04-14）：** Step 4 价值打分 **`prompt`** 为 **f-string** 时，说明文案中的**字面花括号**（JSON 示例）须写成 **`{{` / `}}`**；若误写单独的 **`{}`**，Python 在**导入模块阶段**即 **`SyntaxError: f-string: empty expression not allowed`**，`run_daily_batch.py` 无法加载 **`memory.daily_batch`**，cron 日终静默失败。

**五步流水线：**

| 步骤 | 说明 |
|------|------|
| Step 1 | 巡视 `temporal_states`：`expire_at` 已到期且 `is_active=1` 的记录先 `UPDATE is_active=0`，再用 SUMMARY LLM 将 `state_content` 从「进行时」改写为过去时客观事实，结果列表供 Step 2 使用 |
| Step 2 | **`_resolve_batch_memory_identity`**（当日 **`get_today_user_character_pairs`** 首对）后，**`_persona_dialogue_prefix()` + `_memory_context_prefix()`**（锚点 + **`current_status`/`relationships`** 激活卡）+ Step 1 输出 + **chunk**：**`get_today_chunk_summaries(batch_date)`**（**`COALESCE(source_date::date, created_at::date) <= batch_date`**，积压内容日一并并入）；每条 **`summary_text`** 先 **`strip_lutopia_internal_memory_blocks`**。**不**拼接未达阈值的**原始 `messages`**。有材料时生成今日小传（**`save_summary(..., summary_type='daily', source_date=batch_date)`**）；**成功后** **`delete_today_chunk_summaries(batch_date)`**（**同上界 `<= batch_date`** 删除 chunk）。**若既无 chunk 又无 Step 1 产出**：**不写** `daily`、**不**删 chunk，仍标记 Step 2 完成 |
| Step 3 | 七维 JSON：prompt 在今日小传前附 **7 维既有卡**（对每维 **`get_latest_memory_card_for_dimension`**，拼 **`old_cards_block`**）；输出要求含禁止跨维重复、**仅提取增量/变化/冲突**等。记忆卡片 Upsert：无则 `INSERT`；**有则**先 **`_merge_memory_card_contents`**；对 **`current_status` / `preferences`** 合并结果为 JSON **`merged` + `discarded`**，**仅当 `discarded` 非 null** 时经轻量 LLM 改写后 **`add_memory`**（`state_archive`，metadata 含 **`archived_at` / `original_dimension`**，改写失败则 **`rewrite_failed`**）再 `UPDATE`；其余维度仍为 **`{"content"}`** 合并。**关系时间轴**：LLM JSON → **`insert_relationship_timeline_event(..., created_at=combine(batch_date, 23:59:59))`**（**`batch_date` 业务日**；见 §5.5） |
| Step 3.5 | **`step3_status=1` 且 `step4_status=0`**：`get_daily_summary_by_date` 取当日 **`daily`** 正文 → **`_step35_extract_temporal_states`**：解析 **`new_states` / `deactivate_ids` / `adjust_expire`** → **`save_temporal_state`** / **`deactivate_temporal_states_by_ids`** / **`update_temporal_state_expire_at`**；**LLM 失败或 JSON 整段 `None`** 时 **`raise`**，**`run_daily_batch`** 内对该步 **最多 3 次重试**；三次仍败 **WARNING** + **`send_telegram_main_user_text`**；**不** `return False`、**不占** `daily_batch_log` |
| Step 4 | 主 LLM 打分，prompt 同步输出 `score`（整数 1–10）与 `arousal`（浮点 0.0–1.0，情绪强度；平静约 0.1，激烈事件 0.8+）；`halflife_days`：8–10→600，4–7→200，1–3→30。**全量**向量化入库（`generate_with_context_and_tracking`，`platform=Platform.BATCH`）；metadata 新增 `arousal: float`；先存 `daily_{batch_date}`，再按需拆分事件片段 `daily_{batch_date}_event_N`（同含 `arousal`），metadata 含 `parent_id` 指向当日主文档；增量更新 BM25 |
| Step 5 | Chroma GC：`vector_store.garbage_collect_stale_memories()` — **前置豁免**：`hits >= gc_exempt_hits_threshold`（优先 `config` 表 `gc_exempt_hits_threshold`，默认 10）则跳过不删；再依次判断：闲置天数超过 `gc_stale_days`（默认 180）、半衰期衰减得分 \<0.05、无子文档以该 `doc_id` 为 `parent_id`，三条全满足才物理删除 |

**Step 3 实现要点（与代码一致）：**
- **维度 JSON 提取与重试策略：** 对所有 SUMMARY LLM 相关 JSON 结构和主 LLM 的 `score` / `arousal` 分数结果提取，均已统一采用基于 `_retry_call_and_parse` 的重试机制，出错时最多重试 5 次，并在内部对 SUMMARY LLM 返回依次尝试整段 `json.loads`；失败则截取**首个平衡的 JSON 对象**（跳过前置说明、处理字符串内转义；支持 \`\`\`json 代码块）；再回退原贪婪 `\{...\}` 正则；如果仍失败且未达上限将触发 asyncio 等待并重新生成。
- **Upsert 行定位：** `get_latest_memory_card_for_dimension()`（不过滤 `is_active`），保证「全表 `is_active=0` 后重跑」仍更新同一逻辑行，而非误当作无记录而堆叠 `INSERT`。
- **合并写回：** `_merge_memory_card_contents` → `_call_summary_llm_custom`（**不经** `SummaryLLMInterface.generate_summary` 的 chunk 外壳）。**维度三分支：** `interaction_patterns` 单独细则；**`current_status` / `preferences`** 输出 **`{"merged","discarded"}`**（`discarded` 仅在有实质覆盖时非 null，再触发 `state_archive` 改写入库）；**其余维度**保留「矛盾则 `[YYYY-MM-DD]` 标注」句，输出 **`{"content":"…"}`**。合并失败则 fallback 为「旧正文 + `[batch_date]更新` + 新摘要」且**不**写 `state_archive`。`update_memory_card(..., reactivate=True)` 写库并**重新激活**。

**跑批 Prompt 与人物称呼（2026-04，与代码一致）：**
- **`run_daily_batch`** 在 **`await LLMInterface.create()`** 之后 **`await fetch_active_persona_display_names()`**（同 §3.4.3，来自 `memory.micro_batch`），写入 **`_batch_char_name` / `_batch_user_name`**；定 **`batch_date`** 后 **`await _resolve_batch_memory_identity(batch_date)`** 写入 **`_batch_user_id` / `_batch_char_id`**；**`_persona_dialogue_prefix()`** 返回 `这是 {char} 与 {user} 的对话记录。\n`。
- **Step 1**（时效状态 JSON 数组改写）：**前缀 + 原任务正文**，仅 **`_call_summary_llm_custom`**，避免套 chunk「为对话生成摘要」模板。
- **Step 2**（今日小传）：**`_persona_dialogue_prefix()` + `await _memory_context_prefix()`** + 按时间顺序、话题/事件/情感、保留互动细节与羁绊、勿分点列举等指令 + **`today_content`** + **`今日小传（中文）:`**，**`_call_summary_llm_custom`**。**`today_content`** 为空时跳过 **`save_summary`** / **`delete_today_chunk_summaries`**。
- **Step 3**（七维 JSON、**关系时间轴** JSON）：七维 prompt 前附 **`old_cards_block`**（7 维各取最近一条既有卡）；输出要求含**单条事实只归入最相关一维、禁止跨维重复**与**增量对比**句。仍 **`summary_llm.generate_summary(...)`**；关系时间轴 **`tl_prompt`** 要求 **第三人称客观**、**真实姓名**指称双方、**禁止**「我/你」及「今天/昨天」等相对时间词（与 §3.4.4 表一致）。
- **Step 3.5**（时效三操作）：见上表；**`_call_summary_llm_custom`** 产出 JSON → **`_parse_step35_temporal_operations_json`**（失败为 **`None`** → **`raise`** 触发外层 **3** 重试）→ 三支串行写库；单条坏数据仅 **WARNING**。
- **Step 4**（小传 **score/arousal**）：主 LLM 的 user **prompt 前加 `_persona_dialogue_prefix()`**；**事件拆分**仍 **`generate_summary`** 并传 `char_name` / `user_name`。

**断点续跑：** `daily_batch_log` 记录 `step1_status`～`step5_status`，重启后跳过已完成步骤；另含 **`retry_count`**（**`migrate_database_schema`** 幂等追加），五步**全部成功**后 **`reset_daily_batch_retry_count`** 置 **0**。

**失败后延迟重试与熔断（以代码为准）：** `run_daily_batch.py`、`schedule_daily_batch`、`trigger_daily_batch_manual` 在 **`run_daily_batch` 返回失败** 时调用 **`schedule_daily_batch_retry_if_needed`**：若 **`retry_count >= 3`**，**不再** `Popen` 子进程，向 **`TELEGRAM_MAIN_USER_CHAT_ID`**（**`bot/telegram_notify.send_telegram_main_user_text`**，未配置则跳过）发送熔断文案并记 **ERROR**；若 **`< 3`**，先 **`spawn_run_daily_batch_retry_after_hours`**（**7200s** 后执行 **`run_daily_batch.py <batch_date>`**），**`Popen` 成功后再** **`increment_daily_batch_retry_count`**，并发「已安排 2 小时后重试」Telegram（发送失败仅 **WARNING**）。

**系统日志保留（Mini App「系统日志」）：** 每次 **`run_daily_batch`** 在五步流水线**开始前**调用 **`purge_logs_older_than_days(7)`**（`MessageDatabase` / `memory.database` 模块便捷函数），删除 **`logs`** 表中 **`created_at` 早于当前时刻 7 天** 的行；删除数大于 0 时 INFO。清理失败仅 **WARNING**，不中断跑批（见 §5.10）。

**库内自建调度（`schedule_daily_batch`，可选）：** 每次到点先将 `batch_date` 早于「含今日共 7 天」窗口且仍有未完成步骤的行标记为 `error_message='expired, skipped'`、五步均置 1；再对窗口内未完成日期按 `batch_date` 升序串行调用 `run_daily_batch(该日)`；若当日未出现在补跑列表中，最后再 `run_daily_batch()` 执行今天。**当前 `main.py` 主进程不启动此循环**；若需进程内定时器，须自行在独立进程或脚本中调用，生产推荐 **cron + `run_daily_batch.py`**。

#### 3.4.5 `vector_store.py` — 向量存储

**职责：** 封装 ChromaDB 操作，使用智谱 AI `embedding-3` 模型生成向量；**工程约定为 1024 维**（与占位零向量、检索逻辑一致）。

**边界：**
- 日终由 `daily_batch` 全量写入当日小传（及可选事件片段）；Step 3 在 `current_status`/`preferences` 合并产生 **`discarded`** 时写入 `state_archive`（与 `daily` / `daily_event` / 手工长期记忆并列存在于同一 Chroma 集合）；**Context 长期记忆召回**默认按 `memory/retrieval.py` 白名单排除 `state_archive`，用户消息命中回溯关键词时再纳入；手工长期记忆仍通过 Mini App 写入
- **`ZhipuEmbedding.get_embedding`**：HTTP **429/503** 最多 **3** 次、间隔 **2s**；**`VectorStore.add_memory`**：整段写入最多 **3** 次、间隔 **1s**（仍失败则 **`False`**，由调用方处理）
- 提供 `add_memory()` / `search_memory()` / `delete_memory()` / `update_memory_hits()` 便捷函数
- 集合名由 **`config.CHROMA_COLLECTION_NAME`** 决定（`.env` 可选；未设置时为 **`{APP_NAME}_memories`**，`config.APP_NAME` 默认 `cedarstar`）；与 **`meme_pack`** 集合相互独立
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
- `add_meme` / `upsert_meme`：对 **`document_text`（未传则用 `name`）** 调用 **同步** `siliconflow_embed_text`（仅 .env `SILICONFLOW_API_KEY`）后写入 Chroma。**`add_meme_async` / `upsert_meme_async`**：在已有 asyncio 循环中调用，嵌入走 **`siliconflow_embed_text_async`**（读库内激活 `embedding` 配置，与 `search_meme_async` 一致）。metadata 均含 `name`、`description`（与用于嵌入的 strip 后文本一致）、`url`、`is_animated`、`sqlite_id`（实际为 `meme_pack.id`）
- **`has_meme_id(meme_id)`**：`collection.get(ids=[...])` 判断 Chroma 是否已有以 PG **`meme_pack.id`** 为 id 的文档（供 `import_memes` 判断「仅补向量」）
- `search_by_vector(vector, top_k)`：返回 metadata 列表（含解析后的 `id`）
- 批量导入可走 **`scripts/import_memes.py`**（`await initialize_database()`、库内激活 **vision** 与 **embedding**、`add_meme_async` / `upsert_meme_async`）；亦可单独调用 `add_meme` / `upsert_meme`；均不属核心 Bot 启动路径

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
| `/api/persona` | `persona.py` | 人设配置 CRUD；**`GET /{id}/preview`** 与 **`build_persona_config_system_body`** 一致 |
| `/api/memory` | `memory.py` | 记忆卡片 CRUD + 长期记忆 + `temporal-states` / `relationship-timeline`（长期记忆列表合并 Chroma 元数据，见下） |
| `/api/history` | `history.py` | **`GET ""`** 列表（平台/关键词/日期 + 分页）；**`PATCH /{message_id}`** 更新正文/思维链；**`DELETE /{message_id}`** 删除单条（均 `{success,data,message}`） |
| `/api/logs` | `logs.py` | 系统日志查询（`platform` / `level` / `keyword` / 可选 `time_from`·`time_to` + `page` / `page_size`） |
| `/api/config` | `config.py` | 运行参数读写（含 `buffer_delay`、`chunk_threshold`、`telegram_max_chars`、`telegram_max_msg` 等，见 §5.7） |
| `/api/settings` | `settings.py` | API 配置 CRUD + 激活切换 + Token 统计 |

**Mini App 设置：存储与保存方式（速查）**

- **存储位置：** 均在同一 PostgreSQL 库（由 `memory/database.py` 的 `initialize_database()` 初始化，DSN 来自 `.env` 的 `DATABASE_URL`），**无**独立 `settings` 配置文件目录。与 Mini App 强相关表：**`config`**（`key` / `value` / `updated_at` 运行参数）、**`api_configs`**（多组 API：`name`、`api_key`、`base_url`、`model`、`persona_id`、`config_type`、`is_active` 等）、**`token_usage`**（`GET /api/settings/token-usage` 读取统计）。人设等另有 **`persona_configs`** 等表（见 §5.8）。
- **核心设置页**（`miniapp/src/pages/Settings.jsx`，路由 `/settings`）：**不是**「整页一个接口写死全部配置」。按配置行使用 **`GET` / `POST` / `PUT` / `DELETE /api/settings/api-configs`**；**`PUT /api/settings/api-configs/{config_id}`** 的请求体为 [`ApiConfigUpdate`](api/settings.py) 可选字段，**仅非 `null` 字段更新**（HTTP 为 PUT，语义接近 PATCH）；**`PUT /api/settings/api-configs/{id}/activate`** 切换激活；**`POST /api/settings/api-configs/fetch-models`** 拉模型列表；**`GET /api/settings/token-usage`** 周期统计。
- **助手配置页**（`miniapp/src/pages/Config.jsx`，路由 `/config`）：主按钮 **`PUT /api/config/config`** 提交与 [`api/config.py`](api/config.py) **`DEFAULT_CONFIG`** 键集合对齐的**整对象**，后端写回 **`config` 表**（`set_config` 逐键 `INSERT INTO ... ON CONFLICT DO UPDATE SET`）。**Telegram 回复分段**（`telegram_max_chars` / `telegram_max_msg`）另支持**仅含单键**的 **`PUT /api/config/config`**，与整页保存共用同一接口。

**边界：**
- API 层不包含业务逻辑，直接调用 `memory.database` 的方法（`dashboard.py` 的 **`GET /api/dashboard/memory-overview`** 另从 **`memory.vector_store.get_vector_store()`** 读取 **Chroma** 条数，见下）
- `dashboard.py` 维护一个进程内共享的 `_bot_status` 字典，由 bot 的 `on_ready`/`on_disconnect` 事件写入
- **`GET /api/dashboard/status` 的模型信息：** `active_api_config` / `model_name` 来自 `get_active_api_config('chat')`，与 Settings「对话 API」Tab 的激活项及 Bot 对话路径一致（不包含摘要 API）
- **`GET /api/dashboard/memory-overview`：** `chromadb_count` 为 **`get_vector_store().collection.count()`**（主记忆 Chroma 集合）；`daily_summary_count` / `active_temporal_states_count` 等为 PostgreSQL 聚合；完整字段与含义以 `api/dashboard.py` 为准
- `settings.py` 的 API Key 在返回时脱敏（只显示末4位）
- `memory.py` 长期记忆：**`GET /longterm`** 从 **ChromaDB** 分页拉取全量向量（可选查询参数 **`summary_type`** 过滤 **`metadata.summary_type`**；返回 `items` 含 `chroma_doc_id`、`content`、`summary_type`、`date`、`hits`、`halflife_days`、`arousal`、`last_access_ts`、`base_score`、`is_manual` 等）；**`POST /longterm`** 先写 Chroma（`doc_id` 形如 `manual_{uuid}`，`summary_type=manual`），成功后再写 **`longterm_memories`**；**`DELETE /longterm/{chroma_doc_id}`** 仅 **`manual_` 前缀**，先删 Chroma 再删镜像表（**`delete_longterm_memory_by_chroma_id`**）；**`PATCH /longterm/{chroma_doc_id}/metadata`** 仅更新 **`halflife_days` / `arousal`**（合并写回 Chroma metadata）
- `memory.py` 时效状态：`GET/POST /temporal-states`、`DELETE /temporal-states/{id}`（将 `is_active` 置 0）；`GET /relationship-timeline` 返回全表按 `created_at` 倒序（只读）

**✅ 已改动（2026-04-05）：** **`/api/persona`** 人设 CRUD 与预览已读写 **`persona_configs.user_work`**，与库迁移、Mini App Persona 页、`context_builder._build_system_prompt` 一致。

---

### 3.6 `miniapp/` — 前端管理界面

**职责：** 提供可视化管理界面，通过 REST API 与后端交互。

**技术：** React 18 + React Router + Vite，无 UI 组件库（纯 CSS）；人设页与移动顶栏使用 **lucide-react** 线框图标（与系统 Emoji 解耦）。

**视觉（2026-04 起，以 `miniapp/src/styles/global.css` 为准）：** **复古纸 / 工业裁纸** 调性：页面底 **`--page-bg`**（暖灰米 `#e8e4da`）、大面板 **`--surface`**（浅 off-white）、内嵌控件/子块 **`--control-surface`（白）**；主/次文字 **`--text-main`** / **`--text-sub`**（墨绿灰系）；强调 **`--accent`**（灰紫，用于 KPI/图标点缀）；线框 **`--industrial-border-color`**；**实体硬阴影** **`--shadow-solid` / `--shadow-solid-sm`**（投影色 **`--shadow-color-deep`** 灰绿，非纯黑）。圆角多为 **0**。正文字体 **Noto Sans SC**；等宽用于 slug、部分输入与分页信息。卡片角标多为 **`::before` 绝对定位**、骑在卡片上边框（**`translateY(-50%)`**）、**`width: max-content`**。各页在 `*.css` 中局部覆盖（如 Persona 的 **`--persona-*`** 低饱和变量）。

**页面与对应 API：**

| 页面 | 路径 | 对应后端 API |
|------|------|-------------|
| Dashboard（控制台概览） | `/` | `/api/dashboard/status` `/api/dashboard/memory-overview` `/api/dashboard/batch-log` |
| Persona（人设配置） | `/persona` | `/api/persona` |
| Memory（记忆管理） | `/memory` | `/api/memory/cards` `/api/memory/longterm` `/api/memory/temporal-states` `/api/memory/relationship-timeline` |
| History（对话历史） | `/history` | `GET/PATCH/DELETE /api/history`（见 §3.6 History 页） |
| Logs（系统日志） | `/logs` | `/api/logs` |
| Config（助手配置） | `/config` | `/api/config/config` |
| Settings（核心设置） | `/settings` | `/api/settings/api-configs` `/api/settings/token-usage` |

**Dashboard 页（`Dashboard.jsx` / `dashboard.css`）：** 挂载时并发请求 §3.5 三个控制台接口。顶栏为 Discord/Telegram 在线、**对话**侧激活配置名与模型（`/status`，与 `get_active_api_config('chat')` 一致）、批处理结论（由同页已拉取的 `/batch-log` 最近一条的 `step1_status`～`step5_status` 推导）。下方为跑批日历与记忆库概览；概览数据来自 **`/memory-overview`**，含 **`chromadb_count`**（Chroma 主集合条数）、**`short_term_limit`**、**`chunk_summary_count`**（今日微批摘要条数）、**`dimension_status`**（七维度圆点）、**`latest_daily_summary_time`**、**`daily_summary_count`**、**`active_temporal_states_count`** 等，具体字段以 **`api/dashboard.py`** 为准。**记忆库概览 UI：** **ARCHIVE（长期记忆库）** 两列 KPI——**已归档小传数量** | **已收录片段数量**（**`.memory-archive-metrics`**）；**REAL-TIME（实时感知）** 两列——**短期携带量（条）** | **活跃时效状态（条）**（**`.realtime-kpi-row`**）。样式层含核心 KPI 大字、今日日历高亮、维度 Tooltip 等。**记忆库概览结构**：外层 **`.dashboard-card`** 为 **`--surface`**，内层 **`.memory-section`** 多为 **`--control-surface`（白）** 以分层；网格 **`min-width: 0`** / **`overflow`** 处理防止横向撑破视口。**`.memory-overview-grid`** 设 **`overflow: visible`**（角标不被裁切）。

**Settings 页（`Settings.jsx` / `settings.css`）：** 「对话 API」「摘要 API」「视觉 API」「语音转录 API」「**Embedding**」五个 Tab，列表分别请求 `GET /api/settings/api-configs?config_type=…`（`chat` / `summary` / `vision` / `stt` / `embedding`），切换 Tab 时重新拉取。首次迁移会在 `api_configs` 插入默认 **`config_type=embedding`** 行（名称「硅基流动 bge-m3」、`base_url`/`model` 预填、`api_key` 空、**已激活**），用户在 Mini App 中补 Key 即可。新增/编辑弹窗内可改 `config_type`；**保存成功后以表单中的类型为准**——若与当前 Tab 不一致则自动切换到对应 Tab 并加载列表。`POST`/`PUT` 允许的 `config_type` 含 `embedding`（表情包向量用，与 `stt` 同理独立激活）。**UI（以代码为准）：** API / Period 分类为**横向可滚动**的独立 neo-brutalist 按钮（**`.config-tabs` / `.period-tabs`**，隐藏滚动条）；配置行列表在**窄屏**为纵向堆叠，**`cfg-mid`** 等 **`min-width: 0`** 防挤压；大卡片 **`SETTINGS`**、行内 **`API CONFIG`** 角标配合 **`.settings-page` 与卡片内边距**避免视口裁切。Token 数字区为**双列紧凑网格**（**`.token-nums`**），平台占比（**`.platform-bars`**）为**弱化的细条**辅助信息，数据仍来自动态 **`tokenStats.by_platform`** 与 **`PLATFORM_COLOR`**（逻辑同 2026-04-07）。**Token 统计首次加载：** 挂载时 **`GET /api/settings/token-usage?period=latest`**。页标题图标：**`lucide-react`** 的 **`KeyRound` / `BarChart3`**。

**Config 页（`Config.jsx` / `config.css`）：** 与 `api/config.py` 的 `DEFAULT_CONFIG` 对齐：上方为通用运行参数（**滑块 + 数字步进**），底部 **「保存并立即生效」** 一次 `PUT /api/config/config` 写回当前页全部键。其下 **「Telegram 参数」** 包含流式思维链发送开关 `send_cot_to_telegram`（默认选 1），以及 `telegram_max_chars`（10–1000、步长 10）与 `telegram_max_msg`（1–20），控件布局与同页其它行一致；每项可点 **「保存此项」** 单独 `PUT`，请求体仅含该键（仍走同一接口）。**线下极速模式（2026-04-07 新增）：** 顶部提供一键开关（`POST /api/config/offline-mode/toggle`），后端通过 `MessageDatabase.toggle_offline_mode` 利用 `config` 表进行**影子备份**（将 `buffer_delay`、`telegram_max_chars` 等写入 `backup_*`，并覆写为极速预设，同时写 `offline_mode_active=1`），关闭时从备用键还原，前端状态直接通过加载配置项的 `offline_mode_active` 推断。`memory/database.py` 的 `migrate_database_schema` 通过 `_config_insert_defaults_if_missing` 为缺失行插入默认值（`INSERT OR IGNORE`，不覆盖已有值）。**UI：** 大卡片 **`SYSTEM CONFIG`** 骑线角标与 **`.config-container` `padding-top`** 配合，避免与 Settings 页相同的视口裁切问题。

**Persona 页（`Persona.jsx` / `persona.css`，以代码为准）：** 左栏顶层 **系统规则 / Char / User / 工具（Lutopia）** 使用 **`SectionHead`**（等宽 slug + Lucide 铭牌 + 蓝图分隔线）。**Char、User** 下再嵌 **`PersonaSubBlock`**（小号 slug + 子标题 + 虚线子卡片），分组与 **`build_char_persona_prompt_sections` / `buildUserPreviewChunks`** 语义一致。主区 **左 60% / 右 40%**（**`.persona-editor`** / **`.persona-preview`**）；右侧 **拼接预览** slug **`[ PROMPT_OUT ]`**，**`PersonaPreviewStack`** 分 **系统规则 / Char / User** 三色条区，子块用【】；**`.persona-preview .persona-section-head__nameplate`** 设 **`flex-wrap: nowrap`**，避免窄屏下预览标题区图标与文字折成两行。**「复制」** 按钮写入 **`buildClipboardText(form)`**：以 Markdown **`# 系统规则` / `# Char 人设` / `# User 人设`** 便于粘贴后区分大段，子段仍为【】；**运行时 system 正文无 `#`**，仍以 **`build_persona_config_system_body`** 为准。~~系统规则旁的 Telegram HTML 提示~~ 已移除（从不写入 `system_rules`）。

**✅ 已改动（2026-04-05）：** 表单与 **`persona_configs`** 含 **用户工作**（`user_work`），与 **`context_builder`** User 块 **「工作：…」** 对齐（仍有效）。

**Memory 页（`Memory.jsx` / `memory.css`）：** 四 Tab（记忆卡片、长期记忆、时效状态、关系时间线）。**记忆卡片加载（以代码为准）：** API 可能返回同一 `dimension` 多条（不同 `user_id`）；前端 `loadMemoryCards` 合并为每维度**一条展示**，保留 **`updated_at`（无则 `created_at`）最新**的记录，避免遍历时后读到的旧行覆盖新行。**记忆卡片正文：** 列表区 `.card-content` 多行截断（**`-webkit-line-clamp`**）；是否需 **「查看全文」** 由 **`isMemoryCardContentTruncated`**（离屏同宽节点测量全文高度与可见高度）判定——**勿**仅依赖 **`scrollHeight > clientHeight`**（截断后二者常相等导致漏显）。点击后以 **`createPortal(..., document.body)`** 打开只读全屏层（**`.memory-view-overlay` / `.memory-view-sheet`**），避免与背后卡片叠层。**外壳**：`.memory-container` 为 `height: calc(100vh - 80px)`（与主内容区上下各约 `20px` 的 padding 对齐）、`overflow: hidden`；Tab 栏下方 **`.memory-content-scroll-area`** 为 `flex: 1; min-height: 0; overflow-y: auto; scrollbar-gutter: stable`，**仅该区域纵向滚动**，避免整页高度随 Tab 切换跳变。各 Tab 根为 Fragment，**首子节点**统一 **`.memory-tab-header`**（`margin-top: 24px` 与 Tab 栏留白一致），标题为 **`h2.memory-tab-header__title`**，emoji 与正文分置于 **`span.memory-tab-header__emoji` / `span.memory-tab-header__title-text`**。长期记忆条目中 Chroma 元数据用 **`.memory-meta-chip`** 胶囊展示：`hits`、`halflife_days`、`arousal`（保留两位小数，历史数据无此字段时不显示）；`hits` 达到 `gc_exempt_hits_threshold` 阈值的记忆在正文右侧显示 **`.gc-exempt-badge`**「🔒 免删」徽章（阈值从 `GET /api/config/config` 读取）。顶部 Tab（**`.memory-tabs button.memory-tab`**）采用与全站一致的新拟态凸起/选中态，外侧容器 **`.memory-tabs`** 采用了精致的 `border-radius: var(--radius-card)` 与 **`box-shadow: var(--shadow-inset)`** 内凹轨道设计（移动端亦已移除间距覆写以保留该圆润质感），使得强调色选中态的观感更贴近 §3.6「视觉」规范。**长期记忆 Tab 分页（2026-04-14）：** **`pagination.pagination--outside`** 置于 **`.memory-content-scroll-area`** 之外，与 History / Logs 同为 **首页 / 上页 / 下页 / 尾页** 与 **`pagination-info--stacked`** 两行页码。

**History 页（`History.jsx` / `history.css`）：** 筛选区 **`.filter-controls-row`** 全宽；平台 **`.platform-tabs`** 在移动端使用 2x2 网格布局以适应长文字，**`.tab-button`** 不换行。列表卡片 **`.message-list-container`** 水平 **`padding: 24px 10px`** 使对话区贴近卡片左右约 10px；内层 **`.history-chat-column`**（`max-width: 480px`，移动端 100%）**`padding-left/right: 0`**，**`.message-list`** 同样无额外左右 padding。消息气泡 **`width: fit-content`**、**`max-width: 70%`**（移动端 85%），随内容长短伸缩；**`.message-row.user-row`** **`justify-content: flex-end`** 用户气泡贴右，**`.message-row.assistant-row`** **`flex-start`** 助手贴左；内层避免 **`width: 100%`** 撑满行宽导致「中间一条」。气泡内正文统一左对齐，头部分角色对齐（移动端用户气泡头部为 row-reverse 对称）。**列表接口：** `GET /api/history?...` 与 §3.5；**单条编辑：** `PATCH /api/history/{id}`，body 至少含 `content` 或 `thinking` 之一（助手可改思维链）；**删除：** `DELETE /api/history/{id}`。前端每条气泡下有编辑/删除；编辑用弹窗 textarea；**关键词高亮**用正则 `split` 后按捕获组**奇数位**包裹 `<mark>`（勿对带 `/g` 的 `RegExp` 反复 `test`）。**并发：** `fetchHistory` 使用递增序号，仅最新一次请求的响应会更新列表，避免关键词筛选与无关键词响应互相覆盖。**气泡配色（2026-04-07 更新）：** 用户气泡背景改为淡青蓝 `#e4eef5` + 右侧绿色半透明边框 `rgba(72,199,142,0.50)` + 新拟态阴影；助手气泡改为淡紫灰 `#eaeaf1` + 左侧紫色半透明边框 `rgba(124,107,196,0.25)` + 对应方向新拟态阴影，整体与全站 Soft UI 风格对齐。**分页（2026-04-14）：** 列表与分页条分离，分页容器 **`pagination pagination--outside`** 在 **`.message-list-container`** 之外；按钮 **首页 / 上页 / 下页 / 尾页**，中间 **`pagination-info--stacked`** 两行（第 x 页 / 共 x 页）。

**Logs 页（`Logs.jsx` / `logs.css`）：** **`GET /api/logs`** 与 §3.5；筛选含 **平台 / 级别 / 关键词 / 可选开始·结束时间**（**`datetime-local`**，请求时 **`time_from` / `time_to`** 为 ISO 字符串）。列表区固定视口内滚动（**`.logs-content-scroll-area`**）；**`.logs-list-container`** 内为标题与 **`LogRow`**；单条 **`message`** 超过 **50** 码点列表仅预览 + **「查看全文」**，堆栈区仍用 **「展开」/「收起」**。**分页**同 History：**`.pagination--outside`** 在滚动区外；样式补充见 **`global.css`**。**窄屏（≤768px）：** 时间筛选行 **`.filter-row--time`** 保持**一行两列**；时间输入 **`.search-input.datetime-input`** 略小字号。

**说明：** 在 Mini App 中直接改库 **`messages`** 不会同步修正 Chroma / 摘要等派生数据；若需与向量记忆完全一致，需另行产品或批处理策略。

**API 根地址与请求封装：** 各页通过 `src/apiBase.js` 的 **`apiFetch(path, options)`** 调用后端（内部用 **`apiUrl()`** 拼 URL）。**`apiFetch`** 会为每次请求自动设置 **`Content-Type: application/json`** 与 **`X-Cedarstar-Token`**，令牌来自构建时环境变量 **`VITE_MINIAPP_TOKEN`**（未设置则为空字符串），须与服务器 `.env` 中的 **`MINIAPP_TOKEN`** 一致，否则 `/api/*` 返回 401。环境变量 **`VITE_API_BASE_URL`** 未设置或为空时 **`API_BASE_URL`** 为空字符串，URL 为相对路径 `/api/...`；**开发环境**下由 Vite 将 `/api` 代理到 `http://localhost:8000`。**生产构建**（`vite build`）会读取 `miniapp/.env.production` 等文件中的 `VITE_API_BASE_URL`，用于指向实际后端（公网域名或隧道 URL）；隧道域名变更时只需改环境变量并重新构建，勿在页面中硬编码 `localhost:8000`。**展示名：** **`VITE_APP_NAME`**（可选）→ `src/appName.js` 的 **`APP_DISPLAY_NAME`**（默认 `CedarStar`），用于侧栏 Logo 与 `main.jsx` 设置 `document.title`；`index.html` 中 **`%VITE_APP_NAME%`** 由 Vite 在构建时注入。

**侧栏与导航（`App.jsx` / `sidebar.css` / `router.jsx`）：** **`navItems`** 每项为 **`Icon` / `text` / `path`**，可选 **`dividerBefore`**（在「助手配置」前插入**横向虚线 + ■**）；**无**侧栏 **`code`** 前缀（历史 **`[ 01 ]`…`[ SYS ]`** 已移除）。**`NavLink`** 选中态 **`.nav-item.active`**：**`#1A1A1A` 粗边框 + 右下硬阴影**，**`overflow: visible`** 以免 **`overflow: hidden`** 裁切阴影；侧栏 **右缘粗黑线 + 向右黑色硬阴影**。**路由入口：** `src/router.jsx` 导出 `navItems` 与 `routes`，文件顶部 `import React from 'react'`（见 §6.11）。

**✅ 已修复（2026-04-05）：** **`miniapp/src/App.jsx`** 中 **`BrowserRouter`** 设置 **`basename={routerBasename()}`**（由 **`import.meta.env.BASE_URL`** 推导，与 **`vite.config.js` 的 `base`** 一致）。生产静态资源挂在 **`/app`** 时，无 basename 会导致路径 **`/app/`** 无法匹配路由 **`/`**，Telegram Mini App 打开白屏；设置后与生产 **`/app`** 前缀一致。后端由 **`main.py`** 的 **`serve_miniapp`**（`GET /app/{full_path:path}`，`FileResponse` + 非文件路径回退 **`index.html`**）提供资源，**不再**使用 **`StaticFiles`** 挂载。

---

### 3.7 `services/` 和 `tools/` — 扩展层

- `services/wx_read.py`：微信读书集成（仅有版本号占位，无实现）
- `tools/prompts.py`：**`LUTOPIA_TOOL_DIRECTIVE`** / **`WEATHER_TOOL_DIRECTIVE`** / **`WEIBO_HOT_TOOL_DIRECTIVE`** / **`SEARCH_TOOL_DIRECTIVE`** 等与 **`OPENAI_LUTOPIA_TOOLS`** / **`OPENAI_WEATHER_TOOLS`** / **`OPENAI_WEIBO_TOOLS`** / **`OPENAI_SEARCH_TOOLS`** 中 function 名对齐的 system 片段；**`build_tool_system_suffix(enabled)`** 按启用工具包 key 拼接；**`inject_tool_suffix_into_messages`** 将后缀追加到首条可写 `role=system` 消息（Telegram 工具路径注入 `["lutopia"]`、`["weather"]`、`["weibo"]`、**`["search"]`** 等）
- `tools/lutopia.py`：**Lutopia** 经站方 **MCP**（Python 包 **`mcp`**：`ClientSession` + SSE **`https://daskio.de5.net/mcp/sse`**）。**`OPENAI_LUTOPIA_TOOLS`** 仅 **`lutopia_cli`**（参数 **`command`**，由 MCP 工具 **`cli`** 执行）与 **`lutopia_get_guide`**（**`get_guide`**）。**Bearer** 与论坛一致：读 **`config` 表 `lutopia_uid`**，注入 **`cli`** 调用。**`create_lutopia_mcp_session`** / **`append_tool_exchange_to_messages`**（**`get_weather`** / **`get_weibo_hot`** / **`web_search`** 分别走 **`execute_weather_function_call`** / **`execute_weibo_function_call`** / **`execute_search_function_call`**）/ **`execute_lutopia_function_call`**（**`[tool]`** info 日志）。**工具执行记录（2026-04-26）：** `append_tool_exchange_to_messages` 与 `complete_with_lutopia_tool_loop` 传入 `session_id` / `turn_id` / `user_message_id` 后，每次工具调用通过 **`save_tool_execution_record`** 写入 **`tool_executions`**；一轮多个工具是多行、以 `turn_id + seq` 排序。**`summarize_tool_result_for_context`** 生成短摘要，**`tool_result_for_model`** 在 raw 超长（约 6000 字符以上）时回传压缩 JSON，避免长帖/网页直接进入当前模型上下文。落库旁白：**`lutopia_internal_memory_line`** / **`build_lutopia_internal_memory_appendix`**（**`[系统内部记忆：…]`**；CLI 读操作不生成）。**`strip_lutopia_behavior_appendix`** / **`build_lutopia_behavior_appendix`**（兼容旧 **`[行为记录]`**）；**`strip_lutopia_internal_memory_blocks`** / **`strip_lutopia_user_facing_assistant_text`**。启动时 **`ensure_lutopia_dm_send_enabled_on_startup`**（**`httpx`**：`GET/POST …/forum/api/v1/agents/me`、`dm-settings`）。站方 **`AGENT_GUIDE.md`**（**`https://daskio.de5.net/AGENT_GUIDE.md`**）
- `tools/meme.py`：**`search_meme`** / **`search_meme_async`** 调 `meme_store` 向量检索（**Telegram 有序段发表情走 `search_meme_async`**，以便 `await` 读库内 embedding 配置）；**`send_meme`** 为异步，需传入 Telegram `bot` 与 `chat_id`。不在 LLM 请求中注册为 tools；Telegram 在解析助手正文中的 **`[meme:…]`** 后调用（见 `bot/telegram_bot.py`、`bot/reply_citations.py`）
- `tools/weather.py`：**`fetch_weather`** / **`execute_weather_function_call`**（**`get_weather`**），复用 **`api.weather.fetch_weather_cached`**；**`role=tool`** 内容为 **JSON 对象字符串**（如 **`{"summary":…}`**），满足 Gemini 等网关对工具返回 **Struct** 的要求
- `tools/weibo.py`：**`fetch_weibo_hot_summary_text`** / **`execute_weibo_function_call`**（**`get_weibo_hot`**）；**GET** **`weibo.com/ajax/side/hotSearch`**（**`httpx`**），**`Cookie`** = **`config.WEIBO_COOKIE`**（**`.env` `WEIBO_COOKIE`**）；成功结果进程内缓存；**`role=tool`** 为 **`{"summary":…}`** JSON 字符串
- `tools/search.py`：**`execute_search_function_call`**（**`web_search`**）；**POST** **`https://api.tavily.com/search`**（**`httpx`** 异步），**`api_key`** = **`config.TAVILY_API_KEY`**；多条结果经 **`search_summary`**（或回退 **`summary`**）激活 **`api_configs`** 行驱动 **`LLMInterface`** 压缩；**`role=tool`** 为 **`{"summary":…}`** JSON 字符串
- `tools/location.py`：位置工具（仅有版本号占位，无实现）

占位项在根目录 **`README.md`** 中多已标注为「规划中」；`meme.py`、`lutopia.py`、`weather.py`、`weibo.py`、**`search.py`**、`prompts.py` 为已实现模块。

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
cron（或同类）在运维约定时刻触发 —— 应与 `config.daily_batch_hour`（东八区半小时粒度，默认 23.0）一致
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
  │  Step 3.5：daily 正文 → JSON（new/deact/adj）→ 写库      │
  │    step3=1 且 step4=0；LLM/整段解析失败 raise→最多3重试   │
  │    全败 Telegram；三支单条失败 WARNING；不阻 Step4      │
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

#### 4.2.1 全量备份 `backup.sh`（可选，与日终跑批独立）

**职责（以项目根 `backup.sh` 为准）：** 将 **PostgreSQL**（`pg_dump -F c`）、**Chroma 数据目录** `chroma_db/`、**环境文件** `.env` 打成单份归档，上传对象存储，并清理临时文件与过期本地归档。

**流程概要：**

1. 从项目根 `.env` 解析 **`DATABASE_URL`**（失败则退出）。
2. **`pg_dump -F c`** 导出到 **`/tmp/cedarstar_db.dump`**。
3. **`tar -czf`** 生成 **`/home/backups/cedarstar/cedarstar_backup_YYYYMMDD.tar.gz`**（目录不存在则创建），内含上一步 dump、`chroma_db/`、`.env`。
4. **`rclone copy`** 将该 `.tar.gz` 推送到远程 **`cloudflare_r2:cedarstar-backup`**（远程名与 bucket 以脚本内常量为准；须本机已配置 `rclone` 且 R2 侧 bucket/权限就绪）。
5. 删除 **`/tmp/cedarstar_db.dump`**。
6. **`find`** 删除 **`/home/backups/cedarstar/`** 下超过 **7** 天的 **`cedarstar_backup_*.tar.gz`**。

任一步失败 **`exit 1`**，不继续后续步骤；各步 **`echo`** 带时间戳日志。**cron** 示例（东八区凌晨 5:00、日志追加）见脚本末尾注释；典型写法：`0 5 * * * TZ=Asia/Shanghai /opt/cedarstar/backup.sh >> /var/log/cedarstar_backup.log 2>&1`（项目路径按部署机调整）。

**说明：** 仓库内 **`backups/`** 目录为历史 SQLite 等文件用途（见 §2 目录树）；**生产全量 tarball** 默认落在 **`/home/backups/cedarstar/`**，与前者路径不同。

### 4.3 Mini App 数据流

**CORS（`main.py`）：** 允许的来源与正则以源码中的 **`_CORS_ALLOW_ORIGINS`**、**`_CORS_PAGES_DEV_REGEX`** 为准；部署新前端域名或 Tunnel 时请在 `main.py` 中按需修改上述常量。

**静态 Mini App（`main.py`，以代码为准）：** 在全部 **`include_router`**（**`/api/*`**、**`/webhook/telegram`**）及 **`/`**、**`/health`** 之后注册 **`GET /app/{full_path:path}`** → **`serve_miniapp`**：若 **`MINIAPP_DIST / full_path`**（**`MINIAPP_DIST = Path("miniapp/dist")`**，相对进程工作目录）为**文件**则 **`FileResponse`**；否则若存在 **`MINIAPP_DIST / "index.html"`** 则返回该文件（前端路由 SPA 回退）；否则 **404**。不再使用 **`StaticFiles`** 挂载。

**`/api/*` 鉴权：** 浏览器或前端发往 **`/api/...`** 的请求须带请求头 **`X-Cedarstar-Token: <MINIAPP_TOKEN>`**（与 §3.1、§3.6 一致）。**`POST /webhook/telegram`** **不**要求该头。

**Telegram 服务器 → 后端：** **`POST /webhook/telegram`**（**非** `/api`）直达 `api/webhook.py`，与下述 Mini App 的 `/api/*` 分流并列，**不**经过 `api/router.py` 的同一前缀树（实现上以 `main.py` 分别 `include_router`）。

```
浏览器（React Mini App）
        │  HTTP GET/POST/PUT/DELETE /api/...  +  X-Cedarstar-Token（须与 MINIAPP_TOKEN 一致）
        ▼
  main.py（FastAPI + CORS）
        │
        ├──  （可选）GET /app/... ──► serve_miniapp ──► miniapp/dist（实文件或 index.html 回退）
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
        ├──► api/history.py   ──► memory/database.py（`messages` 列表查询 + 单条 UPDATE/DELETE）
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

**应用层（以代码为准）：** 日终 Step 3.5 可调 **`MessageDatabase.update_temporal_state_expire_at`**（**`UPDATE … SET expire_at`**，配合 **`adjust_expire`**）；停用仍走 **`deactivate_temporal_states_by_ids`**。

---

### 5.5 `relationship_timeline` — 关系时间线表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PK | 主键（字符串 ID） |
| `created_at` | DATETIME | 排序/展示用时间；**日终 Step 3** 写入时由应用层设为 **`batch_date` 当日 23:59:59**（naive，与业务日对齐）。**`insert_relationship_timeline_event`** 若**不传** `created_at` 则列默认 **`NOW()`**（插入时刻） |
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

> 此表是 **手动新增长期记忆**（`doc_id` 以 `manual_` 开头）的 PostgreSQL 镜像；**列表与筛选以 Chroma 为准**（`GET /longterm` 直接分页读向量库）。历史遗留行可能出现 Chroma 已删而表行仍在的情况，以实际接口返回为准。

**API 说明（以代码为准）：**
- **`GET /api/memory/longterm`**：数据源为 **ChromaDB**（分页 `page` / `page_size`，可选 **`summary_type`** 等于 **`metadata.summary_type`**）。`data` 含 `items`、`total`、`page`、`page_size`；每条含 `chroma_doc_id`、`content`、**`summary_type`**（Chroma 元数据，如 `daily` / `daily_event` / `manual` / `state_archive`）、`date`、`hits`、`halflife_days`、`arousal`、`last_access_ts`、`base_score`、`is_manual` 等。**微批 `summaries.summary_type=chunk` 不写入 Chroma**，故 Mini App 不提供 `chunk` 类型筛选。
- **`POST /api/memory/longterm`**：先 Chroma 后 **`longterm_memories`**；可配 `score`、`halflife_days`（写入 Chroma metadata）。
- **`DELETE` / `PATCH`**：见文首更新条与 §6.7。

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
- `daily_batch_hour`：东八区日终跑批**目标时刻**，半小时粒度浮点值 `0.0–23.5`（默认 `23.0`）；供运维配置 **cron** 与业务文档对齐。**`schedule_daily_batch`** 若在进程内运行会在每次睡眠前读库刷新该值；**`run_daily_batch.py` / cron 路径不依赖进程内定时循环**，跑批本身仍读 `config` 表其它键（如 GC 阈值）
- `relationship_timeline_limit`：关系时间线注入条数（默认 3）
- `gc_stale_days`：Step 5 Chroma GC 闲置天数阈值（默认 180）
- `gc_exempt_hits_threshold`：Step 5 GC hits 豁免阈值（默认 10）；`hits` 达到此值的记忆无论衰减分多低都不会被物理删除
- `retrieval_top_k`：向量与 BM25 各路召回候选数（默认 5）
- `telegram_max_chars`：Telegram 正文分段提示词中的 **MAX_CHARS**（默认 50；`api/config.py` 校验 10–1000 且对齐步长 10）；`context_builder.format_telegram_reply_segment_hint()` 读库注入 system；**发送侧** `reply_citations.parse_telegram_segments_with_memes_async` / `telegram_max_chars_from_config()` 读同一键，**仅当助手正文不含 `|||`** 时用于二级 **`_split_oversized_chunk`** 单段超长切分（与提示词一致；**含 `|||`** 时该键不生效）
- `telegram_max_msg`：提示词中的 **MAX_MSG**（默认 8；校验 1–20）；**发送侧** `reply_citations.parse_telegram_segments_with_memes_async` / `telegram_max_msg_from_config()` 读同一键，**仅当正文不含 `|||`** 时用于 **`_enforce_max_msg_segments`** 条数封顶（与提示词一致；**含 `|||`** 时不合并）

**API 响应元数据：** `GET` / `PUT` `/api/config/config` 成功时，返回体中的 `data` 除上述键外另含 `_meta: { updated_at: string | null }`，值为 **`DEFAULT_CONFIG` 所含全部键**（含 `telegram_*`）在 `config` 表中的 `MAX(updated_at)`（ISO 8601 字符串，前端解析时需注意这是 UTC 时间，需转为本地时区），用于 Mini App「上次保存时间」；`_meta` 不是配置项，不参与 `PUT` 写回。实现：`memory/database.py` 的 `get_config_max_updated_at_for_keys`、`api/config.py` 的 `_payload_with_meta`。

---

### 5.8 `persona_configs` — 人设配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `name` | TEXT | 人设名称 |
| `char_name` | TEXT | 角色姓名（锚点句「你的名字是 …」） |
| `char_identity` | TEXT | 存在定义正文（迁移默认 `''`） |
| `char_personality` | TEXT | 角色性格 / 内在人格 |
| `char_speech_style` | TEXT | 说话风格与格式（表达契约） |
| `char_redlines` | TEXT | 行为红线（表达契约；迁移默认 `''`） |
| `char_appearance` | TEXT | 外在形象（并入【存在定义】输出，UI 可在「关系与形象」组编辑） |
| `char_relationships` | TEXT | 机际关系（【关系与形象】块仅输出本列） |
| `char_nsfw` | TEXT | Char 侧成人内容（迁移默认 `''`） |
| `char_tools_guide` | TEXT | 工具使用守则（迁移默认 `''`） |
| `char_offline_mode` | TEXT | 线下模式（迁移默认 `''`） |
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
| `enable_lutopia` | INTEGER | 是否启用 Lutopia 论坛工具（0/1；迁移默认 0） |
| `enable_weather_tool` | INTEGER | 是否启用天气工具 **`get_weather`**（0/1；迁移默认 0） |
| `enable_weibo_tool` | INTEGER | 是否启用微博热搜工具 **`get_weibo_hot`**（0/1；迁移默认 0） |
| `enable_search_tool` | INTEGER | 是否启用网页搜索工具 **`web_search`**（0/1；迁移默认 0） |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

**✅ 已改动（2026-04-05）：** 表新增列 **`user_work`**（`migrate_database_schema` 中 `ALTER TABLE … ADD COLUMN IF NOT EXISTS user_work …`）。**`api/persona.py`**、**`Persona.jsx`**、**`context_builder`** 用户块 **「工作：…」** 已同步。

**✅ 已改动（2026-04，以代码为准）：** `migrate_database_schema` 追加 **`char_identity`、`char_redlines`、`char_nsfw`、`char_tools_guide`、`char_offline_mode`**（均为 **`TEXT DEFAULT ''`**）。**`save_persona_config` / `update_persona_config`** 白名单含上述列及 **`enable_lutopia`**。**`api/persona.py`** 的 **`PersonaCreate` / `PersonaUpdate` / `PersonaResponse`** 与之一致；Char 拼接逻辑见 **`memory/context_builder.py`** 中 **`build_char_persona_prompt_sections`** / **`build_persona_config_system_body`**。

**✅ 已改动（2026-04-20，以代码为准）：** **`migrate_database_schema`** 追加 **`persona_configs.enable_weibo_tool`**（**`INTEGER DEFAULT 0`**）；与 **`enable_weather_tool`** 一并由 **`save_persona_config` / `update_persona_config`** 白名单读写；**`api/persona`** 与 Mini App **Persona** 表单字段一致。

**✅ 已改动（2026-04-20，以代码为准）：** **`migrate_database_schema`** 追加 **`persona_configs.enable_search_tool`**（**`INTEGER DEFAULT 0`**）；**`_ensure_default_search_summary_api_config_row`** 在无 **`config_type=search_summary`** 行时插入占位 **`api_configs`**（名称「搜索摘要模型」，**`is_active=0`**，其余字段空）；**`save_persona_config` / `update_persona_config`** 白名单含 **`enable_search_tool`**；**`api/persona`** 与 Mini App **Persona**「启用搜索工具」一致。

**✅ 已修复（2026-04-07）：** 补全 `persona_configs` 数据库 CRUD 的字段白名单（含 `char_appearance`、`char_relationships` 等），避免 Mini App 保存失败。

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
| `config_type` | TEXT | 配置类型（`chat` / `summary` / `vision` / `stt` / `embedding` / **`search_summary`**） |
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

**保留策略（以代码为准）：** 日终 **`run_daily_batch`** 每次执行时在五步前调用 **`purge_logs_older_than_days(7)`**，物理删除早于 **7 天** 的行，避免表无限增长；与 `async_log_handler` 写入并存。

**Mini App 查询（以代码为准）：** **`GET /api/logs`** 可选 **`time_from` / `time_to`**（含边界），对应 SQL **`created_at >= $…`** / **`created_at <= $…`**；绑定值为 **naive UTC**（见 **`get_logs_filtered`** / **`_pg_timestamp_naive_utc`**），与列类型 **`TIMESTAMP`** 一致。

---

### 5.11 `token_usage` — Token 使用量表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `created_at` | TIMESTAMP | 记录时间 |
| `platform` | TEXT | 调用来源：`config.Platform` 常量，常见值 `discord` / `telegram` / `batch`（日终与微批摘要等）；可为空。**Settings 页 Token 进度条按此字段分平台动态展示，新增平台无需改前端代码（见 §3.6 Settings 页）** |
| `prompt_tokens` | INTEGER | 输入 Token 数 |
| `completion_tokens` | INTEGER | 输出 Token 数 |
| `total_tokens` | INTEGER | 总 Token 数 |
| `model` | TEXT | 使用的模型名称 |
| `cached_tokens` | INTEGER | OpenRouter / OpenAI / GLM 等在 `usage.prompt_tokens_details.cached_tokens` 中报告的缓存读取 Token 数 |
| `cache_write_tokens` | INTEGER | OpenRouter 等在 `usage.prompt_tokens_details.cache_write_tokens` 中报告的缓存写入 Token 数 |
| `cache_hit_tokens` | INTEGER | DeepSeek 官方 `usage.prompt_cache_hit_tokens` |
| `cache_miss_tokens` | INTEGER | DeepSeek 官方 `usage.prompt_cache_miss_tokens` |
| `cache_creation_input_tokens` | INTEGER | Anthropic Messages API 缓存写入 Token（含 `cache_creation` 明细归一化） |
| `cache_read_input_tokens` | INTEGER | Anthropic Messages API 缓存读取 Token |
| `raw_usage_json` | JSONB | 上游原始 `usage`，保留供应商特有字段供排查与 Mini App 细节展示 |

**索引：** `(created_at)`、`(platform, created_at)`、`(model, created_at)`。

**多模型缓存语义：** Claude / Anthropic-compatible 请求层使用显式 `cache_control` breakpoints；DeepSeek / GLM 依赖供应商自动缓存，不注入 Anthropic 专用字段。落库层不按供应商拆表，而是把各家 usage 字段归一到上述列；Mini App 可同时展示通用 token 与各家缓存命中信息。

---

### 5.12 `tool_executions` — 工具执行记录表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | SERIAL PK | 自增主键 |
| `session_id` | TEXT NOT NULL | 会话 ID |
| `turn_id` | TEXT NOT NULL | 同一轮工具链路 ID；一轮内多个工具共享同一 `turn_id` |
| `seq` | INTEGER NOT NULL | 同一 `turn_id` 内的调用顺序，从 0 递增 |
| `tool_name` | TEXT NOT NULL | 工具名，如 `lutopia_cli` / `get_weather` / `web_search` |
| `arguments_json` | TEXT | 工具参数 JSON 字符串 |
| `result_summary` | TEXT | 给模型和后续摘要使用的短摘要 |
| `result_raw` | TEXT | 原始结果，最多保留约 50000 字符，供排查与重摘要 |
| `user_message_id` | INTEGER | 触发本轮的用户 `messages.id`（尽力关联） |
| `assistant_message_id` | INTEGER | 助手消息 `messages.id`（当前可为空，预留回填） |
| `platform` | TEXT | 来源平台 |
| `created_at` | TIMESTAMP | 写入时间 |

**索引：** `(session_id, created_at DESC)`、`(turn_id, seq)`、`(user_message_id)`。

**语义：** 每次工具调用写一行，而不是一轮写一条 JSON 大包；同一轮多个工具靠 `turn_id + seq` 保持顺序。`context_builder` 只注入 `result_summary`，`micro_batch` 生成 chunk 时读取同一消息范围内的工具摘要，避免模型忘记自己查过什么；`result_raw` 不直接进入常规 Context，避免长帖/网页吞掉 Prompt Cache 节省的 token。

---

### 5.13 `daily_batch_log` — 日终跑批日志表

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

### 5.14 `sticker_cache` — Telegram 贴纸描述缓存表

| 字段 | 类型 | 说明 |
|------|------|------|
| `file_unique_id` | TEXT PK | Telegram `Sticker.file_unique_id`（全局稳定指纹） |
| `emoji` | TEXT | 贴纸关联 emoji（可为空） |
| `sticker_set_name` | TEXT | 所属套装名 `set_name`（可为空） |
| `description` | TEXT | 视觉模型生成的短描述；失败时为 `（贴纸）` |
| `created_at` | DATETIME | 写入时间，默认 `CURRENT_TIMESTAMP` |

**建表：** `migrate_database_schema` 内 `_ensure_sticker_cache_table` 执行 `CREATE TABLE IF NOT EXISTS`，已存在则跳过。**访问：** `MessageDatabase.get_sticker_cache` / `save_sticker_cache` / `delete_sticker_cache`（均为 **`async`**，调用方 **`await`**）及模块便捷函数 `get_sticker_cache_row` / `save_sticker_cache_row` / `delete_sticker_cache_row`。

---

### 5.15 `meme_pack` — 表情包元数据表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | SERIAL PRIMARY KEY | 自增主键；Chroma metadata 中以 `sqlite_id` 字段引用（历史兼容名） |
| `name` | TEXT NOT NULL | 短名称（导入清单中的展示名）；**允许重复**（不同 URL 可同名） |
| `description` | TEXT | 可选；视觉模型等生成的长描述，用于向量嵌入与 Chroma `metadata.description`；空则重同步时回退仅用 `name` 嵌入 |
| `url` | TEXT NOT NULL | 图片或动图 URL；**业务上唯一**（唯一索引见下） |
| `is_animated` | INTEGER NOT NULL DEFAULT 0 | `1` 表示动图（发送侧用 `send_animation`），`0` 为静图（`send_photo`） |

**建表与迁移：** 与多张核心表一同在 `create_tables` 中 `CREATE TABLE IF NOT EXISTS`；已有库由 `migrate_database_schema` 幂等执行：`ALTER TABLE meme_pack ADD COLUMN IF NOT EXISTS description TEXT`；**`DROP INDEX IF EXISTS idx_meme_pack_name_unique`**（若曾存在）；**按 `url` 去重**（`DELETE ... USING`，同一 url 仅保留 **最小 `id`**）；**`CREATE UNIQUE INDEX IF NOT EXISTS idx_meme_pack_url_unique ON meme_pack (url)`**。

**访问：** **`fetch_meme_pack_by_url(url)`** → 单行 `id/name/description/url/is_animated` 或 `None`。**`insert_meme_pack(name, url, is_animated, description=...)`**：**`INSERT ... ON CONFLICT (url) DO UPDATE`**（更新 `name`、`description`、`is_animated`），返回该行 **`id`**（新建或已存在）。

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
3. 构建 Prompt，要求 SUMMARY LLM 按 7 个维度返回严格 JSON（`content` 或 `null`）；含 **7 维既有卡 `old_cards_block`** 与**禁止跨维度重复同一事实**、**增量对比**句（见 §3.4.4）
4. **解析 JSON：** 整段 `json.loads` → 失败则截取首个**平衡** `{...}`（含 \`\`\`json 块）→ 再回退贪婪正则；仍失败则 Step 3 报错退出
5. **Upsert：** `get_latest_memory_card_for_dimension`（**含 `is_active=0`**）；有旧卡时：**`_merge_memory_card_contents`**（维度三分支；`current_status`/`preferences` 为 **`merged`/`discarded`** JSON）；若 **`discarded` 非 null** 则 **`_rewrite_discarded_state_for_archive`**（batch guard ≤3 次）后 **`add_memory`**（`state_archive`）并可增量 BM25 → `update_memory_card(..., reactivate=True)`；无则 `INSERT`；合并 LLM 失败时 fallback 为追加式拼接且不写归档
6. 单维度 `try/except + continue`，互不拖累
7. 维度分析仍走 `summary_llm.generate_summary`（chunk 式前缀 + 任务正文，并传入 `char_name`/`user_name`）；**合并**走 **`_call_summary_llm_custom`**（**不经** chunk 外壳；含人物前缀与维度三分支合并规则，见 §3.4.4）

---

### 6.4 ✅ 已修复 / 已演进：`daily_batch.py` Step 4 小传打分

**问题（历史）：** 小传归档前价值打分路径曾把 `self.llm.generate(prompt)` 的返回值（`LLMResponse`）误当作字符串做正则，应先取 `.content`。

**修复与后续：** 已改为先使用 `score_text = score_response.content` 再匹配；当前实现为 **`_step4_archive_daily_and_events`** 中 `score_text, _thinking = self.llm.generate_with_context_and_tracking([{"role":"user","content":prompt}], platform=Platform.BATCH)`（返回 `(str, Optional[str])`，打分仅用正文），并异步写入 `token_usage`。**演进（2026-04）：** 打分用 **user `prompt`** 在任务正文前加 **`_persona_dialogue_prefix()`**（激活人设 `char_name`/`user_name`，见 §3.4.4）。

**演进（2026-04-09）：** 打分 JSON 解析改为 **`coerce_score_and_arousal_defaults`**（单次 LLM 调用，解析失败则 **score=5、arousal=0.1**，不走 Guard 文本重试链）。

---

### 6.5 ✅ 已修复：`api/history.py` / `api/logs.py` 全量加载后内存过滤

**问题：** `get_history()` 接口调用 `db.get_all_messages()` 获取所有消息后在 Python 内存中过滤和分页，当消息量大时性能极差。`api/logs.py` 存在同样问题。

**修复（2026-03-21）：** 在 `memory/database.py` 中新增两个方法，将过滤与分页逻辑完全下推到 SQL 层：

- `get_messages_filtered(platform, keyword, date_from, date_to, page, page_size)`：对 `messages` 表使用 `WHERE` 条件过滤（platform 精确匹配；keyword 非空时对 **`COALESCE(content,'')` 与 `COALESCE(thinking,'')`** 以同一 `%keyword%` 模式做 **`ILIKE`**；date_from/date_to 用 `created_at::date` 比较），`COUNT(*)` 获取总条数，`LIMIT/OFFSET` 分页，同时返回 `{total, messages}`。
- `get_logs_filtered(platform, level, keyword, time_from, time_to, page, page_size)`：对 `logs` 表同理，level 自动转大写后精确匹配，keyword 对 message/stack_trace 做 `LIKE`；**`time_from` / `time_to`** 非空时对 **`created_at`** 做范围比较。时间参数在绑定前经 **`_pg_timestamp_naive_utc`**，保证传入 PostgreSQL **`TIMESTAMP`** 的值为 **naive UTC**（避免 asyncpg **aware/naive** 混编错误）。

`api/history.py` 和 `api/logs.py` 改为直接调用上述新方法，删除了原有的全量加载、Python 内存过滤、手动排序和切片逻辑。过滤条件为空时不拼接对应 `WHERE` 子句。前端接口格式（`total / page / page_size / messages|logs`）保持不变。

**✅ 已修复（2026-04-05）：** `get_messages_filtered` 内将 **`date_from` / `date_to`** 从字符串 **`date.fromisoformat`** 转为 **`datetime.date`** 再绑定 SQL（见 §3.4.1），修复带日期筛选时 History 接口 **500**。

**✅ 已补充（2026-04-14）：** **`GET /api/logs`** 的 **`time_from` / `time_to`** 由 **`api/logs.py`** 以 **字符串** 接收并 **`_parse_log_time_param`** 解析，与 **`get_logs_filtered`** 上述 naive UTC 约定一致。

---

### 6.6 ✅ 已修复：`BM25Retriever` 初始化时索引为空

**问题：** `BM25Retriever._build_index()` 在初始化时将索引设为空列表，需要手动调用 `refresh_index()` 才能从 ChromaDB 加载数据。但 `refresh_index()` 只在日终归档时被调用，导致服务重启后 BM25 索引始终为空，直到下次日终跑批。

**修复（2026-03-21）：** 重写 `_build_index()`，在服务启动时直接从 ChromaDB 拉取全量文档并建立索引。ChromaDB 为空或连接失败时优雅降级为空索引，不抛异常、不阻断服务启动。

---

### 6.7 ✅ 已演进：`longterm_memories` 与 Chroma、Mini App 长期记忆列表

**原问题（2026-03-21）：** 手工长期记忆若先写数据库再写 Chroma，会产生无向量关联行。

**创建（仍以代码为准）：** **`POST /longterm`** 先 **`vector_store.add_memory()`**（`doc_id`=`manual_{uuid}`），成功后再 **`create_longterm_memory(..., chroma_doc_id=...)`**；Chroma 失败则不写库；库写失败则尝试 **`delete_memory`** 回滚 Chroma。

**列表查询（2026-04 演进）：** **`GET /longterm`** 以 **ChromaDB 全量分页** 为数据源，**不再**依赖 `get_longterm_memories` 表扫描 + `is_orphan` 合并逻辑；镜像表主要用于 **手动条目的删除同步** 与 Dashboard 等辅助统计。

**删除（2026-04 演进）：** **`DELETE /longterm/{chroma_doc_id}`** 仅 **`manual_` 前缀**：先删 Chroma，再 **`delete_longterm_memory_by_chroma_id`**；日终归档类向量不允许在此接口删除。

**元数据：** **`PATCH /longterm/{chroma_doc_id}/metadata`** 仅更新 Chroma 中 **`halflife_days` / `arousal`**（读旧 metadata 合并后写回）。

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

**问题：** `python-telegram-bot` 经 httpx 发请求时，httpx（及底层 httpcore）在 **INFO** 会记录整行 `HTTP Request: POST https://api.telegram.org/bot<token>/...`，**`{APP_NAME}.log`**（默认 **`cedarstar.log`**）与控制台长期留存 Bot Token，属安全隐患且非单纯噪音。

**修复（2026-03-22）：** `main.py` 的 `setup_logging()` 在 `discord` / `telegram` / `urllib3` / `requests` 之外，为 **`httpx`** 与 **`httpcore`** 注册 `logging.Filter`：若消息含 `://api.telegram.org` 且级别低于 **WARNING**，则丢弃该条；WARNING 及以上仍输出，便于排查连接或 API 错误。历史日志若已含 token，需轮转或删除文件并视情况在 BotFather 轮换 token。

---

### 6.14 ✅ 演进：Telegram 独立代理 `TELEGRAM_PROXY` 与 PTB `[socks]`

**背景：** Discord 启动会向进程环境写入 `HTTP_PROXY`/`HTTPS_PROXY`。`python-telegram-bot` 使用的 httpx 默认 `trust_env=True`，会把 Bot API 请求也走同一 HTTP 代理，对 `api.telegram.org` 常出现 `ConnectError`（经代理 `start_tls` 失败）。关闭 `trust_env` 后直连，在国内等环境又易 `Timed out`。

**演进（2026-03-28）：** `config.TELEGRAM_PROXY`（`.env`）仅用于 Telegram：`HTTPXRequest(proxy=..., httpx_kwargs={"trust_env": False})`，与 LLM 的 `requests`、Discord 代理解耦。推荐 **SOCKS5** URL（与 Clash 混合端口或 SOCKS 端口一致）；`requirements.txt` 使用 `python-telegram-bot[socks]`、`httpx[socks]`。详见 §3.1 配置表、§3.2「Telegram Bot 特有」。

---

### 6.15 ✅ 已改动：运行日志 logrotate（宿主机）

**说明（2026-04-05）：** 在部署机新增 **`/etc/logrotate.d/cedarstar`**，对项目根目录日志文件（默认实例为 **`cedarstar.log`**；多实例时为 **`{APP_NAME}.log`**）做 **`daily`** 轮转、**`rotate 7`**（保留 7 份）、**`compress`**、**`missingok`**、**`notifempty`**、**`copytruncate`**（与常见 Python 单文件日志进程配合，避免移动文件后进程仍写旧 inode）。具体路径与策略以机上该文件为准；多实例须为各 **`APP_NAME`** 各写一条或通配。

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

**说明：** 长期记忆列表以 **Chroma** 为准（见 §5.6、§6.7）；若需提示表侧孤儿行，属历史数据问题，非当前 `GET /longterm` 主路径。

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
| Observability（调用观测） | `Observability.jsx` | ✅ 无 | 展示 token/cache 与工具执行 |

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

**说明（2026-03-21 任务 7，演进以代码为准）：** 页面分为四个 Tab：记忆卡片、长期记忆、时效状态（`temporal_states` 列表/新增/软删除）、关系时间线（`relationship_timeline` 只读倒序）。**长期记忆** Tab：`GET /api/memory/longterm` 拉取 **Chroma** 分页数据；类型筛选为 **`metadata.summary_type`**（**`daily` / `daily_event` / `manual` / `state_archive`**，**无 `chunk`**）；条目展示 `hits`、`halflife_days`、`arousal`、`base_score` 等；**`manual_` 文档可删**；**PATCH 元数据**编辑半衰期/情绪强度；顶栏与正文/「查看全文」/2×2 参数格布局见 `memory.css`。

**时效状态 Tab UI：** 列表状态由 `getTemporalDisplayStatus` 根据 `is_active` 与 `expire_at` 推导（`生效中` / `已过期`）。**「软删除」（停用）按钮仅对「生效中」展示**；`expire_at` 已到期但日终跑批尚未把该行 `is_active` 置 0 时，界面显示「已过期」且**不**出现软删除，与 §6.2 Step 1 到期结算语义一致，避免对已到期记录重复操作。

**布局与 Tab 切页：** 固定 `.memory-container` 高度 + `.memory-content-scroll-area` 内滚动、统一 `.memory-tab-header` / `h2` 页头与顶距，已去除各 Tab 外层区块不一致的 `margin-top`（原 `longterm-section` / `temporal-section` / `timeline-section`），避免切 Tab 时标题上下跳动；详见 §3.6 Memory 页说明。

**对应接口：** `GET /api/memory/cards`、`GET/POST/DELETE /api/memory/*`（见 §7.4 表）

---

### 7.4 ✅ 其余页面：无 Mock 数据

以下页面均完整调用了对应的后端 API，无硬编码 mock 数据（Memory 页见 §7.3 为可选增强而非 Mock）：

| 页面 | 调用的 API 接口 |
|------|---------------|
| **Memory.jsx** | `GET /api/memory/cards`、`GET /api/memory/longterm`（分页 + 可选 `summary_type`）、`POST /api/memory/cards`、`PUT /api/memory/cards/{id}`、`DELETE /api/memory/cards/{id}`、`POST /api/memory/longterm`、`DELETE /api/memory/longterm/{chroma_doc_id}`（仅 `manual_`）、`PATCH /api/memory/longterm/{chroma_doc_id}/metadata`、`GET/POST /api/memory/temporal-states`、`DELETE /api/memory/temporal-states/{id}`、`GET /api/memory/relationship-timeline` |
| **History.jsx** | `GET /api/history`（platform / keyword / date_from / date_to / page / page_size）；`PATCH /api/history/{id}`（`content` / `thinking` 可选但至少其一）；`DELETE /api/history/{id}` |
| **Logs.jsx** | `GET /api/logs`（`platform` / `level` / `keyword` / 可选 `time_from`·`time_to` / `page` / `page_size`） |
| **Persona.jsx** | `GET /api/persona`、`GET /api/persona/{id}`、`GET /api/persona/{id}/preview`、`POST /api/persona`、`PUT /api/persona/{id}`、`DELETE /api/persona/{id}` |
| **Settings.jsx** | `GET /api/settings/api-configs?config_type=chat|summary|vision|stt|embedding`（按 Tab 过滤）、`POST` / `PUT` / `DELETE` / `PUT .../activate`、`POST .../fetch-models`、`GET /api/settings/token-usage`、`GET /api/persona`；保存配置后按返回表单中的 `config_type` 切换 Tab 或刷新当前列表（见 §3.6 Settings 页说明） |
| **Observability.jsx** | `GET /api/observability/usage?period=today|week|month`（token/cache 聚合、按平台/模型/日期与最近调用）、`GET /api/observability/tool-executions?limit=...`（最近工具执行，raw 为截断预览） |
| **Config.jsx** | `GET /api/config/config`、`PUT /api/config/config`（`data` 含 `_meta.updated_at`；失败时顶部错误提示 + 重试，见 §5.7、§7.2） |

---

### 7.5 ✅ 已修复：`router.jsx` 显式导入 React

**文件：** `miniapp/src/router.jsx`（路由与 `navItems` / `routes` 配置，非页面组件）

**说明：** 该文件内使用 JSX（如 `<Dashboard />`），此前未导入 React。已在顶部补充 `import React from 'react'`，与 §6.11 一致；构建侧仍可配合 Vite 的 React 插件使用。

---

### 7.6 ✅ 已改动（2026-04-07）：WebView 删除失效、时区解析及内建 Prompt 深度调优

**文件：** `miniapp/src/pages/Settings.jsx`、`miniapp/src/styles/settings.css`、`miniapp/src/pages/Config.jsx`、`memory/daily_batch.py`、`memory/micro_batch.py`

**说明：**
1. **WebView 兼容：** 将 API 密钥配置页底层的 `window.confirm` 删除逻辑改为**行内内联状态确认机制**（`confirmDeleteId`），修复了因 Telegram WebView 环境禁用原生 `confirm` 弹窗导致的删除按钮点击毫无响应的问题。
2. **时区显示修复：** 修正了 Config 页面由于将本地数据库时间粗暴加上 `Z` 后因本地浏览器默认东八区而再次附加 8 小时偏移的 Bug。
3. **内建 Prompt 全面升级：** `daily_batch` 及 `micro_batch` 记忆压缩逻辑深度重写：
   - 强化了记忆、事件提取中的**第一人称代入约束**与 **JSON 输出隔离约束**（严禁 Markdown 代码块及冗余文字）。
   - 事件时间拆分与关系时间轴逻辑修改为：严格替换代词为角色/用户本名、限制长度在特定字数范围、**严禁相对时间表述（今天/昨天）** 进而避免独立检索时带来的明显指代歧义和时序错乱。

---

### 7.7 ✅ 已修复与优化（2026-04-07）：系统日志异步入库重构与 Token 实时单机展示

**文件：** `memory/async_log_handler.py`、`main.py`、`api/settings.py`、`memory/database.py`、`miniapp/src/pages/Settings.jsx`、`miniapp/src/styles/settings.css`

**说明：**
1. **系统日志彻底修复（重构 `async_log_handler.py`）：**
   - 彻底移除了原 `AsyncDatabaseLogHandler` 中基于 `threading.Thread` 的错误实现（跨事件循环或无 loop 时调用 `asyncpg.Pool` 会引发严重报错，且原来的实现遗漏了 `await`）。
   - 重新架构：采用无锁的全局内存缓冲队列（`_log_buffer`），配合在主事件循环启动的一枚轻量常驻协程 `log_flusher_task`（1.5s 定时）。成功解决后台报错黑洞，恢复了 MiniApp 中对于运行时内部信息的捕获和显示。
2. **单一请求 Token 消耗展示功能：**
   - 在前端 Settings 页面统计区默认提供「**本次**」独立查询项，取代原先纯全天汇聚模式。
   - 数据标签进一步本土化（“Prompt tokens” -> “输入消耗”，“Completion tokens” -> “生成消耗”）且样式已针对全 Flex 均匀居中。
   - 底层于 `memory/database.py` 实装 `get_latest_token_usage_stats` (`ORDER BY created_at DESC LIMIT 1`)。
   
### 7.8 ✅ 已修复与优化（2026-04-07）：Memory Tab 视觉统一与对话历史思维链回显修复

**文件：** `miniapp/src/styles/memory.css`、`memory/database.py`、`bot/telegram_bot.py`、`bot/discord_bot.py`

**说明：**
1. **Memory 页面容器质感提升：**
   - 原先包裹 4 个顶部 Tab 的大框在移动端和桌面端不够整体，通过重新定义 `.memory-tabs`，给该容器添加上了标准的新拟态圆润倒角（`var(--radius-card)`）和专属的内凹跑道光影（`box-shadow: var(--shadow-inset)`）。
   - 去除了曾用于适配小屏幕但破坏了整块 UI 圆角的移动端 padding / overflow 重置。现在长得就像放进了一个高级凹槽内。
2. **对话历史页（时光机）与助手落库、思维链（`thinking`）：**
   - 前端 History 已拉取并展示 `thinking`（见气泡内「展开思维链」）；若库中 `assistant` 行缺失或 `thinking` 为 `NULL`，多为后端未成功写入。
   - **`MessageDatabase.save_message` 的 `INSERT` 始终包含 `thinking` 列**；此前一类故障来自**模块便捷函数** `memory.database.save_message` 在扩展参数时**未声明、未转发 `thinking`**，而 `bot/telegram_bot.py` / `bot/discord_bot.py` 已传入 `thinking=`，运行时报 **`TypeError: unexpected keyword argument 'thinking'`**。Telegram 缓冲路径下用户行已写入、助手行在 flush 阶段失败时，异常常被 **`MessageBuffer._process_buffer` 外层 `except` 吞掉**，表现为 **Telegram 有回复、DB 无 assistant、时光机只剩用户消息**。
   - **2026-04-08 修复：** 模块便捷 `save_message` 与类方法对齐（含 **`thinking`** 转发）。Telegram **`_flush_buffered_messages`** 另：**`persist_assistant` 时无论是否拿到首条正文 Telegram `message_id` 均落库助手**；无平台 id 时 **`message_id` 用 `ai_{用户消息 id}`**，并与纯文本兜底发送分支配合。

---

*文档由代码自动分析生成，如有遗漏请以实际代码为准。*
