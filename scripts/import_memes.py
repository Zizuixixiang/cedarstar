"""
从文本批量导入表情包：每行「URL 名称」（空格分隔，URL 在前；亦兼容「名称 URL」）。

流程：下载图 → vision 激活配置多模态描述（失败则记失败文件，不写库）→ SQLite meme_pack → meme_store.add_meme。
失败条目写入与清单同名的 *.import_failed.txt（含原行与错误说明）。

用法（项目根 cedarstar/）:
  python scripts/import_memes.py memes.txt
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from config import Platform, config  # noqa: E402
from llm.llm_interface import LLMInterface, build_user_multimodal_content  # noqa: E402
from memory.database import (  # noqa: E402
    VISION_FAIL_CAPTION_SHORT,
    VISION_FAIL_CAPTION_TIMEOUT,
    get_database,
)
from memory.meme_store import get_meme_store  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

VISION_PROMPT = """用一句话描述这张表情包的内容和情绪：

要求：
- 必须同时考虑：画面主体（明确物种，比如白狗，不确定时统一用动物指代） + 动作 + 表情 + 图片上的文字内容
- 如果图片中有文字，优先结合文字理解整体含义
- 可以适度概括含义，但必须基于画面和文字，不得脱离

规则：
- 不要编造画面中不存在的角色或元素
- 不要添加额外背景故事（如未出现的人物关系）
- 可以用简短概括词（如"求饶""狡辩"），但要有画面或文字依据

