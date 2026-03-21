# CedarStar

## 项目简介

CedarStar 是一个具备长期记忆能力的 AI 聊天机器人系统，支持 Discord 与 Telegram 接入，通过消息缓冲、微批摘要、日终小传与向量检索等分层机制持久化对话与记忆，并提供基于 React 的管理后台（Mini App）用于人设、记忆、历史与 API 配置等运维操作。详细架构与数据流见仓库内 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python、FastAPI、SQLite |
| 向量与检索 | ChromaDB、BM25（jieba + rank_bm25）、Cohere Rerank（可选） |
| 机器人 | discord.py、python-telegram-bot |
| LLM | OpenAI 兼容 API、Anthropic Claude（可配置） |
| 向量嵌入 | 智谱 AI embedding-3 |
| 前端 | React、Vite（`miniapp/`） |

## 目录结构（简略）

```
cedarstar/
├── main.py              # 主入口（Bot + 日终任务 + API）
├── config.py            # 环境变量与全局配置
├── requirements.txt
├── api/                 # FastAPI 路由（dashboard、persona、memory、history 等）
├── bot/                 # Discord / Telegram 机器人
├── llm/                 # 统一 LLM 调用封装
├── memory/              # 数据库、上下文组装、微批/日终批处理、向量库
├── services/            # 外部服务扩展（部分占位）
├── tools/               # 工具函数扩展（部分占位）
├── miniapp/             # React 管理界面源码
└── ARCHITECTURE.md      # 完整目录树与模块说明
```

## 规划中，暂未实现

以下文件目前仅为占位或空实现，**规划中，暂未实现**：

- `services/wx_read.py` — 微信读书相关集成  
- `tools/weather.py` — 天气查询工具  
- `tools/location.py` — 位置相关工具  

如需了解各模块职责、数据库表结构与接口约定，请以 **`ARCHITECTURE.md`** 为准。
