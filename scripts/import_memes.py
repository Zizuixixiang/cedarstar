"""
从文本批量导入表情包：每行「URL 名称」（空格分隔，URL 在前；亦兼容「名称 URL」）。

流程：下载图 → vision 激活配置多模态描述（失败则记失败文件，不写库）→ PostgreSQL ``meme_pack`` 表
（短 ``name`` 可重复；``url`` 已在 PG 则不调 vision；若 Chroma 缺该 ``id`` 则用库内描述补向量）
→ 否则 vision → PG upsert → ``add_meme_async``。
失败条目写入与清单同名的 *.import_failed.txt（含原行与错误说明）。

用法（项目根 cedarstar/）:
  python scripts/import_memes.py memes.txt

启动时会 ``await initialize_database()`` 建立 asyncpg 连接池；请勿在未配置 ``DATABASE_URL`` 的环境下运行。

默认并发 5 并带 429 退避重试；仍限流时可调小 ``_CONCURRENCY`` 或稍后再跑。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
import traceback
from pathlib import Path
from typing import Literal, Optional, Tuple
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
    initialize_database,
)
from memory.meme_store import get_meme_store  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

VISION_PROMPT = """用一句话描述这张表情包的内容和情绪：

要求：
- 必须同时考虑：画面主体（明确主体物种，比如白狗。图片的主角基本都是白狗，画面生物有任何不确定的情况都当白狗算） + 动作 + 表情 + 图片上的文字内容
- 如果图片中有文字，优先结合文字理解整体含义
- 可以适度概括含义，但必须基于画面和文字，不得脱离

规则：
- 不要编造画面中不存在的角色或元素
- 不要添加额外背景故事（如未出现的人物关系）
- 可以用简短概括词（如"求饶""狡辩"），但要有画面或文字依据

