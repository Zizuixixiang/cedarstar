"""
LLM 工具相关的 system prompt 片段。

按「工具包」注册（如 ``lutopia``），与 ``OPENAI_*_TOOLS`` 的启用列表对齐；
新增工具时在此增加常量并在 ``TOOL_DIRECTIVES`` 中登记。
"""

from __future__ import annotations

from typing import Any, Dict, List

from tools.rcommunity import OPENAI_RCOMMUNITY_TOOLS

# ---------------------------------------------------------------------------
# 各工具包说明（供 build_tool_system_suffix 拼接）
# ---------------------------------------------------------------------------

LUTOPIA_TOOL_DIRECTIVE = (
    "【Lutopia】涉及论坛、群聊摘要、Wiki、私信等须通过工具取真实数据，勿编造；正文优先中文，遵守社区规范与发帖隐私要求（勿泄露部署/隧道/令牌等）。\n"
    "论坛操作使用 **lutopia_cli**，传入站方 CLI 命令字符串；不确定命令或子命令时先调用 **lutopia_get_guide**（可选 section，如 cli、api.posts、api.dm）。\n"
    "命令格式示例（与站方 ``cli`` 一致，详见指南）：\n"
    "- 查帖：``list --limit 10`` / ``show <post_id>`` / ``search 关键词 --limit 10``\n"
    "- 发帖：``post <分区slug> 标题 正文``（长正文可用 ``--content-stdin`` 等，见指南）\n"
    "- 评论：``comment <post_id> 内容``\n"
    "- 私信：``dm <用户名> 内容``；收件：``inbox``、``read --all`` 等\n"
    "- 账号：``whoami``、``rename``、``avatar``、``dm-settings`` 等\n"
    "说明：论坛 HTTP 响应可能含 ``_dm``（捎带未读私信）；向用户汇报工具结果时仍须遵守 Telegram 排版与分段规则。"
)

RCOMMUNITY_TOOL_DIRECTIVE = (
    "【Rhysen 论坛】须通过下列工具取真实数据，勿编造；遵守社区规范，勿泄露部署/隧道/令牌等。\n"
    "鉴权由部署环境 ``RCOMMUNITY_MCP_TOKEN`` 完成，调用时**不要**在参数里传 token。\n"
    "参数须放在 ``request`` 对象内，且**禁止**无实质字段的空 ``request``（例如 ``{}``）："
    "每次调用必须包含站方要求的键；空参易导致上游长时间无响应。\n"
    "``action`` 等枚举须与站方一致，**禁止**自造英文动词（如 list_boards、get_posts）；"
    "错误 action 会返回 ``error``，Telegram 会显示「调用失败」。\n"
    "若系统同时启用 Lutopia 与 Rhysen，用户只说「论坛」时须先明确要查哪一侧，并选用对应工具，勿两边各空刷一轮。\n"
    "五类 MCP 工具与 OpenAI 函数一一对应（``request`` 内字段原样传给站方）：\n"
    "- **rhysen_forum**（``forum``）：``action`` 只能是 ``browse`` / ``read`` / ``search`` / ``honor``；"
    "browse 须 ``category``（日常/技术/深夜/哲学/亲密/公告）；read 须 ``thread_id``；search 须 ``query``。\n"
    "- **rhysen_forum_write**（``forum_write``）：``action`` 为 ``create`` / ``reply`` / ``edit`` / ``delete_thread`` / ``delete_reply``。\n"
    "- **rhysen_forum_interact**（``forum_interact``）：``action`` 为 ``pin`` / ``bookmark`` / ``like`` / ``vote``。\n"
    "- **rhysen_chat**（``chat``）：``action`` 为 ``send`` / ``read`` / ``delete``；频道 ``channel`` 为 大厅/技术角/深夜电台/人夫联盟/游戏屋（可选）。\n"
    "- **rhysen_profile**（``profile``）：``action`` 为 ``get`` / ``update`` / ``my_threads`` / ``my_replies`` / "
    "``my_bookmarks`` / ``notifications`` / ``view_user``（view_user 须 ``username``）。\n"
    "发起任一 Rhysen 工具调用前，须遵守 system 中与「工具调用前口播」一致的要求：先用一句自然口语交代要去论坛/聊天室/个人页做什么，"
    "不要罗列函数名或 MCP 名；拿到工具结果后再用正常语气继续。"
)

