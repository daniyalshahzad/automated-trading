"""broker.py — All calls to the broker bridge REST API."""
 
import time
from typing import Any, Dict, List, Optional
 
import requests
 
from .config import BRIDGE_URL, HEADERS, log
 
 
def _get(path: str, **params) -> Any:
    r = requests.get(f"{BRIDGE_URL}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()
 
 
def _post(path: str, payload: dict) -> Any:
    r = requests.post(f"{BRIDGE_URL}{path}", headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()
 
 
def get_account() -> Dict:
    return _get("/broker/account")
 
 
def get_positions() -> List[Dict]:
    data = _get("/broker/positions")
    positions = data.get("positions", data) if isinstance(data, dict) else data
    # Normalize field names: bridge uses "position" and "avgCost"
    out = []
    for p in positions:
        p = dict(p)
        p.setdefault("qty", p.get("position", 0))
        p.setdefault("avg_price", p.get("avgCost", 0))
        out.append(p)
    return out
 
 
def get_trades() -> List[Dict]:
    try:
        data = _get("/broker/trades")
        trades = data.get("trades", data) if isinstance(data, dict) else data
        return trades if isinstance(trades, list) else []
    except Exception as e:
        log.warning(f"get_trades failed: {e}")
        return []
 
 
def get_quote(symbol: str) -> Optional[float]:
    """Return live price for a symbol, or None on failure."""
    try:
        data = _get("/v1/quote", symbols=symbol)
        entry = data.get(symbol, {})
        price = entry.get("last") or entry.get("price")
        return float(price) if price else None
    except Exception:
        return None
 
 
def resolve_exchange(symbol: str) -> Optional[str]:
    """Return primaryExchange for a symbol, or None on failure."""
    try:
        data = _get("/v1/contract/resolve", symbol=symbol, secType="STK", currency="USD")
        exch = data.get("primaryExchange") or data.get("primaryExch") or data.get("exchange")
        return str(exch).upper().strip() or None
    except Exception as e:
        log.warning(f"resolve_exchange({symbol}) failed: {e}")
        return None
 
 
def place_order(symbol: str, side: str, qty: int) -> Dict:
    """Place a single market order. side = 'BUY' or 'SELL'."""
    payload = {"orders": [{
        "symbol":     symbol.upper(),
        "side":       side.lower(),
        "order_type": "MKT",
        "tif":        "DAY",
        "quantity":   qty,
    }]}
    log.info(f"  Placing order: {side} {qty} {symbol}")
    result = _post("/v1/orders/place", payload)
    log.info(f"  Order response: {result}")
    return result
 
 
def wait_for_fills(order_ids: List[int], timeout_secs: int = 60) -> Dict[int, str]:
    """Poll trades until all order IDs reach a terminal state."""
    TERMINAL = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}
    deadline = time.time() + timeout_secs
    statuses = {}
 
    while time.time() < deadline:
        for t in get_trades():
            oid = t.get("orderId") or t.get("order_id")
            if oid is not None:
                statuses[int(oid)] = str(t.get("status", ""))
 
        pending = [oid for oid in order_ids if statuses.get(oid) not in TERMINAL]
        if not pending:
            log.info(f"  All {len(order_ids)} order(s) reached terminal state")
            return statuses
 
        log.info(f"  Waiting for fills — {len(pending)} pending ...")
        time.sleep(3)
 
    log.warning(f"  Fill polling timed out after {timeout_secs}s")
    return statuses