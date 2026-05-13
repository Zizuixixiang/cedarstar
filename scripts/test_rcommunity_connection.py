#!/usr/bin/env python3
"""
独立探测 rcommunity 论坛 MCP（**Streamable HTTP** + query token）：initialize、list_tools。

用法（与主服务一致，读项目根 ``.env``）::

    /opt/cedarstar/venv/bin/python scripts/test_rcommunity_connection.py

依赖环境变量 ``RCOMMUNITY_MCP_TOKEN``；可选 ``RCOMMUNITY_MCP_BASE_URL``（默认
``https://rcommunity-v2.rhysen.love/mcp``）。若脚本整体超时或卡在 list_tools，
多为 URL、token 或站方无响应。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


async def _inner() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from tools.rcommunity import (
        RCOMMUNITY_MCP_HTTP_TIMEOUT_SEC,
        RCOMMUNITY_MCP_INIT_TIMEOUT_SEC,
        RCOMMUNITY_MCP_STREAM_READ_TIMEOUT_SEC,
    )

    token = (os.getenv("RCOMMUNITY_MCP_TOKEN") or "").strip()
    if not token:
        print("缺少环境变量 RCOMMUNITY_MCP_TOKEN（可在 .env 中配置）", file=sys.stderr)
        sys.exit(1)

    base = (os.getenv("RCOMMUNITY_MCP_BASE_URL") or "").strip().rstrip("/")
    if not base:
        base = "https://rcommunity-v2.rhysen.love/mcp"
    url = f"{base}?token={token}"
    redacted = url.split("token=")[0] + "token=<redacted>"
    print("MCP URL:", redacted)

    async with streamablehttp_client(
        url,
        headers=None,
        timeout=RCOMMUNITY_MCP_HTTP_TIMEOUT_SEC,
        sse_read_timeout=RCOMMUNITY_MCP_STREAM_READ_TIMEOUT_SEC,
        terminate_on_close=True,
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(
                session.initialize(),
                timeout=RCOMMUNITY_MCP_INIT_TIMEOUT_SEC,
            )
            res = await asyncio.wait_for(session.list_tools(), timeout=45.0)
            tools = getattr(res, "tools", None) or []
            names = [getattr(t, "name", "") or "?" for t in tools]
            print("工具名:", json.dumps(names, ensure_ascii=False))
            for t in tools:
                name = getattr(t, "name", "") or ""
                schema = getattr(t, "inputSchema", None)
                if schema is None and hasattr(t, "model_dump"):
                    d = t.model_dump(mode="json")
                    schema = d.get("inputSchema")
                print(f"\n=== {name} inputSchema ===")
                print(json.dumps(schema or {}, ensure_ascii=False, indent=2))


def main() -> None:
    try:
        asyncio.run(asyncio.wait_for(_inner(), timeout=90.0))
    except asyncio.TimeoutError:
        print(
            "整体超时：请检查网络、RCOMMUNITY_MCP_BASE_URL 与 token。",
            file=sys.stderr,
        )
        sys.exit(2)
    except asyncio.CancelledError:
        raise
    except BaseException as e:
        try:
            from exceptiongroup import BaseExceptionGroup as EBG
        except ImportError:
            EBG = ()  # type: ignore[misc, assignment]
        if EBG and isinstance(e, EBG):
            print(
                "MCP 返回 ExceptionGroup（常见于 Streamable HTTP 清理或建连失败）；"
                "请核对 ``RCOMMUNITY_MCP_BASE_URL`` 与 token。",
                file=sys.stderr,
            )
            for i, sub in enumerate(e.exceptions, 1):
                print(f"  --- 子异常 {i} ---", file=sys.stderr)
                traceback.print_exception(
                    type(sub), sub, sub.__traceback__, file=sys.stderr
                )
            sys.exit(4)
        print(f"失败: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(3)


if __name__ == "__main__":
    main()