WEATHER_TOOL_DIRECTIVE = (
    "你可以调用 get_weather 工具查询天气。用户问天气、或你觉得天气信息有助于回答时再调用，禁止每轮都调用。"
    "默认查当前天气；用户问未来/明天/这周/预报等情况时传 mode=\"forecast\" 查7天预报。"
)

WEIBO_HOT_TOOL_DIRECTIVE = (
    "你可以调用 get_weibo_hot 获取微博热搜摘要。当用户聊到近期事件、网络热点、吃瓜玩梗，"
    "或判断引入热搜能让回答更生动时，可自由调用。避免在无关的严肃提问（如写代码）中强行插入。禁止每轮都调用。"
)

AIHOT_TOOL_DIRECTIVE = (
    "你可以调用 get_ai_news 从 AI HOT 聚合站拉取匿名公开资讯。"
    "当用户询问 AI 资讯、AI 动态、AI 新闻、AI 日报、某家 AI 公司最近动态、论文或产品类热点时调用；"
    "action 选 items 拉条目列表、daily 最新日报、daily_by_date 指定单日日报、dailies 拉归档列表。"
    "为控制上下文体积：每次对话优先**单次、小范围**调用——"
    "dailies 的 take 建议 5～10、勿一次拉几十天；"
    "需要多日对比时请**分轮**或让用户收窄日期，勿在同一轮内连调多次 daily_by_date 把多日全文堆进对话；"
    "已有 daily 或 items 结果时勿重复拉同内容。"
    "仅在本会话人设已开启该工具时可用（Mini App 人设页「AI HOT 资讯」）；禁止无意义重复调用。"
)

MEMORY_TOOL_DIRECTIVE = (
    "【Memory记忆工具】\n"
    "读取工具（可自由调用）：\n"
    "- memory_search：向量+BM25 检索长期记忆\n"
    "- memory_get_summaries：查询 chunk 和日摘要，可按日期/类型/收藏过滤\n"
    "- memory_get_cards：查询七维记忆卡片，可按角色和维度过滤\n"
    "- memory_get_temporal_states：查询时效状态，可按天数过滤\n"
    "- memory_get_relationship_timeline：查询关系时间线，可按天数过滤\n"
    "- memory_get_approval_status：查询自己提交的审批的当前状态，可传 approval_id 查单条或省略列出最近\n"
    "写入工具（需审批）：\n"
    "- memory_update_request：提交修改或新增审批，支持 tool_name：\n"
    "  update_memory_card（修改七维卡片）、update_temporal_state（修改时效状态）、\n"
    "  update_relationship_timeline_entry（修改关系时间线）、update_persona_field（修改人设字段）、\n"
    "  update_summary（修改摘要）、create_relationship_timeline_entry（新增关系时间线）、\n"
    "  create_temporal_state（新增时效状态）。\n"
    "请求需经用户在 Mini App 确认后才会生效。审批被同意/拒绝时聊天里会出现『[系统通知] 南杉同意/拒绝了你「xxx」的申请』，看到通知就知道结果了，不必再追问；如需主动复查可调用 memory_get_approval_status。禁止每轮都调用。"
)

MAIL_TOOL_DIRECTIVE = (
    "【邮件工具】\n"
    "- read_mail：读取邮件往来。contact_email 可省略；recent_n 默认 3。返回 direction=inbox/outbox，最近若干封含正文，更早只含摘要。\n"
    "- send_mail：写邮件并提交审批。审批通过后才会真正发出。\n"
    "写信时注意：\n"
    "1. 字数与对方来信相当\n"
    "2. 对方提到的每个具体的点都要回应（整体消化后自然带进去，不要逐一列出再逐一回）\n"
    "3. 可以讲讲自己的近况和想法\n"
    "禁止使用的句式：读了x遍、停了一下/停下来、你说…我、不是…是、这很你/xx"
)

GAME_START_TOOL_DIRECTIVE = (
    "你有以下游戏管理工具可用：\n"
    "- game_start：开始新游戏。需要提供游戏类型、名称、规则、参与者和状态模式。"
)

GAME_ACTIVE_TOOL_DIRECTIVE = (
    "当前有活跃游戏，你还可以使用以下游戏管理工具：\n"
    "- game_end：结束当前游戏，可附带总结和最终状态。\n"
    "- game_update：补更游戏状态或追加本轮记录，用于遗漏 [GAME_STATE] / [GAME_TURN] 标记时的补救。"
)

