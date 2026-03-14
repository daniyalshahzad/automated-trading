"""
ibeam.py — Raw calls to the IBeam Client Portal Gateway.
IBeam runs on https://localhost:5000 with a self-signed cert.
Every function raises HTTPException on failure.
"""
 
import time
import math
import uuid
from typing import Optional
 
import requests
import urllib3
from fastapi import HTTPException
 
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
 
BASE    = "https://localhost:5000/v1/api"
SESSION = requests.Session()
SESSION.verify = False
 
 
def _get(path: str, params: dict = None) -> dict:
    r = SESSION.get(f"{BASE}{path}", params=params, timeout=10)
    if not r.ok:
        raise HTTPException(502, detail=f"IBeam {path}: {r.text}")
    return r.json()
 
 
def _post(path: str, body: dict) -> dict:
    r = SESSION.post(f"{BASE}{path}", json=body, timeout=10)
    if not r.ok:
        raise HTTPException(502, detail=f"IBeam {path}: {r.text}")
    return r.json()
 
 
def _delete(path: str) -> dict:
    r = SESSION.delete(f"{BASE}{path}", timeout=10)
    if not r.ok:
        raise HTTPException(502, detail=f"IBeam {path}: {r.text}")
    return r.json()
 
 
def _num(val) -> Optional[float]:
    """Parse a float, return None for missing/NaN/Inf."""
    try:
        f = float(str(val).replace(",", ""))
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None
 
 
# ── Session ───────────────────────────────────────────────────
 
def is_authenticated() -> bool:
    try:
        data = _get("/iserver/auth/status")
        return bool(data.get("authenticated"))
    except Exception:
        return False
 
 
def tickle():
    try:
        SESSION.post(f"{BASE}/tickle", timeout=5)
    except Exception:
        pass
 
 
# ── Account ───────────────────────────────────────────────────
 
def get_account_id() -> str:
    data = _get("/iserver/accounts")
    accounts = data.get("accounts", [])
    if not accounts:
        raise HTTPException(502, detail="No accounts found — is IBeam authenticated?")
    return accounts[0]
 
 
def get_account_summary(account_id: str) -> dict:
    data = _get(f"/portfolio/{account_id}/summary")
    return {
        "nlv":            _num((data.get("netliquidation") or {}).get("amount")),
        "availableFunds": _num((data.get("availablefunds") or {}).get("amount")),
        "buyingPower":    _num((data.get("buyingpower")    or {}).get("amount")),
        "totalCashValue": _num((data.get("totalcashvalue") or {}).get("amount")),
    }
 
 
# ── Positions ─────────────────────────────────────────────────
 
def get_positions(account_id: str) -> list[dict]:
    data = _get(f"/portfolio/{account_id}/positions/0")
    if not isinstance(data, list):
        return []
    return [{
        "symbol":        p.get("ticker", ""),
        "conId":         p.get("conid"),
        "secType":       p.get("assetClass", "STK"),
        "exchange":      "NASDAQ",
        "currency":      p.get("currency", "USD"),
        "position":      _num(p.get("position")),
        "avgCost":       _num(p.get("avgCost")),
        "mktValue":      _num(p.get("mktValue")),
        "unrealizedPnl": _num(p.get("unrealizedPnl")),
    } for p in data]
 
 
# ── Market data ───────────────────────────────────────────────
 
def search_contract(symbol: str) -> Optional[dict]:
    """Find the first STK contract for a symbol, return conId + exchange."""
    data = _get("/iserver/secdef/search", params={"symbol": symbol, "name": False})
    if not isinstance(data, list) or not data:
        return None
    item = data[0]
    return {
        "conId":    item.get("conid"),
        "symbol":   item.get("symbol", symbol),
        "exchange": item.get("primaryExch", ""),
    }
 
 
def get_prices(con_ids: list[int]) -> dict:
    """
    Fetch live prices for a list of conIds.
    Returns {conId: price}.
    IBeam needs two calls — first primes the subscription, second returns data.
    """
    ids    = ",".join(str(c) for c in con_ids)
    fields = "31,84,86,7295"  # last, bid, ask, close
 
    _get("/iserver/marketdata/snapshot", params={"conids": ids, "fields": fields})
    time.sleep(1.5)
    data = _get("/iserver/marketdata/snapshot", params={"conids": ids, "fields": fields})
 
    result = {}
    for item in (data if isinstance(data, list) else []):
        con_id = item.get("conid")
        if not con_id:
            continue
        last  = _num(item.get("31"))
        bid   = _num(item.get("84"))
        ask   = _num(item.get("86"))
        close = _num(item.get("7295"))
        price = last or ((bid + ask) / 2 if bid and ask else None) or close
        result[con_id] = price
    return result
 
 
def get_quotes(symbols: list[str]) -> dict:
    """Higher-level: resolve symbols to conIds, fetch prices, return {symbol: price}."""
    sym_to_con = {}
    for sym in symbols:
        contract = search_contract(sym)
        if contract and contract.get("conId"):
            sym_to_con[sym] = int(contract["conId"])
 
    if not sym_to_con:
        return {}
 
    prices    = get_prices(list(sym_to_con.values()))
    con_to_sym = {v: k for k, v in sym_to_con.items()}
    return {con_to_sym[cid]: price for cid, price in prices.items() if cid in con_to_sym}
 
 
def resolve_exchange(symbol: str) -> Optional[str]:
    """Return the primary exchange for a symbol, or None if not found."""
    contract = search_contract(symbol)
    if not contract or not contract.get("conId"):
        return None
    try:
        data = _get(f"/iserver/contract/{contract['conId']}/info")
        return (data.get("primaryExch") or data.get("exchange") or "").upper() or None
    except Exception:
        return None
 
 
# ── Orders ────────────────────────────────────────────────────
 
def place_order(account_id: str, con_id: int, side: str, quantity: float,
                order_type: str = "MKT", limit_price: float = None,
                tif: str = "DAY", client_order_id: str = None) -> dict:
    order = {
        "conid":     con_id,
        "orderType": order_type,
        "side":      side.upper(),
        "quantity":  quantity,
        "tif":       tif,
        "cOID":      client_order_id or str(uuid.uuid4()),
    }
    if order_type == "LMT" and limit_price:
        order["price"] = limit_price
 
    response = _post(f"/iserver/account/{account_id}/orders", {"orders": [order]})
 
    # IBeam may return a confirmation challenge — auto-confirm it
    if isinstance(response, list):
        response = response[0] if response else {}
    if response.get("id"):
        response = _post(f"/iserver/reply/{response['id']}", {"confirmed": True})
        if isinstance(response, list):
            response = response[0] if response else {}
 
    return response
 
 
def get_live_orders(account_id: str) -> list[dict]:
    data = _get(f"/iserver/account/orders?accountId={account_id}")
    orders = data.get("orders", [])
    return orders if isinstance(orders, list) else []
 
 
def cancel_order(account_id: str, order_id: str) -> dict:
    return _delete(f"/iserver/account/{account_id}/order/{order_id}")