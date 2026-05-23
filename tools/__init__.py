"""项目内可调用工具（表情包、Lutopia Forum 等）。"""

from tools.prompts import (
    LUTOPIA_TOOL_DIRECTIVE,
    RCOMMUNITY_TOOL_DIRECTIVE,
    OPENAI_SEARCH_TOOLS,
    OPENAI_WEATHER_TOOLS,
    OPENAI_WEIBO_TOOLS,
    OPENAI_XHS_TOOLS,
    OPENAI_X_TOOLS,
    TOOL_DIRECTIVES,
    GAME_TOOL_DIRECTIVE,
    SEARCH_TOOL_DIRECTIVE,
    WEATHER_TOOL_DIRECTIVE,
    WEIBO_HOT_TOOL_DIRECTIVE,
    XHS_TOOL_DIRECTIVE,
    X_TOOL_DIRECTIVE,
    build_tool_system_suffix,
    inject_tool_suffix_into_messages,
)
from tools.lutopia import (
    OPENAI_LUTOPIA_TOOLS,
    append_tool_exchange_to_messages,
    create_lutopia_mcp_session,
    execute_lutopia_function_call,
    get_lutopia_token,
)
from tools.game_tools import OPENAI_GAME_TOOLS, execute_game_function_call

from tools.rcommunity import (
    OPENAI_RCOMMUNITY_TOOLS,
    create_rcommunity_mcp_session,
    execute_rcommunity_function_call,
    maybe_rcommunity_mcp_session,
)

__all__ = [
    "LUTOPIA_TOOL_DIRECTIVE",
    "RCOMMUNITY_TOOL_DIRECTIVE",
    "SEARCH_TOOL_DIRECTIVE",
    "WEATHER_TOOL_DIRECTIVE",
    "WEIBO_HOT_TOOL_DIRECTIVE",
    "XHS_TOOL_DIRECTIVE",
    "X_TOOL_DIRECTIVE",
    "OPENAI_SEARCH_TOOLS",
    "OPENAI_WEATHER_TOOLS",
    "OPENAI_WEIBO_TOOLS",
    "OPENAI_XHS_TOOLS",
    "OPENAI_X_TOOLS",
    "TOOL_DIRECTIVES",
    "GAME_TOOL_DIRECTIVE",
    "build_tool_system_suffix",
    "inject_tool_suffix_into_messages",
    "OPENAI_LUTOPIA_TOOLS",
    "OPENAI_RCOMMUNITY_TOOLS",
    "OPENAI_GAME_TOOLS",
    "append_tool_exchange_to_messages",
    "create_lutopia_mcp_session",
    "create_rcommunity_mcp_session",
    "maybe_rcommunity_mcp_session",
    "execute_lutopia_function_call",
    "execute_rcommunity_function_call",
    "execute_game_function_call",
    "get_lutopia_token",
]