输出：
- 中文
- 20~40字以内"""


def _vision_user_text_with_label(short_name: str) -> str:
    """在固定任务说明前附上清单短名称，供模型参考系列/主题；强调仍以画面为准。"""
    label = (short_name or "").strip()
    if not label:
        return VISION_PROMPT
    return (
        "【用户提供的短名称/标签】\n"
        f"「{label}」\n"
        "（可作系列、主题或情绪方向的参考；最终描述必须依据图中实际画面与图上文字，"
        "不要只用名称代替观察；若与画面明显不符，以画面为准。）\n\n"
        f"{VISION_PROMPT}"
    )


_FAIL_CAPTIONS = frozenset({VISION_FAIL_CAPTION_SHORT, VISION_FAIL_CAPTION_TIMEOUT})
# 视觉 API 常按 QPS 限流；过大会批量 429（见 memes.import_failed.txt 中 Too Many Requests）
_CONCURRENCY = 5
_VISION_429_MAX_ATTEMPTS = 8
_VISION_429_BACKOFF_START_SEC = 2.0
_VISION_429_BACKOFF_MAX_SEC = 90.0


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


async def _vision_describe(image_b64: str, mime: str, short_name: str) -> str:
    """
    使用数据库中 **config_type=vision 且 is_active=1** 的 API 配置（与 Bot 贴纸识图一致）。

    须在协程内调用 ``await LLMInterface.create(config_type="vision")``；若在线程池里直接
    ``LLMInterface(config_type="vision")``，则无法读库，会退回 .env 的 LLM_*（易误用纯文本模型）。
    """
    try:
        llm = await LLMInterface.create(config_type="vision")
        image_payloads = [{"data": image_b64, "mime_type": mime}]
        user_text = _vision_user_text_with_label(short_name)
        content = build_user_multimodal_content(
            llm.api_base,
            llm.model_name,
            user_text,
            image_payloads,
        )
        messages = [{"role": "user", "content": content}]

        def _call_sync() -> str:
            llm_resp = llm.generate_with_context_and_tracking(
                messages,
                platform=Platform.SYSTEM,
            )
            text = (llm_resp.content or "").strip()
            if not text:
                raise MemeVisionDescribeError(
                    f"视觉返回空文本（短名称={short_name!r}）"
                )
            if text in _FAIL_CAPTIONS:
                raise MemeVisionDescribeError(
                    f"视觉返回失败占位: {text!r}（短名称={short_name!r}）"
                )
            return text

        delay = _VISION_429_BACKOFF_START_SEC
        for attempt in range(_VISION_429_MAX_ATTEMPTS):
            try:
                return await asyncio.to_thread(_call_sync)
            except MemeVisionDescribeError:
                raise
            except Exception as e:
                es = str(e)
                is_429 = "429" in es or "Too Many Requests" in es
                if not is_429 or attempt + 1 >= _VISION_429_MAX_ATTEMPTS:
                    logger.warning(
                        "视觉描述异常: 短名称=%s — %s", short_name[:40], e
                    )
                    raise MemeVisionDescribeError(
                        f"视觉 API 异常（短名称={short_name!r}）: {e}"
                    ) from e
                logger.warning(
                    "视觉 API 429 限流，%.1fs 后重试 (%s/%s) %s",
                    delay,
                    attempt + 1,
                    _VISION_429_MAX_ATTEMPTS,
                    short_name[:40],
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, _VISION_429_BACKOFF_MAX_SEC)
    except MemeVisionDescribeError:
        raise
    except Exception as e:
        logger.warning("视觉描述异常: 短名称=%s — %s", short_name[:40], e)
        raise MemeVisionDescribeError(
            f"视觉 API 异常（短名称={short_name!r}）: {e}"
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
) -> Literal["ok", "skip", "chroma_backfill", "fail"]:
    async with sem:
        try:
            db = get_database()
            existing = await db.fetch_meme_pack_by_url(url)
            if existing is not None:
                store = get_meme_store()
                row_id = int(existing["id"])
                mid = str(row_id)
                if store.has_meme_id(mid):
                    logger.info(
                        "[%s/%s] 跳过（url 已在库且 Chroma 已有）id=%s %s",
                        idx,
                        total,
                        row_id,
                        (url or "").strip()[:80],
                    )
                    return "skip"

                desc_pg = existing.get("description")
                doc = (
                    str(desc_pg).strip()
                    if desc_pg is not None and str(desc_pg).strip()
                    else ""
                )
                if not doc:
                    doc = (existing.get("name") or "").strip()
                if not doc:
                    err = (
                        "PostgreSQL 已有该 url 但 description 与 name 皆不可用，无法补 Chroma"
                    )
                    logger.error("[%s/%s] %s: %s", idx, total, err, url[:80])
                    block = (
                        f"--- [{idx}/{total}] ---\n"
                        f"source_line: {source_line}\n"
                        f"url: {url}\n"
                        f"name: {orig_name}\n"
                        f"error: {err}\n"
                    )
                    async with fail_lock:
                        await asyncio.to_thread(
                            _append_fail_record, fail_path, block
                        )
                    return "fail"

                await store.upsert_meme_async(
                    mid,
                    (existing.get("name") or "").strip() or "meme",
                    (existing.get("url") or "").strip(),
                    int(existing.get("is_animated") or 0),
                    document_text=doc,
                )
                logger.info(
                    "[%s/%s] 补 Chroma（PG 有、向量缺）id=%s %s",
                    idx,
                    total,
                    row_id,
                    doc[:40],
                )
                return "chroma_backfill"

            body, mime = await _download_image(url)
            b64 = base64.b64encode(body).decode("ascii")
            desc = await _vision_describe(b64, mime, orig_name)
            desc = (desc or "").strip()
            if not desc:
                raise MemeVisionDescribeError("视觉描述去空白后为空")

            isa = _is_animated_url(url)
            row_id = await db.insert_meme_pack(
                orig_name, url, isa, description=desc
            )
            if row_id < 0:
                err = "PostgreSQL meme_pack 按 url upsert 失败（RETURNING id 为空）"
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
                return "fail"

            store = get_meme_store()
            await store.add_meme_async(
                str(row_id), orig_name, url, isa, document_text=desc
            )
            logger.info("[%s/%s] OK id=%s %s", idx, total, row_id, desc[:40])
            return "ok"
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
            return "fail"


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
    await initialize_database()
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
    ok = sum(1 for r in results if r == "ok")
    skipped = sum(1 for r in results if r == "skip")
    backfill = sum(1 for r in results if r == "chroma_backfill")
    fail = sum(1 for r in results if r == "fail")
    logger.info(
        "完成：新导入 %s，跳过（PG+Chroma 已有）%s，补 Chroma %s，失败 %s（失败详见 %s）",
        ok,
        skipped,
        backfill,
        fail,
        fail_path,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="从文本批量导入表情包（vision + PostgreSQL meme_pack + Chroma）",
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