输出：
- 中文
- 20~30字以内"""
_FAIL_CAPTIONS = frozenset({VISION_FAIL_CAPTION_SHORT, VISION_FAIL_CAPTION_TIMEOUT})
_CONCURRENCY = 10


class MemeVisionDescribeError(RuntimeError):
    """视觉描述不可用（空、失败占位或 API 异常），本条应进 import_failed 且不落库。"""


def _is_animated_url(url: str) -> int:
    path = (urlparse(url).path or "").lower()
    if path.endswith(".gif") or path.endswith(".webp"):
        return 1
    return 0


def _guess_mime(url: str, content_type: Optional[str]) -> str:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct.startswith("image/"):
            return ct
    path = (urlparse(url).path or "").lower()
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return "image/jpeg"
    return "image/jpeg"


def _httpx_async_client_kwargs(timeout: httpx.Timeout) -> dict:
    """httpx 0.28+ 移除 AsyncClient(proxies=...)，改用 proxy 或 mounts。"""
    kw: dict = {"timeout": timeout, "follow_redirects": True}
    pd = config.proxy_dict
    if not pd:
        return kw
    http_p = pd.get("http")
    https_p = pd.get("https")
    if http_p and https_p and http_p != https_p:
        kw["mounts"] = {
            "http://": httpx.AsyncHTTPTransport(proxy=http_p),
            "https://": httpx.AsyncHTTPTransport(proxy=https_p),
        }
    else:
        p = https_p or http_p
        if p:
            kw["proxy"] = p
    return kw


async def _download_image(url: str) -> Tuple[bytes, str]:
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(**_httpx_async_client_kwargs(timeout)) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        mime = _guess_mime(url, resp.headers.get("content-type"))
        return resp.content, mime


def _vision_describe_sync(image_b64: str, mime: str, list_name: str) -> str:
    """vision 激活配置 + 多模态；失败抛 MemeVisionDescribeError（不写 SQLite/Chroma）。"""
    try:
        llm = LLMInterface(config_type="vision")
        images = [{"data": image_b64, "mime_type": mime}]
        content = build_user_multimodal_content(
            llm.api_base,
            llm.model_name,
            VISION_PROMPT,
            images,
        )
        messages = [{"role": "user", "content": content}]
        llm_resp = llm.generate_with_context_and_tracking(
            messages,
            platform=Platform.SYSTEM,
        )
        text = (llm_resp.content or "").strip()
        if not text:
            raise MemeVisionDescribeError(
                f"视觉返回空文本（清单名={list_name!r}）"
            )
        if text in _FAIL_CAPTIONS:
            raise MemeVisionDescribeError(
                f"视觉返回失败占位: {text!r}（清单名={list_name!r}）"
            )
        return text
    except MemeVisionDescribeError:
        raise
    except Exception as e:
        logger.warning("视觉描述异常: 清单名=%s — %s", list_name[:40], e)
        raise MemeVisionDescribeError(
            f"视觉 API 异常（清单名={list_name!r}）: {e}"
        ) from e


def _append_fail_record(fail_path: Path, block: str) -> None:
    with open(fail_path, "a", encoding="utf-8") as f:
        f.write(block)
        if not block.endswith("\n"):
            f.write("\n")


async def _process_one(
    sem: asyncio.Semaphore,
    fail_lock: asyncio.Lock,
    fail_path: Path,
    idx: int,
    total: int,
    url: str,
    orig_name: str,
    source_line: str,
) -> bool:
    async with sem:
        try:
            body, mime = await _download_image(url)
            b64 = base64.b64encode(body).decode("ascii")
            loop = asyncio.get_running_loop()
            desc = await loop.run_in_executor(
                None,
                lambda: _vision_describe_sync(b64, mime, orig_name),
            )
            desc = (desc or "").strip()
            if not desc:
                raise MemeVisionDescribeError("视觉描述去空白后为空")

            isa = _is_animated_url(url)
            db = get_database()
            row_id = db.insert_meme_pack(desc, url, isa)
            if row_id < 0:
                err = "SQLite insert_meme_pack 返回失败"
                logger.error("[%s/%s] %s: %s", idx, total, err, url[:80])
                block = (
                    f"--- [{idx}/{total}] ---\n"
                    f"source_line: {source_line}\n"
                    f"url: {url}\n"
                    f"name: {orig_name}\n"
                    f"error: {err}\n"
                )
                async with fail_lock:
                    await asyncio.to_thread(_append_fail_record, fail_path, block)
                return False

            store = get_meme_store()
            store.add_meme(str(row_id), desc, url, isa, document_text=desc)
            logger.info("[%s/%s] OK id=%s %s", idx, total, row_id, desc[:40])
            return True
        except Exception as e:
            tb = traceback.format_exc()
            err_one = f"{type(e).__name__}: {e}"
            logger.warning("[%s/%s] 跳过: %s — %s", idx, total, url[:80], err_one)
            block = (
                f"--- [{idx}/{total}] ---\n"
                f"source_line: {source_line}\n"
                f"url: {url}\n"
                f"name: {orig_name}\n"
                f"error: {err_one}\n"
                f"traceback:\n{tb}\n"
            )
            async with fail_lock:
                await asyncio.to_thread(_append_fail_record, fail_path, block)
            return False


def _parse_line_url_name(s: str) -> Optional[Tuple[str, str]]:
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        parts = s.split(None, 1)
        url = parts[0]
        name = (parts[1] or "").strip() if len(parts) > 1 else ""
        if not name:
            name = Path(urlparse(url).path).name or "meme"
        return (url, name)
    if "https://" in s:
        i = s.find("https://")
    elif "http://" in s:
        i = s.find("http://")
    else:
        return None
    url = s[i:].split()[0]
    name = s[:i].strip()
    if not name:
        name = Path(urlparse(url).path).name or "meme"
    return (url, name)


def _parse_lines(path: Path) -> list[Tuple[str, str, str]]:
    """返回 (url, name, source_line)。"""
    out: list[Tuple[str, str, str]] = []
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line_no, line in enumerate(raw, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parsed = _parse_line_url_name(s)
        if not parsed:
            logger.warning(
                "第 %s 行无法解析（需含 http(s)://）: %s",
                line_no,
                s[:100],
            )
            continue
        url, name = parsed
        if not url.startswith(("http://", "https://")):
            logger.warning("第 %s 行 URL 无效: %s", line_no, s[:100])
            continue
        out.append((url, name, s))
    return out


def _fail_log_path(list_path: Path) -> Path:
    return list_path.with_name(list_path.stem + ".import_failed.txt")


async def _run(path: Path) -> None:
    items = _parse_lines(path)
    total = len(items)
    if not total:
        logger.error("没有可导入的行")
        return

    fail_path = _fail_log_path(path)
    fail_path.write_text(
        f"# import_memes 失败记录\n# 源清单: {path}\n# 可直接复制 source_line 回清单重试\n\n",
        encoding="utf-8",
    )

    logger.info("共 %s 条，并发 %s，失败将写入 %s", total, _CONCURRENCY, fail_path)
    sem = asyncio.Semaphore(_CONCURRENCY)
    fail_lock = asyncio.Lock()
    tasks = [
        _process_one(
            sem,
            fail_lock,
            fail_path,
            i + 1,
            total,
            url,
            name,
            src,
        )
        for i, (url, name, src) in enumerate(items)
    ]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r)
    fail = total - ok
    logger.info("完成：成功 %s，失败 %s（详见 %s）", ok, fail, fail_path)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="从文本批量导入表情包（vision + SQLite + Chroma）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/import_memes.py memes.txt"
        ),
    )
    ap.add_argument(
        "txt_file",
        nargs="?",
        type=Path,
        default=None,
        help="清单路径",
    )
    args = ap.parse_args()
    if args.txt_file is None:
        ap.print_help()
        print(
            "\n请指定清单，例如: python scripts/import_memes.py memes.txt",
            file=sys.stderr,
        )
        sys.exit(1)
    p = args.txt_file.resolve()
    if not p.is_file():
        logger.error("文件不存在: %s", p)
        sys.exit(1)
    asyncio.run(_run(p))


if __name__ == "__main__":
    main()
