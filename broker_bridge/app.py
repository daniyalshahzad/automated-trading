"""
IBKR Broker Bridge (Agent-ready) v2.3
======================================
Thin local "control panel" API for Interactive Brokers (TWS / IB Gateway) via ib_insync.

Key goals:
- One-call state snapshot for the agent: /v1/snapshot
  - includes account metrics + positions + open orders + recent trades + account-level PnL
- Execution flow: /v1/orders/preview -> /v1/orders/place -> monitor via /v1/snapshot
- Helper utilities: /v1/quote, /v1/contract/resolve
- Optional hard brake: /v1/trading/halt + /v1/trading/resume + /v1/trading/status

Policy enforcement lives in orchestrator. This bridge provides "state + execution"
with light long-only / cash-only checks.

Run:
  uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787

.env (in project root):
  IB_HOST=127.0.0.1
  IB_PORT=7497
  IB_CLIENT_ID=11
  BRIDGE_TOKEN=...secret...
  ENABLE_TRADING=false
  DEFAULT_CURRENCY=USD
  DEFAULT_EXCHANGE=SMART
  DEFAULT_PRIMARY_EXCHANGE=NASDAQ
  MARKET_DATA_TYPE=1          # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
  SNAPSHOT_WAIT_SECS=2.0
  QUOTE_WAIT_SECS=5.0
"""

import os
import math
import asyncio
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ib_insync import IB, Stock, MarketOrder, LimitOrder, Trade, Contract, Ticker


# -------------------------
# Env + config
# -------------------------
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "11"))

BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "USD").upper()
DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "SMART").upper()
DEFAULT_PRIMARY_EXCHANGE = os.getenv("DEFAULT_PRIMARY_EXCHANGE", "NASDAQ").upper()

# 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
# Use 1 if you have a market data subscription, 3 for paper accounts without one
MARKET_DATA_TYPE = int(os.getenv("MARKET_DATA_TYPE", "1"))

SNAPSHOT_WAIT_SECS = float(os.getenv("SNAPSHOT_WAIT_SECS", "2.0"))
QUOTE_WAIT_SECS = float(os.getenv("QUOTE_WAIT_SECS", "5.0"))

# Hard brake
TRADING_HALTED = False
TRADING_HALTED_REASON: Optional[str] = None
_trading_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_auth(authorization: str | None):
    if not BRIDGE_TOKEN:
        raise RuntimeError("BRIDGE_TOKEN not set in .env")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != BRIDGE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def _is_trading_halted() -> tuple[bool, Optional[str]]:
    with _trading_lock:
        return TRADING_HALTED, TRADING_HALTED_REASON


def _set_trading_halted(halted: bool, reason: Optional[str]):
    global TRADING_HALTED, TRADING_HALTED_REASON
    with _trading_lock:
        TRADING_HALTED = halted
        TRADING_HALTED_REASON = reason if halted else None


def _sanitize_float(x) -> Optional[float]:
    """Return None for None / NaN / Inf — IBKR uses these for missing data."""
    if x is None:
        return None
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None


# -------------------------
# IB event loop — dedicated thread
# -------------------------
ib = IB()
ib_loop = asyncio.new_event_loop()
_thread_started = threading.Event()


def _ib_thread_main():
    asyncio.set_event_loop(ib_loop)
    _thread_started.set()
    ib_loop.run_forever()


threading.Thread(target=_ib_thread_main, daemon=True).start()
_thread_started.wait(timeout=5)


async def _ensure_connected_async():
    if ib.isConnected():
        return
    await ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=5)
    # Set market data type immediately after connect
    ib.reqMarketDataType(MARKET_DATA_TYPE)


def ib_run(coro, timeout=20):
    fut = asyncio.run_coroutine_threadsafe(coro, ib_loop)
    return fut.result(timeout=timeout)


