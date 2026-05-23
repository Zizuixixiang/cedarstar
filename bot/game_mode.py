"""游戏模式回复标记解析。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from memory.database import (
    add_game_turn,
    get_latest_turn_idx,
    update_game_session_state,
)

logger = logging.getLogger(__name__)

_GAME_STATE_RE = re.compile(r"\[GAME_STATE\](.*?)\[/GAME_STATE\]", re.DOTALL)
_GAME_TURN_RE = re.compile(r"\[GAME_TURN\](.*?)\[/GAME_TURN\]", re.DOTALL)


async def process_game_mode_response(
    response_text: str, game_session: Dict[str, Any]
) -> str:
    """解析游戏标记块，更新数据库，返回剥除标记后的文本。"""
    if not game_session:
        return response_text

    clean_text = response_text or ""
    session_id = str(game_session.get("id") or "").strip()
    if not session_id:
        return clean_text

    state_match = _GAME_STATE_RE.search(clean_text)
    state_updated = False
    if state_match:
        try:
            new_state = json.loads(state_match.group(1).strip())
            await update_game_session_state(session_id, new_state)
            state_updated = True
        except json.JSONDecodeError:
            logger.warning("Failed to parse GAME_STATE JSON")
        except Exception:
            logger.exception("Failed to update GAME_STATE session_id=%s", session_id)
        clean_text = clean_text.replace(state_match.group(0), "").strip()
    elif game_session.get("state_mode") == "per_turn":
        logger.warning("per_turn game mode but no [GAME_STATE] block in response")

    turn_match = _GAME_TURN_RE.search(clean_text)
    recorded_turn_idx = None
    if turn_match:
        try:
            turn_data = json.loads(turn_match.group(1).strip())
        except json.JSONDecodeError:
            turn_data = {"raw": turn_match.group(1).strip()}
        try:
            turn_idx = await get_latest_turn_idx(session_id) + 1
            await add_game_turn(session_id, turn_idx, turn_data)
            recorded_turn_idx = turn_idx
        except Exception:
            logger.exception("Failed to add GAME_TURN session_id=%s", session_id)
        clean_text = clean_text.replace(turn_match.group(0), "").strip()
    else:
        logger.warning("Game mode active but no [GAME_TURN] block in response")

    if state_updated and recorded_turn_idx is not None:
        clean_text = f"{clean_text}\n\n「🎮 状态已更新 · 第{recorded_turn_idx}轮已记录」".strip()
    elif state_updated:
        clean_text = f"{clean_text}\n\n「🎮 状态已更新」".strip()
    elif recorded_turn_idx is not None:
        clean_text = f"{clean_text}\n\n「🎮 第{recorded_turn_idx}轮已记录」".strip()

    return clean_text
