"""Mail inbox/outbox API."""

from __future__ import annotations

import asyncio
import hmac
import logging
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel

from config import config

logger = logging.getLogger(__name__)

public_router = APIRouter()
router = APIRouter()


class MailOutboxRequest(BaseModel):
    to_addr: str
    to_name: Optional[str] = None
    subject: Optional[str] = None
    body: str


class MailContactRequest(BaseModel):
    name: Optional[str] = None
    email: str
    note: Optional[str] = None


def _response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


def _check_mail_secret(value: Optional[str]) -> None:
    expected = config.MAIL_SECRET
    supplied = (value or "").strip()
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def parse_mime_message(raw: bytes) -> Dict[str, str]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    from_name, from_addr = parseaddr(str(msg.get("from") or ""))
    subject = str(msg.get("subject") or "").strip()
    plain_parts = []
    html_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/plain":
                plain_parts.append(_decode_part(part))
            elif ctype == "text/html":
                html_parts.append(_decode_part(part))
    else:
        ctype = (msg.get_content_type() or "").lower()
        if ctype == "text/html":
            html_parts.append(_decode_part(msg))
        else:
            plain_parts.append(_decode_part(msg))
    body = "\n\n".join(p.strip() for p in plain_parts if p and p.strip())
    if not body:
        body = "\n\n".join(p.strip() for p in html_parts if p and p.strip())
    return {
        "from_addr": from_addr.strip(),
        "from_name": from_name.strip(),
        "subject": subject,
        "body": body.strip(),
    }


async def _generate_mail_summary(body: str, *, subject: str = "", direction: str = "inbox") -> str:
    from memory.micro_batch import SummaryLLMInterface
    from memory.prompt_registry import get_effective_prompt_text

    base_prompt = await get_effective_prompt_text("chunk_summary_private")
    task = (
        f"{base_prompt}\n\n"
        "请为下面这封邮件写 100-200 字中文摘要。必须保留：对方/我说了什么事、"
        "情绪或语气、具体承诺、具体问题或待回复事项。不要编造邮件外信息。"
    )
    summary_llm = await SummaryLLMInterface.create()
    messages = [
        {
            "role": "user" if direction == "inbox" else "assistant",
            "content": f"Subject: {subject}\n\n{body}",
        }
    ]
    return await asyncio.to_thread(
        summary_llm.generate_summary,
        messages,
        "Clio",
        "Correspondent",
        "",
        None,
        None,
        False,
        None,
        "",
        task,
    )


async def summarize_inbox_mail(inbox_id: int, subject: str, body: str) -> None:
    try:
        from memory.database import update_mail_inbox_summary

        summary = await _generate_mail_summary(body, subject=subject, direction="inbox")
        await update_mail_inbox_summary(inbox_id, summary)
    except Exception as e:
        logger.warning("mail inbox summary failed id=%s: %s", inbox_id, e, exc_info=True)


async def summarize_outbox_mail(outbox_id: int, subject: str, body: str) -> None:
    try:
        from memory.database import update_mail_outbox_summary

        summary = await _generate_mail_summary(body, subject=subject, direction="outbox")
        await update_mail_outbox_summary(outbox_id, summary)
    except Exception as e:
        logger.warning("mail outbox summary failed id=%s: %s", outbox_id, e, exc_info=True)