def ensure_connected():
    try:
        ib_run(_ensure_connected_async(), timeout=10)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Failed to connect to TWS/IB Gateway",
                "host": IB_HOST,
                "port": IB_PORT,
                "clientId": IB_CLIENT_ID,
                "exception": str(e),
                "checklist": [
                    "Is TWS/IB Gateway running and logged into PAPER?",
                    "TWS: Global Configuration → API → Settings → Enable ActiveX and Socket Clients",
                    "Socket port matches IB_PORT (7497 for TWS paper; 4002 for Gateway paper)",
                    "If 'localhost only' is enabled, host must be 127.0.0.1",
                ],
            },
        )


# -------------------------
# PnL subscription (account-level)
# -------------------------
PNL_SUBSCRIBED = False
PNL_ACCOUNT_ID: Optional[str] = None
PNL_LATEST = {"dailyPnL": None, "unrealizedPnL": None, "realizedPnL": None}
_pnl_lock = threading.Lock()


def _set_pnl_latest(daily, unreal, real):
    with _pnl_lock:
        PNL_LATEST["dailyPnL"] = daily
        PNL_LATEST["unrealizedPnL"] = unreal
        PNL_LATEST["realizedPnL"] = real


def _get_pnl_latest() -> dict:
    with _pnl_lock:
        return dict(PNL_LATEST)


async def _ensure_pnl_subscription_async():
    global PNL_SUBSCRIBED, PNL_ACCOUNT_ID
    if PNL_SUBSCRIBED:
        return
    accts = list(ib.managedAccounts() or [])
    if not accts:
        await asyncio.sleep(0.25)
        accts = list(ib.managedAccounts() or [])
    if not accts:
        return
    PNL_ACCOUNT_ID = accts[0]
    pnl_obj = ib.reqPnL(PNL_ACCOUNT_ID, modelCode="")

    def _on_pnl_update(obj):
        _set_pnl_latest(
            _sanitize_float(getattr(obj, "dailyPnL", None)),
            _sanitize_float(getattr(obj, "unrealizedPnL", None)),
            _sanitize_float(getattr(obj, "realizedPnL", None)),
        )

    _on_pnl_update(pnl_obj)
    try:
        pnl_obj.updateEvent += _on_pnl_update
    except Exception:
        pass
    PNL_SUBSCRIBED = True


def ensure_pnl_subscription():
    try:
        ib_run(_ensure_pnl_subscription_async(), timeout=5)
    except Exception:
        pass


# -------------------------
# FastAPI app
# -------------------------
app = FastAPI(title="IBKR Broker Bridge (Agent-ready)", version="2.3")


@app.on_event("startup")
def _startup():
    try:
        ensure_connected()
        ensure_pnl_subscription()
    except Exception:
        pass


@app.get("/health")
def health():
    halted, reason = _is_trading_halted()
    return {
        "ok": True,
        "ts_utc": utc_now(),
        "ib_connected": ib.isConnected(),
        "ib_host": IB_HOST,
        "ib_port": IB_PORT,
        "ib_client_id": IB_CLIENT_ID,
        "enable_trading_env": ENABLE_TRADING,
        "default_currency": DEFAULT_CURRENCY,
        "default_exchange": DEFAULT_EXCHANGE,
        "default_primary_exchange": DEFAULT_PRIMARY_EXCHANGE,
        "market_data_type": MARKET_DATA_TYPE,
        "trading_halted": halted,
        "trading_halted_reason": reason,
        "pnl_subscribed": PNL_SUBSCRIBED,
        "pnl_account_id": PNL_ACCOUNT_ID,
    }


# -------------------------
# Models
# -------------------------
OrderType = Literal["MKT", "LMT"]
Side = Literal["buy", "sell"]
TimeInForce = Literal["DAY", "GTC"]


