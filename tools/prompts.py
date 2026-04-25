"""
LLM 工具相关的 system prompt 片段。

按「工具包」注册（如 ``lutopia``），与 ``OPENAI_*_TOOLS`` 的启用列表对齐；
新增工具时在此增加常量并在 ``TOOL_DIRECTIVES`` 中登记。
"""

from __future__ import annotations

from typing import Any, Dict, List

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

WEATHER_TOOL_DIRECTIVE = (
    "你可以调用 get_weather 工具查询当前天气。用户问天气、或你觉得天气信息有助于回答时再调用，禁止每轮都调用。"
)

WEIBO_HOT_TOOL_DIRECTIVE = (
    "你可以调用 get_weibo_hot 获取微博热搜摘要。当用户聊到近期事件、网络热点、吃瓜玩梗，"
    "或判断引入热搜能让回答更生动时，可自由调用。避免在无关的严肃提问（如写代码）中强行插入。禁止每轮都调用。"
)

SEARCH_TOOL_DIRECTIVE = (
    "你可以调用 web_search 进行联网检索。当需要最新资讯、补充细节、或避免瞎编时均可主动调用，"
    "不必等用户明确要求；简单常识无需搜索。禁止每轮都调用。"
)

OPENAI_WEATHER_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询当前天气状况",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名，留空则用默认配置的城市",
                    }
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

OPENAI_SEARCH_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索并返回经压缩的网页要点摘要（只读）",
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

TOOL_DIRECTIVES: Dict[str, str] = {
    "lutopia": LUTOPIA_TOOL_DIRECTIVE,
    "weather": WEATHER_TOOL_DIRECTIVE,
    "weibo": WEIBO_HOT_TOOL_DIRECTIVE,
    "search": SEARCH_TOOL_DIRECTIVE,
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
