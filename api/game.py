"""游戏模式 API。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


class GameSessionCreateBody(BaseModel):
    game_type: str = Field(..., min_length=1, max_length=64)
    display_name: Optional[str] = None
    system_prompt: Optional[str] = None
    config_json: Dict[str, Any] = Field(default_factory=dict)
    state_json: Dict[str, Any] = Field(default_factory=dict)
    participants: List[Any] = Field(default_factory=list)
    state_mode: str = "on_end"


class GameStateUpdateBody(BaseModel):
    state_json: Dict[str, Any] = Field(default_factory=dict)


class GameEndBody(BaseModel):
    summary: Optional[str] = None
    state_json: Optional[Dict[str, Any]] = None


class ActiveGameBody(BaseModel):
    session_id: Optional[str] = None


class GameSessionUpdateBody(BaseModel):
    display_name: Optional[str] = None
    system_prompt: Optional[str] = None
    config_json: Optional[Dict[str, Any]] = None
    state_json: Optional[Dict[str, Any]] = None
    participants: Optional[List[Any]] = None
    state_mode: Optional[str] = None


class GameTurnBody(BaseModel):
    turn_data: Dict[str, Any] = Field(default_factory=dict)


@router.get("/sessions")
async def get_sessions(game_type: Optional[str] = None, active_only: bool = False):
    from memory.database import list_game_sessions

    rows = await list_game_sessions(game_type=game_type, active_only=active_only)
    return create_response(True, rows, "ok")


@router.post("/sessions")
async def create_session(body: GameSessionCreateBody):
    from memory.database import create_game_session, update_game_session_state

    if body.state_mode not in {"per_turn", "on_end"}:
        raise HTTPException(status_code=400, detail="state_mode must be per_turn or on_end")
    row = await create_game_session(
        body.game_type,
        body.display_name,
        body.system_prompt,
        body.config_json,
        body.participants,
        body.state_mode,
    )
    if body.state_json:
        await update_game_session_state(row["id"], body.state_json)
        row["state_json"] = body.state_json
    return create_response(True, row, "created")


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    from memory.database import get_game_session, get_game_turns

    row = await get_game_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="game session not found")
    row["turns"] = await get_game_turns(session_id)
    return create_response(True, row, "ok")


@router.put("/sessions/{session_id}")
async def update_session(session_id: str, body: GameSessionUpdateBody):
    from memory.database import get_game_session, update_game_session

    if not await get_game_session(session_id):
        raise HTTPException(status_code=404, detail="game session not found")
    fields = body.dict(exclude_unset=True)
    if "state_mode" in fields and fields["state_mode"] not in {"per_turn", "on_end"}:
        raise HTTPException(status_code=400, detail="state_mode must be per_turn or on_end")
    row = await update_game_session(session_id, **fields)
    return create_response(True, row, "updated")


@router.put("/sessions/{session_id}/state")
async def update_state(session_id: str, body: GameStateUpdateBody):
    from memory.database import get_game_session, update_game_session_state

    if not await get_game_session(session_id):
        raise HTTPException(status_code=404, detail="game session not found")
    await update_game_session_state(session_id, body.state_json)
    return create_response(True, {"id": session_id, "state_json": body.state_json}, "updated")


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, body: GameEndBody):
    from memory.database import end_game_session, get_active_game_session_id, get_game_session, set_active_game_session_id

    if not await get_game_session(session_id):
        raise HTTPException(status_code=404, detail="game session not found")
    await end_game_session(session_id, body.summary, body.state_json)
    if await get_active_game_session_id() == session_id:
        await set_active_game_session_id(None)
    return create_response(True, {"id": session_id}, "ended")


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    from memory.database import delete_game_session, get_active_game_session_id, set_active_game_session_id

    deleted = await delete_game_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="game session not found")
    if await get_active_game_session_id() == session_id:
        await set_active_game_session_id(None)
    return create_response(True, {"id": session_id}, "deleted")


@router.get("/active")
async def get_active():
    from memory.database import get_active_game_session_id

    return create_response(True, {"session_id": await get_active_game_session_id()}, "ok")


@router.put("/active")
async def set_active(body: ActiveGameBody):
    from memory.database import get_game_session, set_active_game_session_id

    if body.session_id:
        session = await get_game_session(body.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="game session not found")
        if session.get("ended_at"):
            raise HTTPException(status_code=400, detail="game session already ended")
    await set_active_game_session_id(body.session_id)
    return create_response(True, {"session_id": body.session_id}, "updated")


@router.get("/sessions/{session_id}/turns")
async def get_turns(session_id: str, limit: Optional[int] = None):
    from memory.database import get_game_session, get_game_turns

    if not await get_game_session(session_id):
        raise HTTPException(status_code=404, detail="game session not found")
    return create_response(True, await get_game_turns(session_id, limit=limit), "ok")


@router.post("/sessions/{session_id}/turns")
async def append_turn(session_id: str, body: GameTurnBody):
    from memory.database import add_game_turn, get_game_session, get_latest_turn_idx

    if not await get_game_session(session_id):
        raise HTTPException(status_code=404, detail="game session not found")
    turn_idx = await get_latest_turn_idx(session_id) + 1
    turn_id = await add_game_turn(session_id, turn_idx, body.turn_data)
    return create_response(True, {"id": turn_id, "session_id": session_id, "turn_idx": turn_idx}, "created")


@router.put("/turns/{turn_id}")
async def update_turn(turn_id: int, body: GameTurnBody):
    from memory.database import update_game_turn

    row = await update_game_turn(turn_id, body.turn_data)
    if not row:
        raise HTTPException(status_code=404, detail="game turn not found")
    return create_response(True, row, "updated")


@router.delete("/turns/{turn_id}")
async def delete_turn(turn_id: int):
    from memory.database import delete_game_turn

    deleted = await delete_game_turn(turn_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="game turn not found")
    return create_response(True, {"id": turn_id}, "deleted")