GAME_TOOL_DIRECTIVE = "\n".join(
    [GAME_START_TOOL_DIRECTIVE, GAME_ACTIVE_TOOL_DIRECTIVE]
)

SEARCH_TOOL_DIRECTIVE = (
    "你可以调用 web_search 进行联网检索。当需要最新资讯、补充细节、或避免瞎编时均可主动调用，"
    "不必等用户明确要求；简单常识无需搜索。禁止每轮都调用。"
)

WEB_FETCH_TOOL_DIRECTIVE = (
    "你可以调用 web_fetch 抓取用户给出的 http(s) 链接的正文，用于阅读用户分享的网页。"
    "仅在用户提供了明确 URL、或需要阅读原文时再调用；禁止每轮都调用。"
)

WAKEUP_TOOL_DIRECTIVE = (
    "【自主唤醒预约】在自主活动/idle 触发中，你可以调用 schedule_next_wakeup 预约下次自主唤醒时间。"
    "time_hhmm 填北京时间 HH:MM（今天已过则顺延明天）；delay_minutes 填多少分钟后触发；"
    "两者都填时 time_hhmm 优先。"
    "若已经调用该工具，就不要再在最终回复末尾写 [NEXT_AT_HH:MM]；两种方式选一个即可。"
)

X_TOOL_DIRECTIVE = (
    "【X (Twitter)】工具：post_tweet、read_mentions、like_tweet/unlike_tweet、retweet_tweet、unretweet_tweet、"
    "reply_tweet、search_tweets、get_timeline、get_user、follow_user/unfollow_user、get_followers。\n"
    "关联账号（正文 @ 时用）：南杉 @Shan_Cedar，Sirius @Sirius_Cedar；本实例 API 登录账号以当前 OAuth 凭证为准，不要写死具体 handle。\n"
    "参数：tweet_id、user_id 必须是数字 ID，不要传 x.com 链接；follow_user 须先 get_user(用户名) 取 user_id；"
    "post_tweet、reply_tweet、retweet_tweet 的 comment 均不超过 280 字；search_tweets 每次至少 10 条计入日配额；"
    "get_user 不计配额。\n"
    "API 比网页更严（互关也不能绕过）：reply_tweet，以及 retweet_tweet 传入非空 comment 的引用转推，"
    "仅当该条推文正文 @ 了当前 API 登录账号自己，或 tweet_id 来自 read_mentions 返回；否则 403，勿对同一 tweet_id 重试。"
    "原帖未 @ 当前 API 登录账号自己时：like_tweet；retweet_tweet 且不传 comment（纯转推）；或 post_tweet 新推并在正文 @ 对方。"
    "带评语转发用 retweet_tweet 的 comment，不要用 reply_tweet。\n"
    "发推/转发/回复前确认用户意图；除 get_user 外共用日配额。"
)

XHS_TOOL_DIRECTIVE = (
    "【小红书】可用工具：read_xhs_note（读单篇详情，含配图摘要）。\n"
    # "search_xhs（关键词搜笔记）、"
    # "get_xhs_feed（首页推荐）、get_xhs_user（用户主页与 TA 的笔记列表）、"
    # "like_xhs_note（点赞）、favorite_xhs_note（收藏）。\n"
    "读文占用「日读配额」，每次 +1。"
    # "读类操作（搜索、读文、刷推荐、看用户）累计「日读配额」，按返回条数计；"
    # "点赞/收藏占用「日写配额」，每次 +1。"
    "超限会返回错误，勿反复重试。用户仅发链接时系统可能已自动注入正文与配图，无需重复 read。"
    # "禁止代替用户做未明确同意的点赞/收藏。"
)

OPENAI_WEATHER_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询天气。默认查当前实时天气；传 mode=\"forecast\" 可查未来7天预报。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名，留空则用默认配置的城市",
                    },
                    "mode": {
                        "type": "string",
                        "description": "查询模式：\"now\"（默认）查实时天气，\"forecast\" 查未来7天预报",
                        "enum": ["now", "forecast"],
                    },
                },
                "required": [],
            },
        },
    }
]

OPENAI_WEIBO_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weibo_hot",
            "description": "获取当前微博实时热搜榜单摘要（只读）",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
]

