"""
AI HOT 资讯（aihot.virxact.com）匿名公开 API，供 OpenAI / Gemini 兼容 function calling。

公开 JSON 形态（2026-05 线上，供格式化与排错）：

- ``GET /api/public/items``：``{ count, hasNext, nextCursor, items[] }``；
  每条含 ``id, title, url, source`` 及可选 ``summary, publishedAt, category`` 等。
- ``GET /api/public/daily`` 与 ``/daily/{date}``：``date, generatedAt, windowStart, windowEnd, lead``，
  ``sections[]`` 为 ``{ label, items[] }``，条目为 ``title, summary, sourceUrl, sourceName``；
  另有 ``flashes[]``（可为空）。
- ``GET /api/public/dailies``：``{ count, items[] }``，元素含 ``date, generatedAt, leadTitle, leadParagraph``。
  客户端对 ``take`` 做硬上限（见 ``_DAILIES_TAKE_MAX``），未传时带默认 ``take``，避免一次拉过长列表。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

AIHOT_PUBLIC_BASE = "https://aihot.virxact.com/api/public"
_DEFAULT_TIMEOUT = 30.0
_MAX_ITEM_LINES = 10
# daily / daily_by_date 格式化正文总长度上限（中文字符量级；过大易撑爆工具回传上下文）。
_MAX_DAILY_BODY_CHARS = 10000
_MAX_SUMMARY_PER_LINE = 400
# /dailies 的 take：未传时用默认，传入时硬夹紧，避免一次拉过多归档行。
_DAILIES_TAKE_DEFAULT = 10
_DAILIES_TAKE_MAX = 15


def _norm_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


async def call_aihot(
    action: str,
    mode: str = "selected",
    since: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    date: Optional[str] = None,
    take: Optional[int] = None,
) -> Any:
    """
    按 action 请求 AI HOT 公开 API，成功时返回解析后的 JSON（dict / list），
    失败时返回 ``{"error": "..."}`` 字典（不抛异常，便于上游统一处理）。
    """
    act = (action or "").strip().lower()
    if act not in ("items", "daily", "daily_by_date", "dailies"):
        return {
            "_cedarstar_aihot_error": True,
            "error": f"无效 action: {action!r}，应为 items|daily|daily_by_date|dailies",
        }

    # 部分网关对 httpx 默认 UA 返回 403，显式声明客户端名。
    headers = {"User-Agent": "CedarStar/1.0"}

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, headers=headers) as client:
            if act == "items":
                params: Dict[str, Any] = {"mode": (mode or "selected").strip() or "selected"}
                s_since = _norm_str(since)
                if s_since:
                    params["since"] = s_since
                cat = _norm_str(category)
                if cat:
                    params["category"] = cat
                qq = _norm_str(q)
                if qq:
                    params["q"] = qq
                r = await client.get(f"{AIHOT_PUBLIC_BASE}/items", params=params)
            elif act == "daily":
                r = await client.get(f"{AIHOT_PUBLIC_BASE}/daily")
            elif act == "daily_by_date":
                d = _norm_str(date)
                if not d:
                    return {
                        "_cedarstar_aihot_error": True,
                        "error": "daily_by_date 需要非空 date（YYYY-MM-DD）",
                    }
                r = await client.get(f"{AIHOT_PUBLIC_BASE}/daily/{d}")
            else:  # dailies
                if take is not None:
                    try:
                        take_n = int(take)
                    except (TypeError, ValueError):
                        return {
                            "_cedarstar_aihot_error": True,
                            "error": "take 必须是整数",
                        }
                    take_n = max(1, min(take_n, _DAILIES_TAKE_MAX))
                else:
                    take_n = _DAILIES_TAKE_DEFAULT
                params2 = {"take": take_n}
                r = await client.get(f"{AIHOT_PUBLIC_BASE}/dailies", params=params2)

            if r.headers.get("content-type", "").startswith("application/json"):
                data = r.json()
            else:
                text = (r.text or "")[:500]
                if r.is_success:
                    return {
                        "_cedarstar_aihot_error": True,
                        "error": f"非 JSON 响应: {text!r}",
                    }
                return {
                    "_cedarstar_aihot_error": True,
                    "error": f"HTTP {r.status_code}: {text}",
                }

            if not r.is_success:
                if isinstance(data, dict) and data.get("error"):
                    merged = dict(data)
                    merged["_cedarstar_aihot_error"] = True
                    return merged
                return {
                    "_cedarstar_aihot_error": True,
                    "error": f"HTTP {r.status_code}",
                    "body": data,
                }

            return data
    except httpx.HTTPError as e:
        logger.warning("call_aihot HTTP 错误 action=%s: %s", act, e)
        return {"_cedarstar_aihot_error": True, "error": str(e)}
    except json.JSONDecodeError as e:
        logger.warning("call_aihot JSON 解析失败 action=%s: %s", act, e)
        return {"_cedarstar_aihot_error": True, "error": f"JSON 解析失败: {e}"}


def _format_items(data: Any) -> str:
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    items = data.get("items")
    if not isinstance(items, list):
        return json.dumps(data, ensure_ascii=False)
    total_hint = data.get("count")
    lines: List[str] = []
    for i, it in enumerate(items[:_MAX_ITEM_LINES], start=1):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("title_en") or "").strip() or "(无标题)"
        source = str(it.get("source") or "").strip() or "(来源未知)"
        url = str(it.get("url") or "").strip()
        lines.append(f"{i}. {title}\n   来源：{source}\n   {url}".strip())
    out = "\n\n".join(lines)
    n = len(items)
    if n > _MAX_ITEM_LINES:
        suffix = f"\n\n（仅展示前 {_MAX_ITEM_LINES} 条"
        if isinstance(total_hint, int):
            suffix += f"，本页或总计相关 count={total_hint}"
        suffix += f"，本条响应共 {n} 条）"
        out = out + suffix
    elif isinstance(total_hint, int) and total_hint != n:
        out = f"{out}\n\n（API count={total_hint}，本批 items 条数={n}）"
    return out or "（无条目）"


def _format_daily_hot_official(data: Dict[str, Any]) -> Optional[str]:
    """
    AI HOT ``/daily``、``/daily/{date}`` 官方结构：顶层 ``sections``，
    分区 ``label`` + ``items``（``title`` / ``summary`` / ``sourceUrl`` / ``sourceName``）。
    不匹配时返回 None，交由其它分支处理。
    """
    sections = data.get("sections")
    if not isinstance(sections, list):
        return None

    lines: List[str] = []
    date_s = str(data.get("date") or "").strip()
    head = f"AI HOT 日报 · {date_s}" if date_s else "AI HOT 日报"
    lines.append(f"【{head}】")

    gen_at = str(data.get("generatedAt") or "").strip()
    if gen_at:
        lines.append(f"生成时间：{gen_at}")

    ws = str(data.get("windowStart") or "").strip()
    we = str(data.get("windowEnd") or "").strip()
    if ws or we:
        lines.append(f"统计窗口：{ws or '?'} → {we or '?'}")

    lead = data.get("lead")
    if isinstance(lead, str) and lead.strip():
        lines.append("")
        lines.append("导读：")
        lines.append(lead.strip()[:4000])

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        label = str(sec.get("label") or "").strip() or "未分类"
        items = sec.get("items")
        if not isinstance(items, list) or not items:
            continue
        lines.append("")
        lines.append(f"「{label}」")
        for it in items:
            if not isinstance(it, dict):
                continue
            tit = str(it.get("title") or "").strip() or "(无标题)"
            summ = str(it.get("summary") or "").strip()
            srcn = str(it.get("sourceName") or "").strip()
            surl = str(it.get("sourceUrl") or it.get("url") or "").strip()
            block_lines = [f"- {tit}"]
            if summ:
                block_lines.append(f"  {summ[:_MAX_SUMMARY_PER_LINE]}")
            src_bits: List[str] = []
            if srcn:
                src_bits.append(srcn)
            if surl:
                src_bits.append(surl)
            if src_bits:
                block_lines.append("  " + " · ".join(src_bits))
            lines.append("\n".join(block_lines))

    flashes = data.get("flashes")
    if isinstance(flashes, list) and flashes:
        lines.append("")
        lines.append("「快讯」")
        for fl in flashes[:30]:
            if isinstance(fl, str) and fl.strip():
                lines.append(f"- {fl.strip()[:_MAX_SUMMARY_PER_LINE]}")
            elif isinstance(fl, dict):
                ft = str(fl.get("title") or "").strip()
                fs = str(fl.get("summary") or "").strip()
                if ft or fs:
                    lines.append(f"- {ft or '(无标题)'}" + (f"\n  {fs[:_MAX_SUMMARY_PER_LINE]}" if fs else ""))

    out = "\n".join(lines).strip()
    if not out:
        return None
    if len(out) > _MAX_DAILY_BODY_CHARS:
        out = out[:_MAX_DAILY_BODY_CHARS] + "…（已截断）"
    return out


def _format_daily_like(data: Any) -> str:
    """将 ``/daily`` / ``/daily/{date}`` 类 JSON 压成可读文本。"""
    if isinstance(data, str) and data.strip():
        s = data.strip()
        return s[:_MAX_DAILY_BODY_CHARS] + ("…（已截断）" if len(s) > _MAX_DAILY_BODY_CHARS else "")
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)

    official = _format_daily_hot_official(data)
    if official is not None:
        return official

    title = "AI HOT 日报"
    for k in ("title", "headline", "subject", "name"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    parts: List[str] = [f"【{title}】"]

    body = data.get("content") or data.get("body") or data.get("markdown")
    if isinstance(body, str) and body.strip():
        b = body.strip()
        if len(b) <= _MAX_DAILY_BODY_CHARS:
            parts.append(b)
        else:
            parts.append(b[:_MAX_DAILY_BODY_CHARS] + "…（已截断）")
        return "\n\n".join(parts)

    entries: List[Any] = []
    for k in ("items", "entries", "highlights", "stories", "news"):
        v = data.get(k)
        if isinstance(v, list) and v:
            entries = v
            break

    if not entries:
        compact = json.dumps(data, ensure_ascii=False)
        if len(compact) > _MAX_DAILY_BODY_CHARS:
            compact = compact[:_MAX_DAILY_BODY_CHARS] + "…"
        parts.append(compact)
        return "\n\n".join(parts)

    lines: List[str] = []
    for j, ent in enumerate(entries, start=1):
        if isinstance(ent, str):
            line = ent.strip()
            if line:
                lines.append(f"{j}. {line[:_MAX_SUMMARY_PER_LINE]}")
            continue
        if not isinstance(ent, dict):
            continue
        st = str(ent.get("title") or ent.get("headline") or "").strip()
        summ = str(
            ent.get("summary") or ent.get("abstract") or ent.get("description") or ""
        ).strip()
        if st and summ:
            lines.append(f"{j}. {st}\n   {summ[:_MAX_SUMMARY_PER_LINE]}")
        elif summ:
            lines.append(f"{j}. {summ[:_MAX_SUMMARY_PER_LINE]}")
        elif st:
            lines.append(f"{j}. {st}")
    joined = "\n\n".join(lines)
    if len(joined) > _MAX_DAILY_BODY_CHARS:
        joined = joined[:_MAX_DAILY_BODY_CHARS] + "…（已截断）"
    parts.append(joined)
    return "\n\n".join(parts)


def _format_dailies(data: Any) -> str:
    """``/dailies``：``{ count, items: [{ date, leadTitle, generatedAt, ... }] }`` 或列表兜底。"""
    if isinstance(data, dict):
        items = data.get("items") or data.get("dailies") or data.get("dates")
        if isinstance(items, list) and items:
            has_archive_shape = any(
                isinstance(it, dict) and ("leadTitle" in it or "leadParagraph" in it)
                for it in items
            )
            if has_archive_shape:
                lines = ["日报归档（日期 — 头条）："]
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    ds = str(it.get("date") or "").strip()
                    lt = str(it.get("leadTitle") or "").strip()
                    lp = str(it.get("leadParagraph") or "").strip()
                    ga = str(it.get("generatedAt") or "").strip()
                    seg = f"- {ds}" if ds else "-"
                    if lt:
                        seg += f" — {lt}"
                    if lp:
                        seg += f"\n  {lp[:240]}"
                    if ga and not lp:
                        seg += f"  ({ga})"
                    lines.append(seg)
                return "\n".join(lines)
            return _format_dailies(items)

    out_lines: List[str] = []
    if isinstance(data, list):
        for x in data:
            if isinstance(x, dict):
                d = x.get("date") or x.get("batchDate") or x.get("publishedAt")
                lt = x.get("leadTitle")
                if d is not None or (isinstance(lt, str) and lt.strip()):
                    seg = f"- {str(d).strip()[:32]}" if d is not None else "-"
                    if isinstance(lt, str) and lt.strip():
                        seg += f" — {lt.strip()}"
                    out_lines.append(seg)
            elif isinstance(x, str) and x.strip():
                out_lines.append(f"- {x.strip()[:32]}")

    if not out_lines:
        return json.dumps(data, ensure_ascii=False)
    return "日报归档：\n" + "\n".join(out_lines)


def format_aihot_tool_text(action: str, data: Any) -> str:
    """将 ``call_aihot`` 的 JSON 转为给主模型阅读的纯文本。"""
    act = (action or "").strip().lower()
    if isinstance(data, dict) and data.get("_cedarstar_aihot_error"):
        err = data.get("error")
        return f"请求失败：{err}"

    if act == "items":
        return _format_items(data)
    if act in ("daily", "daily_by_date"):
        if isinstance(data, list):
            return _format_daily_like({"items": data})
        return _format_daily_like(data)
    if act == "dailies":
        return _format_dailies(data)
    return json.dumps(data, ensure_ascii=False)


async def execute_get_ai_news_function_call(function_name: str, arguments: Any) -> str:
    """
    执行 ``get_ai_news``：拉取 AI HOT 数据并格式化为摘要文本。
    返回 JSON 字符串（``summary`` 字段），与其它只读工具一致。
    """
    if function_name != "get_ai_news":
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

    action = _norm_str(args.get("action")) or ""
    mode = str(args.get("mode") or "selected").strip() or "selected"
    since = _norm_str(args.get("since"))
    category = _norm_str(args.get("category"))
    q = _norm_str(args.get("q"))
    date = _norm_str(args.get("date"))
    take: Optional[int] = None
    if args.get("take") is not None:
        try:
            take = int(args["take"])
        except (TypeError, ValueError):
            return json.dumps({"error": "take 必须是整数"}, ensure_ascii=False)

    try:
        raw: Any = await call_aihot(
            action=action,
            mode=mode,
            since=since,
            category=category,
            q=q,
            date=date,
            take=take,
        )
        if isinstance(raw, dict) and raw.get("_cedarstar_aihot_error"):
            return json.dumps(
                {"error": str(raw.get("error") or "未知错误").strip()},
                ensure_ascii=False,
            )
        text = format_aihot_tool_text(action, raw)
        return json.dumps({"summary": text}, ensure_ascii=False)
    except Exception as e:
        logger.warning("get_ai_news 执行失败: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