async def send_mail_outbox_via_resend(outbox_id: int) -> None:
    from memory.database import get_mail_outbox

    row = await get_mail_outbox(outbox_id)
    if not row:
        raise ValueError("mail outbox not found")
    api_key = config.RESEND_API_KEY
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    to_addr = str(row.get("to_addr") or "").strip()
    if not to_addr:
        raise ValueError("mail outbox missing to_addr")
    to_name = str(row.get("to_name") or "").strip()
    to_value = f"{to_name} <{to_addr}>" if to_name else to_addr
    payload = {
        "from": config.MAIL_FROM_ADDR,
        "to": [to_value],
        "subject": str(row.get("subject") or ""),
        "text": str(row.get("body") or ""),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend HTTP {resp.status_code}: {resp.text[:500]}")


@public_router.post("/mail/inbox")
async def receive_mail_inbox(
    request: Request,
    background_tasks: BackgroundTasks,
    x_mail_secret: Optional[str] = Header(default=None),
):
    _check_mail_secret(x_mail_secret)
    raw = await request.body()
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid json: {e}") from e
        raw_text = str(payload.get("raw") or payload.get("mime") or payload.get("content") or "")
        raw = raw_text.encode("utf-8")
    try:
        parsed = parse_mime_message(raw)
    except Exception as e:
        logger.warning("parse inbound mail failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail="invalid MIME message") from e
    if not parsed["from_addr"]:
        logger.info("mail inbox ignored: missing from address")
        return _response(True, {"ignored": True}, "missing from address")

    from memory.database import get_mail_contact_by_email, insert_mail_inbox

    contact = await get_mail_contact_by_email(parsed["from_addr"])
    if not contact:
        logger.info("mail inbox ignored: sender not in mail_contacts from=%s", parsed["from_addr"])
        return _response(True, {"ignored": True}, "sender not in contacts")

    inbox_id = await insert_mail_inbox(**parsed)
    background_tasks.add_task(
        summarize_inbox_mail,
        inbox_id,
        parsed.get("subject") or "",
        parsed.get("body") or "",
    )
    try:
        from bot.telegram_notify import send_telegram_text_to_chat

        chat_id = config.TELEGRAM_MAIN_USER_CHAT_ID
        if chat_id:
            name = parsed.get("from_name") or parsed.get("from_addr") or "未知发件人"
            await send_telegram_text_to_chat(
                chat_id,
                f"收到来自 {name} 的新信件：《{parsed.get('subject') or '无主题'}》",
            )
    except Exception as e:
        logger.warning("mail telegram notification failed: %s", e)
    return _response(True, {"id": inbox_id}, "mail received")


@router.post("/outbox")
async def create_mail_outbox(body: MailOutboxRequest):
    from memory.database import insert_mail_outbox, insert_pending_approval

    _, parsed_to = parseaddr(str(body.to_addr or ""))
    if not parsed_to or "@" not in parsed_to:
        return _response(False, None, "to_addr is invalid")
    outbox_id = await insert_mail_outbox(
        to_addr=parsed_to,
        to_name=body.to_name,
        subject=body.subject,
        body=body.body,
        status="pending",
    )
    approval_id = await insert_pending_approval(
        tool_name="send_mail_outbox",
        arguments={"outbox_id": outbox_id},
        arguments_hash=f"mail_outbox:{outbox_id}",
        before_snapshot={},
        after_preview={
            "id": outbox_id,
            "to_addr": parsed_to,
            "to_name": body.to_name,
            "subject": body.subject,
            "body": body.body,
            "status": "pending",
        },
        requested_by_token_hash="internal_ai_tool",
    )
    return _response(
        True,
        {"id": outbox_id, "status": "pending", "approval_id": approval_id},
        "mail queued",
    )


@router.get("/contacts")
async def list_mail_contacts():
    from memory.database import list_mail_contacts as db_list_mail_contacts

    rows = await db_list_mail_contacts()
    return _response(True, rows, "mail contacts loaded")


@router.post("/contacts")
async def create_mail_contact(body: MailContactRequest):
    from memory.database import create_mail_contact as db_create_mail_contact

    _, parsed_email = parseaddr(str(body.email or ""))
    if not parsed_email or "@" not in parsed_email:
        return _response(False, None, "email is invalid")
    row = await db_create_mail_contact(
        name=str(body.name or "").strip() or None,
        email=parsed_email.strip().lower(),
        note=str(body.note or "").strip() or None,
    )
    return _response(True, row, "mail contact saved")


@router.delete("/contacts/{contact_id}")
async def delete_mail_contact(contact_id: int):
    from memory.database import delete_mail_contact as db_delete_mail_contact

    ok = await db_delete_mail_contact(contact_id)
    if not ok:
        return _response(False, None, "mail contact not found")
    return _response(True, {"id": contact_id}, "mail contact deleted")


@router.get("/inbox")
async def list_mail_inbox(limit: int = 100):
    from memory.database import list_mail_inbox as db_list_mail_inbox

    rows = await db_list_mail_inbox(limit=limit)
    return _response(True, rows, "mail inbox loaded")


@router.get("/thread")
async def list_mail_thread(contact_email: Optional[str] = None, limit: int = 100):
    from memory.database import list_mail_thread as db_list_mail_thread

    rows = await db_list_mail_thread(contact_email=contact_email, limit=limit)
    return _response(True, rows, "mail thread loaded")