class OrderIntent(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, e.g. AAPL")
    side: Side
    order_type: OrderType = "MKT"
    tif: TimeInForce = "DAY"
    quantity: Optional[float] = None
    notional: Optional[float] = None
    notional_currency: Optional[str] = None
    limit_price: Optional[float] = None
    exchange: Optional[str] = None
    primary_exchange: Optional[str] = None
    currency: Optional[str] = None
    client_order_id: Optional[str] = None


class PreviewRequest(BaseModel):
    orders: list[OrderIntent]


class PreviewResponse(BaseModel):
    ok: bool
    timestamp_utc: str
    account_currency: str
    available_funds: float | None
    available_funds_currency: str | None
    resolved_orders: list[dict]
    warnings: list[str] = []


class PlaceRequest(BaseModel):
    orders: list[OrderIntent]


class PlaceResponse(BaseModel):
    ok: bool
    timestamp_utc: str
    placed: list[dict]
    warnings: list[str] = []


class CancelRequest(BaseModel):
    order_id: int


class CancelResponse(BaseModel):
    ok: bool
    timestamp_utc: str
    cancelled: dict


class TradingHaltRequest(BaseModel):
    reason: Optional[str] = None


# -------------------------
# Low-level helpers
# -------------------------
def _get_account_summary_all() -> list[Any]:
    ensure_connected()
    return ib_run(ib.accountSummaryAsync(), timeout=15)


def _pick_tag(summary: list[Any], tag: str):
    candidates = [(x.value, x.currency) for x in summary if x.tag == tag]
    if not candidates:
        return None, None
    for v, c in candidates:
        if (c or "").upper() == DEFAULT_CURRENCY:
            return _sanitize_float(v), (c or DEFAULT_CURRENCY)
    v, c = candidates[0]
    return _sanitize_float(v), (c or DEFAULT_CURRENCY)


def _get_positions_map():
    ensure_connected()
    pos = ib.positions()
    return {p.contract.symbol.upper(): float(p.position) for p in pos}


def _make_stock_contract(
    symbol: str,
    exchange: str | None,
    primary_exchange: str | None,
    currency: str | None,
) -> Contract:
    sym = symbol.upper().strip()
    return Stock(
        sym,
        (exchange or DEFAULT_EXCHANGE).upper(),
        (currency or "USD").upper(),
        primaryExchange=(primary_exchange or DEFAULT_PRIMARY_EXCHANGE).upper(),
    )


async def _snapshot_price_async(contract: Contract, max_wait_secs: float) -> tuple[Optional[float], str]:
    # Ensure market data type is set on the IB loop thread before each request
    ib.reqMarketDataType(MARKET_DATA_TYPE)
    ticker: Ticker = ib.reqMktData(contract, snapshot=True)
    steps = max(1, int(max_wait_secs / 0.1))
    for _ in range(steps):
        await asyncio.sleep(0.1)  # asyncio.sleep — NOT ib.sleep (loop already running)
        last = _sanitize_float(ticker.last)
        close = _sanitize_float(ticker.close)
        bid = _sanitize_float(ticker.bid)
        ask = _sanitize_float(ticker.ask)
        if last is not None and last > 0:
            return last, "last"
        if close is not None and close > 0:
            return close, "close"
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2.0, "mid"
    return None, "none"


async def _req_contract_details_async(contract: Contract):
    return await ib.reqContractDetailsAsync(contract)


async def _place_order_async(contract: Contract, ib_order) -> Trade:
    return ib.placeOrder(contract, ib_order)


async def _cancel_order_async(order) -> None:
    ib.cancelOrder(order)


def _resolve_quantity(intent: OrderIntent, contract: Contract) -> tuple[float, list[str]]:
    warnings: list[str] = []

    if intent.quantity is not None:
        if intent.quantity <= 0:
            raise HTTPException(400, detail=f"Quantity must be > 0 for {intent.symbol}")
        return float(intent.quantity), warnings

    if intent.notional is None:
        raise HTTPException(400, detail=f"Either quantity or notional must be provided for {intent.symbol}")

    notional_ccy = (intent.notional_currency or DEFAULT_CURRENCY).upper()
    if notional_ccy != DEFAULT_CURRENCY:
        warnings.append(
            f"{intent.symbol}: notional_currency={notional_ccy} differs from account currency={DEFAULT_CURRENCY}. "
            "Sizing assumes notional is in account currency."
        )

    if intent.notional <= 0:
        raise HTTPException(400, detail=f"Notional must be > 0 for {intent.symbol}")

    px, px_src = ib_run(
        _snapshot_price_async(contract, SNAPSHOT_WAIT_SECS),
        timeout=int(SNAPSHOT_WAIT_SECS) + 5,
    )
    if px is None or px <= 0:
        raise HTTPException(
            400,
            detail=(
                f"Could not obtain snapshot price for sizing {intent.symbol} "
                f"(MARKET_DATA_TYPE={MARKET_DATA_TYPE} — use 3 for delayed if no live subscription)."
            ),
        )

    # Whole shares only
    qty = int(intent.notional / px)
    if qty <= 0:
        raise HTTPException(400, detail=f"Notional too small to buy any shares of {intent.symbol} at ~{px}")

    warnings.append(
        f"{intent.symbol}: computed qty={qty} (whole shares) from notional={intent.notional} "
        f"using snapshot px≈{px} ({px_src}), marketDataType={MARKET_DATA_TYPE}"
    )
    return float(qty), warnings


def _build_ib_order(intent: OrderIntent, qty: float):
    action = "BUY" if intent.side == "buy" else "SELL"
    if intent.order_type == "MKT":
        return MarketOrder(action, qty, tif=intent.tif)
    if intent.order_type == "LMT":
        if intent.limit_price is None or intent.limit_price <= 0:
            raise HTTPException(400, detail=f"limit_price must be provided for LMT order on {intent.symbol}")
        return LimitOrder(action, qty, intent.limit_price, tif=intent.tif)
    raise HTTPException(400, detail=f"Unsupported order_type={intent.order_type} for {intent.symbol}")


def _cash_only_long_only_checks(intents: list[OrderIntent], positions_map: dict[str, float]):
    for i in intents:
        sym = i.symbol.upper().strip()
        if i.side == "sell":
            have = positions_map.get(sym, 0.0)
            if have <= 0:
                raise HTTPException(400, detail=f"Short selling not allowed: no position to sell for {sym}")


def _available_funds():
    summary = _get_account_summary_all()
    avail, avail_ccy = _pick_tag(summary, "AvailableFunds")
    if avail is None:
        avail, avail_ccy = _pick_tag(summary, "TotalCashValue")
    return avail, avail_ccy, summary


def _approx_spend(resolved_orders: list[dict]) -> float:
    spend = 0.0
    for o in resolved_orders:
        if o["side"] != "buy":
            continue
        if o.get("notional") is not None:
            spend += float(o["notional"])
        elif o["order_type"] == "LMT" and o.get("limit_price") is not None:
            spend += float(o["quantity"]) * float(o["limit_price"])
    return float(spend)


def _normalize_symbol_list(symbols_csv: str) -> list[str]:
    seen = set()
    out = []
    for s in (symbols_csv or "").split(","):
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# -------------------------
# Trading control endpoints
# -------------------------
@app.get("/v1/trading/status")
def trading_status(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    halted, reason = _is_trading_halted()
    return {
        "ok": True,
        "timestamp_utc": utc_now(),
        "enable_trading_env": ENABLE_TRADING,
        "trading_halted": halted,
        "trading_halted_reason": reason,
    }


@app.post("/v1/trading/halt")
def trading_halt(req: TradingHaltRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    _set_trading_halted(True, req.reason or "Halted by API request")
    halted, reason = _is_trading_halted()
    return {"ok": True, "timestamp_utc": utc_now(), "trading_halted": halted, "trading_halted_reason": reason}


@app.post("/v1/trading/resume")
def trading_resume(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    _set_trading_halted(False, None)
    halted, reason = _is_trading_halted()
    return {"ok": True, "timestamp_utc": utc_now(), "trading_halted": halted, "trading_halted_reason": reason}


# -------------------------
# Market data endpoints
# -------------------------
@app.get("/v1/contract/resolve")
def contract_resolve(
    symbol: str = Query(..., description="Ticker symbol, e.g. AAPL"),
    exchange: Optional[str] = Query(default=None),
    primary_exchange: Optional[str] = Query(default=None),
    currency: Optional[str] = Query(default=None),
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    ensure_connected()

    contract = _make_stock_contract(symbol, exchange, primary_exchange, currency)
    qualified = ib_run(ib.qualifyContractsAsync(contract), timeout=10)
    if not qualified:
        raise HTTPException(400, detail=f"Could not qualify contract for {symbol}")

    details = ib_run(_req_contract_details_async(contract), timeout=10)
    d0 = details[0] if details else None

    return {
        "ok": True,
        "timestamp_utc": utc_now(),
        "symbol": contract.symbol,
        "conId": contract.conId,
        "secType": contract.secType,
        "exchange": contract.exchange,
        "primaryExchange": contract.primaryExchange,
        "currency": contract.currency,
        "localSymbol": getattr(contract, "localSymbol", None),
        "longName": getattr(d0, "longName", None) if d0 else None,
        "industry": getattr(d0, "industry", None) if d0 else None,
        "category": getattr(d0, "category", None) if d0 else None,
        "subcategory": getattr(d0, "subcategory", None) if d0 else None,
    }


@app.get("/v1/quote")
def quote(
    symbols: str = Query(..., description="Comma-separated symbols, e.g. AAPL,MSFT"),
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    ensure_connected()

    syms = _normalize_symbol_list(symbols)
    if not syms:
        raise HTTPException(400, detail="No symbols provided")

    out = []
    warnings: list[str] = []

    for sym in syms:
        contract = _make_stock_contract(sym, None, None, "USD")
        qualified = ib_run(ib.qualifyContractsAsync(contract), timeout=10)
        if not qualified:
            warnings.append(f"{sym}: could not qualify contract")
            out.append({"symbol": sym, "ok": False})
            continue

        px, src = ib_run(
            _snapshot_price_async(contract, QUOTE_WAIT_SECS),
            timeout=int(QUOTE_WAIT_SECS) + 5,
        )
        if px is None:
            warnings.append(f"{sym}: quote unavailable (MARKET_DATA_TYPE={MARKET_DATA_TYPE})")
            out.append({"symbol": sym, "ok": True, "price": None, "price_source": "none"})
        else:
            out.append({"symbol": sym, "ok": True, "price": px, "price_source": src})

    return {"ok": True, "timestamp_utc": utc_now(), "quotes": out, "warnings": warnings}


# -------------------------
# Snapshot
# -------------------------
@app.get("/v1/snapshot")
def snapshot(authorization: str | None = Header(default=None)):
    """
    One-call state snapshot for the agent:
    account + pnl + positions (with market prices) + open orders + recent trades
    """
    require_auth(authorization)
    ensure_connected()
    ensure_pnl_subscription()

    halted, reason = _is_trading_halted()
    warnings: list[str] = []

    summary = _get_account_summary_all()
    nlv, nlv_ccy = _pick_tag(summary, "NetLiquidation")
    avail, avail_ccy = _pick_tag(summary, "AvailableFunds")
    bp, bp_ccy = _pick_tag(summary, "BuyingPower")
    cash, cash_ccy = _pick_tag(summary, "TotalCashValue")

    positions = ib.positions()
    pos_out = []
    total_market_value = 0.0
    total_unrealized_pnl = 0.0
    have_any_prices = False

    for p in positions:
        sym = (p.contract.symbol or "").upper()
        qty = _sanitize_float(p.position) or 0.0
        avg_cost = _sanitize_float(p.avgCost)

        try:
            ib_run(ib.qualifyContractsAsync(p.contract), timeout=10)
        except Exception:
            pass

        con_id = getattr(p.contract, "conId", None)
        market_price = None
        market_price_source = "none"
        market_value = None
        unreal_pnl = None
        unreal_pnl_pct = None

        try:
            px, src = ib_run(
                _snapshot_price_async(p.contract, SNAPSHOT_WAIT_SECS),
                timeout=int(SNAPSHOT_WAIT_SECS) + 5,
            )
            if px is not None and px > 0:
                market_price = px
                market_price_source = src
                have_any_prices = True
                market_value = px * qty
                total_market_value += market_value
                if avg_cost is not None:
                    unreal_pnl = (px - avg_cost) * qty
                    total_unrealized_pnl += unreal_pnl
                    cost_basis = avg_cost * qty
                    unreal_pnl_pct = (unreal_pnl / cost_basis) if abs(cost_basis) > 1e-9 else None
        except Exception:
            warnings.append(f"{sym}: market price snapshot unavailable; unrealized PnL not computed")

        pos_out.append({
            "symbol": sym,
            "conId": con_id,
            "secType": p.contract.secType,
            "exchange": p.contract.exchange,
            "primaryExchange": getattr(p.contract, "primaryExchange", None),
            "currency": p.contract.currency,
            "qty": qty,
            "avgCost": avg_cost,
            "marketPrice": market_price,
            "marketPrice_source": market_price_source,
            "marketValue": market_value,
            "unrealizedPnL": unreal_pnl,
            "unrealizedPnL_pct": unreal_pnl_pct,
        })

    if positions and not have_any_prices:
        warnings.append("No market prices available for positions (check MARKET_DATA_TYPE in .env).")

    open_orders = ib.openOrders()
    open_out = [{
        "orderId": o.orderId,
        "action": o.action,
        "type": o.orderType,
        "qty": _sanitize_float(o.totalQuantity),
        "lmtPrice": _sanitize_float(getattr(o, "lmtPrice", None)),
        "tif": o.tif,
    } for o in open_orders]

    trades: list[Trade] = ib.trades()
    trades_out = [{
        "orderId": t.order.orderId,
        "permId": getattr(t.order, "permId", None),
        "symbol": getattr(t.contract, "symbol", None),
        "status": t.orderStatus.status,
        "filled": _sanitize_float(t.orderStatus.filled),
        "remaining": _sanitize_float(t.orderStatus.remaining),
        "avgFillPrice": _sanitize_float(t.orderStatus.avgFillPrice),
        "lastFillPrice": _sanitize_float(t.orderStatus.lastFillPrice),
    } for t in trades[-50:]]

    pnl_latest = _get_pnl_latest()
    if not PNL_SUBSCRIBED:
        warnings.append("PnL stream not subscribed (managed account not available yet).")

    return {
        "ok": True,
        "ts_utc": utc_now(),
        "trading": {
            "enable_trading_env": ENABLE_TRADING,
            "trading_halted": halted,
            "trading_halted_reason": reason,
        },
        "account": {
            "base_currency": DEFAULT_CURRENCY,
            "nlv": nlv, "nlv_currency": nlv_ccy,
            "available_funds": avail, "available_funds_currency": avail_ccy,
            "total_cash": cash, "total_cash_currency": cash_ccy,
            "buying_power": bp, "buying_power_currency": bp_ccy,
        },
        "pnl": {
            "accountId": PNL_ACCOUNT_ID,
            "dailyPnL": pnl_latest["dailyPnL"],
            "unrealizedPnL": pnl_latest["unrealizedPnL"],
            "realizedPnL": pnl_latest["realizedPnL"],
        },
        "positions": pos_out,
        "totals": {
            "totalMarketValue": total_market_value if have_any_prices else None,
            "totalUnrealizedPnL": total_unrealized_pnl if have_any_prices else None,
        },
        "openOrders": open_out,
        "recentTrades": trades_out,
        "warnings": warnings,
    }


# -------------------------
# Order endpoints
# -------------------------
@app.post("/v1/orders/preview", response_model=PreviewResponse)
def orders_preview(req: PreviewRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()

    positions_map = _get_positions_map()
    _cash_only_long_only_checks(req.orders, positions_map)

    avail, avail_ccy, _summary = _available_funds()
    resolved: list[dict] = []
    warnings: list[str] = []

    for intent in req.orders:
        sym = intent.symbol.upper().strip()
        client_order_id = intent.client_order_id or str(uuid.uuid4())

        contract = _make_stock_contract(sym, intent.exchange, intent.primary_exchange, intent.currency)
        qualified = ib_run(ib.qualifyContractsAsync(contract), timeout=10)
        if not qualified:
            raise HTTPException(400, detail=f"Could not qualify contract for {sym}")

        con_id = getattr(contract, "conId", None)
        qty, w = _resolve_quantity(intent, contract)
        warnings.extend(w)

        if intent.side == "sell":
            have = positions_map.get(sym, 0.0)
            if qty > have:
                raise HTTPException(
                    400,
                    detail=f"Short selling not allowed: trying to sell {qty} but have {have} of {sym}",
                )

        ib_order = _build_ib_order(intent, qty)

        resolved.append({
            "clientOrderId": client_order_id,
            "symbol": sym,
            "side": intent.side,
            "order_type": intent.order_type,
            "tif": intent.tif,
            "quantity": qty,
            "limit_price": intent.limit_price,
            "notional": intent.notional,
            "notional_currency": (intent.notional_currency or DEFAULT_CURRENCY).upper()
                if intent.notional is not None else None,
            "contract": {
                "secType": "STK",
                "conId": con_id,
                "exchange": contract.exchange,
                "primaryExchange": contract.primaryExchange,
                "currency": contract.currency,
                "localSymbol": getattr(contract, "localSymbol", None),
            },
            "ib_order": {
                "action": ib_order.action,
                "orderType": ib_order.orderType,
                "totalQuantity": float(ib_order.totalQuantity),
                "tif": ib_order.tif,
                "lmtPrice": _sanitize_float(getattr(ib_order, "lmtPrice", None)),
            },
        })

    approx_spend = _approx_spend(resolved)
    if approx_spend > 0 and avail is not None and approx_spend > avail:
        raise HTTPException(
            400,
            detail=(
                f"Cash-only check failed: approx buy spend {approx_spend:.2f} {DEFAULT_CURRENCY} "
                f"exceeds available {avail:.2f} {avail_ccy}"
            ),
        )

    for r in resolved:
        if r["side"] == "buy" and r["order_type"] == "MKT" and r.get("notional") is None:
            warnings.append(
                f"{r['symbol']}: BUY MKT with quantity-only has unknown final spend. "
                "Prefer notional sizing or limit orders for strict cash-only."
            )

    return PreviewResponse(
        ok=True,
        timestamp_utc=utc_now(),
        account_currency=DEFAULT_CURRENCY,
        available_funds=avail,
        available_funds_currency=avail_ccy,
        resolved_orders=resolved,
        warnings=warnings,
    )


@app.post("/v1/orders/place", response_model=PlaceResponse)
def orders_place(req: PlaceRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()

    halted, reason = _is_trading_halted()
    if halted:
        raise HTTPException(403, detail=f"Trading halted: {reason}")
    if not ENABLE_TRADING:
        raise HTTPException(403, detail="Trading disabled (set ENABLE_TRADING=true in .env)")

    preview = orders_preview(PreviewRequest(orders=req.orders), authorization)

    placed: list[dict] = []
    warnings = list(preview.warnings)

    for r in preview.resolved_orders:
        client_order_id = r["clientOrderId"]
        sym = r["symbol"]
        qty = float(r["quantity"])

        contract = _make_stock_contract(
            sym,
            exchange=r["contract"]["exchange"],
            primary_exchange=r["contract"]["primaryExchange"],
            currency=r["contract"]["currency"],
        )
        qualified = ib_run(ib.qualifyContractsAsync(contract), timeout=10)
        if not qualified:
            raise HTTPException(400, detail=f"Could not qualify contract for {sym}")

        intent_like = OrderIntent(
            symbol=sym,
            side=r["side"],
            order_type=r["order_type"],
            tif=r["tif"],
            quantity=qty,
            limit_price=r.get("limit_price"),
            client_order_id=client_order_id,
        )
        ib_order = _build_ib_order(intent_like, qty)
        trade = ib_run(_place_order_async(contract, ib_order), timeout=10)

        placed.append({
            "clientOrderId": client_order_id,
            "symbol": sym,
            "conId": getattr(contract, "conId", None),
            "orderId": trade.order.orderId,
            "permId": getattr(trade.order, "permId", None),
            "status": trade.orderStatus.status,
            "action": trade.order.action,
            "orderType": trade.order.orderType,
            "totalQuantity": _sanitize_float(trade.order.totalQuantity),
            "tif": trade.order.tif,
            "lmtPrice": _sanitize_float(getattr(trade.order, "lmtPrice", None)),
        })

    return PlaceResponse(ok=True, timestamp_utc=utc_now(), placed=placed, warnings=warnings)


@app.post("/v1/orders/cancel", response_model=CancelResponse)
def orders_cancel(req: CancelRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()

    halted, reason = _is_trading_halted()
    if halted:
        raise HTTPException(403, detail=f"Trading halted: {reason}")
    if not ENABLE_TRADING:
        raise HTTPException(403, detail="Trading disabled (set ENABLE_TRADING=true in .env)")

    trades: list[Trade] = ib.trades()
    target = next((t for t in trades if t.order.orderId == req.order_id), None)
    if target is None:
        raise HTTPException(404, detail=f"OrderId {req.order_id} not found in trades()")

    ib_run(_cancel_order_async(target.order), timeout=10)

    return CancelResponse(
        ok=True,
        timestamp_utc=utc_now(),
        cancelled={
            "orderId": req.order_id,
            "permId": getattr(target.order, "permId", None),
            "symbol": getattr(target.contract, "symbol", None),
            "status_before": target.orderStatus.status,
            "lastLog": str(target.log[-1].message) if target.log else "",
        },
    )


@app.post("/v1/orders/cancelAll")
def orders_cancel_all(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()

    halted, reason = _is_trading_halted()
    if halted:
        raise HTTPException(403, detail=f"Trading halted: {reason}")
    if not ENABLE_TRADING:
        raise HTTPException(403, detail="Trading disabled (set ENABLE_TRADING=true in .env)")

    trades: list[Trade] = ib.trades()
    cancelled = 0
    for t in trades:
        st = (t.orderStatus.status or "").lower()
        if st in ("submitted", "presubmitted", "pendingsubmit"):
            ib_run(_cancel_order_async(t.order), timeout=10)
            cancelled += 1

    return {"ok": True, "timestamp_utc": utc_now(), "cancelled_count": cancelled}


# -------------------------
# Legacy endpoints
# -------------------------
@app.get("/broker/account")
def broker_account(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    summary = _get_account_summary_all()
    nlv, nlv_ccy = _pick_tag(summary, "NetLiquidation")
    avail, avail_ccy = _pick_tag(summary, "AvailableFunds")
    bp, bp_ccy = _pick_tag(summary, "BuyingPower")
    cash, cash_ccy = _pick_tag(summary, "TotalCashValue")
    return {
        "mode": "paper",
        "timestamp_utc": utc_now(),
        "accounts": ib.managedAccounts(),
        "account_currency": DEFAULT_CURRENCY,
        "nlv": nlv, "nlv_currency": nlv_ccy,
        "availableFunds": avail, "availableFunds_currency": avail_ccy,
        "buyingPower": bp, "buyingPower_currency": bp_ccy,
        "totalCashValue": cash, "totalCashValue_currency": cash_ccy,
    }


@app.get("/broker/positions")
def broker_positions(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()
    return {"positions": [{
        "symbol": p.contract.symbol,
        "secType": p.contract.secType,
        "exchange": p.contract.exchange,
        "currency": p.contract.currency,
        "position": _sanitize_float(p.position),
        "avgCost": _sanitize_float(p.avgCost),
    } for p in ib.positions()], "timestamp_utc": utc_now()}


@app.get("/broker/openOrders")
def broker_open_orders(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()
    return {"openOrders": [{
        "orderId": o.orderId,
        "action": o.action,
        "orderType": o.orderType,
        "totalQuantity": _sanitize_float(o.totalQuantity),
        "lmtPrice": _sanitize_float(getattr(o, "lmtPrice", None)),
        "tif": o.tif,
    } for o in ib.openOrders()], "timestamp_utc": utc_now()}


@app.get("/broker/trades")
def broker_trades(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    ensure_connected()
    return {"trades": [{
        "orderId": t.order.orderId,
        "symbol": getattr(t.contract, "symbol", None),
        "status": t.orderStatus.status,
        "filled": _sanitize_float(t.orderStatus.filled),
        "remaining": _sanitize_float(t.orderStatus.remaining),
        "avgFillPrice": _sanitize_float(t.orderStatus.avgFillPrice),
        "lastFillPrice": _sanitize_float(t.orderStatus.lastFillPrice),
    } for t in ib.trades()], "timestamp_utc": utc_now()}