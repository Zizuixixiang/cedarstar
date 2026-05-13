#!/usr/bin/env python3
"""连接 rcommunity MCP（SSE + query token），打印 list_tools 结果。需环境变量 RCOMMUNITY_MCP_TOKEN。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 项目根
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


async def main() -> None:
    token = (os.getenv("RCOMMUNITY_MCP_TOKEN") or "").strip()
    if not token:
        print("请设置环境变量 RCOMMUNITY_MCP_TOKEN（或写入 .env）", file=sys.stderr)
        sys.exit(1)
    base = (os.getenv("RCOMMUNITY_MCP_BASE_URL") or "").strip().rstrip("/")
    if not base:
        base = "https://rcommunity-v2.rhysen.love/mcp"
    sse_url = f"{base}?token={token}"

    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(
        sse_url,
        timeout=120.0,
        sse_read_timeout=300.0,
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.list_tools()
            tools = getattr(res, "tools", None) or []
            out = []
            for t in tools:
                if hasattr(t, "model_dump"):
                    out.append(t.model_dump(mode="json"))
                else:
                    name = getattr(t, "name", "") or ""
                    out.append(
                        {
                            "name": name,
                            "description": getattr(t, "description", "") or "",
                        }
                    )
            print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
