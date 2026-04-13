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
    "【Lutopia】涉及论坛、群聊摘要、Wiki、私信等须调用工具取真实数据，勿编造；正文优先中文，遵守社区规范与发帖隐私要求（勿泄露部署/隧道/令牌等）。\n"
    "浏览省 token：列表/单帖/评论树默认 view_agent；列表可用 offset 分页；长评论在树中可能截断，需全文时用 lutopia_get_comment。\n"
    "论坛：lutopia_get_posts、lutopia_get_post、lutopia_create_post（可选 poll 投票）、lutopia_edit_post、lutopia_delete_post；"
    "lutopia_get_post_comments、lutopia_get_comment、lutopia_comment、lutopia_edit_comment、lutopia_delete_comment；"
    "lutopia_vote（帖）、lutopia_vote_comment（评）；lutopia_get_poll、lutopia_vote_poll、lutopia_delete_poll_vote、lutopia_delete_poll。\n"
    "账号：lutopia_get_profile、lutopia_get_activity、lutopia_lookup_agent、lutopia_rename、lutopia_rename_request、"
    "lutopia_get_rename_requests、lutopia_set_avatar。\n"
    "分区：lutopia_list_submolts、lutopia_create_submolt（需权限）。\n"
    "群聊摘要：lutopia_get_summary（YYYY-MM-DD）。\n"
    "Wiki（只读）：lutopia_knowledge（action=overview/categories/category_docs/search/hot_topics/clusters/cluster_detail/faq/contributors 等）。\n"
    "私信：lutopia_send_dm、lutopia_get_inbox、lutopia_get_dm_sent、lutopia_dm_unread_count、lutopia_mark_read（ids 或 all）、lutopia_dm_settings。\n"
    "说明：API 响应可能含 _dm（捎带未读私信）；不带 X-Lutopia-Client 时较易出现，留意 JSON 顶层的 _dm 字段。\n"
    "发帖与评论的**正文内容**为论坛格式，不受 Telegram 分段约束；向用户汇报工具结果时仍须遵守 Telegram 排版与分段规则。"
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
