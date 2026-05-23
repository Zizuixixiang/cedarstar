"""Internal idle wakeup scheduling tool."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytz

from memory.database import get_database


_SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")
_CONFIG_KEY_SET_BY_TOOL = "idle_next_trigger_set_by_tool"


OPENAI_WAKEUP_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "schedule_next_wakeup",
            "description": (
                "预约下次自主唤醒时间。time_hhmm 填北京时间 HH:MM（今天已过则顺延明天）；"
                "delay_minutes 填多少分钟后触发。两者都填时 time_hhmm 优先。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "time_hhmm": {
                        "type": "string",
                        "description": "北京时间 HH:MM，24 小时制；今天已过则顺延明天。",
                    },
                    "delay_minutes": {
                        "type": "integer",
                        "description": "多少分钟后触发。",
                    },
                },
                "required": [],
            },
        },
    }
]


def _format_shanghai(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(_SHANGHAI_TZ)
    return (
        f"{local.year}年{local.month}月{local.day}日 "
        f"{local.hour:02d}:{local.minute:02d}"
    )


async def execute_wakeup_function_call(function_name: str, arguments: Any) -> str:
    if function_name != "schedule_next_wakeup":
        return json.dumps(
            {"success": False, "error": f"Unknown wakeup tool: {function_name}"},
            ensure_ascii=False,
        )

    args = arguments if isinstance(arguments, dict) else {}
    time_hhmm = str(args.get("time_hhmm") or "").strip()
    delay_raw = args.get("delay_minutes")

    try:
        if time_hhmm:
            from bot.idle_activity import (
                _CONFIG_KEY_NEXT_TRIGGER_AT,
                _parse_next_at_tag_to_utc,
            )

            next_utc = _parse_next_at_tag_to_utc(f"[NEXT_AT_{time_hhmm}]")
            if next_utc is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": "time_hhmm 必须是合法的北京时间 HH:MM（00:00-23:59）。",
                    },
                    ensure_ascii=False,
                )
        else:
            if delay_raw is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": "time_hhmm 和 delay_minutes 至少需要传一个。",
                    },
                    ensure_ascii=False,
                )
            try:
                delay_minutes = int(delay_raw)
            except (TypeError, ValueError):
                return json.dumps(
                    {"success": False, "error": "delay_minutes 必须是整数。"},
                    ensure_ascii=False,
                )
            if delay_minutes <= 0:
                return json.dumps(
                    {"success": False, "error": "delay_minutes 必须大于 0。"},
                    ensure_ascii=False,
                )

            from bot.idle_activity import _CONFIG_KEY_NEXT_TRIGGER_AT

            next_utc = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)

        db = get_database()
        await db.set_config(_CONFIG_KEY_NEXT_TRIGGER_AT, next_utc.isoformat())
        await db.set_config(_CONFIG_KEY_SET_BY_TOOL, "true")
        return json.dumps(
            {
                "success": True,
                "scheduled_at_beijing": _format_shanghai(next_utc),
                "scheduled_at_utc": next_utc.isoformat(),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
