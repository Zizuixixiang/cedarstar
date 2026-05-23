"""Internal game-mode tools exposed to the OpenAI-compatible tool loop."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


OPENAI_GAME_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "game_start",
            "description": "创建新游戏 session 并设为当前活跃游戏。",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_type": {
                        "type": "string",
                        "description": "游戏类型标识，如 stardew / dst / werewolf / undercover / monopoly",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "显示名称，如“海边农场”、“狼人杀第1局”",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "该游戏的完整规则 prompt",
                    },
                    "config_json": {
                        "type": "object",
                        "description": "开局配置，如 {\"wolf_count\": 2, \"seer\": true}",
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参与者列表，如 [\"南杉\", \"Clio\", \"Sirius\"]",
                    },
                    "state_mode": {
                        "type": "string",
                        "enum": ["per_turn", "on_end"],
                        "description": "per_turn=每轮更新 state_json；on_end=session 结束时更新 state_json",
                    },
                },
                "required": ["game_type", "display_name", "system_prompt", "state_mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "game_end",
            "description": "结束当前活跃游戏，可附带总结和最终状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "本次游戏总结",
                    },
                    "state_json": {
                        "type": "object",
                        "description": "最终状态快照",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "game_update",
            "description": "手动补更游戏状态或追加 turn 记录，用于遗漏 GAME_STATE / GAME_TURN 标记时补救。",
            "parameters": {
                "type": "object",
                "properties": {
                    "state_json": {
                        "type": "object",
                        "description": "更新 state_json（覆盖写入）；仅 per_turn 游戏允许进行中更新",
                    },
                    "turn_data": {
                        "type": "object",
                        "description": "追加一条 game_turn 记录",
                    },
                },
                "required": [],
            },
        },
    },
]


def _args_dict(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_response(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _game_start(args: Dict[str, Any]) -> str:
    from memory.database import (
        create_game_session,
        get_active_game_session_id,
        get_game_session,
        set_active_game_session_id,
    )

    active_id = await get_active_game_session_id()
    if active_id:
        active = await get_game_session(active_id)
        display_name = (
            active.get("display_name") if active else None
        ) or active_id
        return _json_response(
            {
                "success": False,
                "error": f"当前已有活跃游戏 {display_name}，请先结束",
                "active_session_id": active_id,
            }
        )

    state_mode = str(args.get("state_mode") or "").strip()
    if state_mode not in {"per_turn", "on_end"}:
        return _json_response(
            {"success": False, "error": "state_mode must be per_turn or on_end"}
        )
    game_type = str(args.get("game_type") or "").strip()
    display_name = str(args.get("display_name") or "").strip()
    system_prompt = str(args.get("system_prompt") or "").strip()
    if not game_type or not display_name or not system_prompt:
        return _json_response(
            {
                "success": False,
                "error": "game_type、display_name、system_prompt 均不能为空",
            }
        )

    participants = args.get("participants") or []
    if not isinstance(participants, list):
        participants = []
    row = await create_game_session(
        game_type,
        display_name,
        system_prompt,
        args.get("config_json") if isinstance(args.get("config_json"), dict) else {},
        [str(p) for p in participants],
        state_mode,
    )
    await set_active_game_session_id(row["id"])
    return _json_response(
        {
            "success": True,
            "message": "游戏已开始",
            "session_id": row["id"],
            "display_name": row.get("display_name"),
            "game_type": row.get("game_type"),
            "state_mode": row.get("state_mode"),
        }
    )


async def _game_end(args: Dict[str, Any]) -> str:
    from memory.database import (
        end_game_session,
        get_active_game_session_id,
        set_active_game_session_id,
    )

    session_id = await get_active_game_session_id()
    if not session_id:
        return _json_response({"success": False, "error": "当前没有活跃游戏"})
    state_json = args.get("state_json")
    await end_game_session(
        session_id,
        args.get("summary"),
        state_json if isinstance(state_json, dict) else None,
    )
    await set_active_game_session_id(None)
    return _json_response(
        {"success": True, "message": "游戏已结束", "session_id": session_id}
    )


async def _game_update(args: Dict[str, Any]) -> str:
    from memory.database import (
        add_game_turn,
        get_active_game_session_id,
        get_game_session,
        get_latest_turn_idx,
        update_game_session_state,
    )

    has_state = isinstance(args.get("state_json"), dict)
    has_turn = isinstance(args.get("turn_data"), dict)
    if not has_state and not has_turn:
        return _json_response(
            {"success": False, "error": "state_json 和 turn_data 至少传一个"}
        )

    session_id = await get_active_game_session_id()
    if not session_id:
        return _json_response({"success": False, "error": "当前没有活跃游戏"})
    session = await get_game_session(session_id)
    if not session:
        return _json_response({"success": False, "error": "当前没有活跃游戏"})

    updated: List[str] = []
    turn_idx = None
    if has_state:
        if session.get("state_mode") == "on_end":
            return _json_response(
                {
                    "success": False,
                    "error": "当前游戏为存档型(on_end)，进行中不允许更新 state_json，请在 game_end 时传入",
                    "session_id": session_id,
                }
            )
        await update_game_session_state(session_id, args["state_json"])
        updated.append("state_json")

    if has_turn:
        turn_idx = await get_latest_turn_idx(session_id) + 1
        await add_game_turn(session_id, turn_idx, args["turn_data"])
        updated.append("turn_data")

    return _json_response(
        {
            "success": True,
            "message": "游戏已更新",
            "session_id": session_id,
            "updated": updated,
            "turn_idx": turn_idx,
        }
    )


async def execute_game_function_call(function_name: str, arguments: Any) -> str:
    args = _args_dict(arguments)
    try:
        if function_name == "game_start":
            return await _game_start(args)
        if function_name == "game_end":
            return await _game_end(args)
        if function_name == "game_update":
            return await _game_update(args)
        return _json_response(
            {"success": False, "error": f"Unknown game tool: {function_name}"}
        )
    except Exception as e:
        logger.exception("execute_game_function_call(%s) failed", function_name)
        return _json_response({"success": False, "error": str(e)})
