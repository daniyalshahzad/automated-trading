"""trading.py — Halt control and order execution."""
 
import threading
from typing import Optional
from fastapi import HTTPException
from . import ibeam
 
# ── Halt state ────────────────────────────────────────────────
 
_halted = False
_reason: Optional[str] = None
_lock   = threading.Lock()
 
 
def is_halted() -> tuple[bool, Optional[str]]:
    with _lock:
        return _halted, _reason
 
 
def halt(reason: str = None):
    global _halted, _reason
    with _lock:
        _halted = True
        _reason = reason or "Halted by API"
 
 
def resume():
    global _halted, _reason
    with _lock:
        _halted = False
        _reason = None
 
 
# ── Order execution ───────────────────────────────────────────
 
def execute(account_id: str, orders: list[dict], enable_trading: bool) -> tuple[list, list]:
    """
    Place a list of orders via IBeam.
    Each order dict: symbol, side, order_type, tif, quantity, limit_price, client_order_id
    Returns (placed, warnings).
    """
    halted, reason = is_halted()
    if halted:
        raise HTTPException(403, detail=f"Trading halted: {reason}")
    if not enable_trading:
        raise HTTPException(403, detail="Trading disabled — set ENABLE_TRADING=true in .env")
 
    placed   = []
    warnings = []
 
    for o in orders:
        symbol = o["symbol"].upper()
 
        # Resolve conId
        contract = ibeam.search_contract(symbol)
        if not contract or not contract.get("conId"):
            raise HTTPException(400, detail=f"Could not resolve contract for {symbol}")
        con_id = int(contract["conId"])
 
        response = ibeam.place_order(
            account_id      = account_id,
            con_id          = con_id,
            side            = o["side"],
            quantity        = float(o["quantity"]),
            order_type      = o.get("order_type", "MKT"),
            limit_price     = o.get("limit_price"),
            tif             = o.get("tif", "DAY"),
            client_order_id = o.get("client_order_id"),
        )
 
        placed.append({
            "clientOrderId": o.get("client_order_id"),
            "symbol":        symbol,
            "conId":         con_id,
            "orderId":       response.get("order_id") or response.get("orderId") or "unknown",
            "permId":        response.get("perm_id")  or response.get("permId"),
            "status":        response.get("order_status") or response.get("status") or "Submitted",
            "action":        o["side"].upper(),
            "orderType":     o.get("order_type", "MKT"),
            "totalQuantity": float(o["quantity"]),
            "tif":           o.get("tif", "DAY"),
            "lmtPrice":      o.get("limit_price"),
        })
 
    return placed, warnings