"""
小红书：通过本机 ``xhs``（xiaohongshu-cli）调用，Cookie 由 ``XHS_COOKIE_PATH`` 指定。

配额：config 表 ``xhs_read_usage_YYYY-MM-DD`` / ``xhs_write_usage_YYYY-MM-DD``，
上限 ``xhs_daily_read_limit`` / ``xhs_daily_write_limit``（Mini App /api/config）。
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

from config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配额：读 / 写 分计（内存 + DB）
# ---------------------------------------------------------------------------
_read_usage: Dict[str, int] = {}
_write_usage: Dict[str, int] = {}
_home_setup_lock = asyncio.Lock()
_xhs_home_prepared: Optional[str] = None

SORT_CHOICES = ("general", "popular", "latest")
TYPE_CHOICES = ("all", "video", "image")

_XHS_LINK_RE = re.compile(
    r"https?://(?:(?:www|m|creator)\.)?(?:xhslink\.com|xiaohongshu\.com)/\S+",
    re.IGNORECASE,
)

# xhslink 落地页 HTML 中的笔记长链（read 前展开，减轻 CLI 解析短链页失败）
_XHSLINK_DISCOVERY_HREF_RE = re.compile(
    r'href="(https://www\.xiaohongshu\.com/discovery/item/[^"]+)"',
    re.IGNORECASE,
)

_XHSLINK_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def today_key() -> str:
    return date.today().isoformat()


def _read_usage_key() -> str:
    return f"xhs_read_usage_{today_key()}"


def _write_usage_key() -> str:
    return f"xhs_write_usage_{today_key()}"


def _get_read_count() -> int:
    return _read_usage.get(today_key(), 0)


def _get_write_count() -> int:
    return _write_usage.get(today_key(), 0)


async def _sync_read_from_db() -> None:
    k = today_key()
    if _read_usage.get(k):
        return
    try:
        from memory.database import get_database

        raw = await get_database().get_config(_read_usage_key(), "0")
        _read_usage[k] = max(0, int(raw))
    except Exception as e:
        logger.debug("xhs 读配额从 DB 同步失败: %s", e)


async def _sync_write_from_db() -> None:
    k = today_key()
    if _write_usage.get(k):
        return
    try:
        from memory.database import get_database

        raw = await get_database().get_config(_write_usage_key(), "0")
        _write_usage[k] = max(0, int(raw))
    except Exception as e:
        logger.debug("xhs 写配额从 DB 同步失败: %s", e)


async def _inc_read(n: int) -> None:
    if n <= 0:
        return
    k = today_key()
    _read_usage[k] = _read_usage.get(k, 0) + n
    try:
        from memory.database import get_database

        await get_database().set_config(_read_usage_key(), str(_read_usage[k]))
    except Exception as e:
        logger.warning("xhs 读配额写库失败: %s", e)


async def _inc_write(n: int = 1) -> None:
    if n <= 0:
        return
    k = today_key()
    _write_usage[k] = _write_usage.get(k, 0) + n
    try:
        from memory.database import get_database

        await get_database().set_config(_write_usage_key(), str(_write_usage[k]))
    except Exception as e:
        logger.warning("xhs 写配额写库失败: %s", e)


async def _get_read_limit() -> int:
    default = 80
    try:
        from memory.database import get_database

        raw = await get_database().get_config("xhs_daily_read_limit", str(default))
        return max(1, int(raw))
    except Exception:
        return default


async def _get_write_limit() -> int:
    default = 30
    try:
        from memory.database import get_database

        raw = await get_database().get_config("xhs_daily_write_limit", str(default))
        return max(1, int(raw))
    except Exception:
        return default


async def _check_read_quota(need: int) -> Optional[Dict[str, Any]]:
    await _sync_read_from_db()
    limit = await _get_read_limit()
    if _get_read_count() + need > limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
        }
    return None


async def _check_write_quota() -> Optional[Dict[str, Any]]:
    await _sync_write_from_db()
    limit = await _get_write_limit()
    if _get_write_count() >= limit:
        return {
            "success": False,
            "error": "xhs_daily_write_limit_exceeded",
            "limit": limit,
            "used_today": _get_write_count(),
        }
    return None


async def get_xhs_usage_today() -> Dict[str, Any]:
    await _sync_read_from_db()
    await _sync_write_from_db()
    return {
        "date": today_key(),
        "read_used": _get_read_count(),
        "read_limit": await _get_read_limit(),
        "write_used": _get_write_count(),
        "write_limit": await _get_write_limit(),
    }


def _xhs_binary() -> Optional[str]:
    override = (os.environ.get("XHS_CLI_PATH") or "").strip()
    if override and Path(override).is_file():
        return override
    cand = shutil.which("xhs")
    if cand:
        return cand
    here = Path(__file__).resolve().parent.parent / "venv" / "bin" / "xhs"
    if here.is_file():
        return str(here)
    return None


def _cookie_file_path() -> Optional[Path]:
    p = (os.environ.get("XHS_COOKIE_PATH") or "").strip()
    if not p:
        return None
    path = Path(p)
    if path.is_file():
        return path.resolve()
    return None


async def _ensure_xhs_cli_home() -> Optional[str]:
    """
    xiaohongshu-cli 固定读 ``$HOME/.xiaohongshu-cli/cookies.json``。
    为支持 ``XHS_COOKIE_PATH``，为每个 APP_NAME 建临时 HOME，并 symlink cookies。
    """
    global _xhs_home_prepared
    cookie = _cookie_file_path()
    if not cookie:
        return None
    app = (os.environ.get("APP_NAME") or "cedarstar").strip() or "cedarstar"
    async with _home_setup_lock:
        base = Path(tempfile.gettempdir()) / f"xhs_cli_home_{app}"
        cfg = base / ".xiaohongshu-cli"
        cfg.mkdir(parents=True, exist_ok=True)
        link = cfg / "cookies.json"
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            try:
                link.symlink_to(cookie)
            except OSError:
                shutil.copy2(cookie, link)
        except OSError as e:
            logger.warning("准备 xhs cookie 目录失败: %s", e)
            return None
        _xhs_home_prepared = str(base)
        return _xhs_home_prepared


def _parse_cli_stdout(stdout: str) -> Dict[str, Any]:
    s = (stdout or "").strip()
    if not s:
        return {"ok": False, "error": {"code": "empty_output", "message": "empty"}}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": {"code": "invalid_json", "message": s[:400]},
        }


async def _run_xhs_json(argv: List[str], *, timeout: float = 120.0) -> Dict[str, Any]:
    home = await _ensure_xhs_cli_home()
    if not home:
        return {
            "ok": False,
            "error": {
                "code": "no_cookie",
                "message": "XHS_COOKIE_PATH 未设置或文件不存在",
            },
        }
    exe = _xhs_binary()
    if not exe:
        return {
            "ok": False,
            "error": {
                "code": "no_cli",
                "message": "未找到 xhs 可执行文件，请安装 xiaohongshu-cli",
            },
        }
    env = os.environ.copy()
    env["HOME"] = home
    env["OUTPUT"] = "json"
    cmd = [exe, *argv, "--json"]

    def _run() -> Dict[str, Any]:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": {"code": "timeout", "message": "xhs subprocess timeout"},
            }
        except Exception as e:
            return {
                "ok": False,
                "error": {"code": "spawn_error", "message": str(e)},
            }
        merged = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        parsed = _parse_cli_stdout(proc.stdout or "")
        if proc.returncode != 0 and parsed.get("ok") is not False:
            return {
                "ok": False,
                "error": {
                    "code": "cli_exit",
                    "message": merged[:800],
                    "returncode": proc.returncode,
                },
            }
        return parsed

    return await asyncio.to_thread(_run)


def _enrich_xhs_cli_error(err: Any) -> Any:
    """
    将 xiaohongshu-cli 的 JSON 错误补充为可运维的中文说明（不改变原有 code/message）。
    """
    if not isinstance(err, dict):
        return err
    out = dict(err)
    code = str(out.get("code") or "")
    msg = str(out.get("message") or "").lower()
    if code == "not_authenticated" or "re-login" in msg or "session expired" in msg:
        out["hint_zh"] = (
            "CLI 判定未登录或会话已过期：请在部署机执行 `xhs login` 生成官方 "
            "`~/.xiaohongshu-cli/cookies.json`，再同步到环境变量 `XHS_COOKIE_PATH` 指向的文件；"
            "浏览器开发者工具里复制的 Cookie 头通常不能替代该文件。"
        )
        logger.warning(
            "小红书 CLI 未认证或会话过期 (code=%s)，工具调用将失败，需更新 cookies",
            code or "unknown",
        )
    return out


def _envelope_data(payload: Dict[str, Any]) -> Any:
    if not payload.get("ok"):
        err = payload.get("error") or {}
        return {"_error": _enrich_xhs_cli_error(err)}
    return payload.get("data")


# ---------------------------------------------------------------------------
# 笔记 URL / note_id
# ---------------------------------------------------------------------------
async def resolve_xhs_url_to_note_ref(url: str) -> Tuple[str, str]:
    """
    返回 (note_id, xsec_token)。短链会跟随重定向。
    """
    u = (url or "").strip()
    if not u:
        return "", ""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers={
                "user-agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            r = await client.get(u)
            final = str(r.url)
    except Exception as e:
        logger.warning("小红书链接解析 HTTP 失败: %s", e)
        final = u
    return parse_note_url(final)


def parse_note_url(url: str) -> Tuple[str, str]:
    """从最终 URL 解析 (note_id, xsec_token)。"""
    if "xiaohongshu.com" not in url:
        return "", ""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.rstrip("/").split("/") if p]
    note_id = parts[-1] if parts else ""
    qs = parse_qs(parsed.query)
    xsec = (qs.get("xsec_token") or [""])[0]
    if note_id and re.match(r"^[0-9a-zA-Z]+$", note_id):
        return note_id, xsec
    return "", xsec


def find_xhs_urls_in_text(text: str) -> List[str]:
    if not text:
        return []
    return list(dict.fromkeys(_XHS_LINK_RE.findall(text)))


def _normalize_sort(sort_by: Optional[str]) -> str:
    s = (sort_by or "general").strip().lower()
    return s if s in SORT_CHOICES else "general"


def _normalize_type(note_type: Optional[str]) -> str:
    t = (note_type or "all").strip().lower()
    return t if t in TYPE_CHOICES else "all"


def _note_items_from_search_or_feed(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        card = it.get("note_card") if isinstance(it.get("note_card"), dict) else {}
        nid = str(it.get("id") or card.get("note_id") or "").strip()
        if not nid:
            continue
        title = str(card.get("display_title") or it.get("display_title") or "").strip()
        desc = str(card.get("desc") or it.get("desc") or "").strip()
        liked = card.get("liked_count") or it.get("liked_count") or 0
        try:
            liked_n = int(liked)
        except (TypeError, ValueError):
            liked_n = 0
        out.append(
            {
                "note_id": nid,
                "title": title,
                "summary": desc[:200],
                "likes": liked_n,
            }
        )
    return out


def _unwrap_note_detail_dict(data: Any) -> dict:
    """统一为单篇笔记 dict：``note``、``items[0].note_card`` 或已是 note_card。"""
    if not isinstance(data, dict):
        return {}
    inner = data.get("note")
    if isinstance(inner, dict):
        return inner
    items = data.get("items")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            card = first.get("note_card")
            if isinstance(card, dict):
                return card
    return data


def _image_url_from_item(im: Any) -> Optional[str]:
    """从图片项（字符串或 dict）提取 http(s) URL。"""
    if isinstance(im, str):
        s = im.strip()
        return s if s.startswith("http") else None
    if not isinstance(im, dict):
        return None
    for key in (
        "url_default",
        "urlDefault",
        "url_pre",
        "urlPre",
        "url",
        "origin_url",
        "originUrl",
    ):
        u = im.get(key)
        if isinstance(u, str) and u.strip().startswith("http"):
            return u.strip()
    nested = im.get("info_list") or im.get("infoList")
    if isinstance(nested, list):
        for sub in nested:
            u = _image_url_from_item(sub)
            if u:
                return u
    return None


def _extract_note_detail_fields(data: Any) -> Tuple[str, str, List[str]]:
    """标题、正文、图片 URL 列表。"""
    data = _unwrap_note_detail_dict(data)
    if not isinstance(data, dict):
        return "", "", []
    title = str(data.get("title") or data.get("note_title") or "").strip()
    text = str(
        data.get("desc")
        or data.get("description")
        or data.get("content")
        or ""
    ).strip()
    urls: List[str] = []
    for key in ("image_list", "imageList", "images", "pics"):
        lst = data.get(key)
        if not isinstance(lst, list):
            continue
        for im in lst:
            u = _image_url_from_item(im)
            if u:
                urls.append(u)
    seen: set[str] = set()
    uniq: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    logger.info("xhs note detail image_urls_count=%s", len(uniq))
    return title, text, uniq


async def _download_image_b64(url: str) -> Optional[Dict[str, str]]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            raw = r.content
            if len(raw) > 4_000_000:
                return None
    except Exception as e:
        logger.warning("下载小红书图片失败 %s: %s", url[:80], e)
        return None
    ct = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not ct.startswith("image/"):
        ct = "image/jpeg"
    return {"data": base64.b64encode(raw).decode("ascii"), "mime_type": ct}


async def _summarize_xhs_images_with_vision(
    images: List[Dict[str, str]],
    *,
    title: str = "",
    text: str = "",
) -> str:
    """用 vision 配置把小红书配图转成文本，供工具调用链继续理解图片。"""
    if not images:
        return ""
    try:
        from llm.llm_interface import LLMInterface, build_user_multimodal_content

        llm = await LLMInterface.create(config_type="vision")
        prompt_parts = [
            "请阅读这篇小红书笔记的配图，并用中文输出给后续对话模型使用的图片摘要。",
            "要求：逐张说明画面主体、人物/物品、可见文字、颜色与关键信息；最后给一段整体判断。",
            "不要说你无法看到图片；如果某张图信息少，也请如实概括。",
        ]
        if title:
            prompt_parts.append(f"笔记标题：{title[:300]}")
        if text:
            prompt_parts.append(f"笔记正文：{text[:1200]}")
        prompt = "\n".join(prompt_parts)
        vision_images: List[Dict[str, Any]] = []
        for idx, im in enumerate(images, start=1):
            b64 = im.get("data")
            if not isinstance(b64, str) or not b64:
                continue
            vision_images.append(
                {
                    "type": "image",
                    "data": b64,
                    "mime_type": im.get("mime_type") or "image/jpeg",
                    "label": f"小红书笔记配图{idx}",
                }
            )
        if not vision_images:
            return ""
        content = build_user_multimodal_content(
            llm.api_base,
            llm.model_name,
            prompt,
            vision_images,
        )
        messages = [{"role": "user", "content": content}]

        def _call() -> str:
            resp = llm.generate_with_context_and_tracking(messages)
            return resp.content or ""

        summary = (await asyncio.to_thread(_call)).strip()
        if len(summary) > 4000:
            summary = summary[:4000] + "..."
        return summary
    except Exception as e:
        logger.warning("小红书配图 vision 摘要失败: %s", e)
        return ""


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
async def search_xhs(
    keyword: str,
    sort_by: Optional[str] = None,
    note_type: Optional[str] = None,
) -> Dict[str, Any]:
    kw = (keyword or "").strip()
    if not kw:
        return {"success": False, "error": "keyword 不能为空"}
    await _sync_read_from_db()
    limit = await _get_read_limit()
    if _get_read_count() >= limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
        }
    sort = _normalize_sort(sort_by)
    nt = _normalize_type(note_type)
    payload = await _run_xhs_json(["search", kw, "--sort", sort, "--type", nt])
    data = _envelope_data(payload)
    if isinstance(data, dict) and data.get("_error"):
        return {"success": False, "error": data["_error"]}
    items = _note_items_from_search_or_feed(data)
    n = len(items)
    if _get_read_count() + n > limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
            "would_need": n,
        }
    await _inc_read(n)
    return {"success": True, "notes": items, **(await get_xhs_usage_today())}


async def read_xhs_note(
    note_id_or_url: str,
    *,
    max_images: int = 6,
    apply_read_quota: bool = True,
    include_image_data: bool = True,
    summarize_images: bool = True,
) -> Dict[str, Any]:
    """读帖子详情；``note_id_or_url`` 可为笔记 ID 或含 token 的完整 URL。图片为 base64。"""
    arg = (note_id_or_url or "").strip()
    if not arg:
        return {"success": False, "error": "note_id 不能为空"}
    if arg.startswith("http") and re.search(r"xhslink\.com", arg, re.I):
        expanded = await _expand_xhslink_com_to_discovery_url(arg)
        if expanded:
            arg = expanded
    if apply_read_quota:
        qerr = await _check_read_quota(1)
        if qerr:
            return qerr
    payload = await _run_xhs_json(["read", arg])
    data = _envelope_data(payload)
    if isinstance(data, dict) and data.get("_error"):
        return {"success": False, "error": data["_error"]}
    title, text, urls = _extract_note_detail_fields(data)
    if arg.startswith("http"):
        nid, _ = await resolve_xhs_url_to_note_ref(arg)
    else:
        nid = arg
    images_out: List[Dict[str, str]] = []
    for u in urls[:max(0, max_images)]:
        b = await _download_image_b64(u)
        if b:
            images_out.append(b)
    image_summary = ""
    if summarize_images:
        image_summary = await _summarize_xhs_images_with_vision(
            images_out,
            title=title,
            text=text,
        )
    if apply_read_quota:
        await _inc_read(1)
    out: Dict[str, Any] = {
        "success": True,
        "note_id": nid or arg,
        "title": title,
        "text": text,
        "image_count": len(images_out),
        "image_summary": image_summary,
        **(
            await get_xhs_usage_today()
            if apply_read_quota
            else {}
        ),
    }
    if include_image_data:
        out["images"] = images_out
    else:
        out["images_omitted"] = bool(images_out)
    return out


async def _expand_xhslink_com_to_discovery_url(short_url: str) -> Optional[str]:
    """
    将 xhslink.com 短链换为 www.xiaohongshu.com/discovery/item/... 长链。
    失败返回 None（调用方继续用原短链，不阻断）。
    """
    u = (short_url or "").strip()
    if not u or not re.search(r"xhslink\.com", u, re.I):
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": _XHSLINK_MOBILE_UA},
        ) as client:
            r = await client.get(u)
            r.raise_for_status()
    except Exception as e:
        logger.warning(
            "xhslink 短链 HTTP 获取失败，将仍用短链走 read: url=%s err=%s",
            u[:160],
            e,
        )
        return None
    final = str(r.url)
    if re.search(
        r"https?://www\.xiaohongshu\.com/discovery/item/", final, re.I
    ):
        return html.unescape(final)
    m = _XHSLINK_DISCOVERY_HREF_RE.search(r.text or "")
    if m:
        return html.unescape(m.group(1).strip())
    logger.warning(
        "xhslink 短链未能从 HTML 解析出 discovery/item 长链，将仍用短链走 read: url=%s",
        u[:160],
    )
    return None


async def read_xhs_note_from_url(
    url: str,
    *,
    max_images: int = 6,
    apply_read_quota: bool = False,
    include_image_data: bool = True,
    summarize_images: bool = True,
) -> Dict[str, Any]:
    """供 Telegram 链接触发（默认不占日读配额）；短链展开在 ``read_xhs_note`` 内统一处理。"""
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "url 为空"}
    return await read_xhs_note(
        u,
        max_images=max_images,
        apply_read_quota=apply_read_quota,
        include_image_data=include_image_data,
        summarize_images=summarize_images,
    )


async def get_xhs_feed() -> Dict[str, Any]:
    await _sync_read_from_db()
    limit = await _get_read_limit()
    if _get_read_count() >= limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
        }
    payload = await _run_xhs_json(["feed"])
    data = _envelope_data(payload)
    if isinstance(data, dict) and data.get("_error"):
        return {"success": False, "error": data["_error"]}
    items = _note_items_from_search_or_feed(data)
    n = len(items)
    if _get_read_count() + n > limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
            "would_need": n,
        }
    await _inc_read(n)
    return {"success": True, "notes": items, **(await get_xhs_usage_today())}


async def get_xhs_user(user_id: str) -> Dict[str, Any]:
    uid = (user_id or "").strip()
    if not uid:
        return {"success": False, "error": "user_id 不能为空"}
    await _sync_read_from_db()
    limit = await _get_read_limit()
    if _get_read_count() >= limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
        }
    p1 = await _run_xhs_json(["user", uid])
    d1 = _envelope_data(p1)
    if isinstance(d1, dict) and d1.get("_error"):
        return {"success": False, "error": d1["_error"]}
    p2 = await _run_xhs_json(["user-posts", uid])
    d2 = _envelope_data(p2)
    if isinstance(d2, dict) and d2.get("_error"):
        return {"success": False, "error": d2["_error"]}
    user_info: Dict[str, Any] = {}
    if isinstance(d1, dict):
        user_info = {
            "user_id": str(d1.get("user_id") or d1.get("id") or uid),
            "nickname": str(d1.get("nickname") or d1.get("name") or ""),
            "desc": str(d1.get("desc") or d1.get("description") or "")[:500],
            "followers": d1.get("followers") or d1.get("fans_count"),
        }
    notes: List[Dict[str, Any]] = []
    if isinstance(d2, dict):
        raw_notes = d2.get("notes")
        if not isinstance(raw_notes, list) and "items" in d2:
            raw_notes = d2.get("items")
        if isinstance(raw_notes, list):
            for it in raw_notes:
                if not isinstance(it, dict):
                    continue
                card = it.get("note_card") if isinstance(it.get("note_card"), dict) else {}
                nid = str(it.get("id") or card.get("note_id") or "").strip()
                if not nid:
                    continue
                title = str(card.get("display_title") or it.get("display_title") or "").strip()
                desc = str(card.get("desc") or it.get("desc") or "").strip()[:200]
                liked = card.get("liked_count") or it.get("liked_count") or 0
                try:
                    ln = int(liked)
                except (TypeError, ValueError):
                    ln = 0
                notes.append(
                    {
                        "note_id": nid,
                        "title": title,
                        "summary": desc,
                        "likes": ln,
                    }
                )
    count = len(notes) if notes else 1
    if _get_read_count() + count > limit:
        return {
            "success": False,
            "error": "xhs_daily_read_limit_exceeded",
            "limit": limit,
            "used_today": _get_read_count(),
            "would_need": count,
        }
    await _inc_read(count)
    return {
        "success": True,
        "user": user_info,
        "notes": notes,
        **(await get_xhs_usage_today()),
    }


async def like_xhs_note(note_id: str) -> Dict[str, Any]:
    nid = (note_id or "").strip()
    if not nid:
        return {"success": False, "error": "note_id 不能为空"}
    werr = await _check_write_quota()
    if werr:
        return werr
    payload = await _run_xhs_json(["like", nid])
    data = _envelope_data(payload)
    if isinstance(data, dict) and data.get("_error"):
        return {"success": False, "error": data["_error"]}
    if not payload.get("ok"):
        return {"success": False, "error": payload.get("error", "unknown")}
    await _inc_write(1)
    return {"success": True, "note_id": nid, **(await get_xhs_usage_today())}


async def favorite_xhs_note(note_id: str) -> Dict[str, Any]:
    nid = (note_id or "").strip()
    if not nid:
        return {"success": False, "error": "note_id 不能为空"}
    werr = await _check_write_quota()
    if werr:
        return werr
    payload = await _run_xhs_json(["favorite", nid])
    data = _envelope_data(payload)
    if isinstance(data, dict) and data.get("_error"):
        return {"success": False, "error": data["_error"]}
    if not payload.get("ok"):
        return {"success": False, "error": payload.get("error", "unknown")}
    await _inc_write(1)
    return {"success": True, "note_id": nid, **(await get_xhs_usage_today())}


async def execute_xhs_function_call(name: str, arguments: Dict[str, Any]) -> str:
    if not config.ENABLE_XHS_TOOL:
        return json.dumps(
            {
                "success": False,
                "error": "小红书工具已在本部署关闭（ENABLE_XHS_TOOL=false）",
            },
            ensure_ascii=False,
        )
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "search_xhs":
            out = await search_xhs(
                str(args.get("keyword") or ""),
                sort_by=args.get("sort_by"),
                note_type=args.get("note_type"),
            )
        elif name == "read_xhs_note":
            out = await read_xhs_note(
                str(args.get("note_id") or ""),
                include_image_data=False,
            )
        elif name == "get_xhs_feed":
            out = await get_xhs_feed()
        elif name == "get_xhs_user":
            out = await get_xhs_user(str(args.get("user_id") or ""))
        elif name == "like_xhs_note":
            out = await like_xhs_note(str(args.get("note_id") or ""))
        elif name == "favorite_xhs_note":
            out = await favorite_xhs_note(str(args.get("note_id") or ""))
        else:
            out = {"success": False, "error": f"未知工具 {name}"}
    except Exception as e:
        logger.warning("execute_xhs_function_call(%s) 失败: %s", name, e)
        out = {"success": False, "error": str(e)}
    return json.dumps(out, ensure_ascii=False)


async def telegram_append_xhs_note_to_message(
    combined_raw: str,
    combined_content: str,
    images: Optional[List[Dict[str, Any]]],
    text_for_llm: Optional[str],
) -> Tuple[str, str, List[Dict[str, Any]], str]:
    """
    检测小红书链接，拉取首条笔记概要 + 最多 6 张图，追加到 LLM 文本并合并 images。
    失败仅打日志，不抛错。
    """
    if not config.ENABLE_XHS_TOOL:
        imgs = list(images or [])
        base_tfl = (text_for_llm or combined_content or "").strip()
        return (combined_raw or ""), (combined_content or ""), imgs, base_tfl
    imgs = list(images or [])
    base_raw = combined_raw or ""
    base_llm = combined_content or ""
    base_tfl = (text_for_llm or combined_content or "").strip()
    urls = find_xhs_urls_in_text(base_raw) or find_xhs_urls_in_text(base_llm)
    if not urls:
        return base_raw, base_llm, imgs, base_tfl
    url = urls[0]
    try:
        detail = await read_xhs_note_from_url(
            url,
            max_images=6,
            apply_read_quota=False,
            summarize_images=False,
        )
    except Exception as e:
        logger.warning("Telegram 小红书链接预处理失败: %s", e)
        return base_raw, base_llm, imgs, base_tfl
    if not detail.get("success"):
        logger.warning("Telegram 小红书笔记拉取失败: %s", detail.get("error"))
        return base_raw, base_llm, imgs, base_tfl
    nid = str(detail.get("note_id") or "note").strip() or "note"
    title = str(detail.get("title") or "").strip()
    body = str(detail.get("text") or "").strip()
    block = f"\n[小红书笔记] {title}\n{body}".strip()
    new_raw = (base_raw.rstrip() + "\n" + block).strip()
    new_llm = (base_llm.rstrip() + "\n" + block).strip()
    new_tfl = (base_tfl.rstrip() + "\n" + block).strip()
    idx = len(imgs)
    for im in detail.get("images") or []:
        if not isinstance(im, dict):
            continue
        b64 = im.get("data")
        mime = im.get("mime_type") or "image/jpeg"
        if not isinstance(b64, str) or not b64:
            continue
        idx += 1
        imgs.append(
            {
                "type": "image",
                "data": b64,
                "mime_type": mime,
                "caption": f"小红书笔记图{idx}",
                "label": f"小红书笔记配图{idx}",
                "platform_file_id": f"xhs_inline_{nid}_{idx}",
            }
        )
    return new_raw, new_llm, imgs, new_tfl