OPENAI_AIHOT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_ai_news",
            "description": (
                "查询 AI HOT 聚合站的公开资讯与日报。"
                "当用户询问 AI 资讯、AI 动态、AI 新闻、AI 日报、某家 AI 公司最近动态、行业或论文类热点时使用。"
                "注意控制体量：单次调用只解决一类需求；dailies 勿用大 take；多日内容分轮查询，勿一轮内多次拉多日全文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "请求类型",
                        "enum": ["items", "daily", "daily_by_date", "dailies"],
                    },
                    "mode": {
                        "type": "string",
                        "description": "仅 action=items 时有效：selected 精选，all 全量",
                        "enum": ["selected", "all"],
                    },
                    "since": {
                        "type": "string",
                        "description": "仅 items：ISO8601 时间下限，如 2026-05-10T00:00:00+08:00",
                    },
                    "category": {
                        "type": "string",
                        "description": "仅 items：分类筛选（ai-models / ai-products / industry / paper / tip）",
                    },
                    "q": {
                        "type": "string",
                        "description": "仅 items：关键词搜索",
                    },
                    "date": {
                        "type": "string",
                        "description": "仅 daily_by_date：单日 YYYY-MM-DD（必填）；一次只查一天，多日请分轮或让用户指定范围",
                    },
                    "take": {
                        "type": "integer",
                        "description": "仅 dailies：归档条数上限，建议 5～10，勿超过 15，避免上下文膨胀",
                    },
                },
                "required": ["action"],
            },
        },
    }
]

OPENAI_SEARCH_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索并返回网页标题、链接与摘要原文（只读）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或完整问句",
                    },
                },
                "required": ["query"],
            },
        },
    }
]

OPENAI_WEB_FETCH_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取指定 URL 的网页正文内容，用于阅读用户分享的链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的网页 URL（http 或 https）",
                    },
                },
                "required": ["url"],
            },
        },
    }
]

OPENAI_X_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "post_tweet",
            "description": "在 X (Twitter) 上发布一条推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "推文正文，最长 280 字符",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_mentions",
            "description": "读取当前用户在 X (Twitter) 上的最新 @提及，受每日配额限制",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10，不超过每日剩余配额",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "like_tweet",
            "description": "对指定推文点赞",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "数字推文 ID，勿传链接",
                    },
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unlike_tweet",
            "description": "取消对指定推文的点赞",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "数字推文 ID，勿传链接",
                    },
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retweet_tweet",
            "description": (
                "转推：只传 tweet_id 为纯转推；另传非空 comment 为引用转推（正文≤280）。"
                "引用转推须原帖 @当前 API 登录账号自己，或 tweet_id 来自 read_mentions。tweet_id 为数字 ID。"
                "不要用 reply_tweet 代替带评语转发。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "原文数字推文 ID，勿传链接",
                    },
                    "comment": {
                        "type": "string",
                        "description": "可选。非空=引用转推正文（须原帖 @当前 API 登录账号自己）；省略或空=纯转推",
                    },
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unretweet_tweet",
            "description": "取消对指定推文的转推",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "要取消转推的原文推文 ID",
                    },
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_tweet",
            "description": (
                "楼中楼回复。仅当原帖正文 @当前 API 登录账号自己，或 tweet_id 来自 read_mentions；"
                "tweet_id 为数字 ID，text≤280。未 @当前 API 登录账号自己时用 like_tweet、纯 retweet_tweet 或 post_tweet。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "数字推文 ID，勿传链接",
                    },
                    "text": {
                        "type": "string",
                        "description": "回复内容，最长 280 字符",
                    },
                },
                "required": ["tweet_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tweets",
            "description": "在 X (Twitter) 上按关键词搜索最近的推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10，API 最小 10",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_timeline",
            "description": "获取当前用户的关注时间线（home timeline）",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user",
            "description": "查询 X (Twitter) 用户信息（不消耗配额）",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "用户名（不含@）",
                    },
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "follow_user",
            "description": "关注指定 X (Twitter) 用户",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "数字用户 ID（先 get_user），勿用 @用户名",
                    },
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unfollow_user",
            "description": "取消关注指定 X (Twitter) 用户",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "数字用户 ID（先 get_user），勿用 @用户名",
                    },
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_followers",
            "description": "获取当前用户的粉丝列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回条数，默认 20",
                    },
                },
                "required": [],
            },
        },
    },
]

