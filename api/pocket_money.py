"""
零花钱记账模块 API。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.stream import EventType, publish_event

router = APIRouter()


def create_response(success: bool, data: Any = None, message: str = "") -> Dict[str, Any]:
    return {"success": success, "data": data, "message": message}


class TransactionCreateBody(BaseModel):
    amount: float = Field(..., gt=0)
    type: str
    income_category: Optional[str] = None
    expense_category: Optional[str] = None
    love_sub_category: Optional[str] = None
    note: Optional[str] = None
    timestamp: Optional[str] = None
    requested_by_ai: bool = False
    pending_approval_id: Optional[int] = None


class PocketMoneyConfigUpdateBody(BaseModel):
    monthly_allowance: Optional[float] = Field(default=None, ge=0)
    annual_interest_rate: Optional[float] = Field(default=None, ge=0)


def _validate_type(value: str) -> str:
    tx_type = (value or "").strip().lower()
    if tx_type not in {"income", "expense"}:
        raise HTTPException(status_code=400, detail="type must be income or expense")
    return tx_type


def _to_decimal(value: float) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="invalid amount")


@router.get("/state")
async def get_state():
    from memory.database import get_current_pocket_money_balance, get_pocket_money_config

    balance = await get_current_pocket_money_balance()
    config = await get_pocket_money_config()
    return create_response(True, {"pocketMoney": float(balance), "config": config}, "ok")


@router.get("/transactions")
async def get_transactions(limit: int = 100, offset: int = 0):
    from memory.database import list_pocket_money_transactions

    rows = await list_pocket_money_transactions(limit=max(1, limit), offset=max(0, offset))
    return create_response(True, rows, "ok")


@router.post("/transactions")
async def create_transaction(body: TransactionCreateBody):
    from memory.database import create_pocket_money_transaction

    tx_type = _validate_type(body.type)
    amount = _to_decimal(body.amount)
    row = await create_pocket_money_transaction(
        amount=amount,
        tx_type=tx_type,
        income_category=body.income_category,
        expense_category=body.expense_category,
        love_sub_category=body.love_sub_category,
        note=body.note,
        timestamp_iso=body.timestamp,
        requested_by_ai=bool(body.requested_by_ai),
        pending_approval_id=body.pending_approval_id,
    )
    await publish_event(EventType.STATUS_UPDATE, {"pocketMoney": float(row["balance_after"])})
    return create_response(True, row, "created")


@router.delete("/transactions/{tx_id}")
async def delete_transaction(tx_id: int):
    from memory.database import delete_pocket_money_transaction

    deleted, balance = await delete_pocket_money_transaction(tx_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="transaction not found")
    await publish_event(EventType.STATUS_UPDATE, {"pocketMoney": float(balance)})
    return create_response(True, {"id": tx_id, "pocketMoney": float(balance)}, "deleted")


@router.put("/config")
async def update_config(body: PocketMoneyConfigUpdateBody):
    from memory.database import update_pocket_money_config

    if body.monthly_allowance is None and body.annual_interest_rate is None:
        raise HTTPException(status_code=400, detail="at least one config field is required")

    result = await update_pocket_money_config(
        monthly_allowance=_to_decimal(body.monthly_allowance) if body.monthly_allowance is not None else None,
        annual_interest_rate=_to_decimal(body.annual_interest_rate) if body.annual_interest_rate is not None else None,
    )
    return create_response(True, result, "updated")
