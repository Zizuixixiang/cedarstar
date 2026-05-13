"""
OpenAI function calling：抓取 URL 网页正文（trafilatura），供模型阅读用户分享的链接。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

import aiohttp

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 4000
FETCH_TIMEOUT_S = 10
USER_AGENT = (
    "Mozilla/5.0 (compatible; CedarStarBot/1.0) "
    "AppleWebKit/537.36 (KHTML, like Gecko)"
)


async def execute_web_fetch_function_call(function_name: str, arguments: Any) -> str:
    """
    执行 ``web_fetch``；``arguments`` 为 dict 或 JSON 字符串。
    返回 JSON 字符串：成功含 ``text``，失败含 ``error``。
    """
    if function_name != "web_fetch":
        return json.dumps({"error": "未知工具"}, ensure_ascii=False)

    args: Dict[str, Any]
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}

    url = str(args.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "缺少 url 参数"}, ensure_ascii=False)
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return json.dumps({"error": "仅支持 http(s) 协议的 URL"}, ensure_ascii=False)

    try:
        import trafilatura
    except ImportError as e:
        logger.warning("web_fetch 无法加载 trafilatura 依赖: %s", e)
        return json.dumps(
            {
                "error": (
                    "网页正文依赖未就绪（trafilatura 或其子依赖无法导入）。"
                    f"详情：{e}。请在运行环境中安装 trafilatura，并在 lxml≥6 时安装 lxml_html_clean。"
                )
            },
            ensure_ascii=False,
        )

    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
    raw = b""
    enc = "utf-8"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
                raise_for_status=False,
            ) as resp:
                if resp.status >= 400:
                    return json.dumps(
                        {"error": f"HTTP {resp.status}：无法获取页面"},
                        ensure_ascii=False,
                    )
                total = 0
                chunks: list[bytes] = []
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        return json.dumps(
                            {"error": "响应体超过 1MB 限制，已中止"},
                            ensure_ascii=False,
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                try:
                    c_enc = resp.get_encoding()
                    if c_enc:
                        enc = c_enc
                except Exception:
                    pass
    except asyncio.TimeoutError:
        return json.dumps({"error": "请求超时（10 秒）"}, ensure_ascii=False)
    except aiohttp.ClientError as e:
        logger.warning("web_fetch 网络错误 url=%s: %s", url, e)
        return json.dumps({"error": f"抓取失败：{e}"}, ensure_ascii=False)
    except Exception as e:
        logger.warning("web_fetch 请求异常 url=%s: %s", url, e)
        return json.dumps({"error": f"抓取失败：{e}"}, ensure_ascii=False)

    try:
        html = raw.decode(enc, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")

    try:
        extracted = trafilatura.extract(html, url=url)
    except Exception as e:
        logger.warning("trafilatura.extract 失败 url=%s: %s", url, e)
        return json.dumps({"error": f"正文解析失败：{e}"}, ensure_ascii=False)

    text = (extracted or "").strip()
    if not text:
        return json.dumps({"error": "未能从页面提取到可读正文"}, ensure_ascii=False)

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    out: Dict[str, Any] = {"text": text}
    if truncated:
        out["truncated"] = True
        out["note"] = f"正文已截断至前 {MAX_TEXT_CHARS} 字符"
    return json.dumps(out, ensure_ascii=False)