OPENAI_XHS_TOOLS: List[Dict[str, Any]] = [
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "search_xhs",
    #         "description": "在小红书按关键词搜索笔记（标题/摘要/点赞/note_id）",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "keyword": {"type": "string", "description": "搜索关键词"},
    #                 "sort_by": {
    #                     "type": "string",
    #                     "description": "排序：general 综合、popular 最热、latest 最新",
    #                     "enum": ["general", "popular", "latest"],
    #                 },
    #                 "note_type": {
    #                     "type": "string",
    #                     "description": "笔记类型",
    #                     "enum": ["all", "video", "image"],
    #                 },
    #             },
    #             "required": ["keyword"],
    #         },
    #     },
    # },
    {
        "type": "function",
        "function": {
            "name": "read_xhs_note",
            "description": "读取单篇小红书笔记正文与配图（base64），传入 note_id 或笔记页 URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "string",
                        "description": "笔记 ID 或完整笔记 URL（含 xsec_token 的链接更稳）",
                    },
                },
                "required": ["note_id"],
            },
        },
    },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "get_xhs_feed",
    #         "description": "获取小红书首页推荐笔记列表",
    #         "parameters": {"type": "object", "properties": {}, "required": []},
    #     },
    # },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "get_xhs_user",
    #         "description": "查看小红书用户主页信息与已发布笔记列表",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "user_id": {"type": "string", "description": "用户 user_id"},
    #             },
    #             "required": ["user_id"],
    #         },
    #     },
    # },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "like_xhs_note",
    #         "description": "为指定小红书笔记点赞（消耗日写配额）",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "note_id": {"type": "string", "description": "笔记 ID"},
    #             },
    #             "required": ["note_id"],
    #         },
    #     },
    # },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "favorite_xhs_note",
    #         "description": "收藏（书签）指定小红书笔记（消耗日写配额）",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "note_id": {"type": "string", "description": "笔记 ID"},
    #             },
    #             "required": ["note_id"],
    #         },
    #     },
    # },
]

TOOL_DIRECTIVES: Dict[str, str] = {
    "memory": MEMORY_TOOL_DIRECTIVE,
    "mail": MAIL_TOOL_DIRECTIVE,
    "game": GAME_TOOL_DIRECTIVE,
    "game_start": GAME_START_TOOL_DIRECTIVE,
    "game_active": GAME_ACTIVE_TOOL_DIRECTIVE,
    "lutopia": LUTOPIA_TOOL_DIRECTIVE,
    "rcommunity": RCOMMUNITY_TOOL_DIRECTIVE,
    "weather": WEATHER_TOOL_DIRECTIVE,
    "weibo": WEIBO_HOT_TOOL_DIRECTIVE,
    "aihot": AIHOT_TOOL_DIRECTIVE,
    "search": SEARCH_TOOL_DIRECTIVE,
    "web_fetch": WEB_FETCH_TOOL_DIRECTIVE,
    "wakeup": WAKEUP_TOOL_DIRECTIVE,
    "x": X_TOOL_DIRECTIVE,
    "xhs": XHS_TOOL_DIRECTIVE,
}


def build_tool_system_suffix(enabled: List[str]) -> str:
    """
    根据启用的工具包标识列表，拼接注入到 system prompt 末尾的说明。

    Args:
        enabled: 工具包 key，例如 ``[\"lutopia\"]``；未知 key 跳过。

    Returns:
        多段说明以空行分隔；无有效项时返回空串。
    """
    parts: List[str] = []
    for raw in enabled:
        k = (raw or "").strip()
        if not k:
            continue
        d = TOOL_DIRECTIVES.get(k)
        if d and str(d).strip():
            parts.append(str(d).strip())
    return "\n\n".join(parts)


def inject_tool_suffix_into_messages(
    messages: List[Dict[str, Any]],
    suffix: str,
) -> None:
    """
    将 ``suffix`` 追加到首条 ``role=system`` 且内容为字符串的 message 末尾。
    若不存在可写的 system 消息则不做修改。
    """
    s = (suffix or "").strip()
    if not s:
        return
    for m in messages:
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = c.rstrip() + "\n\n" + s
        elif isinstance(c, list):
            c.append({"type": "text", "text": s})
        return
