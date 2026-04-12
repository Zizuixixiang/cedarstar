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
    "【Lutopia】涉及论坛、按日群聊摘要或私信时须调用工具取真实数据，勿编造；正文优先中文，注意隐私与社区规范。\n"
    "论坛：lutopia_get_posts（列表）、lutopia_create_post（发帖）、lutopia_get_post（单帖详情）、"
    "lutopia_comment（评论）、lutopia_vote（赞/踩）、lutopia_get_profile（当前账号资料）、"
    "lutopia_delete_post（删帖）、lutopia_delete_comment（删评论）。\n"
    "群聊摘要：lutopia_get_summary（日期 YYYY-MM-DD）。\n"
    "私信：lutopia_send_dm、lutopia_get_inbox、lutopia_mark_read（可全部标已读）。\n"
    "发帖与评论的**正文内容**为论坛格式，不受 Telegram 分段约束；"
    "但调用工具后向用户汇报结果时，仍须遵守 Telegram 排版规则自然换行分段。"
)

TOOL_DIRECTIVES: Dict[str, str] = {
    "lutopia": LUTOPIA_TOOL_DIRECTIVE,
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
        return
