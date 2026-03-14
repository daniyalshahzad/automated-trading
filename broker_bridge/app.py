"""
IBKR Broker Bridge v3.0 — IBeam
================================
Thin REST API over IBeam Client Portal Gateway.
IBeam must be running on https://localhost:5000.
 
Run:
    uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787
 
.env:
    BRIDGE_TOKEN=secret
    ENABLE_TRADING=true
    DEFAULT_CURRENCY=USD
"""
 
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
 
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
 
from .models import PlaceRequest, PlaceResponse, CancelRequest, TradingHaltRequest
from . import ibeam, trading
 
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
 
BRIDGE_TOKEN   = os.getenv("BRIDGE_TOKEN", "")
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "USD").upper()
 
app = FastAPI(title="IBKR Broker Bridge", version="3.0")
 
# Cache account ID after first successful fetch
_account_id: Optional[str] = None
 
 
def now() -> str:
    return datetime.now(timezone.utc).isoformat()
 
 
def auth(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="Missing bearer token")
    if authorization.split(" ", 1)[1].strip() != BRIDGE_TOKEN:
        raise HTTPException(403, detail="Invalid token")
 
 
def account_id() -> str:
    global _account_id
    if not _account_id:
        _account_id = ibeam.get_account_id()
    return _account_id
 
 
# ── Startup ───────────────────────────────────────────────────
 
@app.on_event("startup")
def startup():
    if ibeam.is_authenticated():
        print(f"IBeam: authenticated — account {ibeam.get_account_id()}")
    else:
        print("IBeam: NOT authenticated — approve 2FA on your phone")
 
 
# ── Health ────────────────────────────────────────────────────
 
@app.get("/health")
def health():
    halted, reason = trading.is_halted()
    return {
        "ok":              True,
        "ts_utc":          now(),
        "authenticated":   ibeam.is_authenticated(),
        "enable_trading":  ENABLE_TRADING,
        "trading_halted":  halted,
        "halted_reason":   reason,
    }
 
 
# ── Account ───────────────────────────────────────────────────
 
@app.get("/broker/account")
def broker_account(authorization: str | None = Header(default=None)):
    auth(authorization)
    ibeam.tickle()
    summary = ibeam.get_account_summary(account_id())
    return {
        "mode":                    "paper",
        "timestamp_utc":           now(),
        "accounts":                [account_id()],
        "account_currency":        DEFAULT_CURRENCY,
        "nlv":                     summary["nlv"],
        "availableFunds":          summary["availableFunds"],
        "buyingPower":             summary["buyingPower"],
        "totalCashValue":          summary["totalCashValue"],
    }
 
 
# ── Positions ─────────────────────────────────────────────────
 
@app.get("/broker/positions")
def broker_positions(authorization: str | None = Header(default=None)):
    auth(authorization)
    return {"positions": ibeam.get_positions(account_id()), "timestamp_utc": now()}
 
 
# ── Orders ────────────────────────────────────────────────────
 
@app.get("/broker/trades")
def broker_trades(authorization: str | None = Header(default=None)):
    auth(authorization)
    orders = ibeam.get_live_orders(account_id())
    return {"trades": [{
        "orderId":       o.get("orderId") or o.get("order_id"),
        "symbol":        o.get("ticker")  or o.get("symbol"),
        "status":        o.get("status")  or o.get("order_status"),
        "filled":        o.get("filledQuantity"),
        "remaining":     o.get("remainingQuantity"),
        "avgFillPrice":  o.get("avgPrice"),
    } for o in orders], "timestamp_utc": now()}
 
 
@app.post("/v1/orders/place", response_model=PlaceResponse)
def orders_place(req: PlaceRequest, authorization: str | None = Header(default=None)):
    auth(authorization)
    orders = [{
        "symbol":           o.symbol.upper(),
        "side":             o.side,
        "order_type":       o.order_type,
        "tif":              o.tif,
        "quantity":         o.quantity,
        "limit_price":      o.limit_price,
        "client_order_id":  o.client_order_id,
    } for o in req.orders]
    placed, warnings = trading.execute(account_id(), orders, ENABLE_TRADING)
    return PlaceResponse(ok=True, timestamp_utc=now(), placed=placed, warnings=warnings)
 
 
@app.post("/v1/orders/cancel")
def orders_cancel(req: CancelRequest, authorization: str | None = Header(default=None)):
    auth(authorization)
    result = ibeam.cancel_order(account_id(), str(req.order_id))
    return {"ok": True, "timestamp_utc": now(), "cancelled": result}
 
 
@app.post("/v1/orders/cancelAll")
def orders_cancel_all(authorization: str | None = Header(default=None)):
    auth(authorization)
    orders    = ibeam.get_live_orders(account_id())
    cancelled = 0
    for o in orders:
        st = (o.get("status") or "").lower()
        if st in ("submitted", "presubmitted", "pendingsubmit"):
            oid = o.get("orderId") or o.get("order_id")
            if oid:
                ibeam.cancel_order(account_id(), str(oid))
                cancelled += 1
    return {"ok": True, "timestamp_utc": now(), "cancelled_count": cancelled}
 
 
# ── Market data ───────────────────────────────────────────────
 
@app.get("/v1/quote")
def quote(
    symbols: str = Query(..., description="Comma-separated e.g. NVDA,MSFT"),
    authorization: str | None = Header(default=None),
):
    auth(authorization)
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    prices   = ibeam.get_quotes(sym_list)
    return {sym: {"last": price, "price": price} for sym, price in prices.items()}
 
 
@app.get("/v1/contract/resolve")
def contract_resolve(
    symbol:        str           = Query(...),
    secType:       Optional[str] = Query(default=None),
    currency:      Optional[str] = Query(default=None),
    authorization: str | None    = Header(default=None),
):
    auth(authorization)
    exchange = ibeam.resolve_exchange(symbol)
    if exchange is None:
        raise HTTPException(400, detail=f"Could not resolve contract for {symbol}")
    return {
        "ok":              True,
        "timestamp_utc":   now(),
        "symbol":          symbol.upper(),
        "primaryExchange": exchange,
        "exchange":        exchange,
    }
 
 
# ── Trading control ───────────────────────────────────────────
 
@app.get("/v1/trading/status")
def trading_status(authorization: str | None = Header(default=None)):
    auth(authorization)
    halted, reason = trading.is_halted()
    return {"ok": True, "timestamp_utc": now(), "trading_halted": halted, "halted_reason": reason}
 
 
@app.post("/v1/trading/halt")
def trading_halt(req: TradingHaltRequest, authorization: str | None = Header(default=None)):
    auth(authorization)
    trading.halt(req.reason)
    halted, reason = trading.is_halted()
    return {"ok": True, "timestamp_utc": now(), "trading_halted": halted, "halted_reason": reason}
 
 
@app.post("/v1/trading/resume")
def trading_resume(authorization: str | None = Header(default=None)):
    auth(authorization)
    trading.resume()
    halted, reason = trading.is_halted()
    return {"ok": True, "timestamp_utc": now(), "trading_halted": halted, "halted_reason": reason}